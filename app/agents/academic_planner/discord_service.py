"""Shared construction for durable academic Discord worker execution."""

from __future__ import annotations

from dataclasses import dataclass

from app.agents.academic_planner.agent_clarification import (
    AGENT_CONTEXT_DATA_CLASS,
    AcademicAgentClarificationService,
)
from app.agents.academic_planner.discord_checkin import AcademicDiscordCheckinHandler
from app.agents.academic_planner.memory_workflow import AcademicMemoryService
from app.agents.academic_planner.notion_mutations import DiscoveredAcademicNotionWriter
from app.artifacts.store import ArtifactStore
from app.connectors.discord import (
    DiscordAcademicPlannerAdapter,
    DiscordAcademicResponseDelivery,
)
from app.connectors.notion import NotionConnector
from app.core.config import Settings, get_settings
from app.core.errors import LifeAgentError
from app.db.academic import SQLAlchemyAcademicPlannerStore
from app.db.session import Database
from app.llm.embeddings import AcademicEmbeddingGateway
from app.llm.gateway import LLMGateway
from app.llm.ollama_runtime import OllamaRuntime


@dataclass(slots=True)
class AcademicDiscordService:
    """Worker-owned handler and database lifetime."""

    handler: AcademicDiscordCheckinHandler
    database: Database

    def close(self) -> None:
        self.database.dispose()


def create_academic_discord_service(
    settings: Settings | None = None,
) -> AcademicDiscordService:
    """Build the one canonical Discord academic handler for queue workers."""

    app_settings = settings or get_settings()
    channel_id = app_settings.discord_academic_channel_id
    token = app_settings.discord_bot_token
    application_id = app_settings.discord_application_id
    authorized_users = {
        str(user_id) for user_id in app_settings.discord_academic_authorized_user_ids
    }
    if (
        token is None
        or channel_id is None
        or application_id is None
        or not authorized_users
        or not app_settings.discord_academic_message_content_enabled
    ):
        raise ValueError("Academic Discord worker configuration is incomplete")

    database = Database(app_settings)
    gateway = LLMGateway(app_settings)
    embedding_gateway = AcademicEmbeddingGateway(app_settings)
    store = SQLAlchemyAcademicPlannerStore(
        database.engine,
        confirmation_ttl_hours=app_settings.academic_confirmation_ttl_hours,
        embedding_gateway=embedding_gateway,
        default_practice_minutes=app_settings.academic_memory_default_practice_minutes,
    )
    context_retention_days = max(
        1,
        (app_settings.academic_confirmation_ttl_hours + 23) // 24,
    )
    clarification_service = AcademicAgentClarificationService(
        engine=database.engine,
        artifact_store=ArtifactStore(
            app_settings.artifact_root,
            retention_days_by_class={
                AGENT_CONTEXT_DATA_CLASS: context_retention_days,
            },
            default_retention_days=app_settings.artifact_retention_days,
        ),
        session_ttl_hours=app_settings.academic_confirmation_ttl_hours,
    )
    memory_service = (
        AcademicMemoryService(
            store=store,
            model_gateway=gateway,
            embedding_gateway=embedding_gateway,
            timezone=app_settings.app_timezone,
            default_practice_minutes=app_settings.academic_memory_default_practice_minutes,
            end_of_day_time=app_settings.academic_end_of_day_schedule,
            session_ttl_hours=app_settings.academic_confirmation_ttl_hours,
        )
        if app_settings.academic_memory_enabled
        else None
    )
    adapter = DiscordAcademicPlannerAdapter(
        token=token,
        allowed_channel_ids={channel_id},
        base_url=app_settings.discord_api_url,
    )
    delivery = DiscordAcademicResponseDelivery(
        engine=database.engine,
        channel_id=channel_id,
        adapter=adapter,
    )
    notion_connector = _create_notion_connector(app_settings)
    writer = (
        DiscoveredAcademicNotionWriter(
            connector=notion_connector,
            target_store=store,
        )
        if notion_connector is not None
        else None
    )
    handler = AcademicDiscordCheckinHandler(
        store=store,
        delivery=delivery,
        allowed_channel_ids={channel_id},
        authorized_user_ids=authorized_users,
        writer_provider=lambda: writer,
        ollama_runtime=OllamaRuntime(app_settings),
        agent_gateway=gateway,
        semantic_router_gateway=gateway,
        agent_catalog=store,
        assistant_user_id=application_id,
        timezone=app_settings.app_timezone,
        memory_service=memory_service,
        agent_clarification_service=clarification_service,
    )
    return AcademicDiscordService(handler=handler, database=database)


def _create_notion_connector(settings: Settings) -> NotionConnector | None:
    if settings.notion_token is None or settings.notion_courses_database_id is None:
        return None
    try:
        return NotionConnector(
            token=settings.notion_token,
            courses_database_id=settings.notion_courses_database_id,
            timeout_seconds=settings.connector_timeout_seconds,
        )
    except (LifeAgentError, ValueError):
        return None


__all__ = ["AcademicDiscordService", "create_academic_discord_service"]
