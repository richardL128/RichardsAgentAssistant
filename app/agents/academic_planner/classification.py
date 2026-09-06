"""Deterministic assessment label classification for Notion course calendars."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AssessmentKind(StrEnum):
    QUIZ = "quiz"
    ASSIGNMENT = "assignment"
    UNKNOWN = "unknown"


class LabelClassification(BaseModel):
    """Safe, deterministic classification result for one assessment label."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str = Field(max_length=500)
    kind: AssessmentKind
    source: Literal["trusted_existing", "label", "unknown"]
    matched_token: str | None = Field(default=None, max_length=40)


_ASSIGNMENT_CORRECTIONS: frozenset[str] = frozenset({"assigment"})
_TOKEN_PATTERN = re.compile(r"[A-Za-z]+")
_PREFIX_PATTERN = re.compile(r"^\s*(quiz|assignment)\s+[—-]\s*", re.IGNORECASE)


def classify_assessment_label(
    label: str,
    *,
    trusted_existing_kind: AssessmentKind | str | None = None,
) -> LabelClassification:
    """Classify only explicit quiz/assignment tokens and approved corrections."""

    trusted_kind = _coerce_trusted_kind(trusted_existing_kind)
    if trusted_kind is not None:
        return LabelClassification(
            label=label,
            kind=trusted_kind,
            source="trusted_existing",
            matched_token=trusted_kind.value,
        )

    tokens = tuple(match.group(0).lower() for match in _TOKEN_PATTERN.finditer(label))
    quiz_matches = [token for token in tokens if token == AssessmentKind.QUIZ.value]
    assignment_matches = [
        token
        for token in tokens
        if token == AssessmentKind.ASSIGNMENT.value or token in _ASSIGNMENT_CORRECTIONS
    ]
    if quiz_matches and not assignment_matches:
        return LabelClassification(
            label=label,
            kind=AssessmentKind.QUIZ,
            source="label",
            matched_token=quiz_matches[0],
        )
    if assignment_matches and not quiz_matches:
        matched = assignment_matches[0]
        return LabelClassification(
            label=label,
            kind=AssessmentKind.ASSIGNMENT,
            source="label",
            matched_token=matched,
        )
    return LabelClassification(label=label, kind=AssessmentKind.UNKNOWN, source="unknown")


def canonical_title_previews(label: str) -> dict[AssessmentKind, str]:
    """Return title-only rename previews for Quiz and Assignment choices."""

    body = _title_body(label)
    return {
        AssessmentKind.QUIZ: _bounded_title(f"Quiz — {body}"),
        AssessmentKind.ASSIGNMENT: _bounded_title(f"Assignment — {body}"),
    }


def canonical_assessment_title(kind: AssessmentKind | str, label: str) -> str:
    """Build the exact title for a confirmed Quiz or Assignment selection."""

    resolved = AssessmentKind(kind)
    if resolved not in {AssessmentKind.QUIZ, AssessmentKind.ASSIGNMENT}:
        raise ValueError("canonical title requires quiz or assignment")
    return canonical_title_previews(label)[resolved]


def _coerce_trusted_kind(value: AssessmentKind | str | None) -> AssessmentKind | None:
    if value is None:
        return None
    try:
        kind = AssessmentKind(value)
    except ValueError:
        return None
    if kind in {AssessmentKind.QUIZ, AssessmentKind.ASSIGNMENT}:
        return kind
    return None


def _title_body(label: str) -> str:
    without_prefix = _PREFIX_PATTERN.sub("", label, count=1).strip()
    return without_prefix or "Untitled assessment"


def _bounded_title(value: str) -> str:
    return value[:500]


__all__ = [
    "AssessmentKind",
    "LabelClassification",
    "canonical_assessment_title",
    "canonical_title_previews",
    "classify_assessment_label",
]
