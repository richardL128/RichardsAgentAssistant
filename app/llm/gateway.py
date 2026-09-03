"""The single model boundary used by all LifeAgent workflows."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from datetime import UTC, datetime
from typing import Any, TypeVar
from uuid import UUID, uuid4

from langchain_ollama import ChatOllama
from pydantic import BaseModel, ValidationError

from app.core.config import Settings, get_settings
from app.llm.contracts import InvocationResult, InvocationStatus, ModelCallTelemetry
from app.llm.parsing import (
    estimate_tokens,
    parse_model_json,
    reported_token_count,
    response_text,
)

ResponseT = TypeVar("ResponseT", bound=BaseModel)
AsyncModel = Any

# This is intentionally process-wide.  All workers have a separate process,
# while every gateway instance within one process must share this backpressure
# gate.  The safe Phase 1 default is one physical Ollama call at a time.
_MODEL_SEMAPHORE = asyncio.Semaphore(1)
_active_model_calls = 0
_max_active_model_calls = 0


class LLMGateway:
    """Invoke the configured local model and validate structured responses."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        chat_model: AsyncModel | None = None,
        model: AsyncModel | None = None,
    ) -> None:
        if chat_model is not None and model is not None:
            raise ValueError("provide only one injected chat model")
        self.settings = settings or get_settings()
        self._model = chat_model if chat_model is not None else model
        if self._model is None:
            self._model = self._build_chat_model()
        self.model_identity = self._model_identity()
        self.config_version = self._config_version()

    async def invoke_structured(
        self,
        *,
        prompt: str,
        response_model: type[ResponseT],
    ) -> InvocationResult[ResponseT]:
        """Invoke the model, optionally repair invalid JSON, and return a result."""

        request_id = uuid4()
        call_prompt = self._schema_prompt(prompt, response_model)
        estimated_input_tokens = estimate_tokens(call_prompt)
        if estimated_input_tokens > self.settings.ollama_max_input_tokens:
            return self._result(
                request_id=request_id,
                status=InvocationStatus.FAILED,
                error_code="input_token_budget_exceeded",
                error_diagnostic="Prompt exceeds the configured input token budget.",
            )

        telemetry: list[ModelCallTelemetry] = []
        raw_text = ""
        validation_diagnostic = "Response was not valid JSON."
        for attempt in range(1, self.settings.ollama_repair_attempts + 2):
            outcome = await self._invoke_once(
                request_id=request_id,
                attempt=attempt,
                prompt=call_prompt,
                response_schema=response_model.model_json_schema(),
                telemetry=telemetry,
            )
            if outcome.error_code is not None:
                return self._result(
                    request_id=request_id,
                    status=InvocationStatus.FAILED,
                    telemetry=telemetry,
                    raw_text=outcome.raw_text,
                    error_code=outcome.error_code,
                    error_diagnostic=outcome.error_diagnostic,
                )
            raw_text = outcome.raw_text
            try:
                parsed = parse_model_json(raw_text, response_model)
            except json.JSONDecodeError:
                parsed = None
                validation_diagnostic = "Response was not valid JSON."
                telemetry[-1].error_code = "invalid_json"
            except ValidationError as exc:
                parsed = None
                locations = sorted(
                    {
                        ".".join(str(part) for part in error["loc"]) or "$"
                        for error in exc.errors(include_input=False)
                    }
                )
                validation_diagnostic = (
                    "Response failed schema validation at " + ", ".join(locations) + "."
                )
                telemetry[-1].error_code = "schema_validation_failed"
            except (TypeError, ValueError):
                parsed = None
                validation_diagnostic = "Response could not be validated."
                telemetry[-1].error_code = "schema_validation_failed"
            if parsed is not None:
                telemetry[-1].output_valid = True
                return self._result(
                    request_id=request_id,
                    status=InvocationStatus.VALID,
                    output=parsed,
                    telemetry=telemetry,
                )
            if attempt <= self.settings.ollama_repair_attempts:
                call_prompt = self._repair_prompt(prompt, raw_text, response_model)
                if estimate_tokens(call_prompt) > self.settings.ollama_max_input_tokens:
                    return self._result(
                        request_id=request_id,
                        status=InvocationStatus.INVALID_OUTPUT,
                        telemetry=telemetry,
                        raw_text=raw_text,
                        error_code="analysis_invalid_output",
                        error_diagnostic=(
                            "Model output did not match the requested JSON schema. "
                            + validation_diagnostic
                        ),
                    )

        return self._result(
            request_id=request_id,
            status=InvocationStatus.INVALID_OUTPUT,
            telemetry=telemetry,
            raw_text=raw_text,
            error_code="analysis_invalid_output",
            error_diagnostic=(
                "Model output did not match the requested JSON schema. " + validation_diagnostic
            ),
        )

    async def _invoke_once(
        self,
        *,
        request_id: UUID,
        attempt: int,
        prompt: str,
        response_schema: dict[str, Any],
        telemetry: list[ModelCallTelemetry],
    ) -> _CallOutcome:
        started_at = datetime.now(UTC)
        started_monotonic = time.monotonic()
        model_started_at = started_at
        model_finished_at = started_at
        model_started_monotonic = started_monotonic
        model_finished_monotonic = started_monotonic
        raw_text = ""
        reported_input: int | None = None
        reported_output: int | None = None
        error_code: str | None = None
        error_diagnostic: str | None = None
        global _active_model_calls, _max_active_model_calls
        try:
            async with _MODEL_SEMAPHORE:
                model_started_at = datetime.now(UTC)
                model_started_monotonic = time.monotonic()
                _active_model_calls += 1
                _max_active_model_calls = max(_max_active_model_calls, _active_model_calls)
                try:
                    response = await asyncio.wait_for(
                        self._ainvoke(prompt, response_schema),
                        timeout=self.settings.ollama_timeout_seconds,
                    )
                finally:
                    model_finished_at = datetime.now(UTC)
                    model_finished_monotonic = time.monotonic()
                    _active_model_calls -= 1
            raw_text = response_text(response)
            reported_input = reported_token_count(response, input_tokens=True)
            reported_output = reported_token_count(response, input_tokens=False)
        except TimeoutError:
            error_code = "model_timeout"
            error_diagnostic = "Model request timed out."
        except Exception:
            # Do not include exception text: HTTP errors can contain URLs or
            # request material, and diagnostics are safe for logs/UI surfaces.
            error_code = "model_error"
            error_diagnostic = "Model request failed."
        finished_at = datetime.now(UTC)
        telemetry.append(
            ModelCallTelemetry(
                request_id=request_id,
                attempt=attempt,
                model_identity=self.model_identity,
                config_version=self.config_version,
                started_at=started_at,
                finished_at=finished_at,
                model_started_at=model_started_at,
                model_finished_at=model_finished_at,
                latency_ms=max(0.0, (time.monotonic() - started_monotonic) * 1000),
                queue_wait_ms=max(0.0, (model_started_monotonic - started_monotonic) * 1000),
                model_latency_ms=max(
                    0.0, (model_finished_monotonic - model_started_monotonic) * 1000
                ),
                input_characters=len(prompt),
                output_characters=len(raw_text),
                estimated_input_tokens=estimate_tokens(prompt),
                estimated_output_tokens=estimate_tokens(raw_text),
                reported_input_tokens=reported_input,
                reported_output_tokens=reported_output,
                output_valid=False,
                error_code=error_code,
            )
        )
        return _CallOutcome(
            raw_text=raw_text,
            error_code=error_code,
            error_diagnostic=error_diagnostic,
        )

    async def _ainvoke(self, prompt: str, response_schema: dict[str, Any]) -> Any:
        invoker = getattr(self._model, "ainvoke", None)
        if invoker is None or not callable(invoker):
            raise TypeError("injected chat model does not provide ainvoke")
        result = invoker(
            prompt,
            format=response_schema,
            options={
                "num_ctx": self.settings.ollama_num_ctx,
                "num_batch": self.settings.ollama_num_batch,
                "num_predict": self.settings.ollama_max_output_tokens,
                "temperature": 0.0,
                "seed": self.settings.ollama_seed,
            },
        )
        if inspect.isawaitable(result):
            return await result
        return result

    def _build_chat_model(self) -> ChatOllama:
        return ChatOllama(
            model=self.settings.ollama_model,
            base_url=self.settings.ollama_url,
            num_ctx=self.settings.ollama_num_ctx,
            num_predict=self.settings.ollama_max_output_tokens,
            temperature=0.0,
            seed=self.settings.ollama_seed,
            reasoning=self.settings.ollama_reasoning,
            format="json",
            async_client_kwargs={"timeout": self.settings.ollama_timeout_seconds},
        )

    def _model_identity(self) -> str:
        digest = self.settings.ollama_model_digest
        return f"{self.settings.ollama_model}@{digest}" if digest else self.settings.ollama_model

    def _config_version(self) -> str:
        config = {
            "base_url": self.settings.ollama_url,
            "model": self.settings.ollama_model,
            "model_digest": self.settings.ollama_model_digest,
            "max_concurrency": self.settings.ollama_max_concurrency,
            "num_ctx": self.settings.ollama_num_ctx,
            "num_batch": self.settings.ollama_num_batch,
            "max_output_tokens": self.settings.ollama_max_output_tokens,
            "timeout_seconds": self.settings.ollama_timeout_seconds,
            "max_input_tokens": self.settings.ollama_max_input_tokens,
            "repair_attempts": self.settings.ollama_repair_attempts,
            "temperature": 0.0,
            "seed": self.settings.ollama_seed,
            "reasoning": self.settings.ollama_reasoning,
        }
        serialized = json.dumps(config, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _schema_prompt(original_prompt: str, response_model: type[BaseModel]) -> str:
        schema = response_model.model_json_schema()
        encoded_schema = json.dumps(schema, sort_keys=True, separators=(",", ":"))
        return (
            "Return only one valid JSON object matching the supplied JSON schema. "
            "Do not include markdown, commentary, tool calls, or fields absent from the schema.\n"
            f"JSON schema:\n{encoded_schema}\nRequest:\n{original_prompt}"
        )

    @staticmethod
    def reset_concurrency_metrics() -> None:
        """Reset non-secret process metrics before a benchmark probe."""

        global _max_active_model_calls
        if _active_model_calls != 0:
            raise RuntimeError("cannot reset concurrency metrics while a model call is active")
        _max_active_model_calls = 0

    @staticmethod
    def concurrency_metrics() -> dict[str, int]:
        """Return current/peak physical model calls for acceptance evidence."""

        return {
            "active": _active_model_calls,
            "peak": _max_active_model_calls,
            "limit": 1,
        }

    @staticmethod
    def _repair_prompt(
        original_prompt: str,
        invalid_output: str,
        response_model: type[BaseModel],
    ) -> str:
        schema = response_model.model_json_schema()
        encoded_schema = json.dumps(schema, sort_keys=True, separators=(",", ":"))
        return (
            "Repair the previous response. Return only one valid JSON object "
            "matching this JSON schema; do not include markdown, commentary, or tool calls.\n"
            f"Original request:\n{original_prompt}\nPrevious response:\n{invalid_output}\n"
            f"{encoded_schema}"
        )

    @staticmethod
    def _result(
        *,
        request_id: UUID,
        status: InvocationStatus,
        output: BaseModel | None = None,
        telemetry: list[ModelCallTelemetry] | None = None,
        raw_text: str = "",
        error_code: str | None = None,
        error_diagnostic: str | None = None,
    ) -> InvocationResult[Any]:
        return InvocationResult[Any](
            request_id=request_id,
            status=status,
            output=output,
            telemetry=telemetry or [],
            raw_text=raw_text,
            error_code=error_code,
            error_diagnostic=error_diagnostic,
        )


class _CallOutcome:
    def __init__(
        self,
        *,
        raw_text: str,
        error_code: str | None,
        error_diagnostic: str | None,
    ) -> None:
        self.raw_text = raw_text
        self.error_code = error_code
        self.error_diagnostic = error_diagnostic


__all__ = ["LLMGateway"]
