from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from app.agents.harness import (
    AgentHarnessEvent,
    NativeTool,
    ToolExecutionResult,
    _pending_elapsed_schedule,
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


def test_model_pending_schedule_uses_fixed_elapsed_buckets() -> None:
    assert _pending_elapsed_schedule((8.0, 20.0, 45.0), 30.0) == (
        8.0,
        20.0,
        45.0,
        75.0,
        105.0,
        135.0,
        165.0,
        195.0,
        225.0,
        255.0,
        285.0,
        315.0,
    )


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
        ("model_turn_started", None),
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
    assert events[-1].content == result.final_response
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
    assert events[-1].content == "Final answer."
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
        "model_turn_started",
        "tool_call",
        "tool_result",
        "model_turn_started",
        "final_response",
    ]
    assert events[1].content == "I will look that up."
    assert events[1].tool_name == "lookup"
    assert events[1].args_json == '{"query":"calendar"}'
    assert events[2].result_json == '{"content":{"echo":"calendar"},"status":"succeeded"}'
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
    assert events[1].content == "I will check that."
    assert events[2].result_json == (
        '{"content":{"secret_uuid":"11111111-2222-3333-4444-555555555555"},"status":"succeeded"}'
    )
    assert gateway.calls[1][-1].content == events[2].result_json


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
        "model_turn_started",
        "tool_call",
        "tool_error",
        "tool_call",
        "tool_error",
        "model_turn_started",
        "final_response",
    ]
    assert events[1].content == "Trying tools."
    assert events[2].error == "unknown tool: missing"
    assert events[4].error == "tool arguments must be an object"
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
    assert events[1].content == "Two tool calls."
    assert events[2].kind == "tool_error"
    assert events[2].error == "tool execution failed (ValueError)"
    assert events[4].kind == "tool_result"
    assert events[4].result_json == (
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
        "model_turn_started",
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
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_model_turn_pending_events_are_bounded_and_ordered_before_final() -> None:
    events: list[AgentHarnessEvent] = []
    release = asyncio.Event()

    class SlowGateway(Gateway):
        async def invoke_tools(
            self,
            messages: Sequence[BaseMessage],
            tools: Sequence[Mapping[str, object]],
        ) -> AIMessage:
            self.calls.append(tuple(messages))
            self.tools.append(tools)
            await release.wait()
            return AIMessage(content="Eventually done.")

    async def sink(event: AgentHarnessEvent) -> None:
        events.append(event)
        if len(events) == 13:
            release.set()

    gateway = SlowGateway([])

    result = await run_native_tool_loop(
        gateway=gateway,
        user_input="take your time",
        event_sink=sink,
        _model_pending_elapsed_seconds=(0.0,) * 12,
    )

    assert result.final_response == "Eventually done."
    assert [event.kind for event in events[:13]] == [
        "model_turn_started",
        *["model_turn_pending"] * 12,
    ]
    assert events[-1].kind == "final_response"
    assert all(event.turn == 1 for event in events)
    assert all(event.turn_limit == 50 for event in events[:13])


@pytest.mark.asyncio
async def test_model_pending_budget_is_shared_across_all_turns_in_one_request() -> None:
    events: list[AgentHarnessEvent] = []
    release_first = asyncio.Event()
    release_second = asyncio.Event()
    second_started = asyncio.Event()

    class TwoTurnSlowGateway(Gateway):
        async def invoke_tools(
            self,
            messages: Sequence[BaseMessage],
            tools: Sequence[Mapping[str, object]],
        ) -> AIMessage:
            self.calls.append(tuple(messages))
            self.tools.append(tools)
            if len(self.calls) == 1:
                await release_first.wait()
                return AIMessage(
                    content="I will check.",
                    tool_calls=[{"id": "call-1", "name": "lookup", "args": {}}],
                )
            second_started.set()
            await release_second.wait()
            return AIMessage(content="Done after two turns.")

    async def lookup(_: Mapping[str, object]) -> object:
        return {"ok": True}

    async def sink(event: AgentHarnessEvent) -> None:
        events.append(event)
        if len([item for item in events if item.kind == "model_turn_pending"]) == 12:
            release_first.set()

    gateway = TwoTurnSlowGateway([])
    task = asyncio.create_task(
        run_native_tool_loop(
            gateway=gateway,
            user_input="use two turns",
            tools=(NativeTool(schema=tool_schema("lookup"), handler=lookup),),
            event_sink=sink,
            _model_pending_elapsed_seconds=(0.0,) * 12,
            _model_pending_repeat_seconds=0.001,
        )
    )
    await asyncio.wait_for(second_started.wait(), timeout=1.0)
    await asyncio.sleep(0.01)

    assert len([event for event in events if event.kind == "model_turn_pending"]) == 12
    release_second.set()
    result = await asyncio.wait_for(task, timeout=1.0)

    assert result.final_response == "Done after two turns."
    assert [event.turn for event in events if event.kind == "model_turn_started"] == [1, 2]


@pytest.mark.asyncio
async def test_model_invocation_is_cancelled_cleanly_on_parent_cancellation() -> None:
    events: list[AgentHarnessEvent] = []
    started = asyncio.Event()
    cancelled = asyncio.Event()
    pending_seen = asyncio.Event()

    class HangingGateway(Gateway):
        async def invoke_tools(
            self,
            messages: Sequence[BaseMessage],
            tools: Sequence[Mapping[str, object]],
        ) -> AIMessage:
            self.calls.append(tuple(messages))
            self.tools.append(tools)
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    gateway = HangingGateway([])

    async def sink(event: AgentHarnessEvent) -> None:
        events.append(event)
        if event.kind == "model_turn_pending":
            pending_seen.set()

    task = asyncio.create_task(
        run_native_tool_loop(
            gateway=gateway,
            user_input="wait forever",
            event_sink=sink,
            _model_pending_elapsed_seconds=(0.0,),
            _model_pending_repeat_seconds=0.001,
        )
    )
    await started.wait()
    await asyncio.wait_for(pending_seen.wait(), timeout=1.0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancelled.is_set()
    assert [event.kind for event in events[:2]] == [
        "model_turn_started",
        "model_turn_pending",
    ]


@pytest.mark.asyncio
async def test_model_turn_started_events_cover_the_native_fifty_turn_limit() -> None:
    events: list[AgentHarnessEvent] = []

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
    )

    result = await run_native_tool_loop(
        gateway=gateway,
        user_input="keep checking",
        tools=(NativeTool(schema=tool_schema("lookup"), handler=lookup),),
        event_sink=lambda event: collect(events, event),
    )

    assert result.status == "turn_limit"
    assert result.turns == 50
    assert [event.turn for event in events if event.kind == "model_turn_started"] == list(
        range(1, 51)
    )
    assert events[-1].kind == "final_response"
    assert events[-1].turn == 50
