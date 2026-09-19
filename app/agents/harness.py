"""Generic visible tool-call harness for native agent loops."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, Literal, Protocol, cast

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

type HarnessEventKind = Literal[
    "assistant_text",
    "model_turn_started",
    "model_turn_pending",
    "tool_call",
    "tool_result",
    "tool_error",
    "final_response",
]
type HarnessStatus = Literal["completed", "awaiting_user", "failed", "turn_limit"]
type ConversationDisposition = Literal["awaiting_user", "completed"]
type TranscriptCheckpointKind = Literal["assistant_message", "tool_result"]
type ToolResultStatus = Literal["succeeded", "review_required"]
type ToolSideEffectClass = Literal[
    "read_only",
    "proposal_only",
    "durable_local_write",
    "external_write",
]

type EventSink = Callable[["AgentHarnessEvent"], Awaitable[None]]
type AbortCheck = Callable[[], Awaitable[None] | None]
type CheckpointSink = Callable[["AgentTranscriptCheckpoint"], Awaitable[None]]
type PreModelContextHook = Callable[
    ["PreModelContext"],
    Awaitable[Sequence[BaseMessage]] | Sequence[BaseMessage],
]
type LifecycleValidator = Callable[
    ["ConversationLifecycle", Sequence[BaseMessage]],
    Awaitable[str | None] | str | None,
]

DEFAULT_SYSTEM_MESSAGE = (
    "You are a helpful assistant. Use available tools when they are useful. "
    "Do not reveal hidden reasoning."
)
TERMINAL_RESPONSE_TOOL_NAME = "emit_conversation_response"
TERMINAL_RESPONSE_TOOL_SCHEMA: Mapping[str, Any] = {
    "type": "function",
    "function": {
        "name": TERMINAL_RESPONSE_TOOL_NAME,
        "description": (
            "End the current owner-visible turn with an explicit conversation lifecycle "
            "disposition and exact response text."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "disposition": {
                    "type": "string",
                    "enum": ["awaiting_user", "completed"],
                    "description": (
                        "Use awaiting_user only when the owner must reply before the request "
                        "can be completed; use completed for final answers and terminal outcomes."
                    ),
                },
                "content": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The exact owner-visible Discord response.",
                },
            },
            "required": ["disposition", "content"],
        },
    },
}
_LIFECYCLE_CORRECTION_PREFIX = (
    "Host correction: the previous assistant message did not follow the terminal response "
    "contract. Finish ordinary tool work first. When ready to respond to the owner, call "
    f"{TERMINAL_RESPONSE_TOOL_NAME} as the only tool in the assistant message with "
    'disposition "awaiting_user" or "completed" and exact owner-visible content. '
    "Do not answer with plain assistant text in terminal-response mode."
)
_LIFECYCLE_FAILURE_RESPONSE = (
    "The model did not produce a valid terminal conversation response. Please try again."
)
MAX_EVENT_JSON_CHARS = 4_096
MAX_JSON_STRING_CHARS = 1_024
MAX_JSON_ITEMS = 50
MAX_JSON_DEPTH = 6
MODEL_PENDING_ELAPSED_SECONDS: tuple[float, ...] = (8.0, 20.0, 45.0)
MODEL_PENDING_REPEAT_SECONDS = 30.0
MAX_MODEL_PENDING_EVENTS = 12
_REASONING_BLOCK_RE = re.compile(
    r"<\s*(think|thinking|reasoning)\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_ORPHAN_REASONING_END_RE = re.compile(
    r"\A.*?</\s*(?:think|thinking|reasoning)\s*>",
    re.IGNORECASE | re.DOTALL,
)
_REASONING_TAG_RE = re.compile(
    r"</?\s*(?:think|thinking|reasoning)\b[^>]*>",
    re.IGNORECASE,
)
_REASONING_PREFIX_RE = re.compile(
    r"(?im)^\s*(?:thinking|reasoning|thought|analysis)\s*:\s*",
)


class AgentHarnessGateway(Protocol):
    """Gateway capable of producing assistant messages with native tool calls."""

    async def invoke_tools(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Mapping[str, Any]],
    ) -> AIMessage: ...


class NativeToolHandler(Protocol):
    """Execute one model-selected tool call from safe JSON-like arguments."""

    async def __call__(self, arguments: Mapping[str, object]) -> object: ...


class ToolExecutionError(ValueError):
    """Explicit, user-safe tool error that may be shown to the model and owner."""


class UserAbortRequested(asyncio.CancelledError):
    """Raised when a user-requested abort has been durably observed."""


@dataclass(frozen=True, slots=True)
class ConversationLifecycle:
    """Semantic owner-visible response plus host lifecycle disposition."""

    disposition: ConversationDisposition
    content: str


@dataclass(frozen=True, slots=True)
class NativeTool:
    """Model-facing schema plus the local handler for one tool."""

    schema: Mapping[str, Any]
    handler: NativeToolHandler
    name: str | None = None
    side_effect_class: ToolSideEffectClass = "read_only"
    activity: str | None = None


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    """Structured tool result for side-effect boundaries and ordinary results."""

    content: object
    status: ToolResultStatus = "succeeded"


@dataclass(frozen=True, slots=True)
class AgentHarnessEvent:
    """Visible, ordered event emitted by the harness."""

    kind: HarnessEventKind
    turn: int
    turn_limit: int | None = None
    elapsed_seconds: int | None = None
    content: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_activity: str | None = None
    tool_side_effect_class: ToolSideEffectClass | None = None
    lifecycle_disposition: ConversationDisposition | None = None
    args_json: str | None = None
    result_json: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class AgentTranscriptCheckpoint:
    """Durable checkpoint boundary for the full ordered native transcript."""

    kind: TranscriptCheckpointKind
    turn: int
    messages: tuple[BaseMessage, ...]
    message_index: int
    tool_call_id: str | None = None
    tool_name: str | None = None
    lifecycle_disposition: ConversationDisposition | None = None


@dataclass(frozen=True, slots=True)
class PreModelContext:
    """Canonical loop state supplied to a host-owned context assembler."""

    canonical_messages: tuple[BaseMessage, ...]
    tools: tuple[Mapping[str, Any], ...]
    turn: int
    turn_limit: int


@dataclass(frozen=True, slots=True)
class AgentHarnessResult:
    """Final harness result and the message transcript given back to callers."""

    status: HarnessStatus
    final_response: str
    turns: int
    messages: tuple[BaseMessage, ...]
    lifecycle_disposition: ConversationDisposition | None = None


@dataclass(frozen=True, slots=True)
class _ToolCall:
    call_id: str
    name: str
    args: object
    malformed_error: str | None = None


@dataclass(slots=True)
class _PendingEventBudget:
    remaining: int = MAX_MODEL_PENDING_EVENTS


async def run_native_tool_loop(
    *,
    gateway: AgentHarnessGateway,
    user_input: str | None,
    tools: Sequence[NativeTool] = (),
    system_message: str = DEFAULT_SYSTEM_MESSAGE,
    max_turns: int = 50,
    event_sink: EventSink | None = None,
    checkpoint_sink: CheckpointSink | None = None,
    abort_check: AbortCheck | None = None,
    restored_messages: Sequence[BaseMessage] = (),
    require_terminal_response: bool = False,
    lifecycle_validator: LifecycleValidator | None = None,
    pre_model_context_hook: PreModelContextHook | None = None,
    _model_pending_elapsed_seconds: Sequence[float] = MODEL_PENDING_ELAPSED_SECONDS,
    _model_pending_repeat_seconds: float = MODEL_PENDING_REPEAT_SECONDS,
) -> AgentHarnessResult:
    """Run a bounded model/tool loop without semantic routing or keyword rules."""

    if max_turns < 1:
        raise ValueError("max_turns must be positive")
    tool_map = _tool_map(tools)
    if require_terminal_response and TERMINAL_RESPONSE_TOOL_NAME in tool_map:
        raise ValueError(f"{TERMINAL_RESPONSE_TOOL_NAME} is reserved for lifecycle control")
    tool_schemas = tuple(tool.schema for tool in tools)
    if require_terminal_response:
        tool_schemas = (*tool_schemas, TERMINAL_RESPONSE_TOOL_SCHEMA)
    messages: list[BaseMessage] = [SystemMessage(content=system_message), *restored_messages]
    if user_input is not None:
        messages.append(HumanMessage(content=user_input))
    pending_event_budget = _PendingEventBudget()
    lifecycle_correction_used = False
    restored_turns = _assistant_message_count(messages)

    resume_result = await _resume_checkpointed_tail(
        messages=messages,
        tool_map=tool_map,
        require_terminal_response=require_terminal_response,
        lifecycle_validator=lifecycle_validator,
        checkpoint_sink=checkpoint_sink,
        event_sink=event_sink,
        abort_check=abort_check,
        restored_turns=restored_turns,
    )
    if isinstance(resume_result, AgentHarnessResult):
        return resume_result
    lifecycle_correction_used = resume_result
    restored_turns = _assistant_message_count(messages)

    for attempt in range(1, max_turns + 1):
        turn = restored_turns + attempt
        turn_limit = restored_turns + max_turns
        await _raise_if_abort_requested(abort_check)
        model_messages: Sequence[BaseMessage] = tuple(messages)
        if pre_model_context_hook is not None:
            assembled = pre_model_context_hook(
                PreModelContext(
                    canonical_messages=tuple(messages),
                    tools=tool_schemas,
                    turn=turn,
                    turn_limit=turn_limit,
                )
            )
            if inspect.isawaitable(assembled):
                assembled = await assembled
            model_messages = tuple(assembled)
            if not model_messages or not isinstance(model_messages[0], SystemMessage):
                raise RuntimeError("context_assembly_invalid")
        assistant = await _invoke_model_turn(
            gateway=gateway,
            messages=model_messages,
            tools=tool_schemas,
            turn=turn,
            turn_limit=turn_limit,
            event_sink=event_sink,
            pending_elapsed_seconds=_model_pending_elapsed_seconds,
            pending_repeat_seconds=_model_pending_repeat_seconds,
            pending_event_budget=pending_event_budget,
        )
        messages.append(assistant)
        await _checkpoint(
            checkpoint_sink,
            AgentTranscriptCheckpoint(
                kind="assistant_message",
                turn=turn,
                messages=tuple(messages),
                message_index=len(messages) - 1,
            ),
        )
        calls = _tool_calls(assistant, turn)
        if require_terminal_response:
            lifecycle, validation_error = await _validate_lifecycle_turn(
                assistant=assistant,
                calls=calls,
                messages=tuple(messages),
                lifecycle_validator=lifecycle_validator,
            )
            if lifecycle is not None:
                await _emit(
                    event_sink,
                    AgentHarnessEvent(
                        kind="final_response",
                        turn=turn,
                        content=lifecycle.content,
                        lifecycle_disposition=lifecycle.disposition,
                    ),
                )
                return AgentHarnessResult(
                    status=(
                        "awaiting_user" if lifecycle.disposition == "awaiting_user" else "completed"
                    ),
                    final_response=lifecycle.content,
                    turns=turn,
                    messages=tuple(messages),
                    lifecycle_disposition=lifecycle.disposition,
                )
            if validation_error is not None:
                if lifecycle_correction_used or attempt >= max_turns:
                    return await _fail_closed_lifecycle(
                        event_sink=event_sink,
                        messages=tuple(messages),
                        turn=turn,
                    )
                lifecycle_correction_used = True
                messages.append(SystemMessage(content=_lifecycle_correction(validation_error)))
                continue

        if not calls:
            text = _visible_text(assistant)
            final = text
            await _emit(
                event_sink,
                AgentHarnessEvent(kind="final_response", turn=turn, content=final),
            )
            return AgentHarnessResult(
                status="completed",
                final_response=final,
                turns=turn,
                messages=tuple(messages),
            )

        # Native assistant content is the semantic description of this action.
        # Associate it with the first structured call and pass it through
        # verbatim; the harness must never synthesize conversational tool text.
        action_description = _visible_text(assistant)
        for index, call in enumerate(calls):
            tool = tool_map.get(call.name)
            await _raise_if_abort_requested(abort_check)
            await _emit(
                event_sink,
                AgentHarnessEvent(
                    kind="tool_call",
                    turn=turn,
                    content=action_description if index == 0 else None,
                    tool_call_id=call.call_id,
                    tool_name=call.name,
                    tool_activity=tool.activity if tool is not None else None,
                    tool_side_effect_class=(tool.side_effect_class if tool is not None else None),
                    args_json=_safe_json(call.args),
                ),
            )
            tool_message, event = await _execute_tool_call(call, tool_map, turn, abort_check)
            messages.append(tool_message)
            await _checkpoint(
                checkpoint_sink,
                AgentTranscriptCheckpoint(
                    kind="tool_result",
                    turn=turn,
                    messages=tuple(messages),
                    message_index=len(messages) - 1,
                    tool_call_id=call.call_id,
                    tool_name=call.name,
                ),
            )
            await _emit(event_sink, event)

    final = "The agent reached its turn limit before producing a final response."
    await _emit(
        event_sink,
        AgentHarnessEvent(
            kind="final_response",
            turn=restored_turns + max_turns,
            content=final,
        ),
    )
    return AgentHarnessResult(
        status="turn_limit",
        final_response=final,
        turns=restored_turns + max_turns,
        messages=tuple(messages),
    )


async def _resume_checkpointed_tail(
    *,
    messages: list[BaseMessage],
    tool_map: Mapping[str, NativeTool],
    require_terminal_response: bool,
    lifecycle_validator: LifecycleValidator | None,
    checkpoint_sink: CheckpointSink | None,
    event_sink: EventSink | None,
    abort_check: AbortCheck | None,
    restored_turns: int,
) -> AgentHarnessResult | bool:
    tail = _checkpointed_assistant_tail(messages)
    if tail is None:
        return False
    _assistant_index, assistant, suffix = tail
    turn = max(1, restored_turns)
    calls = _tool_calls(assistant, turn)
    if require_terminal_response:
        lifecycle, validation_error = await _validate_lifecycle_turn(
            assistant=assistant,
            calls=calls,
            messages=tuple(messages),
            lifecycle_validator=lifecycle_validator,
        )
        if lifecycle is not None:
            await _emit(
                event_sink,
                AgentHarnessEvent(
                    kind="final_response",
                    turn=turn,
                    content=lifecycle.content,
                    lifecycle_disposition=lifecycle.disposition,
                ),
            )
            return AgentHarnessResult(
                status=(
                    "awaiting_user" if lifecycle.disposition == "awaiting_user" else "completed"
                ),
                final_response=lifecycle.content,
                turns=turn,
                messages=tuple(messages),
                lifecycle_disposition=lifecycle.disposition,
            )
        if validation_error is not None:
            messages.append(SystemMessage(content=_lifecycle_correction(validation_error)))
            return True

    matched_tool_call_ids = _matched_tool_call_ids(suffix)
    for index, call in enumerate(calls):
        if call.call_id in matched_tool_call_ids:
            continue
        tool = tool_map.get(call.name)
        await _raise_if_abort_requested(abort_check)
        await _emit(
            event_sink,
            AgentHarnessEvent(
                kind="tool_call",
                turn=turn,
                content=_visible_text(assistant) if index == 0 else None,
                tool_call_id=call.call_id,
                tool_name=call.name,
                tool_activity=tool.activity if tool is not None else None,
                tool_side_effect_class=tool.side_effect_class if tool is not None else None,
                args_json=_safe_json(call.args),
            ),
        )
        tool_message, event = await _execute_tool_call(call, tool_map, turn, abort_check)
        messages.append(tool_message)
        await _checkpoint(
            checkpoint_sink,
            AgentTranscriptCheckpoint(
                kind="tool_result",
                turn=turn,
                messages=tuple(messages),
                message_index=len(messages) - 1,
                tool_call_id=call.call_id,
                tool_name=call.name,
            ),
        )
        await _emit(event_sink, event)
    return False


def _checkpointed_assistant_tail(
    messages: Sequence[BaseMessage],
) -> tuple[int, AIMessage, tuple[ToolMessage, ...]] | None:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, AIMessage):
            continue
        suffix = messages[index + 1 :]
        if not all(isinstance(item, ToolMessage) for item in suffix):
            return None
        return index, message, cast(tuple[ToolMessage, ...], tuple(suffix))
    return None


def _matched_tool_call_ids(messages: Sequence[ToolMessage]) -> frozenset[str]:
    return frozenset(
        tool_call_id
        for message in messages
        if isinstance((tool_call_id := getattr(message, "tool_call_id", None)), str)
        and tool_call_id
    )


def _assistant_message_count(messages: Sequence[BaseMessage]) -> int:
    return sum(1 for message in messages if isinstance(message, AIMessage))


async def _invoke_model_turn(
    *,
    gateway: AgentHarnessGateway,
    messages: Sequence[BaseMessage],
    tools: Sequence[Mapping[str, Any]],
    turn: int,
    turn_limit: int,
    event_sink: EventSink | None,
    pending_elapsed_seconds: Sequence[float],
    pending_repeat_seconds: float,
    pending_event_budget: _PendingEventBudget,
) -> AIMessage:
    if event_sink is None:
        return await gateway.invoke_tools(messages, tools)

    await _emit(
        event_sink,
        AgentHarnessEvent(kind="model_turn_started", turn=turn, turn_limit=turn_limit),
    )
    invocation = asyncio.create_task(gateway.invoke_tools(messages, tools))
    previous_elapsed_seconds = 0.0
    try:
        for elapsed_seconds in _pending_elapsed_schedule(
            pending_elapsed_seconds,
            pending_repeat_seconds,
        ):
            if pending_event_budget.remaining <= 0:
                break
            timeout_seconds = max(0.0, elapsed_seconds - previous_elapsed_seconds)
            previous_elapsed_seconds = elapsed_seconds
            try:
                return await asyncio.wait_for(
                    asyncio.shield(invocation),
                    timeout=timeout_seconds,
                )
            except TimeoutError:
                if invocation.done():
                    return await invocation
                await _emit(
                    event_sink,
                    AgentHarnessEvent(
                        kind="model_turn_pending",
                        turn=turn,
                        turn_limit=turn_limit,
                        elapsed_seconds=max(0, round(elapsed_seconds)),
                    ),
                )
                pending_event_budget.remaining -= 1
        return await invocation
    except asyncio.CancelledError:
        invocation.cancel()
        with suppress(asyncio.CancelledError):
            await invocation
        raise
    except Exception:
        if not invocation.done():
            invocation.cancel()
            with suppress(asyncio.CancelledError):
                await invocation
        raise


def _pending_elapsed_schedule(
    initial_elapsed_seconds: Sequence[float],
    repeat_seconds: float,
) -> tuple[float, ...]:
    if repeat_seconds <= 0:
        raise ValueError("pending repeat seconds must be positive")
    ordered = tuple(float(seconds) for seconds in initial_elapsed_seconds)
    if any(seconds < 0 for seconds in ordered):
        raise ValueError("pending elapsed seconds must be non-negative")
    if any(later < earlier for earlier, later in pairwise(ordered)):
        raise ValueError("pending elapsed seconds must be ordered")
    elapsed = list(ordered)
    next_elapsed = (elapsed[-1] if elapsed else 0.0) + repeat_seconds
    while len(elapsed) < MAX_MODEL_PENDING_EVENTS:
        elapsed.append(next_elapsed)
        next_elapsed += repeat_seconds
    return tuple(elapsed[:MAX_MODEL_PENDING_EVENTS])


def _tool_map(tools: Sequence[NativeTool]) -> dict[str, NativeTool]:
    tool_map: dict[str, NativeTool] = {}
    for tool in tools:
        name = tool.name or _schema_name(tool.schema)
        if not name:
            raise ValueError("tool schema must include a name")
        if name in tool_map:
            raise ValueError(f"duplicate tool name: {name}")
        tool_map[name] = tool
    return tool_map


def _schema_name(schema: Mapping[str, Any]) -> str | None:
    direct = schema.get("name")
    if isinstance(direct, str) and direct:
        return direct
    function = schema.get("function")
    if isinstance(function, Mapping):
        nested = cast(Mapping[str, object], function).get("name")
        if isinstance(nested, str) and nested:
            return nested
    return None


def _visible_text(message: AIMessage) -> str:
    content: object = message.content
    if isinstance(content, str):
        return _sanitize_visible_text(content)
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            text = _sanitize_visible_text(block)
            if text:
                parts.append(text)
            continue
        block_map = cast(Mapping[str, object], block)
        block_type = block_map.get("type")
        if isinstance(block_type, str) and block_type.casefold() in {
            "reasoning",
            "thinking",
            "redacted_thinking",
        }:
            continue
        text = block_map.get("text")
        if isinstance(text, str):
            sanitized = _sanitize_visible_text(text)
            if sanitized:
                parts.append(sanitized)
    return "\n".join(parts)


def _sanitize_visible_text(text: str) -> str:
    """Remove common hidden-reasoning wrappers while preserving answer text."""

    without_blocks = _REASONING_BLOCK_RE.sub("", text)
    without_orphan_reasoning = _ORPHAN_REASONING_END_RE.sub("", without_blocks)
    without_tags = _REASONING_TAG_RE.sub("", without_orphan_reasoning)
    without_prefixes = _REASONING_PREFIX_RE.sub("", without_tags)
    lines = [line.strip() for line in without_prefixes.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _tool_calls(message: AIMessage, turn: int) -> tuple[_ToolCall, ...]:
    calls: list[_ToolCall] = []
    valid_calls = cast(Sequence[object], getattr(message, "tool_calls", ()) or ())
    for index, raw in enumerate(valid_calls):
        raw_object: object = raw
        if not isinstance(raw_object, Mapping):
            calls.append(
                _ToolCall(
                    call_id=f"malformed-{turn}-{index}",
                    name="malformed_tool_call",
                    args={},
                    malformed_error="tool call must be an object",
                )
            )
            continue
        raw_map = cast(Mapping[str, object], raw_object)
        name = raw_map.get("name")
        call_id = raw_map.get("id")
        args = raw_map.get("args", {})
        if not isinstance(name, str) or not name:
            calls.append(
                _ToolCall(
                    call_id=str(call_id)
                    if isinstance(call_id, str) and call_id
                    else (f"malformed-{turn}-{index}"),
                    name="malformed_tool_call",
                    args=args,
                    malformed_error="tool call name is missing",
                )
            )
            continue
        calls.append(
            _ToolCall(
                call_id=str(call_id)
                if isinstance(call_id, str) and call_id
                else (f"{name}-{turn}-{index}"),
                name=name,
                args=args,
                malformed_error=(
                    None if isinstance(args, Mapping) else "tool arguments must be an object"
                ),
            )
        )
    invalid_calls = cast(Sequence[object], getattr(message, "invalid_tool_calls", ()) or ())
    for index, raw in enumerate(invalid_calls):
        raw_object = raw
        raw_map: Mapping[str, object] = (
            cast(Mapping[str, object], raw_object) if isinstance(raw_object, Mapping) else {}
        )
        name = raw_map.get("name")
        call_id = raw_map.get("id")
        args = raw_map.get("args", {})
        error = raw_map.get("error")
        calls.append(
            _ToolCall(
                call_id=str(call_id)
                if isinstance(call_id, str) and call_id
                else (f"invalid-{turn}-{index}"),
                name=str(name) if isinstance(name, str) and name else "malformed_tool_call",
                args=args,
                malformed_error=str(error)
                if isinstance(error, str) and error
                else ("tool call could not be parsed"),
            )
        )
    return tuple(calls)


async def _validate_lifecycle_turn(
    *,
    assistant: AIMessage,
    calls: Sequence[_ToolCall],
    messages: Sequence[BaseMessage],
    lifecycle_validator: LifecycleValidator | None,
) -> tuple[ConversationLifecycle | None, str | None]:
    terminal_calls = [call for call in calls if call.name == TERMINAL_RESPONSE_TOOL_NAME]
    if not calls:
        return None, f"{TERMINAL_RESPONSE_TOOL_NAME} was not called"
    if not terminal_calls:
        return None, None
    if len(calls) != 1:
        return None, f"{TERMINAL_RESPONSE_TOOL_NAME} must be the only tool call"
    call = terminal_calls[0]
    if call.malformed_error is not None:
        return None, call.malformed_error
    raw_args: object = call.args
    if not isinstance(raw_args, Mapping):
        return None, "terminal response arguments must be an object"
    lifecycle, error = _lifecycle_from_arguments(cast(Mapping[str, object], raw_args))
    if lifecycle is None:
        return None, error
    if _visible_text(assistant):
        return None, f"{TERMINAL_RESPONSE_TOOL_NAME} must carry the owner response in content"
    if lifecycle_validator is not None:
        validation = lifecycle_validator(lifecycle, messages)
        if inspect.isawaitable(validation):
            validation = await validation
        if validation is not None:
            cleaned = str(validation).strip()
            if cleaned:
                return None, cleaned
    return lifecycle, None


def _lifecycle_from_arguments(
    arguments: Mapping[str, object],
) -> tuple[ConversationLifecycle | None, str]:
    disposition = arguments.get("disposition")
    content = arguments.get("content")
    if disposition not in {"awaiting_user", "completed"}:
        return None, "terminal response disposition must be awaiting_user or completed"
    if not isinstance(content, str) or not content.strip():
        return None, "terminal response content must be non-empty text"
    return (
        ConversationLifecycle(
            disposition=cast(ConversationDisposition, disposition),
            content=content,
        ),
        "",
    )


def _lifecycle_correction(reason: str) -> str:
    cleaned = reason.strip()[:MAX_JSON_STRING_CHARS] or "invalid terminal response"
    return f"{_LIFECYCLE_CORRECTION_PREFIX} Contract error: {cleaned}."


async def _fail_closed_lifecycle(
    *,
    event_sink: EventSink | None,
    messages: Sequence[BaseMessage],
    turn: int,
) -> AgentHarnessResult:
    await _emit(
        event_sink,
        AgentHarnessEvent(
            kind="final_response",
            turn=turn,
            content=_LIFECYCLE_FAILURE_RESPONSE,
        ),
    )
    return AgentHarnessResult(
        status="failed",
        final_response=_LIFECYCLE_FAILURE_RESPONSE,
        turns=turn,
        messages=tuple(messages),
    )


async def _execute_tool_call(
    call: _ToolCall,
    tools: Mapping[str, NativeTool],
    turn: int,
    abort_check: AbortCheck | None,
) -> tuple[ToolMessage, AgentHarnessEvent]:
    if call.malformed_error is not None:
        return _tool_error(call, call.malformed_error, turn)
    tool = tools.get(call.name)
    if tool is None:
        return _tool_error(call, f"unknown tool: {call.name}", turn)
    call_args: object = call.args
    if not isinstance(call_args, Mapping):
        return _tool_error(call, "tool arguments must be an object", turn)
    arg_mapping: Mapping[object, object] = cast(Mapping[object, object], call_args)
    args: dict[str, object] = {}
    for key, item in arg_mapping.items():
        args[str(key)] = item
    try:
        await _raise_if_abort_requested(abort_check)
        result = await tool.handler(args)
    except Exception as exc:
        return _tool_error(
            call,
            _safe_exception_text(exc),
            turn,
            tool_activity=tool.activity,
            tool_side_effect_class=tool.side_effect_class,
        )
    payload = _tool_result_payload(result)
    result_json = _safe_json(payload)
    return (
        ToolMessage(
            content=result_json,
            tool_call_id=call.call_id,
            name=call.name,
            status="success",
        ),
        AgentHarnessEvent(
            kind="tool_result",
            turn=turn,
            tool_call_id=call.call_id,
            tool_name=call.name,
            tool_activity=tool.activity,
            tool_side_effect_class=tool.side_effect_class,
            result_json=result_json,
        ),
    )


def _tool_error(
    call: _ToolCall,
    error: str,
    turn: int,
    *,
    tool_activity: str | None = None,
    tool_side_effect_class: ToolSideEffectClass | None = None,
) -> tuple[ToolMessage, AgentHarnessEvent]:
    payload = {"status": "error", "error": error}
    content = _safe_json(payload)
    return (
        ToolMessage(
            content=content,
            tool_call_id=call.call_id,
            name=call.name,
            status="error",
        ),
        AgentHarnessEvent(
            kind="tool_error",
            turn=turn,
            tool_call_id=call.call_id,
            tool_name=call.name,
            tool_activity=tool_activity,
            tool_side_effect_class=tool_side_effect_class,
            error=error,
            result_json=content,
        ),
    )


def _tool_result_payload(result: object) -> Mapping[str, object]:
    if isinstance(result, ToolExecutionResult):
        return {"status": result.status, "content": result.content}
    return {"status": "succeeded", "content": result}


async def _emit(sink: EventSink | None, event: AgentHarnessEvent) -> None:
    if sink is None:
        return
    await sink(event)


async def _checkpoint(
    sink: CheckpointSink | None,
    checkpoint: AgentTranscriptCheckpoint,
) -> None:
    if sink is None:
        return
    await sink(checkpoint)


async def _raise_if_abort_requested(check: AbortCheck | None) -> None:
    if check is None:
        return
    result = check()
    if inspect.isawaitable(result):
        await result


def _safe_exception_text(exc: Exception) -> str:
    if isinstance(exc, ToolExecutionError):
        message = str(exc).strip()
        return message[:MAX_JSON_STRING_CHARS] or "tool execution failed"
    return f"tool execution failed ({exc.__class__.__name__})"


def _safe_json(value: object, *, max_chars: int = MAX_EVENT_JSON_CHARS) -> str:
    encoded = json.dumps(
        _json_safe(value),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(encoded) <= max_chars:
        return encoded
    return json.dumps(
        {"truncated": True, "preview": encoded[: max_chars - 128]},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _json_safe(value: object, *, depth: int = 0) -> object:
    if depth > MAX_JSON_DEPTH:
        return "<truncated>"
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value if len(value) <= MAX_JSON_STRING_CHARS else value[:MAX_JSON_STRING_CHARS]
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        result: dict[str, object] = {}
        for index, (key, item) in enumerate(mapping.items()):
            if index >= MAX_JSON_ITEMS:
                result["<truncated>"] = True
                break
            result[str(key)[:128]] = _json_safe(item, depth=depth + 1)
        return result
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        sequence = cast(Sequence[object], value)
        items = [_json_safe(item, depth=depth + 1) for item in sequence[:MAX_JSON_ITEMS]]
        if len(sequence) > MAX_JSON_ITEMS:
            items.append("<truncated>")
        return items
    return f"<{value.__class__.__name__}>"


__all__ = [
    "DEFAULT_SYSTEM_MESSAGE",
    "TERMINAL_RESPONSE_TOOL_NAME",
    "TERMINAL_RESPONSE_TOOL_SCHEMA",
    "AbortCheck",
    "AgentHarnessEvent",
    "AgentHarnessGateway",
    "AgentHarnessResult",
    "AgentTranscriptCheckpoint",
    "CheckpointSink",
    "ConversationDisposition",
    "ConversationLifecycle",
    "LifecycleValidator",
    "NativeTool",
    "NativeToolHandler",
    "ToolExecutionError",
    "ToolExecutionResult",
    "ToolSideEffectClass",
    "TranscriptCheckpointKind",
    "UserAbortRequested",
    "run_native_tool_loop",
]
