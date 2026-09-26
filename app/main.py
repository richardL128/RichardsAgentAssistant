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
from app.agents.academic_planner.material_ingestion import AssessmentMaterialIngestionService
from app.agents.academic_planner.material_planning import (
    AssessmentMaterialPlanningProfileService,
    MaterialPlanningModelIdentity,
)
from app.agents.academic_planner.sync import AcademicNotionSync
from app.agents.job_interviews.sync import JobInterviewNotionSync
from app.api.academic import router as academic_router
from app.api.discord_handoff import router as discord_handoff_router
from app.api.finance import router as finance_router
from app.api.health import router as health_router
from app.api.job_interviews import router as job_interviews_router
from app.api.operations import router as operations_router
from app.api.pages import router as pages_router
from app.artifacts.store import ArtifactStore
from app.connectors.discord import DiscordAcademicPlannerAdapter
from app.connectors.google_calendar import GoogleCalendarConnector
from app.connectors.notion import NotionConnector
from app.core.config import Settings, get_settings
from app.core.errors import LifeAgentError
from app.db.academic import SQLAlchemyAcademicPlannerStore
from app.db.finance import SQLAlchemyFinanceStore
from app.db.job_interviews import SQLAlchemyJobInterviewStore
from app.db.session import Database
from app.llm.embeddings import AcademicEmbeddingGateway
from app.llm.gateway import LLMGateway
from app.llm.ollama_runtime import OllamaRuntime
from app.queue import tasks as queue_tasks
from app.queue.app import procrastinate_app

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
    ollama_runtime = OllamaRuntime(app_settings)
    embedding_gateway = AcademicEmbeddingGateway(app_settings)
    academic_store = SQLAlchemyAcademicPlannerStore(
        database.engine,
        confirmation_ttl_hours=app_settings.academic_confirmation_ttl_hours,
        embedding_gateway=embedding_gateway,
        default_practice_minutes=app_settings.academic_memory_default_practice_minutes,
    )
    material_profile_generator = AssessmentMaterialPlanningProfileService(
        model=gateway,
        repository=academic_store,
        model_identity=MaterialPlanningModelIdentity(
            generator_model=gateway.model_identity,
            critic_model=gateway.model_identity,
        ),
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
    schedule_connector = None
    notion_setup_condition = "notion_configuration_missing"
    notion_database_ids = (
        app_settings.notion_courses_database_id,
        app_settings.notion_action_items_database_id,
        app_settings.notion_applications_database_id,
        app_settings.notion_interviews_database_id,
    )
    if app_settings.notion_token is not None and all(
        value is not None for value in notion_database_ids
    ):
        try:
            assert app_settings.notion_courses_database_id is not None
            assert app_settings.notion_action_items_database_id is not None
            assert app_settings.notion_applications_database_id is not None
            assert app_settings.notion_interviews_database_id is not None
            notion_connector = NotionConnector(
                token=app_settings.notion_token,
                courses_database_id=app_settings.notion_courses_database_id,
                action_items_database_id=app_settings.notion_action_items_database_id,
                applications_database_id=app_settings.notion_applications_database_id,
                interviews_database_id=app_settings.notion_interviews_database_id,
                timeout_seconds=app_settings.connector_timeout_seconds,
            )
        except (LifeAgentError, ValueError):
            notion_setup_condition = "notion_configuration_invalid"
    elif app_settings.notion_token is not None or any(
        value is not None for value in notion_database_ids
    ):
        notion_setup_condition = "notion_configuration_invalid"
    if app_settings.academic_schedule_ical_url is not None:
        try:
            schedule_connector = GoogleCalendarConnector(
                ical_url=app_settings.academic_schedule_ical_url,
                timeout_seconds=app_settings.academic_schedule_ical_timeout_seconds,
                max_response_bytes=app_settings.academic_schedule_ical_max_bytes,
            )
        except (LifeAgentError, ValueError):
            schedule_connector = None

    def build_material_ingestion() -> AssessmentMaterialIngestionService | None:
        if notion_connector is None:
            return None
        return AssessmentMaterialIngestionService(
            engine=database.engine,
            connector=notion_connector,
            artifact_store=ArtifactStore(
                app_settings.artifact_root,
                default_retention_days=app_settings.artifact_retention_days,
            ),
            embedding_gateway=embedding_gateway,
            max_bytes=app_settings.notion_attachment_max_bytes,
            max_depth=app_settings.notion_material_max_block_depth,
            max_blocks=app_settings.notion_material_max_blocks,
            max_requests=app_settings.notion_material_max_cursor_pages,
            pdf_max_pages=app_settings.academic_material_pdf_max_pages,
            ocr_timeout_seconds=app_settings.academic_material_ocr_timeout_seconds,
            ocr_min_page_chars=app_settings.academic_material_ocr_min_page_chars,
            profile_generator=material_profile_generator,
        )

    async def enqueue_academic_material(page_id: str, fingerprint: str) -> object:
        return await queue_tasks.defer_academic_material_ingestion(page_id, fingerprint)

    academic_syncer = AcademicNotionSync(
        connector=notion_connector,
        store=academic_store,
        discord=academic_discord,
        discord_channel_id=academic_channel,
        timezone=app_settings.app_timezone,
        clarification_ttl_hours=app_settings.academic_confirmation_ttl_hours,
        setup_condition_code=notion_setup_condition,
        material_enqueuer=enqueue_academic_material if use_global_queue else None,
        schedule_connector=schedule_connector,
        schedule_lookback_days=app_settings.academic_sync_lookback_days,
        schedule_horizon_days=max(11, app_settings.academic_plan_horizon_days),
    )
    job_interview_store = SQLAlchemyJobInterviewStore(database.engine)
    job_interview_syncer = JobInterviewNotionSync(
        connector=notion_connector,
        store=job_interview_store,
        timezone=app_settings.app_timezone,
        setup_condition_code=notion_setup_condition,
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
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
    app.state.ollama_runtime = ollama_runtime
    app.state.academic_store = academic_store
    app.state.academic_syncer = academic_syncer
    app.state.academic_material_ingestion_factory = build_material_ingestion
    app.state.job_interview_store = job_interview_store
    app.state.job_interview_syncer = job_interview_syncer
    app.state.job_interview_notion_writer = None
    # Discord Gateway ingress is a native macOS LaunchAgent. The API owns only
    # authenticated durable handoff processing and never opens a Gateway session.
    app.state.discord_host_ingress_state = "external"
    app.state.finance_store = SQLAlchemyFinanceStore(
        database.engine,
        allowlist_version=app_settings.finance_source_allowlist_version,
    )
    app.state.notion_writer = None
    app.mount("/static", StaticFiles(directory=str(_APP_ROOT / "static")), name="static")
    app.include_router(health_router)
    app.include_router(academic_router)
    app.include_router(discord_handoff_router)
    app.include_router(job_interviews_router)
    app.include_router(finance_router)
    app.include_router(operations_router)
    app.include_router(pages_router)
    return app


app = create_app()

__all__ = ["app", "create_app", "format_toronto_time", "templates"]
