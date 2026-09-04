"""Typed facts and proposals for the deterministic academic planner."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
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
    ambiguous_questions: tuple[AmbiguousFact, ...] = ()
    critique: PlanCritique | None = None

    @field_validator("created_at")
    @classmethod
    def created_at_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class ProposedChange(PlannerModel):
    field: Literal["completed", "actual_minutes", "new_task", "new_deadline", "new_event"]
    value: str = Field(min_length=1, max_length=1_000)
    assessment_id: str | None = Field(default=None, max_length=255)


class CheckinProposal(PlannerModel):
    proposal_id: UUID
    confirmation_event: str = Field(min_length=1, max_length=255)
    changes: tuple[ProposedChange, ...] = Field(max_length=20)
    source_plan_id: UUID | None = None
    question: str | None = Field(default=None, max_length=2_000)


class CheckinExtraction(PlannerModel):
    """Structured model output used only to propose, never apply, updates."""

    changes: tuple[ProposedChange, ...] = Field(max_length=20)


__all__ = [
    "AmbiguousFact",
    "Assessment",
    "AssessmentType",
    "AvailabilityWindow",
    "CheckinExtraction",
    "CheckinProposal",
    "DailyPlan",
    "FixedCommitment",
    "IncompleteBlock",
    "PlanCritique",
    "PlannerFacts",
    "ProposedChange",
    "StudyBlock",
    "WorkBreakdown",
]
