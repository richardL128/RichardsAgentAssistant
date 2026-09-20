"""Shared construction for durable academic Discord worker execution."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, cast

from app.agents.academic_planner.discord_harness import NativeAcademicDiscordHandler
from app.agents.academic_planner.material_intake import AcademicMaterialIntakeService
from app.agents.academic_planner.memory_workflow import AcademicMemoryService
from app.agents.academic_planner.notion_mutations import DiscoveredAcademicNotionWriter
from app.agents.academic_planner.sync import AcademicNotionSync
from app.agents.calendar_briefing.semantic_interpreter import CalendarEventSemanticInterpreter
from app.agents.conversation.context import ConversationContextAssembler
from app.agents.conversation.service import NativeConversationService
from app.agents.harness import AbortCheck
from app.agents.job_interviews.agent_loop import CareerAgentToolState
from app.agents.job_interviews.notion_mutations import DiscoveredCareerNotionWriter
from app.agents.job_interviews.sync import JobInterviewNotionSync
from app.agents.learn.notion_proposals import LearnNotionProposalBuilder
from app.agents.learn.semantic_interpreter import LearnAnnouncementSemanticInterpreter
from app.agents.learn.tool_state import LearnToolState
from app.agents.memory.service import UserMemoryService
from app.artifacts.store import ArtifactStore
from app.connectors.discord import (
    DiscordAcademicPlannerAdapter,
    DiscordAcademicResponseDelivery,
)
from app.connectors.learn_bridge import LearnBridgeConnector
from app.connectors.notion import NotionConnector
from app.core.config import Settings, get_settings
from app.core.errors import LifeAgentError
from app.db.academic import SQLAlchemyAcademicPlannerStore
from app.db.job_interviews import SQLAlchemyJobInterviewStore
from app.db.session import Database
from app.db.user_memory import SQLAlchemyUserMemoryStore
from app.llm.embeddings import AcademicEmbeddingGateway
from app.llm.gateway import LLMGateway
from app.llm.ollama_runtime import OllamaRuntime
from app.queue import tasks as queue_tasks


@dataclass(slots=True)
class AcademicDiscordService:
    """Worker-owned handler and database lifetime."""

    handler: NativeAcademicDiscordHandler
    database: Database
    _turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def handle(
        self,
        message: object,
        *,
        abort_check: AbortCheck | None = None,
        activity_sink: Callable[[Mapping[str, object]], Awaitable[None] | None] | None = None,
    ) -> object:
        """Run one wake with callbacks scoped to it while reusing the service graph."""

        async with self._turn_lock:
            mutable_handler = cast(Any, self.handler)
            previous_abort = mutable_handler._abort_check
            previous_activity = mutable_handler._activity_sink
            mutable_handler._abort_check = abort_check
            mutable_handler._activity_sink = activity_sink
            try:
                return await self.handler(message)  # type: ignore[arg-type]
            finally:
                mutable_handler._abort_check = previous_abort
                mutable_handler._activity_sink = previous_activity

    def close(self) -> None:
        self.database.dispose()


def create_academic_discord_service(
    settings: Settings | None = None,
    *,
    database: Database | None = None,
    abort_check: AbortCheck | None = None,
    activity_sink: Callable[[Mapping[str, object]], Awaitable[None] | None] | None = None,
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

    database = database or Database(app_settings)
    gateway = LLMGateway(app_settings)
    compaction_gateway = LLMGateway(
        app_settings.model_copy(
            update={
                "ollama_max_output_tokens": (app_settings.conversation_compaction_max_output_tokens)
            }
        )
    )
    embedding_gateway = AcademicEmbeddingGateway(app_settings)
    store = SQLAlchemyAcademicPlannerStore(
        database.engine,
        confirmation_ttl_hours=app_settings.academic_confirmation_ttl_hours,
        embedding_gateway=embedding_gateway,
        default_practice_minutes=app_settings.academic_memory_default_practice_minutes,
    )
    artifact_store = ArtifactStore(
        app_settings.artifact_root,
        retention_days_by_class={
            "native_context_manifest": app_settings.conversation_context_manifest_retention_days,
            "user_memory_content": None,
            "user_memory_evidence": None,
        },
        default_retention_days=app_settings.artifact_retention_days,
    )
    material_intake = AcademicMaterialIntakeService(
        store=store,
        artifact_store=artifact_store,
        max_bytes=app_settings.discord_academic_pdf_max_bytes,
        max_pages=app_settings.academic_material_pdf_max_pages,
    )
    conversation_service = NativeConversationService(
        engine=database.engine,
        artifact_store=artifact_store,
        session_ttl_hours=app_settings.academic_confirmation_ttl_hours,
    )
    user_memory_service = (
        UserMemoryService(
            store=SQLAlchemyUserMemoryStore(
                engine=database.engine,
                artifact_store=artifact_store,
            ),
            embedding_gateway=embedding_gateway,
            retrieval_limit=app_settings.user_memory_retrieval_limit,
        )
        if app_settings.user_memory_enabled
        else None
    )
    context_assembler = ConversationContextAssembler(
        settings=app_settings,
        gateway=compaction_gateway,
        conversation_service=conversation_service,
        artifact_store=artifact_store,
        user_memory_service=user_memory_service,
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
    memory_service = (
        AcademicMemoryService(
            store=store,
            model_gateway=gateway,
            embedding_gateway=embedding_gateway,
            timezone=app_settings.app_timezone,
            end_of_day_time=app_settings.academic_end_of_day_schedule,
            default_practice_minutes=app_settings.academic_memory_default_practice_minutes,
        )
        if app_settings.academic_memory_enabled
        else None
    )
    calendar_semantic_interpreter = CalendarEventSemanticInterpreter(
        gateway,
        max_prompt_chars=app_settings.calendar_semantic_prompt_max_chars,
    )
    learn_connector = _create_learn_connector(app_settings)
    learn_semantic_interpreter = LearnAnnouncementSemanticInterpreter(
        gateway,
        max_body_chars=app_settings.learn_announcement_max_body_chars,
        max_chunk_chars=app_settings.learn_announcement_chunk_chars,
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
        learn_tool_state_factory=(
            lambda message: LearnToolState(
                connector=learn_connector,
                semantic_interpreter=learn_semantic_interpreter,
                now=message.timestamp,
                proposal_builder=LearnNotionProposalBuilder(
                    model=gateway,
                    store=store,
                    timezone=app_settings.app_timezone,
                ),
                engine=database.engine,
                explicit_request_key=message.message_id,
            )
            if learn_connector is not None
            else None
        ),
        career_engine=database.engine,
        career_writer_provider=lambda: career_writer,
        material_intake=material_intake,
        memory_service=memory_service,
        user_memory_service=user_memory_service,
        context_assembler=context_assembler,
        calendar_semantic_interpreter=calendar_semantic_interpreter,
        conversation_service=conversation_service,
        abort_check=abort_check,
        activity_sink=activity_sink,
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


def _create_learn_connector(settings: Settings) -> LearnBridgeConnector | None:
    if not settings.learn_bridge_enabled or settings.learn_bridge_hmac_secret is None:
        return None
    try:
        return LearnBridgeConnector(
            base_url=str(settings.learn_bridge_url),
            hmac_secret=settings.learn_bridge_hmac_secret,
            timeout_seconds=settings.learn_bridge_timeout_seconds,
            max_response_bytes=settings.learn_bridge_max_response_bytes,
            max_clock_skew_seconds=settings.learn_bridge_max_clock_skew_seconds,
            max_courses=settings.learn_bridge_max_courses,
            max_scheduled_items=settings.learn_bridge_max_scheduled_items,
            max_announcements=settings.learn_bridge_max_announcements,
        )
    except ValueError:
        return None


__all__ = ["AcademicDiscordService", "create_academic_discord_service"]
