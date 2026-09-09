"""Deterministic assessment label classification for Notion course calendars."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AssessmentKind(StrEnum):
    QUIZ = "quiz"
    ASSIGNMENT = "assignment"
    TUTORIAL = "tutorial"
    LAB = "lab"
    STUDYING_BLOCK = "studying_block"
    UNKNOWN = "unknown"


class LabelClassification(BaseModel):
    """Safe, deterministic classification result for one assessment label."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str = Field(max_length=500)
    kind: AssessmentKind
    source: Literal["trusted_existing", "label", "unknown"]
    matched_token: str | None = Field(default=None, max_length=40)


_SUPPORTED_KINDS: frozenset[AssessmentKind] = frozenset(
    {
        AssessmentKind.QUIZ,
        AssessmentKind.ASSIGNMENT,
        AssessmentKind.TUTORIAL,
        AssessmentKind.LAB,
        AssessmentKind.STUDYING_BLOCK,
    }
)
_CANONICAL_LABELS: dict[AssessmentKind, str] = {
    AssessmentKind.QUIZ: "Quiz",
    AssessmentKind.ASSIGNMENT: "Assignment",
    AssessmentKind.TUTORIAL: "Tutorial",
    AssessmentKind.LAB: "Lab",
    AssessmentKind.STUDYING_BLOCK: "Studying Block",
}
_SINGLE_TOKEN_KINDS: dict[str, AssessmentKind] = {
    AssessmentKind.QUIZ.value: AssessmentKind.QUIZ,
    AssessmentKind.ASSIGNMENT.value: AssessmentKind.ASSIGNMENT,
    AssessmentKind.TUTORIAL.value: AssessmentKind.TUTORIAL,
    AssessmentKind.LAB.value: AssessmentKind.LAB,
    "assigment": AssessmentKind.ASSIGNMENT,
}
_TRUSTED_KIND_ALIASES: dict[str, AssessmentKind] = {
    "quiz": AssessmentKind.QUIZ,
    "assignment": AssessmentKind.ASSIGNMENT,
    "tutorial": AssessmentKind.TUTORIAL,
    "lab": AssessmentKind.LAB,
    "studying_block": AssessmentKind.STUDYING_BLOCK,
}
_TOKEN_PATTERN = re.compile(r"[A-Za-z]+")
_PREFIX_PATTERN = re.compile(
    r"^\s*(quiz|assignment|tutorial|lab|studying\s+block)\s+[—-]\s*",
    re.IGNORECASE,
)


def classify_assessment_label(
    label: str,
    *,
    trusted_existing_kind: AssessmentKind | str | None = None,
) -> LabelClassification:
    """Classify only explicit supported todo labels and approved corrections."""

    trusted_kind = _coerce_trusted_kind(trusted_existing_kind)
    if trusted_kind is not None:
        return LabelClassification(
            label=label,
            kind=trusted_kind,
            source="trusted_existing",
            matched_token=trusted_kind.value,
        )

    matches = _label_matches(label)
    kinds = {kind for kind, _token in matches}
    if len(kinds) == 1:
        kind, matched_token = matches[0]
        return LabelClassification(
            label=label,
            kind=kind,
            source="label",
            matched_token=matched_token,
        )
    return LabelClassification(label=label, kind=AssessmentKind.UNKNOWN, source="unknown")


def matched_assessment_kinds(label: str) -> frozenset[AssessmentKind]:
    """Return the supported todo kinds explicitly present in free text."""

    return frozenset(kind for kind, _token in _label_matches(label))


def canonical_title_previews(label: str) -> dict[AssessmentKind, str]:
    """Return title-only rename previews for all supported todo choices."""

    body = _title_body(label)
    return {
        kind: _bounded_title(f"{display} — {body}") for kind, display in _CANONICAL_LABELS.items()
    }


def canonical_assessment_title(kind: AssessmentKind | str, label: str) -> str:
    """Build the exact title for a confirmed supported todo selection."""

    resolved = AssessmentKind(kind)
    if resolved not in _SUPPORTED_KINDS:
        raise ValueError("canonical title requires a supported todo kind")
    return canonical_title_previews(label)[resolved]


def _coerce_trusted_kind(value: AssessmentKind | str | None) -> AssessmentKind | None:
    if value is None:
        return None
    kind = _TRUSTED_KIND_ALIASES.get(_trusted_kind_key(str(value)))
    if kind in _SUPPORTED_KINDS:
        return kind
    return None


def _trusted_kind_key(value: str) -> str:
    return re.sub(r"\s+", "_", value.strip().casefold())


def _label_matches(label: str) -> tuple[tuple[AssessmentKind, str], ...]:
    tokens = tuple(match.group(0).lower() for match in _TOKEN_PATTERN.finditer(label))
    matches: list[tuple[AssessmentKind, str]] = []
    for index, word in enumerate(tokens):
        kind = _SINGLE_TOKEN_KINDS.get(word)
        if kind is not None:
            matches.append((kind, word))
        if word == "studying" and index + 1 < len(tokens) and tokens[index + 1] == "block":
            matches.append((AssessmentKind.STUDYING_BLOCK, "studying block"))
    return tuple(matches)


def _title_body(label: str) -> str:
    body = label.strip()
    while True:
        without_prefix = _PREFIX_PATTERN.sub("", body, count=1).strip()
        if without_prefix == body:
            return body or "Untitled assessment"
        body = without_prefix


def _bounded_title(value: str) -> str:
    return value[:500]


__all__ = [
    "AssessmentKind",
    "LabelClassification",
    "canonical_assessment_title",
    "canonical_title_previews",
    "classify_assessment_label",
    "matched_assessment_kinds",
]
