from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from app.agents.harness import (
    AgentHarnessEvent,
    AgentTranscriptCheckpoint,
    ConversationLifecycle,
    NativeTool,
    ToolExecutionError,
    ToolExecutionResult,
    UserAbortRequested,
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


def terminal_call(
    *,
    disposition: str = "completed",
    content: str = "Done.",
    call_id: str = "terminal-1",
) -> dict[str, object]:
    return {
        "id": call_id,
        "name": "emit_conversation_response",
        "args": {"disposition": disposition, "content": content},
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
async def test_oversized_tool_result_is_model_facing_error_not_truncated_success() -> None:
    events: list[AgentHarnessEvent] = []

    async def lookup(_arguments: Mapping[str, object]) -> object:
        return {"rows": ["x" * 5000]}

    gateway = Gateway(
        [
            AIMessage(
                content="I will look that up.",
                tool_calls=[
                    {
                        "id": "call-1",
                        "name": "lookup",
                        "args": {},
                    }
                ],
            ),
            AIMessage(content="Please narrow the search."),
        ]
    )

    result = await run_native_tool_loop(
        gateway=gateway,
        user_input="look up everything",
        tools=(NativeTool(schema=tool_schema("lookup"), handler=lookup),),
        event_sink=lambda event: collect(events, event),
    )

    assert result.status == "completed"
    assert [event.kind for event in events[:3]] == [
        "model_turn_started",
        "tool_call",
        "tool_error",
    ]
    assert isinstance(result.messages[3], ToolMessage)
    assert result.messages[3].status == "error"
    assert "tool_result_oversize" in str(result.messages[3].content)
    assert "truncated" not in str(result.messages[3].content)
    assert gateway.calls[1][-1].content == result.messages[3].content


@pytest.mark.asyncio
async def test_restored_messages_are_replayed_between_current_system_and_new_user() -> None:
    prior = (
        HumanMessage(content="Add a quiz."),
        AIMessage(content="Which course?"),
    )
    gateway = Gateway([AIMessage(content="ECE 202 quiz added to the proposal.")])

    result = await run_native_tool_loop(
        gateway=gateway,
        user_input="ECE 202",
        restored_messages=prior,
        system_message="Current policy.",
    )

    assert result.status == "completed"
    assert [message.type for message in gateway.calls[0]] == [
        "system",
        "human",
        "ai",
        "human",
    ]
    assert gateway.calls[0][0].content == "Current policy."
    assert gateway.calls[0][1:3] == prior
    assert gateway.calls[0][-1].content == "ECE 202"


@pytest.mark.asyncio
async def test_resume_with_current_human_already_restored_does_not_append_duplicate() -> None:
    restored = (HumanMessage(content="Continue the open request."),)
    gateway = Gateway([AIMessage(content="Continuing now.")])

    result = await run_native_tool_loop(
        gateway=gateway,
        user_input=None,
        restored_messages=restored,
    )

    assert result.status == "completed"
    assert [message.type for message in gateway.calls[0]] == ["system", "human"]
    assert gateway.calls[0][-1].content == "Continue the open request."


@pytest.mark.asyncio
async def test_resume_checkpointed_terminal_lifecycle_returns_without_model_call() -> None:
    events: list[AgentHarnessEvent] = []
    restored = (
        HumanMessage(content="Add the quiz to ECE 202."),
        AIMessage(
            content="",
            tool_calls=[
                terminal_call(
                    disposition="completed",
                    content="I prepared that for review.",
                )
            ],
        ),
    )
    gateway = Gateway([AIMessage(content="Should not be called.")])

    result = await run_native_tool_loop(
        gateway=gateway,
        user_input=None,
        restored_messages=restored,
        require_terminal_response=True,
        event_sink=lambda event: collect(events, event),
    )

    assert result.status == "completed"
    assert result.lifecycle_disposition == "completed"
    assert result.final_response == "I prepared that for review."
    assert gateway.calls == []
    assert [(event.kind, event.content, event.lifecycle_disposition) for event in events] == [
        ("final_response", "I prepared that for review.", "completed")
    ]


@pytest.mark.asyncio
async def test_resume_executes_only_missing_tool_results_then_continues() -> None:
    checkpoints: list[AgentTranscriptCheckpoint] = []
    executed: list[str] = []

    async def lookup(arguments: Mapping[str, object]) -> object:
        value = str(arguments["value"])
        executed.append(value)
        return {"value": value}

    restored_assistant = AIMessage(
        content="I will look up both values.",
        tool_calls=[
            {"id": "call-1", "name": "lookup", "args": {"value": "already-done"}},
            {"id": "call-2", "name": "lookup", "args": {"value": "missing"}},
        ],
    )
    restored = (
        HumanMessage(content="Look up two values."),
        restored_assistant,
        ToolMessage(
            content='{"content":{"value":"already-done"},"status":"succeeded"}',
            tool_call_id="call-1",
            name="lookup",
            status="success",
        ),
    )
    gateway = Gateway([AIMessage(content="Both values are ready.")])

    result = await run_native_tool_loop(
        gateway=gateway,
        user_input=None,
        restored_messages=restored,
        tools=(NativeTool(schema=tool_schema("lookup"), handler=lookup),),
        checkpoint_sink=lambda checkpoint: collect(checkpoints, checkpoint),
    )

    assert result.status == "completed"
    assert executed == ["missing"]
    assert [item.kind for item in checkpoints] == ["tool_result", "assistant_message"]
    assert checkpoints[0].tool_call_id == "call-2"
    assert [message.type for message in gateway.calls[0]] == [
        "system",
        "human",
        "ai",
        "tool",
        "tool",
    ]
    assert gateway.calls[0][-1].tool_call_id == "call-2"


@pytest.mark.asyncio
async def test_resume_with_all_tool_results_does_not_reexecute_tools() -> None:
    executed = False

    async def lookup(_: Mapping[str, object]) -> object:
        nonlocal executed
        executed = True
        return {"ok": True}

    restored = (
        HumanMessage(content="Look up one value."),
        AIMessage(
            content="I will look that up.",
            tool_calls=[{"id": "call-1", "name": "lookup", "args": {}}],
        ),
        ToolMessage(
            content='{"content":{"ok":true},"status":"succeeded"}',
            tool_call_id="call-1",
            name="lookup",
            status="success",
        ),
    )
    gateway = Gateway([AIMessage(content="Already have it.")])

    result = await run_native_tool_loop(
        gateway=gateway,
        user_input=None,
        restored_messages=restored,
        tools=(NativeTool(schema=tool_schema("lookup"), handler=lookup),),
    )

    assert result.status == "completed"
    assert executed is False
    assert len(gateway.calls) == 1


@pytest.mark.asyncio
async def test_resume_invalid_lifecycle_checkpoint_uses_bounded_correction() -> None:
    restored = (
        HumanMessage(content="Answer in lifecycle mode."),
        AIMessage(content="Plain text cannot finish lifecycle mode."),
    )
    gateway = Gateway(
        [
            AIMessage(
                content="",
                tool_calls=[
                    terminal_call(
                        disposition="completed",
                        content="Corrected after resume.",
                    )
                ],
            )
        ]
    )

    result = await run_native_tool_loop(
        gateway=gateway,
        user_input=None,
        restored_messages=restored,
        require_terminal_response=True,
    )

    assert result.status == "completed"
    assert result.final_response == "Corrected after resume."
    assert len(gateway.calls) == 1
    assert isinstance(gateway.calls[0][-1], SystemMessage)
    assert "emit_conversation_response was not called" in str(gateway.calls[0][-1].content)


@pytest.mark.asyncio
async def test_checkpoint_sink_receives_full_transcript_after_assistant_and_tool_result() -> None:
    checkpoints: list[AgentTranscriptCheckpoint] = []

    async def checkpoint(item: AgentTranscriptCheckpoint) -> None:
        checkpoints.append(item)

    async def lookup(_: Mapping[str, object]) -> object:
        return {"ok": True}

    reasoning_message = AIMessage(
        content="I will check.",
        additional_kwargs={"reasoning_content": "private native thinking"},
        tool_calls=[{"id": "call-1", "name": "lookup", "args": {}}],
    )
    gateway = Gateway([reasoning_message, AIMessage(content="Done.")])

    result = await run_native_tool_loop(
        gateway=gateway,
        user_input="look up",
        tools=(NativeTool(schema=tool_schema("lookup"), handler=lookup),),
        checkpoint_sink=checkpoint,
    )

    assert result.status == "completed"
    assert [item.kind for item in checkpoints] == [
        "assistant_message",
        "tool_result",
        "assistant_message",
    ]
    assert checkpoints[0].messages[-1].additional_kwargs["reasoning_content"] == (
        "private native thinking"
    )
    assert isinstance(checkpoints[1].messages[-1], ToolMessage)
    assert checkpoints[1].tool_call_id == "call-1"
    assert checkpoints[1].tool_name == "lookup"


@pytest.mark.asyncio
async def test_terminal_response_tool_returns_completed_lifecycle_without_tool_result() -> None:
    events: list[AgentHarnessEvent] = []
    gateway = Gateway(
        [
            AIMessage(
                content="",
                tool_calls=[terminal_call(disposition="completed", content="All set.")],
            )
        ]
    )

    result = await run_native_tool_loop(
        gateway=gateway,
        user_input="finish this",
        require_terminal_response=True,
        event_sink=lambda event: collect(events, event),
    )

    assert result.status == "completed"
    assert result.lifecycle_disposition == "completed"
    assert result.final_response == "All set."
    assert [event.kind for event in events] == ["model_turn_started", "final_response"]
    assert events[-1].lifecycle_disposition == "completed"
    assert len(result.messages) == 3
    assert isinstance(result.messages[-1], AIMessage)


@pytest.mark.asyncio
async def test_terminal_response_tool_can_leave_conversation_awaiting_user() -> None:
    gateway = Gateway(
        [
            AIMessage(
                content="",
                tool_calls=[
                    terminal_call(
                        disposition="awaiting_user",
                        content="Which course should I use?",
                    )
                ],
            )
        ]
    )

    result = await run_native_tool_loop(
        gateway=gateway,
        user_input="Add a quiz tomorrow.",
        require_terminal_response=True,
    )

    assert result.status == "awaiting_user"
    assert result.lifecycle_disposition == "awaiting_user"
    assert result.final_response == "Which course should I use?"


@pytest.mark.asyncio
async def test_missing_terminal_response_gets_one_correction_turn() -> None:
    gateway = Gateway(
        [
            AIMessage(content="Plain text is not terminal in lifecycle mode."),
            AIMessage(
                content="",
                tool_calls=[terminal_call(disposition="completed", content="Corrected.")],
            ),
        ]
    )

    result = await run_native_tool_loop(
        gateway=gateway,
        user_input="answer",
        require_terminal_response=True,
    )

    assert result.status == "completed"
    assert result.final_response == "Corrected."
    assert len(gateway.calls) == 2
    assert isinstance(gateway.calls[1][-1], SystemMessage)
    assert "emit_conversation_response was not called" in str(gateway.calls[1][-1].content)


@pytest.mark.asyncio
async def test_repeated_invalid_terminal_response_fails_closed() -> None:
    gateway = Gateway(
        [
            AIMessage(
                content="",
                tool_calls=[
                    terminal_call(
                        disposition="completed",
                        content="Mixed.",
                        call_id="terminal-1",
                    ),
                    {"id": "call-1", "name": "lookup", "args": {}},
                ],
            ),
            AIMessage(content="Still plain text."),
        ]
    )

    async def lookup(_: Mapping[str, object]) -> object:
        return {"ok": True}

    result = await run_native_tool_loop(
        gateway=gateway,
        user_input="answer",
        tools=(NativeTool(schema=tool_schema("lookup"), handler=lookup),),
        require_terminal_response=True,
    )

    assert result.status == "failed"
    assert result.final_response == (
        "The model did not produce a valid terminal conversation response. Please try again."
    )
    assert len(gateway.calls) == 2


@pytest.mark.asyncio
async def test_failed_tool_then_failed_lifecycle_repair_returns_actionable_host_error() -> None:
    async def unavailable(_: Mapping[str, object]) -> object:
        raise ToolExecutionError("The requested calendar is unavailable.")

    gateway = Gateway(
        [
            AIMessage(
                content="I will check the calendar.",
                tool_calls=[{"id": "call-1", "name": "lookup", "args": {}}],
            ),
            AIMessage(content="The calendar is unavailable."),
            AIMessage(content="It is still unavailable."),
        ]
    )

    result = await run_native_tool_loop(
        gateway=gateway,
        user_input="What is due today?",
        tools=(NativeTool(schema=tool_schema("lookup"), handler=unavailable),),
        require_terminal_response=True,
    )

    assert result.status == "completed"
    assert result.lifecycle_disposition == "completed"
    assert result.final_response == (
        "I couldn't complete that request because the required data was unavailable: "
        "The requested calendar is unavailable. No change was made."
    )


@pytest.mark.asyncio
async def test_lifecycle_validator_can_reject_and_correct_terminal_response() -> None:
    seen: list[ConversationLifecycle] = []

    def validator(
        lifecycle: ConversationLifecycle,
        _messages: Sequence[BaseMessage],
    ) -> str | None:
        seen.append(lifecycle)
        if lifecycle.content == "Which course?":
            return "duplicate clarification"
        return None

    gateway = Gateway(
        [
            AIMessage(
                content="",
                tool_calls=[
                    terminal_call(
                        disposition="awaiting_user",
                        content="Which course?",
                        call_id="terminal-1",
                    )
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    terminal_call(
                        disposition="completed",
                        content="I used the course you already gave me.",
                        call_id="terminal-2",
                    )
                ],
            ),
        ]
    )

    result = await run_native_tool_loop(
        gateway=gateway,
        user_input="ECE 202",
        require_terminal_response=True,
        lifecycle_validator=validator,
    )

    assert result.status == "completed"
    assert result.final_response == "I used the course you already gave me."
    assert [item.content for item in seen] == [
        "Which course?",
        "I used the course you already gave me.",
    ]


@pytest.mark.asyncio
async def test_abort_check_stops_before_model_turn() -> None:
    gateway = Gateway([AIMessage(content="Should not be reached.")])

    def abort() -> None:
        raise UserAbortRequested()

    with pytest.raises(UserAbortRequested):
        await run_native_tool_loop(
            gateway=gateway,
            user_input="stop first",
            abort_check=abort,
        )

    assert gateway.calls == []


@pytest.mark.asyncio
async def test_abort_check_stops_before_next_tool_handler() -> None:
    events: list[AgentHarnessEvent] = []
    checks = 0
    handler_called = False

    def abort_after_model() -> None:
        nonlocal checks
        checks += 1
        if checks >= 3:
            raise UserAbortRequested()

    async def lookup(_: Mapping[str, object]) -> object:
        nonlocal handler_called
        handler_called = True
        return {"ok": True}

    gateway = Gateway(
        [
            AIMessage(
                content="I will check safely.",
                tool_calls=[{"id": "call-1", "name": "lookup", "args": {"secret": "redacted"}}],
            )
        ]
    )

    with pytest.raises(UserAbortRequested):
        await run_native_tool_loop(
            gateway=gateway,
            user_input="look up",
            tools=(
                NativeTool(
                    schema=tool_schema("lookup"),
                    handler=lookup,
                    side_effect_class="proposal_only",
                    activity="proposal_drafting",
                ),
            ),
            event_sink=lambda event: collect(events, event),
            abort_check=abort_after_model,
        )

    assert handler_called is False
    assert [event.kind for event in events] == ["model_turn_started", "tool_call"]
    assert events[1].tool_side_effect_class == "proposal_only"
    assert events[1].tool_activity == "proposal_drafting"


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
