"""Versioned contracts for durable native conversation artifacts."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal, cast

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
    messages_from_dict,
    messages_to_dict,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator

NativeConversationState = Literal[
    "processing",
    "awaiting_user",
    "completed",
    "failed",
    "expired",
    "cancelled",
]
NativeConversationDisposition = Literal[
    "awaiting_user",
    "completed",
    "failed",
    "expired",
    "cancelled",
]
NativeConversationBeginStatus = Literal[
    "started",
    "resumed",
    "finished",
    "cancelled",
    "duplicate",
    "in_progress",
    "no_open",
    "failed",
    "expired",
    "corrupt",
]

TRANSCRIPT_SCHEMA_VERSION = "native_conversation_transcript.v1"
TOOL_CHECKPOINT_SCHEMA_VERSION = "native_tool_checkpoint.v1"
SUMMARY_SCHEMA_VERSION = "native_conversation_summary.v1"
CONTEXT_MANIFEST_SCHEMA_VERSION = "native_context_assembly.v1"


class NativeTranscriptMessage(BaseModel):
    """One ordered provider-native message payload in a transcript manifest."""

    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1)
    message: dict[str, Any]

    @field_validator("message")
    @classmethod
    def message_has_langchain_shape(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value.get("type"), str):
            raise ValueError("message payload must include a LangChain message type")
        data = value.get("data")
        if not isinstance(data, dict):
            raise ValueError("message payload must include LangChain message data")
        return value


class NativeLifecycleEvent(BaseModel):
    """Host-owned lifecycle metadata stored in the private artifact."""

    model_config = ConfigDict(extra="forbid")

    disposition: NativeConversationDisposition
    content: str | None = Field(default=None, max_length=8_000)
    occurred_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class UserTranscriptBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["user"] = "user"
    sequence: int = Field(ge=1)
    message_index: int = Field(ge=1)
    content: Any


class AssistantVisibleTranscriptBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["assistant_visible"] = "assistant_visible"
    sequence: int = Field(ge=1)
    message_index: int = Field(ge=1)
    content: Any


class AssistantToolCallTranscriptBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["assistant_tool_call"] = "assistant_tool_call"
    sequence: int = Field(ge=1)
    message_index: int = Field(ge=1)
    call_id: str = Field(min_length=1, max_length=255)
    tool_name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResultTranscriptBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["tool_result"] = "tool_result"
    sequence: int = Field(ge=1)
    message_index: int = Field(ge=1)
    call_id: str = Field(min_length=1, max_length=255)
    tool_name: str | None = Field(default=None, max_length=128)
    status: Literal["success", "error"]
    content: Any


class AssistantReasoningTranscriptBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["assistant_reasoning"] = "assistant_reasoning"
    sequence: int = Field(ge=1)
    message_index: int = Field(ge=1)
    provider: str = Field(min_length=1, max_length=64)
    content: Any


class HostLifecycleTranscriptBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["host_lifecycle"] = "host_lifecycle"
    sequence: int = Field(ge=1)
    disposition: NativeConversationDisposition
    content: str | None = Field(default=None, max_length=8_000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime


NativeTranscriptBlock = Annotated[
    UserTranscriptBlock
    | AssistantVisibleTranscriptBlock
    | AssistantToolCallTranscriptBlock
    | ToolResultTranscriptBlock
    | AssistantReasoningTranscriptBlock
    | HostLifecycleTranscriptBlock,
    Field(discriminator="kind"),
]


class NativeTranscriptManifest(BaseModel):
    """Immutable artifact payload containing replayable native chat messages."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["native_conversation_transcript.v1"] = TRANSCRIPT_SCHEMA_VERSION
    conversation_id: uuid.UUID | None = None
    revision: int = Field(ge=1)
    messages: tuple[NativeTranscriptMessage, ...] = ()
    blocks: tuple[NativeTranscriptBlock, ...] = ()
    lifecycle_events: tuple[NativeLifecycleEvent, ...] = ()

    @classmethod
    def from_messages(
        cls,
        messages: Sequence[BaseMessage],
        *,
        revision: int,
        conversation_id: uuid.UUID | None = None,
        lifecycle_events: Sequence[NativeLifecycleEvent] = (),
    ) -> NativeTranscriptManifest:
        payloads = messages_to_dict(list(messages))
        return cls(
            conversation_id=conversation_id,
            revision=revision,
            messages=tuple(
                NativeTranscriptMessage(sequence=index, message=payload)
                for index, payload in enumerate(payloads, start=1)
            ),
            blocks=_blocks_from_messages(messages),
            lifecycle_events=tuple(lifecycle_events),
        )

    def to_messages(self) -> tuple[BaseMessage, ...]:
        return tuple(messages_from_dict([item.message for item in self.messages]))

    def with_appended_messages(
        self,
        appended: Sequence[BaseMessage],
        *,
        revision: int,
    ) -> NativeTranscriptManifest:
        payloads = messages_to_dict(list(appended))
        appended_messages = tuple(
            NativeTranscriptMessage(
                sequence=len(self.messages) + index,
                message=payload,
            )
            for index, payload in enumerate(payloads, start=1)
        )
        return self.model_copy(
            update={
                "revision": revision,
                "messages": (*self.messages, *appended_messages),
                "blocks": (
                    *self.blocks,
                    *_blocks_from_messages(
                        appended,
                        message_index_offset=len(self.messages),
                        sequence_offset=len(self.blocks),
                    ),
                ),
            }
        )

    def with_lifecycle_event(
        self,
        event: NativeLifecycleEvent,
        *,
        revision: int,
    ) -> NativeTranscriptManifest:
        return self.model_copy(
            update={
                "revision": revision,
                "lifecycle_events": (*self.lifecycle_events, event),
                "blocks": (
                    *self.blocks,
                    HostLifecycleTranscriptBlock(
                        sequence=len(self.blocks) + 1,
                        disposition=event.disposition,
                        content=event.content,
                        metadata=event.metadata,
                        occurred_at=event.occurred_at,
                    ),
                ),
            }
        )


