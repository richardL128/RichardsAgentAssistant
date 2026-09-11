from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from app.agents.harness import (
    AgentHarnessEvent,
    NativeTool,
    ToolExecutionResult,
    run_native_tool_loop,
)


class Gateway:
    def __init__(self, responses: Sequence[AIMessage]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[BaseMessage, ...]] = []
        self.tools: list[Sequence[Mapping[str, object]]] = []

    async def invoke_tools(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Mapping[str, object]],
    ) -> AIMessage:
        self.calls.append(tuple(messages))
        self.tools.append(tools)
        return self.responses.pop(0)


async def collect(events: list[AgentHarnessEvent], event: AgentHarnessEvent) -> None:
    events.append(event)


def tool_schema(name: str) -> Mapping[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "test tool",
            "parameters": {"type": "object", "properties": {}},
        },
    }


@pytest.mark.asyncio
async def test_arbitrary_answer_uses_human_message_and_emits_final_response() -> None:
    events: list[AgentHarnessEvent] = []
    gateway = Gateway([AIMessage(content="Any safe answer.")])

    result = await run_native_tool_loop(
        gateway=gateway,
        user_input="anything at all, no command grammar",
        event_sink=lambda event: collect(events, event),
    )

    assert result.status == "completed"
    assert result.final_response == "Any safe answer."
    assert isinstance(gateway.calls[0][1], HumanMessage)
    assert gateway.calls[0][1].content == "anything at all, no command grammar"
    assert [(event.kind, event.content) for event in events] == [
        ("final_response", "Any safe answer."),
    ]


@pytest.mark.asyncio
async def test_visible_text_strips_reasoning_blocks_tags_and_prefixes() -> None:
    events: list[AgentHarnessEvent] = []
    gateway = Gateway(
        [
            AIMessage(
                content=(
                    "<think>private chain of thought</think>\n"
                    "</think>\n"
                    "Thinking: Visible answer.\n"
                    "I think this ordinary sentence should remain."
                )
            )
        ]
    )

    result = await run_native_tool_loop(
        gateway=gateway,
        user_input="answer safely",
        event_sink=lambda event: collect(events, event),
    )

    assert result.final_response == (
        "Visible answer.\nI think this ordinary sentence should remain."
    )
    assert events[0].content == result.final_response
    assert "think>" not in result.final_response
    assert "private chain" not in result.final_response


@pytest.mark.asyncio
async def test_visible_text_strips_reasoning_content_blocks() -> None:
    events: list[AgentHarnessEvent] = []
    gateway = Gateway(
        [
            AIMessage(
                content=[
                    {"type": "reasoning", "text": "hidden scratchpad"},
                    {"type": "text", "text": "<thinking>hidden</thinking>\nFinal answer."},
                    "</reasoning>",
                ]
            )
        ]
    )

    result = await run_native_tool_loop(
        gateway=gateway,
        user_input="answer safely",
        event_sink=lambda event: collect(events, event),
    )

    assert result.final_response == "Final answer."
    assert events[0].content == "Final answer."
    assert "hidden" not in result.final_response


@pytest.mark.asyncio
async def test_visible_text_strips_reasoning_before_orphan_closing_tag() -> None:
    gateway = Gateway(
        [AIMessage(content="private scratchpad without opener\n</think>\nVisible answer.")]
    )

    result = await run_native_tool_loop(gateway=gateway, user_input="answer safely")

    assert result.final_response == "Visible answer."
    assert "scratchpad" not in result.final_response


@pytest.mark.asyncio
async def test_tool_call_result_and_answer_events_are_ordered() -> None:
    events: list[AgentHarnessEvent] = []

    async def lookup(arguments: Mapping[str, object]) -> object:
        return {"echo": arguments["query"]}

    gateway = Gateway(
        [
            AIMessage(
                content="I will look that up.",
                tool_calls=[
                    {
                        "id": "call-1",
                        "name": "lookup",
                        "args": {"query": "calendar"},
                    }
                ],
            ),
            AIMessage(content="The answer is calendar."),
        ]
    )

    result = await run_native_tool_loop(
        gateway=gateway,
        user_input="look up calendar",
        tools=(NativeTool(schema=tool_schema("lookup"), handler=lookup),),
        event_sink=lambda event: collect(events, event),
    )

    assert result.status == "completed"
    assert [event.kind for event in events] == [
        "tool_call",
        "tool_result",
        "final_response",
    ]
    assert events[0].content == "I will look that up."
    assert events[0].tool_name == "lookup"
    assert events[0].args_json == '{"query":"calendar"}'
    assert events[1].result_json == '{"content":{"echo":"calendar"},"status":"succeeded"}'
    assert isinstance(result.messages[3], ToolMessage)
    assert result.messages[3].content == '{"content":{"echo":"calendar"},"status":"succeeded"}'
    assert gateway.calls[1][-1].content == result.messages[3].content


@pytest.mark.asyncio
async def test_tool_call_description_is_sanitized_but_tool_result_stays_internal() -> None:
    events: list[AgentHarnessEvent] = []

    async def lookup(_: Mapping[str, object]) -> object:
        return {"secret_uuid": "11111111-2222-3333-4444-555555555555"}

    gateway = Gateway(
        [
            AIMessage(
                content="<think>hidden</think>\nReasoning: I will check that.",
                tool_calls=[{"id": "call-1", "name": "lookup", "args": {}}],
            ),
            AIMessage(content="Done."),
        ]
    )

    result = await run_native_tool_loop(
        gateway=gateway,
        user_input="look up private item",
        tools=(NativeTool(schema=tool_schema("lookup"), handler=lookup),),
        event_sink=lambda event: collect(events, event),
    )

    assert result.status == "completed"
    assert events[0].content == "I will check that."
    assert events[1].result_json == (
        '{"content":{"secret_uuid":"11111111-2222-3333-4444-555555555555"},"status":"succeeded"}'
    )
    assert gateway.calls[1][-1].content == events[1].result_json


