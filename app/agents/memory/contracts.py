"""Contracts for generic owner-scoped durable user memory."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class MemoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


class UserMemoryKind(StrEnum):
    PREFERENCE = "preference"
    PROFILE = "profile"
    STANDING_INSTRUCTION = "standing_instruction"
    CONSTRAINT = "constraint"
    PERSONAL_FACT = "personal_fact"


class UserMemoryStatus(StrEnum):
    ACTIVE = "active"
    PENDING_CONFIRMATION = "pending_confirmation"
    SUPERSEDED = "superseded"
    DELETED = "deleted"


class UserMemorySensitivity(StrEnum):
    STANDARD = "standard"
    PRIVATE = "private"
    SENSITIVE = "sensitive"


class UserMemoryAction(StrEnum):
    REMEMBER = "remember"
    REVIEW = "review"
    CORRECT = "correct"
    FORGET = "forget"


class UserMemorySemanticStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    NOT_REQUESTED = "not_requested"


class UserMemoryOwnerScope(MemoryModel):
    """Authenticated owner scope supplied by the host, never by the model."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    owner_user_id: str = Field(min_length=1, max_length=255)
    owner_channel_id: str = Field(min_length=1, max_length=255)


class UserMemoryHostContext(MemoryModel):
    """Trusted host metadata for one inbound owner event."""

    owner_scope: UserMemoryOwnerScope
    received_at: datetime
    source_conversation_id: str | None = Field(default=None, min_length=1, max_length=255)
    source_external_event_id: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("received_at")
    @classmethod
    def received_at_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class UserMemoryToolCommand(MemoryModel):
    """Model-visible memory command arguments.

    Owner and channel identifiers are intentionally absent. Callers must pair
    this command with a trusted UserMemoryHostContext.
    """

    action: UserMemoryAction
    raw_text: str = Field(min_length=1, max_length=8_000, repr=False)
    content: str | None = Field(default=None, min_length=1, max_length=4_000, repr=False)
    replacement_content: str | None = Field(
        default=None,
        min_length=1,
        max_length=4_000,
        repr=False,
    )
    memory_id: str | None = Field(default=None, min_length=1, max_length=255)
    kind: UserMemoryKind = UserMemoryKind.PERSONAL_FACT
    normalized_subject: str | None = Field(default=None, min_length=1, max_length=255)
    sensitivity: UserMemorySensitivity = UserMemorySensitivity.STANDARD
    explicit_confirmation: bool = False

    @model_validator(mode="after")
    def action_has_required_payload(self) -> UserMemoryToolCommand:
        if self.action is UserMemoryAction.REMEMBER and not (self.content or self.raw_text.strip()):
            raise ValueError("remember requires content or raw_text")
        if self.action is UserMemoryAction.CORRECT and not self.replacement_content:
            raise ValueError("correct requires replacement_content")
        if self.action is UserMemoryAction.FORGET and not (self.memory_id or self.content):
            raise ValueError("forget requires memory_id or content")
        return self


