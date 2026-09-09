"""Redacted readiness boundary for the host-managed Ollama runtime."""

from __future__ import annotations

import asyncio
from typing import Literal, Protocol, cast

import httpx
from pydantic import BaseModel, ConfigDict

from app.core.config import Settings, get_settings

OllamaRuntimeErrorCode = Literal[
    "unavailable",
    "model_missing",
    "digest_mismatch",
    "malformed_response",
    "startup_timeout",
]


class OllamaRuntimeReady(BaseModel):
    """Non-secret model identity returned after a successful readiness probe."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str
    digest: str | None = None


class _OllamaModelTag(BaseModel):
    name: str
    digest: str | None = None


class _OllamaTagsResponse(BaseModel):
    models: list[_OllamaModelTag]


class OllamaRuntimeError(RuntimeError):
    """Typed safe failure that never includes endpoint or response details."""

    def __init__(self, code: OllamaRuntimeErrorCode) -> None:
        self.code = code
        self.diagnostic = {
            "unavailable": "The host Ollama API is unavailable.",
            "model_missing": "The configured Qwen model is not installed.",
            "digest_mismatch": "The configured Qwen model digest does not match.",
            "malformed_response": "The host Ollama API returned an invalid model list.",
            "startup_timeout": "The host Ollama readiness check timed out.",
        }[code]
        super().__init__(self.diagnostic)


class OllamaRuntimeHttpClient(Protocol):
    async def get(self, url: str) -> httpx.Response: ...


class OllamaRuntime:
    """Check the fixed Ollama API and coalesce simultaneous readiness probes."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        http_client: OllamaRuntimeHttpClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._http_client = http_client
        self._inflight_lock = asyncio.Lock()
        self._inflight: asyncio.Task[OllamaRuntimeReady] | None = None

    async def ensure_ready(self) -> OllamaRuntimeReady:
        """Verify API reachability and the exact configured model identity."""

        async with self._inflight_lock:
            task = self._inflight
            if task is None or task.done():
                task = asyncio.create_task(self._bounded_probe(), name="ollama-readiness")
                self._inflight = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                async with self._inflight_lock:
                    if self._inflight is task:
                        self._inflight = None

    async def _bounded_probe(self) -> OllamaRuntimeReady:
        try:
            return await asyncio.wait_for(
                self._probe(),
                timeout=self._settings.ollama_startup_timeout_seconds,
            )
        except TimeoutError as exc:
            raise OllamaRuntimeError("startup_timeout") from exc

    async def _probe(self) -> OllamaRuntimeReady:
        owns_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(
            base_url=self._settings.ollama_url,
            timeout=httpx.Timeout(self._settings.ollama_startup_timeout_seconds),
        )
        try:
            try:
                response = await client.get("/api/tags")
                response.raise_for_status()
            except httpx.TimeoutException as exc:
                raise OllamaRuntimeError("startup_timeout") from exc
            except httpx.HTTPError as exc:
                raise OllamaRuntimeError("unavailable") from exc
            try:
                payload = _OllamaTagsResponse.model_validate(response.json())
            except (TypeError, ValueError) as exc:
                raise OllamaRuntimeError("malformed_response") from exc
        finally:
            if owns_client:
                await cast(httpx.AsyncClient, client).aclose()

        expected_model = self._settings.ollama_model
        installed: _OllamaModelTag | None = None
        for item in payload.models:
            if item.name == expected_model:
                installed = item
                break
        if installed is None:
            raise OllamaRuntimeError("model_missing")

        installed_digest = installed.digest
        expected_digest = self._settings.ollama_model_digest
        if expected_digest is not None and installed_digest != expected_digest:
            raise OllamaRuntimeError("digest_mismatch")
        return OllamaRuntimeReady(model=expected_model, digest=installed_digest)


__all__ = [
    "OllamaRuntime",
    "OllamaRuntimeError",
    "OllamaRuntimeErrorCode",
    "OllamaRuntimeHttpClient",
    "OllamaRuntimeReady",
]
