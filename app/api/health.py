"""Liveness and readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app import __version__
from app.health.checks import (
    HealthResponse,
    HealthState,
    check_academic_discord_gateway,
    readiness,
)

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", response_model=HealthResponse, status_code=200)
async def live() -> HealthResponse:
    return HealthResponse(status=HealthState.HEALTHY, version=__version__)


@router.get("/ready", response_model=HealthResponse)
async def ready(request: Request) -> HealthResponse | Response:
    result = await readiness(
        request.app.state.settings,
        request.app.state.database,
        version=__version__,
    )
    result.checks.append(
        check_academic_discord_gateway(
            request.app.state.settings,
            getattr(request.app.state, "discord_academic_gateway_state", None),
        )
    )
    if any(check.state is HealthState.FAILED for check in result.checks):
        result.status = HealthState.FAILED
    elif result.status is HealthState.HEALTHY and any(
        check.state is HealthState.ATTENTION for check in result.checks
    ):
        result.status = HealthState.ATTENTION
    if result.status is HealthState.FAILED:
        return JSONResponse(status_code=503, content=result.model_dump(mode="json"))
    return result
