"""Generic visible tool-call harness for native agent loops."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from itertools import pairwise
from typing import Any, Literal, Protocol, cast
from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, ValidationError

from app.llm.contracts import GatewayFailure

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
type HostLifecycleDisposition = Literal["complete", "continue_model", "awaiting_user", "failed"]
type TranscriptCheckpointKind = Literal["assistant_message", "tool_result", "batch_resolution"]
type ToolResultStatus = Literal["succeeded", "review_required"]
type ToolSideEffectClass = Literal[
    "read_only",
    "proposal_only",
    "durable_local_write",
    "external_write",
]
type ToolCallOutcomeStatus = Literal["success", "error"]
type CheckpointStatus = Literal["checkpointed", "not_configured"]

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
type LifecycleRenderer = Callable[
    ["ConversationLifecycle", Sequence[BaseMessage]],
    Awaitable[str] | str,
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
                    "enum": ["awaiting_user"],
                    "description": (
                        "Use awaiting_user only when the owner must reply before the request "
                        "can be completed. Final answers are completed by ordinary assistant "
                        "text or trusted host rendering, not this tool."
                    ),
                },
                "content": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The exact owner-visible Discord response.",
                },
                "grounding": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "query_id": {"type": "string", "minLength": 1, "maxLength": 128},
                        "item_ids": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1, "maxLength": 255},
                            "maxItems": 20,
                        },
                        "acknowledge_incomplete": {"type": "boolean"},
                        "acknowledge_stale": {"type": "boolean"},
                    },
                    "required": [
                        "query_id",
                        "item_ids",
                        "acknowledge_incomplete",
                        "acknowledge_stale",
                    ],
                },
            },
            "required": ["disposition", "content"],
        },
    },
}
_LIFECYCLE_CORRECTION_PREFIX = (
    "Host correction: the previous assistant message did not follow the terminal response "
    "contract. Finish ordinary tool work first. Use ordinary assistant text for final answers. "
    f"Call {TERMINAL_RESPONSE_TOOL_NAME} only when the owner must reply before the request can "
    'continue, with disposition "awaiting_user" and exact owner-visible content.'
)
_LIFECYCLE_FAILURE_RESPONSE = (
    "The model did not produce a valid terminal conversation response. Please try again."
)
_HOST_LIFECYCLE_FAILURE_RESPONSE = (
    "The host did not produce a valid terminal conversation response. Please try again."
)
_REPEATED_TOOL_FAILURE_RESPONSE = (
    "I couldn't complete that request because the same tool call failed twice in the same way. "
    "Please adjust the request or try again after the underlying data is available."
)
_REPAIRABLE_ARGUMENT_ERROR_CODES = frozenset(
    {
        "tool_arguments_invalid",
        "tool_malformed_call",
    }
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

    def __init__(
        self,
        message: str,
        *,
        code: str = "tool_execution_failed",
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ToolResultOversizeError(ToolExecutionError):
    """Signal that a domain envelope cannot fit even after safe page reduction."""


class UserAbortRequested(asyncio.CancelledError):
    """Raised when a user-requested abort has been durably observed."""


@dataclass(frozen=True, slots=True)
class ConversationLifecycle:
    """Semantic owner-visible response plus host lifecycle disposition."""

    disposition: ConversationDisposition
    content: str
    grounding: TerminalGrounding | None = None


@dataclass(frozen=True, slots=True)
class TerminalGrounding:
    query_id: str
    item_ids: tuple[str, ...]
    acknowledge_incomplete: bool = False
    acknowledge_stale: bool = False


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
    message_index: int | None
    tool_call_id: str | None = None
    tool_name: str | None = None
    lifecycle_disposition: ConversationDisposition | None = None
    batch_outcome: ToolBatchOutcome | None = None
    resolution_disposition: HostLifecycleDisposition | ConversationDisposition | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class PreModelContext:
    """Canonical loop state supplied to a host-owned context assembler."""

    canonical_messages: tuple[BaseMessage, ...]
    tools: tuple[Mapping[str, Any], ...]
    turn: int
    turn_limit: int


@dataclass(frozen=True, slots=True)
class ToolCallOutcome:
    """Durable outcome for one assistant-selected tool call."""

    call_id: str
    name: str
    effect_class: ToolSideEffectClass | None
    status: ToolCallOutcomeStatus
    safe_error_code: str | None = None
    retryable: bool = True
    checkpoint_status: CheckpointStatus = "not_configured"


@dataclass(frozen=True, slots=True)
class ToolBatchOutcome:
    """Ordered outcome for every tool call in one assistant message."""

    turn: int
    outcomes: tuple[ToolCallOutcome, ...]
    messages: tuple[BaseMessage, ...]


@dataclass(frozen=True, slots=True)
class HostLifecycleResolution:
    """Closed host-owned lifecycle decision after text or a durable tool batch."""

    disposition: HostLifecycleDisposition
    content: str = ""
    error_code: str | None = None
    diagnostics: Mapping[str, object] | None = None
    lifecycle: ConversationLifecycle | None = None


@dataclass(frozen=True, slots=True)
class PostToolLifecycleContext:
    """Trusted transcript boundary available to host-owned lifecycle finalizers."""

    turn: int
    messages: tuple[BaseMessage, ...]
    trigger: Literal["tool_batch", "assistant_response", "tool_result"] = "tool_batch"
    batch: ToolBatchOutcome | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None


type PostToolLifecycleResolver = Callable[
    [PostToolLifecycleContext],
    Awaitable[HostLifecycleResolution | ConversationLifecycle | None]
    | HostLifecycleResolution
    | ConversationLifecycle
    | None,
]


@dataclass(frozen=True, slots=True)
class AgentHarnessResult:
    """Final harness result and the message transcript given back to callers."""

    status: HarnessStatus
    final_response: str
    turns: int
    messages: tuple[BaseMessage, ...]
    lifecycle_disposition: ConversationDisposition | None = None
    error_code: str | None = None
    diagnostics: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class _ToolCall:
    call_id: str
    name: str
    args: object
    malformed_error: str | None = None


@dataclass(slots=True)
class _PendingEventBudget:
    remaining: int = MAX_MODEL_PENDING_EVENTS


class AgentHarnessError(RuntimeError):
    """Classified host-side failure that must not leak raw exception text."""

    def __init__(
        self,
        code: str,
        *,
        diagnostics: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.diagnostics = diagnostics


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
    lifecycle_renderer: LifecycleRenderer | None = None,
    pre_model_context_hook: PreModelContextHook | None = None,
    post_tool_lifecycle_resolver: PostToolLifecycleResolver | None = None,
    _model_pending_elapsed_seconds: Sequence[float] = MODEL_PENDING_ELAPSED_SECONDS,
    _model_pending_repeat_seconds: float = MODEL_PENDING_REPEAT_SECONDS,
) -> AgentHarnessResult:
    try:
        return await _run_native_tool_loop_impl(
            gateway=gateway,
            user_input=user_input,
            tools=tools,
            system_message=system_message,
            max_turns=max_turns,
            event_sink=event_sink,
            checkpoint_sink=checkpoint_sink,
            abort_check=abort_check,
            restored_messages=restored_messages,
            require_terminal_response=require_terminal_response,
            lifecycle_validator=lifecycle_validator,
            lifecycle_renderer=lifecycle_renderer,
            pre_model_context_hook=pre_model_context_hook,
            post_tool_lifecycle_resolver=post_tool_lifecycle_resolver,
            _model_pending_elapsed_seconds=_model_pending_elapsed_seconds,
            _model_pending_repeat_seconds=_model_pending_repeat_seconds,
        )
    except AgentHarnessError as exc:
        return AgentHarnessResult(
            status="failed",
            final_response=_failure_response_for_code(exc.code),
            turns=_assistant_message_count(restored_messages),
            messages=tuple(restored_messages),
            error_code=exc.code,
            diagnostics=exc.diagnostics,
        )
    except GatewayFailure as exc:
        return AgentHarnessResult(
            status="failed",
            final_response=_failure_response_for_code(exc.code),
            turns=_assistant_message_count(restored_messages),
            messages=tuple(restored_messages),
            error_code=exc.code,
            diagnostics={
                "phase": exc.phase,
                "retryable": exc.retryable,
                "request_id": str(exc.request_id),
            },
        )


async def _run_native_tool_loop_impl(
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
    lifecycle_renderer: LifecycleRenderer | None = None,
    pre_model_context_hook: PreModelContextHook | None = None,
    post_tool_lifecycle_resolver: PostToolLifecycleResolver | None = None,
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
    failed_fingerprints = _failed_tool_fingerprints_from_messages(messages)

    resume_result = await _resume_checkpointed_tail(
        messages=messages,
        tool_map=tool_map,
        require_terminal_response=require_terminal_response,
        lifecycle_validator=lifecycle_validator,
        lifecycle_renderer=lifecycle_renderer,
        post_tool_lifecycle_resolver=post_tool_lifecycle_resolver,
        checkpoint_sink=checkpoint_sink,
        event_sink=event_sink,
        abort_check=abort_check,
        restored_turns=restored_turns,
        failed_fingerprints=failed_fingerprints,
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
                raise AgentHarnessError("context_assembly_invalid")
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
        if require_terminal_response and calls:
            lifecycle, validation_error = await _validate_lifecycle_turn(
                assistant=assistant,
                calls=calls,
                messages=tuple(messages),
                lifecycle_validator=lifecycle_validator,
                allow_completed=False,
            )
            if lifecycle is not None:
                return await _complete_lifecycle_result(
                    lifecycle=lifecycle,
                    lifecycle_renderer=lifecycle_renderer,
                    messages=tuple(messages),
                    turn=turn,
                    event_sink=event_sink,
                    checkpoint_sink=None,
                )
            if validation_error is not None:
                recovered = await _resolve_assistant_lifecycle(
                    post_tool_lifecycle_resolver,
                    turn=turn,
                    lifecycle_validator=lifecycle_validator,
                    lifecycle_renderer=lifecycle_renderer,
                    event_sink=event_sink,
                    checkpoint_sink=checkpoint_sink,
                    messages=tuple(messages),
                )
                if recovered is not None:
                    return recovered
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
            recovered = await _resolve_assistant_lifecycle(
                post_tool_lifecycle_resolver,
                turn=turn,
                lifecycle_validator=lifecycle_validator,
                lifecycle_renderer=lifecycle_renderer,
                event_sink=event_sink,
                checkpoint_sink=checkpoint_sink,
                messages=tuple(messages),
            )
            if recovered is not None:
                return recovered
            if require_terminal_response:
                if _has_successful_non_read_tool_after_latest_human(messages):
                    return await _fail_with_code(
                        code="lifecycle_invalid_terminal_response",
                        event_sink=event_sink,
                        messages=tuple(messages),
                        turn=turn,
                    )
                if _current_turn_only_failed_tools(messages):
                    return await _fail_closed_lifecycle(
                        event_sink=event_sink,
                        messages=tuple(messages),
                        turn=turn,
                    )
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
        batch_outcomes: list[ToolCallOutcome] = []
        deferred_failures: list[tuple[ToolCallOutcome, str]] = []
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
            checkpoint_status = await _checkpoint(
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
            outcome = _tool_call_outcome(
                call,
                tool,
                tool_message,
                checkpoint_status=checkpoint_status,
            )
            batch_outcomes.append(outcome)
            repeated = _repeated_failure_code(outcome, call, failed_fingerprints)
            if repeated is not None:
                deferred_failures.append((outcome, repeated))
        blocking_failure = _blocking_batch_failure(batch_outcomes, deferred_failures)
        if blocking_failure is not None:
            return await _fail_with_code(
                code=blocking_failure,
                event_sink=event_sink,
                messages=tuple(messages),
                turn=turn,
                final_response=(
                    _REPEATED_TOOL_FAILURE_RESPONSE
                    if blocking_failure == "repeated_failed_tool_call"
                    else None
                ),
            )
        finalized = await _resolve_tool_batch_lifecycle(
            post_tool_lifecycle_resolver,
            turn=turn,
            outcomes=tuple(batch_outcomes),
            lifecycle_validator=lifecycle_validator,
            lifecycle_renderer=lifecycle_renderer,
            event_sink=event_sink,
            checkpoint_sink=checkpoint_sink,
            messages=tuple(messages),
        )
        if finalized is not None:
            return finalized
        if deferred_failures:
            failure_code = deferred_failures[0][1]
            return await _fail_with_code(
                code=failure_code,
                event_sink=event_sink,
                messages=tuple(messages),
                turn=turn,
                final_response=(
                    _REPEATED_TOOL_FAILURE_RESPONSE
                    if failure_code == "repeated_failed_tool_call"
                    else None
                ),
            )

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
        error_code="turn_limit",
    )


async def _resume_checkpointed_tail(
    *,
    messages: list[BaseMessage],
    tool_map: Mapping[str, NativeTool],
    require_terminal_response: bool,
    lifecycle_validator: LifecycleValidator | None,
    lifecycle_renderer: LifecycleRenderer | None,
    post_tool_lifecycle_resolver: PostToolLifecycleResolver | None,
    checkpoint_sink: CheckpointSink | None,
    event_sink: EventSink | None,
    abort_check: AbortCheck | None,
    restored_turns: int,
    failed_fingerprints: set[str],
) -> AgentHarnessResult | bool:
    tail = _checkpointed_assistant_tail(messages)
    if tail is None:
        return False
    _assistant_index, assistant, suffix = tail
    turn = max(1, restored_turns)
    calls = _tool_calls(assistant, turn)
    if not calls:
        if require_terminal_response:
            recovered = await _resolve_assistant_lifecycle(
                post_tool_lifecycle_resolver,
                turn=turn,
                lifecycle_validator=lifecycle_validator,
                lifecycle_renderer=lifecycle_renderer,
                event_sink=event_sink,
                checkpoint_sink=checkpoint_sink,
                messages=tuple(messages),
            )
            if recovered is not None:
                return recovered
            if _has_successful_non_read_tool_after_latest_human(messages):
                return await _fail_with_code(
                    code="lifecycle_invalid_terminal_response",
                    event_sink=event_sink,
                    messages=tuple(messages),
                    turn=turn,
                )
            if _current_turn_only_failed_tools(messages):
                return await _fail_closed_lifecycle(
                    event_sink=event_sink,
                    messages=tuple(messages),
                    turn=turn,
                )
        text = _visible_text(assistant)
        await _emit(
            event_sink,
            AgentHarnessEvent(kind="final_response", turn=turn, content=text),
        )
        return AgentHarnessResult(
            status="completed",
            final_response=text,
            turns=turn,
            messages=tuple(messages),
        )
    if require_terminal_response:
        lifecycle, validation_error = await _validate_lifecycle_turn(
            assistant=assistant,
            calls=calls,
            messages=tuple(messages),
            lifecycle_validator=lifecycle_validator,
            allow_completed=True,
        )
        if lifecycle is not None:
            return await _complete_lifecycle_result(
                lifecycle=lifecycle,
                lifecycle_renderer=lifecycle_renderer,
                messages=tuple(messages),
                turn=turn,
                event_sink=event_sink,
                checkpoint_sink=None,
            )
        if validation_error is not None:
            recovered = await _resolve_assistant_lifecycle(
                post_tool_lifecycle_resolver,
                turn=turn,
                lifecycle_validator=lifecycle_validator,
                lifecycle_renderer=lifecycle_renderer,
                event_sink=event_sink,
                checkpoint_sink=checkpoint_sink,
                messages=tuple(messages),
            )
            if recovered is not None:
                return recovered
            messages.append(SystemMessage(content=_lifecycle_correction(validation_error)))
            return True

    matched_tool_call_ids = _matched_tool_call_ids(suffix)
    tool_results = _tool_result_by_call_id(suffix)
    batch_outcomes: list[ToolCallOutcome] = []
    deferred_failures: list[tuple[ToolCallOutcome, str]] = []
    for index, call in enumerate(calls):
        if call.call_id in matched_tool_call_ids:
            tool = tool_map.get(call.name)
            existing_tool_message = tool_results.get(call.call_id)
            if existing_tool_message is not None:
                batch_outcomes.append(
                    _tool_call_outcome(
                        call,
                        tool,
                        existing_tool_message,
                        checkpoint_status="checkpointed",
                    )
                )
                existing_outcome = batch_outcomes[-1]
                restored_failure = _restored_terminal_failure_code(
                    existing_outcome,
                    call,
                    messages,
                )
                if restored_failure is not None:
                    deferred_failures.append((existing_outcome, restored_failure))
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
        checkpoint_status = await _checkpoint(
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
        outcome = _tool_call_outcome(
            call,
            tool,
            tool_message,
            checkpoint_status=checkpoint_status,
        )
        batch_outcomes.append(outcome)
        repeated = _repeated_failure_code(outcome, call, failed_fingerprints)
        if repeated is not None:
            deferred_failures.append((outcome, repeated))
    if batch_outcomes:
        blocking_failure = _blocking_batch_failure(batch_outcomes, deferred_failures)
        if blocking_failure is not None:
            return await _fail_with_code(
                code=blocking_failure,
                event_sink=event_sink,
                messages=tuple(messages),
                turn=turn,
                final_response=(
                    _REPEATED_TOOL_FAILURE_RESPONSE
                    if blocking_failure == "repeated_failed_tool_call"
                    else None
                ),
            )
        finalized = await _resolve_tool_batch_lifecycle(
            post_tool_lifecycle_resolver,
            turn=turn,
            outcomes=tuple(batch_outcomes),
            lifecycle_validator=lifecycle_validator,
            lifecycle_renderer=lifecycle_renderer,
            event_sink=event_sink,
            checkpoint_sink=checkpoint_sink,
            messages=tuple(messages),
        )
        if finalized is not None:
            return finalized
        if deferred_failures:
            failure_code = deferred_failures[0][1]
            return await _fail_with_code(
                code=failure_code,
                event_sink=event_sink,
                messages=tuple(messages),
                turn=turn,
                final_response=(
                    _REPEATED_TOOL_FAILURE_RESPONSE
                    if failure_code == "repeated_failed_tool_call"
                    else None
                ),
            )
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


def _tool_result_by_call_id(
    messages: Sequence[ToolMessage],
) -> dict[str, ToolMessage]:
    results: dict[str, ToolMessage] = {}
    for message in messages:
        tool_call_id = getattr(message, "tool_call_id", None)
        if isinstance(tool_call_id, str) and tool_call_id and tool_call_id not in results:
            results[tool_call_id] = message
    return results


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
    allow_completed: bool,
) -> tuple[ConversationLifecycle | None, str | None]:
    terminal_calls = [call for call in calls if call.name == TERMINAL_RESPONSE_TOOL_NAME]
    if not calls:
        return None, None
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
    lifecycle, error = _lifecycle_from_arguments(
        cast(Mapping[str, object], raw_args),
        allow_completed=allow_completed,
    )
    if lifecycle is None:
        return None, error
    if _visible_text(assistant):
        return None, f"{TERMINAL_RESPONSE_TOOL_NAME} must carry the owner response in content"
    validation_error = await _lifecycle_validation_error(
        lifecycle,
        messages,
        lifecycle_validator,
    )
    if validation_error is not None:
        return None, validation_error
    return lifecycle, None


async def _lifecycle_validation_error(
    lifecycle: ConversationLifecycle,
    messages: Sequence[BaseMessage],
    lifecycle_validator: LifecycleValidator | None,
) -> str | None:
    if lifecycle_validator is None:
        return None
    validation = lifecycle_validator(lifecycle, messages)
    if inspect.isawaitable(validation):
        validation = await validation
    if validation is None:
        return None
    cleaned = str(validation).strip()
    return cleaned or None


def _lifecycle_from_arguments(
    arguments: Mapping[str, object],
    *,
    allow_completed: bool = True,
) -> tuple[ConversationLifecycle | None, str]:
    disposition = arguments.get("disposition")
    content = arguments.get("content")
    raw_grounding = arguments.get("grounding")
    allowed_dispositions = {"awaiting_user", "completed"} if allow_completed else {"awaiting_user"}
    if disposition not in allowed_dispositions:
        if allow_completed:
            return None, "terminal response disposition must be awaiting_user or completed"
        return None, "terminal response disposition must be awaiting_user"
    if not isinstance(content, str) or not content.strip():
        return None, "terminal response content must be non-empty text"
    grounding = None
    if raw_grounding is not None:
        if not isinstance(raw_grounding, Mapping):
            return None, "terminal response grounding must be an object"
        grounding_map = cast(Mapping[str, object], raw_grounding)
        if set(grounding_map) != {
            "query_id",
            "item_ids",
            "acknowledge_incomplete",
            "acknowledge_stale",
        }:
            return None, "terminal response grounding fields are invalid"
        query_id = grounding_map.get("query_id")
        item_ids = grounding_map.get("item_ids")
        acknowledge_incomplete = grounding_map.get("acknowledge_incomplete")
        acknowledge_stale = grounding_map.get("acknowledge_stale")
        if not isinstance(query_id, str) or not query_id.strip():
            return None, "terminal response grounding query_id is invalid"
        if not isinstance(item_ids, Sequence) or isinstance(item_ids, str | bytes):
            return None, "terminal response grounding item_ids are invalid"
        normalized_ids = tuple(cast(Sequence[object], item_ids))
        if len(normalized_ids) > 20 or not all(
            isinstance(item, str) and item for item in normalized_ids
        ):
            return None, "terminal response grounding item_ids are invalid"
        if len(set(normalized_ids)) != len(normalized_ids):
            return None, "terminal response grounding item_ids must be unique"
        if not isinstance(acknowledge_incomplete, bool) or not isinstance(acknowledge_stale, bool):
            return None, "terminal response grounding acknowledgements are invalid"
        grounding = TerminalGrounding(
            query_id=query_id,
            item_ids=cast(tuple[str, ...], normalized_ids),
            acknowledge_incomplete=acknowledge_incomplete,
            acknowledge_stale=acknowledge_stale,
        )
    return (
        ConversationLifecycle(
            disposition=cast(ConversationDisposition, disposition),
            content=content,
            grounding=grounding,
        ),
        "",
    )


async def _render_lifecycle(
    renderer: LifecycleRenderer | None,
    lifecycle: ConversationLifecycle,
    messages: Sequence[BaseMessage],
) -> str:
    if renderer is None:
        return lifecycle.content
    rendered = renderer(lifecycle, messages)
    if inspect.isawaitable(rendered):
        rendered = await rendered
    text = str(rendered).strip()
    return text or lifecycle.content


async def _complete_lifecycle_result(
    *,
    lifecycle: ConversationLifecycle,
    lifecycle_renderer: LifecycleRenderer | None,
    messages: Sequence[BaseMessage],
    turn: int,
    event_sink: EventSink | None,
    checkpoint_sink: CheckpointSink | None = None,
) -> AgentHarnessResult:
    del checkpoint_sink
    rendered_content = await _render_lifecycle(lifecycle_renderer, lifecycle, messages)
    await _emit(
        event_sink,
        AgentHarnessEvent(
            kind="final_response",
            turn=turn,
            content=rendered_content,
            lifecycle_disposition=lifecycle.disposition,
        ),
    )
    return AgentHarnessResult(
        status=("awaiting_user" if lifecycle.disposition == "awaiting_user" else "completed"),
        final_response=rendered_content,
        turns=turn,
        messages=tuple(messages),
        lifecycle_disposition=lifecycle.disposition,
    )


async def _resolve_assistant_lifecycle(
    resolver: PostToolLifecycleResolver | None,
    *,
    turn: int,
    lifecycle_validator: LifecycleValidator | None,
    lifecycle_renderer: LifecycleRenderer | None,
    event_sink: EventSink | None,
    checkpoint_sink: CheckpointSink | None,
    messages: tuple[BaseMessage, ...],
) -> AgentHarnessResult | None:
    if resolver is None:
        return None
    try:
        lifecycle_result: Any = resolver(
            PostToolLifecycleContext(
                turn=turn,
                messages=messages,
                trigger="assistant_response",
            )
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise AgentHarnessError("host_lifecycle_resolver_failed") from exc
    if inspect.isawaitable(lifecycle_result):
        try:
            lifecycle_result = await lifecycle_result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise AgentHarnessError("host_lifecycle_resolver_failed") from exc
    if lifecycle_result is None:
        return None
    if (
        isinstance(lifecycle_result, HostLifecycleResolution)
        and lifecycle_result.disposition == "continue_model"
    ):
        return None
    return await _complete_host_resolution(
        lifecycle_result,
        lifecycle_validator=lifecycle_validator,
        lifecycle_renderer=lifecycle_renderer,
        event_sink=event_sink,
        checkpoint_sink=checkpoint_sink,
        batch_outcome=None,
        messages=messages,
        turn=turn,
    )


async def _resolve_tool_batch_lifecycle(
    resolver: PostToolLifecycleResolver | None,
    *,
    turn: int,
    outcomes: tuple[ToolCallOutcome, ...],
    lifecycle_validator: LifecycleValidator | None,
    lifecycle_renderer: LifecycleRenderer | None,
    event_sink: EventSink | None,
    checkpoint_sink: CheckpointSink | None,
    messages: tuple[BaseMessage, ...],
) -> AgentHarnessResult | None:
    if resolver is None:
        return None
    last_success = next((item for item in reversed(outcomes) if item.status == "success"), None)
    batch = ToolBatchOutcome(turn=turn, outcomes=outcomes, messages=messages)
    try:
        lifecycle_result: Any = resolver(
            PostToolLifecycleContext(
                turn=turn,
                messages=messages,
                trigger="tool_batch",
                batch=batch,
                tool_call_id=last_success.call_id if last_success is not None else None,
                tool_name=last_success.name if last_success is not None else None,
            )
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise AgentHarnessError("host_lifecycle_resolver_failed") from exc
    if inspect.isawaitable(lifecycle_result):
        try:
            lifecycle_result = await lifecycle_result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise AgentHarnessError("host_lifecycle_resolver_failed") from exc
    if lifecycle_result is None:
        return None
    if (
        isinstance(lifecycle_result, HostLifecycleResolution)
        and lifecycle_result.disposition == "continue_model"
    ):
        return None
    return await _complete_host_resolution(
        lifecycle_result,
        lifecycle_validator=lifecycle_validator,
        lifecycle_renderer=lifecycle_renderer,
        event_sink=event_sink,
        checkpoint_sink=checkpoint_sink,
        batch_outcome=batch,
        messages=messages,
        turn=turn,
    )


async def _complete_host_resolution(
    result: HostLifecycleResolution | ConversationLifecycle,
    *,
    lifecycle_validator: LifecycleValidator | None,
    lifecycle_renderer: LifecycleRenderer | None,
    event_sink: EventSink | None,
    checkpoint_sink: CheckpointSink | None,
    batch_outcome: ToolBatchOutcome | None,
    messages: tuple[BaseMessage, ...],
    turn: int,
) -> AgentHarnessResult:
    if isinstance(result, ConversationLifecycle):
        lifecycle = result
    else:
        if result.disposition == "continue_model":
            raise AgentHarnessError("host_lifecycle_resolver_failed")
        if result.disposition == "failed":
            await _checkpoint_batch_resolution(
                checkpoint_sink,
                batch_outcome=batch_outcome,
                messages=messages,
                turn=turn,
                disposition="failed",
                error_code=result.error_code or "host_lifecycle_resolver_failed",
            )
            return await _fail_with_code(
                code=result.error_code or "host_lifecycle_resolver_failed",
                event_sink=event_sink,
                messages=messages,
                turn=turn,
                final_response=result.content or _HOST_LIFECYCLE_FAILURE_RESPONSE,
                diagnostics=result.diagnostics,
            )
        if result.disposition == "awaiting_user":
            lifecycle = result.lifecycle or ConversationLifecycle(
                disposition="awaiting_user",
                content=result.content,
            )
        elif result.disposition == "complete":
            lifecycle = result.lifecycle or ConversationLifecycle(
                disposition="completed",
                content=result.content,
            )
        else:
            return await _fail_with_code(
                code="host_lifecycle_resolver_failed",
                event_sink=event_sink,
                messages=messages,
                turn=turn,
                final_response=_HOST_LIFECYCLE_FAILURE_RESPONSE,
            )
    if lifecycle.disposition not in {"awaiting_user", "completed"}:
        await _checkpoint_batch_resolution(
            checkpoint_sink,
            batch_outcome=batch_outcome,
            messages=messages,
            turn=turn,
            disposition="failed",
            error_code="host_lifecycle_resolver_failed",
        )
        return await _fail_with_code(
            code="host_lifecycle_resolver_failed",
            event_sink=event_sink,
            messages=messages,
            turn=turn,
            final_response=_HOST_LIFECYCLE_FAILURE_RESPONSE,
        )
    validation_error = await _lifecycle_validation_error(
        lifecycle,
        messages,
        lifecycle_validator,
    )
    if validation_error is not None:
        await _checkpoint_batch_resolution(
            checkpoint_sink,
            batch_outcome=batch_outcome,
            messages=messages,
            turn=turn,
            disposition="failed",
            error_code="host_lifecycle_resolver_failed",
        )
        return await _fail_with_code(
            code="host_lifecycle_resolver_failed",
            event_sink=event_sink,
            messages=messages,
            turn=turn,
            final_response=_HOST_LIFECYCLE_FAILURE_RESPONSE,
        )
    rendered_content = await _render_lifecycle(lifecycle_renderer, lifecycle, messages)
    if not str(rendered_content).strip():
        await _checkpoint_batch_resolution(
            checkpoint_sink,
            batch_outcome=batch_outcome,
            messages=messages,
            turn=turn,
            disposition="failed",
            error_code="host_lifecycle_resolver_failed",
        )
        return await _fail_with_code(
            code="host_lifecycle_resolver_failed",
            event_sink=event_sink,
            messages=messages,
            turn=turn,
            final_response=_HOST_LIFECYCLE_FAILURE_RESPONSE,
        )
    await _checkpoint_batch_resolution(
        checkpoint_sink,
        batch_outcome=batch_outcome,
        messages=messages,
        turn=turn,
        disposition=lifecycle.disposition,
        error_code=None,
    )
    await _emit(
        event_sink,
        AgentHarnessEvent(
            kind="final_response",
            turn=turn,
            content=rendered_content,
            lifecycle_disposition=lifecycle.disposition,
        ),
    )
    return AgentHarnessResult(
        status=("awaiting_user" if lifecycle.disposition == "awaiting_user" else "completed"),
        final_response=rendered_content,
        turns=turn,
        messages=messages,
        lifecycle_disposition=lifecycle.disposition,
    )


def _lifecycle_correction(reason: str) -> str:
    cleaned = reason.strip()[:MAX_JSON_STRING_CHARS] or "invalid terminal response"
    return f"{_LIFECYCLE_CORRECTION_PREFIX} Contract error: {cleaned}."


async def _fail_closed_lifecycle(
    *,
    event_sink: EventSink | None,
    messages: Sequence[BaseMessage],
    turn: int,
    failure_response: str = _LIFECYCLE_FAILURE_RESPONSE,
) -> AgentHarnessResult:
    actionable_failure = _actionable_tool_failure(messages)
    if actionable_failure is not None:
        await _emit(
            event_sink,
            AgentHarnessEvent(
                kind="final_response",
                turn=turn,
                content=actionable_failure,
                lifecycle_disposition="completed",
            ),
        )
        return AgentHarnessResult(
            status="completed",
            final_response=actionable_failure,
            turns=turn,
            messages=tuple(messages),
            lifecycle_disposition="completed",
        )
    await _emit(
        event_sink,
        AgentHarnessEvent(
            kind="final_response",
            turn=turn,
            content=failure_response,
        ),
    )
    return AgentHarnessResult(
        status="failed",
        final_response=failure_response,
        turns=turn,
        messages=tuple(messages),
        error_code="lifecycle_invalid_terminal_response",
    )


async def _fail_with_code(
    *,
    code: str,
    event_sink: EventSink | None,
    messages: Sequence[BaseMessage],
    turn: int,
    final_response: str | None = None,
    diagnostics: Mapping[str, object] | None = None,
) -> AgentHarnessResult:
    response = final_response or _failure_response_for_code(code)
    await _emit(
        event_sink,
        AgentHarnessEvent(
            kind="final_response",
            turn=turn,
            content=response,
        ),
    )
    return AgentHarnessResult(
        status="failed",
        final_response=response,
        turns=turn,
        messages=tuple(messages),
        error_code=code,
        diagnostics=diagnostics,
    )


def _failure_response_for_code(code: str) -> str:
    if code == "repeated_failed_tool_call":
        return _REPEATED_TOOL_FAILURE_RESPONSE
    if code == "turn_limit":
        return "The agent reached its turn limit before producing a final response."
    if code == "host_lifecycle_resolver_failed":
        return _HOST_LIFECYCLE_FAILURE_RESPONSE
    if code in {"event_sink_failed", "checkpoint_sink_failed"}:
        return "The agent could not safely persist or publish its progress. Please try again."
    if code.startswith("model_") or code == "invalid_native_response":
        return (
            "The model request failed before the agent could complete the turn. Please try again."
        )
    return "The agent could not safely complete that request. Please try again."


def _actionable_tool_failure(messages: Sequence[BaseMessage]) -> str | None:
    """Render a safe terminal error when the model cannot repair the lifecycle call.

    This fallback is intentionally limited to turns containing only failed tool calls.
    A turn with any successful tool result may have prepared a proposal, so it continues
    to fail closed through the ordinary lifecycle path.
    """

    current_turn: list[ToolMessage] = []
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            break
        if isinstance(message, ToolMessage):
            current_turn.append(message)
    if not current_turn or any(message.status != "error" for message in current_turn):
        return None
    latest = current_turn[0]
    try:
        decoded = json.loads(str(latest.content))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, Mapping):
        return None
    payload = cast(Mapping[str, object], decoded)
    if payload.get("status") != "error":
        return None
    raw_error = payload.get("error")
    if isinstance(raw_error, str):
        message = raw_error
    elif isinstance(raw_error, Mapping):
        candidate = cast(Mapping[str, object], raw_error).get("message")
        message = candidate if isinstance(candidate, str) else None
    else:
        message = None
    if message is None or not message.strip():
        return None
    safe_message = message.strip()[:MAX_JSON_STRING_CHARS]
    return (
        "I couldn't complete that request because the required data was unavailable: "
        f"{safe_message} No change was made."
    )


def _current_turn_only_failed_tools(messages: Sequence[BaseMessage]) -> bool:
    current_turn: list[ToolMessage] = []
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            break
        if isinstance(message, ToolMessage):
            current_turn.append(message)
    return bool(current_turn) and all(message.status == "error" for message in current_turn)


def _has_successful_non_read_tool_after_latest_human(messages: Sequence[BaseMessage]) -> bool:
    latest_tools = _tools_after_latest_human(messages)
    return any(
        message.status == "success" and _tool_result_status(message) == "review_required"
        for message in latest_tools
    )


def _tools_after_latest_human(messages: Sequence[BaseMessage]) -> tuple[ToolMessage, ...]:
    current_turn: list[ToolMessage] = []
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            break
        if isinstance(message, ToolMessage):
            current_turn.append(message)
    return tuple(reversed(current_turn))


def _tool_result_status(message: ToolMessage) -> str | None:
    try:
        decoded = json.loads(str(message.content))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, Mapping):
        return None
    status = cast(Mapping[str, object], decoded).get("status")
    return status if isinstance(status, str) else None


async def _execute_tool_call(
    call: _ToolCall,
    tools: Mapping[str, NativeTool],
    turn: int,
    abort_check: AbortCheck | None,
) -> tuple[ToolMessage, AgentHarnessEvent]:
    if call.malformed_error is not None:
        code = (
            "tool_arguments_invalid"
            if call.malformed_error == "tool arguments must be an object"
            else "tool_malformed_call"
        )
        return _tool_contract_error(call, code=code, message=call.malformed_error, turn=turn)
    tool = tools.get(call.name)
    if tool is None:
        return _tool_contract_error(
            call,
            code="tool_unknown",
            message=f"unknown tool: {call.name}",
            turn=turn,
            retryable=False,
        )
    call_args: object = call.args
    if not isinstance(call_args, Mapping):
        return _tool_contract_error(
            call,
            code="tool_arguments_invalid",
            message="tool arguments must be an object",
            turn=turn,
            tool_activity=tool.activity,
            tool_side_effect_class=tool.side_effect_class,
        )
    arg_mapping: Mapping[object, object] = cast(Mapping[object, object], call_args)
    args: dict[str, object] = {}
    for key, item in arg_mapping.items():
        args[str(key)] = item
    try:
        await _raise_if_abort_requested(abort_check)
        result = await tool.handler(args)
    except ToolResultOversizeError:
        return _tool_contract_error(
            call,
            code="tool_result_oversize",
            message=(
                "The tool result exceeded the model payload budget; retry with narrower "
                "filters or a smaller limit."
            ),
            turn=turn,
            tool_activity=tool.activity,
            tool_side_effect_class=tool.side_effect_class,
            retryable=True,
        )
    except ToolExecutionError as exc:
        return _tool_contract_error(
            call,
            code=exc.code,
            message=_safe_exception_text(exc),
            turn=turn,
            tool_activity=tool.activity,
            tool_side_effect_class=tool.side_effect_class,
            retryable=exc.retryable,
        )
    except ValidationError:
        return _tool_contract_error(
            call,
            code="tool_arguments_invalid",
            message="tool arguments failed validation",
            turn=turn,
            tool_activity=tool.activity,
            tool_side_effect_class=tool.side_effect_class,
            retryable=True,
        )
    except Exception as exc:
        return _tool_contract_error(
            call,
            code="tool_execution_failed",
            message=_safe_exception_text(exc),
            turn=turn,
            tool_activity=tool.activity,
            tool_side_effect_class=tool.side_effect_class,
            retryable=True,
        )
    payload = _tool_result_payload(result)
    try:
        result_json = _json_for_tool_message(payload)
    except (TypeError, ValueError):
        return _tool_contract_error(
            call,
            code="tool_result_not_serializable",
            message="The tool returned a result that could not be serialized safely.",
            turn=turn,
            tool_activity=tool.activity,
            tool_side_effect_class=tool.side_effect_class,
            retryable=True,
        )
    if result_json is None:
        return _tool_contract_error(
            call,
            code="tool_result_oversize",
            message=(
                "The tool result exceeded the model payload budget; retry with narrower "
                "filters, a smaller limit, or the provided pagination contract."
            ),
            turn=turn,
            tool_activity=tool.activity,
            tool_side_effect_class=tool.side_effect_class,
        )
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


def _tool_call_outcome(
    call: _ToolCall,
    tool: NativeTool | None,
    tool_message: ToolMessage,
    *,
    checkpoint_status: CheckpointStatus,
) -> ToolCallOutcome:
    safe_error_code = None
    retryable = True
    if tool_message.status == "error":
        safe_error_code = _tool_error_code(tool_message)
        retryable = _tool_error_retryable(tool_message)
    return ToolCallOutcome(
        call_id=call.call_id,
        name=call.name,
        effect_class=tool.side_effect_class if tool is not None else None,
        status="success" if tool_message.status == "success" else "error",
        safe_error_code=safe_error_code,
        retryable=retryable,
        checkpoint_status=checkpoint_status,
    )


def _tool_error_code(message: ToolMessage) -> str:
    try:
        decoded = json.loads(str(message.content))
    except (TypeError, ValueError, json.JSONDecodeError):
        return "tool_execution_failed"
    if not isinstance(decoded, Mapping):
        return "tool_execution_failed"
    raw_error = cast(Mapping[str, object], decoded).get("error")
    if isinstance(raw_error, Mapping):
        code = cast(Mapping[str, object], raw_error).get("code")
        if isinstance(code, str) and code:
            return code
    return "tool_execution_failed"


def _tool_error_retryable(message: ToolMessage) -> bool:
    try:
        decoded = json.loads(str(message.content))
    except (TypeError, ValueError, json.JSONDecodeError):
        return True
    if not isinstance(decoded, Mapping):
        return True
    raw_error = cast(Mapping[str, object], decoded).get("error")
    if isinstance(raw_error, Mapping):
        retryable = cast(Mapping[str, object], raw_error).get("retryable")
        if isinstance(retryable, bool):
            return retryable
    return True


def _failed_tool_fingerprints_from_messages(messages: Sequence[BaseMessage]) -> set[str]:
    fingerprints: set[str] = set()
    for index, message in enumerate(messages):
        if not isinstance(message, AIMessage):
            continue
        calls = _tool_calls(message, index)
        calls_by_id = {call.call_id: call for call in calls}
        cursor = index + 1
        while cursor < len(messages) and isinstance(messages[cursor], ToolMessage):
            tool_message = cast(ToolMessage, messages[cursor])
            if tool_message.status == "error":
                tool_call_id = getattr(tool_message, "tool_call_id", None)
                if isinstance(tool_call_id, str) and tool_call_id in calls_by_id:
                    fingerprints.add(
                        _failed_tool_fingerprint(
                            calls_by_id[tool_call_id],
                            _tool_error_code(tool_message),
                        )
                    )
            cursor += 1
    return fingerprints


def _repeated_failure_code(
    outcome: ToolCallOutcome,
    call: _ToolCall,
    failed_fingerprints: set[str],
) -> str | None:
    if outcome.status != "error" or outcome.safe_error_code is None:
        return None
    if outcome.safe_error_code == "tool_unknown":
        return "tool_unknown"
    if outcome.safe_error_code in _REPAIRABLE_ARGUMENT_ERROR_CODES:
        fingerprint = _failed_tool_fingerprint(call, outcome.safe_error_code)
        if fingerprint in failed_fingerprints:
            return "repeated_failed_tool_call"
        failed_fingerprints.add(fingerprint)
        return None
    if not outcome.retryable:
        return outcome.safe_error_code
    if outcome.effect_class not in {None, "read_only"}:
        return outcome.safe_error_code
    fingerprint = _failed_tool_fingerprint(call, outcome.safe_error_code)
    if fingerprint in failed_fingerprints:
        return "repeated_failed_tool_call"
    failed_fingerprints.add(fingerprint)
    return None


def _blocking_batch_failure(
    outcomes: Sequence[ToolCallOutcome],
    deferred_failures: Sequence[tuple[ToolCallOutcome, str]],
) -> str | None:
    """Return a failure that must win before host rendering.

    A failed write or malformed/unknown call is safety-critical even beside an
    independent read success. A read-only failure may be degraded by the host
    when another call in the same durable batch succeeded.
    """

    if not deferred_failures:
        return None
    has_success = any(outcome.status == "success" for outcome in outcomes)
    for outcome, code in deferred_failures:
        if outcome.effect_class != "read_only":
            return code
    if not has_success:
        return deferred_failures[0][1]
    return None


def _restored_terminal_failure_code(
    outcome: ToolCallOutcome,
    call: _ToolCall,
    messages: Sequence[BaseMessage],
) -> str | None:
    """Reconstruct terminal failure control from an already durable result."""

    if outcome.status != "error" or outcome.safe_error_code is None:
        return None
    if outcome.safe_error_code == "tool_unknown":
        return outcome.safe_error_code
    if not outcome.retryable or outcome.effect_class != "read_only":
        return outcome.safe_error_code
    fingerprint = _failed_tool_fingerprint(call, outcome.safe_error_code)
    if _failed_tool_fingerprint_count(messages, fingerprint) >= 2:
        return "repeated_failed_tool_call"
    return None


def _failed_tool_fingerprint_count(messages: Sequence[BaseMessage], fingerprint: str) -> int:
    count = 0
    for index, message in enumerate(messages):
        if not isinstance(message, AIMessage):
            continue
        calls_by_id = {call.call_id: call for call in _tool_calls(message, index)}
        cursor = index + 1
        while cursor < len(messages) and isinstance(messages[cursor], ToolMessage):
            tool_message = cast(ToolMessage, messages[cursor])
            tool_call_id = getattr(tool_message, "tool_call_id", None)
            call = calls_by_id.get(tool_call_id) if isinstance(tool_call_id, str) else None
            if (
                call is not None
                and tool_message.status == "error"
                and _failed_tool_fingerprint(call, _tool_error_code(tool_message)) == fingerprint
            ):
                count += 1
            cursor += 1
    return count


def _failed_tool_fingerprint(call: _ToolCall, safe_error_code: str) -> str:
    return "|".join((call.name, _safe_json(call.args), safe_error_code))


def _tool_contract_error(
    call: _ToolCall,
    *,
    code: str,
    message: str,
    turn: int,
    tool_activity: str | None = None,
    tool_side_effect_class: ToolSideEffectClass | None = None,
    retryable: bool = True,
) -> tuple[ToolMessage, AgentHarnessEvent]:
    payload = {
        "status": "error",
        "error": {"code": code, "message": message, "retryable": retryable},
    }
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
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
            error=f"{code}: {message}",
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
    try:
        await sink(event)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise AgentHarnessError(
            "event_sink_failed",
            diagnostics={"event_kind": event.kind},
        ) from exc


async def _checkpoint(
    sink: CheckpointSink | None,
    checkpoint: AgentTranscriptCheckpoint,
) -> CheckpointStatus:
    if sink is None:
        return "not_configured"
    try:
        await sink(checkpoint)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise AgentHarnessError(
            "checkpoint_sink_failed",
            diagnostics={"checkpoint_kind": checkpoint.kind},
        ) from exc
    return "checkpointed"


async def _checkpoint_batch_resolution(
    sink: CheckpointSink | None,
    *,
    batch_outcome: ToolBatchOutcome | None,
    messages: tuple[BaseMessage, ...],
    turn: int,
    disposition: HostLifecycleDisposition | ConversationDisposition,
    error_code: str | None,
) -> CheckpointStatus:
    if batch_outcome is None:
        return "not_configured"
    return await _checkpoint(
        sink,
        AgentTranscriptCheckpoint(
            kind="batch_resolution",
            turn=turn,
            messages=messages,
            message_index=None,
            batch_outcome=batch_outcome,
            resolution_disposition=disposition,
            error_code=error_code,
        ),
    )


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
    encoded = _encode_json(value)
    if len(encoded) <= max_chars:
        return encoded
    return json.dumps(
        {"truncated": True, "preview": encoded[: max_chars - 128]},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _json_for_tool_message(value: object, *, max_chars: int = MAX_EVENT_JSON_CHARS) -> str | None:
    encoded = json.dumps(
        value,
        default=_model_json_default,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return encoded if len(encoded) <= max_chars else None


def _model_json_default(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"{value.__class__.__name__} is not JSON serializable")


def _encode_json(value: object) -> str:
    return json.dumps(
        _json_safe(value),
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
    "AgentHarnessError",
    "AgentHarnessEvent",
    "AgentHarnessGateway",
    "AgentHarnessResult",
    "AgentTranscriptCheckpoint",
    "CheckpointSink",
    "CheckpointStatus",
    "ConversationDisposition",
    "ConversationLifecycle",
    "HostLifecycleDisposition",
    "HostLifecycleResolution",
    "LifecycleRenderer",
    "LifecycleValidator",
    "NativeTool",
    "NativeToolHandler",
    "PostToolLifecycleContext",
    "PostToolLifecycleResolver",
    "TerminalGrounding",
    "ToolBatchOutcome",
    "ToolCallOutcome",
    "ToolCallOutcomeStatus",
    "ToolExecutionError",
    "ToolExecutionResult",
    "ToolResultOversizeError",
    "ToolSideEffectClass",
    "TranscriptCheckpointKind",
    "UserAbortRequested",
    "run_native_tool_loop",
]
