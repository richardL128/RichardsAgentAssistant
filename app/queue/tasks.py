"""Named Procrastinate task entry points for the three isolated workers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from procrastinate import JobContext
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import HealthCheck as PersistedHealthCheck
from app.db.models import RunStatus
from app.db.repositories import RunRepository
from app.db.session import Database
from app.health.checks import (
    HealthState,
    check_artifact_root,
    check_connector_configuration,
    check_database,
    check_ollama,
    check_queue,
)
from app.health.evaluator import DeliveryStatus as HealthDeliveryStatus
from app.health.evaluator import OperationalFacts, ProcessingStatus
from app.health.service import evaluate_and_persist
from app.queue.app import JOB_KINDS, default_retry_strategy, procrastinate_app
from app.queue.execution import execute_recorded_attempt
from app.queue.idempotency import validate_idempotency_key
from app.queue.periodic import PeriodicOccurrence, TorontoPeriodicSchedule, stable_period_key

TaskHandler = Callable[[str, str], Awaitable[dict[str, Any]]]
_handlers: dict[str, TaskHandler] = {}
_database = Database(get_settings())
_MODEL_LOCK = "ollama:exclusive"


def register_task_handler(job_kind: str, handler: TaskHandler) -> None:
    """Register a workflow handler during worker startup, not module import."""

    if job_kind not in JOB_KINDS:
        raise ValueError(f"unknown job kind: {job_kind}")
    _handlers[job_kind] = handler


async def _dispatch(
    context: JobContext,
    queue_name: str,
    run_id: str,
    idempotency_key: str,
    kind: str = "",
) -> dict[str, Any]:
    if not run_id.strip():
        raise ValueError("run_id must not be empty")
    validate_idempotency_key(idempotency_key)
    job_kind = kind or queue_name
    if JOB_KINDS.get(job_kind) != queue_name:
        raise ValueError(f"job kind {job_kind} does not belong to queue {queue_name}")
    handler = _handlers.get(job_kind)
    if handler is None:
        raise RuntimeError(f"no handler registered for {job_kind}")
    parsed_run_id = UUID(run_id)

    async def operation() -> dict[str, object]:
        return await handler(run_id, idempotency_key)

    await execute_recorded_attempt(
        operation,
        engine=_database.engine,
        run_id=parsed_run_id,
        node_name=f"queue.{job_kind}",
        attempt=context.job.attempts + 1,
        retry_policy=default_retry_strategy.policy,
    )
    return {"status": "succeeded", "run_id": run_id}


def defer_idempotent(task: Any, run_id: str, idempotency_key: str, kind: str = "") -> Any:
    """Defer a task with a per-work-item queueing lock.

    Procrastinate's decorator-level lock is static.  Callers should use this
    helper (or equivalent explicit ``configure`` call) so two periods with
    different keys do not block one another while identical keys collapse to a
    single waiting job.  The actual database insert occurs only when ``defer``
    is called, never during module import.
    """

    if not run_id.strip():
        raise ValueError("run_id must not be empty")
    validate_idempotency_key(idempotency_key)
    arguments: dict[str, str] = {"run_id": run_id, "idempotency_key": idempotency_key}
    if kind:
        arguments["kind"] = kind
    return task.configure(lock=_MODEL_LOCK, queueing_lock=idempotency_key).defer(**arguments)


async def defer_idempotent_async(
    task: Any, run_id: str, idempotency_key: str, kind: str = ""
) -> Any:
    """Async form used by periodic deferrer tasks."""

    if not run_id.strip():
        raise ValueError("run_id must not be empty")
    validate_idempotency_key(idempotency_key)
    arguments: dict[str, str] = {"run_id": run_id, "idempotency_key": idempotency_key}
    if kind:
        arguments["kind"] = kind
    return await task.configure(lock=_MODEL_LOCK, queueing_lock=idempotency_key).defer_async(
        **arguments
    )


@procrastinate_app.task(
    name="lifeagent.code_review",
    queue="code_review",
    retry=default_retry_strategy,
    pass_context=True,
)
async def code_review_task(
    context: JobContext, run_id: str, idempotency_key: str, kind: str = ""
) -> dict[str, Any]:
    return await _dispatch(context, "code_review", run_id, idempotency_key, kind)


@procrastinate_app.task(
    name="lifeagent.code_review_daily",
    queue="code_review",
    retry=default_retry_strategy,
    pass_context=True,
)
async def code_review_daily_task(
    context: JobContext, run_id: str, idempotency_key: str
) -> dict[str, Any]:
    """Run the one-per-Toronto-day report consolidation."""

    return await _dispatch(context, "code_review", run_id, idempotency_key, "code_review_daily")


@procrastinate_app.task(
    name="lifeagent.code_review_ingest",
    queue="code_review",
    retry=default_retry_strategy,
    pass_context=True,
)
async def code_review_ingest_task(
    context: JobContext, run_id: str, idempotency_key: str
) -> dict[str, Any]:
    """Run one resumable repository-profile ingestion page."""

    return await _dispatch(context, "code_review", run_id, idempotency_key, "code_review_ingest")


@procrastinate_app.task(
    name="lifeagent.academic_planner",
    queue="academic_planner",
    retry=default_retry_strategy,
    pass_context=True,
)
async def academic_planner_task(
    context: JobContext, run_id: str, idempotency_key: str, kind: str = ""
) -> dict[str, Any]:
    return await _dispatch(context, "academic_planner", run_id, idempotency_key, kind)


@procrastinate_app.task(
    name="lifeagent.finance",
    queue="finance",
    retry=default_retry_strategy,
    pass_context=True,
)
async def finance_task(
    context: JobContext, run_id: str, idempotency_key: str, kind: str = ""
) -> dict[str, Any]:
    return await _dispatch(context, "finance", run_id, idempotency_key, kind)


def _create_scheduled_run(
    agent_name: str,
    key: str,
    schedule_name: str,
) -> tuple[UUID, str]:
    with Session(_database.engine) as session, session.begin():
        run = RunRepository.create_or_get(
            session,
            idempotency_key=key,
            agent_name=agent_name,
            trigger="schedule",
            schedule=schedule_name,
        )
        session.flush()
        return run.id, str(run.status)


async def _periodic_tick(
    *,
    timestamp: int,
    job_kind: str,
    schedule_name: str,
    schedule: TorontoPeriodicSchedule,
    task: Any,
) -> dict[str, object]:
    if job_kind not in _handlers:
        return {"status": "disabled_no_handler"}
    scheduled_at = datetime.fromtimestamp(timestamp, UTC)
    local_time = scheduled_at.astimezone(schedule.zone)
    if not schedule.matches(local_time):
        return {"status": "not_due"}
    occurrence = PeriodicOccurrence(local_time=local_time, scheduled_at=scheduled_at)
    key = stable_period_key(schedule_name, occurrence)
    run_id, status = await asyncio.to_thread(
        _create_scheduled_run,
        job_kind,
        key,
        schedule_name,
    )
    if status in {RunStatus.SUCCEEDED.value, RunStatus.CANCELLED.value}:
        return {"status": "already_complete", "run_id": str(run_id)}
    job_id = await defer_idempotent_async(task, str(run_id), key, job_kind)
    return {"status": "enqueued", "run_id": str(run_id), "job_id": job_id}


_settings = get_settings()


@procrastinate_app.periodic(cron="* * * * *", periodic_id="code-review-dynamic")
@procrastinate_app.task(name="lifeagent.schedule.code_review", queue="code_review")
async def code_review_periodic(timestamp: int) -> dict[str, object]:
    return await _periodic_tick(
        timestamp=timestamp,
        job_kind="code_review_daily",
        schedule_name="code-review-daily",
        schedule=TorontoPeriodicSchedule.from_time(_settings.code_review_schedule),
        task=code_review_daily_task,
    )


@procrastinate_app.periodic(cron="* * * * *", periodic_id="academic-morning-dynamic")
@procrastinate_app.task(name="lifeagent.schedule.academic_morning", queue="academic_planner")
async def academic_morning_periodic(timestamp: int) -> dict[str, object]:
    return await _periodic_tick(
        timestamp=timestamp,
        job_kind="academic_planner",
        schedule_name="academic-morning",
        schedule=TorontoPeriodicSchedule.from_time(_settings.academic_morning_schedule),
        task=academic_planner_task,
    )


@procrastinate_app.periodic(cron="* * * * *", periodic_id="academic-eod-dynamic")
@procrastinate_app.task(name="lifeagent.schedule.academic_eod", queue="academic_planner")
async def academic_eod_periodic(timestamp: int) -> dict[str, object]:
    return await _periodic_tick(
        timestamp=timestamp,
        job_kind="academic_planner",
        schedule_name="academic-end-of-day",
        schedule=TorontoPeriodicSchedule.from_time(_settings.academic_end_of_day_schedule),
        task=academic_planner_task,
    )


@procrastinate_app.periodic(cron="* * * * *", periodic_id="finance-market-open-dynamic")
@procrastinate_app.task(name="lifeagent.schedule.finance", queue="finance")
async def finance_periodic(timestamp: int) -> dict[str, object]:
    return await _periodic_tick(
        timestamp=timestamp,
        job_kind="finance",
        schedule_name="finance-market-open",
        schedule=TorontoPeriodicSchedule.from_time(
            _settings.finance_market_open_schedule,
            weekdays=frozenset(range(5)),
        ),
        task=finance_task,
    )


@procrastinate_app.periodic(cron="*/5 * * * *", periodic_id="shared-services-health")
@procrastinate_app.task(name="lifeagent.health.shared_services", queue="code_review")
async def shared_services_periodic(timestamp: int) -> dict[str, object]:
    """Persist deterministic shared-service health without invoking an agent or model."""

    evaluated_at = datetime.fromtimestamp(timestamp, UTC)
    database_checks = await asyncio.to_thread(check_database, _database)
    async with httpx.AsyncClient(
        timeout=min(_settings.connector_timeout_seconds, 10.0)
    ) as ollama_client:
        ollama_check = await check_ollama(_settings, ollama_client)
    checks = [
        *database_checks,
        await asyncio.to_thread(check_queue, _database),
        await asyncio.to_thread(check_artifact_root, _settings),
        check_connector_configuration(_settings),
        ollama_check,
    ]
    states = {check.state for check in checks}
    if HealthState.FAILED in states:
        processing = ProcessingStatus.FAILED
    elif HealthState.ATTENTION in states:
        processing = ProcessingStatus.ATTENTION
    else:
        processing = ProcessingStatus.SUCCEEDED
    exceptions = tuple(check for check in checks if check.state is not HealthState.HEALTHY)
    diagnostic = (
        ",".join(f"{check.name}={check.state.value}" for check in exceptions)
        if exceptions
        else f"all {len(checks)} shared-service probes are healthy"
    )[:120]
    with Session(_database.engine) as session, session.begin():
        prior_success = session.scalar(
            select(PersistedHealthCheck.last_success_at).where(
                PersistedHealthCheck.check_name == "shared_services"
            )
        )
        health = evaluate_and_persist(
            session,
            OperationalFacts(
                component="shared_services",
                processing=processing,
                delivery=HealthDeliveryStatus.NOT_REQUIRED,
                connector_authenticated=(
                    next(check for check in checks if check.name == "connector_configuration").state
                    is not HealthState.FAILED
                ),
                evaluated_at=evaluated_at,
                last_success_at=(
                    evaluated_at if processing is ProcessingStatus.SUCCEEDED else prior_success
                ),
                next_expected_at=evaluated_at + timedelta(minutes=5),
                diagnostic_code=diagnostic,
            ),
        )
    return {"status": health.state.value, "diagnostic": health.diagnostic}


__all__ = [
    "academic_planner_task",
    "code_review_daily_task",
    "code_review_ingest_task",
    "code_review_task",
    "defer_idempotent",
    "defer_idempotent_async",
    "finance_task",
    "register_task_handler",
    "shared_services_periodic",
]
