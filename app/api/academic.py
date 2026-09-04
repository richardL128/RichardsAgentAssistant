"""Academic planner HTTP boundaries: check-ins propose, confirmation writes."""

from __future__ import annotations

from typing import Literal, cast
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.agents.academic_planner.workflow import (
    confirm_checkin_proposal,
    create_checkin_proposal,
)

router = APIRouter(prefix="/academic", tags=["academic"])


class CheckinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reply: str = Field(min_length=1, max_length=4_000)
    plan_id: UUID | None = None


class CheckinResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["proposal_pending"]
    proposal_id: UUID
    confirmation_event: str
    change_count: int
    question: str | None


class ConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation_event: str = Field(min_length=1, max_length=255)


class ConfirmationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["applied", "confirmation_required", "not_found"]
    proposal_id: UUID
    change_count: int | None = None


@router.post("/checkin", response_model=CheckinResponse, status_code=202)
async def academic_checkin(
    request: Request, payload: CheckinRequest
) -> CheckinResponse | JSONResponse:
    """Persist a proposal and return its exact confirmation event."""

    store = getattr(request.app.state, "academic_store", None)
    if store is None:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    try:
        proposal = await create_checkin_proposal(
            store=store,
            reply=payload.reply,
            plan_id=payload.plan_id,
            model=getattr(request.app.state, "academic_model", None),
            delivery=getattr(request.app.state, "academic_delivery", None),
        )
    except ValueError:
        return JSONResponse(
            status_code=400,
            content={"status": "rejected", "error_code": "input_invalid"},
        )
    return CheckinResponse(
        status="proposal_pending",
        proposal_id=proposal.proposal_id,
        confirmation_event=proposal.confirmation_event,
        change_count=len(proposal.changes),
        question=proposal.question,
    )


@router.post("/confirm/{proposal_id}", response_model=ConfirmationResponse)
async def academic_confirmation(
    request: Request, proposal_id: UUID, payload: ConfirmationRequest
) -> ConfirmationResponse:
    """Apply a proposal only after exact confirmation token matching."""

    store = getattr(request.app.state, "academic_store", None)
    writer = getattr(request.app.state, "notion_writer", None)
    if store is None or writer is None:
        return ConfirmationResponse(status="not_found", proposal_id=proposal_id)
    result = await confirm_checkin_proposal(
        store=store,
        writer=writer,
        proposal_id=proposal_id,
        confirmation_event=payload.confirmation_event,
    )
    status = cast(Literal["applied", "confirmation_required", "not_found"], result["status"])
    change_count = result.get("change_count")
    return ConfirmationResponse(
        status=status,
        proposal_id=proposal_id,
        change_count=int(cast(int, change_count)) if change_count is not None else None,
    )


__all__ = [
    "CheckinRequest",
    "CheckinResponse",
    "ConfirmationRequest",
    "ConfirmationResponse",
    "academic_checkin",
    "academic_confirmation",
    "router",
]
