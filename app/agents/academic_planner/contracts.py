"""Typed facts and proposals for the deterministic academic planner."""

from __future__ import annotations

from datetime import UTC, datetime
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
    MIDTERM = "midterm"
    FINAL = "final"
    EVENT = "event"


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
    assessment_type: AssessmentType | None = None
    expected_last_edited_at: datetime | None = None
    expected_title: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("due_at", "expected_last_edited_at")
    @classmethod
    def optional_times_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value) if value is not None else None


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
    assessment_type: AssessmentType

    @field_validator("due_at")
    @classmethod
    def create_due_at_aware(cls, value: datetime) -> datetime:
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

    @field_validator("next_review_at", "snoozed_until")
    @classmethod
    def optional_times_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value) if value is not None else None


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
    | UpdateAssessmentCall
    | ArchiveAssessmentCall,
    Field(discriminator="tool"),
]


class AcademicAgentDecision(PlannerModel):
    """One bounded model turn for academic tool selection."""

    tool_calls: tuple[AcademicAgentToolCall, ...] = Field(default=(), max_length=20)
    question: str | None = Field(default=None, min_length=1, max_length=1_000)


class AcademicAgentLoopResult(PlannerModel):
    """Result of a Discord academic agent loop without external writes."""

    changes: tuple[ProposedChange, ...] = Field(max_length=20)
    question: str | None = Field(default=None, min_length=1, max_length=1_000)
    turns: int = Field(ge=1, le=4)


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
    "AcademicAgentDecision",
    "AcademicAgentLoopResult",
    "AcademicAgentToolCall",
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
    "AcademicSemanticCandidate",
    "AcademicSemanticSearchResult",
    "AmbiguousFact",
    "ArchiveAssessmentCall",
    "Assessment",
    "AssessmentType",
    "AvailabilityWindow",
    "CheckinExtraction",
    "CheckinProposal",
    "CreateAssessmentCall",
    "CreateLearningFocusAction",
    "DailyPlan",
    "DiscourseClarification",
    "DiscourseIntent",
    "DiscoursePartialFacts",
    "FixedCommitment",
    "IncompleteBlock",
    "LearningFocusStatus",
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
    "WorkBreakdown",
]
