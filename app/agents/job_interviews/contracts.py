"""Typed contracts for the job interview preparation domain."""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agents.action_items import TemporalValue


class CareerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


class JobsDiscoveryStatus(StrEnum):
    VALID = "valid"
    MISSING = "missing"
    DUPLICATE = "duplicate"
    INACCESSIBLE = "inaccessible"
    MALFORMED = "malformed"


class LinkState(StrEnum):
    MATCHED = "matched"
    AMBIGUOUS = "ambiguous"
    NEEDS_CLARIFICATION = "needs_clarification"
    REJECTED = "rejected"


class ResearchStatus(StrEnum):
    PENDING = "pending"
    FETCHED = "fetched"
    PARTIAL = "partial"
    FAILED = "failed"
    STALE = "stale"


class PreparationPlanStatus(StrEnum):
    DRAFT = "draft"
    CURRENT = "current"
    STALE = "stale"
    FAILED = "failed"


class ClarificationKind(StrEnum):
    JOBS_CONFIGURATION = "jobs_configuration"
    APPLICATION_MATCH = "application_match"
    INTERVIEW_DATE = "interview_date"
    POSTING_URL = "posting_url"
    PREPARATION_CONTEXT = "preparation_context"


class WriteOperation(StrEnum):
    INTERVIEW_DATE = "interview_date"
    PREPARATION_PLAN = "preparation_plan"


class SourceCell(CareerModel):
    row_block_id: str = Field(min_length=1, max_length=255)
    column_index: int = Field(ge=0, le=200)
    header: str | None = Field(default=None, max_length=255)
    text: str = Field(min_length=1, max_length=1_000)


class JobsWorkspaceSnapshot(CareerModel):
    jobs_page_id: str | None = Field(default=None, min_length=1, max_length=255)
    jobs_page_title: str | None = Field(default=None, min_length=1, max_length=255)
    discovery_status: JobsDiscoveryStatus
    diagnostic_code: str | None = Field(default=None, max_length=128)
    diagnostic_fingerprint: str | None = Field(default=None, max_length=128)
    interviews_database_id: str | None = Field(default=None, min_length=1, max_length=255)
    interviews_data_source_id: str | None = Field(default=None, min_length=1, max_length=255)
    title_property_id: str | None = Field(default=None, min_length=1, max_length=255)
    date_property_id: str | None = Field(default=None, min_length=1, max_length=255)
    discovered_at: datetime

    @field_validator("discovered_at")
    @classmethod
    def discovered_at_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class ApplicationRowSnapshot(CareerModel):
    table_block_id: str = Field(min_length=1, max_length=255)
    row_block_id: str = Field(min_length=1, max_length=255)
    row_order: int = Field(ge=0, le=10_000)
    is_header: bool = False
    cells: tuple[str, ...] = Field(max_length=200)
    normalized_cells: tuple[str, ...] = Field(max_length=200)
    content_fingerprint: str = Field(min_length=1, max_length=128)
    last_seen_at: datetime
    active: bool = True

    @field_validator("last_seen_at")
    @classmethod
    def last_seen_at_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class ApplicationTableSnapshot(CareerModel):
    table_block_id: str = Field(min_length=1, max_length=255)
    has_column_header: bool
    table_order: int = Field(ge=0, le=100)
    content_fingerprint: str = Field(min_length=1, max_length=128)
    last_seen_at: datetime
    rows: tuple[ApplicationRowSnapshot, ...] = Field(default=(), max_length=1_000)
    active: bool = True

    @field_validator("last_seen_at")
    @classmethod
    def last_seen_at_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class ApplicationInterpretation(CareerModel):
    row_block_id: str = Field(min_length=1, max_length=255)
    company_name: str | None = Field(default=None, max_length=255)
    role_title: str | None = Field(default=None, max_length=500)
    status: str | None = Field(default=None, max_length=255)
    confidence: float = Field(ge=0, le=1)
    evidence: tuple[SourceCell, ...] = Field(default=(), max_length=12)
    model_version: str | None = Field(default=None, max_length=255)
    interpreted_at: datetime

    @field_validator("interpreted_at")
    @classmethod
    def interpreted_at_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class UrlCandidate(CareerModel):
    url: str = Field(min_length=1, max_length=2_048)
    source_kind: Literal["property", "block"]
    source_id: str = Field(min_length=1, max_length=255)
    label: str | None = Field(default=None, max_length=255)


CareerTemporalValue = TemporalValue


