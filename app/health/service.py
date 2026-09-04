"""Persistence bridge for model-free operational health evaluations."""

from __future__ import annotations

from datetime import UTC, datetime
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
    "finance": "finance",
}


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


def _next_expected(settings: Settings, component: str, current: datetime) -> datetime | None:
    # Importing through app.queue at module load would initialize the task registry,
    # which itself imports this health service.
    from app.queue.periodic import TorontoPeriodicSchedule

    schedules = {
        "finance": (
            TorontoPeriodicSchedule.from_time(
                settings.finance_market_open_schedule,
                weekdays=frozenset(range(5)),
            ),
        ),
        "code_review": (TorontoPeriodicSchedule.from_time(settings.code_review_schedule),),
        "academic_planner": (
            TorontoPeriodicSchedule.from_time(settings.academic_morning_schedule),
            TorontoPeriodicSchedule.from_time(settings.academic_end_of_day_schedule),
        ),
    }.get(component, ())
    occurrences = tuple(schedule.next_occurrence(current).scheduled_at for schedule in schedules)
    return min(occurrences) if occurrences else None


__all__ = ["evaluate_and_persist", "evaluate_run_and_persist"]
