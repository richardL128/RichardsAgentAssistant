from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.agents.academic_planner.allocator import allocate_plan, priority_score
from app.agents.academic_planner.contracts import (
    Assessment,
    AssessmentType,
    AvailabilityWindow,
    FixedCommitment,
    IncompleteBlock,
    PlannerFacts,
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
