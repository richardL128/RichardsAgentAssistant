"""Shared construction for durable academic Discord worker execution."""

from __future__ import annotations

from dataclasses import dataclass

from app.agents.academic_planner.discord_harness import NativeAcademicDiscordHandler
from app.agents.academic_planner.material_intake import AcademicMaterialIntakeService
from app.agents.academic_planner.notion_mutations import DiscoveredAcademicNotionWriter
from app.agents.academic_planner.sync import AcademicNotionSync
from app.agents.job_interviews.agent_loop import CareerAgentToolState
from app.agents.job_interviews.notion_mutations import DiscoveredCareerNotionWriter
from app.agents.job_interviews.sync import JobInterviewNotionSync
from app.artifacts.store import ArtifactStore
from app.connectors.discord import (
    DiscordAcademicPlannerAdapter,
    DiscordAcademicResponseDelivery,
)
from app.connectors.notion import NotionConnector
from app.core.config import Settings, get_settings
from app.core.errors import LifeAgentError
from app.db.academic import SQLAlchemyAcademicPlannerStore
from app.db.job_interviews import SQLAlchemyJobInterviewStore
from app.db.session import Database
from app.llm.gateway import LLMGateway
from app.llm.ollama_runtime import OllamaRuntime
from app.queue import tasks as queue_tasks


@dataclass(slots=True)
class AcademicDiscordService:
    """Worker-owned handler and database lifetime."""

    handler: NativeAcademicDiscordHandler
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
    store = SQLAlchemyAcademicPlannerStore(
        database.engine,
        confirmation_ttl_hours=app_settings.academic_confirmation_ttl_hours,
        default_practice_minutes=app_settings.academic_memory_default_practice_minutes,
    )
    artifact_store = ArtifactStore(
        app_settings.artifact_root,
        default_retention_days=app_settings.artifact_retention_days,
    )
    material_intake = AcademicMaterialIntakeService(
        store=store,
        artifact_store=artifact_store,
        max_bytes=app_settings.discord_academic_pdf_max_bytes,
        max_pages=app_settings.academic_material_pdf_max_pages,
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
    notion_setup_condition = "notion_configuration_missing"
    notion_connector = _create_notion_connector(app_settings)
    if (
        notion_connector is None
        and app_settings.notion_token is not None
        and app_settings.notion_courses_database_id is not None
    ):
        notion_setup_condition = "notion_configuration_invalid"

    async def enqueue_material(page_id: str, fingerprint: str) -> object:
        return await queue_tasks.defer_academic_material_ingestion(page_id, fingerprint)

    catalog_syncer = AcademicNotionSync(
        connector=notion_connector,
        store=store,
        discord=None,
        discord_channel_id=None,
        timezone=app_settings.app_timezone,
        clarification_ttl_hours=app_settings.academic_confirmation_ttl_hours,
        setup_condition_code=notion_setup_condition,
        material_enqueuer=enqueue_material,
    )
    career_store = SQLAlchemyJobInterviewStore(database.engine)
    career_syncer = JobInterviewNotionSync(
        connector=notion_connector,
        store=career_store,
        timezone=app_settings.app_timezone,
        setup_condition_code=notion_setup_condition,
    )
    writer = (
        DiscoveredAcademicNotionWriter(
            connector=notion_connector,
            target_store=store,
            artifact_loader=artifact_store,
            post_seed_syncer=catalog_syncer,
        )
        if notion_connector is not None
        else None
    )
    career_writer = (
        DiscoveredCareerNotionWriter(engine=database.engine, connector=notion_connector)
        if notion_connector is not None
        else None
    )
    handler = NativeAcademicDiscordHandler(
        store=store,
        delivery=delivery,
        allowed_channel_ids={channel_id},
        authorized_user_ids=authorized_users,
        writer_provider=lambda: writer,
        ollama_runtime=OllamaRuntime(app_settings),
        agent_gateway=gateway,
        agent_catalog=store,
        assistant_user_id=application_id,
        catalog_syncer=catalog_syncer,
        catalog_sync_timeout_seconds=min(
            60.0,
            max(5.0, app_settings.connector_timeout_seconds * 3),
        ),
        timezone=app_settings.app_timezone,
        career_tool_state_factory=lambda message: CareerAgentToolState(
            store=career_store,
            syncer=career_syncer,
            gateway=gateway,
            engine=database.engine,
            requester=message.author_id,
            external_event_id=message.message_id,
            now=message.timestamp,
            timezone=app_settings.app_timezone,
            sync_timeout_seconds=min(
                60.0,
                max(5.0, app_settings.connector_timeout_seconds * 3),
            ),
            research_timeout_seconds=app_settings.job_research_timeout_seconds,
            research_max_redirects=app_settings.job_research_max_redirects,
            research_max_response_bytes=app_settings.job_research_max_response_bytes,
            research_max_pages=app_settings.job_research_max_pages,
            research_max_search_results=app_settings.job_research_max_search_results,
        ),
        career_engine=database.engine,
        career_writer_provider=lambda: career_writer,
        material_intake=material_intake,
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