class NativeToolCheckpointManifest(BaseModel):
    """Host-trusted tool checkpoint payload; never model-visible by itself."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["native_tool_checkpoint.v1"] = TOOL_CHECKPOINT_SCHEMA_VERSION
    conversation_id: uuid.UUID
    revision: int = Field(ge=1)
    checkpoint: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_mapping(
        cls,
        checkpoint: Mapping[str, Any],
        *,
        conversation_id: uuid.UUID,
        revision: int,
    ) -> NativeToolCheckpointManifest:
        return cls(
            conversation_id=conversation_id,
            revision=revision,
            checkpoint=dict(checkpoint),
        )


class NativeConversationSummaryManifest(BaseModel):
    """Validated, model-visible continuity for a compacted transcript prefix."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["native_conversation_summary.v1"] = SUMMARY_SCHEMA_VERSION
    conversation_id: uuid.UUID
    parent_compaction_id: uuid.UUID | None = None
    covered_from_message_index: int = Field(ge=1)
    covered_through_message_index: int = Field(ge=1)
    conversation_state: str = Field(default="", max_length=4_000)
    answered_questions: tuple[str, ...] = Field(default=(), max_length=20)
    open_threads: tuple[str, ...] = Field(default=(), max_length=20)
    tool_outcomes: tuple[str, ...] = Field(default=(), max_length=30)
    user_statements: tuple[str, ...] = Field(default=(), max_length=30)
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator(
        "answered_questions",
        "open_threads",
        "tool_outcomes",
        "user_statements",
    )
    @classmethod
    def bounded_summary_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() or len(item) > 1_000 for item in value):
            raise ValueError("summary items must be non-empty and at most 1000 characters")
        return value

    @field_validator("covered_through_message_index")
    @classmethod
    def covered_range_is_ordered(cls, value: int, info: Any) -> int:
        start = info.data.get("covered_from_message_index")
        if isinstance(start, int) and value < start:
            raise ValueError("summary coverage range is invalid")
        return value


