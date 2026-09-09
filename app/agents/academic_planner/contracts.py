"""Typed facts and proposals for the deterministic academic planner."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PlannerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


class AssessmentType(StrEnum):
    ASSIGNMENT = "assignment"
    QUIZ = "quiz"
    TUTORIAL = "tutorial"
    LAB = "lab"
    STUDYING_BLOCK = "studying_block"
    MIDTERM = "midterm"
    FINAL = "final"
    EVENT = "event"


class UserCreatableAssessmentType(StrEnum):
    ASSIGNMENT = "assignment"
    QUIZ = "quiz"
    TUTORIAL = "tutorial"
    LAB = "lab"
    STUDYING_BLOCK = "studying_block"


class Assessment(PlannerModel):
    id: str = Field(min_length=1, max_length=255)
    course: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=500)
    assessment_type: AssessmentType
    due_at: datetime
    estimated_minutes: int = Field(gt=0, le=10_080)
    weight_percent: float = Field(ge=0, le=100)
    course_priority: int = Field(ge=0, le=100, default=50)
    confidence_gap: float = Field(ge=0, le=1, default=0.5)
    scope_size: float = Field(ge=0, le=100, default=0)
    completed: bool = False
    ambiguous: bool = False
    citations: tuple[str, ...] = ()

    @field_validator("due_at")
    @classmethod
    def due_at_aware(cls, value: datetime) -> datetime:
        return _aware(value)


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


class IncompleteBlock(PlannerModel):
    id: str = Field(min_length=1, max_length=255)
    assessment_id: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=500)
    remaining_minutes: int = Field(gt=0, le=10_080)
    original_due_at: datetime | None = None

    @field_validator("original_due_at")
    @classmethod
    def due_at_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value) if value is not None else None


class AmbiguousFact(PlannerModel):
    id: str = Field(min_length=1, max_length=255)
    question: str = Field(min_length=1, max_length=1_000)
    source_citation: str = Field(min_length=1, max_length=500)
    candidate_value: str | None = Field(default=None, max_length=500)


class PlannerFacts(PlannerModel):
    assessments: tuple[Assessment, ...] = ()
    commitments: tuple[FixedCommitment, ...] = ()
    availability: tuple[AvailabilityWindow, ...] = ()
    incomplete_blocks: tuple[IncompleteBlock, ...] = ()
    practice_needs: tuple[PracticeNeed, ...] = ()
    performance_signals: tuple[AcademicPerformanceSignal, ...] = Field(default=(), max_length=20)
    ambiguous_facts: tuple[AmbiguousFact, ...] = ()
    buffer_minutes: int = Field(ge=0, le=240, default=15)
    horizon_days: int = Field(ge=7, le=14, default=7)


class WorkBreakdown(PlannerModel):
    assessment_id: str = Field(min_length=1, max_length=255)
    steps: tuple[str, ...] = Field(max_length=20)
    estimated_minutes: int = Field(gt=0, le=10_080)
    rationale: str = Field(min_length=1, max_length=2_000)


class PlanCritique(PlannerModel):
    acceptable: bool
    concerns: tuple[str, ...] = Field(max_length=20)


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


class MorningBriefingBlockFact(PlannerModel):
    block_id: str = Field(min_length=1, max_length=255)
    assessment_id: str = Field(min_length=1, max_length=255)
    learning_focus_id: str | None = Field(default=None, min_length=1, max_length=255)
    block_kind: Literal["assessment", "practice"]
    title: str = Field(min_length=1, max_length=500)
    start_at_local: str = Field(min_length=1, max_length=64)
    duration_minutes: int = Field(gt=0, le=10_080)
    carried_over: bool


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
    scheduled_blocks: tuple[MorningBriefingBlockFact, ...] = Field(default=(), max_length=100)
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
    deferred_practice_focus_ids: tuple[str, ...] = Field(default=(), max_length=20)


class MorningBriefing(PlannerModel):
    """One Discord-ready model-written message plus host-verifiable provenance."""

    message_text: str = Field(min_length=1, max_length=2_000)
    referenced_assessment_ids: tuple[str, ...] = Field(default=(), max_length=50)
    referenced_block_ids: tuple[str, ...] = Field(default=(), max_length=100)
    referenced_focus_ids: tuple[str, ...] = Field(default=(), max_length=20)
    referenced_performance_signal_ids: tuple[str, ...] = Field(default=(), max_length=20)
    referenced_insight_ids: tuple[str, ...] = Field(default=(), max_length=30)

    @field_validator(
        "referenced_assessment_ids",
        "referenced_block_ids",
        "referenced_focus_ids",
        "referenced_performance_signal_ids",
        "referenced_insight_ids",
    )
    @classmethod
    def reference_ids_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("briefing reference ids must be unique")
        return value


class StudyBlock(PlannerModel):
    id: str = Field(min_length=1, max_length=255)
    assessment_id: str = Field(min_length=1, max_length=255)
    learning_focus_id: str | None = Field(default=None, min_length=1, max_length=255)
    block_kind: Literal["assessment", "practice"] = "assessment"
    title: str = Field(min_length=1, max_length=500)
    start_at: datetime
    end_at: datetime
    carried_over: bool = False
    priority_score: float = Field(ge=0)
    rationale: str = Field(min_length=1, max_length=1_000)

    @field_validator("start_at", "end_at")
    @classmethod
    def times_aware(cls, value: datetime) -> datetime:
        return _aware(value)

    def model_post_init(self, __context: object) -> None:
        if self.end_at <= self.start_at:
            raise ValueError("study block must end after it starts")


class DailyPlan(PlannerModel):
    plan_id: UUID
    created_at: datetime
    blocks: tuple[StudyBlock, ...]
    deferred_assessment_ids: tuple[str, ...] = ()
    deferred_practice_focus_ids: tuple[str, ...] = ()
    ambiguous_questions: tuple[AmbiguousFact, ...] = ()
    critique: PlanCritique | None = None

    @field_validator("created_at")
    @classmethod
    def created_at_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class ProposedChange(PlannerModel):
    field: Literal[
        "completed",
        "actual_minutes",
        "new_task",
        "new_deadline",
        "new_event",
        "create_assessment",
        "update_assessment",
        "archive_assessment",
    ]
    value: str = Field(min_length=1, max_length=1_000)
    assessment_id: str | None = Field(default=None, max_length=255)
    course_id: str | None = Field(default=None, max_length=255)
    course_code: str | None = Field(default=None, min_length=1, max_length=80)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    due_at: datetime | None = None
    ends_at: datetime | None = None
    assessment_type: AssessmentType | None = None
    expected_last_edited_at: datetime | None = None
    expected_title: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("due_at", "ends_at", "expected_last_edited_at")
    @classmethod
    def optional_times_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value) if value is not None else None

    def model_post_init(self, __context: object) -> None:
        if self.ends_at is None:
            return
        if self.due_at is None:
            raise ValueError("proposal end timestamp requires a start timestamp")
        duration = self.ends_at - self.due_at
        if duration <= timedelta(0):
            raise ValueError("proposal end timestamp must be after the start timestamp")
        if self.assessment_type is AssessmentType.STUDYING_BLOCK and duration > timedelta(
            minutes=240
        ):
            raise ValueError("studying block duration must be at most 240 minutes")


class AcademicCourseOption(PlannerModel):
    """Bounded course metadata returned by the read-only academic catalog."""

    course_id: str = Field(min_length=1, max_length=255)
    course_code: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=255)


class AcademicAssessmentOption(PlannerModel):
    """Bounded assessment metadata returned by the read-only academic catalog."""

    assessment_id: str = Field(min_length=1, max_length=255)
    course_id: str = Field(min_length=1, max_length=255)
    course_code: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=500)
    due_at: datetime | None = None
    assessment_type: AssessmentType
    expected_last_edited_at: datetime | None = None

    @field_validator("due_at", "expected_last_edited_at")
    @classmethod
    def catalog_times_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value) if value is not None else None


class AcademicCourseSearchResult(PlannerModel):
    results: tuple[AcademicCourseOption, ...] = Field(max_length=20)


class AcademicAssessmentSearchResult(PlannerModel):
    results: tuple[AcademicAssessmentOption, ...] = Field(max_length=20)


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

    @field_validator("due_at")
    @classmethod
    def create_due_at_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class CreateStudySessionCall(PlannerModel):
    """Create one confirmed studying block with a host-derived end timestamp."""

    tool: Literal["create_study_session"]
    course_id: str = Field(min_length=1, max_length=255)
    topic: str = Field(min_length=1, max_length=300)
    starts_at: datetime
    duration_minutes: int = Field(ge=5, le=240)

    @field_validator("starts_at")
    @classmethod
    def starts_at_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class UpdateAssessmentCall(PlannerModel):
    tool: Literal["update_assessment"]
    assessment_id: str = Field(min_length=1, max_length=255)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    due_at: datetime | None = None

    @field_validator("due_at")
    @classmethod
    def update_due_at_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value) if value is not None else None

    def model_post_init(self, __context: object) -> None:
        if self.title is None and self.due_at is None:
            raise ValueError("update_assessment must include title or due_at")


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


class PracticeNeed(PlannerModel):
    """One separate daily practice block for an active focus before next_review_at."""

    topic: str = Field(min_length=1, max_length=300)
    target_minutes: int = Field(ge=5, le=240)
    next_review_at: datetime
    review_cadence: Literal["daily"] = "daily"
    source_action: Literal["create_focus", "reinforce_focus"]
    focus_id: str | None = Field(default=None, min_length=1, max_length=255)
    course_id: str | None = Field(default=None, min_length=1, max_length=255)
    course_code: str | None = Field(default=None, min_length=1, max_length=80)
    assessment_id: str | None = Field(default=None, min_length=1, max_length=255)
    assessment_title: str | None = Field(default=None, min_length=1, max_length=500)
    rationale: str = Field(min_length=1, max_length=1_000)

    @field_validator("next_review_at")
    @classmethod
    def practice_next_review_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class AcademicDiscourseLoopResult(PlannerModel):
    """Result of a semantic discourse loop without external writes."""

    actions: tuple[AcademicDiscourseAction, ...] = Field(max_length=20)
    practice_needs: tuple[PracticeNeed, ...] = Field(default=(), max_length=20)
    clarification: DiscourseClarification | None = None
    continuation_state: AcademicDiscourseContinuationState | None = None
    applicable: bool = True
    turns: int = Field(ge=1, le=4)


type AcademicAgentToolCall = Annotated[
    SearchCoursesCall
    | SearchAssessmentsCall
    | CreateAssessmentCall
    | CreateStudySessionCall
    | UpdateAssessmentCall
    | ArchiveAssessmentCall,
    Field(discriminator="tool"),
]


class AcademicRequestRouteDecision(PlannerModel):
    """Semantic split of one unsanitized academic message into independent domains."""

    calendar_request: str | None = Field(default=None, min_length=1, max_length=2_000)
    memory_request: str | None = Field(default=None, min_length=1, max_length=1_000)
    unrelated: bool = False

    def model_post_init(self, __context: object) -> None:
        has_request = self.calendar_request is not None or self.memory_request is not None
        if self.unrelated == has_request:
            raise ValueError("route must contain requests or be unrelated, but not both")


class AcademicAgentDecision(PlannerModel):
    """One bounded model turn for academic tool selection."""

    tool_calls: tuple[AcademicAgentToolCall, ...] = Field(default=(), max_length=20)
    question: str | None = Field(default=None, min_length=1, max_length=1_000)
    answer: str | None = Field(default=None, min_length=1, max_length=1_800)
    not_applicable: bool = False

    def model_post_init(self, __context: object) -> None:
        if self.not_applicable and (
            self.tool_calls or self.question is not None or self.answer is not None
        ):
            raise ValueError(
                "not_applicable cannot include event tools, an answer, or clarification"
            )
        selected_modes = (
            int(bool(self.tool_calls))
            + int(self.question is not None)
            + int(self.answer is not None)
            + int(self.not_applicable)
        )
        if selected_modes != 1:
            raise ValueError(
                "exactly one of tool_calls, question, answer, or not_applicable must be selected"
            )


class AcademicAgentWireDecision(PlannerModel):
    """Required-mode wire contract used for structured model decoding."""

    mode: Literal["tools", "answer", "clarification", "not_applicable"] = Field(
        description=(
            "Choose tools for a supported action or required lookup, answer for a read-only "
            "request grounded in prior tool results, clarification only when facts remain "
            "ambiguous after required lookups, or not_applicable for a different domain."
        )
    )
    tool_calls: tuple[AcademicAgentToolCall, ...] = Field(default=(), max_length=20)
    question: str | None = Field(default=None, min_length=1, max_length=1_000)
    answer: str | None = Field(default=None, min_length=1, max_length=1_800)

    def model_post_init(self, __context: object) -> None:
        if self.mode == "tools" and (
            not self.tool_calls or self.question is not None or self.answer is not None
        ):
            raise ValueError("tools mode requires tool calls and no answer or clarification")
        if self.mode == "answer" and (
            self.tool_calls or self.question is not None or self.answer is None
        ):
            raise ValueError("answer mode requires one answer and no tools or clarification")
        if self.mode == "clarification" and (
            self.tool_calls or self.question is None or self.answer is not None
        ):
            raise ValueError("clarification mode requires one question and no tools or answer")
        if self.mode == "not_applicable" and (
            self.tool_calls or self.question is not None or self.answer is not None
        ):
            raise ValueError(
                "not_applicable mode cannot include tools, an answer, or clarification"
            )

    def to_decision(self) -> AcademicAgentDecision:
        return AcademicAgentDecision(
            tool_calls=self.tool_calls,
            question=self.question,
            answer=self.answer,
            not_applicable=self.mode == "not_applicable",
        )


class AcademicAgentProgressPhase(StrEnum):
    RUNTIME_WAKING = "runtime_waking"
    MODEL_TURN = "model_turn"
    COURSE_LOOKUP = "course_lookup"
    ASSESSMENT_LOOKUP = "assessment_lookup"
    PROPOSAL_VALIDATION = "proposal_validation"
    PROPOSAL_READY = "proposal_ready"
    COMPLETED = "completed"
    CLARIFICATION_NEEDED = "clarification_needed"
    FAILED = "failed"


class AcademicAgentLookupKind(StrEnum):
    COURSE = "course"
    ASSESSMENT = "assessment"


class AcademicAgentLoopOutcome(StrEnum):
    PROPOSAL_READY = "proposal_ready"
    ANSWER_READY = "answer_ready"
    CLARIFICATION_REQUIRED = "clarification_required"
    NOT_APPLICABLE = "not_applicable"
    MODEL_INVALID_OUTPUT = "model_invalid_output"
    MODEL_FAILED = "model_failed"
    MODEL_TIMEOUT = "model_timeout"
    AGENT_TURN_LIMIT_EXHAUSTED = "agent_turn_limit_exhausted"
    HOST_VALIDATION_FAILED = "host_validation_failed"


class AcademicAgentProgressEvent(PlannerModel):
    """Host-observed progress metadata safe for user-visible rendering."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    phase: AcademicAgentProgressPhase
    attempt_number: int = Field(ge=1, le=3)
    attempt_limit: int = Field(ge=1, le=3)
    model_turn: int | None = Field(default=None, ge=1, le=10)
    model_turn_limit: int | None = Field(default=None, ge=1, le=10)
    lookup_kind: AcademicAgentLookupKind | None = None
    result_count: int | None = Field(default=None, ge=0, le=20)
    terminal: bool = False

    def model_post_init(self, __context: object) -> None:
        if self.attempt_number > self.attempt_limit:
            raise ValueError("attempt_number must be less than or equal to attempt_limit")
        if (self.model_turn is None) != (self.model_turn_limit is None):
            raise ValueError("model turn metadata must be provided together")
        if (
            self.model_turn is not None
            and self.model_turn_limit is not None
            and self.model_turn > self.model_turn_limit
        ):
            raise ValueError("model_turn must be less than or equal to model_turn_limit")
        if self.result_count is not None and self.lookup_kind is None:
            raise ValueError("result_count requires lookup_kind")


