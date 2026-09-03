"""Small transaction-friendly repositories for shared durable records.

Repositories never commit a caller's transaction.  Callers can compose a run,
step, delivery, and audit write in one transaction and commit once at the
side-effect boundary.  Inputs are metadata and artifact keys; raw content is
deliberately not accepted by these APIs.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, NoResultFound
from sqlalchemy.orm import Session

from app.db.models import (
    AgentRun,
    ApprovalRequest,
    ApprovalState,
    AuditEvent,
    Delivery,
    DeliveryStatus,
    HealthCheck,
    HealthState,
    RunStatus,
    RunStep,
    StepStatus,
    UIAcknowledgement,
)


def utc_now() -> datetime:
    """Return an aware UTC timestamp for explicit lifecycle updates."""

    return datetime.now(UTC)


def _insert_or_get(
    session: Session,
    model: type[Any],
    values: dict[str, Any],
    conflict_columns: Sequence[str],
) -> Any:
    """Insert a row once and return the winner under PostgreSQL concurrency."""

    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        statement = (
            pg_insert(model)
            .values(**values)
            .on_conflict_do_nothing(index_elements=list(conflict_columns))
            .returning(model.id)
        )
        row = session.execute(statement).first()
        if row is not None:
            return session.get(model, row[0])
        filters = [getattr(model, column) == values[column] for column in conflict_columns]
        return session.execute(select(model).where(*filters)).scalar_one()

    # The fallback is useful for fast unit tests.  A nested transaction keeps a
    # duplicate-key race from poisoning the caller's outer transaction.
    filters = [getattr(model, column) == values[column] for column in conflict_columns]
    existing = session.execute(select(model).where(*filters)).scalar_one_or_none()
    if existing is not None:
        return existing
    instance = model(**values)
    try:
        with session.begin_nested():
            session.add(instance)
            session.flush()
    except IntegrityError:
        existing = session.execute(select(model).where(*filters)).scalar_one_or_none()
        if existing is None:
            raise
        return existing
    return instance


class RunRepository:
    """Create and advance durable agent runs and node attempts."""

    @staticmethod
    def create_or_get(
        session: Session,
        *,
        idempotency_key: str,
        agent_name: str,
        trigger: str,
        schedule: str | None = None,
        model_version: str | None = None,
        config_version: str | None = None,
        input_version: str | None = None,
        artifact_key: str | None = None,
    ) -> AgentRun:
        return _insert_or_get(
            session,
            AgentRun,
            {
                "idempotency_key": idempotency_key,
                "agent_name": agent_name,
                "trigger": trigger,
                "schedule": schedule,
                "model_version": model_version,
                "config_version": config_version,
                "input_version": input_version,
                "artifact_key": artifact_key,
            },
            ("idempotency_key",),
        )

    @staticmethod
    def set_status(
        session: Session,
        run_id: uuid.UUID,
        status: RunStatus,
        *,
        summary: str | None = None,
        error_code: str | None = None,
        artifact_key: str | None = None,
    ) -> AgentRun:
        now = utc_now()
        values: dict[str, Any] = {
            "status": status,
            "updated_at": now,
            "error_code": error_code,
        }
        if status in {
            RunStatus.SUCCEEDED,
            RunStatus.ATTENTION,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            values["finished_at"] = now
        if summary is not None:
            values["summary"] = summary
        if artifact_key is not None:
            values["artifact_key"] = artifact_key
        session.execute(update(AgentRun).where(AgentRun.id == run_id).values(**values))
        instance = session.get(AgentRun, run_id)
        if instance is None:
            raise NoResultFound(f"agent run {run_id} was not found")
        return instance

    @staticmethod
    def create_step_attempt(
        session: Session,
        *,
        run_id: uuid.UUID,
        node_name: str,
        attempt: int = 1,
        status: StepStatus = StepStatus.QUEUED,
        diagnostic: str | None = None,
        model_call_ref: str | None = None,
        artifact_key: str | None = None,
    ) -> RunStep:
        return _insert_or_get(
            session,
            RunStep,
            {
                "run_id": run_id,
                "node_name": node_name,
                "attempt": attempt,
                "status": status,
                "diagnostic": diagnostic,
                "model_call_ref": model_call_ref,
                "artifact_key": artifact_key,
            },
            ("run_id", "node_name", "attempt"),
        )

    @staticmethod
    def update_step(
        session: Session,
        step_id: uuid.UUID,
        status: StepStatus,
        *,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        diagnostic: str | None = None,
        model_call_ref: str | None = None,
        artifact_key: str | None = None,
    ) -> RunStep:
        values: dict[str, Any] = {"status": status, "updated_at": utc_now()}
        optional_values = {
            "started_at": started_at,
            "ended_at": ended_at,
            "diagnostic": diagnostic,
            "model_call_ref": model_call_ref,
            "artifact_key": artifact_key,
        }
        values.update({key: value for key, value in optional_values.items() if value is not None})
        session.execute(update(RunStep).where(RunStep.id == step_id).values(**values))
        instance = session.get(RunStep, step_id)
        if instance is None:
            raise NoResultFound(f"run step {step_id} was not found")
        return instance

    @staticmethod
    def list_steps(session: Session, run_id: uuid.UUID) -> list[RunStep]:
        statement: Select[tuple[RunStep]] = (
            select(RunStep)
            .where(RunStep.run_id == run_id)
            .order_by(RunStep.node_name, RunStep.attempt, RunStep.created_at)
        )
        return list(session.scalars(statement))


class DeliveryRepository:
    """Persist idempotent delivery intents and delivery attempts."""

    @staticmethod
    def create_or_get_intent(
        session: Session,
        *,
        channel: str,
        target: str,
        idempotency_key: str,
        run_id: uuid.UUID | None = None,
    ) -> Delivery:
        return _insert_or_get(
            session,
            Delivery,
            {
                "run_id": run_id,
                "channel": channel,
                "target": target,
                "idempotency_key": idempotency_key,
            },
            ("channel", "idempotency_key"),
        )

    @staticmethod
    def record_attempt(
        session: Session,
        delivery_id: uuid.UUID,
        status: DeliveryStatus,
        *,
        external_url: str | None = None,
        receipt_artifact_key: str | None = None,
        error_code: str | None = None,
        attempted_at: datetime | None = None,
    ) -> Delivery:
        delivery = session.get(Delivery, delivery_id)
        if delivery is None:
            raise NoResultFound(f"delivery {delivery_id} was not found")
        delivery.status = status
        delivery.attempt_count += 1
        delivery.last_attempt_at = attempted_at or utc_now()
        delivery.updated_at = utc_now()
        if external_url is not None:
            delivery.external_url = external_url
        if receipt_artifact_key is not None:
            delivery.receipt_artifact_key = receipt_artifact_key
        if error_code is not None:
            delivery.error_code = error_code
        session.flush()
        return delivery

    @staticmethod
    def get_by_idempotency(
        session: Session, *, channel: str, idempotency_key: str
    ) -> Delivery | None:
        return session.execute(
            select(Delivery).where(
                Delivery.channel == channel, Delivery.idempotency_key == idempotency_key
            )
        ).scalar_one_or_none()


class AuditRepository:
    """Append/read audit records; mutation APIs intentionally do not exist."""

    @staticmethod
    def append(
        session: Session,
        *,
        actor: str,
        action: str,
        target_type: str,
        target_id: str,
        result: str,
        run_id: uuid.UUID | None = None,
        artifact_key: str | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            actor=actor,
            action=action,
            target_type=target_type,
            target_id=target_id,
            result=result,
            run_id=run_id,
            artifact_key=artifact_key,
        )
        session.add(event)
        session.flush()
        return event

    @staticmethod
    def list_for_run(session: Session, run_id: uuid.UUID) -> list[AuditEvent]:
        return list(
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.run_id == run_id)
                .order_by(AuditEvent.created_at, AuditEvent.id)
            )
        )

    @staticmethod
    def list_for_target(session: Session, *, target_type: str, target_id: str) -> list[AuditEvent]:
        return list(
            session.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.target_type == target_type,
                    AuditEvent.target_id == target_id,
                )
                .order_by(AuditEvent.created_at, AuditEvent.id)
            )
        )


class ApprovalRepository:
    """Create approval requests and atomically audit state transitions."""

    @staticmethod
    def create_or_get(
        session: Session,
        *,
        idempotency_key: str,
        operation: str,
        requester: str,
        redacted_preview: str | None = None,
        run_id: uuid.UUID | None = None,
        expires_at: datetime | None = None,
        artifact_key: str | None = None,
    ) -> ApprovalRequest:
        return _insert_or_get(
            session,
            ApprovalRequest,
            {
                "idempotency_key": idempotency_key,
                "operation": operation,
                "requester": requester,
                "redacted_preview": redacted_preview,
                "run_id": run_id,
                "expires_at": expires_at,
                "artifact_key": artifact_key,
            },
            ("idempotency_key",),
        )

    @staticmethod
    def transition(
        session: Session,
        approval_id: uuid.UUID,
        state: ApprovalState,
        *,
        actor: str,
    ) -> tuple[ApprovalRequest, AuditEvent]:
        request = session.execute(
            select(ApprovalRequest).where(ApprovalRequest.id == approval_id).with_for_update()
        ).scalar_one()
        if state not in {
            ApprovalState.APPROVED,
            ApprovalState.REJECTED,
            ApprovalState.EXPIRED,
            ApprovalState.CANCELLED,
        }:
            raise ValueError("approval transition must target a terminal state")
        if request.state != ApprovalState.PENDING:
            if request.state == state and request.audit_event_id is not None:
                existing_event = session.get(AuditEvent, request.audit_event_id)
                if existing_event is not None:
                    return request, existing_event
            raise ValueError("approval request has already reached a terminal state")
        request.state = state
        request.decision = (
            state.value if state in {ApprovalState.APPROVED, ApprovalState.REJECTED} else None
        )
        request.updated_at = utc_now()
        event = AuditRepository.append(
            session,
            actor=actor,
            action=f"approval.{state.value}",
            target_type="approval_request",
            target_id=str(approval_id),
            result=state.value,
            run_id=request.run_id,
        )
        request.audit_event_id = event.id
        session.flush()
        return request, event


class HealthRepository:
    """Store deterministic health rule evaluations and query due checks."""

    @staticmethod
    def upsert(
        session: Session,
        *,
        check_name: str,
        rule: str,
        state: HealthState,
        last_success_at: datetime | None = None,
        next_due_at: datetime | None = None,
        diagnostic: str | None = None,
        artifact_key: str | None = None,
    ) -> HealthCheck:
        values = {
            "check_name": check_name,
            "rule": rule,
            "state": state,
            "last_success_at": last_success_at,
            "next_due_at": next_due_at,
            "checked_at": utc_now(),
            "diagnostic": diagnostic,
            "artifact_key": artifact_key,
        }
        bind = session.get_bind()
        if bind.dialect.name == "postgresql":
            statement = (
                pg_insert(HealthCheck)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["check_name"],
                    set_={key: values[key] for key in values if key != "check_name"},
                )
                .returning(HealthCheck.id)
            )
            row = session.execute(statement).first()
            if row is None:
                raise RuntimeError("health check upsert did not return a row")
            instance = session.get(HealthCheck, row[0])
            if instance is None:
                raise RuntimeError("health check upsert returned an unknown row")
            return instance
        instance = session.execute(
            select(HealthCheck).where(HealthCheck.check_name == check_name)
        ).scalar_one_or_none()
        if instance is None:
            instance = HealthCheck(**values)
            session.add(instance)
        else:
            for key, value in values.items():
                setattr(instance, key, value)
        session.flush()
        return instance

    @staticmethod
    def due(session: Session, *, at: datetime | None = None) -> list[HealthCheck]:
        now = at or utc_now()
        return list(
            session.scalars(
                select(HealthCheck)
                .where(
                    HealthCheck.next_due_at.is_(None) | (HealthCheck.next_due_at <= now),
                )
                .order_by(HealthCheck.check_name)
            )
        )


class UIAcknowledgementRepository:
    """Persist presentation-layer acknowledgements without changing runs."""

    @staticmethod
    def acknowledge(
        session: Session,
        *,
        user_id: str,
        alert_key: str,
        run_id: uuid.UUID | None = None,
    ) -> UIAcknowledgement:
        return _insert_or_get(
            session,
            UIAcknowledgement,
            {"user_id": user_id, "alert_key": alert_key, "run_id": run_id},
            ("user_id", "run_id", "alert_key"),
        )


__all__ = [
    "ApprovalRepository",
    "AuditRepository",
    "DeliveryRepository",
    "HealthRepository",
    "RunRepository",
    "UIAcknowledgementRepository",
]