class UserMemoryEmbeddingPayload(MemoryModel):
    """Validated embedding metadata passed to persistence adapters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    vector: tuple[float, ...] = Field(min_length=1, repr=False)
    model_identity: str = Field(min_length=1, max_length=255)
    dimensions: int = Field(gt=0)

    @model_validator(mode="after")
    def dimensions_match_vector(self) -> UserMemoryEmbeddingPayload:
        if self.dimensions != len(self.vector):
            raise ValueError("embedding dimensions must match vector length")
        return self


class UserMemoryCreate(MemoryModel):
    """Payload for persistence adapters that create one memory revision."""

    owner_scope: UserMemoryOwnerScope
    kind: UserMemoryKind
    status: UserMemoryStatus
    content: str = Field(min_length=1, max_length=4_000, repr=False)
    redacted_preview: str = Field(min_length=1, max_length=500)
    normalized_subject: str | None = Field(default=None, min_length=1, max_length=255)
    sensitivity: UserMemorySensitivity = UserMemorySensitivity.STANDARD
    source_conversation_id: str | None = Field(default=None, min_length=1, max_length=255)
    source_external_event_id: str | None = Field(default=None, min_length=1, max_length=255)
    embedding: UserMemoryEmbeddingPayload | None = None
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class UserMemoryRecord(MemoryModel):
    """Owner-filtered memory record returned by a persistence adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=255)
    owner_scope: UserMemoryOwnerScope
    kind: UserMemoryKind
    status: UserMemoryStatus
    content: str | None = Field(default=None, min_length=1, max_length=4_000, repr=False)
    redacted_preview: str = Field(min_length=1, max_length=500)
    normalized_subject: str | None = Field(default=None, min_length=1, max_length=255)
    sensitivity: UserMemorySensitivity = UserMemorySensitivity.STANDARD
    revision: int = Field(ge=1, default=1)
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamps_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class UserMemoryContextItem(MemoryModel):
    """One active memory safe to inject into bounded model context."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=255)
    kind: UserMemoryKind
    content: str = Field(min_length=1, max_length=4_000, repr=False)
    redacted_preview: str = Field(min_length=1, max_length=500)
    normalized_subject: str | None = Field(default=None, min_length=1, max_length=255)
    revision: int = Field(ge=1)


class UserMemoryActionResult(MemoryModel):
    """Safe result for explicit memory management commands."""

    status: Literal[
        "applied",
        "pending_confirmation",
        "deleted",
        "not_found",
        "review",
        "no_change",
    ]
    response: str = Field(min_length=1, max_length=2_000)
    memory_ids: tuple[str, ...] = Field(default=(), max_length=20)
    previews: tuple[str, ...] = Field(default=(), max_length=20)
    semantic_status: UserMemorySemanticStatus = UserMemorySemanticStatus.NOT_REQUESTED


class UserMemoryRetrievalResult(MemoryModel):
    """Hybrid retrieval result with unavailable semantic search distinct from emptiness."""

    items: tuple[UserMemoryContextItem, ...] = Field(default=(), max_length=50)
    semantic_status: UserMemorySemanticStatus
    semantic_error_code: str | None = Field(default=None, min_length=1, max_length=128)
    exact_count: int = Field(ge=0)
    semantic_count: int = Field(ge=0)
    omitted_count: int = Field(ge=0)


class UserMemoryContextBlock(MemoryModel):
    """Rendered bounded untrusted memory block plus manifest fields."""

    text: str = Field(max_length=10_000)
    included_memory_ids: tuple[str, ...] = Field(default=(), max_length=50)
    semantic_status: UserMemorySemanticStatus
    omitted_count: int = Field(ge=0)


class UserMemoryContextResult(MemoryModel):
    """Convenience result for native context integration."""

    items: tuple[UserMemoryContextItem, ...] = Field(default=(), max_length=50)
    rendered_text: str = Field(max_length=10_000)
    included_memory_ids: tuple[str, ...] = Field(default=(), max_length=50)
    semantic_status: UserMemorySemanticStatus
    semantic_available: bool
    omitted_count: int = Field(ge=0)


UserMemorySearchStatusFilter = Annotated[
    tuple[UserMemoryStatus, ...],
    Field(default=(UserMemoryStatus.ACTIVE,), min_length=1, max_length=4),
]


__all__ = [
    "MemoryModel",
    "UserMemoryAction",
    "UserMemoryActionResult",
    "UserMemoryContextBlock",
    "UserMemoryContextItem",
    "UserMemoryContextResult",
    "UserMemoryCreate",
    "UserMemoryEmbeddingPayload",
    "UserMemoryHostContext",
    "UserMemoryKind",
    "UserMemoryOwnerScope",
    "UserMemoryRecord",
    "UserMemoryRetrievalResult",
    "UserMemorySearchStatusFilter",
    "UserMemorySemanticStatus",
    "UserMemorySensitivity",
    "UserMemoryStatus",
    "UserMemoryToolCommand",
]
