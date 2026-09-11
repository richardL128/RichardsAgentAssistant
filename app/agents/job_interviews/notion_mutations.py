"""Exact-preview, confirmation-gated Notion writes for interview records."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.connectors.notion import NotionConnector, NotionWriteReceipt
from app.db.job_interviews import (
    CareerWriteProposalInput,
    JobInterviewRepository,
)
from app.db.models import CareerInterviewEvent, CareerJobsWorkspace, CareerPreparationPlan

_PROPOSAL_NAMESPACE = uuid.UUID("8c7f530c-f946-4f63-95e5-a0fd868003e9")


class CareerNotionWriter(Protocol):
    async def apply(self, proposal: dict[str, Any]) -> NotionWriteReceipt: ...


class DiscoveredCareerNotionWriter:
    """Resolve only persisted interview targets and LifeAgent-owned write fields."""

    def __init__(self, *, engine: Engine, connector: NotionConnector) -> None:
        self._engine = engine
        self._connector = connector

    async def apply(self, proposal: dict[str, Any]) -> NotionWriteReceipt:
        target_page_id = str(proposal["target_page_id"])
        with Session(self._engine) as session:
            interview = session.scalar(
                select(CareerInterviewEvent).where(
                    CareerInterviewEvent.interview_page_id == target_page_id,
                    CareerInterviewEvent.active.is_(True),
                    CareerInterviewEvent.archived.is_(False),
                )
            )
            if interview is None:
                raise ValueError("interview write target is no longer active")
            workspace = session.get(CareerJobsWorkspace, interview.workspace_id)
            if workspace is None or not workspace.date_property_id:
                raise ValueError("interview Date property is not safely discovered")
            expected = proposal.get("expected_last_edited_at")
            if not isinstance(expected, datetime):
                raise ValueError("interview write precondition is missing")
            operation = str(proposal["operation"])
            payload = proposal.get("payload")
            if not isinstance(payload, dict):
                raise ValueError("career write payload is invalid")
            proposal_id = str(proposal["id"])
            date_property_id = workspace.date_property_id
            interview_id = interview.id
            typed_payload = cast(dict[str, Any], payload)
        if operation == "interview_date":
            value = typed_payload.get("date_start")
            if not isinstance(value, str):
                raise ValueError("interview Date proposal is invalid")
            return await self._connector.guarded_update_interview_date(
                proposal_id=proposal_id,
                page_id=target_page_id,
                date_property_id=date_property_id,
                expected_last_edited_at=expected,
                due=value,
            )
        if operation == "preparation_plan":
            revision = typed_payload.get("plan_revision")
            text = typed_payload.get("plan_text")
            if not isinstance(revision, int) or not isinstance(text, str):
                raise ValueError("preparation-plan proposal is invalid")
            with Session(self._engine) as session:
                current = session.scalar(
                    select(CareerPreparationPlan).where(
                        CareerPreparationPlan.interview_id == interview_id
                    )
                )
                if current is None or current.revision != revision:
                    raise ValueError("preparation plan changed since the preview")
            return await self._connector.append_interview_preparation_plan(
                proposal_id=proposal_id,
                page_id=target_page_id,
                date_property_id=date_property_id,
                expected_last_edited_at=expected,
                plan_revision=revision,
                plan_text=text,
            )
        raise ValueError("career write operation is not allowlisted")


def propose_interview_date_write(
    *,
    engine: Engine,
    interview_page_id: str,
    proposed_date: datetime | str,
    requester: str,
    idempotency_key: str,
    now: datetime,
    ttl_hours: int = 24,
) -> dict[str, Any]:
    """Persist an immutable Date preview without calling Notion."""

    with Session(engine) as session, session.begin():
        interview = _active_interview(session, interview_page_id)
        date_text = (
            proposed_date.isoformat() if isinstance(proposed_date, datetime) else proposed_date
        )
        if not date_text.strip() or len(date_text) > 100:
            raise ValueError("proposed interview date is invalid")
        proposal_id = uuid.uuid5(_PROPOSAL_NAMESPACE, idempotency_key)
        token = f"confirm {proposal_id}"
        preview = (
            f'Interview: "{interview.title}"\nNew Date: {date_text}\n'
            "No other Notion field will change."
        )
        row = JobInterviewRepository.save_write_proposal(
            session,
            proposal=CareerWriteProposalInput(
                idempotency_key=idempotency_key,
                operation="interview_date",
                target_page_id=interview.interview_page_id,
                interview_page_id=interview.interview_page_id,
                expected_last_edited_at=_stored_utc(interview.notion_last_edited_at),
                payload={"date_start": date_text},
                redacted_preview=preview,
                confirmation_token=token,
                requester=requester,
                expires_at=_aware(now) + timedelta(hours=ttl_hours),
            ),
        )
        return _proposal_result(row)


def propose_preparation_plan_write(
    *,
    engine: Engine,
    interview_page_id: str,
    requester: str,
    idempotency_key: str,
    now: datetime,
    ttl_hours: int = 24,
) -> dict[str, Any]:
    """Persist the exact current-plan revision destined for an isolated child page."""

    with Session(engine) as session, session.begin():
        interview = _active_interview(session, interview_page_id)
        plan = session.scalar(
            select(CareerPreparationPlan).where(CareerPreparationPlan.interview_id == interview.id)
        )
        if plan is None:
            raise ValueError("interview has no maintained preparation plan")
        plan_text = _render_plan(plan.plan_payload)
        proposal_id = uuid.uuid5(_PROPOSAL_NAMESPACE, idempotency_key)
        token = f"confirm {proposal_id}"
        preview = (
            f'Interview: "{interview.title}"\n'
            f"Destination: Interview Preparation — LifeAgent child page\n"
            f"Plan revision: {plan.revision}\n{plan_text}"
        )[:4_000]
        row = JobInterviewRepository.save_write_proposal(
            session,
            proposal=CareerWriteProposalInput(
                idempotency_key=idempotency_key,
                operation="preparation_plan",
                target_page_id=interview.interview_page_id,
                interview_page_id=interview.interview_page_id,
                expected_last_edited_at=_stored_utc(interview.notion_last_edited_at),
                payload={"plan_revision": plan.revision, "plan_text": plan_text},
                redacted_preview=preview,
                confirmation_token=token,
                requester=requester,
                expires_at=_aware(now) + timedelta(hours=ttl_hours),
            ),
        )
        return _proposal_result(row)


async def confirm_career_write(
    *,
    engine: Engine,
    writer: CareerNotionWriter,
    proposal_id: uuid.UUID,
    confirmation_event: str,
    now: datetime,
) -> dict[str, Any]:
    """Consume an exact confirmation and apply one idempotent guarded operation."""

    with Session(engine) as session, session.begin():
        proposal = JobInterviewRepository.get_write_proposal(session, proposal_id=proposal_id)
        if proposal is None:
            return {"status": "not_found", "proposal_id": str(proposal_id)}
        status, _ = JobInterviewRepository.confirm_write_proposal(
            session,
            proposal_id=proposal_id,
            confirmation_token=confirmation_event,
            confirmation_event=confirmation_event,
            now=now,
        )
        if status not in {"confirmed", "already_confirmed", "applied"}:
            return {"status": status, "proposal_id": str(proposal_id)}
        if status == "applied":
            return {"status": "applied", "proposal_id": str(proposal_id)}
        payload_hash = _payload_hash(proposal)
        receipt_status, _ = JobInterviewRepository.begin_write_receipt(
            session,
            proposal_id=proposal_id,
            operation_id=str(proposal["operation"]),
            payload_hash=payload_hash,
        )
        if receipt_status == "already_applied":
            return {"status": "applied", "proposal_id": str(proposal_id)}
        if receipt_status in {"in_progress", "uncertain"}:
            return {"status": receipt_status, "proposal_id": str(proposal_id)}
    try:
        receipt = await writer.apply(proposal)
    except Exception:
        with Session(engine) as session, session.begin():
            JobInterviewRepository.mark_write_receipt_uncertain(
                session,
                proposal_id=proposal_id,
                operation_id=str(proposal["operation"]),
                payload_hash=payload_hash,
                error_code="career_notion_write_unverified",
            )
        raise
    with Session(engine) as session, session.begin():
        JobInterviewRepository.mark_write_receipt_applied(
            session,
            proposal_id=proposal_id,
            operation_id=str(proposal["operation"]),
            payload_hash=payload_hash,
            receipt=receipt.model_dump(mode="json", exclude_none=True),
            applied_at=now,
        )
    return {"status": "applied", "proposal_id": str(proposal_id)}


def reject_career_write(
    *, engine: Engine, proposal_id: uuid.UUID, rejection_event: str
) -> dict[str, Any]:
    with Session(engine) as session, session.begin():
        proposal = JobInterviewRepository.get_write_proposal(session, proposal_id=proposal_id)
        if proposal is None:
            return {"status": "not_found", "proposal_id": str(proposal_id)}
        row = JobInterviewRepository.reject_write_proposal(
            session,
            proposal_id=proposal_id,
            confirmation_event=rejection_event,
        )
        return {"status": row.state, "proposal_id": str(proposal_id)}


def _active_interview(session: Session, page_id: str) -> CareerInterviewEvent:
    interview = session.scalar(
        select(CareerInterviewEvent).where(
            CareerInterviewEvent.interview_page_id == page_id,
            CareerInterviewEvent.active.is_(True),
            CareerInterviewEvent.archived.is_(False),
        )
    )
    if interview is None:
        raise ValueError("active interview was not found")
    return interview


def _proposal_result(row: Any) -> dict[str, Any]:
    return {
        "proposal_id": str(row.id),
        "preview": row.redacted_preview,
        "confirmation_token": row.confirmation_token,
        "status": row.state,
    }


def _payload_hash(proposal: dict[str, Any]) -> str:
    body = json.dumps(proposal["payload"], ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _render_plan(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)[:12_000]


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _stored_utc(value: datetime) -> datetime:
    """Restore SQL backends that return UTC columns without tzinfo."""

    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "CareerNotionWriter",
    "DiscoveredCareerNotionWriter",
    "confirm_career_write",
    "propose_interview_date_write",
    "propose_preparation_plan_write",
    "reject_career_write",
]
