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
from app.agents.academic_planner.agent_clarification import (
    AGENT_CONTEXT_DATA_CLASS,
    AcademicAgentClarificationService,
)
from app.agents.academic_planner.material_ingestion import AssessmentMaterialIngestionService
from app.agents.academic_planner.memory_workflow import AcademicMemoryService
from app.agents.academic_planner.notion_mutations import DiscoveredAcademicNotionWriter
from app.agents.academic_planner.sync import AcademicNotionSync
from app.api.academic import router as academic_router
from app.api.discord_handoff import router as discord_handoff_router
from app.api.finance import router as finance_router
from app.api.github import router as github_router
from app.api.health import router as health_router
from app.api.operations import router as operations_router
from app.api.pages import router as pages_router
from app.artifacts.store import ArtifactStore
from app.connectors.discord import (
    DiscordAcademicPlannerAdapter,
    DiscordAcademicResponseDelivery,
)
from app.connectors.notion import NotionConnector
from app.core.config import Settings, get_settings
from app.core.errors import LifeAgentError
from app.db.academic import SQLAlchemyAcademicPlannerStore
from app.db.finance import SQLAlchemyFinanceStore
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
    agent_context_retention_days = max(
        1,
        (app_settings.academic_confirmation_ttl_hours + 23) // 24,
    )
    academic_artifact_store = ArtifactStore(
        app_settings.artifact_root,
        retention_days_by_class={
            AGENT_CONTEXT_DATA_CLASS: agent_context_retention_days,
        },
        default_retention_days=app_settings.artifact_retention_days,
        create_root=False,
    )
    academic_agent_clarification_service = AcademicAgentClarificationService(
        engine=database.engine,
        artifact_store=academic_artifact_store,
        session_ttl_hours=app_settings.academic_confirmation_ttl_hours,
    )
    academic_memory_service = (
        AcademicMemoryService(
            store=academic_store,
            model_gateway=gateway,
            embedding_gateway=embedding_gateway,
            timezone=app_settings.app_timezone,
            default_practice_minutes=(app_settings.academic_memory_default_practice_minutes),
            end_of_day_time=app_settings.academic_end_of_day_schedule,
            session_ttl_hours=app_settings.academic_confirmation_ttl_hours,
        )
        if app_settings.academic_memory_enabled
        else None
    )
    academic_channel = app_settings.discord_academic_channel_id
    academic_discord = None
    academic_delivery = None
    if app_settings.discord_bot_token is not None and academic_channel is not None:
        academic_discord = DiscordAcademicPlannerAdapter(
            token=app_settings.discord_bot_token,
            allowed_channel_ids={academic_channel},
            base_url=app_settings.discord_api_url,
        )
        academic_delivery = DiscordAcademicResponseDelivery(
            engine=database.engine,
            channel_id=academic_channel,
            adapter=academic_discord,
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
    )

    async def enqueue_code_review(run_id: str, idempotency_key: str) -> object:
        return await queue_tasks.defer_idempotent_async(
            queue_tasks.code_review_task,
            run_id,
            idempotency_key,
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
    app.state.enqueue_code_review = enqueue_code_review
    app.state.academic_store = academic_store
    app.state.academic_syncer = academic_syncer
    app.state.academic_material_ingestion_factory = build_material_ingestion
    # The legacy HTTP check-in boundary remains deterministic and model-free.
    # Qwen is reachable only through the authenticated host handoff path.
    app.state.academic_model = None
    app.state.academic_memory_service = academic_memory_service
    app.state.academic_agent_clarification_service = academic_agent_clarification_service
    app.state.academic_delivery = academic_delivery
    # Discord Gateway ingress is a native macOS LaunchAgent. The API owns only
    # authenticated durable handoff processing and never opens a Gateway session.
    app.state.discord_host_ingress_state = "external"
    app.state.finance_store = SQLAlchemyFinanceStore(
        database.engine,
        allowlist_version=app_settings.finance_source_allowlist_version,
    )
    # Writes resolve only synchronized, valid per-course calendar mappings and
    # still require the proposal's exact confirmation event.
    app.state.notion_writer = (
        DiscoveredAcademicNotionWriter(
            connector=notion_connector,
            target_store=academic_store,
        )
        if notion_connector is not None
        else None
    )
    app.mount("/static", StaticFiles(directory=str(_APP_ROOT / "static")), name="static")
    app.include_router(health_router)
    app.include_router(github_router)
    app.include_router(academic_router)
    app.include_router(discord_handoff_router)
    app.include_router(finance_router)
    app.include_router(operations_router)
    app.include_router(pages_router)
    return app


app = create_app()

__all__ = ["app", "create_app", "format_toronto_time", "templates"]