class AcademicAgentContinuationInput(PlannerModel):
    """Structured untrusted user context for clarification reruns."""

    original_user_request: str = Field(min_length=1, max_length=4_000)
    prior_clarification_questions: tuple[str, ...] = Field(default=(), max_length=2)
    clarification_answers: tuple[str, ...] = Field(default=(), max_length=2)

    def model_post_init(self, __context: object) -> None:
        if len(self.clarification_answers) > len(self.prior_clarification_questions):
            raise ValueError("answers cannot outnumber clarification questions")


class AcademicAgentLoopResult(PlannerModel):
    """Result of a Discord academic agent loop without external writes."""

    changes: tuple[ProposedChange, ...] = Field(max_length=20)
    question: str | None = Field(default=None, min_length=1, max_length=1_000)
    response: str | None = Field(default=None, min_length=1, max_length=1_800)
    turns: int = Field(ge=1, le=10)
    outcome: AcademicAgentLoopOutcome = AcademicAgentLoopOutcome.CLARIFICATION_REQUIRED


class CheckinProposal(PlannerModel):
    proposal_id: UUID
    confirmation_event: str = Field(min_length=1, max_length=255)
    changes: tuple[ProposedChange, ...] = Field(max_length=20)
    source_plan_id: UUID | None = None
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
    "AcademicAgentContinuationInput",
    "AcademicAgentDecision",
    "AcademicAgentLookupKind",
    "AcademicAgentLoopOutcome",
    "AcademicAgentLoopResult",
    "AcademicAgentProgressEvent",
    "AcademicAgentProgressPhase",
    "AcademicAgentToolCall",
    "AcademicAgentWireDecision",
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
    "AcademicRequestRouteDecision",
    "AcademicSemanticCandidate",
    "AcademicSemanticSearchResult",
    "AmbiguousFact",
    "ArchiveAssessmentCall",
    "Assessment",
    "AssessmentMaterialDiagnostic",
    "AssessmentMaterialExtractionStatus",
    "AssessmentMaterialSource",
    "AssessmentMaterialSourceKind",
    "AssessmentType",
    "AvailabilityWindow",
    "CheckinExtraction",
    "CheckinProposal",
    "CreateAssessmentCall",
    "CreateLearningFocusAction",
    "CreateStudySessionCall",
    "DailyPlan",
    "DiscourseClarification",
    "DiscourseIntent",
    "DiscoursePartialFacts",
    "FixedCommitment",
    "GroundedAssessmentInsight",
    "IncompleteBlock",
    "LearningFocusStatus",
    "MemoryManagementOutcome",
    "MorningBriefing",
    "MorningBriefingAssessmentFact",
    "MorningBriefingBlockFact",
    "MorningBriefingContext",
    "MorningBriefingLearningFocusFact",
    "MorningBriefingMaterialInsightFact",
    "MorningBriefingPerformanceFact",
    "MorningBriefingWorkBreakdownFact",
    "PlanCritique",
    "PlannerFacts",
    "PracticeNeed",
    "ProposedChange",
    "ReinforceLearningFocusAction",
    "ResolveLearningFocusAction",
    "SearchAssessmentsCall",
    "SearchCoursesCall",
    "SearchLearningFocusesCall",
    "SearchSemanticFocusesCall",
    "SnoozeLearningFocusAction",
    "StudyBlock",
    "UpdateAssessmentCall",
    "UserCreatableAssessmentType",
    "WorkBreakdown",
]
