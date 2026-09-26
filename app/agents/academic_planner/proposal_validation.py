"""Mandatory application validation for model-proposed academic mutations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from app.agents.academic_planner.calendar_roles import (
    AcademicCalendarRole,
    canonical_misc_task_title,
)
from app.agents.academic_planner.classification import canonical_assessment_title
from app.agents.academic_planner.contracts import (
    ActionItemDomain,
    ActionItemKind,
    ActionItemStatus,
    AcademicAssessmentOption,
    AcademicCourseOption,
    ArchiveActionItemCall,
    ArchiveAssessmentCall,
    AssessmentType,
    AttachAssessmentMaterialCall,
    CreateActionItemCall,
    CreateAssessmentCall,
    CreateCourseEventCall,
    CreateMiscTaskCall,
    InboundMaterialProposalPreview,
    ProposedChange,
    DateOnlyValue,
    DateTimeValue,
    TemporalValue,
    UpdateActionItemCall,
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
    CreateActionItemCall
    | UpdateActionItemCall
    | ArchiveActionItemCall
    | CreateAssessmentCall
    | CreateMiscTaskCall
    | CreateCourseEventCall
    | UpdateAssessmentCall
    | ArchiveAssessmentCall
    | AttachAssessmentMaterialCall
)

_OWNER_TIMEZONE = ZoneInfo("America/Toronto")

_ACTION_TO_ASSESSMENT_TYPE: dict[ActionItemKind, AssessmentType] = {
    ActionItemKind.TASK: AssessmentType.TASK,
    ActionItemKind.ASSIGNMENT: AssessmentType.ASSIGNMENT,
    ActionItemKind.QUIZ: AssessmentType.QUIZ,
    ActionItemKind.TUTORIAL: AssessmentType.TUTORIAL,
    ActionItemKind.LAB: AssessmentType.LAB,
    ActionItemKind.EVENT: AssessmentType.EVENT,
    ActionItemKind.EXAM: AssessmentType.MIDTERM,
    ActionItemKind.DEADLINE: AssessmentType.TASK,
    ActionItemKind.MEETING: AssessmentType.EVENT,
    ActionItemKind.NEEDS_REVIEW: AssessmentType.TASK,
}

_ASSESSMENT_TO_ACTION_KIND: dict[AssessmentType, ActionItemKind] = {
    AssessmentType.TASK: ActionItemKind.TASK,
    AssessmentType.ASSIGNMENT: ActionItemKind.ASSIGNMENT,
    AssessmentType.QUIZ: ActionItemKind.QUIZ,
    AssessmentType.TUTORIAL: ActionItemKind.TUTORIAL,
    AssessmentType.LAB: ActionItemKind.LAB,
    AssessmentType.EVENT: ActionItemKind.EVENT,
    AssessmentType.MIDTERM: ActionItemKind.EXAM,
    AssessmentType.FINAL: ActionItemKind.EXAM,
}


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

        if isinstance(call, CreateActionItemCall):
            course_id = call.course_id
            starts_at = _temporal_start_at(call.temporal)
            course = known_courses.get(course_id)
            if call.domain is not ActionItemDomain.ACADEMIC:
                if course_id is None:
                    return (), _MISC_TARGET_UNVERIFIED
                if course is None or course.calendar_role is not AcademicCalendarRole.MISC:
                    return (), _MISC_TARGET_UNVERIFIED
                if starts_at <= now:
                    return (), _MISC_DUE_FUTURE_REQUIRED
                title = canonical_misc_task_title(call.title)
            else:
                if course_id is None:
                    return (), _CREATE_COURSE_UNVERIFIED
                if course is None or course.calendar_role is not AcademicCalendarRole.COURSE:
                    return (), _CREATE_COURSE_UNVERIFIED
                if starts_at <= now:
                    return (), _CREATE_DUE_FUTURE_REQUIRED
                title = _canonical_action_item_title(call.kind, call.title)
            assessment_type = _ACTION_TO_ASSESSMENT_TYPE.get(call.kind, AssessmentType.TASK)
            changes.append(
                ProposedChange(
                    field="create_action_item",
                    value=title,
                    course_id=course_id,
                    course_code=course.course_code,
                    title=title,
                    assessment_type=assessment_type,
                    action_domain=call.domain,
                    action_status=ActionItemStatus.TO_DO,
                    action_kind=call.kind,
                    action_temporal=call.temporal,
                    action_context=call.context,
                    inbound_material_ids=call.inbound_material_ids or None,
                    inbound_material_previews=previews or None,
                    supersedes_proposal_id=call.supersedes_proposal_id,
                )
            )
            continue

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

        if isinstance(call, UpdateActionItemCall):
            if call.item_id not in known_assessments:
                return (), _ASSESSMENT_UNVERIFIED
            existing = known_assessments[call.item_id]
            if existing.expected_last_edited_at is None:
                return (), _ASSESSMENT_METADATA_MISSING
            temporal = call.temporal
            if isinstance(temporal, DateTimeValue) and temporal.end_at is None:
                ends_at = _shift_existing_end(existing, temporal.start_at)
                if ends_at is not None:
                    temporal = DateTimeValue(
                        start_at=temporal.start_at,
                        end_at=ends_at,
                        timezone=temporal.timezone,
                    )
            status = call.status or _status_from_existing(existing)
            changes.append(
                ProposedChange(
                    field="update_action_item",
                    value="update_action_item",
                    assessment_id=call.item_id,
                    course_id=existing.course_id,
                    course_code=existing.course_code,
                    title=call.title,
                    assessment_type=existing.assessment_type,
                    action_domain=_domain_from_assessment(existing),
                    action_status=status,
                    action_kind=_ASSESSMENT_TO_ACTION_KIND.get(existing.assessment_type),
                    action_temporal=temporal,
                    action_context=call.context,
                    expected_title=existing.title,
                    expected_last_edited_at=existing.expected_last_edited_at,
                )
            )
            continue

        if isinstance(call, ArchiveActionItemCall):
            if call.item_id not in known_assessments:
                return (), _ASSESSMENT_UNVERIFIED
            existing = known_assessments[call.item_id]
            if existing.expected_last_edited_at is None:
                return (), _ASSESSMENT_METADATA_MISSING
            changes.append(
                ProposedChange(
                    field="archive_action_item",
                    value="archive_action_item",
                    assessment_id=call.item_id,
                    course_id=existing.course_id,
                    course_code=existing.course_code,
                    assessment_type=existing.assessment_type,
                    action_domain=_domain_from_assessment(existing),
                    action_status=ActionItemStatus.CANCELED,
                    action_kind=_ASSESSMENT_TO_ACTION_KIND.get(existing.assessment_type),
                    action_context=call.context,
                    expected_title=existing.title,
                    expected_last_edited_at=existing.expected_last_edited_at,
                )
            )
            continue

        if call.assessment_id not in known_assessments:
            return (), _ASSESSMENT_UNVERIFIED
        existing = known_assessments[call.assessment_id]
        if existing.expected_last_edited_at is None:
            return (), _ASSESSMENT_METADATA_MISSING
        if isinstance(call, UpdateAssessmentCall):
            ends_at = call.ends_at
            if ends_at is None and call.due_at is not None:
                ends_at = _shift_existing_end(existing, call.due_at)
            changes.append(
                ProposedChange(
                    field="update_assessment",
                    value="update_assessment",
                    assessment_id=call.assessment_id,
                    title=call.title,
                    due_at=call.due_at,
                    ends_at=ends_at,
                    is_all_day=True if existing.is_all_day and call.due_at is not None else None,
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


def _shift_existing_end(
    existing: AcademicAssessmentOption,
    new_start: datetime,
) -> datetime | None:
    if existing.due_at is None or existing.ends_at is None:
        return None
    return new_start + (existing.ends_at - existing.due_at)


def _temporal_start_at(value: TemporalValue) -> datetime:
    if isinstance(value, DateTimeValue):
        return value.start_at
    if isinstance(value, DateOnlyValue):
        return datetime.combine(value.start_date, time.min, tzinfo=_OWNER_TIMEZONE).astimezone(UTC)
    raise TypeError("unsupported action-item temporal value")


def _canonical_action_item_title(action_kind: ActionItemKind, title: str) -> str:
    if action_kind is ActionItemKind.TASK:
        return " ".join(title.split())
    if action_kind in {
        ActionItemKind.ASSIGNMENT,
        ActionItemKind.QUIZ,
        ActionItemKind.TUTORIAL,
        ActionItemKind.LAB,
        ActionItemKind.EVENT,
    }:
        return canonical_assessment_title(action_kind.value, title)
    return " ".join(title.split())


def _domain_from_assessment(existing: AcademicAssessmentOption) -> ActionItemDomain:
    if existing.source_area is AcademicCalendarRole.MISC:
        return ActionItemDomain.PERSONAL
    return ActionItemDomain.ACADEMIC


def _status_from_existing(existing: AcademicAssessmentOption) -> ActionItemStatus:
    return ActionItemStatus.DONE if existing.completed else ActionItemStatus.TO_DO


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
