"""Named Procrastinate task entry points for the three isolated workers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from procrastinate import JobContext
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import RunStatus
from app.db.repositories import RunRepository
from app.db.session import Database
from app.queue.app import QUEUE_NAMES, default_retry_strategy, procrastinate_app
from app.queue.execution import execute_recorded_attempt
from app.queue.idempotency import validate_idempotency_key
from app.queue.periodic import PeriodicOccurrence, TorontoPeriodicSchedule, stable_period_key

TaskHandler = Callable[[str, str], Awaitable[dict[str, Any]]]
_handlers: dict[str, TaskHandler] = {}
_database = Database(get_settings())
_MODEL_LOCK = "ollama:exclusive"


def register_task_handler(task_name: str, handler: TaskHandler) -> None:
    """Register a workflow handler during worker startup, not module import."""

    if task_name not in QUEUE_NAMES:
        raise ValueError(f"unknown queue task: {task_name}")
    _handlers[task_name] = handler


async def _dispatch(
    context: JobContext,
    task_name: str,
    run_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    if not run_id.strip():
        raise ValueError("run_id must not be empty")
    validate_idempotency_key(idempotency_key)
    handler = _handlers.get(task_name)
    if handler is None:
        raise RuntimeError(f"no handler registered for {task_name}")
    parsed_run_id = UUID(run_id)

    async def operation() -> dict[str, object]:
        return await handler(run_id, idempotency_key)

    await execute_recorded_attempt(
        operation,
        engine=_database.engine,
        run_id=parsed_run_id,
        node_name=f"queue.{task_name}",
        attempt=context.job.attempts + 1,
        retry_policy=default_retry_strategy.policy,
    )
    return {"status": "succeeded", "run_id": run_id}


def defer_idempotent(task: Any, run_id: str, idempotency_key: str) -> Any:
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
    return task.configure(lock=_MODEL_LOCK, queueing_lock=idempotency_key).defer(
        run_id=run_id,
        idempotency_key=idempotency_key,
    )


async def defer_idempotent_async(task: Any, run_id: str, idempotency_key: str) -> Any:
    """Async form used by periodic deferrer tasks."""

    if not run_id.strip():
        raise ValueError("run_id must not be empty")
    validate_idempotency_key(idempotency_key)
    return await task.configure(lock=_MODEL_LOCK, queueing_lock=idempotency_key).defer_async(
        run_id=run_id,
        idempotency_key=idempotency_key,
    )


@procrastinate_app.task(
    name="lifeagent.code_review",
    queue="code_review",
    retry=default_retry_strategy,
    pass_context=True,
)
async def code_review_task(
    context: JobContext, run_id: str, idempotency_key: str
) -> dict[str, Any]:
    return await _dispatch(context, "code_review", run_id, idempotency_key)


@procrastinate_app.task(
    name="lifeagent.academic_planner",
    queue="academic_planner",
    retry=default_retry_strategy,
    pass_context=True,
)
async def academic_planner_task(
    context: JobContext, run_id: str, idempotency_key: str
) -> dict[str, Any]:
    return await _dispatch(context, "academic_planner", run_id, idempotency_key)


@procrastinate_app.task(
    name="lifeagent.finance",
    queue="finance",
    retry=default_retry_strategy,
    pass_context=True,
)
async def finance_task(context: JobContext, run_id: str, idempotency_key: str) -> dict[str, Any]:
    return await _dispatch(context, "finance", run_id, idempotency_key)


def _create_scheduled_run(
    task_name: str,
    key: str,
    schedule_name: str,
) -> tuple[UUID, str]:
    with Session(_database.engine) as session, session.begin():
        run = RunRepository.create_or_get(
            session,
            idempotency_key=key,
            agent_name=task_name,
            trigger="schedule",
            schedule=schedule_name,
        )
        session.flush()
        return run.id, str(run.status)


async def _periodic_tick(
    *,
    timestamp: int,
    task_name: str,
    schedule_name: str,
    schedule: TorontoPeriodicSchedule,
    task: Any,
) -> dict[str, object]:
    if task_name not in _handlers:
        return {"status": "disabled_no_handler"}
    scheduled_at = datetime.fromtimestamp(timestamp, UTC)
    local_time = scheduled_at.astimezone(schedule.zone)
    if not schedule.matches(local_time):
        return {"status": "not_due"}
    occurrence = PeriodicOccurrence(local_time=local_time, scheduled_at=scheduled_at)
    key = stable_period_key(schedule_name, occurrence)
    run_id, status = await asyncio.to_thread(
        _create_scheduled_run,
        task_name,
        key,
        schedule_name,
    )
    if status in {RunStatus.SUCCEEDED.value, RunStatus.CANCELLED.value}:
        return {"status": "already_complete", "run_id": str(run_id)}
    job_id = await defer_idempotent_async(task, str(run_id), key)
    return {"status": "enqueued", "run_id": str(run_id), "job_id": job_id}


_settings = get_settings()


@procrastinate_app.periodic(cron="* * * * *", periodic_id="code-review-dynamic")
@procrastinate_app.task(name="lifeagent.schedule.code_review", queue="code_review")
async def code_review_periodic(timestamp: int) -> dict[str, object]:
    return await _periodic_tick(
        timestamp=timestamp,
        task_name="code_review",
        schedule_name="code-review-daily",
        schedule=TorontoPeriodicSchedule.from_time(_settings.code_review_schedule),
        task=code_review_task,
    )


@procrastinate_app.periodic(cron="* * * * *", periodic_id="academic-morning-dynamic")
@procrastinate_app.task(name="lifeagent.schedule.academic_morning", queue="academic_planner")
async def academic_morning_periodic(timestamp: int) -> dict[str, object]:
    return await _periodic_tick(
        timestamp=timestamp,
        task_name="academic_planner",
        schedule_name="academic-morning",
        schedule=TorontoPeriodicSchedule.from_time(_settings.academic_morning_schedule),
        task=academic_planner_task,
    )


@procrastinate_app.periodic(cron="* * * * *", periodic_id="academic-eod-dynamic")
@procrastinate_app.task(name="lifeagent.schedule.academic_eod", queue="academic_planner")
async def academic_eod_periodic(timestamp: int) -> dict[str, object]:
    return await _periodic_tick(
        timestamp=timestamp,
        task_name="academic_planner",
        schedule_name="academic-end-of-day",
        schedule=TorontoPeriodicSchedule.from_time(_settings.academic_end_of_day_schedule),
        task=academic_planner_task,
    )


@procrastinate_app.periodic(cron="* * * * *", periodic_id="finance-market-open-dynamic")
@procrastinate_app.task(name="lifeagent.schedule.finance", queue="finance")
async def finance_periodic(timestamp: int) -> dict[str, object]:
    return await _periodic_tick(
        timestamp=timestamp,
        task_name="finance",
        schedule_name="finance-market-open",
        schedule=TorontoPeriodicSchedule.from_time(
            _settings.finance_market_open_schedule,
            weekdays=frozenset(range(5)),
        ),
        task=finance_task,
    )


__all__ = [
    "academic_planner_task",
    "code_review_task",
    "defer_idempotent",
    "defer_idempotent_async",
    "finance_task",
    "register_task_handler",
]
