"""Read-only Phase 6 finance API surfaces."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.db.finance import FinanceRunFilterMetadata, FinanceSourceRecord

router = APIRouter(prefix="/finance", tags=["finance"])


class FinanceReadStore(Protocol):
    def source_approval_gate(self) -> bool: ...

    def list_source_records(self) -> tuple[FinanceSourceRecord, ...]: ...

    def list_run_filter_metadata(
        self, *, limit: int = 100
    ) -> tuple[FinanceRunFilterMetadata, ...]: ...


class SourceRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    name: str
    base_url: str
    classification: str
    entitlement: str
    license_note: str
    source_version: str
    allowlist_version: str
    enabled: bool
    approved_at: datetime | None
    health: str | None
    health_checked_at: datetime | None


class SourceGateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    sources: tuple[SourceRecordResponse, ...]


class RunFilterMetadataResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    generated_at: datetime
    status: str
    source_allowlist_version: str
    tickers: tuple[str, ...] = Field(max_length=100)
    themes: tuple[str, ...] = Field(max_length=50)


def _store(request: Request) -> FinanceReadStore | None:
    value = getattr(request.app.state, "finance_store", None)
    return cast(FinanceReadStore | None, value)


@router.get("/sources", response_model=SourceGateResponse)
async def finance_sources(request: Request) -> SourceGateResponse | JSONResponse:
    store = _store(request)
    if store is None:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    records = tuple(
        SourceRecordResponse(**asdict(record)) for record in store.list_source_records()
    )
    return SourceGateResponse(enabled=store.source_approval_gate(), sources=records)


@router.get("/runs/filters", response_model=tuple[RunFilterMetadataResponse, ...])
async def finance_run_filters(
    request: Request, limit: int = Query(default=100, ge=1, le=500)
) -> tuple[RunFilterMetadataResponse, ...] | JSONResponse:
    store = _store(request)
    if store is None:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return tuple(
        RunFilterMetadataResponse(**asdict(record))
        for record in store.list_run_filter_metadata(limit=limit)
    )


__all__ = [
    "RunFilterMetadataResponse",
    "SourceGateResponse",
    "SourceRecordResponse",
    "finance_run_filters",
    "finance_sources",
    "router",
]
