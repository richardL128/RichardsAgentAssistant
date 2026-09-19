"""Mandatory application validation for model-proposed academic mutations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from uuid import UUID

from app.agents.academic_planner.calendar_roles import (
    AcademicCalendarRole,
    canonical_misc_task_title,
)
from app.agents.academic_planner.classification import canonical_assessment_title
from app.agents.academic_planner.contracts import (
    AcademicAssessmentOption,
    AcademicCourseOption,
    ArchiveAssessmentCall,
    AssessmentType,
    AttachAssessmentMaterialCall,
    CreateAssessmentCall,
    CreateCourseEventCall,
    CreateMiscTaskCall,
    InboundMaterialProposalPreview,
    ProposedChange,
    UpdateAssessmentCall,
)

MAX_PROPOSED_MUTATIONS = 20

_TOO_MANY_CHANGES = "That request includes too many changes; please split it up."
_CREATE_COURSE_UNVERIFIED = "I could not verify the course for a requested new assessment."
_CREATE_DUE_FUTURE_REQUIRED = "New assessments must be due in the future."
_MISC_TARGET_UNVERIFIED = "I could not verify the reserved misc calendar for this task."
_MISC_DUE_FUTURE_REQUIRED = "New miscellaneous tasks must be due in the future."
_ASSESSMENT_UNVERIFIED = "I could not verify the assessment id for a requested change."
_ASSESSMENT_METADATA_MISSING = "That assessment is missing the metadata required for a safe change."
_NO_SUPPORTED_CHANGE = "I need at least one supported academic change before creating a proposal."
_EVENT_COURSE_UNVERIFIED = "I could not verify the course for a requested calendar event."
_EVENT_START_FUTURE_REQUIRED = "Course events must start in the future."
_EVENT_DURATION_INVALID = "Course events must be between 5 and 240 minutes."
_EVENT_DUPLICATE = "Duplicate course events were not accepted."
_EVENT_ORDER_INVALID = "Course events must be ordered and must not overlap."
_MATERIAL_UNVERIFIED = "I could not verify every captured PDF for this proposal."
_MATERIAL_DUPLICATE = "Duplicate PDFs were not accepted in one proposal."

HOST_VALIDATION_ERRORS = frozenset(
    {
        _TOO_MANY_CHANGES,
        _CREATE_COURSE_UNVERIFIED,
        _CREATE_DUE_FUTURE_REQUIRED,
        _MISC_TARGET_UNVERIFIED,
        _MISC_DUE_FUTURE_REQUIRED,
        _ASSESSMENT_UNVERIFIED,
        _ASSESSMENT_METADATA_MISSING,
        _EVENT_COURSE_UNVERIFIED,
        _EVENT_START_FUTURE_REQUIRED,
        _EVENT_DURATION_INVALID,
        _EVENT_DUPLICATE,
        _EVENT_ORDER_INVALID,
        _MATERIAL_UNVERIFIED,
        _MATERIAL_DUPLICATE,
    }
)

MutationCall = (
    CreateAssessmentCall
    | CreateMiscTaskCall
    | CreateCourseEventCall
    | UpdateAssessmentCall
    | ArchiveAssessmentCall
    | AttachAssessmentMaterialCall
)


def proposed_changes_from_calls(
    calls: Sequence[MutationCall],
    *,
    known_courses: Mapping[str, AcademicCourseOption],
    known_assessments: Mapping[str, AcademicAssessmentOption],
    now: datetime,
    valid_inbound_material_ids: set[UUID] | frozenset[UUID] | None = None,
    inbound_material_previews: Mapping[UUID, InboundMaterialProposalPreview] | None = None,
) -> tuple[tuple[ProposedChange, ...], str | None]:
    """Validate model-selected Notion calls at the mandatory application boundary."""

    if len(calls) > MAX_PROPOSED_MUTATIONS:
        return (), _TOO_MANY_CHANGES

    event_calls = tuple(call for call in calls if isinstance(call, CreateCourseEventCall))
    if event_calls:
        failed = _validate_course_event_calls(
            event_calls,
            known_courses=known_courses,
            now=now,
        )
        if failed is not None:
            return (), failed

    changes: list[ProposedChange] = []
    for call in calls:
        material_ids = tuple(getattr(call, "inbound_material_ids", ()))
        if len(material_ids) != len(set(material_ids)):
            return (), _MATERIAL_DUPLICATE
        if material_ids and (
            valid_inbound_material_ids is None
            or not set(material_ids).issubset(valid_inbound_material_ids)
        ):
            return (), _MATERIAL_UNVERIFIED
        previews = tuple(
            inbound_material_previews[material_id]
            for material_id in material_ids
            if inbound_material_previews is not None and material_id in inbound_material_previews
        )
        if material_ids and len(previews) != len(material_ids):
            return (), _MATERIAL_UNVERIFIED

        if isinstance(call, CreateCourseEventCall):
            course = known_courses[call.course_id]
            if call.assessment_id is not None:
                assessment = known_assessments.get(call.assessment_id)
                if assessment is None or assessment.course_id != call.course_id:
                    return (), _ASSESSMENT_UNVERIFIED
            title = " ".join(call.title.split())
            starts_at = call.starts_at
            changes.append(
                ProposedChange(
                    field="create_assessment",
                    value=title,
                    course_id=call.course_id,
                    assessment_id=call.assessment_id,
                    course_code=course.course_code,
                    title=title,
                    due_at=starts_at,
                    ends_at=starts_at + timedelta(minutes=call.duration_minutes),
                    assessment_type=AssessmentType.EVENT,
                )
            )
            continue

        if isinstance(call, CreateMiscTaskCall):
            course = known_courses.get(call.course_id)
            if course is None or course.calendar_role is not AcademicCalendarRole.MISC:
                return (), _MISC_TARGET_UNVERIFIED
            if call.due_at <= now:
                return (), _MISC_DUE_FUTURE_REQUIRED
            title = canonical_misc_task_title(call.title)
            changes.append(
                ProposedChange(
                    field="create_assessment",
                    value=title,
                    course_id=call.course_id,
                    course_code=course.course_code,
                    title=title,
                    due_at=call.due_at,
                    assessment_type=AssessmentType.TASK,
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
                    inbound_material_ids=call.inbound_material_ids or None,
                    inbound_material_previews=previews or None,
                    supersedes_proposal_id=call.supersedes_proposal_id,
                )
            )
            continue

        if isinstance(call, AttachAssessmentMaterialCall):
            if call.assessment_id not in known_assessments:
                return (), _ASSESSMENT_UNVERIFIED
            existing = known_assessments[call.assessment_id]
            if existing.expected_last_edited_at is None:
                return (), _ASSESSMENT_METADATA_MISSING
            changes.append(
                ProposedChange(
                    field="attach_assessment_material",
                    value="attach_assessment_material",
                    assessment_id=call.assessment_id,
                    course_id=existing.course_id,
                    course_code=existing.course_code,
                    expected_title=existing.title,
                    expected_last_edited_at=existing.expected_last_edited_at,
                    inbound_material_ids=call.inbound_material_ids,
                    inbound_material_previews=previews or None,
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


def _validate_course_event_calls(
    calls: Sequence[CreateCourseEventCall],
    *,
    known_courses: Mapping[str, AcademicCourseOption],
    now: datetime,
) -> str | None:
    if any(call.course_id not in known_courses for call in calls):
        return _EVENT_COURSE_UNVERIFIED

    seen: set[tuple[str, str, datetime]] = set()
    for call in calls:
        if call.starts_at <= now:
            return _EVENT_START_FUTURE_REQUIRED
        if call.duration_minutes < 5 or call.duration_minutes > 240:
            return _EVENT_DURATION_INVALID
        key = (call.course_id, " ".join(call.title.casefold().split()), call.starts_at)
        if key in seen:
            return _EVENT_DUPLICATE
        seen.add(key)

    previous_end: datetime | None = None
    for call in calls:
        if previous_end is not None and call.starts_at < previous_end:
            return _EVENT_ORDER_INVALID
        previous_end = call.starts_at + timedelta(minutes=call.duration_minutes)
    return None


__all__ = [
    "HOST_VALIDATION_ERRORS",
    "MAX_PROPOSED_MUTATIONS",
    "MutationCall",
    "proposed_changes_from_calls",
]
