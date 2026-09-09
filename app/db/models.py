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
            "state IN ('queued','running','completed','failed')",
            name="state_valid",
        ),
        CheckConstraint(
            "interaction_action IS NULL OR interaction_action IN "
            "('quiz','assignment','tutorial','lab','studying_block','ignore')",
            name="interaction_action_valid",
        ),
        CheckConstraint("retry_count >= 0", name="retry_count_nonnegative"),
        CheckConstraint("length(content_artifact_key) > 0", name="content_artifact_key_nonempty"),
        Index("ix_discord_wake_state_created", "state", "created_at"),
        Index("ix_discord_wake_received", "received_at"),
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
    retry_count: Mapped[int] = mapped_column(nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
    """A typed assessment fact with citation and uncertainty state."""

    __tablename__ = "assessments"
    __table_args__ = (
        UniqueConstraint("notion_id", name="uq_assessments_notion_id"),
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
        Index("ix_assessments_course_due", "course_id", "due_at"),
        Index("ix_assessments_fact_state", "fact_state", "due_at"),
        Index("ix_assessments_source_active", "source_id", "active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), nullable=False
    )
    notion_id: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    assessment_type: Mapped[str] = mapped_column(String(64), nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    source_id: Mapped[str | None] = mapped_column(String(255))
    source_scope: Mapped[str | None] = mapped_column(String(255))
    notion_last_edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    title_property_id: Mapped[str | None] = mapped_column(String(255))
    label_source: Mapped[str | None] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class AcademicCourseCalendar(TimestampMixin, Base):
    """Discovered Assessments database/data-source mapping for one course page."""

    __tablename__ = "academic_course_calendars"
    __table_args__ = (
        UniqueConstraint("course_id", name="uq_academic_course_calendars_course"),
        UniqueConstraint("child_data_source_id", name="uq_academic_course_calendars_source"),
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
    title_property_id: Mapped[str | None] = mapped_column(String(255))
    title_property_name: Mapped[str | None] = mapped_column(String(255))
    date_property_id: Mapped[str | None] = mapped_column(String(255))
    date_property_name: Mapped[str | None] = mapped_column(String(255))
    discovery_status: Mapped[str] = mapped_column(String(32), nullable=False, default="valid")
    diagnostic_code: Mapped[str | None] = mapped_column(String(128))
    diagnostic_fingerprint: Mapped[str | None] = mapped_column(String(128))
    last_discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
            "('quiz','assignment','tutorial','lab','studying_block','ignore')",
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
    studying_block_preview_title: Mapped[str | None] = mapped_column(String(1_024))
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
    embedding: Mapped[list[float] | None] = mapped_column(Vector().with_variant(JSON, "sqlite"))
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


class StudyPlan(TimestampMixin, Base):
    """A deterministic plan window whose blocks are safely replayable."""

    __tablename__ = "study_plans"
    __table_args__ = (
        UniqueConstraint("plan_key", name="uq_study_plans_plan_key"),
        CheckConstraint("ends_on >= starts_on", name="study_plan_dates_valid"),
        CheckConstraint("status IN ('draft','published','superseded')", name="status_valid"),
        Index("ix_study_plans_window", "starts_on", "ends_on"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    plan_key: Mapped[str] = mapped_column(String(255), nullable=False)
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    ends_on: Mapped[date] = mapped_column(Date, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    preference_version: Mapped[str | None] = mapped_column(String(128))


class StudyBlock(TimestampMixin, Base):
    """One allocated work block, including visible carry-forward lineage."""

    __tablename__ = "study_blocks"
    __table_args__ = (
        UniqueConstraint("plan_id", "block_key", name="uq_study_blocks_plan_key"),
        CheckConstraint("allocated_minutes > 0", name="allocated_minutes_positive"),
        CheckConstraint("ends_at > starts_at", name="study_block_times_valid"),
        CheckConstraint(
            "status IN ('planned','in_progress','completed','incomplete','carried_forward')",
            name="status_valid",
        ),
        CheckConstraint(
            "block_kind IN ('assessment','practice')",
            name="block_kind_valid",
        ),
        Index("ix_study_blocks_plan_start", "plan_id", "starts_at"),
        Index("ix_study_blocks_assessment", "assessment_id", "status"),
        Index("ix_study_blocks_learning_focus", "learning_focus_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("study_plans.id", ondelete="CASCADE"), nullable=False
    )
    block_key: Mapped[str] = mapped_column(String(255), nullable=False)
    assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assessments.id", ondelete="SET NULL")
    )
    learning_focus_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("academic_learning_focuses.id", ondelete="SET NULL")
    )
    block_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="assessment")
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    allocated_minutes: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="planned")
    carry_forward_from_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("study_blocks.id", ondelete="SET NULL")
    )
    notes: Mapped[str | None] = mapped_column(String(2_000))


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
    embedding: Mapped[list[float] | None] = mapped_column(Vector().with_variant(JSON, "sqlite"))
    embedding_model: Mapped[str | None] = mapped_column(String(255))
    embedding_dimensions: Mapped[int | None] = mapped_column()


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
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("study_plans.id", ondelete="SET NULL")
    )


class AcademicProposedChange(TimestampMixin, Base):
    """A pending Notion mutation which cannot apply without exact confirmation."""

    __tablename__ = "academic_proposed_changes"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_academic_proposals_idempotency"),
        CheckConstraint(
            "state IN ('pending','confirmed','applying','rejected','applied','expired')",
            name="state_valid",
        ),
        Index("ix_academic_proposals_state", "state", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    checkin_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("academic_checkins.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    target_type: Mapped[str] = mapped_column(String(128), nullable=False)
    target_id: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    redacted_preview: Mapped[str] = mapped_column(String(4_000), nullable=False)
    confirmation_token: Mapped[str] = mapped_column(String(255), nullable=False)
    confirmation_event: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
    "AcademicCheckIn",
    "AcademicClarification",
    "AcademicCourseCalendar",
    "AcademicDiscourseSession",
    "AcademicDiscourseTurn",
    "AcademicDocument",
    "AcademicDocumentChunk",
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
    "CheckIn",
    "CodeRepository",
    "Course",
    "DailyReviewReport",
    "Delivery",
    "DeliveryStatus",
    "DiscordWakeInbound",
    "Document",
    "DocumentChunk",
    "EvidenceClassification",
    "EvidenceRef",
    "FixedCommitment",
    "HealthCheck",
    "HealthState",
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
    "StudyBlock",
    "StudyPlan",
    "SyncCursor",
    "UIAcknowledgement",
]
