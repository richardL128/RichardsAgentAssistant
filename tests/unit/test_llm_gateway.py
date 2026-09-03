"""Unit coverage for the Phase 1 model gateway."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from app.core.config import Settings
from app.llm.contracts import InvocationStatus
from app.llm.gateway import LLMGateway


class Answer(BaseModel):
    answer: str


class FakeChatModel:
    def __init__(self, responses: Sequence[object], *, delay: float = 0.0) -> None:
        self.responses = iter(responses)
        self.delay = delay
        self.prompts: list[str] = []
        self.formats: list[dict[str, object]] = []
        self.options: list[dict[str, object]] = []

    async def ainvoke(self, prompt: str, **kwargs: object) -> object:
        self.prompts.append(prompt)
        response_format = kwargs.get("format")
        assert isinstance(response_format, dict)
        self.formats.append(response_format)
        options = kwargs.get("options")
        assert isinstance(options, dict)
        self.options.append(options)
        if self.delay:
            await asyncio.sleep(self.delay)
        return next(self.responses)


@pytest.mark.asyncio
async def test_valid_response_records_estimated_and_reported_telemetry() -> None:
    fake = FakeChatModel(
        [
            AIMessage(
                content='{"answer":"ok"}',
                usage_metadata={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
            )
        ]
    )
    result = await LLMGateway(Settings(), chat_model=fake).invoke_structured(
        prompt="four words", response_model=Answer
    )

    assert result.status is InvocationStatus.VALID
    assert result.output == Answer(answer="ok")
    assert len(result.telemetry) == 1
    assert result.telemetry[0].output_valid is True
    assert result.telemetry[0].reported_input_tokens == 2
    assert result.telemetry[0].reported_output_tokens == 3
    assert result.telemetry[0].estimated_input_tokens >= 3
    assert result.telemetry[0].input_characters > len("four words")
    assert result.telemetry[0].estimated_output_tokens == 4
    assert fake.formats[0]["title"] == "Answer"
    assert fake.options[0]["num_batch"] == 32


@pytest.mark.asyncio
async def test_invalid_response_uses_exactly_one_repair_call() -> None:
    fake = FakeChatModel(["not json", '{"answer":"repaired"}'])
    result = await LLMGateway(
        Settings(ollama_repair_attempts=1), chat_model=fake
    ).invoke_structured(prompt="extract an answer", response_model=Answer)

    assert result.status is InvocationStatus.VALID
    assert result.output == Answer(answer="repaired")
    assert len(fake.prompts) == 2
    assert [item.attempt for item in result.telemetry] == [1, 2]


@pytest.mark.asyncio
async def test_terminal_invalid_response_is_deterministic() -> None:
    fake = FakeChatModel(["bad", "still bad"])
    result = await LLMGateway(
        Settings(ollama_repair_attempts=1), chat_model=fake
    ).invoke_structured(prompt="extract an answer", response_model=Answer)

    assert result.status is InvocationStatus.INVALID_OUTPUT
    assert result.error_code == "analysis_invalid_output"
    assert result.error_diagnostic == (
        "Model output did not match the requested JSON schema. Response was not valid JSON."
    )
    assert len(result.telemetry) == 2
    assert all(item.output_valid is False for item in result.telemetry)
    assert all(item.error_code == "invalid_json" for item in result.telemetry)


@pytest.mark.asyncio
async def test_input_token_budget_rejects_before_model_call() -> None:
    fake = FakeChatModel(['{"answer":"never called"}'])
    settings = Settings(ollama_max_input_tokens=1)
    result = await LLMGateway(settings, chat_model=fake).invoke_structured(
        prompt="this prompt is too long", response_model=Answer
    )

    assert result.status is InvocationStatus.FAILED
    assert result.error_code == "input_token_budget_exceeded"
    assert result.telemetry == []
    assert fake.prompts == []


@pytest.mark.asyncio
async def test_timeout_returns_safe_failure() -> None:
    fake = FakeChatModel(['{"answer":"late"}'], delay=0.05)
    result = await LLMGateway(
        Settings(ollama_timeout_seconds=0.001), chat_model=fake
    ).invoke_structured(prompt="answer", response_model=Answer)

    assert result.status is InvocationStatus.FAILED
    assert result.error_code == "model_timeout"
    assert result.error_diagnostic == "Model request timed out."
    assert len(result.telemetry) == 1
    assert result.telemetry[0].error_code == "model_timeout"


@pytest.mark.asyncio
async def test_semaphore_is_shared_across_gateway_instances() -> None:
    class ConcurrentFake(FakeChatModel):
        active = 0
        maximum = 0

        async def ainvoke(self, prompt: str, **kwargs: object) -> object:
            type(self).active += 1
            type(self).maximum = max(type(self).maximum, type(self).active)
            try:
                await asyncio.sleep(0.01)
                return '{"answer":"ok"}'
            finally:
                type(self).active -= 1

    first = ConcurrentFake([])
    second = ConcurrentFake([])
    gateway_one = LLMGateway(Settings(), chat_model=first)
    gateway_two = LLMGateway(Settings(), chat_model=second)
    LLMGateway.reset_concurrency_metrics()
    results = await asyncio.gather(
        gateway_one.invoke_structured(prompt="one", response_model=Answer),
        gateway_two.invoke_structured(prompt="two", response_model=Answer),
    )

    assert all(result.status is InvocationStatus.VALID for result in results)
    assert ConcurrentFake.maximum == 1
    assert LLMGateway.concurrency_metrics() == {"active": 0, "peak": 1, "limit": 1}
