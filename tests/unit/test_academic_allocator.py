from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.agents.academic_planner.allocator import (
    allocate_plan,
    allocate_plan_with_deferred,
    priority_score,
)
from app.agents.academic_planner.contracts import (
    Assessment,
    AssessmentType,
    AvailabilityWindow,
    FixedCommitment,
    IncompleteBlock,
    PlannerFacts,
    PracticeNeed,
)


def _assessment(identifier: str = "essay", *, due_days: int = 2) -> Assessment:
    return Assessment(
        id=identifier,
        course="CSC",
        title="Write essay",
        assessment_type=AssessmentType.ASSIGNMENT,
        due_at=datetime(2025, 3, 11, 20, tzinfo=UTC),
        estimated_minutes=120,
        weight_percent=20,
        course_priority=80,
    )


def test_priority_score_prefers_nearer_deadline() -> None:
    now = datetime(2025, 3, 9, 15, tzinfo=UTC)
    near = _assessment("near")
    far = near.model_copy(update={"id": "far", "due_at": near.due_at + timedelta(days=3)})
    assert priority_score(near, now=now) > priority_score(far, now=now)


def test_allocator_preserves_fixed_class_and_buffer() -> None:
    now = datetime(2025, 3, 9, 14, tzinfo=UTC)
    facts = PlannerFacts(
        assessments=(_assessment(),),
        availability=(
            AvailabilityWindow(
                start_at=datetime(2025, 3, 9, 14, tzinfo=UTC),
                end_at=datetime(2025, 3, 9, 20, tzinfo=UTC),
            ),
        ),
        commitments=(
            FixedCommitment(
                id="class",
                title="Class",
                start_at=datetime(2025, 3, 9, 16, tzinfo=UTC),
                end_at=datetime(2025, 3, 9, 17, tzinfo=UTC),
                kind="class",
            ),
        ),
        buffer_minutes=30,
        horizon_days=7,
    )
    blocks = allocate_plan(facts, now=now)
    assert blocks
    assert all(
        block.end_at <= datetime(2025, 3, 9, 15, 30, tzinfo=UTC)
        or block.start_at >= datetime(2025, 3, 9, 17, 30, tzinfo=UTC)
        for block in blocks
    )


def test_incomplete_work_is_carried_forward_and_ambiguous_is_excluded() -> None:
    now = datetime(2025, 3, 9, 14, tzinfo=UTC)
    assessment = _assessment()
    ambiguous = assessment.model_copy(update={"id": "ambiguous", "ambiguous": True})
    facts = PlannerFacts(
        assessments=(assessment, ambiguous),
        availability=(
            AvailabilityWindow(
                start_at=now,
                end_at=now + timedelta(hours=4),
            ),
        ),
        incomplete_blocks=(
            IncompleteBlock(
                id="old",
                assessment_id="essay",
                title="Old essay block",
                remaining_minutes=30,
            ),
        ),
    )
    blocks = allocate_plan(facts, now=now)
    assert any(block.carried_over for block in blocks)
    assert all(block.assessment_id != "ambiguous" for block in blocks)


def test_dst_timestamp_inputs_are_normalized_and_allocatable() -> None:
    facts = PlannerFacts(
        assessments=(_assessment(),),
        availability=(
            AvailabilityWindow(
                start_at=datetime(2025, 3, 9, 1, 30, tzinfo=UTC),
                end_at=datetime(2025, 3, 9, 5, 30, tzinfo=UTC),
            ),
        ),
    )
    assert allocate_plan(facts, now=datetime(2025, 3, 9, 1, 30, tzinfo=UTC))


def test_active_focus_gets_a_separate_practice_block_before_assessment_work() -> None:
    now = datetime(2025, 3, 9, 14, tzinfo=UTC)
    facts = PlannerFacts(
        assessments=(_assessment(),),
        practice_needs=(
            PracticeNeed(
                focus_id="focus-recursion",
                course_code="ECE 250",
                topic="recursion",
                target_minutes=30,
                next_review_at=now + timedelta(hours=6),
                source_action="reinforce_focus",
                rationale="The user explicitly reported difficulty with recursion.",
            ),
        ),
        availability=(AvailabilityWindow(start_at=now, end_at=now + timedelta(hours=4)),),
        buffer_minutes=15,
    )

    blocks, deferred = allocate_plan_with_deferred(facts, now=now)

    practice = [block for block in blocks if block.block_kind == "practice"]
    assessment = [block for block in blocks if block.block_kind == "assessment"]
    assert len(practice) == 1
    assert practice[0].learning_focus_id == "focus-recursion"
    assert practice[0].title == "Practice ECE 250 recursion"
    assert practice[0].end_at - practice[0].start_at == timedelta(minutes=30)
    assert assessment
    assert practice[0].end_at + timedelta(minutes=15) <= assessment[0].start_at
    assert deferred == ()


def test_unschedulable_practice_focus_is_reported_as_deferred() -> None:
    now = datetime(2025, 3, 9, 14, tzinfo=UTC)
    facts = PlannerFacts(
        practice_needs=(
            PracticeNeed(
                focus_id="focus-recursion",
                topic="recursion",
                target_minutes=60,
                next_review_at=now + timedelta(hours=1),
                source_action="reinforce_focus",
                rationale="The user explicitly reported difficulty with recursion.",
            ),
        ),
        availability=(AvailabilityWindow(start_at=now, end_at=now + timedelta(minutes=30)),),
    )

    blocks, deferred = allocate_plan_with_deferred(facts, now=now)

    assert blocks == ()
    assert deferred == ("focus-recursion",)


def test_expanded_todo_types_schedule_as_generic_assessment_blocks() -> None:
    now = datetime(2025, 3, 9, 14, tzinfo=UTC)
    facts = PlannerFacts(
        assessments=(
            _assessment("tutorial").model_copy(
                update={
                    "title": "Tutorial problems",
                    "assessment_type": AssessmentType.TUTORIAL,
                    "due_at": now + timedelta(days=1),
                    "estimated_minutes": 30,
                }
            ),
            _assessment("lab").model_copy(
                update={
                    "title": "Lab writeup",
                    "assessment_type": AssessmentType.LAB,
                    "due_at": now + timedelta(days=2),
                    "estimated_minutes": 30,
                }
            ),
            _assessment("study-session").model_copy(
                update={
                    "title": "Review chapter 4",
                    "assessment_type": AssessmentType.STUDYING_BLOCK,
                    "due_at": now + timedelta(days=3),
                    "estimated_minutes": 30,
                }
            ),
        ),
        availability=(
            AvailabilityWindow(
                start_at=now,
                end_at=now + timedelta(hours=3),
            ),
        ),
        buffer_minutes=0,
    )

    blocks = allocate_plan(facts, now=now)

    studying_todo = next(
        assessment for assessment in facts.assessments if assessment.id == "study-session"
    )
    assert {block.assessment_id for block in blocks} == {"tutorial", "lab", "study-session"}
    assert studying_todo.assessment_type == AssessmentType.STUDYING_BLOCK
    assert all(block.block_kind == "assessment" for block in blocks)
    studying_todo_block = next(block for block in blocks if block.assessment_id == "study-session")
    assert studying_todo_block.block_kind == "assessment"
    assert studying_todo_block.title == "Review chapter 4"
