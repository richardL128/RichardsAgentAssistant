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
    ],
)
def test_classifier_recognizes_explicit_tokens_and_small_correction(
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
    ],
)
def test_classifier_leaves_unclear_or_conflicting_labels_unknown(label: str) -> None:
    result = classify_assessment_label(label)

    assert result.kind is AssessmentKind.UNKNOWN
    assert result.source == "unknown"
    assert result.matched_token is None


def test_classifier_preserves_trusted_existing_quiz_or_assignment() -> None:
    assignment = classify_assessment_label(
        "quiz wording in title",
        trusted_existing_kind=AssessmentKind.ASSIGNMENT,
    )
    quiz = classify_assessment_label("homework", trusted_existing_kind="quiz")
    unknown = classify_assessment_label("homework", trusted_existing_kind="event")

    assert assignment.kind is AssessmentKind.ASSIGNMENT
    assert assignment.source == "trusted_existing"
    assert quiz.kind is AssessmentKind.QUIZ
    assert quiz.source == "trusted_existing"
    assert unknown.kind is AssessmentKind.UNKNOWN


def test_canonical_title_previews_add_exactly_one_prefix() -> None:
    previews = canonical_title_previews("Chapter 4")
    already_quiz = canonical_title_previews("Quiz — Chapter 4")
    already_assignment = canonical_assessment_title(AssessmentKind.QUIZ, "Assignment - Lab 2")

    assert previews[AssessmentKind.QUIZ] == "Quiz — Chapter 4"
    assert previews[AssessmentKind.ASSIGNMENT] == "Assignment — Chapter 4"
    assert already_quiz[AssessmentKind.QUIZ] == "Quiz — Chapter 4"
    assert already_quiz[AssessmentKind.ASSIGNMENT] == "Assignment — Chapter 4"
    assert already_assignment == "Quiz — Lab 2"
