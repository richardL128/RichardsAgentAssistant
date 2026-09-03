"""SQLAlchemy models for the shared durable orchestration core.

The relational schema intentionally stores metadata and references only.  Raw
documents, model transcripts, tool logs, and rendered reports belong in the
content-addressed artifact store and are represented here by ``*_artifact_key``
columns.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    MetaData,
    String,
    UniqueConstraint,
    text,
)
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
        Index("ix_repositories_enabled_name", "enabled", "full_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    full_name: Mapped[str] = mapped_column(String(201), nullable=False, unique=True)
    clone_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    default_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    allowlist_version: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ReviewedCommit(TimestampMixin, Base):
    __tablename__ = "reviewed_commits"
    __table_args__ = (
        UniqueConstraint("repository_id", "head_sha", name="uq_reviewed_commits_repo_head"),
        CheckConstraint(
            "status IN ('queued','running','succeeded','attention','failed','cancelled')",
            name="status_valid",
        ),
        CheckConstraint("risk IN ('high','medium','low')", name="risk_valid"),
        Index("ix_reviewed_commits_status_created", "status", "created_at"),
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


__all__ = [
    "AgentRun",
    "ApprovalRequest",
    "ApprovalState",
    "AuditEvent",
    "Base",
    "CodeRepository",
    "Delivery",
    "DeliveryStatus",
    "EvidenceClassification",
    "EvidenceRef",
    "HealthCheck",
    "HealthState",
    "RepositoryProfile",
    "ReviewFinding",
    "ReviewedCommit",
    "RunStatus",
    "RunStep",
    "StepStatus",
    "UIAcknowledgement",
]
