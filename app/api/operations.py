"""JSON API for the personal operations console."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.api.auth import require_ops_console_user
from app.core.config import Settings
from app.operations.contracts import (
    AcknowledgementResult,
    ActivityDetail,
    ActivityPage,
    HealthCard,
    SourceSettings,
)
from app.operations.repository import OperationsRepository

router = APIRouter(prefix="/api/operations", tags=["operations"])


class DatabaseState(Protocol):
    engine: Engine


class AcknowledgementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID | None = None
    alert_key: str = Field(default="run", min_length=1, max_length=255, pattern=r"^[\w.-]+$")


AuthenticatedUser = Annotated[str, Depends(require_ops_console_user)]


def _database(request: Request) -> DatabaseState:
    database = getattr(request.app.state, "database", None)
    engine = getattr(database, "engine", None)
    if not isinstance(engine, Engine):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="operations database is unavailable",
        )
    return cast(DatabaseState, database)


def _settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def _validate_window(date_from: datetime | None, date_to: datetime | None) -> None:
    for value in (date_from, date_to):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="date filters must include a timezone",
            )
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="date_from must be before or equal to date_to",
        )


@router.get("", response_model=tuple[HealthCard, ...])
async def operations_health(request: Request, _: AuthenticatedUser) -> tuple[HealthCard, ...]:
    database = _database(request)
    with Session(database.engine) as session:
        return OperationsRepository.health_cards(session)


@router.get("/activity", response_model=ActivityPage)
async def operations_activity(
    request: Request,
    user_id: AuthenticatedUser,
    agent: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    attention_only: bool = False,
    repository: str | None = None,
    ticker_theme: str | None = None,
    course: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> ActivityPage:
    _validate_window(date_from, date_to)
    database = _database(request)
    with Session(database.engine) as session:
        return OperationsRepository.activity(
            session,
            user_id=user_id,
            agent=agent,
            date_from=date_from,
            date_to=date_to,
            attention_only=attention_only,
            repository=repository,
            ticker_theme=ticker_theme,
            course=course,
            page=page,
            page_size=page_size,
        )


@router.get("/activity/{run_id}", response_model=ActivityDetail)
async def operations_activity_detail(
    request: Request, run_id: UUID, user_id: AuthenticatedUser
) -> ActivityDetail:
    database = _database(request)
    with Session(database.engine) as session:
        detail = OperationsRepository.detail(session, run_id=run_id, user_id=user_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    return detail


@router.get("/settings/sources", response_model=SourceSettings)
async def operations_source_settings(request: Request, _: AuthenticatedUser) -> SourceSettings:
    settings = _settings(request)
    database = _database(request)
    with Session(database.engine) as session:
        return OperationsRepository.source_settings(
            session,
            allowlist_version=settings.finance_source_allowlist_version,
            schedule_configured=True,
        )


@router.post("/activity/{run_id}/acknowledgements", response_model=AcknowledgementResult)
async def operations_acknowledge(
    request: Request,
    run_id: UUID,
    payload: AcknowledgementRequest,
    user_id: AuthenticatedUser,
) -> AcknowledgementResult:
    if payload.run_id is not None and payload.run_id != run_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="body run_id must match path run_id",
        )
    database = _database(request)
    with Session(database.engine) as session:
        result = OperationsRepository.acknowledge(
            session,
            user_id=user_id,
            run_id=run_id,
            alert_key=payload.alert_key,
        )
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    return result


__all__ = [
    "AcknowledgementRequest",
    "operations_acknowledge",
    "operations_activity",
    "operations_activity_detail",
    "operations_health",
    "operations_source_settings",
    "router",
]
