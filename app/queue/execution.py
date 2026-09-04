"""Record every durable task attempt around an injected async operation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.core.errors import (
    ErrorCode,
    LifeAgentError,
    authorization_error,
    permanent_error,
    transient_error,
)
from app.db.models import RunStatus, StepStatus
from app.db.repositories import RunRepository, utc_now
from app.health.service import evaluate_run_and_persist
from app.queue.retry import RetryClassification, RetryPolicy


def _safe_error_code(exception: BaseException, classification: RetryClassification) -> str:
    if isinstance(exception, LifeAgentError):
        return exception.record.code.value
    return f"{classification.value}_failure"


def _safe_exception(
    exception: Exception,
    classification: RetryClassification,
) -> LifeAgentError:
    if isinstance(exception, LifeAgentError):
        return exception
    if classification is RetryClassification.AUTHORIZATION:
        return authorization_error()
    if classification is RetryClassification.TRANSIENT:
        return transient_error(ErrorCode.CONNECTOR_TRANSIENT, "task dependency is unavailable")
    return permanent_error(ErrorCode.INTERNAL, "task failed permanently")


def _record_started(engine: Engine, run_id: UUID, node_name: str, attempt: int) -> UUID:
    with Session(engine) as session, session.begin():
        step = RunRepository.create_step_attempt(
            session,
            run_id=run_id,
            node_name=node_name,
            attempt=attempt,
            status=StepStatus.RUNNING,
        )
        RunRepository.update_step(
            session,
            step.id,
            StepStatus.RUNNING,
            started_at=utc_now(),
            diagnostic="attempt_started",
        )
        RunRepository.set_status(session, run_id, RunStatus.RUNNING)
        return step.id


def _record_finished(
    engine: Engine,
    *,
    run_id: UUID,
    step_id: UUID,
    step_status: StepStatus,
    run_status: RunStatus,
    diagnostic: str,
    error_code: str | None,
    retry_attempt: int,
    retry_limit: int,
) -> None:
    from app.core.config import get_settings

    with Session(engine) as session, session.begin():
        RunRepository.update_step(
            session,
            step_id,
            step_status,
            ended_at=utc_now(),
            diagnostic=diagnostic,
        )
        RunRepository.set_status(
            session,
            run_id,
            run_status,
            error_code=error_code,
        )
        evaluate_run_and_persist(
            session,
            run_id=run_id,
            settings=get_settings(),
            retry_attempt=retry_attempt,
            retry_limit=retry_limit,
        )


def _result_status(
    result: dict[str, object],
    default: RunStatus,
) -> tuple[StepStatus, RunStatus, str, str | None]:
    status = str(result.get("status", ""))
    error_code = result.get("error_code")
    safe_error = str(error_code) if isinstance(error_code, str) and error_code else None
    if status in {"failed", "cancelled"}:
        return StepStatus.FAILED, RunStatus.FAILED, "attempt_failed", safe_error or status
    if status in {"attention", "approval_required", "confirmation_required"}:
        diagnostic = safe_error or status
        return StepStatus.ATTENTION, RunStatus.ATTENTION, diagnostic, diagnostic
    delivered = result.get("delivered")
    delivery_count = result.get("delivery_count")
    if (
        delivered is False
        or (isinstance(delivered, str) and delivered != "sent")
        or delivery_count == 0
    ):
        return (
            StepStatus.ATTENTION,
            RunStatus.ATTENTION,
            "delivery_not_completed",
            "delivery_not_completed",
        )
    return StepStatus.SUCCEEDED, default, "attempt_succeeded", None


async def execute_recorded_attempt(
    operation: Callable[[], Awaitable[dict[str, object]]],
    *,
    engine: Engine,
    run_id: UUID,
    node_name: str,
    attempt: int,
    retry_policy: RetryPolicy,
    success_status: RunStatus = RunStatus.SUCCEEDED,
) -> dict[str, object]:
    """Execute one attempt and persist start/outcome without raw error text."""

    if attempt < 1:
        raise ValueError("attempt must be positive")
    step_id = await asyncio.to_thread(_record_started, engine, run_id, node_name, attempt)
    try:
        result = await operation()
    except Exception as exc:
        classification = retry_policy.classify(exc)
        will_retry = (
            classification is RetryClassification.TRANSIENT and attempt < retry_policy.max_attempts
        )
        error_code = _safe_error_code(exc, classification)
        await asyncio.to_thread(
            _record_finished,
            engine,
            run_id=run_id,
            step_id=step_id,
            step_status=StepStatus.ATTENTION if will_retry else StepStatus.FAILED,
            run_status=RunStatus.ATTENTION if will_retry else RunStatus.FAILED,
            diagnostic="retry_scheduled" if will_retry else "attempt_failed",
            error_code=error_code,
            retry_attempt=attempt,
            retry_limit=retry_policy.max_attempts,
        )
        raise _safe_exception(exc, classification) from None
    step_status, run_status, diagnostic, error_code = _result_status(result, success_status)
    await asyncio.to_thread(
        _record_finished,
        engine,
        run_id=run_id,
        step_id=step_id,
        step_status=step_status,
        run_status=run_status,
        diagnostic=diagnostic,
        error_code=error_code,
        retry_attempt=attempt,
        retry_limit=retry_policy.max_attempts,
    )
    return result


__all__ = ["execute_recorded_attempt"]
