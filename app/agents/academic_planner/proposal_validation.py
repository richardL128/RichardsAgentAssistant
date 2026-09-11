"""Mandatory application validation for model-proposed academic mutations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

from app.agents.academic_planner.classification import (
    AssessmentKind,
    canonical_assessment_title,
)
from app.agents.academic_planner.contracts import (
    AcademicAssessmentOption,
    AcademicCourseOption,
    ArchiveAssessmentCall,
    AssessmentType,
    CreateAssessmentCall,
    CreateStudySessionCall,
    ProposedChange,
    UpdateAssessmentCall,
)

MAX_PROPOSED_MUTATIONS = 20

_TOO_MANY_CHANGES = "That request includes too many changes; please split it up."
_CREATE_COURSE_UNVERIFIED = "I could not verify the course for a requested new assessment."
_CREATE_DUE_FUTURE_REQUIRED = "New assessments must be due in the future."
_ASSESSMENT_UNVERIFIED = "I could not verify the assessment id for a requested change."
_ASSESSMENT_METADATA_MISSING = "That assessment is missing the metadata required for a safe change."
_NO_SUPPORTED_CHANGE = "I need at least one supported academic change before creating a proposal."
_STUDY_COURSE_UNVERIFIED = "I could not verify the course for a requested study session."
_STUDY_ONE_COURSE_REQUIRED = "A study-session request must resolve to exactly one course."
_STUDY_START_FUTURE_REQUIRED = "Study sessions must start in the future."
_STUDY_DURATION_INVALID = "Study sessions must be between 5 and 240 minutes."
_STUDY_DUPLICATE = "Duplicate study-session blocks were not accepted."
_STUDY_ORDER_INVALID = "Study-session blocks must be ordered and must not overlap."

HOST_VALIDATION_ERRORS = frozenset(
    {
        _TOO_MANY_CHANGES,
        _CREATE_COURSE_UNVERIFIED,
        _CREATE_DUE_FUTURE_REQUIRED,
        _ASSESSMENT_UNVERIFIED,
        _ASSESSMENT_METADATA_MISSING,
        _STUDY_COURSE_UNVERIFIED,
        _STUDY_ONE_COURSE_REQUIRED,
        _STUDY_START_FUTURE_REQUIRED,
        _STUDY_DURATION_INVALID,
        _STUDY_DUPLICATE,
        _STUDY_ORDER_INVALID,
    }
)

MutationCall = (
    CreateAssessmentCall | CreateStudySessionCall | UpdateAssessmentCall | ArchiveAssessmentCall
)


def proposed_changes_from_calls(
    calls: Sequence[MutationCall],
    *,
    known_courses: Mapping[str, AcademicCourseOption],
    known_assessments: Mapping[str, AcademicAssessmentOption],
    now: datetime,
) -> tuple[tuple[ProposedChange, ...], str | None]:
    """Validate model-selected Notion calls at the mandatory application boundary."""

    if len(calls) > MAX_PROPOSED_MUTATIONS:
        return (), _TOO_MANY_CHANGES

    study_calls = tuple(call for call in calls if isinstance(call, CreateStudySessionCall))
    if study_calls:
        failed = _validate_study_session_calls(
            study_calls,
            known_courses=known_courses,
            now=now,
        )
        if failed is not None:
            return (), failed

    changes: list[ProposedChange] = []
    for call in calls:
        if isinstance(call, CreateStudySessionCall):
            course = known_courses[call.course_id]
            title = canonical_assessment_title(AssessmentKind.STUDYING_BLOCK, call.topic)
            starts_at = call.starts_at
            changes.append(
                ProposedChange(
                    field="create_assessment",
                    value=title,
                    course_id=call.course_id,
                    course_code=course.course_code,
                    title=title,
                    due_at=starts_at,
                    ends_at=starts_at + timedelta(minutes=call.duration_minutes),
                    assessment_type=AssessmentType.STUDYING_BLOCK,
                )
            )
            continue

        if isinstance(call, CreateAssessmentCall):
            if call.course_id not in known_courses:
                return (), _CREATE_COURSE_UNVERIFIED
            if call.due_at <= now:
                return (), _CREATE_DUE_FUTURE_REQUIRED
            title = canonical_assessment_title(call.assessment_type.value, call.title)
            changes.append(
                ProposedChange(
                    field="create_assessment",
                    value=title,
                    course_id=call.course_id,
                    course_code=known_courses[call.course_id].course_code,
                    title=title,
                    due_at=call.due_at,
                    assessment_type=AssessmentType(call.assessment_type.value),
                )
            )
            continue

        if call.assessment_id not in known_assessments:
            return (), _ASSESSMENT_UNVERIFIED
        existing = known_assessments[call.assessment_id]
        if existing.expected_last_edited_at is None:
            return (), _ASSESSMENT_METADATA_MISSING
        if isinstance(call, UpdateAssessmentCall):
            changes.append(
                ProposedChange(
                    field="update_assessment",
                    value="update_assessment",
                    assessment_id=call.assessment_id,
                    title=call.title,
                    due_at=call.due_at,
                    expected_title=existing.title,
                    expected_last_edited_at=existing.expected_last_edited_at,
                )
            )
            continue

        changes.append(
            ProposedChange(
                field="archive_assessment",
                value="archive_assessment",
                assessment_id=call.assessment_id,
                expected_title=existing.title,
                expected_last_edited_at=existing.expected_last_edited_at,
            )
        )

    if not changes:
        return (), _NO_SUPPORTED_CHANGE
    return tuple(changes), None


def _validate_study_session_calls(
    calls: Sequence[CreateStudySessionCall],
    *,
    known_courses: Mapping[str, AcademicCourseOption],
    now: datetime,
) -> str | None:
    if any(call.course_id not in known_courses for call in calls):
        return _STUDY_COURSE_UNVERIFIED
    if len({call.course_id for call in calls}) != 1:
        return _STUDY_ONE_COURSE_REQUIRED

    seen: set[tuple[str, datetime]] = set()
    for call in calls:
        if call.starts_at <= now:
            return _STUDY_START_FUTURE_REQUIRED
        if call.duration_minutes < 5 or call.duration_minutes > 240:
            return _STUDY_DURATION_INVALID
        key = (" ".join(call.topic.casefold().split()), call.starts_at)
        if key in seen:
            return _STUDY_DUPLICATE
        seen.add(key)

    previous_end: datetime | None = None
    for call in calls:
        if previous_end is not None and call.starts_at < previous_end:
            return _STUDY_ORDER_INVALID
        previous_end = call.starts_at + timedelta(minutes=call.duration_minutes)
    return None


__all__ = [
    "HOST_VALIDATION_ERRORS",
    "MAX_PROPOSED_MUTATIONS",
    "MutationCall",
    "proposed_changes_from_calls",
]
