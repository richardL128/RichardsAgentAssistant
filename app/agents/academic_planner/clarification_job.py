"""Durable queue handlers for Discord assessment clarification decisions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from app.agents.academic_planner.sync import AcademicClarificationService, AcademicSyncStore
from app.connectors.discord import DiscordAcademicPlannerAdapter
from app.connectors.discord_gateway import DiscordClarificationAction
from app.connectors.notion import NotionConnector
from app.core.config import Settings, get_settings
from app.db.academic import SQLAlchemyAcademicPlannerStore
from app.db.session import Database
from app.queue.tasks import defer_academic_clarification_status

_TERMINAL_STATES = frozenset({"applied", "ignored", "conflict", "failed", "expired"})
_ACTION_LABELS: dict[str, str] = {
    "quiz": "Quiz",
    "assignment": "Assignment",
    "tutorial": "Tutorial",
    "lab": "Lab",
    "studying_block": "Studying Block",
    "ignore": "Ignore",
}


class AcademicClarificationStatusAdapter(Protocol):
    async def edit_clarification(
        self,
        *,
        channel_id: str,
        message_id: str,
        content: str,
    ) -> Any: ...


StatusDeferrer = Callable[
    [str, DiscordClarificationAction],
    Awaitable[Any],
]


@dataclass(slots=True)
class AcademicClarificationJob:
    """Apply a queued clarification choice and enqueue the terminal bot edit."""

    store: AcademicSyncStore
    connector: NotionConnector | None
    status_deferrer: StatusDeferrer

    async def __call__(
        self,
        clarification_id: str,
        action: DiscordClarificationAction,
        user_id: str,
        attempt: int,
        attempt_limit: int,
    ) -> dict[str, Any]:
        row = self.store.get_clarification(clarification_id)
        if row is None:
            return {"status": "invalid", "clarification_id": clarification_id}
        initial_state = str(row.get("state"))
        if initial_state in _TERMINAL_STATES:
            state = initial_state
        elif self.connector is None:
            self.store.mark_clarification_failed(
                clarification_id,
                error_code="notion_configuration_missing",
            )
            row = self.store.get_clarification(clarification_id)
            state = str(row.get("state")) if row is not None else "missing"
        else:
            service = AcademicClarificationService(
                store=self.store,
                connector=self.connector,
            )
            result = await service.apply_choice(
                clarification_id=clarification_id,
                action=action,
                user_id=user_id,
                attempt=attempt,
                attempt_limit=attempt_limit,
            )
            row = self.store.get_clarification(clarification_id)
            if row is None:
                return {"status": result.status, "clarification_id": clarification_id}
            state = str(row.get("state"))
        if state in _TERMINAL_STATES:
            await self.status_deferrer(clarification_id, action)
        return {"status": state, "clarification_id": clarification_id}


@dataclass(slots=True)
class AcademicClarificationStatusJob:
    """Deliver terminal clarification state to Discord with an independent retry budget."""

    store: AcademicSyncStore
    adapter: AcademicClarificationStatusAdapter | None
    channel_id: str | None

    async def __call__(
        self,
        clarification_id: str,
        action: DiscordClarificationAction,
        attempt: int,
        attempt_limit: int,
    ) -> dict[str, Any]:
        row = self.store.get_clarification(clarification_id)
        if row is None:
            return {"status": "invalid", "clarification_id": clarification_id}
        state = str(row.get("state"))
        if state not in _TERMINAL_STATES:
            return {"status": "not_terminal", "clarification_id": clarification_id}
        delivery_id = row.get("delivery_id")
        if (
            self.adapter is None
            or self.channel_id is None
            or not isinstance(delivery_id, str)
            or not delivery_id
        ):
            return {"status": "skipped", "clarification_id": clarification_id}
        await self.adapter.edit_clarification(
            channel_id=self.channel_id,
            message_id=delivery_id,
            content=render_clarification_terminal_content(row, action),
        )
        return {
            "status": "edited",
            "clarification_id": clarification_id,
            "attempt": attempt,
            "attempt_limit": attempt_limit,
        }


def render_clarification_terminal_content(
    row: Mapping[str, Any],
    action: DiscordClarificationAction,
) -> str:
    """Render deterministic terminal Discord content without interaction tokens."""

    state = str(row.get("state"))
    decision = row.get("decision")
    selected = decision if decision in _ACTION_LABELS else action
    label = _ACTION_LABELS[str(selected)]
    if state == "applied":
        return f"Confirmed choice: {label}. The Notion assessment was updated once."
    if state == "ignored":
        return "Confirmed choice: Ignore. No Notion change was made."
    if state == "conflict":
        return (
            f"Choice received: {label}. I could not update Notion because the assessment "
            "changed after this prompt was sent. Please sync again for a fresh prompt."
        )
    if state == "failed":
        return (
            f"Choice received: {label}. I could not update Notion after retries. "
            "Please check the Notion integration and try again."
        )
    if state == "expired":
        return "This clarification expired before a choice could be applied. Please sync again."
    return f"Choice received: {label}. This clarification is no longer active."


async def run_academic_clarification(
    clarification_id: str,
    action: DiscordClarificationAction,
    user_id: str,
    attempt: int,
    attempt_limit: int,
) -> dict[str, Any]:
    job = create_academic_clarification_job()
    return await job(clarification_id, action, user_id, attempt, attempt_limit)


async def run_academic_clarification_status(
    clarification_id: str,
    action: DiscordClarificationAction,
    attempt: int,
    attempt_limit: int,
) -> dict[str, Any]:
    job = create_academic_clarification_status_job()
    return await job(clarification_id, action, attempt, attempt_limit)


def create_academic_clarification_job(
    settings: Settings | None = None,
) -> AcademicClarificationJob:
    app_settings = settings or get_settings()
    return AcademicClarificationJob(
        store=_create_store(app_settings),
        connector=_create_notion_connector(app_settings),
        status_deferrer=_defer_status,
    )


def create_academic_clarification_status_job(
    settings: Settings | None = None,
) -> AcademicClarificationStatusJob:
    app_settings = settings or get_settings()
    adapter, channel_id = _create_status_adapter(app_settings)
    return AcademicClarificationStatusJob(
        store=_create_store(app_settings),
        adapter=adapter,
        channel_id=channel_id,
    )


async def _defer_status(clarification_id: str, action: DiscordClarificationAction) -> Any:
    return await defer_academic_clarification_status(
        clarification_id=clarification_id,
        action=action,
    )


def _create_store(settings: Settings) -> AcademicSyncStore:
    database = Database(settings)
    return SQLAlchemyAcademicPlannerStore(
        database.engine,
        confirmation_ttl_hours=settings.academic_confirmation_ttl_hours,
        default_practice_minutes=settings.academic_memory_default_practice_minutes,
    )


def _create_notion_connector(settings: Settings) -> NotionConnector | None:
    if settings.notion_token is None or settings.notion_courses_database_id is None:
        return None
    try:
        return NotionConnector(
            token=settings.notion_token,
            courses_database_id=settings.notion_courses_database_id,
            timeout_seconds=settings.connector_timeout_seconds,
        )
    except ValueError:
        return None


def _create_status_adapter(
    settings: Settings,
) -> tuple[AcademicClarificationStatusAdapter | None, str | None]:
    if settings.discord_bot_token is None or settings.discord_academic_channel_id is None:
        return None, settings.discord_academic_channel_id
    return (
        DiscordAcademicPlannerAdapter(
            token=settings.discord_bot_token,
            allowed_channel_ids={settings.discord_academic_channel_id},
            base_url=settings.discord_api_url,
        ),
        settings.discord_academic_channel_id,
    )


__all__ = [
    "AcademicClarificationJob",
    "AcademicClarificationStatusAdapter",
    "AcademicClarificationStatusJob",
    "create_academic_clarification_job",
    "create_academic_clarification_status_job",
    "render_clarification_terminal_content",
    "run_academic_clarification",
    "run_academic_clarification_status",
]
