"""Generic visible tool-call harness for native agent loops."""

from __future__ import annotations

import asyncio
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
type HarnessStatus = Literal["completed", "turn_limit"]
type ToolResultStatus = Literal["succeeded", "review_required"]

type EventSink = Callable[["AgentHarnessEvent"], Awaitable[None]]

DEFAULT_SYSTEM_MESSAGE = (
    "You are a helpful assistant. Use available tools when they are useful. "
    "Do not reveal hidden reasoning."
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


@dataclass(frozen=True, slots=True)
class NativeTool:
    """Model-facing schema plus the local handler for one tool."""

    schema: Mapping[str, Any]
    handler: NativeToolHandler
    name: str | None = None


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
    args_json: str | None = None
    result_json: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class AgentHarnessResult:
    """Final harness result and the message transcript given back to callers."""

    status: HarnessStatus
    final_response: str
    turns: int
    messages: tuple[BaseMessage, ...]


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
    user_input: str,
    tools: Sequence[NativeTool] = (),
    system_message: str = DEFAULT_SYSTEM_MESSAGE,
    max_turns: int = 50,
    event_sink: EventSink | None = None,
    _model_pending_elapsed_seconds: Sequence[float] = MODEL_PENDING_ELAPSED_SECONDS,
    _model_pending_repeat_seconds: float = MODEL_PENDING_REPEAT_SECONDS,
) -> AgentHarnessResult:
    """Run a bounded model/tool loop without semantic routing or keyword rules."""

    if max_turns < 1:
        raise ValueError("max_turns must be positive")
    tool_handlers = _tool_handlers(tools)
    tool_schemas = tuple(tool.schema for tool in tools)
    messages: list[BaseMessage] = [
        SystemMessage(content=system_message),
        HumanMessage(content=user_input),
    ]
    pending_event_budget = _PendingEventBudget()

    for turn in range(1, max_turns + 1):
        assistant = await _invoke_model_turn(
            gateway=gateway,
            messages=tuple(messages),
            tools=tool_schemas,
            turn=turn,
            turn_limit=max_turns,
            event_sink=event_sink,
            pending_elapsed_seconds=_model_pending_elapsed_seconds,
            pending_repeat_seconds=_model_pending_repeat_seconds,
            pending_event_budget=pending_event_budget,
        )
        messages.append(assistant)
        calls = _tool_calls(assistant, turn)
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
            await _emit(
                event_sink,
                AgentHarnessEvent(
                    kind="tool_call",
                    turn=turn,
                    content=action_description if index == 0 else None,
                    tool_call_id=call.call_id,
                    tool_name=call.name,
                    args_json=_safe_json(call.args),
                ),
            )
            tool_message, event = await _execute_tool_call(call, tool_handlers, turn)
            messages.append(tool_message)
            await _emit(event_sink, event)

    final = "The agent reached its turn limit before producing a final response."
    await _emit(
        event_sink,
        AgentHarnessEvent(kind="final_response", turn=max_turns, content=final),
    )
    return AgentHarnessResult(
        status="turn_limit",
        final_response=final,
        turns=max_turns,
        messages=tuple(messages),
    )


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


def _tool_handlers(tools: Sequence[NativeTool]) -> dict[str, NativeToolHandler]:
    handlers: dict[str, NativeToolHandler] = {}
    for tool in tools:
        name = tool.name or _schema_name(tool.schema)
        if not name:
            raise ValueError("tool schema must include a name")
        if name in handlers:
            raise ValueError(f"duplicate tool name: {name}")
        handlers[name] = tool.handler
    return handlers


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


async def _execute_tool_call(
    call: _ToolCall,
    handlers: Mapping[str, NativeToolHandler],
    turn: int,
) -> tuple[ToolMessage, AgentHarnessEvent]:
    if call.malformed_error is not None:
        return _tool_error(call, call.malformed_error, turn)
    if call.name not in handlers:
        return _tool_error(call, f"unknown tool: {call.name}", turn)
    call_args: object = call.args
    if not isinstance(call_args, Mapping):
        return _tool_error(call, "tool arguments must be an object", turn)
    arg_mapping: Mapping[object, object] = cast(Mapping[object, object], call_args)
    args: dict[str, object] = {}
    for key, item in arg_mapping.items():
        args[str(key)] = item
    try:
        result = await handlers[call.name](args)
    except Exception as exc:
        return _tool_error(call, _safe_exception_text(exc), turn)
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
            result_json=result_json,
        ),
    )


def _tool_error(call: _ToolCall, error: str, turn: int) -> tuple[ToolMessage, AgentHarnessEvent]:
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
    "AgentHarnessEvent",
    "AgentHarnessGateway",
    "AgentHarnessResult",
    "NativeTool",
    "NativeToolHandler",
    "ToolExecutionError",
    "ToolExecutionResult",
    "run_native_tool_loop",
]
