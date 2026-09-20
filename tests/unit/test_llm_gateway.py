"""Unit coverage for the Phase 1 model gateway."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ValidationError, model_validator

from app.core.config import Settings
from app.llm.contracts import InvocationStatus
from app.llm.gateway import LLMGateway


class Answer(BaseModel):
    answer: str


class CitedAnswer(BaseModel):
    answer: str
    citation_ids: list[str] = []

    @model_validator(mode="after")
    def requires_citations_for_valid_answer(self) -> CitedAnswer:
        if not self.citation_ids:
            raise ValueError("valid answer requires citations")
        return self


class FakeChatModel:
    def __init__(self, responses: Sequence[object], *, delay: float = 0.0) -> None:
        self.responses = iter(responses)
        self.delay = delay
        self.prompts: list[str] = []
        self.formats: list[object] = []
        self.options: list[dict[str, object]] = []
        self.keep_alive: list[object] = []

    async def ainvoke(self, prompt: str, **kwargs: object) -> object:
        self.prompts.append(prompt)
        response_format = kwargs.get("format")
        self.formats.append(response_format)
        options = kwargs.get("options")
        assert isinstance(options, dict)
        self.options.append(cast(dict[str, object], options))
        self.keep_alive.append(kwargs.get("keep_alive"))
        if self.delay:
            await asyncio.sleep(self.delay)
        return next(self.responses)


class NativeFakeChatModel:
    def __init__(self, responses: Sequence[object], *, delay: float = 0.0) -> None:
        self.responses = iter(responses)
        self.delay = delay
        self.bound_tools: list[list[dict[str, object]]] = []
        self.messages: list[list[object]] = []
        self.kwargs: list[dict[str, object]] = []

    def bind_tools(self, tools: Sequence[dict[str, object]]) -> NativeFakeChatModel:
        self.bound_tools.append([dict(tool) for tool in tools])
        return self

    async def ainvoke(self, messages: Sequence[object], **kwargs: object) -> object:
        self.messages.append(list(messages))
        self.kwargs.append(dict(kwargs))
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
    assert isinstance(fake.formats[0], dict)
    assert fake.formats[0]["title"] == "Answer"
    assert fake.options[0]["num_batch"] == 32
    assert fake.keep_alive == ["300s"]


@pytest.mark.asyncio
async def test_structured_response_can_use_plain_json_transport() -> None:
    fake = FakeChatModel([AIMessage(content='{"answer":"ok"}')])
    result = await LLMGateway(
        Settings(ollama_structured_output_transport="json"), chat_model=fake
    ).invoke_structured(prompt="answer", response_model=Answer)

    assert result.status is InvocationStatus.VALID
    assert result.output == Answer(answer="ok")
    assert fake.formats == ["json"]
    assert "JSON schema:" in fake.prompts[0]


def test_structured_response_transport_changes_config_version() -> None:
    schema_gateway = LLMGateway(Settings(), chat_model=FakeChatModel([]))
    json_gateway = LLMGateway(
        Settings(ollama_structured_output_transport="json"),
        chat_model=FakeChatModel([]),
    )

    assert schema_gateway.config_version != json_gateway.config_version
    assert schema_gateway.native_config_version == json_gateway.native_config_version


def test_reasoning_setting_changes_structured_and_native_config_versions() -> None:
    reasoning_disabled = LLMGateway(Settings(), chat_model=FakeChatModel([]))
    reasoning_enabled = LLMGateway(
        Settings(ollama_reasoning=True),
        chat_model=FakeChatModel([]),
    )

    assert reasoning_disabled.config_version != reasoning_enabled.config_version
    assert reasoning_disabled.native_config_version != reasoning_enabled.native_config_version


def test_structured_response_transport_setting_is_strict_and_diagnosed() -> None:
    settings = Settings()

    assert settings.ollama_structured_output_transport == "json_schema"
    assert settings.safe_diagnostics()["ollama_structured_output_transport"] == "json_schema"
    with pytest.raises(ValidationError):
        Settings(ollama_structured_output_transport="schema")  # type: ignore[arg-type]


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
    assert fake.keep_alive == ["300s", "300s"]


@pytest.mark.asyncio
async def test_repair_prompt_includes_safe_validation_reason() -> None:
    fake = FakeChatModel(
        [
            '{"answer":"secret source text should stay out of validation reason"}',
            '{"answer":"repaired","citation_ids":["fragment-1"]}',
        ]
    )

    result = await LLMGateway(
        Settings(ollama_repair_attempts=1), chat_model=fake
    ).invoke_structured(prompt="extract a cited answer", response_model=CitedAnswer)

    assert result.status is InvocationStatus.VALID
    assert result.output == CitedAnswer(answer="repaired", citation_ids=["fragment-1"])
    assert len(fake.prompts) == 2
    repair_prompt = fake.prompts[1]
    validation_reason = repair_prompt.split("Original request:", maxsplit=1)[0]
    assert "Validation failure reason:" in validation_reason
    assert "Response failed schema validation" in validation_reason
    assert "$: Value error, valid answer requires citations" in validation_reason
    assert "secret source text" not in validation_reason
    assert [item.error_code for item in result.telemetry] == [
        "schema_validation_failed",
        None,
    ]


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
    settings = Settings(
        ollama_max_input_tokens=3,
        conversation_compaction_target_tokens=1,
        conversation_compaction_trigger_tokens=2,
        conversation_recent_tail_max_tokens=1,
    )
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


@pytest.mark.asyncio
async def test_native_invocation_preserves_tool_calls_without_json_forcing() -> None:
    tool_schema = {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search the web.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }
    response = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "search_web",
                "args": {"query": "current weather"},
                "id": "call_1",
            }
        ],
        usage_metadata={"input_tokens": 8, "output_tokens": 4, "total_tokens": 12},
    )
    fake = NativeFakeChatModel([response])

    result = await LLMGateway(Settings(), chat_model=fake).invoke_native(
        messages=[
            SystemMessage(content="You are a tool-capable assistant."),
            HumanMessage(content="Search for current weather."),
        ],
        tools=[tool_schema],
    )

    assert result.status is InvocationStatus.VALID
    output = result.output
    assert isinstance(output, AIMessage)
    assert output is response
    assert output.tool_calls[0]["name"] == "search_web"
    assert output.tool_calls[0]["args"] == {"query": "current weather"}
    assert fake.bound_tools == [[tool_schema]]
    messages = cast(list[BaseMessage], fake.messages[0])
    assert [message.type for message in messages] == ["system", "human"]
    assert "format" not in fake.kwargs[0]
    assert fake.kwargs[0]["keep_alive"] == "300s"
    options = fake.kwargs[0]["options"]
    assert isinstance(options, dict)
    assert options["num_batch"] == 32
    assert options["temperature"] == 0.0
    assert options["seed"] == 1729
    assert result.telemetry[0].output_valid is True
    assert result.telemetry[0].reported_input_tokens == 8
    assert result.telemetry[0].reported_output_tokens == 4
    assert result.estimated_input_tokens == result.telemetry[0].estimated_input_tokens
    assert result.reported_input_tokens == 8
    assert result.reported_output_tokens == 4


@pytest.mark.asyncio
async def test_native_invocation_preserves_arbitrary_assistant_content() -> None:
    content: list[str | dict[Any, Any]] = [
        {"type": "text", "text": "I can answer directly "},
        {"type": "text", "text": "without a tool."},
    ]
    response = AIMessage(content=content)
    fake = NativeFakeChatModel([response])

    result = await LLMGateway(Settings(), chat_model=fake).invoke_native(
        messages=[HumanMessage(content="Just answer normally.")],
        tools=[],
    )

    assert result.status is InvocationStatus.VALID
    output = result.output
    assert isinstance(output, AIMessage)
    assert output is response
    assert output.content == content
    assert result.raw_text == "I can answer directly without a tool."
    assert fake.bound_tools == []


@pytest.mark.asyncio
async def test_native_invocation_preserves_private_reasoning_metadata() -> None:
    response = AIMessage(
        content="Visible answer.",
        additional_kwargs={"reasoning_content": "private native thinking"},
        usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
    )
    fake = NativeFakeChatModel([response])

    result = await LLMGateway(Settings(), chat_model=fake).invoke_native(
        messages=[HumanMessage(content="Answer with reasoning enabled.")],
    )

    assert result.status is InvocationStatus.VALID
    output = result.output
    assert isinstance(output, AIMessage)
    assert output is response
    assert output.additional_kwargs["reasoning_content"] == "private native thinking"
    assert result.reported_input_tokens == 3
    assert result.reported_output_tokens == 2


@pytest.mark.asyncio
async def test_native_input_token_budget_rejects_before_model_call() -> None:
    fake = NativeFakeChatModel([AIMessage(content="never called")])

    result = await LLMGateway(
        Settings(
            ollama_max_input_tokens=3,
            conversation_compaction_target_tokens=1,
            conversation_compaction_trigger_tokens=2,
            conversation_recent_tail_max_tokens=1,
        ),
        chat_model=fake,
    ).invoke_native(messages=[HumanMessage(content="this native prompt is too long")])

    assert result.status is InvocationStatus.FAILED
    assert result.error_code == "input_token_budget_exceeded"
    assert result.estimated_input_tokens > 0
    assert result.reported_input_tokens is None
    assert result.reported_output_tokens is None
    assert result.telemetry == []
    assert fake.messages == []


@pytest.mark.asyncio
async def test_native_input_budget_counts_prior_tool_call_arguments() -> None:
    fake = NativeFakeChatModel([AIMessage(content="never called")])
    prior_tool_call = AIMessage(
        content="I will inspect the supplied query.",
        tool_calls=[
            {
                "id": "call-1",
                "name": "search",
                "args": {"query": "large-value " * 100},
            }
        ],
    )

    result = await LLMGateway(
        Settings(
            ollama_max_input_tokens=40,
            conversation_compaction_target_tokens=10,
            conversation_compaction_trigger_tokens=20,
            conversation_recent_tail_max_tokens=10,
        ),
        chat_model=fake,
    ).invoke_native(messages=[HumanMessage(content="search"), prior_tool_call])

    assert result.status is InvocationStatus.FAILED
    assert result.error_code == "input_token_budget_exceeded"
    assert fake.messages == []


@pytest.mark.asyncio
async def test_native_timeout_returns_safe_failure() -> None:
    fake = NativeFakeChatModel([AIMessage(content="late")], delay=0.05)

    result = await LLMGateway(
        Settings(ollama_timeout_seconds=0.001), chat_model=fake
    ).invoke_native(messages=[HumanMessage(content="answer")])

    assert result.status is InvocationStatus.FAILED
    assert result.error_code == "model_timeout"
    assert result.error_diagnostic == "Model request timed out."
    assert len(result.telemetry) == 1
    assert result.telemetry[0].error_code == "model_timeout"
