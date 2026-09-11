"""Shared construction for durable academic Discord worker execution."""

from __future__ import annotations

from dataclasses import dataclass

from app.agents.academic_planner.discord_harness import NativeAcademicDiscordHandler
from app.agents.academic_planner.notion_mutations import DiscoveredAcademicNotionWriter
from app.agents.academic_planner.sync import AcademicNotionSync
from app.connectors.discord import (
    DiscordAcademicPlannerAdapter,
    DiscordAcademicResponseDelivery,
)
from app.connectors.notion import NotionConnector
from app.core.config import Settings, get_settings
from app.core.errors import LifeAgentError
from app.db.academic import SQLAlchemyAcademicPlannerStore
from app.db.session import Database
from app.llm.gateway import LLMGateway
from app.llm.ollama_runtime import OllamaRuntime


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
    catalog_syncer = AcademicNotionSync(
        connector=notion_connector,
        store=store,
        discord=None,
        discord_channel_id=None,
        timezone=app_settings.app_timezone,
        clarification_ttl_hours=app_settings.academic_confirmation_ttl_hours,
        setup_condition_code=notion_setup_condition,
        material_enqueuer=None,
    )
    writer = (
        DiscoveredAcademicNotionWriter(
            connector=notion_connector,
            target_store=store,
        )
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
