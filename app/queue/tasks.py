"""Named Procrastinate task entry points for the three isolated workers."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

import httpx
from procrastinate import JobContext
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.artifacts.store import ArtifactStore
from app.connectors.discord import deliver_failure_alert
from app.core.config import Settings, get_settings
from app.core.errors import ErrorCode, LifeAgentError
from app.db.models import Delivery, RunStatus
from app.db.models import HealthCheck as PersistedHealthCheck
from app.db.repositories import AuditRepository, RunRepository
from app.db.session import Database
from app.health.checks import (
    HealthCheck,
    HealthState,
    check_artifact_root,
    check_connector_configuration,
    check_connector_liveness,
    check_database,
    check_ollama,
    check_queue,
)
from app.health.evaluator import DeliveryStatus as HealthDeliveryStatus
from app.health.evaluator import OperationalFacts, OperationalHealth, ProcessingStatus
from app.health.service import evaluate_and_persist
from app.queue.app import JOB_KINDS, default_retry_strategy, procrastinate_app
from app.queue.execution import execute_recorded_attempt
from app.queue.idempotency import build_idempotency_key, validate_idempotency_key
from app.queue.periodic import PeriodicOccurrence, TorontoPeriodicSchedule, stable_period_key

TaskHandler = Callable[[str, str], Awaitable[dict[str, Any]]]
_handlers: dict[str, TaskHandler] = {}
_database = Database(get_settings())
_MODEL_LOCK = "ollama:exclusive"


class FailureAlertSender(Protocol):
    def __call__(
        self,
        *,
        engine: Engine,
        run_id: UUID,
        channel_id: str,
        component: str,
        state: HealthState,
        error_code: ErrorCode,
        attempt: int,
        attempt_limit: int,
        idempotency_key: str,
    ) -> Awaitable[Delivery]: ...


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


def _iter_artifact_sidecars(store: ArtifactStore) -> tuple[tuple[str, Path], ...]:
    candidates: list[tuple[str, Path]] = []
    for metadata_path in sorted(store.root.glob("**/*.json")):
        if metadata_path.is_symlink():
            continue
        try:
            relative = metadata_path.relative_to(store.root)
        except ValueError:
            continue
        if len(relative.parts) != 2:
            continue
        key = f"{relative.parts[0]}{metadata_path.stem}"
        candidates.append((key, metadata_path))
    return tuple(candidates)


def _prune_expired_artifacts(
    settings: Settings,
    database: Database,
    *,
    now: datetime,
) -> tuple[str, ...]:
    store = ArtifactStore(
        settings.artifact_root,
        default_retention_days=settings.artifact_retention_days,
    )
    current = now.astimezone(UTC)
    pruned: list[str] = []
    for key, metadata_path in _iter_artifact_sidecars(store):
        try:
            metadata = store.get_metadata(key)
            if metadata.key != key or not store.retention_candidate(key, now=current):
                continue
            artifact_path = metadata_path.with_suffix("")
            if artifact_path.is_symlink():
                continue
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        _append_artifact_prune_audit(database, key=key, result="prune_requested")
        try:
            artifact_path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
        except OSError:
            _append_artifact_prune_audit(database, key=key, result="prune_failed")
            continue
        _append_artifact_prune_audit(database, key=key, result="pruned")
        pruned.append(key)
    return tuple(pruned)


def _append_artifact_prune_audit(database: Database, *, key: str, result: str) -> None:
    with Session(database.engine) as session, session.begin():
        AuditRepository.append(
            session,
            actor="system",
            action="artifact.prune",
            target_type="artifact",
            target_id=key,
            result=result,
        )


def _check_queue_with_settings(
    database: Database, settings: Settings, now: datetime
) -> HealthCheck:
    try:
        return check_queue(
            database,
            now=now,
            stalled_after_seconds=settings.queue_stalled_after_seconds,
        )
    except TypeError:
        return check_queue(database)


def _shared_services_alert_channel(settings: Settings) -> str | None:
    if settings.discord_bot_token is None:
        return None
    candidates = (
        settings.discord_code_review_channel_id,
        settings.discord_finance_channel_id,
        settings.discord_academic_channel_id,
        *settings.discord_target_channels,
    )
    return next((channel for channel in candidates if channel), None)


def _shared_services_alert_required(
    health: OperationalHealth,
    checks: tuple[HealthCheck, ...],
) -> bool:
    if health.state is HealthState.FAILED:
        return True
    if health.rule in {"run_overdue", "waiting_for_retry"}:
        return True
    queue = next((check for check in checks if check.name == "queue"), None)
    return queue is not None and queue.state is HealthState.ATTENTION


def _shared_services_alert_error_code(checks: tuple[HealthCheck, ...]) -> ErrorCode:
    if any(
        check.state is HealthState.FAILED and check.name.endswith("_authentication")
        for check in checks
    ):
        return ErrorCode.AUTHORIZATION_INVALID
    if any(
        check.state is HealthState.FAILED and check.name == "github_installation_token"
        for check in checks
    ):
        return ErrorCode.AUTHORIZATION_INVALID
    if any(check.state is not HealthState.HEALTHY and check.name == "ollama" for check in checks):
        return ErrorCode.MODEL_TRANSIENT
    if any(check.state is not HealthState.HEALTHY and check.name == "queue" for check in checks):
        return ErrorCode.SCHEDULE_LATE
    if any(
        check.state is not HealthState.HEALTHY and "connector" in check.name for check in checks
    ):
        return ErrorCode.CONNECTOR_TRANSIENT
    return ErrorCode.INTERNAL


def _shared_services_alert_key(health: OperationalHealth) -> str:
    digest = hashlib.sha256(f"{health.rule}:{health.diagnostic}".encode()).hexdigest()[:24]
    return build_idempotency_key(
        "shared-services-alert",
        health.state.value,
        health.rule,
        digest,
    )


def _create_shared_services_alert_run(
    database: Database,
    *,
    key: str,
    health: OperationalHealth,
    error_code: ErrorCode,
) -> UUID:
    with Session(database.engine) as session, session.begin():
        run = RunRepository.create_or_get(
            session,
            idempotency_key=key,
            agent_name="shared_services",
            trigger="health_alert",
            schedule="shared-services-health",
            input_version=health.rule,
        )
        status = RunStatus.FAILED if health.state is HealthState.FAILED else RunStatus.ATTENTION
        RunRepository.set_status(
            session,
            run.id,
            status,
            summary=health.diagnostic,
            error_code=error_code.value,
        )
        return run.id


async def _maybe_send_shared_services_alert(
    *,
    database: Database,
    settings: Settings,
    health: OperationalHealth,
    checks: tuple[HealthCheck, ...],
    alert_sender: FailureAlertSender = deliver_failure_alert,
) -> dict[str, object]:
    if not _shared_services_alert_required(health, checks):
        return {"status": "not_required"}
    channel_id = _shared_services_alert_channel(settings)
    if channel_id is None:
        return {"status": "disabled"}
    error_code = _shared_services_alert_error_code(checks)
    key = _shared_services_alert_key(health)
    run_id = await asyncio.to_thread(
        _create_shared_services_alert_run,
        database,
        key=key,
        health=health,
        error_code=error_code,
    )
    try:
        delivery = await alert_sender(
            engine=database.engine,
            run_id=run_id,
            channel_id=channel_id,
            component="shared_services",
            state=health.state,
            error_code=error_code,
            attempt=0,
            attempt_limit=settings.retry_max_attempts,
            idempotency_key=key,
        )
    except LifeAgentError as exc:
        return {"status": "failed", "error_code": exc.record.code.value}
    except ValueError:
        return {"status": "failed", "error_code": ErrorCode.INPUT_INVALID.value}
    return {"status": str(delivery.status), "delivery_id": str(delivery.id)}


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


@procrastinate_app.periodic(cron="* * * * *", periodic_id="artifact-retention-dynamic")
@procrastinate_app.task(name="lifeagent.artifacts.retention", queue="code_review")
async def artifact_retention_periodic(timestamp: int) -> dict[str, object]:
    """Prune expired redacted artifacts on the configured local schedule."""

    scheduled_at = datetime.fromtimestamp(timestamp, UTC)
    schedule = TorontoPeriodicSchedule.from_time(_settings.artifact_retention_schedule)
    if not schedule.matches(scheduled_at.astimezone(schedule.zone)):
        return {"status": "not_due"}
    pruned = await asyncio.to_thread(
        _prune_expired_artifacts,
        _settings,
        _database,
        now=scheduled_at,
    )
    return {"status": "succeeded", "pruned": len(pruned)}


@procrastinate_app.periodic(cron="*/5 * * * *", periodic_id="shared-services-health")
@procrastinate_app.task(name="lifeagent.health.shared_services", queue="code_review")
async def shared_services_periodic(timestamp: int) -> dict[str, object]:
    """Persist deterministic shared-service health without invoking an agent or model."""

    evaluated_at = datetime.fromtimestamp(timestamp, UTC)
    database_checks = await asyncio.to_thread(check_database, _database)
    async with httpx.AsyncClient(
        timeout=min(_settings.connector_timeout_seconds, 10.0)
    ) as shared_client:
        ollama_check, connector_liveness_checks = await asyncio.gather(
            check_ollama(_settings, shared_client),
            check_connector_liveness(_settings, shared_client, now=evaluated_at),
        )
    connector_configuration = check_connector_configuration(_settings)
    checks = [
        *database_checks,
        await asyncio.to_thread(_check_queue_with_settings, _database, _settings, evaluated_at),
        await asyncio.to_thread(check_artifact_root, _settings),
        connector_configuration,
        *connector_liveness_checks,
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
                    connector_configuration.state is not HealthState.FAILED
                    and all(
                        check.state is not HealthState.FAILED for check in connector_liveness_checks
                    )
                ),
                evaluated_at=evaluated_at,
                last_success_at=(
                    evaluated_at if processing is ProcessingStatus.SUCCEEDED else prior_success
                ),
                next_expected_at=evaluated_at + timedelta(minutes=5),
                diagnostic_code=diagnostic,
            ),
        )
    alert = await _maybe_send_shared_services_alert(
        database=_database,
        settings=_settings,
        health=health,
        checks=tuple(checks),
    )
    return {"status": health.state.value, "diagnostic": health.diagnostic, "alert": alert}


__all__ = [
    "academic_planner_task",
    "artifact_retention_periodic",
    "code_review_daily_task",
    "code_review_ingest_task",
    "code_review_task",
    "defer_idempotent",
    "defer_idempotent_async",
    "finance_task",
    "register_task_handler",
    "shared_services_periodic",
]