class ContextAssemblyManifest(BaseModel):
    """Non-secret audit metadata for one bounded prompt assembly."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["native_context_assembly.v1"] = CONTEXT_MANIFEST_SCHEMA_VERSION
    conversation_id: uuid.UUID
    turn: int = Field(ge=1)
    summary_compaction_id: uuid.UUID | None = None
    included_memory_ids: tuple[uuid.UUID, ...] = ()
    tail_from_message_index: int = Field(ge=1)
    tail_through_message_index: int = Field(ge=1)
    estimated_input_tokens: int = Field(ge=0)
    omitted_message_count: int = Field(ge=0)
    semantic_memory_available: bool | None = None
    compacted: bool = False
    created_at: datetime

    @field_validator("tail_through_message_index")
    @classmethod
    def tail_range_is_ordered(cls, value: int, info: Any) -> int:
        start = info.data.get("tail_from_message_index")
        if isinstance(start, int) and value < start:
            raise ValueError("tail range is invalid")
        return value


def _blocks_from_messages(
    messages: Sequence[BaseMessage],
    *,
    message_index_offset: int = 0,
    sequence_offset: int = 0,
) -> tuple[NativeTranscriptBlock, ...]:
    blocks: list[NativeTranscriptBlock] = []

    def next_sequence() -> int:
        return sequence_offset + len(blocks) + 1

    for message_index, message in enumerate(messages, start=1):
        message_index += message_index_offset
        if isinstance(message, HumanMessage):
            blocks.append(
                UserTranscriptBlock(
                    sequence=next_sequence(),
                    message_index=message_index,
                    content=message.content,
                )
            )
            continue
        if isinstance(message, AIMessage):
            reasoning = message.additional_kwargs.get("reasoning_content")
            if reasoning is not None:
                blocks.append(
                    AssistantReasoningTranscriptBlock(
                        sequence=next_sequence(),
                        message_index=message_index,
                        provider="langchain",
                        content=reasoning,
                    )
                )
            for reasoning_block in _reasoning_content_blocks(message.content):
                blocks.append(  # noqa: PERF401 - next_sequence depends on incremental appends.
                    AssistantReasoningTranscriptBlock(
                        sequence=next_sequence(),
                        message_index=message_index,
                        provider="langchain",
                        content=reasoning_block,
                    )
                )
            visible = _visible_content(message.content)
            if visible not in ("", (), [], None):
                blocks.append(
                    AssistantVisibleTranscriptBlock(
                        sequence=next_sequence(),
                        message_index=message_index,
                        content=visible,
                    )
                )
            for raw in message.tool_calls:
                call_id = str(raw.get("id") or "")
                name = str(raw.get("name") or "")
                args = raw.get("args") or {}
                if call_id and name:
                    blocks.append(
                        AssistantToolCallTranscriptBlock(
                            sequence=next_sequence(),
                            message_index=message_index,
                            call_id=call_id,
                            tool_name=name,
                            arguments=args,
                        )
                    )
            continue
        if isinstance(message, ToolMessage):
            status = "error" if getattr(message, "status", None) == "error" else "success"
            blocks.append(
                ToolResultTranscriptBlock(
                    sequence=next_sequence(),
                    message_index=message_index,
                    call_id=str(message.tool_call_id),
                    tool_name=message.name,
                    status=status,
                    content=message.content,
                )
            )
    return tuple(blocks)


def _visible_content(content: Any) -> Any:
    if not isinstance(content, list):
        return content
    visible: list[Any] = []
    for item in cast(list[object], content):
        if isinstance(item, Mapping):
            item_mapping = cast(Mapping[str, object], item)
            block_type = item_mapping.get("type")
            if isinstance(block_type, str) and block_type in {
                "reasoning",
                "thinking",
                "redacted_thinking",
            }:
                continue
        visible.append(item)
    return visible


def _reasoning_content_blocks(content: Any) -> tuple[Any, ...]:
    if not isinstance(content, list):
        return ()
    blocks: list[Any] = []
    for item in cast(list[object], content):
        if not isinstance(item, Mapping):
            continue
        item_mapping = cast(Mapping[str, object], item)
        block_type = item_mapping.get("type")
        if isinstance(block_type, str) and block_type in {
            "reasoning",
            "thinking",
            "redacted_thinking",
        }:
            blocks.append(dict(item_mapping))
    return tuple(blocks)


@dataclass(frozen=True, slots=True)
class NativeConversationBeginResult:
    """Service result for an inbound owner turn."""

    status: NativeConversationBeginStatus
    session_id: uuid.UUID | None
    root_event_id: str | None
    state: str | None
    revision: int | None
    transcript_messages: tuple[BaseMessage, ...] = ()
    checkpoint: Mapping[str, Any] | None = None
    response: str | None = None
    duplicate: bool = False


__all__ = [
    "CONTEXT_MANIFEST_SCHEMA_VERSION",
    "SUMMARY_SCHEMA_VERSION",
    "TOOL_CHECKPOINT_SCHEMA_VERSION",
    "TRANSCRIPT_SCHEMA_VERSION",
    "ContextAssemblyManifest",
    "NativeConversationBeginResult",
    "NativeConversationBeginStatus",
    "NativeConversationDisposition",
    "NativeConversationState",
    "NativeConversationSummaryManifest",
    "NativeLifecycleEvent",
    "NativeToolCheckpointManifest",
    "NativeTranscriptBlock",
    "NativeTranscriptManifest",
    "NativeTranscriptMessage",
]
