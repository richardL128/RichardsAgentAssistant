"""FastAPI application entry point."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app import __version__
from app.api.academic import router as academic_router
from app.api.finance import router as finance_router
from app.api.github import router as github_router
from app.api.health import router as health_router
from app.api.operations import router as operations_router
from app.api.pages import router as pages_router
from app.core.config import Settings, get_settings
from app.db.academic import SQLAlchemyAcademicPlannerStore
from app.db.finance import SQLAlchemyFinanceStore
from app.db.session import Database
from app.llm.gateway import LLMGateway
from app.queue.app import procrastinate_app
from app.queue.tasks import code_review_task, defer_idempotent_async

_APP_ROOT = Path(__file__).resolve().parent
_TORONTO = ZoneInfo("America/Toronto")
_CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    )
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Apply the browser security boundary without permitting inline output execution."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = _CONTENT_SECURITY_POLICY
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response


def format_toronto_time(value: datetime | None) -> str:
    """Render an exact Toronto timestamp for server-rendered console pages."""

    if value is None:
        return "Never"
    aware = (
        value
        if value.tzinfo is not None and value.utcoffset() is not None
        else value.replace(tzinfo=UTC)
    )
    return aware.astimezone(_TORONTO).strftime("%Y-%m-%d %H:%M:%S %Z")


templates = Jinja2Templates(directory=str(_APP_ROOT / "templates"))
templates.env.filters["toronto_time"] = format_toronto_time


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the API application, allowing tests to inject settings."""

    use_global_queue = settings is None
    app_settings = settings or get_settings()
    database = Database(app_settings)
    gateway = LLMGateway(app_settings)

    async def enqueue_code_review(run_id: str, idempotency_key: str) -> object:
        return await defer_idempotent_async(code_review_task, run_id, idempotency_key)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
        try:
            if use_global_queue:
                async with procrastinate_app.open_async():
                    yield
            else:
                yield
        finally:
            database.dispose()

    app = FastAPI(title=app_settings.app_name, version=__version__, lifespan=lifespan)
    app.add_middleware(SecurityHeadersMiddleware)
    app.state.settings = app_settings
    app.state.database = database
    app.state.templates = templates
    app.state.model_identity = gateway.model_identity
    app.state.model_config_version = gateway.config_version
    app.state.enqueue_code_review = enqueue_code_review
    app.state.academic_store = SQLAlchemyAcademicPlannerStore(
        database.engine,
        confirmation_ttl_hours=app_settings.academic_confirmation_ttl_hours,
    )
    app.state.finance_store = SQLAlchemyFinanceStore(
        database.engine,
        allowlist_version=app_settings.finance_source_allowlist_version,
    )
    # The concrete Notion writer is injected only after scoped database,
    # property, and page-target mappings have been supplied.  Keeping it
    # absent makes confirmation fail closed while proposal capture remains
    # available in a local deployment.
    app.state.notion_writer = None
    app.mount("/static", StaticFiles(directory=str(_APP_ROOT / "static")), name="static")
    app.include_router(health_router)
    app.include_router(github_router)
    app.include_router(academic_router)
    app.include_router(finance_router)
    app.include_router(operations_router)
    app.include_router(pages_router)
    return app


app = create_app()

__all__ = ["app", "create_app", "format_toronto_time", "templates"]