@pytest.mark.asyncio
async def test_unknown_and_malformed_tool_calls_return_tool_errors_for_recovery() -> None:
    events: list[AgentHarnessEvent] = []
    gateway = Gateway(
        [
            AIMessage(
                content="Trying tools.",
                tool_calls=[
                    {"id": "unknown-1", "name": "missing", "args": {"x": 1}},
                ],
                invalid_tool_calls=[
                    {
                        "id": "bad-1",
                        "name": "known",
                        "args": "not-json-object",
                        "error": "tool arguments must be an object",
                    }
                ],
            ),
            AIMessage(content="Recovered after tool errors."),
        ]
    )

    async def known(_: Mapping[str, object]) -> object:
        raise AssertionError("malformed args should not reach the handler")

    result = await run_native_tool_loop(
        gateway=gateway,
        user_input="call tools",
        tools=(NativeTool(schema=tool_schema("known"), handler=known),),
        event_sink=lambda event: collect(events, event),
    )

    assert result.status == "completed"
    assert [event.kind for event in events] == [
        "tool_call",
        "tool_error",
        "tool_call",
        "tool_error",
        "final_response",
    ]
    assert events[0].content == "Trying tools."
    assert events[1].error == "unknown tool: missing"
    assert events[3].error == "tool arguments must be an object"
    assert all(isinstance(message, ToolMessage) for message in result.messages[3:5])
    assert gateway.calls[1][-2].content == '{"error":"unknown tool: missing","status":"error"}'
    assert gateway.calls[1][-1].content == (
        '{"error":"tool arguments must be an object","status":"error"}'
    )


@pytest.mark.asyncio
async def test_tool_failure_and_review_required_result_are_visible_tool_messages() -> None:
    events: list[AgentHarnessEvent] = []

    async def fail(_: Mapping[str, object]) -> object:
        raise ValueError("temporary failure")

    async def review(arguments: Mapping[str, object]) -> object:
        return ToolExecutionResult(
            {"proposed": arguments["item"]},
            status="review_required",
        )

    gateway = Gateway(
        [
            AIMessage(
                content="Two tool calls.",
                tool_calls=[
                    {"id": "fail-1", "name": "fail", "args": {}},
                    {"id": "review-1", "name": "review", "args": {"item": "archive page"}},
                ],
            ),
            AIMessage(content="I prepared the review item."),
        ]
    )

    result = await run_native_tool_loop(
        gateway=gateway,
        user_input="prepare side effect",
        tools=(
            NativeTool(schema=tool_schema("fail"), handler=fail),
            NativeTool(schema=tool_schema("review"), handler=review),
        ),
        event_sink=lambda event: collect(events, event),
    )

    assert result.status == "completed"
    assert events[0].content == "Two tool calls."
    assert events[1].kind == "tool_error"
    assert events[1].error == "tool execution failed (ValueError)"
    assert events[3].kind == "tool_result"
    assert events[3].result_json == (
        '{"content":{"proposed":"archive page"},"status":"review_required"}'
    )


@pytest.mark.asyncio
async def test_default_turn_budget_stops_before_turn_51() -> None:
    async def lookup(_: Mapping[str, object]) -> object:
        return {"ok": True}

    gateway = Gateway(
        [
            AIMessage(
                content="Still working.",
                tool_calls=[{"id": f"call-{index}", "name": "lookup", "args": {}}],
            )
            for index in range(50)
        ]
        + [AIMessage(content="This would require turn 51.")]
    )

    result = await run_native_tool_loop(
        gateway=gateway,
        user_input="keep checking",
        tools=(NativeTool(schema=tool_schema("lookup"), handler=lookup),),
    )

    assert result.status == "turn_limit"
    assert result.turns == 50
    assert len(gateway.calls) == 50
    assert len(gateway.responses) == 1


@pytest.mark.asyncio
async def test_turn_limit_emits_final_response_without_unbounded_looping() -> None:
    events: list[AgentHarnessEvent] = []

    async def lookup(_: Mapping[str, object]) -> object:
        return {"ok": True}

    gateway = Gateway(
        [
            AIMessage(
                content="Still working.",
                tool_calls=[{"id": "call-1", "name": "lookup", "args": {}}],
            )
        ]
    )

    result = await run_native_tool_loop(
        gateway=gateway,
        user_input="keep looping",
        tools=(NativeTool(schema=tool_schema("lookup"), handler=lookup),),
        max_turns=1,
        event_sink=lambda event: collect(events, event),
    )

    assert result.status == "turn_limit"
    assert result.turns == 1
    assert result.final_response == (
        "The agent reached its turn limit before producing a final response."
    )
    assert [event.kind for event in events] == [
        "tool_call",
        "tool_result",
        "final_response",
    ]


@pytest.mark.asyncio
async def test_event_sink_failure_stops_the_turn_instead_of_hiding_missing_messages() -> None:
    seen = 0
    gateway = Gateway([AIMessage(content="Done.")])

    async def failing_sink(_: AgentHarnessEvent) -> None:
        nonlocal seen
        seen += 1
        raise RuntimeError("sink unavailable")

    with pytest.raises(RuntimeError, match="sink unavailable"):
        await run_native_tool_loop(
            gateway=gateway,
            user_input="hello",
            event_sink=failing_sink,
        )

    assert seen == 1
