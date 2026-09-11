"""Persistence bridge for model-free operational health evaluations."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import AgentRun, Delivery, RunStatus
from app.db.models import DeliveryStatus as DatabaseDeliveryStatus
from app.db.models import HealthState as DatabaseHealthState
from app.db.repositories import HealthRepository
from app.health.evaluator import (
    DeliveryStatus,
    OperationalFacts,
    OperationalHealth,
    ProcessingStatus,
    evaluate_operational_health,
)

_COMPONENT_ALIASES = {
    "code_review_daily": "code_review",
    "code_review_ingest": "code_review",
    "academic_planner": "academic_planner",
    "academic_morning_notification": "academic_morning",
    "finance": "finance",
}

_ACADEMIC_MORNING_AGENT = "academic_morning_notification"
_ACADEMIC_MORNING_COMPONENT = "academic_morning"
_ACADEMIC_MORNING_NAMESPACE = "academic-morning"
_DEFAULT_ACADEMIC_MORNING_GRACE_MINUTES = 30


def evaluate_and_persist(session: Session, facts: OperationalFacts) -> OperationalHealth:
    """Evaluate ordered rules and upsert only their redacted structured result."""

    health = evaluate_operational_health(facts)
    HealthRepository.upsert(
        session,
        check_name=health.component,
        rule=health.rule,
        state=DatabaseHealthState(health.state.value),
        last_success_at=health.last_success_at,
        next_due_at=health.next_expected_at,
        diagnostic=health.diagnostic,
    )
    return health


def evaluate_run_and_persist(
    session: Session,
    *,
    run_id: UUID,
    settings: Settings,
    retry_attempt: int,
    retry_limit: int,
    evaluated_at: datetime | None = None,
) -> OperationalHealth:
    """Project a completed durable run and its receipts into deterministic health."""

    run = session.get(AgentRun, run_id)
    if run is None:
        raise ValueError("operational health run was not found")
    current = (evaluated_at or datetime.now(UTC)).astimezone(UTC)
    component = _COMPONENT_ALIASES.get(run.agent_name, run.agent_name)
    processing = (
        ProcessingStatus.WAITING_APPROVAL
        if run.error_code in {"approval_required", "confirmation_required"}
        else _processing_status(run.status)
    )
    deliveries = tuple(session.scalars(select(Delivery).where(Delivery.run_id == run_id)))
    delivery = _delivery_status(deliveries, processing)
    connector_authenticated = run.error_code != "authorization_invalid" and all(
        receipt.error_code != "authorization_invalid" for receipt in deliveries
    )
    last_success_at = session.scalar(
        select(AgentRun.finished_at)
        .where(
            AgentRun.agent_name.in_(_agent_names(component)),
            AgentRun.status == RunStatus.SUCCEEDED,
            AgentRun.finished_at.is_not(None),
        )
        .order_by(AgentRun.finished_at.desc())
        .limit(1)
    )
    return evaluate_and_persist(
        session,
        OperationalFacts(
            component=component,
            processing=processing,
            delivery=delivery,
            connector_authenticated=connector_authenticated,
            evaluated_at=current,
            last_success_at=last_success_at,
            next_expected_at=_next_expected(settings, component, current),
            retry_attempt=retry_attempt,
            retry_limit=retry_limit,
            diagnostic_code=run.error_code or f"run_{run.status}",
        ),
    )


def evaluate_academic_morning_health(
    session: Session,
    *,
    settings: Settings,
    evaluated_at: datetime,
) -> OperationalHealth:
    """Persist schedule-specific health for the model-free academic morning notifier."""

    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise ValueError("evaluated_at must be timezone-aware")
    current = evaluated_at.astimezone(UTC)
    schedule, occurrence = _academic_morning_occurrence(settings, current)
    grace = timedelta(minutes=_academic_morning_grace_minutes(settings))
    current_deadline = occurrence.scheduled_at + grace
    period_key = _academic_morning_period_key(occurrence)
    run = session.scalar(
        select(AgentRun)
        .where(
            AgentRun.agent_name == _ACADEMIC_MORNING_AGENT,
            AgentRun.idempotency_key == period_key,
        )
        .order_by(AgentRun.created_at.desc())
        .limit(1)
    )
    last_success_at = _latest_academic_morning_success(session)
    next_expected_at = current_deadline
    if run is None:
        processing = (
            ProcessingStatus.SUCCEEDED if current <= current_deadline else ProcessingStatus.QUEUED
        )
        delivery = DeliveryStatus.NOT_REQUIRED
        connector_authenticated = True
        diagnostic_code = (
            f"academic_morning_waiting:{period_key}"
            if current <= current_deadline
            else f"academic_morning_missing:{period_key}"
        )
    else:
        processing = (
            ProcessingStatus.WAITING_APPROVAL
            if run.error_code in {"approval_required", "confirmation_required"}
            else _processing_status(run.status)
        )
        deliveries = tuple(session.scalars(select(Delivery).where(Delivery.run_id == run.id)))
        delivery = _delivery_status(deliveries, processing)
        connector_authenticated = run.error_code != "authorization_invalid" and all(
            receipt.error_code != "authorization_invalid" for receipt in deliveries
        )
        if processing is ProcessingStatus.SUCCEEDED and delivery is DeliveryStatus.SUCCEEDED:
            next_expected_at = _next_academic_morning_deadline(
                schedule,
                current=current,
                current_scheduled_at=occurrence.scheduled_at,
                grace=grace,
            )
        diagnostic_code = run.error_code or f"run_{run.status!s}"
    return evaluate_and_persist(
        session,
        OperationalFacts(
            component=_ACADEMIC_MORNING_COMPONENT,
            processing=processing,
            delivery=delivery,
            connector_authenticated=connector_authenticated,
            evaluated_at=current,
            last_success_at=last_success_at,
            next_expected_at=next_expected_at,
            diagnostic_code=diagnostic_code,
        ),
    )


def _processing_status(status: RunStatus) -> ProcessingStatus:
    return {
        RunStatus.QUEUED: ProcessingStatus.QUEUED,
        RunStatus.RUNNING: ProcessingStatus.RUNNING,
        RunStatus.SUCCEEDED: ProcessingStatus.SUCCEEDED,
        RunStatus.ATTENTION: ProcessingStatus.ATTENTION,
        RunStatus.FAILED: ProcessingStatus.FAILED,
        RunStatus.PAUSED: ProcessingStatus.WAITING_APPROVAL,
        RunStatus.CANCELLED: ProcessingStatus.FAILED,
    }[status]


def _delivery_status(
    deliveries: tuple[Delivery, ...],
    processing: ProcessingStatus,
) -> DeliveryStatus:
    if not deliveries:
        return (
            DeliveryStatus.FAILED
            if processing is ProcessingStatus.SUCCEEDED
            else DeliveryStatus.NOT_REQUIRED
        )
    statuses = {receipt.status for receipt in deliveries}
    if DatabaseDeliveryStatus.FAILED in statuses:
        return DeliveryStatus.FAILED
    if DatabaseDeliveryStatus.UNCERTAIN in statuses:
        return DeliveryStatus.UNCERTAIN
    if statuses.intersection({DatabaseDeliveryStatus.PENDING, DatabaseDeliveryStatus.SENDING}):
        return DeliveryStatus.INTENT
    if statuses.issubset({DatabaseDeliveryStatus.SENT, DatabaseDeliveryStatus.ACKNOWLEDGED}):
        return DeliveryStatus.SUCCEEDED
    return DeliveryStatus.FAILED


def _agent_names(component: str) -> tuple[str, ...]:
    return tuple(
        name
        for name in {component, *_COMPONENT_ALIASES}
        if _COMPONENT_ALIASES.get(name, name) == component
    )


def _academic_morning_grace_minutes(settings: Settings) -> int:
    value = getattr(
        settings,
        "academic_morning_catchup_grace_minutes",
        _DEFAULT_ACADEMIC_MORNING_GRACE_MINUTES,
    )
    if not isinstance(value, int):
        value = int(value)
    if value < 0:
        raise ValueError("academic morning catch-up grace must not be negative")
    return value


def _academic_morning_occurrence(settings: Settings, current: datetime) -> tuple[Any, Any]:
    # Import lazily to avoid initializing the queue task registry while loading health services.
    from app.queue.periodic import TorontoPeriodicSchedule

    schedule = TorontoPeriodicSchedule.from_time(
        settings.academic_morning_schedule,
        timezone_name=settings.app_timezone,
    )
    local_day = current.astimezone(schedule.zone).date()
    day_start = datetime.combine(local_day, time.min, tzinfo=schedule.zone)
    candidate = schedule.next_occurrence(day_start.astimezone(UTC) - timedelta(microseconds=1))
    if candidate.local_time.astimezone(schedule.zone).date() == local_day:
        return schedule, candidate
    return schedule, schedule.next_occurrence(current)


def _academic_morning_period_key(occurrence: Any) -> str:
    from app.queue.periodic import stable_period_key

    return stable_period_key(_ACADEMIC_MORNING_NAMESPACE, occurrence)


def _next_academic_morning_deadline(
    schedule: Any,
    *,
    current: datetime,
    current_scheduled_at: datetime,
    grace: timedelta,
) -> datetime:
    next_after = max(current, current_scheduled_at) + timedelta(microseconds=1)
    return schedule.next_occurrence(next_after).scheduled_at + grace


def _latest_academic_morning_success(session: Session) -> datetime | None:
    finished_at = session.scalar(
        select(AgentRun.finished_at)
        .join(Delivery, Delivery.run_id == AgentRun.id)
        .where(
            AgentRun.agent_name == _ACADEMIC_MORNING_AGENT,
            AgentRun.status == RunStatus.SUCCEEDED,
            AgentRun.finished_at.is_not(None),
            Delivery.status.in_(
                (
                    DatabaseDeliveryStatus.SENT,
                    DatabaseDeliveryStatus.ACKNOWLEDGED,
                )
            ),
        )
        .order_by(AgentRun.finished_at.desc())
        .limit(1)
    )
    if finished_at is None:
        return None
    if finished_at.tzinfo is None or finished_at.utcoffset() is None:
        return finished_at.replace(tzinfo=UTC)
    return finished_at.astimezone(UTC)


def _next_expected(settings: Settings, component: str, current: datetime) -> datetime | None:
    """Return a due time only for the model-free scheduled morning component."""

    if component != _ACADEMIC_MORNING_COMPONENT:
        return None
    from app.queue.periodic import TorontoPeriodicSchedule

    schedule = TorontoPeriodicSchedule.from_time(
        settings.academic_morning_schedule,
        timezone_name=settings.app_timezone,
    )
    return schedule.next_occurrence(current).scheduled_at + timedelta(
        minutes=_academic_morning_grace_minutes(settings)
    )


__all__ = [
    "evaluate_academic_morning_health",
    "evaluate_and_persist",
    "evaluate_run_and_persist",
]
