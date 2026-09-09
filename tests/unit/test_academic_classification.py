from __future__ import annotations

import pytest

from app.agents.academic_planner.classification import (
    AssessmentKind,
    canonical_assessment_title,
    canonical_title_previews,
    classify_assessment_label,
)


@pytest.mark.parametrize(
    ("label", "kind", "matched"),
    [
        ("Chapter 4 Quiz", AssessmentKind.QUIZ, "quiz"),
        ("QUIZ: derivatives", AssessmentKind.QUIZ, "quiz"),
        ("Assignment 2", AssessmentKind.ASSIGNMENT, "assignment"),
        ("assigment 2", AssessmentKind.ASSIGNMENT, "assigment"),
        ("Tutorial #3", AssessmentKind.TUTORIAL, "tutorial"),
        ("LAB: op amps", AssessmentKind.LAB, "lab"),
        ("studying block for circuits", AssessmentKind.STUDYING_BLOCK, "studying block"),
        ("Studying-Block: Fourier", AssessmentKind.STUDYING_BLOCK, "studying block"),
    ],
)
def test_classifier_recognizes_explicit_tokens_phrase_and_small_correction(
    label: str,
    kind: AssessmentKind,
    matched: str,
) -> None:
    result = classify_assessment_label(label)

    assert result.kind is kind
    assert result.source == "label"
    assert result.matched_token == matched


@pytest.mark.parametrize(
    "label",
    [
        "Homework 2",
        "Research paper",
        "midterm test",
        "exam review",
        "Quiz assignment packet",
        "tutorial lab packet",
        "studying blocks",
        "prelab",
    ],
)
def test_classifier_leaves_unclear_or_conflicting_labels_unknown(label: str) -> None:
    result = classify_assessment_label(label)

    assert result.kind is AssessmentKind.UNKNOWN
    assert result.source == "unknown"
    assert result.matched_token is None


@pytest.mark.parametrize(
    ("trusted_existing_kind", "expected"),
    [
        (AssessmentKind.ASSIGNMENT, AssessmentKind.ASSIGNMENT),
        ("quiz", AssessmentKind.QUIZ),
        ("Tutorial", AssessmentKind.TUTORIAL),
        ("LAB", AssessmentKind.LAB),
        ("Studying Block", AssessmentKind.STUDYING_BLOCK),
        ("studying_block", AssessmentKind.STUDYING_BLOCK),
    ],
)
def test_classifier_preserves_trusted_existing_supported_types(
    trusted_existing_kind: AssessmentKind | str,
    expected: AssessmentKind,
) -> None:
    result = classify_assessment_label(
        "conflicting quiz wording in title",
        trusted_existing_kind=trusted_existing_kind,
    )

    assert result.kind is expected
    assert result.source == "trusted_existing"
    assert result.matched_token == expected.value


@pytest.mark.parametrize("trusted_existing_kind", ["event", "Thing", "Midterm", "Final Exam"])
def test_classifier_leaves_unsupported_trusted_existing_metadata_unknown(
    trusted_existing_kind: str,
) -> None:
    result = classify_assessment_label(
        "homework",
        trusted_existing_kind=trusted_existing_kind,
    )

    assert result.kind is AssessmentKind.UNKNOWN
    assert result.source == "unknown"


def test_canonical_title_previews_add_exactly_one_prefix() -> None:
    previews = canonical_title_previews("Chapter 4")
    already_quiz = canonical_title_previews("Quiz — Chapter 4")
    already_assignment = canonical_assessment_title(AssessmentKind.QUIZ, "Assignment - Lab 2")
    already_studying = canonical_assessment_title(
        AssessmentKind.LAB,
        "Studying Block - Fourier review",
    )
    repeated_prefixes = canonical_assessment_title(
        AssessmentKind.QUIZ,
        "Lab — Assignment — Chapter 3",
    )

    assert previews[AssessmentKind.QUIZ] == "Quiz — Chapter 4"
    assert previews[AssessmentKind.ASSIGNMENT] == "Assignment — Chapter 4"
    assert previews[AssessmentKind.TUTORIAL] == "Tutorial — Chapter 4"
    assert previews[AssessmentKind.LAB] == "Lab — Chapter 4"
    assert previews[AssessmentKind.STUDYING_BLOCK] == "Studying Block — Chapter 4"
    assert already_quiz[AssessmentKind.QUIZ] == "Quiz — Chapter 4"
    assert already_quiz[AssessmentKind.ASSIGNMENT] == "Assignment — Chapter 4"
    assert already_assignment == "Quiz — Lab 2"
    assert already_studying == "Lab — Fourier review"
    assert repeated_prefixes == "Quiz — Chapter 3"
