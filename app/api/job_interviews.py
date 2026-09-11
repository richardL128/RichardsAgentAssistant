"""Read-only operations and explicit sync boundary for job interviews."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.agents.job_interviews.notion_mutations import (
    confirm_career_write,
    reject_career_write,
)

router = APIRouter(prefix="/job-interviews", tags=["job-interviews"])


class JobInterviewSyncResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["succeeded", "partial", "setup_required", "failed"]
    table_count: int = Field(ge=0)
    application_row_count: int = Field(ge=0)
    interview_count: int = Field(ge=0)
    unscheduled_interview_count: int = Field(ge=0)
    inactive_application_row_count: int = Field(ge=0)
    inactive_interview_count: int = Field(ge=0)
    diagnostic_codes: list[str]
    synced_at: datetime | None = None
    error_code: str | None = None
    retryable: bool = False


class JobInterviewHealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jobs_discovery_status: str
    last_successful_jobs_sync: datetime | None = None
    active_application_row_count: int = Field(ge=0)
    upcoming_interview_count: int = Field(ge=0)
    unresolved_clarification_count: int = Field(ge=0)
    failed_or_stale_plan_count: int = Field(ge=0)
    pending_write_proposal_count: int = Field(ge=0)
    last_interview_reminder_at: datetime | None = None
    research_provider_configured: bool


class CareerConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation_event: str = Field(min_length=1, max_length=255)


class CareerRejectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rejection_event: str = Field(min_length=1, max_length=255)


class CareerWriteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = Field(min_length=1, max_length=64)
    proposal_id: UUID


@router.post("/sync", response_model=JobInterviewSyncResponse)
async def sync_job_interviews(request: Request) -> JobInterviewSyncResponse | JSONResponse:
    syncer = getattr(request.app.state, "job_interview_syncer", None)
    if syncer is None:
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "error_code": "job_interview_sync_unavailable"},
        )
    result = await syncer.sync()
    return JobInterviewSyncResponse.model_validate(result.as_dict())


@router.get("/health", response_model=JobInterviewHealthResponse)
async def job_interview_health(request: Request) -> JobInterviewHealthResponse | JSONResponse:
    store = getattr(request.app.state, "job_interview_store", None)
    if store is None:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    summary: Mapping[str, Any] = store.health_summary()
    settings = request.app.state.settings
    return JobInterviewHealthResponse.model_validate(
        {
            **summary,
            "research_provider_configured": (
                settings.job_research_search_provider != "unconfigured"
                and settings.job_research_search_api_key is not None
            ),
        }
    )


@router.post("/confirm/{proposal_id}", response_model=CareerWriteResponse)
async def confirm_job_interview_write(
    request: Request,
    proposal_id: UUID,
    payload: CareerConfirmationRequest,
) -> CareerWriteResponse | JSONResponse:
    if payload.confirmation_event != f"confirm {proposal_id}":
        return CareerWriteResponse(status="confirmation_required", proposal_id=proposal_id)
    database = getattr(request.app.state, "database", None)
    writer = getattr(request.app.state, "job_interview_notion_writer", None)
    if database is None or writer is None:
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "error_code": "career_notion_writer_unconfigured"},
        )
    result = await confirm_career_write(
        engine=database.engine,
        writer=writer,
        proposal_id=proposal_id,
        confirmation_event=payload.confirmation_event,
        now=datetime.now(UTC),
    )
    return CareerWriteResponse.model_validate(result)


@router.post("/reject/{proposal_id}", response_model=CareerWriteResponse)
async def reject_job_interview_write(
    request: Request,
    proposal_id: UUID,
    payload: CareerRejectionRequest,
) -> CareerWriteResponse | JSONResponse:
    database = getattr(request.app.state, "database", None)
    if database is None:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return CareerWriteResponse.model_validate(
        reject_career_write(
            engine=database.engine,
            proposal_id=proposal_id,
            rejection_event=payload.rejection_event,
        )
    )


__all__ = [
    "CareerWriteResponse",
    "JobInterviewHealthResponse",
    "JobInterviewSyncResponse",
    "confirm_job_interview_write",
    "job_interview_health",
    "reject_job_interview_write",
    "router",
    "sync_job_interviews",
]
