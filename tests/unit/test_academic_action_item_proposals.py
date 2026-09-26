from __future__ import annotations

from datetime import UTC, datetime

from app.agents.academic_planner.calendar_roles import AcademicCalendarRole
from app.agents.academic_planner.contracts import (
    ActionItemDomain,
    ActionItemKind,
    ActionItemStatus,
    AcademicAssessmentOption,
    AcademicCourseOption,
    ArchiveActionItemCall,
    AssessmentType,
    CreateActionItemCall,
    DateTimeValue,
    UpdateActionItemCall,
)
from app.agents.academic_planner.proposal_validation import proposed_changes_from_calls


NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)


def test_create_personal_action_item_uses_canonical_domain_status_and_temporal() -> None:
    misc = AcademicCourseOption(
        course_id="misc-1",
        course_code="MISC",
        title="Misc",
        calendar_role=AcademicCalendarRole.MISC,
    )

    changes, error = proposed_changes_from_calls(
        (
            CreateActionItemCall(
                tool="create_action_item",
                domain=ActionItemDomain.PERSONAL,
                course_id="misc-1",
                title="pick up printer paper",
                temporal=DateTimeValue(
                    start_at=datetime(2026, 9, 11, 13, tzinfo=UTC),
                    timezone="America/Toronto",
                ),
                kind=ActionItemKind.TASK,
            ),
        ),
        known_courses={"misc-1": misc},
        known_assessments={},
        now=NOW,
    )

    assert error is None
    assert len(changes) == 1
    change = changes[0]
    assert change.field == "create_action_item"
    assert change.action_domain is ActionItemDomain.PERSONAL
    assert change.action_status is ActionItemStatus.TO_DO
    assert change.action_kind is ActionItemKind.TASK
    assert change.assessment_type is AssessmentType.TASK
    assert change.action_temporal is not None
    assert change.action_temporal.start_at == datetime(2026, 9, 11, 13, tzinfo=UTC)


def test_update_and_archive_action_items_keep_guarded_target_context() -> None:
    course = AcademicCourseOption(
        course_id="course-1",
        course_code="ECE 202",
        title="ECE 202",
        calendar_role=AcademicCalendarRole.COURSE,
    )
    existing = AcademicAssessmentOption(
        assessment_id="item-1",
        course_id="course-1",
        course_code="ECE 202",
        title="Quiz 1",
        due_at=datetime(2026, 9, 11, 20, tzinfo=UTC),
        ends_at=datetime(2026, 9, 11, 21, tzinfo=UTC),
        assessment_type=AssessmentType.QUIZ,
        expected_last_edited_at=datetime(2026, 9, 10, 10, tzinfo=UTC),
    )

    changes, error = proposed_changes_from_calls(
        (
            UpdateActionItemCall(
                tool="update_action_item",
                item_id="item-1",
                temporal=DateTimeValue(
                    start_at=datetime(2026, 9, 12, 20, tzinfo=UTC),
                    timezone="America/Toronto",
                ),
            ),
            ArchiveActionItemCall(tool="archive_action_item", item_id="item-1"),
        ),
        known_courses={"course-1": course},
        known_assessments={"item-1": existing},
        now=NOW,
    )

    assert error is None
    assert [change.field for change in changes] == ["update_action_item", "archive_action_item"]
    update, archive = changes
    assert update.action_temporal is not None
    assert update.action_temporal.end_at == datetime(2026, 9, 12, 21, tzinfo=UTC)
    assert update.action_domain is ActionItemDomain.ACADEMIC
    assert update.action_status is ActionItemStatus.TO_DO
    assert update.expected_title == "Quiz 1"
    assert archive.action_status is ActionItemStatus.CANCELED
    assert archive.expected_last_edited_at == datetime(2026, 9, 10, 10, tzinfo=UTC)
