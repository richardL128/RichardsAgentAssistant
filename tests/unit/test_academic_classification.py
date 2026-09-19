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
        "Quiz assignment packet",
        "tutorial lab packet",
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
        ("event", AssessmentKind.EVENT),
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


@pytest.mark.parametrize("trusted_existing_kind", ["Thing", "Midterm", "Final Exam"])
def test_classifier_ignores_unsupported_metadata_and_keeps_ordinary_event(
    trusted_existing_kind: str,
) -> None:
    result = classify_assessment_label(
        "homework",
        trusted_existing_kind=trusted_existing_kind,
    )

    assert result.kind is AssessmentKind.EVENT
    assert result.source == "label"


def test_canonical_title_previews_add_exactly_one_prefix() -> None:
    previews = canonical_title_previews("Chapter 4")
    already_quiz = canonical_title_previews("Quiz — Chapter 4")
    already_assignment = canonical_assessment_title(AssessmentKind.QUIZ, "Assignment - Lab 2")
    repeated_prefixes = canonical_assessment_title(
        AssessmentKind.QUIZ,
        "Lab — Assignment — Chapter 3",
    )

    assert previews[AssessmentKind.QUIZ] == "Quiz — Chapter 4"
    assert previews[AssessmentKind.ASSIGNMENT] == "Assignment — Chapter 4"
    assert previews[AssessmentKind.TUTORIAL] == "Tutorial — Chapter 4"
    assert previews[AssessmentKind.LAB] == "Lab — Chapter 4"
    assert previews[AssessmentKind.EVENT] == "Chapter 4"
    assert already_quiz[AssessmentKind.QUIZ] == "Quiz — Chapter 4"
    assert already_quiz[AssessmentKind.ASSIGNMENT] == "Assignment — Chapter 4"
    assert already_assignment == "Quiz — Lab 2"
    assert repeated_prefixes == "Quiz — Chapter 3"


@pytest.mark.parametrize(
    "label",
    [
        "Homework 2",
        "Research paper",
        "midterm test",
        "exam review",
        "Review latest lesson",
        "Quick look on Error propagation",
        "studying blocks",
        "prelab",
    ],
)
def test_classifier_treats_untyped_titles_as_ordinary_events_without_study_rules(
    label: str,
) -> None:
    result = classify_assessment_label(label)

    assert result.kind is AssessmentKind.EVENT
    assert result.source == "label"
    assert result.matched_token == "event"
