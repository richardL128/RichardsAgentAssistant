"""Authenticated server-rendered operations-console pages."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Protocol, cast
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.api.auth import require_ops_console_user
from app.core.config import Settings
from app.operations.repository import OperationsRepository

router = APIRouter(tags=["operations-console"])
AuthenticatedUser = Annotated[str, Depends(require_ops_console_user)]


class DatabaseState(Protocol):
    engine: Engine


def _database(request: Request) -> DatabaseState:
    database = getattr(request.app.state, "database", None)
    engine = getattr(database, "engine", None)
    if not isinstance(engine, Engine):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="operations database is unavailable",
        )
    return cast(DatabaseState, database)


def _templates(request: Request) -> Jinja2Templates:
    templates = getattr(request.app.state, "templates", None)
    if not isinstance(templates, Jinja2Templates):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="operations templates are unavailable",
        )
    return templates


def _settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def _validate_window(date_from: datetime | None, date_to: datetime | None) -> None:
    for value in (date_from, date_to):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="date filters must include a timezone",
            )
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from must be before or equal to date_to",
        )


def _pagination_url(request: Request, page: int) -> str:
    parameters = [
        (key, value) for key, value in request.query_params.multi_items() if key != "page"
    ]
    parameters.append(("page", str(page)))
    return f"/activity?{urlencode(parameters)}"


@router.get("/", response_class=HTMLResponse)
async def health_page(request: Request, _: AuthenticatedUser) -> HTMLResponse:
    database = _database(request)
    with Session(database.engine) as session:
        cards = OperationsRepository.health_cards(session)
    return _templates(request).TemplateResponse(
        request=request,
        name="health.html",
        context={
            "active_page": "health",
            "cards": cards,
            "failed_cards": tuple(card for card in cards if card.state == "failed"),
        },
    )


@router.get("/activity", response_class=HTMLResponse)
async def activity_page(
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
) -> HTMLResponse:
    _validate_window(date_from, date_to)
    database = _database(request)
    with Session(database.engine) as session:
        activity = OperationsRepository.activity(
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
    return _templates(request).TemplateResponse(
        request=request,
        name="activity.html",
        context={
            "active_page": "activity",
            "activity": activity,
            "filters": {
                "agent": agent or "",
                "date_from": request.query_params.get("date_from", ""),
                "date_to": request.query_params.get("date_to", ""),
                "attention_only": attention_only,
                "repository": repository or "",
                "ticker_theme": ticker_theme or "",
                "course": course or "",
            },
            "previous_url": _pagination_url(request, page - 1) if page > 1 else None,
            "next_url": (_pagination_url(request, page + 1) if page < activity.pages else None),
        },
    )


@router.get("/activity/{run_id}", response_class=HTMLResponse)
async def activity_detail_page(
    request: Request,
    run_id: UUID,
    user_id: AuthenticatedUser,
) -> HTMLResponse:
    database = _database(request)
    with Session(database.engine) as session:
        detail = OperationsRepository.detail(session, run_id=run_id, user_id=user_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    return _templates(request).TemplateResponse(
        request=request,
        name="activity_detail.html",
        context={"active_page": "activity", "detail": detail},
    )


@router.post("/activity/{run_id}/ack", response_class=HTMLResponse)
async def acknowledge_page(
    request: Request,
    run_id: UUID,
    user_id: AuthenticatedUser,
    hx_request: Annotated[str | None, Header(alias="HX-Request")] = None,
) -> HTMLResponse:
    if hx_request != "true":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="HTMX request required",
        )
    database = _database(request)
    with Session(database.engine) as session:
        result = OperationsRepository.acknowledge(
            session,
            user_id=user_id,
            run_id=run_id,
            alert_key="run",
        )
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    return _templates(request).TemplateResponse(
        request=request,
        name="partials/acknowledgement.html",
        context={"result": result},
    )


@router.get("/settings/sources", response_class=HTMLResponse)
async def source_settings_page(request: Request, _: AuthenticatedUser) -> HTMLResponse:
    settings = _settings(request)
    database = _database(request)
    with Session(database.engine) as session:
        source_settings = OperationsRepository.source_settings(
            session,
            allowlist_version=settings.finance_source_allowlist_version,
            schedule_configured=True,
        )
    return _templates(request).TemplateResponse(
        request=request,
        name="settings_sources.html",
        context={
            "active_page": "sources",
            "settings": source_settings,
        },
    )


__all__ = [
    "acknowledge_page",
    "activity_detail_page",
    "activity_page",
    "health_page",
    "router",
    "source_settings_page",
]
