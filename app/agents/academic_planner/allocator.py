"""Deterministic priority scoring and constraint-aware study allocation."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from app.agents.academic_planner.contracts import (
    Assessment,
    AvailabilityWindow,
    PlannerFacts,
    StudyBlock,
)

_BLOCK_NAMESPACE = uuid.UUID("f70c1eb0-0c58-4f2a-9cb8-2f1ad390d2fb")


def _aware(value: datetime, name: str = "timestamp") -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def priority_score(
    assessment: Assessment,
    *,
    now: datetime,
    available_minutes: int | None = None,
) -> float:
    """Compute an explainable score; larger means schedule sooner.

    Deadline pressure, grade weight, effort pressure, confidence/risk and
    course priority are all deterministic.  The score is not a model output.
    """

    current = _aware(now, "now")
    remaining_days = max((assessment.due_at - current).total_seconds() / 86_400, 0.0)
    deadline_pressure = max(0.0, 100.0 - remaining_days * 12.0)
    effort_pressure = 0.0
    if available_minutes is not None and available_minutes > 0:
        effort_pressure = min(100.0, assessment.estimated_minutes / available_minutes * 100.0)
    risk = assessment.confidence_gap * 30.0 + assessment.scope_size * 0.2
    return round(
        deadline_pressure
        + assessment.weight_percent
        + effort_pressure
        + risk
        + assessment.course_priority * 0.2,
        4,
    )


def _overlaps(
    start: datetime, end: datetime, intervals: Iterable[tuple[datetime, datetime]]
) -> bool:
    return any(start < other_end and end > other_start for other_start, other_end in intervals)


def _available_minutes(
    windows: Iterable[AvailabilityWindow], start: datetime, end: datetime
) -> int:
    total = 0
    for window in windows:
        left = max(window.start_at, start)
        right = min(window.end_at, end)
        if right > left:
            total += int((right - left).total_seconds() // 60)
    return total


def allocate_plan(
    facts: PlannerFacts,
    *,
    now: datetime,
) -> tuple[StudyBlock, ...]:
    """Allocate blocks in the next 7-14 days without moving fixed events.

    Windows are traversed in chronological order and candidates by descending
    deterministic priority.  Fixed commitments are expanded by the requested
    buffer, so a block cannot consume protected transition time.  Incomplete
    work is scheduled first for its linked assessment and marked visibly as
    carried over.
    """

    current = _aware(now, "now")
    horizon_end = current + timedelta(days=facts.horizon_days)
    fixed: list[tuple[datetime, datetime]] = []
    for commitment in facts.commitments:
        start = max(commitment.start_at, current - timedelta(days=1))
        end = min(commitment.end_at, horizon_end + timedelta(days=1))
        if end <= start:
            continue
        fixed.append(
            (
                start - timedelta(minutes=facts.buffer_minutes),
                end + timedelta(minutes=facts.buffer_minutes),
            )
        )
    windows = tuple(facts.availability)
    available = _available_minutes(windows, current, horizon_end)
    incomplete = {block.assessment_id: block for block in facts.incomplete_blocks}
    candidates = [
        assessment
        for assessment in facts.assessments
        if not assessment.completed
        and not assessment.ambiguous
        and assessment.due_at > current
        and assessment.due_at <= horizon_end + timedelta(days=1)
    ]
    candidates.sort(
        key=lambda item: (
            -priority_score(item, now=current, available_minutes=available),
            item.due_at,
            item.id,
        )
    )
    placed: list[StudyBlock] = []
    occupied = list(fixed)
    for assessment in candidates:
        remaining = incomplete.get(assessment.id)
        minutes_left = (
            remaining.remaining_minutes if remaining is not None else assessment.estimated_minutes
        )
        carried = remaining is not None
        score = priority_score(assessment, now=current, available_minutes=available)
        for window in sorted(windows, key=lambda value: value.start_at):
            cursor = max(window.start_at, current)
            while cursor < window.end_at and minutes_left > 0:
                cursor += timedelta(minutes=(15 - cursor.minute % 15) % 15)
                if cursor >= window.end_at:
                    break
                chunk = min(90, minutes_left)
                end = cursor + timedelta(minutes=chunk)
                if (
                    end > window.end_at
                    or end > assessment.due_at
                    or _overlaps(cursor, end, occupied)
                ):
                    cursor += timedelta(minutes=15)
                    continue
                block_id = uuid.uuid5(
                    _BLOCK_NAMESPACE,
                    f"{assessment.id}:{cursor.isoformat()}:{end.isoformat()}:{carried}",
                )
                placed.append(
                    StudyBlock(
                        id=str(block_id),
                        assessment_id=assessment.id,
                        title=(
                            f"Carry forward: {remaining.title}" if remaining else assessment.title
                        ),
                        start_at=cursor,
                        end_at=end,
                        carried_over=carried,
                        priority_score=score,
                        rationale=(
                            "Carried incomplete work forward before its deadline."
                            if carried
                            else "Scheduled by deadline, effort, weight, risk and course priority."
                        ),
                    )
                )
                occupied.append((cursor, end))
                minutes_left -= chunk
                cursor = end + timedelta(minutes=facts.buffer_minutes)
            if minutes_left <= 0:
                break
    placed.sort(key=lambda block: (block.start_at, -block.priority_score, block.id))
    return tuple(placed)


__all__ = ["allocate_plan", "priority_score"]
