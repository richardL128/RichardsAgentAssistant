"""Named Procrastinate task entry points for the academic worker."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import UUID

import httpx
from procrastinate import JobContext
from procrastinate.exceptions import AlreadyEnqueued
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
from app.health.service import evaluate_academic_morning_health, evaluate_and_persist
from app.queue.app import default_retry_strategy, procrastinate_app
from app.queue.execution import execute_recorded_attempt
from app.queue.idempotency import build_idempotency_key, validate_idempotency_key
from app.queue.periodic import PeriodicOccurrence, TorontoPeriodicSchedule, stable_period_key

AcademicClarificationAction = Literal[
    "quiz",
    "assignment",
    "tutorial",
    "lab",
    "studying_block",
    "ignore",
]
AcademicClarificationHandler = Callable[
    [str, AcademicClarificationAction, str, int, int],
    Awaitable[dict[str, Any]],
]
AcademicClarificationStatusHandler = Callable[
    [str, AcademicClarificationAction, int, int],
    Awaitable[dict[str, Any]],
]
AcademicMaterialIngestionHandler = Callable[[str, str], Awaitable[dict[str, object]]]
DiscordWakeHandler = Callable[[str, int, int], Awaitable[dict[str, Any]]]
AcademicMorningNotificationHandler = Callable[
    [str, str, str, int, int],
    Awaitable[dict[str, Any]],
]
_academic_clarification_handler: AcademicClarificationHandler | None = None
_academic_clarification_status_handler: AcademicClarificationStatusHandler | None = None
_academic_material_ingestion_handler: AcademicMaterialIngestionHandler | None = None
_discord_wake_handler: DiscordWakeHandler | None = None
_academic_morning_notification_handler: AcademicMorningNotificationHandler | None = None
_database = Database(get_settings())
_MODEL_LOCK = "ollama:exclusive"
_ACADEMIC_CLARIFICATION_LOCK_PREFIX = "academic-clarification"
_ACADEMIC_MORNING_SCHEDULE_NAME = "academic-morning"
_MATERIAL_PAGE_ID = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")
_SHA256_HEX = re.compile(r"\A[0-9a-f]{64}\Z")


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


def register_academic_clarification_handler(handler: AcademicClarificationHandler) -> None:
    """Register the Discord clarification apply handler during worker startup."""

    global _academic_clarification_handler
    _academic_clarification_handler = handler


def register_academic_clarification_status_handler(
    handler: AcademicClarificationStatusHandler,
) -> None:
    """Register the Discord clarification status-edit handler during worker startup."""

    global _academic_clarification_status_handler
    _academic_clarification_status_handler = handler


def register_academic_material_ingestion_handler(
    handler: AcademicMaterialIngestionHandler,
) -> None:
    """Register the private assessment-material ingestion boundary."""

    global _academic_material_ingestion_handler
    _academic_material_ingestion_handler = handler


def register_discord_wake_handler(handler: DiscordWakeHandler) -> None:
    """Register the durable ID-only Discord worker boundary."""

    global _discord_wake_handler
    _discord_wake_handler = handler


def register_academic_morning_notification_handler(
    handler: AcademicMorningNotificationHandler,
) -> None:
    """Register the focused model-free scheduled morning notifier."""

    global _academic_morning_notification_handler
    _academic_morning_notification_handler = handler


async def defer_discord_wake(wake_id: str) -> Any:
    """Enqueue one durable inbound row without raw content or credentials."""

    parsed_wake_id = str(UUID(wake_id))
    queueing_lock = f"discord-wake:{parsed_wake_id}"
    try:
        return await discord_wake_task.configure(
            lock=_MODEL_LOCK,
            queueing_lock=queueing_lock,
        ).defer_async(wake_id=parsed_wake_id)
    except AlreadyEnqueued:
        return {"status": "already_enqueued", "wake_id": parsed_wake_id}


async def defer_academic_material_ingestion(
    assessment_page_id: str,
    source_fingerprint: str,
) -> Any:
    """Queue an ingestion using only a page ID and stable metadata fingerprint."""

    if _MATERIAL_PAGE_ID.fullmatch(assessment_page_id) is None:
        raise ValueError("assessment page ID is invalid")
    if _SHA256_HEX.fullmatch(source_fingerprint) is None:
        raise ValueError("assessment material fingerprint is invalid")
    lock = f"academic-material:{assessment_page_id}:{source_fingerprint}"
    try:
        return await academic_material_ingestion_task.configure(
            lock=_MODEL_LOCK,
            queueing_lock=lock,
        ).defer_async(
            assessment_page_id=assessment_page_id,
            source_fingerprint=source_fingerprint,
        )
    except AlreadyEnqueued:
        return {"status": "already_enqueued", "assessment_page_id": assessment_page_id}


def _run_status_value(status: object) -> str:
    return getattr(status, "value", str(status))


def _create_academic_morning_run(
    *,
    period_key: str,
    occurrence_at: datetime,
) -> tuple[UUID, str]:
    with Session(_database.engine) as session, session.begin():
        run = RunRepository.create_or_get(
            session,
            idempotency_key=period_key,
            agent_name="academic_morning_notification",
            trigger="schedule",
            schedule=_ACADEMIC_MORNING_SCHEDULE_NAME,
            input_version=occurrence_at.isoformat(),
        )
        session.flush()
        return run.id, _run_status_value(run.status)


def _terminal_academic_morning_status(status: str) -> bool:
    return status in {
        RunStatus.SUCCEEDED.value,
        RunStatus.ATTENTION.value,
        RunStatus.FAILED.value,
        RunStatus.CANCELLED.value,
    }


async def defer_academic_morning_notification(occurrence: PeriodicOccurrence) -> Any:
    """Queue one model-free morning notification for a stable local period."""

    period_key = stable_period_key(_ACADEMIC_MORNING_SCHEDULE_NAME, occurrence)
    validate_idempotency_key(period_key)
    run_id, status = await asyncio.to_thread(
        _create_academic_morning_run,
        period_key=period_key,
        occurrence_at=occurrence.scheduled_at,
    )
    if _terminal_academic_morning_status(status):
        return {
            "status": "already_complete",
            "run_id": str(run_id),
            "period_key": period_key,
        }
    try:
        job_id = await academic_morning_notification_task.configure(
            queueing_lock=period_key,
        ).defer_async(
            occurrence_at_iso=occurrence.scheduled_at.isoformat(),
            run_id=str(run_id),
            period_key=period_key,
        )
    except AlreadyEnqueued:
        return {
            "status": "already_enqueued",
            "run_id": str(run_id),
            "period_key": period_key,
        }
    return {
        "status": "enqueued",
        "run_id": str(run_id),
        "period_key": period_key,
        "job_id": job_id,
    }


def _academic_clarification_args(
    clarification_id: str,
    action: str,
) -> tuple[str, AcademicClarificationAction, str]:
    parsed_clarification_id = str(UUID(str(clarification_id)))
    if action not in {
        "quiz",
        "assignment",
        "tutorial",
        "lab",
        "studying_block",
        "ignore",
    }:
        raise ValueError("academic clarification action is invalid")
    queueing_lock = f"{_ACADEMIC_CLARIFICATION_LOCK_PREFIX}:{parsed_clarification_id}"
    return parsed_clarification_id, cast(AcademicClarificationAction, action), queueing_lock


async def defer_academic_clarification(
    *,
    clarification_id: str,
    action: str,
    user_id: str,
) -> Any:
    """Queue one Discord clarification decision without taking the model lock."""

    parsed_clarification_id, parsed_action, queueing_lock = _academic_clarification_args(
        clarification_id,
        action,
    )
    if not user_id.strip():
        raise ValueError("user_id must not be empty")
    try:
        return await academic_clarification_task.configure(
            lock=queueing_lock,
            queueing_lock=queueing_lock,
        ).defer_async(
            clarification_id=parsed_clarification_id,
            action=parsed_action,
            user_id=user_id,
        )
    except AlreadyEnqueued:
        return {
            "status": "already_enqueued",
            "clarification_id": parsed_clarification_id,
        }


async def defer_academic_clarification_status(
    *,
    clarification_id: str,
    action: str,
) -> Any:
    """Queue the terminal Discord message edit with its own retry budget."""

    parsed_clarification_id, parsed_action, queueing_lock = _academic_clarification_args(
        clarification_id,
        action,
    )
    status_lock = queueing_lock.replace(
        f"{_ACADEMIC_CLARIFICATION_LOCK_PREFIX}:",
        f"{_ACADEMIC_CLARIFICATION_LOCK_PREFIX}:status:",
        1,
    )
    try:
        return await academic_clarification_status_task.configure(
            lock=status_lock,
            queueing_lock=status_lock,
        ).defer_async(
            clarification_id=parsed_clarification_id,
            action=parsed_action,
        )
    except AlreadyEnqueued:
        return {
            "status": "already_enqueued",
            "clarification_id": parsed_clarification_id,
        }


@procrastinate_app.task(
    name="lifeagent.discord_academic",
    queue="academic_planner",
    retry=default_retry_strategy,
    pass_context=True,
)
async def discord_wake_task(context: JobContext, wake_id: str) -> dict[str, Any]:
    """Run one accepted Discord event by durable row ID under the model lock."""

    if _discord_wake_handler is None:
        raise RuntimeError("no handler registered for discord_academic")
    return await _discord_wake_handler(
        str(UUID(wake_id)),
        context.job.attempts + 1,
        default_retry_strategy.policy.max_attempts,
    )


@procrastinate_app.task(
    name="lifeagent.academic_clarification",
    queue="academic_planner",
    retry=default_retry_strategy,
    pass_context=True,
)
async def academic_clarification_task(
    context: JobContext,
    clarification_id: str,
    action: AcademicClarificationAction,
    user_id: str,
) -> dict[str, Any]:
    if _academic_clarification_handler is None:
        raise RuntimeError("no handler registered for academic_clarification")
    return await _academic_clarification_handler(
        str(UUID(str(clarification_id))),
        action,
        user_id,
        context.job.attempts + 1,
        default_retry_strategy.policy.max_attempts,
    )


@procrastinate_app.task(
    name="lifeagent.academic_clarification_status",
    queue="academic_planner",
    retry=default_retry_strategy,
    pass_context=True,
)
async def academic_clarification_status_task(
    context: JobContext,
    clarification_id: str,
    action: AcademicClarificationAction,
) -> dict[str, Any]:
    if _academic_clarification_status_handler is None:
        raise RuntimeError("no handler registered for academic_clarification_status")
    return await _academic_clarification_status_handler(
        str(UUID(str(clarification_id))),
        action,
        context.job.attempts + 1,
        default_retry_strategy.policy.max_attempts,
    )


@procrastinate_app.task(
    name="lifeagent.academic_material_ingestion",
    queue="academic_planner",
    retry=default_retry_strategy,
    pass_context=True,
)
async def academic_material_ingestion_task(
    context: JobContext,
    assessment_page_id: str,
    source_fingerprint: str,
) -> dict[str, object]:
    """Refresh and ingest one assessment page without raw queue arguments."""

    del context
    if _academic_material_ingestion_handler is None:
        raise RuntimeError("no handler registered for academic_material_ingestion")
    return await _academic_material_ingestion_handler(assessment_page_id, source_fingerprint)


def _parse_occurrence_at_iso(value: str) -> datetime:
    try:
        occurrence_at = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("occurrence_at_iso must be a valid ISO datetime") from None
    if occurrence_at.tzinfo is None or occurrence_at.utcoffset() is None:
        raise ValueError("occurrence_at_iso must be timezone-aware")
    return occurrence_at.astimezone(UTC)


@procrastinate_app.task(
    name="lifeagent.academic_morning_notification",
    queue="academic_planner",
    retry=default_retry_strategy,
    pass_context=True,
)
async def academic_morning_notification_task(
    context: JobContext,
    occurrence_at_iso: str,
    run_id: str,
    period_key: str,
) -> dict[str, Any]:
    """Execute one anchored model-free morning notification attempt."""

    if _academic_morning_notification_handler is None:
        raise RuntimeError("no handler registered for academic_morning_notification")
    handler = _academic_morning_notification_handler
    parsed_run_id = UUID(run_id)
    parsed_period_key = validate_idempotency_key(period_key)
    occurrence_at = _parse_occurrence_at_iso(occurrence_at_iso)
    attempt = context.job.attempts + 1
    attempt_limit = default_retry_strategy.policy.max_attempts

    async def operation() -> dict[str, object]:
        return await handler(
            occurrence_at.isoformat(),
            str(parsed_run_id),
            parsed_period_key,
            attempt,
            attempt_limit,
        )

    result = await execute_recorded_attempt(
        operation,
        engine=_database.engine,
        run_id=parsed_run_id,
        node_name="queue.academic_morning_notification",
        attempt=attempt,
        retry_policy=default_retry_strategy.policy,
    )
    response = dict(result)
    response.setdefault("run_id", str(parsed_run_id))
    response.setdefault("period_key", parsed_period_key)
    return response


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
    morning = next((check for check in checks if check.name == "academic_morning"), None)
    return (queue is not None and queue.state is HealthState.ATTENTION) or (
        morning is not None and morning.state is HealthState.ATTENTION
    )


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
        check.state is not HealthState.HEALTHY and check.name == "academic_morning"
        for check in checks
    ):
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


@procrastinate_app.periodic(cron="* * * * *", periodic_id="academic-morning-notification")
@procrastinate_app.task(
    name="lifeagent.schedule.academic_morning_notification",
    queue="academic_planner",
)
async def academic_morning_notification_periodic(timestamp: int) -> dict[str, object]:
    """Defer the configured Toronto-local morning notification with bounded catch-up."""

    evaluated_at = datetime.fromtimestamp(timestamp, UTC)
    schedule = TorontoPeriodicSchedule.from_time(
        _settings.academic_morning_schedule,
        timezone_name=_settings.app_timezone,
    )
    grace = timedelta(minutes=_settings.academic_morning_catchup_grace_minutes)
    occurrence = schedule.due_within_grace(evaluated_at, grace=grace)
    if occurrence is None:
        return {"status": "not_due"}
    return await defer_academic_morning_notification(occurrence)


@procrastinate_app.periodic(cron="* * * * *", periodic_id="artifact-retention-dynamic")
@procrastinate_app.task(name="lifeagent.artifacts.retention", queue="academic_planner")
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
@procrastinate_app.task(name="lifeagent.health.shared_services", queue="academic_planner")
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
    with Session(_database.engine) as session, session.begin():
        academic_morning_health = evaluate_academic_morning_health(
            session,
            settings=_settings,
            evaluated_at=evaluated_at,
        )
    checks = [
        *database_checks,
        await asyncio.to_thread(_check_queue_with_settings, _database, _settings, evaluated_at),
        await asyncio.to_thread(check_artifact_root, _settings),
        connector_configuration,
        HealthCheck(
            name="academic_morning",
            state=academic_morning_health.state,
            diagnostic=academic_morning_health.diagnostic,
        ),
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
    "academic_clarification_status_task",
    "academic_clarification_task",
    "academic_material_ingestion_task",
    "academic_morning_notification_periodic",
    "academic_morning_notification_task",
    "artifact_retention_periodic",
    "defer_academic_clarification",
    "defer_academic_clarification_status",
    "defer_academic_material_ingestion",
    "defer_academic_morning_notification",
    "defer_discord_wake",
    "discord_wake_task",
    "register_academic_clarification_handler",
    "register_academic_clarification_status_handler",
    "register_academic_material_ingestion_handler",
    "register_academic_morning_notification_handler",
    "register_discord_wake_handler",
    "shared_services_periodic",
]
