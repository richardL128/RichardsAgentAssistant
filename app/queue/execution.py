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
) -> None:
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
        )
        raise _safe_exception(exc, classification) from None
    await asyncio.to_thread(
        _record_finished,
        engine,
        run_id=run_id,
        step_id=step_id,
        step_status=StepStatus.SUCCEEDED,
        run_status=success_status,
        diagnostic="attempt_succeeded",
        error_code=None,
    )
    return result


__all__ = ["execute_recorded_attempt"]
