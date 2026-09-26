"""SQLAlchemy models for the shared durable orchestration core.

The relational schema intentionally stores metadata and references only.  Raw
documents, model transcripts, tool logs, and rendered reports belong in the
content-addressed artifact store and are represented here by ``*_artifact_key``
columns.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    FetchedValue,
    Float,
    ForeignKey,
    Index,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for application-owned tables."""

    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RunStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    ATTENTION = "attention"
    FAILED = "failed"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class StepStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    ATTENTION = "attention"
    FAILED = "failed"
    SKIPPED = "skipped"


class DeliveryStatus(enum.StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    UNCERTAIN = "uncertain"
    SENT = "sent"
    FAILED = "failed"
    ACKNOWLEDGED = "acknowledged"


class EvidenceClassification(enum.StrEnum):
    PRIMARY = "primary"
    REPORTED = "reported"
    SECONDARY = "secondary"


class HealthState(enum.StrEnum):
    HEALTHY = "healthy"
    ATTENTION = "attention"
    FAILED = "failed"


class ApprovalState(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class TimestampMixin:
    """Common timezone-aware timestamps."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=_utc_now,
    )


class AgentRun(TimestampMixin, Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint("length(agent_name) > 0", name="agent_name_nonempty"),
        CheckConstraint("length(idempotency_key) > 0", name="idempotency_key_nonempty"),
        CheckConstraint(
            "status IN ('queued','running','succeeded','attention','failed','paused','cancelled')",
            name="status_valid",
        ),
        Index("ix_agent_runs_status_created", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    agent_name: Mapped[str] = mapped_column(String(128), nullable=False)
    trigger: Mapped[str] = mapped_column(String(128), nullable=False)
    schedule: Mapped[str | None] = mapped_column(String(128))
    model_version: Mapped[str | None] = mapped_column(String(255))
    config_version: Mapped[str | None] = mapped_column(String(255))
    input_version: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[RunStatus] = mapped_column(String(32), nullable=False, default=RunStatus.QUEUED)
    summary: Mapped[str | None] = mapped_column(String(4000))
    artifact_key: Mapped[str | None] = mapped_column(String(512))
    error_code: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunStep(TimestampMixin, Base):
    __tablename__ = "run_steps"
    __table_args__ = (
        UniqueConstraint("run_id", "node_name", "attempt", name="uq_run_steps_run_node_attempt"),
        CheckConstraint("attempt >= 1", name="attempt_positive"),
        CheckConstraint(
            "status IN ('queued','running','succeeded','attention','failed','skipped')",
            name="status_valid",
        ),
        Index("ix_run_steps_run_created", "run_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    node_name: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt: Mapped[int] = mapped_column(nullable=False, default=1)
    status: Mapped[StepStatus] = mapped_column(
        String(32), nullable=False, default=StepStatus.QUEUED
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    diagnostic: Mapped[str | None] = mapped_column(String(2000))
    model_call_ref: Mapped[str | None] = mapped_column(String(255))
    artifact_key: Mapped[str | None] = mapped_column(String(512))


class Delivery(TimestampMixin, Base):
    __tablename__ = "deliveries"
    __table_args__ = (
        UniqueConstraint("channel", "idempotency_key", name="uq_deliveries_channel_idempotency"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        CheckConstraint(
            "status IN ('pending','sending','uncertain','sent','failed','acknowledged')",
            name="status_valid",
        ),
        Index("ix_deliveries_run_status", "run_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[DeliveryStatus] = mapped_column(
        String(32), nullable=False, default=DeliveryStatus.PENDING
    )
    attempt_count: Mapped[int] = mapped_column(nullable=False, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_url: Mapped[str | None] = mapped_column(String(2048))
    receipt_artifact_key: Mapped[str | None] = mapped_column(String(512))
    error_code: Mapped[str | None] = mapped_column(String(128))


class DiscordWakeInbound(TimestampMixin, Base):
    """Durable Discord wake handoff metadata; raw inbound content stays in artifacts."""

    __tablename__ = "discord_wake_inbound"
    __table_args__ = (
        UniqueConstraint("discord_event_id", name="uq_discord_wake_event"),
        UniqueConstraint("handoff_nonce", name="uq_discord_wake_nonce"),
        CheckConstraint("length(discord_event_id) > 0", name="discord_event_id_nonempty"),
        CheckConstraint(
            "length(handoff_nonce) > 0 AND length(handoff_nonce) <= 64",
            name="handoff_nonce_bounded",
        ),
        CheckConstraint(
            "event_kind IN ('message','interaction')",
            name="event_kind_valid",
        ),
        CheckConstraint(
            "action IN "
            "('academic_checkin','academic_continuation','agent_clarification','proposal_confirmation',"
            "'proposal_rejection')",
            name="action_valid",
        ),
        CheckConstraint(
            "state IN ('queued','running','abort_requested','aborted','completed','failed')",
            name="state_valid",
        ),
        CheckConstraint(
            "abort_requested_prior_state IS NULL OR abort_requested_prior_state IN "
            "('queued','running','abort_requested','aborted','completed','failed')",
            name="abort_requested_prior_state_valid",
        ),
        CheckConstraint(
            "activity_side_effect_class IS NULL OR activity_side_effect_class IN "
            "('read_only','proposal_only','durable_local_write','external_write','unknown')",
            name="activity_side_effect_class_valid",
        ),
        CheckConstraint(
            "activity_tool_status IS NULL OR activity_tool_status IN "
            "('not_started','running','succeeded','failed','unknown','completed_before_cancel',"
            "'cancellation_requested','cancelled')",
            name="activity_tool_status_valid",
        ),
        CheckConstraint(
            "interaction_action IS NULL OR interaction_action IN "
            "('quiz','assignment','tutorial','lab','event','ignore')",
            name="interaction_action_valid",
        ),
        CheckConstraint("retry_count >= 0", name="retry_count_nonnegative"),
        CheckConstraint("length(content_artifact_key) > 0", name="content_artifact_key_nonempty"),
        Index("ix_discord_wake_state_created", "state", "created_at"),
        Index("ix_discord_wake_received", "received_at"),
        Index("ix_discord_wake_scope_active", "discord_channel_id", "discord_user_id", "state"),
        Index("ix_discord_wake_queue_job", "queue_job_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    discord_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    handoff_nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    event_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    action_id: Mapped[uuid.UUID | None] = mapped_column()
    clarification_id: Mapped[uuid.UUID | None] = mapped_column()
    interaction_action: Mapped[str | None] = mapped_column(String(32))
    discord_channel_id: Mapped[str | None] = mapped_column(String(32))
    discord_user_id: Mapped[str | None] = mapped_column(String(32))
    discord_message_id: Mapped[str | None] = mapped_column(String(32))
    discord_interaction_id: Mapped[str | None] = mapped_column(String(32))
    ack_message_id: Mapped[str | None] = mapped_column(String(32))
    content_artifact_key: Mapped[str] = mapped_column(String(512), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    queue_job_id: Mapped[int | None] = mapped_column(BigInteger)
    retry_count: Mapped[int] = mapped_column(nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    abort_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    abort_requested_by_event_id: Mapped[str | None] = mapped_column(String(255))
    abort_requested_prior_state: Mapped[str | None] = mapped_column(String(32))
    abort_terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    abort_reason_code: Mapped[str | None] = mapped_column(String(128))
    activity_phase: Mapped[str | None] = mapped_column(String(64))
    activity_model_turn: Mapped[int | None] = mapped_column()
    activity_tool_name: Mapped[str | None] = mapped_column(String(128))
    activity_tool_status: Mapped[str | None] = mapped_column(String(64))
    activity_side_effect_class: Mapped[str | None] = mapped_column(String(64))
    activity_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DiscordAbortRequest(TimestampMixin, Base):
    """Content-free owner/channel ABORT receipt covering late handoff races."""

    __tablename__ = "discord_abort_requests"
    __table_args__ = (
        UniqueConstraint("handoff_nonce", name="uq_discord_abort_nonce"),
        CheckConstraint("length(abort_event_id) > 0", name="abort_event_id_nonempty"),
        CheckConstraint(
            "status IN ('processing','accepted','no_active','unconfirmed')",
            name="status_valid",
        ),
        CheckConstraint(
            "safe_tool_status IN "
            "('none','cancelled','cancellation_requested','unknown','completed_before_cancel')",
            name="safe_tool_status_valid",
        ),
        CheckConstraint(
            "target_count >= 0 AND running_count >= 0 AND queued_count >= 0",
            name="abort_counts_nonnegative",
        ),
        Index(
            "ix_discord_abort_scope_received",
            "discord_channel_id",
            "discord_user_id",
            "received_at",
        ),
    )

    abort_event_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    handoff_nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    discord_channel_id: Mapped[str] = mapped_column(String(32), nullable=False)
    discord_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    ack_message_id: Mapped[str] = mapped_column(String(32), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="processing")
    target_count: Mapped[int] = mapped_column(nullable=False, default=0)
    running_count: Mapped[int] = mapped_column(nullable=False, default=0)
    queued_count: Mapped[int] = mapped_column(nullable=False, default=0)
    safe_activity_label: Mapped[str | None] = mapped_column(String(80))
    safe_tool_status: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NativeConversationSession(TimestampMixin, Base):
    """Metadata for one durable native-model conversation.

    Raw message content, tool output, and private reasoning stay in immutable
    artifacts. This row only carries routing, lifecycle, and artifact pointers.
    """

    __tablename__ = "native_conversation_sessions"
    __table_args__ = (
        UniqueConstraint("root_event_id", name="uq_native_conversations_root_event"),
        CheckConstraint("length(root_event_id) > 0", name="root_event_id_nonempty"),
        CheckConstraint("channel = 'discord'", name="channel_discord_only"),
        CheckConstraint(
            "state IN ('processing','awaiting_user','completed','failed','expired','cancelled')",
            name="state_valid",
        ),
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint("next_event_sequence >= 1", name="next_event_sequence_positive"),
        CheckConstraint(
            "length(transcript_artifact_key) = 64",
            name="transcript_artifact_key_valid",
        ),
        CheckConstraint(
            "tool_checkpoint_artifact_key IS NULL OR length(tool_checkpoint_artifact_key) = 64",
            name="tool_checkpoint_artifact_key_valid",
        ),
        CheckConstraint(
            "last_disposition IS NULL OR last_disposition IN "
            "('awaiting_user','completed','failed','expired','cancelled')",
            name="last_disposition_valid",
        ),
        Index(
            "uq_native_conversations_open_owner_channel",
            "owner_discord_user_id",
            "discord_channel_id",
            unique=True,
            sqlite_where=text("state IN ('processing','awaiting_user')"),
            postgresql_where=text("state IN ('processing','awaiting_user')"),
        ),
        Index("ix_native_conversations_state_expiry", "state", "expires_at"),
        Index(
            "ix_native_conversations_owner_state",
            "owner_discord_user_id",
            "discord_channel_id",
            "state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    root_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False, default="discord")
    discord_channel_id: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_discord_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="processing")
    revision: Mapped[int] = mapped_column(nullable=False, default=1)
    next_event_sequence: Mapped[int] = mapped_column(nullable=False, default=1)
    transcript_artifact_key: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_checkpoint_artifact_key: Mapped[str | None] = mapped_column(String(64))
    model_identity: Mapped[str | None] = mapped_column(String(255))
    prompt_config_version: Mapped[str | None] = mapped_column(String(255))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_turn_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_disposition: Mapped[str | None] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(128))


class NativeConversationInboundEvent(TimestampMixin, Base):
    """Idempotency metadata for one inbound owner event in a native conversation."""

    __tablename__ = "native_conversation_inbound_events"
    __table_args__ = (
        UniqueConstraint("external_event_id", name="uq_native_conversation_event"),
        UniqueConstraint(
            "conversation_id",
            "event_sequence",
            name="uq_native_conversation_event_sequence",
        ),
        CheckConstraint("length(external_event_id) > 0", name="external_event_id_nonempty"),
        CheckConstraint("event_sequence >= 1", name="event_sequence_positive"),
        Index(
            "ix_native_conversation_events_session",
            "conversation_id",
            "event_sequence",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("native_conversation_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    external_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_sequence: Mapped[int] = mapped_column(nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class NativeConversationCompaction(TimestampMixin, Base):
    """Validated, artifact-backed summary metadata for a native conversation prefix."""

    __tablename__ = "native_conversation_compactions"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "covered_from_message_index",
            "covered_through_message_index",
            "source_fingerprint",
            "summary_prompt_version",
            name="uq_native_compaction_source_range",
        ),
        CheckConstraint("covered_from_message_index >= 0", name="covered_from_nonnegative"),
        CheckConstraint(
            "covered_through_message_index >= covered_from_message_index",
            name="covered_range_valid",
        ),
        CheckConstraint(
            "length(source_transcript_artifact_key) = 64",
            name="source_transcript_artifact_key_valid",
        ),
        CheckConstraint("length(source_fingerprint) > 0", name="source_fingerprint_nonempty"),
        CheckConstraint("length(summary_artifact_key) = 64", name="summary_artifact_key_valid"),
        CheckConstraint("length(summary_model_identity) > 0", name="summary_model_nonempty"),
        CheckConstraint("length(summary_prompt_version) > 0", name="summary_prompt_nonempty"),
        CheckConstraint("estimated_input_tokens >= 0", name="estimated_input_tokens_nonnegative"),
        CheckConstraint(
            "reported_input_tokens IS NULL OR reported_input_tokens >= 0",
            name="reported_input_tokens_nonnegative",
        ),
        CheckConstraint(
            "reported_output_tokens IS NULL OR reported_output_tokens >= 0",
            name="reported_output_tokens_nonnegative",
        ),
        CheckConstraint("status IN ('valid','superseded','failed')", name="status_valid"),
        CheckConstraint(
            "error_code IS NULL OR length(error_code) > 0",
            name="error_code_nonempty",
        ),
        Index(
            "ix_native_compactions_conversation_status_range",
            "conversation_id",
            "status",
            "covered_through_message_index",
        ),
        Index("ix_native_compactions_parent", "parent_compaction_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("native_conversation_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_compaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("native_conversation_compactions.id", ondelete="SET NULL")
    )
    covered_from_message_index: Mapped[int] = mapped_column(nullable=False)
    covered_through_message_index: Mapped[int] = mapped_column(nullable=False)
    source_transcript_artifact_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    summary_artifact_key: Mapped[str] = mapped_column(String(64), nullable=False)
    summary_model_identity: Mapped[str] = mapped_column(String(255), nullable=False)
    summary_prompt_version: Mapped[str] = mapped_column(String(128), nullable=False)
    estimated_input_tokens: Mapped[int] = mapped_column(nullable=False)
    reported_input_tokens: Mapped[int | None] = mapped_column()
    reported_output_tokens: Mapped[int | None] = mapped_column()
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="valid")
    error_code: Mapped[str | None] = mapped_column(String(128))


class UserMemoryFact(TimestampMixin, Base):
    """Owner-scoped generic memory metadata; private text remains artifact-backed."""

    __tablename__ = "user_memory_facts"
    __table_args__ = (
        CheckConstraint("length(owner_user_id) > 0", name="owner_user_id_nonempty"),
        CheckConstraint("length(owner_channel_id) > 0", name="owner_channel_id_nonempty"),
        CheckConstraint(
            "kind IN ('preference','profile','standing_instruction','constraint','personal_fact')",
            name="kind_valid",
        ),
        CheckConstraint(
            "status IN ('active','pending_confirmation','superseded','deleted')",
            name="status_valid",
        ),
        CheckConstraint("length(content_artifact_key) = 64", name="content_artifact_key_valid"),
        CheckConstraint("length(redacted_preview) > 0", name="redacted_preview_nonempty"),
        CheckConstraint("length(normalized_subject) > 0", name="normalized_subject_nonempty"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)",
            name="confidence_probability",
        ),
        CheckConstraint(
            "sensitivity IN ('low','medium','high')",
            name="sensitivity_valid",
        ),
        CheckConstraint(
            "evidence_artifact_key IS NULL OR length(evidence_artifact_key) = 64",
            name="evidence_artifact_key_valid",
        ),
        CheckConstraint(
            "embedding_dimensions IS NULL OR embedding_dimensions > 0",
            name="embedding_dimensions_positive",
        ),
        CheckConstraint("revision >= 1", name="revision_positive"),
        Index(
            "ix_user_memory_owner_status_kind",
            "owner_user_id",
            "owner_channel_id",
            "status",
            "kind",
        ),
        Index(
            "ix_user_memory_owner_subject",
            "owner_user_id",
            "owner_channel_id",
            "normalized_subject",
        ),
        Index(
            "ix_user_memory_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_where=text("embedding IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_channel_id: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending_confirmation")
    content_artifact_key: Mapped[str] = mapped_column(String(64), nullable=False)
    redacted_preview: Mapped[str] = mapped_column(String(2_000), nullable=False)
    normalized_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    sensitivity: Mapped[str] = mapped_column(String(32), nullable=False, default="medium")
    source_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("native_conversation_sessions.id", ondelete="SET NULL")
    )
    source_external_event_id: Mapped[str | None] = mapped_column(String(255))
    evidence_artifact_key: Mapped[str | None] = mapped_column(String(64))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024).with_variant(JSON, "sqlite"))
    embedding_model: Mapped[str | None] = mapped_column(String(255))
    embedding_dimensions: Mapped[int | None] = mapped_column()
    revision: Mapped[int] = mapped_column(nullable=False, default=1)


class UserMemoryEvent(TimestampMixin, Base):
    """Append-only audit event for generic user memory lifecycle changes."""

    __tablename__ = "user_memory_events"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "owner_channel_id",
            "external_event_id",
            name="uq_user_memory_events_owner_external",
        ),
        CheckConstraint("length(owner_user_id) > 0", name="owner_user_id_nonempty"),
        CheckConstraint("length(owner_channel_id) > 0", name="owner_channel_id_nonempty"),
        CheckConstraint("length(external_event_id) > 0", name="external_event_id_nonempty"),
        CheckConstraint(
            "event_type IN "
            "('create','confirm','correct','supersede','delete','retrieval_feedback')",
            name="event_type_valid",
        ),
        CheckConstraint("actor IS NULL OR length(actor) > 0", name="actor_nonempty"),
        Index("ix_user_memory_events_memory_time", "memory_id", "occurred_at"),
        Index(
            "ix_user_memory_events_owner_time",
            "owner_user_id",
            "owner_channel_id",
            "occurred_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    memory_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_memory_facts.id", ondelete="SET NULL")
    )
    owner_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_channel_id: Mapped[str] = mapped_column(String(32), nullable=False)
    external_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(255))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class EvidenceRef(TimestampMixin, Base):
    __tablename__ = "evidence_refs"
    __table_args__ = (
        Index("ix_evidence_refs_run_claim", "run_id", "claim_id"),
        CheckConstraint("length(title) > 0", name="title_nonempty"),
        CheckConstraint(
            "classification IN ('primary','reported','secondary')",
            name="classification_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    claim_id: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    source_version: Mapped[str | None] = mapped_column(String(255))
    classification: Mapped[EvidenceClassification] = mapped_column(
        String(32), nullable=False, default=EvidenceClassification.REPORTED
    )
    access_classification: Mapped[str | None] = mapped_column(String(128))
    artifact_key: Mapped[str | None] = mapped_column(String(512))


class HealthCheck(TimestampMixin, Base):
    __tablename__ = "health_checks"
    __table_args__ = (
        UniqueConstraint("check_name", name="uq_health_checks_check_name"),
        CheckConstraint("state IN ('healthy','attention','failed')", name="state_valid"),
        Index("ix_health_checks_state_due", "state", "next_due_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    check_name: Mapped[str] = mapped_column(String(128), nullable=False)
    rule: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[HealthState] = mapped_column(String(32), nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    diagnostic: Mapped[str | None] = mapped_column(String(2000))
    artifact_key: Mapped[str | None] = mapped_column(String(512))


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_target_created", "target_type", "target_id", "created_at"),
        Index("ix_audit_events_run_created", "run_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    target_type: Mapped[str] = mapped_column(String(128), nullable=False)
    target_id: Mapped[str] = mapped_column(String(255), nullable=False)
    result: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    artifact_key: Mapped[str | None] = mapped_column(String(512))


class UIAcknowledgement(TimestampMixin, Base):
    __tablename__ = "ui_acknowledgements"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "run_id",
            "alert_key",
            name="uq_ui_ack_user_run_alert",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_ui_ack_run_user", "run_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE")
    )
    alert_key: Mapped[str] = mapped_column(String(255), nullable=False)
    acknowledged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class ApprovalRequest(TimestampMixin, Base):
    __tablename__ = "approval_requests"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_approval_requests_idempotency_key"),
        CheckConstraint("length(operation) > 0", name="operation_nonempty"),
        CheckConstraint(
            "state IN ('pending','approved','rejected','expired','cancelled')",
            name="state_valid",
        ),
        Index("ix_approval_requests_state_expires", "state", "expires_at"),
        Index("ix_approval_requests_run", "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    operation: Mapped[str] = mapped_column(String(255), nullable=False)
    redacted_preview: Mapped[str | None] = mapped_column(String(4000))
    requester: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[ApprovalState] = mapped_column(
        String(32), nullable=False, default=ApprovalState.PENDING
    )
    decision: Mapped[str | None] = mapped_column(String(2000))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    audit_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("audit_events.id", ondelete="SET NULL")
    )
    artifact_key: Mapped[str | None] = mapped_column(String(512))


class CodeRepository(TimestampMixin, Base):
    __tablename__ = "repositories"
    __table_args__ = (
        CheckConstraint("length(full_name) > 2", name="full_name_nonempty"),
        CheckConstraint(
            "profile_state IN ('unprofiled','profiling','profiled','failed')",
            name="profile_state_valid",
        ),
        Index("ix_repositories_enabled_name", "enabled", "full_name"),
        Index("ix_repositories_profile_state", "profile_state", "full_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    full_name: Mapped[str] = mapped_column(String(201), nullable=False, unique=True)
    clone_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    default_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    allowlist_version: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    discovery_version: Mapped[str | None] = mapped_column(String(128))
    discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    profile_state: Mapped[str] = mapped_column(String(32), nullable=False, default="unprofiled")
    profiled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    profile_error_code: Mapped[str | None] = mapped_column(String(128))
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_review_prompted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewedCommit(TimestampMixin, Base):
    __tablename__ = "reviewed_commits"
    __table_args__ = (
        UniqueConstraint("repository_id", "head_sha", name="uq_reviewed_commits_repo_head"),
        CheckConstraint(
            "status IN ('queued','running','succeeded','attention','failed','cancelled')",
            name="status_valid",
        ),
        CheckConstraint("risk IN ('high','medium','low')", name="risk_valid"),
        CheckConstraint(
            "trigger IN ('push','quick_scan','daily','catchup','manual')",
            name="trigger_valid",
        ),
        Index("ix_reviewed_commits_status_created", "status", "created_at"),
        Index("ix_reviewed_commits_repo_created", "repository_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    delivery_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    ref: Mapped[str] = mapped_column(String(500), nullable=False)
    base_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    risk: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")
    trigger: Mapped[str] = mapped_column(String(32), nullable=False, default="push")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    report_artifact_key: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewFinding(TimestampMixin, Base):
    __tablename__ = "review_findings"
    __table_args__ = (
        UniqueConstraint(
            "reviewed_commit_id",
            "fingerprint",
            name="uq_review_findings_commit_fingerprint",
        ),
        CheckConstraint("severity IN ('block','important','suggestion')", name="severity_valid"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_valid"),
        CheckConstraint("line >= 1", name="line_positive"),
        CheckConstraint("published_inline = false", name="phase3_inline_disabled"),
        Index("ix_review_findings_commit_severity", "reviewed_commit_id", "severity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    reviewed_commit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reviewed_commits.id", ondelete="CASCADE"), nullable=False
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    line: Mapped[int] = mapped_column(nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    explanation: Mapped[str] = mapped_column(String(2000), nullable=False)
    reproduction_or_missing_test: Mapped[str] = mapped_column(String(2000), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    assumptions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    evidence_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    published_inline: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class RepositoryProfile(TimestampMixin, Base):
    __tablename__ = "project_profiles"
    __table_args__ = (
        UniqueConstraint(
            "repository_id",
            "commit_sha",
            "profile_version",
            name="uq_project_profiles_repo_commit_version",
        ),
        Index("ix_project_profiles_repo_reviewed", "repository_id", "reviewed"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_version: Mapped[str] = mapped_column(String(128), nullable=False)
    summary: Mapped[str] = mapped_column(String(2000), nullable=False)
    artifact_key: Mapped[str] = mapped_column(String(64), nullable=False)
    instruction_provenance: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    reviewed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class RepositoryDiscoveryState(TimestampMixin, Base):
    """Resumable cursor for account-scale repository discovery.

    One row per discovery scope.  ``cursor``/``page`` record where the last
    interrupted listing stopped so a restart continues instead of re-walking
    the installation, and ``discovery_complete`` marks a finished sweep.
    """

    __tablename__ = "repository_discovery_state"
    __table_args__ = (
        UniqueConstraint("scope", name="uq_repository_discovery_state_scope"),
        CheckConstraint("page >= 1", name="page_positive"),
        CheckConstraint("discovered_count >= 0", name="discovered_count_nonnegative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    scope: Mapped[str] = mapped_column(String(128), nullable=False)
    discovery_version: Mapped[str] = mapped_column(String(128), nullable=False)
    page: Mapped[int] = mapped_column(nullable=False, default=1)
    cursor: Mapped[str | None] = mapped_column(String(512))
    discovered_count: Mapped[int] = mapped_column(nullable=False, default=0)
    discovery_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_full_name: Mapped[str | None] = mapped_column(String(201))
    last_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewFindingDismissal(TimestampMixin, Base):
    """A finding the user dismissed, with the reason it was dismissed.

    Dismissals are keyed by repository and finding fingerprint so the same
    defect stays suppressed across later commits, and the reason is retained
    for review history rather than being discarded.
    """

    __tablename__ = "review_finding_dismissals"
    __table_args__ = (
        UniqueConstraint(
            "repository_id",
            "fingerprint",
            name="uq_review_finding_dismissals_repo_fingerprint",
        ),
        CheckConstraint("length(reason_code) > 0", name="reason_code_nonempty"),
        Index("ix_review_finding_dismissals_repo", "repository_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(2000))
    dismissed_by: Mapped[str] = mapped_column(String(255), nullable=False)
    dismissed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL")
    )


class DailyReviewReport(TimestampMixin, Base):
    """One nightly consolidation of the day's reviews, keyed by Toronto date.

    ``report_date`` is the local period date the consolidation covers, which is
    what makes the nightly job idempotent across retries and restarts.
    """

    __tablename__ = "daily_review_reports"
    __table_args__ = (
        UniqueConstraint("report_date", name="uq_daily_review_reports_report_date"),
        CheckConstraint(
            "status IN ('running','succeeded','attention','failed')",
            name="status_valid",
        ),
        CheckConstraint("commit_count >= 0", name="commit_count_nonnegative"),
        CheckConstraint("repository_count >= 0", name="repository_count_nonnegative"),
        Index("ix_daily_review_reports_date", "report_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    schedule_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    commit_count: Mapped[int] = mapped_column(nullable=False, default=0)
    repository_count: Mapped[int] = mapped_column(nullable=False, default=0)
    finding_counts: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False, default=dict)
    artifact_key: Mapped[str | None] = mapped_column(String(64))
    delivery_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("deliveries.id", ondelete="SET NULL")
    )
    error_code: Mapped[str | None] = mapped_column(String(128))
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Course(TimestampMixin, Base):
    """One scoped course synchronized from the user's academic source."""

    __tablename__ = "courses"
    __table_args__ = (
        UniqueConstraint("notion_id", name="uq_courses_notion_id"),
        CheckConstraint("length(notion_id) > 0", name="notion_id_nonempty"),
        CheckConstraint("priority >= 0 AND priority <= 100", name="priority_valid"),
        Index("ix_courses_term_code", "term", "course_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    notion_id: Mapped[str] = mapped_column(String(255), nullable=False)
    course_code: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    term: Mapped[str] = mapped_column(String(128), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="America/Toronto")
    priority: Mapped[int] = mapped_column(nullable=False, default=50)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Assessment(TimestampMixin, Base):
    """Canonical action item store preserving legacy assessment identities."""

    __tablename__ = "assessments"
    __table_args__ = (
        UniqueConstraint("notion_id", name="uq_assessments_notion_id"),
        CheckConstraint(
            "domain IS NULL OR domain IN "
            "('academic','career','personal','administrative','project')",
            name="action_domain_valid",
        ),
        CheckConstraint(
            "item_kind IN "
            "('task','assignment','quiz','exam','lab','tutorial','meeting','event',"
            "'application_follow_up','interview_prep','deadline','needs_review')",
            name="item_kind_valid",
        ),
        CheckConstraint(
            "status IN "
            "('inbox','needs_review','to_do','in_progress','waiting','done','canceled')",
            name="action_status_valid",
        ),
        CheckConstraint(
            "source_kind IN "
            "('notion_action_items','notion_applications','notion_interviews','learn',"
            "'google_calendar','manual','legacy_notion_course_assessment')",
            name="action_source_kind_valid",
        ),
        CheckConstraint(
            "date_precision IS NULL OR date_precision IN ('date','datetime')",
            name="action_date_precision_valid",
        ),
        CheckConstraint(
            "("
            "date_precision IS NULL AND start_date IS NULL AND end_date_exclusive IS NULL "
            "AND start_at IS NULL AND end_at IS NULL"
            ") OR ("
            "date_precision = 'date' AND start_date IS NOT NULL AND start_at IS NULL "
            "AND end_at IS NULL AND (end_date_exclusive IS NULL OR end_date_exclusive > start_date)"
            ") OR ("
            "date_precision = 'datetime' AND start_date IS NULL AND end_date_exclusive IS NULL "
            "AND start_at IS NOT NULL AND timezone IS NOT NULL "
            "AND (end_at IS NULL OR end_at > start_at)"
            ")",
            name="action_temporal_shape_valid",
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_valid"),
        CheckConstraint(
            "fact_state IN ('unconfirmed','confirmed','ambiguous','rejected')",
            name="fact_state_valid",
        ),
        CheckConstraint(
            "grade_weight_percent IS NULL OR "
            "(grade_weight_percent >= 0 AND grade_weight_percent <= 100)",
            name="grade_weight_valid",
        ),
        CheckConstraint("source_page IS NULL OR source_page >= 1", name="source_page_positive"),
        CheckConstraint("estimated_minutes > 0", name="estimated_minutes_positive"),
        CheckConstraint("confidence_gap >= 0 AND confidence_gap <= 1", name="confidence_gap_valid"),
        CheckConstraint("scope_size >= 0 AND scope_size <= 100", name="scope_size_valid"),
        CheckConstraint(
            "ends_at IS NULL OR due_at IS NULL OR ends_at > due_at",
            name="assessment_date_range_valid",
        ),
        CheckConstraint(
            "calendar_semantic_status IS NULL OR calendar_semantic_status IN "
            "('valid','unavailable','invalid')",
            name="calendar_semantic_status_valid",
        ),
        CheckConstraint(
            "calendar_semantic_intent_status IS NULL OR calendar_semantic_intent_status IN "
            "('valid','unavailable','invalid')",
            name="calendar_semantic_intent_status_valid",
        ),
        CheckConstraint(
            "calendar_semantic_intent_value IS NULL OR "
            "calendar_semantic_intent_value IN ('study','regular')",
            name="calendar_semantic_intent_value_valid",
        ),
        CheckConstraint(
            "calendar_semantic_intent_rationale IS NULL OR "
            "length(calendar_semantic_intent_rationale) > 0",
            name="calendar_semantic_intent_rationale_nonempty",
        ),
        Index("ix_assessments_course_due", "course_id", "due_at"),
        Index(
            "ix_assessments_active_incomplete_temporal",
            "is_all_day",
            "due_at",
            "title",
            "id",
            postgresql_where=text(
                "active IS TRUE AND archived IS FALSE AND completed IS FALSE "
                "AND notion_last_edited_at IS NOT NULL"
            ),
            sqlite_where=text(
                "active = 1 AND archived = 0 AND completed = 0 "
                "AND notion_last_edited_at IS NOT NULL"
            ),
        ),
        Index("ix_assessments_fact_state", "fact_state", "due_at"),
        Index("ix_assessments_source_active", "source_id", "active"),
        Index(
            "ix_assessments_domain_status_temporal",
            "domain",
            "status",
            "start_date",
            "start_at",
        ),
        Index("ix_assessments_application", "application_id", "status"),
        Index("ix_assessments_interview", "interview_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    course_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE")
    )
    notion_id: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    assessment_type: Mapped[str] = mapped_column(String(64), nullable=False)
    domain: Mapped[str | None] = mapped_column(String(32))
    item_kind: Mapped[str] = mapped_column(String(64), nullable=False, default="needs_review")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="to_do")
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date_exclusive: Mapped[date | None] = mapped_column(Date)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    date_precision: Mapped[str | None] = mapped_column(String(32))
    timezone: Mapped[str | None] = mapped_column(String(64))
    grade_weight_percent: Mapped[float | None] = mapped_column(Float)
    estimated_minutes: Mapped[int] = mapped_column(nullable=False, default=60)
    confidence_gap: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    scope_size: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    scope: Mapped[str | None] = mapped_column(String(4_000))
    fact_state: Mapped[str] = mapped_column(String(32), nullable=False, default="unconfirmed")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    ambiguity_reason: Mapped[str | None] = mapped_column(String(2_000))
    source_page: Mapped[int | None] = mapped_column()
    source_block: Mapped[str | None] = mapped_column(String(255))
    source_url: Mapped[str | None] = mapped_column(String(1_000))
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_all_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_kind: Mapped[str] = mapped_column(
        String(64), nullable=False, default="legacy_notion_course_assessment"
    )
    source_label: Mapped[str | None] = mapped_column(String(255))
    context: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source_id: Mapped[str | None] = mapped_column(String(255))
    source_scope: Mapped[str | None] = mapped_column(String(255))
    notion_database_id: Mapped[str | None] = mapped_column(String(255))
    notion_data_source_id: Mapped[str | None] = mapped_column(String(255))
    notion_page_id: Mapped[str | None] = mapped_column(String(255))
    notion_title_property_id: Mapped[str | None] = mapped_column(String(255))
    notion_date_property_id: Mapped[str | None] = mapped_column(String(255))
    notion_domain_property_id: Mapped[str | None] = mapped_column(String(255))
    notion_status_property_id: Mapped[str | None] = mapped_column(String(255))
    notion_kind_property_id: Mapped[str | None] = mapped_column(String(255))
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("career_applications.id", ondelete="SET NULL")
    )
    interview_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("career_interview_events.id", ondelete="SET NULL")
    )
    notion_last_edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    title_property_id: Mapped[str | None] = mapped_column(String(255))
    label_source: Mapped[str | None] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    calendar_semantic_overview: Mapped[str | None] = mapped_column(String(700))
    calendar_semantic_description: Mapped[str | None] = mapped_column(String(1500))
    calendar_semantic_status: Mapped[str | None] = mapped_column(String(32))
    calendar_semantic_intent_value: Mapped[str | None] = mapped_column(String(128))
    calendar_semantic_intent_status: Mapped[str | None] = mapped_column(String(32))
    calendar_semantic_intent_rationale: Mapped[str | None] = mapped_column(String(500))
    calendar_semantic_intent_evidence_ids: Mapped[list[str] | None] = mapped_column(JSON)
    calendar_semantic_evidence_ids: Mapped[list[str] | None] = mapped_column(JSON)
    calendar_semantic_description_evidence_ids: Mapped[list[str] | None] = mapped_column(JSON)
    calendar_semantic_source_fingerprint: Mapped[str | None] = mapped_column(String(128))
    calendar_semantic_source_last_edited_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    calendar_semantic_model_identity: Mapped[str | None] = mapped_column(String(128))
    calendar_semantic_config_version: Mapped[str | None] = mapped_column(String(128))
    calendar_semantic_prompt_version: Mapped[str | None] = mapped_column(String(128))
    calendar_semantic_analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AcademicCourseCalendar(TimestampMixin, Base):
    """Discovered calendar source mapping for one course page."""

    __tablename__ = "academic_course_calendars"
    __table_args__ = (
        UniqueConstraint("course_id", name="uq_academic_course_calendars_course"),
        UniqueConstraint("child_data_source_id", name="uq_academic_course_calendars_source"),
        UniqueConstraint("external_source_id", name="uq_academic_course_calendars_external_source"),
        CheckConstraint("length(course_page_id) > 0", name="course_page_id_nonempty"),
        CheckConstraint(
            "child_database_id IS NULL OR length(child_database_id) > 0",
            name="child_database_id_nonempty",
        ),
        CheckConstraint(
            "child_data_source_id IS NULL OR length(child_data_source_id) > 0",
            name="child_data_source_id_nonempty",
        ),
        CheckConstraint(
            "external_source_id IS NULL OR length(external_source_id) > 0",
            name="external_source_id_nonempty",
        ),
        CheckConstraint(
            "source_kind IN ('notion','google_ical')",
            name="source_kind_valid",
        ),
        CheckConstraint(
            "discovery_status IN ('valid','missing','inaccessible','malformed','duplicate')",
            name="discovery_status_valid",
        ),
        Index("ix_academic_course_calendars_status", "discovery_status", "last_synced_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), nullable=False
    )
    course_page_id: Mapped[str] = mapped_column(String(255), nullable=False)
    child_database_id: Mapped[str | None] = mapped_column(String(255))
    child_data_source_id: Mapped[str | None] = mapped_column(String(255))
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="notion")
    external_source_id: Mapped[str | None] = mapped_column(String(255))
    title_property_id: Mapped[str | None] = mapped_column(String(255))
    title_property_name: Mapped[str | None] = mapped_column(String(255))
    date_property_id: Mapped[str | None] = mapped_column(String(255))
    date_property_name: Mapped[str | None] = mapped_column(String(255))
    learn_context_property_id: Mapped[str | None] = mapped_column(String(255))
    learn_context_property_name: Mapped[str | None] = mapped_column(String(255))
    discovery_status: Mapped[str] = mapped_column(String(32), nullable=False, default="valid")
    diagnostic_code: Mapped[str | None] = mapped_column(String(128))
    diagnostic_fingerprint: Mapped[str | None] = mapped_column(String(128))
    last_discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LearnCourse(TimestampMixin, Base):
    """Bounded LEARN course metadata from visible Brightspace pages."""

    __tablename__ = "learn_courses"
    __table_args__ = (
        UniqueConstraint("org_unit_id", name="uq_learn_courses_org_unit"),
        CheckConstraint("length(org_unit_id) > 0", name="org_unit_id_nonempty"),
        CheckConstraint("length(code) > 0", name="code_nonempty"),
        CheckConstraint("length(name) > 0", name="name_nonempty"),
        CheckConstraint("url IS NULL OR length(url) > 0", name="url_nonempty"),
        Index("ix_learn_courses_active_code", "active", "code"),
        Index("ix_learn_courses_term_code", "term", "code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_unit_id: Mapped[str] = mapped_column(String(128), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    term: Mapped[str | None] = mapped_column(String(128))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    url: Mapped[str | None] = mapped_column(String(2048))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    disappeared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LearnScheduledItem(TimestampMixin, Base):
    """Normalized LEARN calendar/schedule item metadata."""

    __tablename__ = "learn_scheduled_items"
    __table_args__ = (
        UniqueConstraint("source_id", name="uq_learn_scheduled_items_source"),
        UniqueConstraint("source_id", "fingerprint", name="uq_learn_scheduled_items_fingerprint"),
        CheckConstraint("length(source_id) > 0", name="source_id_nonempty"),
        CheckConstraint("length(title) > 0", name="title_nonempty"),
        CheckConstraint("length(fingerprint) > 0", name="fingerprint_nonempty"),
        CheckConstraint(
            "date_precision IN ('date','datetime')",
            name="date_precision_valid",
        ),
        CheckConstraint(
            "completion_state IN ('unknown','incomplete','complete','cancelled')",
            name="completion_state_valid",
        ),
        CheckConstraint(
            "end_at IS NULL OR start_at IS NULL OR end_at >= start_at",
            name="range_valid",
        ),
        Index("ix_learn_scheduled_items_course_start", "course_id", "start_date", "start_at"),
        Index("ix_learn_scheduled_items_active", "active", "start_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learn_courses.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    date_precision: Mapped[str] = mapped_column(String(32), nullable=False, default="datetime")
    completion_state: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    url: Mapped[str | None] = mapped_column(String(2048))
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    disappeared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LearnAnnouncementSource(TimestampMixin, Base):
    """Announcement source metadata without raw title or body storage."""

    __tablename__ = "learn_announcement_sources"
    __table_args__ = (
        UniqueConstraint("source_id", name="uq_learn_announcements_source"),
        UniqueConstraint("source_id", "fingerprint", name="uq_learn_announcements_fingerprint"),
        CheckConstraint("length(source_id) > 0", name="source_id_nonempty"),
        CheckConstraint("length(fingerprint) > 0", name="fingerprint_nonempty"),
        CheckConstraint("url IS NULL OR length(url) > 0", name="url_nonempty"),
        Index("ix_learn_announcements_course_effective", "course_id", "effective_at"),
        Index("ix_learn_announcements_visible_effective", "visible", "effective_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learn_courses.id", ondelete="CASCADE"), nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    url: Mapped[str | None] = mapped_column(String(2048))
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    has_attachments: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    disappeared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LearnAnnouncementSemanticResult(TimestampMixin, Base):
    """Validated semantic interpretation of one announcement fingerprint."""

    __tablename__ = "learn_announcement_semantic_results"
    __table_args__ = (
        UniqueConstraint(
            "announcement_id",
            "source_fingerprint",
            "prompt_version",
            name="uq_learn_semantic_announcement_fingerprint_prompt",
        ),
        CheckConstraint("length(source_fingerprint) > 0", name="source_fingerprint_nonempty"),
        CheckConstraint("length(summary) > 0", name="summary_nonempty"),
        CheckConstraint("length(why_it_matters) > 0", name="why_it_matters_nonempty"),
        CheckConstraint("length(model_identity) > 0", name="model_identity_nonempty"),
        CheckConstraint("length(prompt_version) > 0", name="prompt_version_nonempty"),
        CheckConstraint(
            "status IN ('valid','summary_unavailable','invalid','superseded','source_removed')",
            name="status_valid",
        ),
        Index("ix_learn_semantic_status", "status", "interpreted_at"),
        Index("ix_learn_semantic_announcement_status", "announcement_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    announcement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learn_announcement_sources.id", ondelete="CASCADE"), nullable=False
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learn_courses.id", ondelete="CASCADE"), nullable=False
    )
    source_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    summary: Mapped[str] = mapped_column(String(1600), nullable=False)
    why_it_matters: Mapped[str] = mapped_column(String(1600), nullable=False)
    action_items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    evidence_fragments: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    source_url: Mapped[str | None] = mapped_column(String(2048))
    model_identity: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(128), nullable=False)
    critic_model_identity: Mapped[str | None] = mapped_column(String(255))
    critic_prompt_version: Mapped[str | None] = mapped_column(String(128))
    repair_attempted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    anti_copy_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="valid")
    error_code: Mapped[str | None] = mapped_column(String(128))
    interpreted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LearnDatedImplication(TimestampMixin, Base):
    """Grounded academic date extracted semantically from an announcement."""

    __tablename__ = "learn_dated_implications"
    __table_args__ = (
        UniqueConstraint(
            "semantic_result_id",
            "implication_key",
            name="uq_learn_dated_implications_result_key",
        ),
        CheckConstraint("length(implication_key) > 0", name="implication_key_nonempty"),
        CheckConstraint("length(activity_type) > 0", name="activity_type_nonempty"),
        CheckConstraint(
            "date_precision IN ('date','datetime')",
            name="date_precision_valid",
        ),
        CheckConstraint(
            "end_at IS NULL OR start_at IS NULL OR end_at >= start_at",
            name="range_valid",
        ),
        CheckConstraint(
            "status IN ('active','superseded','source_removed')",
            name="status_valid",
        ),
        Index("ix_learn_dated_implications_reminder", "status", "reminder_date"),
        Index("ix_learn_dated_implications_course_date", "course_id", "academic_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    semantic_result_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learn_announcement_semantic_results.id", ondelete="CASCADE"),
        nullable=False,
    )
    announcement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learn_announcement_sources.id", ondelete="CASCADE"), nullable=False
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learn_courses.id", ondelete="CASCADE"), nullable=False
    )
    source_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    implication_key: Mapped[str] = mapped_column(String(128), nullable=False)
    activity_type: Mapped[str] = mapped_column(String(128), nullable=False)
    academic_date: Mapped[date] = mapped_column(Date, nullable=False)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    date_precision: Mapped[str] = mapped_column(String(32), nullable=False)
    reminder_date: Mapped[date] = mapped_column(Date, nullable=False)
    evidence_fragments: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")


class LearnNotificationDelivery(TimestampMixin, Base):
    """Idempotency records for LEARN briefing selections and reconnect alerts."""

    __tablename__ = "learn_notification_deliveries"
    __table_args__ = (
        UniqueConstraint("message_key", name="uq_learn_notification_deliveries_message"),
        CheckConstraint("length(message_key) > 0", name="message_key_nonempty"),
        CheckConstraint(
            "delivery_kind IN ('announcement_window','day_before','reconnect_alert')",
            name="delivery_kind_valid",
        ),
        CheckConstraint(
            "status IN ('pending','sent','failed','superseded')",
            name="status_valid",
        ),
        Index(
            "ix_learn_notification_delivery_occurrence",
            "delivery_kind",
            "occurrence_date",
            "status",
        ),
        Index("ix_learn_notification_delivery_source", "announcement_id", "source_fingerprint"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    message_key: Mapped[str] = mapped_column(String(512), nullable=False)
    delivery_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    occurrence_date: Mapped[date] = mapped_column(Date, nullable=False)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    channel: Mapped[str] = mapped_column(String(64), nullable=False, default="discord")
    target: Mapped[str | None] = mapped_column(String(255))
    announcement_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("learn_announcement_sources.id", ondelete="CASCADE")
    )
    dated_implication_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("learn_dated_implications.id", ondelete="CASCADE")
    )
    source_fingerprint: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_message_id: Mapped[str | None] = mapped_column(String(255))
    error_code: Mapped[str | None] = mapped_column(String(128))


class LearnNotionProposalLink(TimestampMixin, Base):
    """LEARN source to confirmation-gated academic proposal/idempotency link."""

    __tablename__ = "learn_notion_proposal_links"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_learn_proposal_links_idempotency"),
        CheckConstraint("length(source_id) > 0", name="source_id_nonempty"),
        CheckConstraint("length(source_fingerprint) > 0", name="source_fingerprint_nonempty"),
        CheckConstraint(
            "source_kind IN ('scheduled_item','announcement_implication')",
            name="source_kind_valid",
        ),
        CheckConstraint(
            "operation IN ('create_learn_calendar_event','enrich_learn_calendar_event')",
            name="operation_valid",
        ),
        CheckConstraint(
            "state IN "
            "('pending','confirmed','rejected','expired','applied','superseded','skipped')",
            name="state_valid",
        ),
        Index(
            "ix_learn_proposal_links_source",
            "source_kind",
            "source_id",
            "source_fingerprint",
            "operation",
            "state",
        ),
        Index("ix_learn_proposal_links_proposal", "proposal_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    scheduled_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("learn_scheduled_items.id", ondelete="CASCADE")
    )
    dated_implication_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("learn_dated_implications.id", ondelete="CASCADE")
    )
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False)
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("academic_proposed_changes.id", ondelete="SET NULL")
    )
    reserved_calendar_notion_id: Mapped[str] = mapped_column(String(255), nullable=False)
    target_notion_page_id: Mapped[str | None] = mapped_column(String(255))
    target_expected_title: Mapped[str | None] = mapped_column(String(500))
    target_expected_date: Mapped[date | None] = mapped_column(Date)
    target_expected_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    target_expected_last_edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    learn_context_property_id: Mapped[str] = mapped_column(String(255), nullable=False)
    learn_context_property_name: Mapped[str] = mapped_column(String(255), nullable=False)
    explicit_request_key: Mapped[str | None] = mapped_column(String(255))
    conflict_code: Mapped[str | None] = mapped_column(String(128))


class CareerJobsWorkspace(TimestampMixin, Base):
    """Discovered Notion Jobs page and Interviews source state."""

    __tablename__ = "career_jobs_workspaces"
    __table_args__ = (
        UniqueConstraint("scope", name="uq_career_jobs_workspaces_scope"),
        UniqueConstraint("jobs_page_id", name="uq_career_jobs_workspaces_page"),
        UniqueConstraint(
            "interviews_data_source_id", name="uq_career_jobs_workspaces_interviews_source"
        ),
        CheckConstraint("length(scope) > 0", name="scope_nonempty"),
        CheckConstraint(
            "discovery_status IN ('valid','missing','duplicate','inaccessible','malformed')",
            name="discovery_status_valid",
        ),
        Index("ix_career_jobs_workspaces_status", "discovery_status", "last_synced_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    scope: Mapped[str] = mapped_column(String(128), nullable=False, default="default")
    jobs_page_id: Mapped[str | None] = mapped_column(String(255))
    jobs_page_title: Mapped[str | None] = mapped_column(String(255))
    discovery_status: Mapped[str] = mapped_column(String(32), nullable=False, default="missing")
    diagnostic_code: Mapped[str | None] = mapped_column(String(128))
    diagnostic_fingerprint: Mapped[str | None] = mapped_column(String(128))
    interviews_database_id: Mapped[str | None] = mapped_column(String(255))
    interviews_data_source_id: Mapped[str | None] = mapped_column(String(255))
    title_property_id: Mapped[str | None] = mapped_column(String(255))
    title_property_name: Mapped[str | None] = mapped_column(String(255))
    date_property_id: Mapped[str | None] = mapped_column(String(255))
    date_property_name: Mapped[str | None] = mapped_column(String(255))
    last_discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class CareerSyncCursor(TimestampMixin, Base):
    """Career-domain resumable source cursor."""

    __tablename__ = "career_sync_cursors"
    __table_args__ = (
        UniqueConstraint("scope", name="uq_career_sync_cursors_scope"),
        CheckConstraint("length(scope) > 0", name="scope_nonempty"),
        CheckConstraint("status IN ('idle','running','succeeded','failed')", name="status_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    scope: Mapped[str] = mapped_column(String(255), nullable=False)
    source_version: Mapped[str] = mapped_column(String(128), nullable=False, default="notion")
    cursor: Mapped[str | None] = mapped_column(String(512))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="idle")
    error_code: Mapped[str | None] = mapped_column(String(128))


class CareerApplicationTable(TimestampMixin, Base):
    """Lossless ordinary-table snapshot under the Jobs page."""

    __tablename__ = "career_application_tables"
    __table_args__ = (
        UniqueConstraint("table_block_id", name="uq_career_application_tables_block"),
        CheckConstraint("length(table_block_id) > 0", name="table_block_id_nonempty"),
        CheckConstraint("table_order >= 0", name="table_order_nonnegative"),
        CheckConstraint("row_count >= 0", name="row_count_nonnegative"),
        Index("ix_career_application_tables_workspace_active", "workspace_id", "active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("career_jobs_workspaces.id", ondelete="CASCADE"), nullable=False
    )
    table_block_id: Mapped[str] = mapped_column(String(255), nullable=False)
    table_order: Mapped[int] = mapped_column(nullable=False, default=0)
    has_column_header: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    row_count: Mapped[int] = mapped_column(nullable=False, default=0)
    column_count: Mapped[int] = mapped_column(nullable=False, default=0)
    content_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class CareerApplicationRow(TimestampMixin, Base):
    """One table_row snapshot with bounded cell text and source fingerprint."""

    __tablename__ = "career_application_rows"
    __table_args__ = (
        UniqueConstraint("row_block_id", name="uq_career_application_rows_block"),
        CheckConstraint("length(row_block_id) > 0", name="row_block_id_nonempty"),
        CheckConstraint("row_order >= 0", name="row_order_nonnegative"),
        Index("ix_career_application_rows_table_active", "table_id", "active", "row_order"),
        Index("ix_career_application_rows_fingerprint", "content_fingerprint"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    table_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("career_application_tables.id", ondelete="CASCADE"), nullable=False
    )
    row_block_id: Mapped[str] = mapped_column(String(255), nullable=False)
    row_order: Mapped[int] = mapped_column(nullable=False)
    is_header: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cells: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    normalized_cells: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    content_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class CareerApplicationInterpretation(TimestampMixin, Base):
    """Optional Qwen interpretation of an application row with cited cells."""

    __tablename__ = "career_application_interpretations"
    __table_args__ = (
        UniqueConstraint("row_id", name="uq_career_application_interpretations_row"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_valid"),
        Index("ix_career_application_interpretations_company", "company_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    row_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("career_application_rows.id", ondelete="CASCADE"), nullable=False
    )
    company_name: Mapped[str | None] = mapped_column(String(255))
    role_title: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str | None] = mapped_column(String(255))
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    model_version: Mapped[str | None] = mapped_column(String(255))
    interpreted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CareerApplication(TimestampMixin, Base):
    """Typed Notion application row while preserving legacy table snapshots."""

    __tablename__ = "career_applications"
    __table_args__ = (
        UniqueConstraint("application_page_id", name="uq_career_applications_page"),
        CheckConstraint(
            "source_kind IN ('notion_applications','manual')",
            name="source_kind_valid",
        ),
        CheckConstraint(
            "status IS NULL OR length(status) > 0",
            name="status_nonempty",
        ),
        CheckConstraint(
            "next_action_status IS NULL OR "
            "next_action_status IN ('inbox','needs_review','to_do','in_progress','waiting',"
            "'done','canceled')",
            name="next_action_status_valid",
        ),
        CheckConstraint(
            "("
            "deadline_date_precision IS NULL AND deadline_start_date IS NULL "
            "AND deadline_start_at IS NULL AND deadline_timezone IS NULL"
            ") OR ("
            "deadline_date_precision = 'date' AND deadline_start_date IS NOT NULL "
            "AND deadline_start_at IS NULL"
            ") OR ("
            "deadline_date_precision = 'datetime' AND deadline_start_date IS NULL "
            "AND deadline_start_at IS NOT NULL AND deadline_timezone IS NOT NULL"
            ")",
            name="deadline_temporal_shape_valid",
        ),
        CheckConstraint(
            "("
            "next_action_date_precision IS NULL AND next_action_start_date IS NULL "
            "AND next_action_start_at IS NULL AND next_action_timezone IS NULL"
            ") OR ("
            "next_action_date_precision = 'date' AND next_action_start_date IS NOT NULL "
            "AND next_action_start_at IS NULL"
            ") OR ("
            "next_action_date_precision = 'datetime' AND next_action_start_date IS NULL "
            "AND next_action_start_at IS NOT NULL AND next_action_timezone IS NOT NULL"
            ")",
            name="next_action_temporal_shape_valid",
        ),
        Index("ix_career_applications_company_active", "company_name", "active"),
        Index("ix_career_applications_status_active", "status", "active"),
        Index("ix_career_applications_legacy_row", "legacy_application_row_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("career_jobs_workspaces.id", ondelete="SET NULL")
    )
    legacy_application_row_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("career_application_rows.id", ondelete="SET NULL")
    )
    application_page_id: Mapped[str | None] = mapped_column(String(255))
    applications_database_id: Mapped[str | None] = mapped_column(String(255))
    applications_data_source_id: Mapped[str | None] = mapped_column(String(255))
    source_kind: Mapped[str] = mapped_column(
        String(64), nullable=False, default="notion_applications"
    )
    company_name: Mapped[str | None] = mapped_column(String(255))
    role_title: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str | None] = mapped_column(String(128))
    deadline_date_precision: Mapped[str | None] = mapped_column(String(32))
    deadline_start_date: Mapped[date | None] = mapped_column(Date)
    deadline_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_timezone: Mapped[str | None] = mapped_column(String(64))
    next_action: Mapped[str | None] = mapped_column(String(1_000))
    next_action_status: Mapped[str | None] = mapped_column(String(32))
    next_action_date_precision: Mapped[str | None] = mapped_column(String(32))
    next_action_start_date: Mapped[date | None] = mapped_column(Date)
    next_action_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_action_timezone: Mapped[str | None] = mapped_column(String(64))
    posting_url: Mapped[str | None] = mapped_column(String(2_048))
    applied_on: Mapped[date | None] = mapped_column(Date)
    next_action_date: Mapped[date | None] = mapped_column(Date)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notion_last_edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_url: Mapped[str | None] = mapped_column(String(2_048))
    source: Mapped[str | None] = mapped_column(String(255))
    location: Mapped[str | None] = mapped_column(String(255))
    contact_name: Mapped[str | None] = mapped_column(String(255))
    contact_email: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)
    property_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    content_fingerprint: Mapped[str | None] = mapped_column(String(128))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class CareerInterviewEvent(TimestampMixin, Base):
    """One Notion Interviews page, with deterministic scheduling fields."""

    __tablename__ = "career_interview_events"
    __table_args__ = (
        UniqueConstraint("interview_page_id", name="uq_career_interview_events_page"),
        CheckConstraint("length(interview_page_id) > 0", name="interview_page_id_nonempty"),
        CheckConstraint("length(title) > 0", name="title_nonempty"),
        CheckConstraint(
            "calendar_semantic_status IS NULL OR calendar_semantic_status IN "
            "('valid','unavailable','invalid')",
            name="calendar_semantic_status_valid",
        ),
        CheckConstraint(
            "calendar_semantic_intent_status IS NULL OR calendar_semantic_intent_status IN "
            "('valid','unavailable','invalid')",
            name="calendar_semantic_intent_status_valid",
        ),
        CheckConstraint(
            "calendar_semantic_intent_value IS NULL OR "
            "calendar_semantic_intent_value IN ('study','regular')",
            name="calendar_semantic_intent_value_valid",
        ),
        CheckConstraint(
            "calendar_semantic_intent_rationale IS NULL OR "
            "length(calendar_semantic_intent_rationale) > 0",
            name="calendar_semantic_intent_rationale_nonempty",
        ),
        Index("ix_career_interview_events_date_active", "active", "local_date"),
        Index("ix_career_interview_events_workspace", "workspace_id", "active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("career_jobs_workspaces.id", ondelete="CASCADE"), nullable=False
    )
    interview_page_id: Mapped[str] = mapped_column(String(255), nullable=False)
    interviews_database_id: Mapped[str | None] = mapped_column(String(255))
    interviews_data_source_id: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    date_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    local_date: Mapped[date | None] = mapped_column(Date)
    is_all_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="America/Toronto")
    notion_last_edited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(2_048))
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    property_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    url_candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    content_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    content_artifact_key: Mapped[str | None] = mapped_column(String(512))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    calendar_semantic_overview: Mapped[str | None] = mapped_column(String(700))
    calendar_semantic_description: Mapped[str | None] = mapped_column(String(1500))
    calendar_semantic_status: Mapped[str | None] = mapped_column(String(32))
    calendar_semantic_intent_value: Mapped[str | None] = mapped_column(String(128))
    calendar_semantic_intent_status: Mapped[str | None] = mapped_column(String(32))
    calendar_semantic_intent_rationale: Mapped[str | None] = mapped_column(String(500))
    calendar_semantic_intent_evidence_ids: Mapped[list[str] | None] = mapped_column(JSON)
    calendar_semantic_evidence_ids: Mapped[list[str] | None] = mapped_column(JSON)
    calendar_semantic_description_evidence_ids: Mapped[list[str] | None] = mapped_column(JSON)
    calendar_semantic_source_fingerprint: Mapped[str | None] = mapped_column(String(128))
    calendar_semantic_source_last_edited_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    calendar_semantic_model_identity: Mapped[str | None] = mapped_column(String(128))
    calendar_semantic_config_version: Mapped[str | None] = mapped_column(String(128))
    calendar_semantic_prompt_version: Mapped[str | None] = mapped_column(String(128))
    calendar_semantic_analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CareerInterviewApplicationLink(TimestampMixin, Base):
    """Current resolved or unresolved link from interview event to application row."""

    __tablename__ = "career_interview_application_links"
    __table_args__ = (
        UniqueConstraint("interview_id", name="uq_career_interview_links_interview"),
        CheckConstraint(
            "state IN ('matched','ambiguous','needs_clarification','rejected')",
            name="state_valid",
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_valid"),
        CheckConstraint("resolution_source IN ('model','user')", name="resolution_source_valid"),
        Index("ix_career_interview_links_row", "application_row_id", "state"),
        Index("ix_career_interview_links_application", "application_id", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    interview_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("career_interview_events.id", ondelete="CASCADE"), nullable=False
    )
    application_row_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("career_application_rows.id", ondelete="SET NULL")
    )
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("career_applications.id", ondelete="SET NULL")
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    rationale: Mapped[str] = mapped_column(String(1_000), nullable=False)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    clarification_id: Mapped[uuid.UUID | None] = mapped_column()
    interview_content_fingerprint: Mapped[str | None] = mapped_column(String(128))
    application_content_fingerprint: Mapped[str | None] = mapped_column(String(128))
    resolution_source: Mapped[str] = mapped_column(String(16), nullable=False, default="model")
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class CareerResearchSnapshot(TimestampMixin, Base):
    """Bounded research metadata and artifact references for an interview."""

    __tablename__ = "career_research_snapshots"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','fetched','partial','failed','stale')",
            name="status_valid",
        ),
        Index("ix_career_research_interview_time", "interview_id", "retrieved_at"),
        Index("ix_career_research_status_freshness", "status", "freshness_expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    interview_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("career_interview_events.id", ondelete="CASCADE"), nullable=False
    )
    source_url: Mapped[str] = mapped_column(String(2_048), nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(String(2_048))
    company_name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    content_fingerprint: Mapped[str | None] = mapped_column(String(128))
    excerpt_artifact_key: Mapped[str | None] = mapped_column(String(512))
    failure_code: Mapped[str | None] = mapped_column(String(128))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    freshness_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class CareerPreparationPlan(TimestampMixin, Base):
    """Current preparation plan for one interview."""

    __tablename__ = "career_preparation_plans"
    __table_args__ = (
        UniqueConstraint("interview_id", name="uq_career_preparation_plans_interview"),
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint(
            "status IN ('draft','current','stale','failed')",
            name="status_valid",
        ),
        Index("ix_career_preparation_plans_status", "status", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    interview_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("career_interview_events.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="current")
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    summary: Mapped[str] = mapped_column(String(1_000), nullable=False)
    next_actions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    evidence: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    research_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("career_research_snapshots.id", ondelete="SET NULL")
    )
    artifact_key: Mapped[str | None] = mapped_column(String(512))
    material_change_reason: Mapped[str | None] = mapped_column(String(1_000))
    plan_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class CareerPreparationPlanRevision(TimestampMixin, Base):
    """Immutable revision history for interview preparation plans."""

    __tablename__ = "career_preparation_plan_revisions"
    __table_args__ = (
        UniqueConstraint("plan_id", "revision", name="uq_career_plan_revisions_plan_revision"),
        CheckConstraint("revision >= 1", name="revision_positive"),
        Index("ix_career_plan_revisions_interview", "interview_id", "revision"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("career_preparation_plans.id", ondelete="CASCADE"), nullable=False
    )
    interview_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("career_interview_events.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    summary: Mapped[str] = mapped_column(String(1_000), nullable=False)
    next_actions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    evidence: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    research_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("career_research_snapshots.id", ondelete="SET NULL")
    )
    artifact_key: Mapped[str | None] = mapped_column(String(512))
    material_change_reason: Mapped[str | None] = mapped_column(String(1_000))
    plan_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class CareerClarification(TimestampMixin, Base):
    """Durable career clarification and continuation state."""

    __tablename__ = "career_clarifications"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_career_clarifications_idempotency"),
        CheckConstraint(
            "kind IN ('jobs_configuration','application_match','interview_date','posting_url',"
            "'preparation_context')",
            name="kind_valid",
        ),
        CheckConstraint(
            "state IN ('pending','delivered','answered','resolved','failed','expired','cancelled')",
            name="state_valid",
        ),
        Index("ix_career_clarifications_state_expiry", "state", "expires_at"),
        Index("ix_career_clarifications_subject", "subject_type", "subject_id", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False)
    question: Mapped[str] = mapped_column(String(1_000), nullable=False)
    choices: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    partial_state: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    response_artifact_key: Mapped[str | None] = mapped_column(String(512))
    response_summary: Mapped[str | None] = mapped_column(String(1_000))
    discord_channel_id: Mapped[str | None] = mapped_column(String(32))
    discord_user_id: Mapped[str | None] = mapped_column(String(32))
    delivery_id: Mapped[str | None] = mapped_column(String(255))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(128))


class CareerReminderDelivery(TimestampMixin, Base):
    """Morning reminder idempotency keyed by local day and interview."""

    __tablename__ = "career_reminder_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "interview_id",
            "reminder_date",
            "reminder_kind",
            name="uq_career_reminder_deliveries_event_day_kind",
        ),
        CheckConstraint("days_until >= 0", name="days_until_nonnegative"),
        CheckConstraint(
            "status IN ('pending','included','sent','failed','skipped')",
            name="status_valid",
        ),
        Index("ix_career_reminder_deliveries_date_status", "reminder_date", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    interview_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("career_interview_events.id", ondelete="CASCADE"), nullable=False
    )
    reminder_date: Mapped[date] = mapped_column(Date, nullable=False)
    reminder_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    days_until: Mapped[int] = mapped_column(nullable=False)
    delivery_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("deliveries.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    included_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(128))


class CareerWriteProposal(TimestampMixin, Base):
    """A confirmation-gated Notion write preview for career records."""

    __tablename__ = "career_write_proposals"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_career_write_proposals_idempotency"),
        CheckConstraint(
            "operation IN ('interview_date','preparation_plan')",
            name="operation_valid",
        ),
        CheckConstraint(
            "state IN ('pending','confirmed','applying','rejected','applied','expired','failed')",
            name="state_valid",
        ),
        Index("ix_career_write_proposals_state", "state", "created_at"),
        Index("ix_career_write_proposals_target", "target_page_id", "operation"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    interview_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("career_interview_events.id", ondelete="SET NULL")
    )
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    target_page_id: Mapped[str] = mapped_column(String(255), nullable=False)
    expected_last_edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    redacted_preview: Mapped[str] = mapped_column(String(4_000), nullable=False)
    confirmation_token: Mapped[str] = mapped_column(String(255), nullable=False)
    confirmation_event: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    requester: Mapped[str | None] = mapped_column(String(255))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CareerWriteReceipt(TimestampMixin, Base):
    """Durable bounded receipt state for non-transactional career writes."""

    __tablename__ = "career_write_receipts"
    __table_args__ = (
        UniqueConstraint("proposal_id", "operation_id", name="uq_career_write_receipts_operation"),
        CheckConstraint("length(payload_hash) = 64", name="payload_hash_valid"),
        CheckConstraint(
            "state IN ('in_progress','applied','uncertain','failed')",
            name="state_valid",
        ),
        Index("ix_career_write_receipts_state", "proposal_id", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("career_write_proposals.id", ondelete="CASCADE"), nullable=False
    )
    operation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="in_progress")
    receipt: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(128))


class AcademicClarification(TimestampMixin, Base):
    """Durable Discord clarification state for ambiguous assessment labels."""

    __tablename__ = "academic_clarifications"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_academic_clarifications_idempotency"),
        CheckConstraint("length(event_notion_id) > 0", name="event_notion_id_nonempty"),
        CheckConstraint(
            "state IN "
            "('pending','delivered','claimed','ignored','applied','conflict','failed','expired')",
            name="state_valid",
        ),
        CheckConstraint(
            "decision IS NULL OR decision IN "
            "('quiz','assignment','tutorial','lab','event','ignore')",
            name="decision_valid",
        ),
        CheckConstraint(
            "write_status IN ('none','skipped','pending','applied','conflict','failed')",
            name="write_status_valid",
        ),
        Index("ix_academic_clarifications_state_expiry", "state", "expires_at"),
        Index("ix_academic_clarifications_event", "event_notion_id", "expected_edited_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    course_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("courses.id", ondelete="SET NULL")
    )
    assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assessments.id", ondelete="SET NULL")
    )
    event_notion_id: Mapped[str] = mapped_column(String(255), nullable=False)
    original_title: Mapped[str] = mapped_column(String(1_024), nullable=False)
    raw_label: Mapped[str | None] = mapped_column(String(1_024))
    quiz_preview_title: Mapped[str] = mapped_column(String(1_024), nullable=False)
    assignment_preview_title: Mapped[str] = mapped_column(String(1_024), nullable=False)
    tutorial_preview_title: Mapped[str | None] = mapped_column(String(1_024))
    lab_preview_title: Mapped[str | None] = mapped_column(String(1_024))
    event_preview_title: Mapped[str | None] = mapped_column(String(1_024))
    expected_edited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    title_property_id: Mapped[str | None] = mapped_column(String(255))
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    delivery_id: Mapped[str | None] = mapped_column(String(255))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision: Mapped[str | None] = mapped_column(String(32))
    decision_user_id: Mapped[int | None] = mapped_column(BigInteger)
    decision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    write_status: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    write_error_code: Mapped[str | None] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AcademicSetupReminder(TimestampMixin, Base):
    """Once-per-day setup reminder delivery state keyed by condition fingerprint."""

    __tablename__ = "academic_setup_reminders"
    __table_args__ = (
        UniqueConstraint(
            "condition",
            "schema_fingerprint",
            "reminder_day",
            name="uq_academic_setup_reminders_condition_day",
        ),
        CheckConstraint("length(condition) > 0", name="condition_nonempty"),
        CheckConstraint("length(schema_fingerprint) > 0", name="schema_fingerprint_nonempty"),
        CheckConstraint(
            "state IN ('pending','delivered','failed','cleared')",
            name="state_valid",
        ),
        Index("ix_academic_setup_reminders_condition", "condition", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    condition: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    reminder_day: Mapped[date] = mapped_column(Date, nullable=False)
    affected_course_codes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_id: Mapped[str | None] = mapped_column(String(255))
    error_code: Mapped[str | None] = mapped_column(String(128))


class AcademicDiscourseSession(TimestampMixin, Base):
    """Durable multi-turn academic discourse state keyed by Discord event."""

    __tablename__ = "academic_discourse_sessions"
    __table_args__ = (
        UniqueConstraint("external_event_id", name="uq_academic_discourse_external_event"),
        CheckConstraint("length(external_event_id) > 0", name="external_event_id_nonempty"),
        CheckConstraint("channel = 'discord'", name="channel_discord_only"),
        CheckConstraint("state IN ('open','completed','expired')", name="state_valid"),
        CheckConstraint("missed_review_count >= 0", name="missed_review_count_nonnegative"),
        CheckConstraint("reminder_count >= 0", name="reminder_count_nonnegative"),
        Index("ix_academic_discourse_state_expiry", "state", "expires_at"),
        Index("ix_academic_discourse_last_turn", "state", "last_turn_at"),
        Index(
            "ix_academic_discourse_owner_kind_state_expiry",
            "discord_user_id",
            "discord_channel_id",
            "session_kind",
            "state",
            "expires_at",
        ),
        Index(
            "uq_academic_discourse_open_agent_clarification_owner",
            "discord_user_id",
            "discord_channel_id",
            "session_kind",
            unique=True,
            sqlite_where=text(
                "state = 'open' AND session_kind = 'agent_clarification' "
                "AND discord_user_id IS NOT NULL AND discord_channel_id IS NOT NULL"
            ),
            postgresql_where=text(
                "state = 'open' AND session_kind = 'agent_clarification' "
                "AND discord_user_id IS NOT NULL AND discord_channel_id IS NOT NULL"
            ),
        ),
        Index(
            "ix_academic_discourse_discord_owner",
            "discord_channel_id",
            "discord_user_id",
            "state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    external_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False, default="discord")
    discord_channel_id: Mapped[str | None] = mapped_column(String(24))
    discord_user_id: Mapped[str | None] = mapped_column(String(24))
    session_kind: Mapped[str] = mapped_column(String(64), nullable=False, default="learning_focus")
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    partial_state: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    missed_review_count: Mapped[int] = mapped_column(nullable=False, default=0)
    reminder_count: Mapped[int] = mapped_column(nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_turn_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AcademicLearningFocus(TimestampMixin, Base):
    """A user-confirmed academic struggle that should shape practice planning."""

    __tablename__ = "academic_learning_focuses"
    __table_args__ = (
        UniqueConstraint("source_external_event_id", name="uq_academic_focus_source_event"),
        CheckConstraint("length(topic) > 0", name="topic_nonempty"),
        CheckConstraint("status IN ('active','snoozed')", name="status_valid"),
        CheckConstraint("reinforcement_count >= 1", name="reinforcement_count_positive"),
        CheckConstraint("missed_review_count >= 0", name="missed_review_count_nonnegative"),
        CheckConstraint("reminder_count >= 0", name="reminder_count_nonnegative"),
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint(
            "practice_minutes IS NULL OR practice_minutes > 0",
            name="practice_minutes_positive",
        ),
        Index("ix_academic_focus_status_review", "status", "next_review_at"),
        Index("ix_academic_focus_course_status", "course_id", "status"),
        Index(
            "ix_academic_focus_owner_status_review",
            "owner_user_id",
            "owner_channel_id",
            "status",
            "next_review_at",
        ),
        Index(
            "ix_academic_focus_owner_course_status",
            "owner_user_id",
            "owner_channel_id",
            "course_id",
            "status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    course_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("courses.id", ondelete="SET NULL")
    )
    assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assessments.id", ondelete="SET NULL")
    )
    course_code: Mapped[str | None] = mapped_column(String(64))
    topic: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    owner_user_id: Mapped[str | None] = mapped_column(String(24))
    owner_channel_id: Mapped[str | None] = mapped_column(String(24))
    revision: Mapped[int] = mapped_column(nullable=False, default=1)
    source_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("academic_discourse_sessions.id", ondelete="SET NULL")
    )
    source_external_event_id: Mapped[str | None] = mapped_column(String(255))
    reinforcement_count: Mapped[int] = mapped_column(nullable=False, default=1)
    next_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    practice_due_on: Mapped[date | None] = mapped_column(Date)
    practice_minutes: Mapped[int | None] = mapped_column()
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_review_prompted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reinforced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    missed_review_count: Mapped[int] = mapped_column(nullable=False, default=0)
    reminder_count: Mapped[int] = mapped_column(nullable=False, default=0)
    last_reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    snoozed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AcademicDiscourseTurn(TimestampMixin, Base):
    """Idempotency metadata for one Discord message in a discourse session."""

    __tablename__ = "academic_discourse_turns"
    __table_args__ = (
        UniqueConstraint("external_event_id", name="uq_academic_discourse_turn_event"),
        Index("ix_academic_discourse_turn_session", "session_id", "received_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("academic_discourse_sessions.id", ondelete="CASCADE"), nullable=False
    )
    external_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AcademicLearningFocusEvent(TimestampMixin, Base):
    """Append-only lifecycle events for a focus until the focus is hard-deleted."""

    __tablename__ = "academic_learning_focus_events"
    __table_args__ = (
        UniqueConstraint("external_event_id", name="uq_academic_focus_events_external_event"),
        CheckConstraint("length(event_type) > 0", name="event_type_nonempty"),
        CheckConstraint("actor IS NULL OR length(actor) > 0", name="actor_nonempty"),
        Index("ix_academic_focus_events_focus_time", "focus_id", "occurred_at"),
        Index("ix_academic_focus_events_session", "session_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    focus_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("academic_learning_focuses.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("academic_discourse_sessions.id", ondelete="SET NULL")
    )
    external_event_id: Mapped[str | None] = mapped_column(String(255))
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(255))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class AcademicReflectionMemory(TimestampMixin, Base):
    """Raw reflection text plus optional embedding data for academic retrieval."""

    __tablename__ = "academic_reflection_memories"
    __table_args__ = (
        UniqueConstraint("external_event_id", name="uq_academic_reflection_external_event"),
        CheckConstraint("length(raw_text) > 0", name="raw_text_nonempty"),
        CheckConstraint(
            "embedding_dimensions IS NULL OR embedding_dimensions > 0",
            name="embedding_dimensions_positive",
        ),
        Index("ix_academic_reflections_focus_time", "focus_id", "recorded_at"),
        Index("ix_academic_reflections_session", "session_id", "recorded_at"),
        Index(
            "ix_academic_reflections_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_where=text("embedding IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    focus_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("academic_learning_focuses.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("academic_discourse_sessions.id", ondelete="SET NULL")
    )
    external_event_id: Mapped[str | None] = mapped_column(String(255))
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    redacted_summary: Mapped[str | None] = mapped_column(String(2_000))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024).with_variant(JSON, "sqlite"))
    embedding_model: Mapped[str | None] = mapped_column(String(255))
    embedding_dimensions: Mapped[int | None] = mapped_column()
    embedding_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FixedCommitment(TimestampMixin, Base):
    """A fixed class/event that the allocator must never move."""

    __tablename__ = "fixed_commitments"
    __table_args__ = (
        UniqueConstraint("notion_id", name="uq_fixed_commitments_notion_id"),
        CheckConstraint(
            "fact_state IN ('unconfirmed','confirmed','ambiguous','rejected')",
            name="fact_state_valid",
        ),
        CheckConstraint("source_page IS NULL OR source_page >= 1", name="source_page_positive"),
        CheckConstraint("ends_at > starts_at", name="commitment_times_valid"),
        Index("ix_fixed_commitments_time", "starts_at", "ends_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    course_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("courses.id", ondelete="SET NULL")
    )
    notion_id: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    commitment_type: Mapped[str] = mapped_column(String(64), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    fact_state: Mapped[str] = mapped_column(String(32), nullable=False, default="unconfirmed")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    ambiguity_reason: Mapped[str | None] = mapped_column(String(2_000))
    source_page: Mapped[int | None] = mapped_column()
    source_block: Mapped[str | None] = mapped_column(String(255))
    source_url: Mapped[str | None] = mapped_column(String(1_000))


class PlanningPreference(TimestampMixin, Base):
    """Versioned scheduling preferences; availability is structured JSON."""

    __tablename__ = "planning_preferences"
    __table_args__ = (
        UniqueConstraint("scope", name="uq_planning_preferences_scope"),
        CheckConstraint("daily_capacity_minutes > 0", name="daily_capacity_positive"),
        CheckConstraint("buffer_minutes >= 0", name="buffer_nonnegative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    scope: Mapped[str] = mapped_column(String(128), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    availability: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    daily_capacity_minutes: Mapped[int] = mapped_column(nullable=False, default=240)
    buffer_minutes: Mapped[int] = mapped_column(nullable=False, default=15)
    sleep_schedule: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    version: Mapped[str] = mapped_column(String(128), nullable=False, default="v1")


class AcademicDocument(TimestampMixin, Base):
    """Document metadata; raw bodies remain in the artifact store."""

    __tablename__ = "academic_documents"
    __table_args__ = (
        UniqueConstraint(
            "source_key", "document_version", name="uq_academic_documents_source_version"
        ),
        CheckConstraint("length(content_hash) = 64", name="content_hash_length"),
        CheckConstraint(
            "source_kind IS NULL OR source_kind IN "
            "('notion_page_body','notion_property_file','notion_block_file')",
            name="academic_document_source_kind_valid",
        ),
        CheckConstraint("source_key IS NULL OR length(source_key) > 0", name="source_key_nonempty"),
        CheckConstraint(
            "extraction_status IN "
            "('pending','extracted','partial','ocr_required','ocr_processing',"
            "'unsupported','failed','inactive')",
            name="academic_document_extraction_status_valid",
        ),
        Index("ix_academic_documents_course_version", "course_id", "document_version"),
        Index(
            "uq_academic_documents_legacy_version",
            "notion_id",
            "document_version",
            unique=True,
            postgresql_where=text("source_key IS NULL"),
            sqlite_where=text("source_key IS NULL"),
        ),
        Index("ix_academic_documents_assessment_active", "assessment_id", "active"),
        Index("ix_academic_documents_source_active", "source_key", "active", "retrieved_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    course_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("courses.id", ondelete="SET NULL")
    )
    assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assessments.id", ondelete="SET NULL")
    )
    notion_id: Mapped[str] = mapped_column(String(255), nullable=False)
    document_version: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    document_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_kind: Mapped[str | None] = mapped_column(String(64))
    source_page_id: Mapped[str | None] = mapped_column(String(255))
    source_block_id: Mapped[str | None] = mapped_column(String(255))
    source_property_id: Mapped[str | None] = mapped_column(String(255))
    source_key: Mapped[str | None] = mapped_column(String(512))
    original_filename: Mapped[str | None] = mapped_column(String(500))
    media_type: Mapped[str | None] = mapped_column(String(255))
    source_last_edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_url: Mapped[str | None] = mapped_column(String(1_000))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    artifact_key: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    access_classification: Mapped[str] = mapped_column(
        String(64), nullable=False, default="private"
    )
    extraction_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    extraction_error_code: Mapped[str | None] = mapped_column(String(128))
    extraction_error_detail: Mapped[str | None] = mapped_column(String(1_000))


class AcademicDocumentChunk(TimestampMixin, Base):
    """Bounded cited text suitable for PostgreSQL full-text retrieval."""

    __tablename__ = "academic_document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "ordinal", name="uq_academic_chunks_document_ordinal"),
        CheckConstraint("ordinal >= 0", name="ordinal_nonnegative"),
        CheckConstraint("source_page IS NULL OR source_page >= 1", name="source_page_positive"),
        CheckConstraint(
            "embedding_dimensions IS NULL OR embedding_dimensions > 0",
            name="academic_chunk_embedding_dimensions_positive",
        ),
        Index("ix_academic_chunks_document_page", "document_id", "source_page", "ordinal"),
        Index(
            "ix_academic_chunks_embedding_model",
            "embedding_model",
            "embedding_dimensions",
        ),
        Index(
            "ix_academic_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_where=text("embedding IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("academic_documents.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(nullable=False)
    heading: Mapped[str | None] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_page: Mapped[int | None] = mapped_column()
    source_block: Mapped[str | None] = mapped_column(String(255))
    source_url: Mapped[str | None] = mapped_column(String(1_000))
    token_count: Mapped[int | None] = mapped_column()
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR().with_variant(Text, "sqlite"), server_default=FetchedValue()
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024).with_variant(JSON, "sqlite"))
    embedding_model: Mapped[str | None] = mapped_column(String(255))
    embedding_dimensions: Mapped[int | None] = mapped_column()


class AcademicAssessmentMaterialProfile(TimestampMixin, Base):
    """Validated planning signals derived from active assessment material chunks."""

    __tablename__ = "academic_assessment_material_profiles"
    __table_args__ = (
        UniqueConstraint("profile_version", name="uq_academic_material_profiles_version"),
        CheckConstraint(
            "state IN ('validated','active','rejected','inactive')",
            name="state_valid",
        ),
        CheckConstraint("effort_lower_minutes > 0", name="effort_lower_positive"),
        CheckConstraint("effort_upper_minutes >= effort_lower_minutes", name="effort_range_valid"),
        CheckConstraint("scope_score >= 0 AND scope_score <= 1", name="scope_score_valid"),
        CheckConstraint(
            "dependency_risk_score >= 0 AND dependency_risk_score <= 1",
            name="dependency_risk_score_valid",
        ),
        CheckConstraint(
            "explicit_grade_weight_percent IS NULL OR "
            "(explicit_grade_weight_percent >= 0 AND explicit_grade_weight_percent <= 100)",
            name="explicit_grade_weight_valid",
        ),
        Index(
            "uq_academic_material_profiles_active_assessment",
            "assessment_id",
            unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
        Index("ix_academic_material_profiles_assessment_state", "assessment_id", "state"),
        Index("ix_academic_material_profiles_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    assessment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assessments.id", ondelete="CASCADE"), nullable=False
    )
    profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    profile_version: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="validated")
    deliverables_summary: Mapped[str] = mapped_column(String(1_000), nullable=False)
    success_criteria_summary: Mapped[str] = mapped_column(String(1_000), nullable=False)
    study_topics_summary: Mapped[str] = mapped_column(String(1_000), nullable=False)
    explicit_grade_weight_percent: Mapped[float | None] = mapped_column(Float)
    effort_lower_minutes: Mapped[int] = mapped_column(nullable=False)
    effort_upper_minutes: Mapped[int] = mapped_column(nullable=False)
    scope_score: Mapped[float] = mapped_column(Float, nullable=False)
    dependency_risk_score: Mapped[float] = mapped_column(Float, nullable=False)
    evidence_chunk_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    document_versions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    model_identity: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    critique: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    rejection_reason: Mapped[str | None] = mapped_column(String(500))
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AcademicSyncCursor(TimestampMixin, Base):
    """Durable Notion delta cursor, keyed by integration/database scope."""

    __tablename__ = "academic_sync_cursors"
    __table_args__ = (UniqueConstraint("scope", name="uq_academic_sync_cursors_scope"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    scope: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="notion")
    cursor: Mapped[str | None] = mapped_column(String(512))
    source_version: Mapped[str] = mapped_column(String(128), nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="idle")
    error_code: Mapped[str | None] = mapped_column(String(128))


class AcademicCheckIn(TimestampMixin, Base):
    """Redacted check-in metadata; message bodies belong in artifacts."""

    __tablename__ = "academic_checkins"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_academic_checkins_idempotency"),
        UniqueConstraint("external_event_id", name="uq_academic_checkins_external_event"),
        CheckConstraint(
            "status IN ('received','questioned','planned','proposal_pending','completed','failed')",
            name="status_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False)
    external_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_artifact_key: Mapped[str | None] = mapped_column(String(64))
    redacted_summary: Mapped[str | None] = mapped_column(String(2_000))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="received")


class AcademicProposedChange(TimestampMixin, Base):
    """A pending Notion mutation which cannot apply without exact confirmation."""

    __tablename__ = "academic_proposed_changes"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_academic_proposals_idempotency"),
        CheckConstraint(
            "state IN "
            "('pending','confirmed','applying','rejected','applied','expired','superseded')",
            name="state_valid",
        ),
        Index("ix_academic_proposals_state", "state", "created_at"),
        Index("ix_academic_proposals_owner_channel", "owner_discord_user_id", "discord_channel_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    checkin_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("academic_checkins.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    target_type: Mapped[str] = mapped_column(String(128), nullable=False)
    target_id: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_discord_user_id: Mapped[str | None] = mapped_column(String(32))
    discord_channel_id: Mapped[str | None] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    redacted_preview: Mapped[str] = mapped_column(String(4_000), nullable=False)
    confirmation_token: Mapped[str] = mapped_column(String(255), nullable=False)
    confirmation_event: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("academic_proposed_changes.id", ondelete="SET NULL")
    )
    superseded_reason: Mapped[str | None] = mapped_column(String(128))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AcademicInboundMaterial(TimestampMixin, Base):
    """Captured Discord PDF awaiting a confirmation-gated Notion seed."""

    __tablename__ = "academic_inbound_materials"
    __table_args__ = (
        UniqueConstraint(
            "discord_message_id",
            "discord_attachment_id",
            name="uq_academic_inbound_materials_discord_attachment",
        ),
        CheckConstraint("length(discord_message_id) > 0", name="discord_message_id_nonempty"),
        CheckConstraint("length(discord_attachment_id) > 0", name="discord_attachment_id_nonempty"),
        CheckConstraint("length(owner_discord_user_id) > 0", name="owner_discord_user_id_nonempty"),
        CheckConstraint("length(discord_channel_id) > 0", name="discord_channel_id_nonempty"),
        CheckConstraint("length(filename) > 0", name="filename_nonempty"),
        CheckConstraint(
            "declared_byte_size IS NULL OR declared_byte_size > 0",
            name="declared_size_positive",
        ),
        CheckConstraint("observed_byte_size > 0", name="observed_size_positive"),
        CheckConstraint("length(content_hash) = 64", name="content_hash_length"),
        CheckConstraint("length(raw_artifact_key) = 64", name="raw_artifact_key_length"),
        CheckConstraint(
            "state IN "
            "('captured','awaiting_target','proposal_pending','seeding','seeded','failed',"
            "'uncertain','expired')",
            name="state_valid",
        ),
        Index(
            "ix_academic_inbound_materials_owner_pending",
            "owner_discord_user_id",
            "discord_channel_id",
            "state",
            "created_at",
        ),
        Index("ix_academic_inbound_materials_assessment", "assessment_id", "state"),
        Index("ix_academic_inbound_materials_proposal", "proposal_id", "state"),
        Index("ix_academic_inbound_materials_hash", "content_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    discord_message_id: Mapped[str] = mapped_column(String(32), nullable=False)
    discord_attachment_id: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_discord_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    discord_channel_id: Mapped[str] = mapped_column(String(32), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str | None] = mapped_column(String(128))
    declared_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    observed_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_artifact_key: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="captured")
    assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assessments.id", ondelete="SET NULL")
    )
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("academic_proposed_changes.id", ondelete="SET NULL")
    )
    notion_page_id: Mapped[str | None] = mapped_column(String(255))
    notion_block_id: Mapped[str | None] = mapped_column(String(255))
    notion_upload_id: Mapped[str | None] = mapped_column(String(255))
    error_code: Mapped[str | None] = mapped_column(String(128))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AcademicProposalOperationJournal(TimestampMixin, Base):
    """Durable per-operation receipt state for non-transactional proposal batches."""

    __tablename__ = "academic_proposal_operation_journal"
    __table_args__ = (
        UniqueConstraint("proposal_id", "ordinal", name="uq_academic_proposal_operation"),
        CheckConstraint("ordinal >= 0", name="academic_proposal_operation_ordinal_nonnegative"),
        CheckConstraint("length(payload_hash) = 64", name="academic_proposal_payload_hash_valid"),
        CheckConstraint(
            "state IN ('in_progress','applied','uncertain','failed')",
            name="academic_proposal_operation_state_valid",
        ),
        Index(
            "ix_academic_proposal_operation_state",
            "proposal_id",
            "state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    proposal_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    ordinal: Mapped[int] = mapped_column(nullable=False)
    operation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="in_progress")
    receipt: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(128))


# Short aliases keep the storage contract convenient for document workers while
# retaining explicit academic names for callers that prefer them.
Document = AcademicDocument
DocumentChunk = AcademicDocumentChunk
SyncCursor = AcademicSyncCursor
CheckIn = AcademicCheckIn
ProposedChange = AcademicProposedChange
ProposalOperationJournal = AcademicProposalOperationJournal


__all__ = [
    "AcademicAssessmentMaterialProfile",
    "AcademicCheckIn",
    "AcademicClarification",
    "AcademicCourseCalendar",
    "AcademicDiscourseSession",
    "AcademicDiscourseTurn",
    "AcademicDocument",
    "AcademicDocumentChunk",
    "AcademicInboundMaterial",
    "AcademicLearningFocus",
    "AcademicLearningFocusEvent",
    "AcademicProposalOperationJournal",
    "AcademicProposedChange",
    "AcademicReflectionMemory",
    "AcademicSetupReminder",
    "AcademicSyncCursor",
    "AgentRun",
    "ApprovalRequest",
    "ApprovalState",
    "Assessment",
    "AuditEvent",
    "Base",
    "CareerApplicationInterpretation",
    "CareerApplicationRow",
    "CareerApplicationTable",
    "CareerClarification",
    "CareerInterviewApplicationLink",
    "CareerInterviewEvent",
    "CareerJobsWorkspace",
    "CareerPreparationPlan",
    "CareerPreparationPlanRevision",
    "CareerReminderDelivery",
    "CareerResearchSnapshot",
    "CareerSyncCursor",
    "CareerWriteProposal",
    "CareerWriteReceipt",
    "CheckIn",
    "CodeRepository",
    "Course",
    "DailyReviewReport",
    "Delivery",
    "DeliveryStatus",
    "DiscordAbortRequest",
    "DiscordWakeInbound",
    "Document",
    "DocumentChunk",
    "EvidenceClassification",
    "EvidenceRef",
    "FixedCommitment",
    "HealthCheck",
    "HealthState",
    "LearnAnnouncementSemanticResult",
    "LearnAnnouncementSource",
    "LearnCourse",
    "LearnDatedImplication",
    "LearnNotificationDelivery",
    "LearnNotionProposalLink",
    "LearnScheduledItem",
    "NativeConversationCompaction",
    "NativeConversationInboundEvent",
    "NativeConversationSession",
    "PlanningPreference",
    "ProposedChange",
    "RepositoryDiscoveryState",
    "RepositoryProfile",
    "ReviewFinding",
    "ReviewFindingDismissal",
    "ReviewedCommit",
    "RunStatus",
    "RunStep",
    "StepStatus",
    "SyncCursor",
    "UIAcknowledgement",
    "UserMemoryEvent",
    "UserMemoryFact",
]
