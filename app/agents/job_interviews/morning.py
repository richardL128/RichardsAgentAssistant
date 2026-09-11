"""Deterministic Toronto-local interview reminders for the shared briefing."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Protocol, cast
from zoneinfo import ZoneInfo

from app.agents.job_interviews.contracts import (
    InterviewEventSnapshot,
    InterviewReminderFact,
    PreparationPlanSnapshot,
)

_MILESTONES = frozenset({1, 3, 7, 14})


class ReminderStore(Protocol):
    def load_upcoming_interviews(self, *, now: datetime) -> Iterable[InterviewEventSnapshot]: ...

    def get_current_plan(self, interview_page_id: str) -> PreparationPlanSnapshot | None: ...


def reminder_label(days_until: int) -> str | None:
    """Return the required immutable milestone label, if today is a milestone."""

    if days_until == 0:
        return "**INTERVIEW TODAY**"
    if days_until == 1:
        return "**INTERVIEW TOMORROW**"
    if days_until in _MILESTONES:
        return f"**INTERVIEW IN {days_until} DAYS**"
    return None


def build_interview_reminder_fact(
    interview: InterviewEventSnapshot,
    *,
    current_local_date: date,
    plan: PreparationPlanSnapshot | None = None,
) -> InterviewReminderFact | None:
    """Build one authoritative reminder, excluding archived and past events."""

    if not interview.active or interview.archived or interview.local_date < current_local_date:
        return None
    days_until = (interview.local_date - current_local_date).days
    next_actions = ()
    revision = None
    if plan is not None and plan.interview_page_id == interview.interview_page_id:
        revision = plan.revision
        next_actions = plan.next_actions[:1]
    return InterviewReminderFact(
        interview_page_id=interview.interview_page_id,
        title=interview.title,
        reminder_date=current_local_date,
        interview_date=interview.local_date,
        days_until=days_until,
        emphasis=(
            "today" if days_until == 0 else "milestone" if days_until in _MILESTONES else "normal"
        ),
        preparation_plan_revision=revision,
        grounded_next_actions=next_actions,
    )


def build_interview_reminder_facts(
    interviews: Iterable[InterviewEventSnapshot],
    *,
    now: datetime,
    plans: Iterable[PreparationPlanSnapshot] = (),
    timezone_name: str = "America/Toronto",
) -> tuple[InterviewReminderFact, ...]:
    """Build all active reminders in stable chronological order."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    local_date = now.astimezone(ZoneInfo(timezone_name)).date()
    plan_by_interview = {plan.interview_page_id: plan for plan in plans}
    facts = (
        build_interview_reminder_fact(
            interview,
            current_local_date=local_date,
            plan=plan_by_interview.get(interview.interview_page_id),
        )
        for interview in interviews
    )
    return tuple(
        sorted(
            (fact for fact in facts if fact is not None),
            key=lambda fact: (fact.interview_date, fact.title.casefold(), fact.interview_page_id),
        )
    )


def render_interview_reminders(
    reminders: Iterable[InterviewReminderFact],
    *,
    interviews: Iterable[InterviewEventSnapshot] = (),
    timezone_name: str = "America/Toronto",
) -> str:
    """Render exact dates and host-approved actions without model arithmetic."""

    zone = ZoneInfo(timezone_name)
    events = {event.interview_page_id: event for event in interviews}
    lines: list[str] = []
    for item in reminders:
        label = reminder_label(item.days_until)
        if label is None:
            label = (
                f"Interview in {item.days_until} days"
                if item.days_until != 1
                else "Interview tomorrow"
            )
        exact_date = (
            f"{item.interview_date.strftime('%A, %B')} {item.interview_date.day}, "
            f"{item.interview_date.year}"
        )
        event = events.get(item.interview_page_id)
        if event is not None and not event.is_all_day and event.date_start is not None:
            local_time = event.date_start.astimezone(zone).strftime("%H:%M %Z")
            exact_date = f"{exact_date} at {local_time}"
        lines.append(f"- {label} — {item.title} — {exact_date}")
        if item.grounded_next_actions:
            lines.append(f"  Next: {item.grounded_next_actions[0]}")
        elif item.days_until <= 14:
            lines.append(
                "  Preparation guidance is waiting for a matched posting or clarification."
            )
    return "\n".join(lines)


def load_reminder_inputs(
    store: object, *, now: datetime
) -> tuple[tuple[InterviewEventSnapshot, ...], tuple[PreparationPlanSnapshot, ...]]:
    """Adapt the SQL store boundary without leaking ORM records into rendering."""

    loader = getattr(store, "load_upcoming_interviews", None)
    if not callable(loader):
        return (), ()
    typed_store = cast(ReminderStore, store)
    interviews = tuple(typed_store.load_upcoming_interviews(now=now))
    plan_loader = getattr(store, "get_current_plan", None)
    if not callable(plan_loader):
        plan_loader = getattr(store, "get_current_preparation_plan", None)
    plans = tuple(
        plan
        for interview in interviews
        if callable(plan_loader)
        and isinstance((plan := plan_loader(interview.interview_page_id)), PreparationPlanSnapshot)
    )
    return interviews, plans


def utc_now() -> datetime:
    """Small injectable clock helper used by runtime composition."""

    return datetime.now(UTC)


__all__ = [
    "build_interview_reminder_fact",
    "build_interview_reminder_facts",
    "load_reminder_inputs",
    "reminder_label",
    "render_interview_reminders",
]