class CareerApplicationSnapshot(CareerModel):
    application_id: str = Field(min_length=1, max_length=255)
    company_name: str | None = Field(default=None, max_length=255)
    role_title: str | None = Field(default=None, max_length=500)
    pipeline_status: str | None = Field(default=None, max_length=255)
    next_action: str | None = Field(default=None, max_length=1_000)
    next_action_temporal: TemporalValue | None = None
    timezone: str = Field(default="America/Toronto", min_length=1, max_length=64)
    posting_url: str | None = Field(default=None, max_length=2_048)
    source_url: str | None = Field(default=None, max_length=2_048)
    content_fingerprint: str = Field(min_length=1, max_length=128)
    last_edited_at: datetime
    active: bool = True

    @field_validator("last_edited_at")
    @classmethod
    def last_edited_at_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class InterviewEventSnapshot(CareerModel):
    interview_page_id: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=500)
    date_start: datetime | None = None
    local_date: date
    is_all_day: bool
    timezone: str = Field(min_length=1, max_length=64, default="America/Toronto")
    temporal_value: TemporalValue | None = None
    application_id: str | None = Field(default=None, max_length=255)
    stage: str | None = Field(default=None, max_length=255)
    interview_status: str | None = Field(default=None, max_length=255)
    preparation_status: str | None = Field(default=None, max_length=255)
    last_edited_at: datetime
    source_url: str | None = Field(default=None, max_length=2_048)
    tags: tuple[str, ...] = Field(default=(), max_length=50)
    url_candidates: tuple[UrlCandidate, ...] = Field(default=(), max_length=25)
    content_fingerprint: str = Field(min_length=1, max_length=128)
    calendar_semantic_status: str | None = Field(default=None, max_length=32)
    calendar_semantic_overview: str | None = Field(default=None, max_length=700)
    calendar_semantic_description: str | None = Field(default=None, max_length=1_500)
    calendar_semantic_evidence_ids: tuple[str, ...] = Field(default=(), max_length=12)
    calendar_semantic_description_evidence_ids: tuple[str, ...] = Field(default=(), max_length=12)
    calendar_semantic_source_fingerprint: str | None = Field(default=None, max_length=128)
    calendar_semantic_source_last_edited_at: datetime | None = None
    calendar_semantic_model_identity: str | None = Field(default=None, max_length=128)
    calendar_semantic_config_version: str | None = Field(default=None, max_length=128)
    calendar_semantic_prompt_version: str | None = Field(default=None, max_length=128)
    calendar_semantic_analyzed_at: datetime | None = None
    active: bool = True
    archived: bool = False

    @field_validator(
        "date_start",
        "last_edited_at",
        "calendar_semantic_source_last_edited_at",
        "calendar_semantic_analyzed_at",
    )
    @classmethod
    def timestamps_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value) if value is not None else None


class InterviewApplicationLinkEvidence(CareerModel):
    interview_page_id: str = Field(min_length=1, max_length=255)
    row_block_id: str | None = Field(default=None, min_length=1, max_length=255)
    state: LinkState
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1, max_length=1_000)
    evidence: tuple[SourceCell, ...] = Field(default=(), max_length=12)
    clarification_id: str | None = Field(default=None, min_length=1, max_length=64)
    interview_content_fingerprint: str | None = Field(default=None, max_length=128)
    application_content_fingerprint: str | None = Field(default=None, max_length=128)
    resolution_source: Literal["model", "user"] = "model"
    resolved_at: datetime

    @field_validator("resolved_at")
    @classmethod
    def resolved_at_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class ResearchSnapshot(CareerModel):
    interview_page_id: str = Field(min_length=1, max_length=255)
    source_url: str = Field(min_length=1, max_length=2_048)
    canonical_url: str | None = Field(default=None, max_length=2_048)
    company_name: str | None = Field(default=None, max_length=255)
    status: ResearchStatus
    content_fingerprint: str | None = Field(default=None, max_length=128)
    excerpt_artifact_key: str | None = Field(default=None, max_length=512)
    failure_code: str | None = Field(default=None, max_length=128)
    retrieved_at: datetime
    freshness_expires_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("retrieved_at", "freshness_expires_at")
    @classmethod
    def research_times_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value) if value is not None else None


class PreparationPlanSnapshot(CareerModel):
    interview_page_id: str = Field(min_length=1, max_length=255)
    revision: int = Field(ge=1)
    status: PreparationPlanStatus = PreparationPlanStatus.CURRENT
    generated_at: datetime
    plan_hash: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=1_000)
    next_actions: tuple[str, ...] = Field(default=(), max_length=20)
    evidence: tuple[str, ...] = Field(default=(), max_length=50)
    research_snapshot_id: str | None = Field(default=None, min_length=1, max_length=64)
    artifact_key: str | None = Field(default=None, max_length=512)
    material_change_reason: str | None = Field(default=None, max_length=1_000)
    plan: dict[str, Any] = Field(default_factory=dict)

    @field_validator("generated_at")
    @classmethod
    def generated_at_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class CareerClarificationRequest(CareerModel):
    kind: ClarificationKind
    subject_type: Literal["workspace", "application_row", "interview", "plan", "research"]
    subject_id: str = Field(min_length=1, max_length=255)
    question: str = Field(min_length=1, max_length=1_000)
    choices: tuple[str, ...] = Field(default=(), max_length=20)
    idempotency_key: str = Field(min_length=1, max_length=512)
    partial_state: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def expires_at_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value) if value is not None else None


class CareerWriteProposalPreview(CareerModel):
    operation: WriteOperation
    target_page_id: str = Field(min_length=1, max_length=255)
    expected_last_edited_at: datetime | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    redacted_preview: str = Field(min_length=1, max_length=4_000)
    confirmation_token: str = Field(min_length=1, max_length=255)
    expires_at: datetime | None = None

    @field_validator("expected_last_edited_at", "expires_at")
    @classmethod
    def write_times_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value) if value is not None else None


class InterviewReminderFact(CareerModel):
    interview_page_id: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=500)
    reminder_date: date
    interview_date: date
    days_until: int = Field(ge=0, le=366)
    emphasis: Literal["normal", "milestone", "today"]
    preparation_plan_revision: int | None = Field(default=None, ge=1)
    grounded_next_actions: tuple[str, ...] = Field(default=(), max_length=10)

    @model_validator(mode="after")
    def validate_day_distance(self) -> InterviewReminderFact:
        if self.interview_date < self.reminder_date:
            raise ValueError("reminder cannot be after the interview date")
        if (self.interview_date - self.reminder_date).days != self.days_until:
            raise ValueError("days_until must match reminder and interview dates")
        return self
