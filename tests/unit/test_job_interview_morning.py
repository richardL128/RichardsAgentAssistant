from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.agents.job_interviews.contracts import InterviewEventSnapshot, PreparationPlanSnapshot
from app.agents.job_interviews.morning import (
    build_interview_reminder_facts,
    reminder_label,
    render_interview_reminders,
)


def _event(day: date, *, event_id: str = "interview-1", timed: bool = False):
    return InterviewEventSnapshot(
        interview_page_id=event_id,
        title="Shopify Backend Technical Round",
        date_start=datetime(day.year, day.month, day.day, 18, tzinfo=UTC) if timed else None,
        local_date=day,
        is_all_day=not timed,
        last_edited_at=datetime(2026, 9, 1, tzinfo=UTC),
        content_fingerprint=f"fingerprint-{event_id}",
    )


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (14, "**INTERVIEW IN 14 DAYS**"),
        (7, "**INTERVIEW IN 7 DAYS**"),
        (3, "**INTERVIEW IN 3 DAYS**"),
        (1, "**INTERVIEW TOMORROW**"),
        (0, "**INTERVIEW TODAY**"),
        (8, None),
    ],
)
def test_exact_milestone_labels(days: int, expected: str | None) -> None:
    assert reminder_label(days) == expected


def test_every_future_interview_is_included_and_past_interviews_stop() -> None:
    current = datetime(2026, 9, 10, 12, tzinfo=UTC)
    facts = build_interview_reminder_facts(
        (_event(date(2026, 9, 9), event_id="past"), _event(date(2026, 10, 10), event_id="future")),
        now=current,
    )
    assert [fact.interview_page_id for fact in facts] == ["future"]
    assert facts[0].days_until == 30
    assert facts[0].emphasis == "normal"


def test_toronto_calendar_arithmetic_uses_local_date_across_dst() -> None:
    event = _event(date(2026, 11, 2), timed=True)
    facts = build_interview_reminder_facts(
        (event,),
        now=datetime.fromisoformat("2026-11-01T23:30:00-05:00"),
    )
    assert facts[0].days_until == 1
    assert "**INTERVIEW TOMORROW**" in render_interview_reminders(facts, interviews=(event,))
    assert "13:00 EST" in render_interview_reminders(facts, interviews=(event,))


def test_only_grounded_current_plan_action_is_rendered() -> None:
    event = _event(date(2026, 9, 13))
    plan = PreparationPlanSnapshot(
        interview_page_id=event.interview_page_id,
        revision=2,
        generated_at=datetime(2026, 9, 10, tzinfo=UTC),
        plan_hash="hash-2",
        summary="Grounded preparation plan",
        next_actions=("Practice the verified API design topics for 45 minutes.",),
        evidence=("posting:requirements",),
    )
    facts = build_interview_reminder_facts(
        (event,), now=datetime(2026, 9, 10, 12, tzinfo=UTC), plans=(plan,)
    )
    rendered = render_interview_reminders(facts, interviews=(event,))
    assert "**INTERVIEW IN 3 DAYS**" in rendered
    assert "Practice the verified API design topics" in rendered
    assert facts[0].preparation_plan_revision == 2
