"""FastAPI application entry point."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app import __version__
from app.agents.academic_planner.sync import AcademicClarificationService, AcademicNotionSync
from app.api.academic import router as academic_router
from app.api.finance import router as finance_router
from app.api.github import router as github_router
from app.api.health import router as health_router
from app.api.operations import router as operations_router
from app.api.pages import router as pages_router
from app.connectors.discord import DiscordAcademicPlannerAdapter
from app.connectors.discord_gateway import DiscordGatewayListener
from app.connectors.notion import NotionConnector
from app.core.config import Settings, get_settings
from app.core.errors import LifeAgentError
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
    academic_store = SQLAlchemyAcademicPlannerStore(
        database.engine,
        confirmation_ttl_hours=app_settings.academic_confirmation_ttl_hours,
    )
    academic_channel = app_settings.discord_academic_channel_id
    academic_discord = None
    if app_settings.discord_bot_token is not None and academic_channel is not None:
        academic_discord = DiscordAcademicPlannerAdapter(
            token=app_settings.discord_bot_token,
            allowed_channel_ids={academic_channel},
            base_url=app_settings.discord_api_url,
        )
    notion_connector = None
    notion_setup_condition = "notion_configuration_missing"
    if (
        app_settings.notion_token is not None
        and app_settings.notion_courses_database_id is not None
    ):
        try:
            notion_connector = NotionConnector(
                token=app_settings.notion_token,
                courses_database_id=app_settings.notion_courses_database_id,
                timeout_seconds=app_settings.connector_timeout_seconds,
            )
        except (LifeAgentError, ValueError):
            notion_setup_condition = "notion_configuration_invalid"
    academic_syncer = AcademicNotionSync(
        connector=notion_connector,
        store=academic_store,
        discord=academic_discord,
        discord_channel_id=academic_channel,
        timezone=app_settings.app_timezone,
        clarification_ttl_hours=app_settings.academic_confirmation_ttl_hours,
        setup_condition_code=notion_setup_condition,
    )
    clarification_service = (
        AcademicClarificationService(
            store=academic_store,
            connector=notion_connector,
            syncer=academic_syncer,
        )
        if notion_connector is not None
        else None
    )
    gateway_listener = None
    gateway_state = "disabled"
    if app_settings.discord_academic_gateway_enabled:
        gateway_state = "setup_required"
        if (
            app_settings.discord_bot_token is not None
            and academic_channel is not None
            and app_settings.discord_academic_authorized_user_ids
            and clarification_service is not None
        ):
            gateway_listener = DiscordGatewayListener(
                token=app_settings.discord_bot_token,
                api_base_url=app_settings.discord_api_url,
                allowed_channel_ids={academic_channel},
                authorized_user_ids={
                    str(item) for item in app_settings.discord_academic_authorized_user_ids
                },
                handler=clarification_service,
            )
            gateway_state = "starting"

    async def enqueue_code_review(run_id: str, idempotency_key: str) -> object:
        return await defer_idempotent_async(code_review_task, run_id, idempotency_key)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
        gateway_task: asyncio.Task[None] | None = None
        if gateway_listener is not None:
            listener = gateway_listener
            application.state.discord_academic_gateway_state = "running"

            async def run_gateway() -> None:
                try:
                    await listener.run_forever()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    application.state.discord_academic_gateway_state = "failed"

            gateway_task = asyncio.create_task(
                run_gateway(),
                name="discord-academic-gateway",
            )
        try:
            if use_global_queue:
                async with procrastinate_app.open_async():
                    yield
            else:
                yield
        finally:
            if gateway_task is not None:
                gateway_task.cancel()
                with suppress(asyncio.CancelledError):
                    await gateway_task
                application.state.discord_academic_gateway_state = "stopped"
            database.dispose()

    app = FastAPI(title=app_settings.app_name, version=__version__, lifespan=lifespan)
    app.add_middleware(SecurityHeadersMiddleware)
    app.state.settings = app_settings
    app.state.database = database
    app.state.templates = templates
    app.state.model_identity = gateway.model_identity
    app.state.model_config_version = gateway.config_version
    app.state.enqueue_code_review = enqueue_code_review
    app.state.academic_store = academic_store
    app.state.academic_syncer = academic_syncer
    app.state.discord_academic_gateway_state = gateway_state
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
