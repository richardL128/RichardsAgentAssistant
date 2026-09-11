"""Academic planner HTTP boundaries: confirmation writes and manual sync."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, cast
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.agents.academic_planner.proposal_review import (
    confirm_checkin_proposal,
    reject_checkin_proposal,
)

router = APIRouter(prefix="/academic", tags=["academic"])


class ConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation_event: str = Field(min_length=1, max_length=255)


class ConfirmationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["applied", "confirmation_required", "not_found"]
    proposal_id: UUID
    change_count: int | None = None


class RejectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rejection_event: str = Field(min_length=1, max_length=255)


class RejectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["rejected", "already_rejected", "rejection_required", "not_found"]
    proposal_id: UUID
    change_count: int | None = None


class AcademicSyncResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["succeeded", "partial", "setup_required", "failed"]
    course_count: int
    valid_course_count: int
    assessment_count: int
    archived_count: int
    clarification_count: int
    material_job_count: int = 0
    invalid_calendar_count: int
    diagnostic_codes: list[str]
    synced_at: datetime | None = None
    error_code: str | None = None
    retryable: bool = False


@router.post("/confirm/{proposal_id}", response_model=ConfirmationResponse)
async def academic_confirmation(
    request: Request, proposal_id: UUID, payload: ConfirmationRequest
) -> ConfirmationResponse | JSONResponse:
    """Apply a proposal only after exact confirmation token matching."""

    store = getattr(request.app.state, "academic_store", None)
    if payload.confirmation_event != f"confirm {proposal_id}":
        return ConfirmationResponse(
            status="confirmation_required",
            proposal_id=proposal_id,
        )
    writer = getattr(request.app.state, "notion_writer", None)
    if store is None or writer is None:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unavailable",
                "error_code": "notion_writer_unconfigured",
            },
        )
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


@router.post("/reject/{proposal_id}", response_model=RejectionResponse)
async def academic_rejection(
    request: Request, proposal_id: UUID, payload: RejectionRequest
) -> RejectionResponse | JSONResponse:
    """Reject a pending proposal without requiring or calling a Notion writer."""

    store = getattr(request.app.state, "academic_store", None)
    if store is None:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    result = reject_checkin_proposal(
        store=store,
        proposal_id=proposal_id,
        rejection_event=payload.rejection_event,
    )
    status_value = cast(
        Literal["rejected", "already_rejected", "rejection_required", "not_found"],
        result["status"],
    )
    change_count = result.get("change_count")
    return RejectionResponse(
        status=status_value,
        proposal_id=proposal_id,
        change_count=int(cast(int, change_count)) if change_count is not None else None,
    )


@router.post("/sync", response_model=AcademicSyncResponse)
async def academic_sync(request: Request) -> AcademicSyncResponse | JSONResponse:
    """Run the same idempotent Notion sync used before morning planning."""

    syncer = getattr(request.app.state, "academic_syncer", None)
    if syncer is None:
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "error_code": "academic_sync_unavailable"},
        )
    result = await syncer.sync()
    return AcademicSyncResponse.model_validate(result.as_dict())


__all__ = [
    "AcademicSyncResponse",
    "ConfirmationRequest",
    "ConfirmationResponse",
    "RejectionRequest",
    "RejectionResponse",
    "academic_confirmation",
    "academic_rejection",
    "academic_sync",
    "router",
]
