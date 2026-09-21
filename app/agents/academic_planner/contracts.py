"""Typed academic calendar, memory, and proposal contracts."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agents.academic_planner.calendar_roles import AcademicCalendarRole
from app.agents.query_contracts import (
    CompletionMode,
    QueryEnvelope,
    TemporalQuery,
)


class PlannerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


class AssessmentType(StrEnum):
    TASK = "task"
    ASSIGNMENT = "assignment"
    QUIZ = "quiz"
    TUTORIAL = "tutorial"
    LAB = "lab"
    MIDTERM = "midterm"
    FINAL = "final"
    EVENT = "event"


class UserCreatableAssessmentType(StrEnum):
    ASSIGNMENT = "assignment"
    QUIZ = "quiz"
    TUTORIAL = "tutorial"
    LAB = "lab"


class ValidatedMaterialPlanningSignals(PlannerModel):
    """Critic-validated, evidence-backed inputs allowed to influence priority."""

    effort_lower_minutes: int = Field(gt=0, le=10_080)
    effort_upper_minutes: int = Field(gt=0, le=10_080)
    scope_score: float = Field(ge=0, le=1)
    dependency_risk_score: float = Field(ge=0, le=1)
    evidence_chunk_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    profile_version: str = Field(min_length=1, max_length=128)
    critic_validated: Literal[True] = True

    def model_post_init(self, __context: object) -> None:
        if self.effort_upper_minutes < self.effort_lower_minutes:
            raise ValueError("material effort upper bound must not be below its lower bound")


class FixedCommitment(PlannerModel):
    id: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=500)
    start_at: datetime
    end_at: datetime
    kind: Literal[
        "class",
        "test",
        "quiz",
        "midterm",
        "final",
        "deadline",
        "sleep",
        "commute",
        "personal",
        "event",
    ]

    @field_validator("start_at", "end_at")
    @classmethod
    def times_aware(cls, value: datetime) -> datetime:
        return _aware(value)

    def model_post_init(self, __context: object) -> None:
        if self.end_at <= self.start_at:
            raise ValueError("fixed commitment must end after it starts")


class AvailabilityWindow(PlannerModel):
    start_at: datetime
    end_at: datetime

    @field_validator("start_at", "end_at")
    @classmethod
    def times_aware(cls, value: datetime) -> datetime:
        return _aware(value)

    def model_post_init(self, __context: object) -> None:
        if self.end_at <= self.start_at:
            raise ValueError("availability window must end after it starts")


class CalendarAvailabilityFacts(PlannerModel):
    commitments: tuple[FixedCommitment, ...] = ()
    availability: tuple[AvailabilityWindow, ...] = ()
    buffer_minutes: int = Field(ge=0, le=240, default=15)
    horizon_days: int = Field(ge=7, le=14, default=7)


class AssessmentMaterialSourceKind(StrEnum):
    NOTION_PAGE_BODY = "notion_page_body"
    NOTION_PROPERTY_FILE = "notion_property_file"
    NOTION_BLOCK_FILE = "notion_block_file"


class AssessmentMaterialExtractionStatus(StrEnum):
    PENDING = "pending"
    EXTRACTED = "extracted"
    PARTIAL = "partial"
    OCR_REQUIRED = "ocr_required"
    OCR_PROCESSING = "ocr_processing"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    INACTIVE = "inactive"


class AssessmentMaterialDiagnostic(PlannerModel):
    code: str = Field(min_length=1, max_length=80)
    detail: str | None = Field(default=None, max_length=500)


class AssessmentMaterialSource(PlannerModel):
    """Stable source identity for one Notion assessment material input."""

    assessment_id: str = Field(min_length=1, max_length=255)
    course_id: str = Field(min_length=1, max_length=255)
    source_kind: AssessmentMaterialSourceKind
    source_page_id: str = Field(min_length=1, max_length=255)
    source_key: str = Field(min_length=1, max_length=512)
    source_block_id: str | None = Field(default=None, min_length=1, max_length=255)
    source_property_id: str | None = Field(default=None, min_length=1, max_length=255)
    ordinal: int = Field(ge=0, le=10_000)
    title: str = Field(min_length=1, max_length=500)
    media_type: str = Field(min_length=1, max_length=128)
    last_edited_at: datetime
    text: str | None = Field(default=None, max_length=1_000_000)

    @field_validator("last_edited_at")
    @classmethod
    def last_edited_at_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class GroundedAssessmentInsight(PlannerModel):
    """Free-form material insight whose evidence is host-verifiable."""

    insight_id: str = Field(min_length=1, max_length=255)
    assessment_id: str = Field(min_length=1, max_length=255)
    text: str = Field(min_length=1, max_length=700)
    evidence_chunk_ids: tuple[str, ...] = Field(min_length=1, max_length=8)

    @field_validator("evidence_chunk_ids")
    @classmethod
    def evidence_ids_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("insight evidence ids must be unique")
        return value


class AcademicPerformanceSignal(PlannerModel):
    """Explicitly verified academic performance evidence safe for briefing use."""

    signal_id: str = Field(min_length=1, max_length=255)
    course_code: str = Field(min_length=1, max_length=80)
    assessment_id: str | None = Field(default=None, min_length=1, max_length=255)
    outcome: Literal["positive", "improved", "needs_attention"]
    observed_at: datetime
    verified_summary: str = Field(min_length=1, max_length=500)
    explicitly_verified: Literal[True] = True

    @field_validator("observed_at")
    @classmethod
    def observed_at_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class MorningBriefingAssessmentFact(PlannerModel):
    assessment_id: str = Field(min_length=1, max_length=255)
    course_code: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=500)
    assessment_type: AssessmentType
    due_at_local: str = Field(min_length=1, max_length=64)
    exact_date_label: str = Field(min_length=1, max_length=64)
    relative_date_label: str = Field(min_length=1, max_length=64)
    priority_rank: int = Field(ge=1, le=50)


class MorningBriefingLearningFocusFact(PlannerModel):
    focus_id: str = Field(min_length=1, max_length=255)
    course_code: str | None = Field(default=None, min_length=1, max_length=80)
    topic: str = Field(min_length=1, max_length=300)
    verified_context_label: str = Field(min_length=1, max_length=500)


class MorningBriefingPerformanceFact(PlannerModel):
    signal_id: str = Field(min_length=1, max_length=255)
    course_code: str = Field(min_length=1, max_length=80)
    assessment_id: str | None = Field(default=None, min_length=1, max_length=255)
    outcome: Literal["positive", "improved", "needs_attention"]
    observed_at: str = Field(min_length=1, max_length=64)
    verified_summary: str = Field(min_length=1, max_length=500)


class MorningBriefingMaterialInsightFact(PlannerModel):
    insight_id: str = Field(min_length=1, max_length=255)
    assessment_id: str = Field(min_length=1, max_length=255)
    text: str = Field(min_length=1, max_length=700)
    evidence_chunk_ids: tuple[str, ...] = Field(min_length=1, max_length=8)


class MorningBriefingWorkBreakdownFact(PlannerModel):
    assessment_id: str = Field(min_length=1, max_length=255)
    steps: tuple[str, ...] = Field(min_length=1, max_length=8)
    rationale: str = Field(min_length=1, max_length=700)


class MorningBriefingContext(PlannerModel):
    """Bounded normalized facts that are the sole model input for a morning message."""

    current_local_date: str = Field(min_length=1, max_length=32)
    timezone: Literal["America/Toronto"] = "America/Toronto"
    student_display_name: Literal["Richard"] = "Richard"
    assessments: tuple[MorningBriefingAssessmentFact, ...] = Field(default=(), max_length=50)
    learning_focuses: tuple[MorningBriefingLearningFocusFact, ...] = Field(
        default=(), max_length=20
    )
    performance_signals: tuple[MorningBriefingPerformanceFact, ...] = Field(
        default=(), max_length=20
    )
    material_insights: tuple[MorningBriefingMaterialInsightFact, ...] = Field(
        default=(), max_length=30
    )
    work_breakdowns: tuple[MorningBriefingWorkBreakdownFact, ...] = Field(default=(), max_length=30)
    deferred_assessment_ids: tuple[str, ...] = Field(default=(), max_length=50)


class MorningBriefing(PlannerModel):
    """One Discord-ready model-written message plus host-verifiable provenance."""

    message_text: str = Field(min_length=1, max_length=2_000)
    referenced_assessment_ids: tuple[str, ...] = Field(default=(), max_length=50)
    referenced_focus_ids: tuple[str, ...] = Field(default=(), max_length=20)
    referenced_performance_signal_ids: tuple[str, ...] = Field(default=(), max_length=20)
    referenced_insight_ids: tuple[str, ...] = Field(default=(), max_length=30)

    @field_validator(
        "referenced_assessment_ids",
        "referenced_focus_ids",
        "referenced_performance_signal_ids",
        "referenced_insight_ids",
    )
    @classmethod
    def reference_ids_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("briefing reference ids must be unique")
        return value


class InboundMaterialProposalPreview(PlannerModel):
    """Bounded PDF metadata safe to show in an exact-confirmation preview."""

    inbound_material_id: UUID
    filename: str = Field(min_length=1, max_length=255)
    byte_size: int = Field(gt=0, le=20_971_520)


class ProposedChange(PlannerModel):
    field: Literal[
        "completed",
        "actual_minutes",
        "new_task",
        "new_deadline",
        "new_event",
        "create_assessment",
        "attach_assessment_material",
        "update_assessment",
        "archive_assessment",
        "create_learn_calendar_event",
        "enrich_learn_calendar_event",
    ]
    value: str = Field(min_length=1, max_length=1_000)
    assessment_id: str | None = Field(default=None, max_length=255)
    course_id: str | None = Field(default=None, max_length=255)
    course_code: str | None = Field(default=None, min_length=1, max_length=80)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    due_at: datetime | None = None
    ends_at: datetime | None = None
    is_all_day: Literal[True] | None = None
    assessment_type: AssessmentType | None = None
    expected_last_edited_at: datetime | None = None
    expected_title: str | None = Field(default=None, min_length=1, max_length=500)
    inbound_material_ids: tuple[UUID, ...] | None = Field(default=None, max_length=5)
    inbound_material_previews: tuple[InboundMaterialProposalPreview, ...] | None = Field(
        default=None, max_length=5
    )
    supersedes_proposal_id: UUID | None = None
    learn_source_id: str | None = Field(default=None, min_length=1, max_length=255)
    learn_source_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)
    learn_course_code: str | None = Field(default=None, min_length=1, max_length=80)
    learn_summary: str | None = Field(default=None, min_length=1, max_length=1_500)
    learn_source_url: str | None = Field(default=None, min_length=1, max_length=4_096)
    learn_date: date | datetime | None = None
    learn_ends_at: date | datetime | None = None
    learn_date_precision: Literal["date", "datetime"] | None = None
    learn_context: str | None = Field(default=None, min_length=1, max_length=10_000)
    target_page_id: str | None = Field(default=None, min_length=1, max_length=255)
    expected_due_value: date | datetime | None = None

    @field_validator("due_at", "ends_at", "expected_last_edited_at")
    @classmethod
    def optional_times_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value) if value is not None else None

    @field_validator("learn_date", "learn_ends_at", "expected_due_value")
    @classmethod
    def learn_values_are_valid(cls, value: date | datetime | None) -> date | datetime | None:
        if isinstance(value, datetime):
            return _aware(value)
        return value

    def model_post_init(self, __context: object) -> None:
        if self.field in {"create_learn_calendar_event", "enrich_learn_calendar_event"}:
            required = (
                self.learn_source_id,
                self.learn_source_fingerprint,
                self.learn_course_code,
                self.learn_summary,
                self.learn_source_url,
                self.learn_date,
                self.learn_date_precision,
                self.learn_context,
            )
            if any(value is None for value in required):
                raise ValueError("LEARN proposal is missing grounded source context")
            if self.learn_date_precision == "date" and isinstance(self.learn_date, datetime):
                raise ValueError("date-precision LEARN proposal must use a date value")
            if self.learn_date_precision == "datetime" and not isinstance(
                self.learn_date, datetime
            ):
                raise ValueError("datetime-precision LEARN proposal must use a datetime")
            if self.field == "create_learn_calendar_event" and self.title is None:
                raise ValueError("LEARN event creation requires a title")
            if self.field == "enrich_learn_calendar_event" and (
                self.assessment_id is None
                or self.course_id is None
                or self.target_page_id is None
                or self.expected_title is None
                or self.expected_last_edited_at is None
                or self.expected_due_value is None
            ):
                raise ValueError("LEARN enrichment requires guarded target preconditions")
            if self.learn_ends_at is not None:
                learn_start = self.learn_date
                if learn_start is None:
                    raise ValueError("LEARN proposal is missing its start value")
                if isinstance(learn_start, datetime) != isinstance(self.learn_ends_at, datetime):
                    raise ValueError("LEARN start and end precision must match")
                if self.learn_ends_at <= learn_start:
                    raise ValueError("LEARN end must be after start")
        if self.ends_at is None:
            return
        if self.due_at is None:
            raise ValueError("proposal end timestamp requires a start timestamp")
        duration = self.ends_at - self.due_at
        if duration <= timedelta(0):
            raise ValueError("proposal end timestamp must be after the start timestamp")


class AcademicCourseOption(PlannerModel):
    """Bounded course metadata returned by the read-only academic catalog."""

    course_id: str = Field(min_length=1, max_length=255)
    course_code: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=255)
    calendar_role: AcademicCalendarRole = AcademicCalendarRole.COURSE


class AcademicAssessmentOption(PlannerModel):
    """Bounded assessment metadata returned by the read-only academic catalog."""

    assessment_id: str = Field(min_length=1, max_length=255)
    course_id: str = Field(min_length=1, max_length=255)
    course_code: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=500)
    due_at: datetime | None = None
    ends_at: datetime | None = None
    due_at_local: str | None = Field(default=None, min_length=1, max_length=64)
    due_date_local: date | None = None
    is_all_day: bool = False
    assessment_type: AssessmentType
    expected_last_edited_at: datetime | None = None

    @field_validator("due_at", "ends_at", "expected_last_edited_at")
    @classmethod
    def catalog_times_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value) if value is not None else None

    def model_post_init(self, __context: object) -> None:
        if self.ends_at is not None:
            if self.due_at is None:
                raise ValueError("assessment end timestamp requires a start timestamp")
            if self.ends_at <= self.due_at:
                raise ValueError("assessment end timestamp must be after the start timestamp")


class AcademicCourseSearchResult(PlannerModel):
    results: tuple[AcademicCourseOption, ...] = Field(max_length=20)
    envelope: QueryEnvelope[AcademicCourseOption]


class AcademicAssessmentSearchResult(PlannerModel):
    results: tuple[AcademicAssessmentOption, ...] = Field(max_length=20)
    envelope: QueryEnvelope[AcademicAssessmentOption]


class AcademicCourseQueryArgs(PlannerModel):
    """Deterministic course lookup selected by the model and enforced by the host."""

    query: str = Field(default="", max_length=300)
    roles: tuple[AcademicCalendarRole, ...] = Field(default=(), max_length=3)
    limit: int = Field(default=10, ge=1, le=20)
    cursor: str | None = Field(default=None, min_length=1, max_length=2_000)


class AcademicAssessmentQueryArgs(PlannerModel):
    """Host-enforced deterministic read contract for assessment searches."""

    query: str = Field(default="", max_length=300)
    course_id: str | None = Field(default=None, min_length=1, max_length=255)
    roles: tuple[AcademicCalendarRole, ...] = Field(default=(), max_length=3)
    temporal: TemporalQuery = Field(default_factory=TemporalQuery)
    completion: CompletionMode = CompletionMode.INCOMPLETE
    limit: int = Field(default=10, ge=1, le=20)
    cursor: str | None = Field(default=None, min_length=1, max_length=2_000)


class SearchCoursesCall(PlannerModel):
    tool: Literal["search_courses"]
    query: str = Field(min_length=1, max_length=300)


class SearchAssessmentsCall(PlannerModel):
    tool: Literal["search_assessments"]
    query: str = Field(min_length=1, max_length=300)
    course_id: str | None = Field(default=None, min_length=1, max_length=255)


class CreateAssessmentCall(PlannerModel):
    tool: Literal["create_assessment"]
    course_id: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=500)
    due_at: datetime
    assessment_type: UserCreatableAssessmentType
    inbound_material_ids: tuple[UUID, ...] = Field(default=(), max_length=5)
    supersedes_proposal_id: UUID | None = None

    @field_validator("due_at")
    @classmethod
    def create_due_at_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class CreateMiscTaskCall(PlannerModel):
    """Host-bound creation call for the unique reserved misc calendar."""

    tool: Literal["create_misc_task"]
    course_id: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=500)
    due_at: datetime

    @field_validator("due_at")
    @classmethod
    def due_at_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class CreateCourseEventCall(PlannerModel):
    """Propose one ordinary course calendar event with a natural title."""

    tool: Literal["create_course_event"]
    course_id: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=500)
    starts_at: datetime
    duration_minutes: int = Field(ge=5, le=240)
    assessment_id: str | None = Field(default=None, min_length=1, max_length=255)
    requires_study_intent: bool = False

    @field_validator("starts_at")
    @classmethod
    def starts_at_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class AttachAssessmentMaterialCall(PlannerModel):
    """Propose attaching captured owner-scoped PDFs to one searched assessment."""

    tool: Literal["attach_material_to_assessment"]
    assessment_id: str = Field(min_length=1, max_length=255)
    inbound_material_ids: tuple[UUID, ...] = Field(min_length=1, max_length=5)


class UpdateAssessmentCall(PlannerModel):
    tool: Literal["update_assessment"]
    assessment_id: str = Field(min_length=1, max_length=255)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    due_at: datetime | None = None
    ends_at: datetime | None = None

    @field_validator("due_at", "ends_at")
    @classmethod
    def update_due_at_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value) if value is not None else None

    def model_post_init(self, __context: object) -> None:
        if self.title is None and self.due_at is None and self.ends_at is None:
            raise ValueError("update_assessment must include title or due_at")
        if self.ends_at is not None:
            if self.due_at is None:
                raise ValueError("update_assessment end timestamp requires due_at")
            if self.ends_at <= self.due_at:
                raise ValueError("update_assessment end timestamp must be after due_at")


class ArchiveAssessmentCall(PlannerModel):
    tool: Literal["archive_assessment"]
    assessment_id: str = Field(min_length=1, max_length=255)


class LearningFocusStatus(StrEnum):
    ACTIVE = "active"
    SNOOZED = "snoozed"


class AcademicLearningFocusOption(PlannerModel):
    """Bounded academic learning-focus metadata returned by the host store."""

    focus_id: str = Field(min_length=1, max_length=255)
    status: LearningFocusStatus
    topic: str = Field(min_length=1, max_length=300)
    course_id: str | None = Field(default=None, min_length=1, max_length=255)
    course_code: str | None = Field(default=None, min_length=1, max_length=80)
    assessment_id: str | None = Field(default=None, min_length=1, max_length=255)
    assessment_title: str | None = Field(default=None, min_length=1, max_length=500)
    target_minutes: int = Field(ge=5, le=240, default=30)
    next_review_at: datetime | None = None
    missed_checkin_count: int = Field(ge=0, le=5, default=0)
    snoozed_until: datetime | None = None
    revision: int = Field(ge=1, default=1)
    current_reflection_summary: str | None = Field(default=None, max_length=500)

    @field_validator("next_review_at", "snoozed_until")
    @classmethod
    def optional_times_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value) if value is not None else None


class AcademicMemorySummary(PlannerModel):
    """Grounded Discord summary over a bounded host-provided focus set."""

    summary_text: str = Field(min_length=1, max_length=1_800)
    covered_focus_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    memory_set_truncated: bool


class MemoryManagementOutcome(StrEnum):
    SUMMARIZE_MEMORY = "summarize_memory"
    DELETE_FOCUS = "delete_focus"
    REPLACE_FOCUS = "replace_focus"
    REINFORCE_FOCUS = "reinforce_focus"
    CLARIFY = "clarify"
    CANCEL = "cancel"
    NO_CHANGE = "no_change"


class AcademicMemoryReviewDecision(PlannerModel):
    """One bounded model decision; the host alone validates and mutates."""

    outcome: MemoryManagementOutcome
    focus_id: str | None = Field(default=None, min_length=1, max_length=255)
    subject_text: str | None = Field(default=None, min_length=1, max_length=500)
    replacement_topic: str | None = Field(default=None, min_length=1, max_length=300)
    replacement_course_code: str | None = Field(default=None, min_length=1, max_length=80)
    reason: str | None = Field(default=None, min_length=1, max_length=500)
    target_minutes: int | None = Field(default=None, ge=5, le=240)
    clarification_question: str | None = Field(default=None, min_length=1, max_length=1_000)


class AcademicSemanticCandidate(PlannerModel):
    """A bounded semantic match from raw reflections or learning-focus embeddings."""

    candidate_id: str = Field(min_length=1, max_length=255)
    source_kind: Literal["reflection", "learning_focus"]
    source_id: str = Field(min_length=1, max_length=255)
    text: str = Field(min_length=1, max_length=1_000)
    score: float = Field(ge=0, le=1)
    focus: AcademicLearningFocusOption | None = None


class AcademicLearningFocusSearchResult(PlannerModel):
    results: tuple[AcademicLearningFocusOption, ...] = Field(max_length=20)


class AcademicSemanticSearchResult(PlannerModel):
    results: tuple[AcademicSemanticCandidate, ...] = Field(max_length=20)


class SearchLearningFocusesCall(PlannerModel):
    tool: Literal["search_learning_focuses"]
    query: str | None = Field(default=None, min_length=1, max_length=300)
    statuses: tuple[LearningFocusStatus, ...] = (
        LearningFocusStatus.ACTIVE,
        LearningFocusStatus.SNOOZED,
    )

    def model_post_init(self, __context: object) -> None:
        if not self.statuses:
            raise ValueError("search_learning_focuses must include at least one status")


class SearchSemanticFocusesCall(PlannerModel):
    tool: Literal["search_semantic_focuses"]
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(ge=1, le=20, default=10)


class DiscourseIntent(StrEnum):
    CREATE_FOCUS = "create_focus"
    REINFORCE_FOCUS = "reinforce_focus"
    RESOLVE_FOCUS = "resolve_focus"
    SNOOZE_FOCUS = "snooze_focus"


class DiscoursePartialFacts(PlannerModel):
    intent: DiscourseIntent | None = None
    topic: str | None = Field(default=None, min_length=1, max_length=300)
    course_query: str | None = Field(default=None, min_length=1, max_length=300)
    assessment_query: str | None = Field(default=None, min_length=1, max_length=300)
    focus_query: str | None = Field(default=None, min_length=1, max_length=300)
    target_minutes: int | None = Field(default=None, ge=5, le=240)
    next_review_at: datetime | None = None
    evidence_text: str | None = Field(default=None, min_length=1, max_length=1_000)

    @field_validator("next_review_at")
    @classmethod
    def next_review_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value) if value is not None else None


class DiscourseClarification(PlannerModel):
    question: str = Field(min_length=1, max_length=1_000)
    partial_facts: DiscoursePartialFacts = Field(default_factory=DiscoursePartialFacts)


class CreateLearningFocusAction(PlannerModel):
    action: Literal["create_focus"]
    topic: str = Field(min_length=1, max_length=300)
    course_id: str | None = Field(default=None, min_length=1, max_length=255)
    assessment_id: str | None = Field(default=None, min_length=1, max_length=255)
    evidence_text: str = Field(min_length=1, max_length=1_000)
    target_minutes: int = Field(ge=5, le=240, default=30)
    next_review_at: datetime | None = None

    @field_validator("next_review_at")
    @classmethod
    def create_next_review_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value) if value is not None else None


class ReinforceLearningFocusAction(PlannerModel):
    action: Literal["reinforce_focus"]
    focus_id: str = Field(min_length=1, max_length=255)
    evidence_text: str = Field(min_length=1, max_length=1_000)
    target_minutes: int | None = Field(default=None, ge=5, le=240)
    next_review_at: datetime | None = None

    @field_validator("next_review_at")
    @classmethod
    def reinforce_next_review_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value) if value is not None else None


class ResolveLearningFocusAction(PlannerModel):
    action: Literal["resolve_focus"]
    focus_id: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=1, max_length=500)
    delete_focus: Literal[True] = True


class SnoozeLearningFocusAction(PlannerModel):
    action: Literal["snooze_focus"]
    focus_id: str = Field(min_length=1, max_length=255)
    snoozed_until: datetime
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("snoozed_until")
    @classmethod
    def snoozed_until_aware(cls, value: datetime) -> datetime:
        return _aware(value)


type AcademicDiscourseToolCall = Annotated[
    SearchCoursesCall
    | SearchAssessmentsCall
    | SearchLearningFocusesCall
    | SearchSemanticFocusesCall,
    Field(discriminator="tool"),
]


type AcademicDiscourseAction = Annotated[
    CreateLearningFocusAction
    | ReinforceLearningFocusAction
    | ResolveLearningFocusAction
    | SnoozeLearningFocusAction,
    Field(discriminator="action"),
]


class AcademicDiscourseDecision(PlannerModel):
    """One bounded model turn for academic semantic discourse."""

    tool_calls: tuple[AcademicDiscourseToolCall, ...] = Field(default=(), max_length=20)
    actions: tuple[AcademicDiscourseAction, ...] = Field(default=(), max_length=20)
    clarification: DiscourseClarification | None = None
    not_applicable: bool = False


class AcademicDiscourseContinuationState(PlannerModel):
    """Host-persisted state for multi-turn reflection clarification."""

    partial_facts: DiscoursePartialFacts = Field(default_factory=DiscoursePartialFacts)
    verified_courses: tuple[AcademicCourseOption, ...] = Field(default=(), max_length=20)
    verified_assessments: tuple[AcademicAssessmentOption, ...] = Field(default=(), max_length=20)
    verified_focuses: tuple[AcademicLearningFocusOption, ...] = Field(default=(), max_length=20)
    verified_semantic_candidates: tuple[AcademicSemanticCandidate, ...] = Field(
        default=(), max_length=20
    )
    prior_user_messages: tuple[str, ...] = Field(default=(), max_length=5)


class AcademicDiscourseLoopResult(PlannerModel):
    """Result of a semantic discourse loop without external writes."""

    actions: tuple[AcademicDiscourseAction, ...] = Field(max_length=20)
    clarification: DiscourseClarification | None = None
    continuation_state: AcademicDiscourseContinuationState | None = None
    applicable: bool = True
    turns: int = Field(ge=1, le=4)


class CheckinProposal(PlannerModel):
    proposal_id: UUID
    confirmation_event: str = Field(min_length=1, max_length=255)
    changes: tuple[ProposedChange, ...] = Field(max_length=20)
    expires_at: datetime | None = None
    question: str | None = Field(default=None, max_length=2_000)

    @field_validator("expires_at")
    @classmethod
    def expires_at_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value) if value is not None else None


class CheckinExtraction(PlannerModel):
    """Structured model output used only to propose, never apply, updates."""

    changes: tuple[ProposedChange, ...] = Field(max_length=20)


__all__ = [
    "AcademicAssessmentOption",
    "AcademicAssessmentSearchResult",
    "AcademicCourseOption",
    "AcademicCourseSearchResult",
    "AcademicDiscourseAction",
    "AcademicDiscourseContinuationState",
    "AcademicDiscourseDecision",
    "AcademicDiscourseLoopResult",
    "AcademicDiscourseToolCall",
    "AcademicLearningFocusOption",
    "AcademicLearningFocusSearchResult",
    "AcademicMemoryReviewDecision",
    "AcademicMemorySummary",
    "AcademicPerformanceSignal",
    "AcademicSemanticCandidate",
    "AcademicSemanticSearchResult",
    "ArchiveAssessmentCall",
    "AssessmentMaterialDiagnostic",
    "AssessmentMaterialExtractionStatus",
    "AssessmentMaterialSource",
    "AssessmentMaterialSourceKind",
    "AssessmentType",
    "AttachAssessmentMaterialCall",
    "AvailabilityWindow",
    "CalendarAvailabilityFacts",
    "CheckinExtraction",
    "CheckinProposal",
    "CreateAssessmentCall",
    "CreateCourseEventCall",
    "CreateLearningFocusAction",
    "CreateMiscTaskCall",
    "DiscourseClarification",
    "DiscourseIntent",
    "DiscoursePartialFacts",
    "FixedCommitment",
    "GroundedAssessmentInsight",
    "InboundMaterialProposalPreview",
    "LearningFocusStatus",
    "MemoryManagementOutcome",
    "MorningBriefing",
    "MorningBriefingAssessmentFact",
    "MorningBriefingContext",
    "MorningBriefingLearningFocusFact",
    "MorningBriefingMaterialInsightFact",
    "MorningBriefingPerformanceFact",
    "MorningBriefingWorkBreakdownFact",
    "ProposedChange",
    "ReinforceLearningFocusAction",
    "ResolveLearningFocusAction",
    "SearchAssessmentsCall",
    "SearchCoursesCall",
    "SearchLearningFocusesCall",
    "SearchSemanticFocusesCall",
    "SnoozeLearningFocusAction",
    "UpdateAssessmentCall",
    "UserCreatableAssessmentType",
    "ValidatedMaterialPlanningSignals",
]
