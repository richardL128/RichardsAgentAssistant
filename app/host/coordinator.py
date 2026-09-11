"""Coalesced Discord mention wake coordinator."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, cast
from uuid import UUID

from app.connectors.discord_gateway import (
    DiscordAcademicMessageCreate,
    DiscordClarificationAction,
    DiscordClarificationCallbackResult,
    DiscordClarificationInteraction,
    DiscordMessageCallbackResult,
)
from app.host.commands import HostWakeError
from app.host.discord import (
    HOST_COMMAND_ACKNOWLEDGEMENT,
    HOST_WAKE_ACKNOWLEDGEMENT,
    DiscordWakeFailure,
    safe_failure_content,
)
from app.host.handoff import (
    DiscordHostHandoff,
    DiscordHostHandoffAccepted,
    DiscordHostHandoffEvent,
    DiscordHostInteractionHandoffEvent,
    handoff_nonce,
)
from app.host.outbox import InteractionOutboxRow, WakeOutboxRow
from app.host.settings import HostWakeSettings

logger = logging.getLogger(__name__)


class DiscordWakeDelivery(Protocol):
    async def send_acknowledgement(
        self,
        *,
        channel_id: str,
        root_message_id: str,
        content: str = HOST_WAKE_ACKNOWLEDGEMENT,
    ) -> str: ...

    async def edit_acknowledgement(
        self,
        *,
        channel_id: str,
        acknowledgement_message_id: str,
        content: str,
    ) -> None: ...


class HostReadyService(Protocol):
    async def ensure_ready(self) -> None: ...


class HostOutbox(Protocol):
    def record_event(
        self,
        *,
        message_id: str,
        channel_id: str,
        author_id: str,
        event_timestamp: datetime,
        request_kind: Literal["mention", "command", "continuation"] = "mention",
    ) -> WakeOutboxRow: ...

    def mark_acknowledged(
        self,
        message_id: str,
        acknowledgement_message_id: str,
    ) -> WakeOutboxRow: ...

    def mark_accepted(self, message_id: str) -> WakeOutboxRow: ...

    def mark_failed(self, message_id: str, safe_error_code: str) -> WakeOutboxRow: ...

    def pending_rows(self, *, limit: int = 100) -> tuple[WakeOutboxRow, ...]: ...

    def prune_terminal(self, *, older_than: timedelta) -> int: ...

    def record_interaction(
        self,
        *,
        interaction_id: str,
        channel_id: str,
        user_id: str,
        clarification_id: UUID,
        action: str,
        event_timestamp: datetime,
    ) -> InteractionOutboxRow: ...

    def mark_interaction_accepted(self, interaction_id: str) -> InteractionOutboxRow: ...

    def mark_interaction_failed(
        self, interaction_id: str, safe_error_code: str
    ) -> InteractionOutboxRow: ...

    def pending_interactions(self, *, limit: int = 100) -> tuple[InteractionOutboxRow, ...]: ...


class BackendHandoff(Protocol):
    async def submit(self, event: DiscordHostHandoff) -> DiscordHostHandoffAccepted: ...


WakeResult = Literal["handled", "ignored", "duplicate", "failed"]


class HostWakeCoordinator:
    """Accept verified mentions, wake dependencies once, and hand off by reference."""

    def __init__(
        self,
        *,
        settings: HostWakeSettings,
        outbox: HostOutbox,
        discord: DiscordWakeDelivery,
        docker: HostReadyService,
        compose: HostReadyService,
        ollama: HostReadyService,
        backend_live: HostReadyService,
        handoff: BackendHandoff,
        deployment: HostReadyService | None = None,
    ) -> None:
        self._settings = settings
        self._outbox = outbox
        self._discord = discord
        self._docker = docker
        self._compose = compose
        self._ollama = ollama
        self._deployment = deployment
        self._backend_live = backend_live
        self._handoff = handoff
        self._wake_lock = asyncio.Lock()
        self._wake_task: asyncio.Task[None] | None = None
        self._interaction_tasks: dict[str, asyncio.Task[None]] = {}

    async def handle_message(
        self,
        message: DiscordAcademicMessageCreate,
    ) -> DiscordMessageCallbackResult:
        result = await self.process_message(message)
        return DiscordMessageCallbackResult(status="handled" if result == "handled" else result)

    async def process_message(self, message: DiscordAcademicMessageCreate) -> WakeResult:
        request_kind = self._request_kind(message)
        if request_kind is None:
            return "ignored"
        row = self._outbox.record_event(
            message_id=message.message_id,
            channel_id=message.channel_id,
            author_id=message.author_id,
            event_timestamp=message.timestamp,
            request_kind=request_kind,
        )
        if row.state == "accepted":
            return "duplicate"
        acknowledgement_message_id = row.acknowledgement_message_id
        if acknowledgement_message_id is None:
            if request_kind == "command":
                acknowledgement_message_id = await self._discord.send_acknowledgement(
                    channel_id=message.channel_id,
                    root_message_id=message.message_id,
                    content=HOST_COMMAND_ACKNOWLEDGEMENT,
                )
            else:
                acknowledgement_message_id = await self._discord.send_acknowledgement(
                    channel_id=message.channel_id,
                    root_message_id=message.message_id,
                )
            row = self._outbox.mark_acknowledged(message.message_id, acknowledgement_message_id)
        try:
            await self._ensure_wake_ready()
            await self._submit_row(row)
        except Exception as exc:
            failure = _failure_code(exc)
            self._outbox.mark_failed(message.message_id, failure)
            await self._edit_failure(
                channel_id=message.channel_id,
                acknowledgement_message_id=acknowledgement_message_id,
                failure=failure,
            )
            return "failed"
        self._outbox.mark_accepted(message.message_id)
        return "handled"

    async def handle_interaction(
        self,
        interaction: DiscordClarificationInteraction,
    ) -> DiscordClarificationCallbackResult:
        """Spool before Discord's deadline, then wake and hand off in background."""

        row = self._outbox.record_interaction(
            interaction_id=interaction.interaction_id,
            channel_id=interaction.channel_id,
            user_id=interaction.user_id,
            clarification_id=interaction.clarification_id,
            action=interaction.action,
            event_timestamp=_discord_snowflake_timestamp(interaction.interaction_id),
        )
        if row.state == "accepted":
            return DiscordClarificationCallbackResult(status="duplicate")
        if interaction.interaction_id not in self._interaction_tasks:
            task = asyncio.create_task(
                self._process_interaction(row),
                name=f"discord-wake-interaction-{interaction.interaction_id}",
            )
            self._interaction_tasks[interaction.interaction_id] = task
            task.add_done_callback(
                lambda _task, event_id=interaction.interaction_id: self._interaction_tasks.pop(
                    event_id, None
                )
            )
        return DiscordClarificationCallbackResult(status="queued")

    async def replay_pending(self, *, limit: int = 100) -> int:
        accepted = 0
        for row in self._outbox.pending_rows(limit=limit):
            acknowledgement_message_id = row.acknowledgement_message_id
            if acknowledgement_message_id is None:
                if row.request_kind == "command":
                    acknowledgement_message_id = await self._discord.send_acknowledgement(
                        channel_id=row.channel_id,
                        root_message_id=row.message_id,
                        content=HOST_COMMAND_ACKNOWLEDGEMENT,
                    )
                else:
                    acknowledgement_message_id = await self._discord.send_acknowledgement(
                        channel_id=row.channel_id,
                        root_message_id=row.message_id,
                    )
                row = self._outbox.mark_acknowledged(row.message_id, acknowledgement_message_id)
            try:
                await self._ensure_wake_ready()
                await self._submit_row(row)
            except Exception as exc:
                failure = _failure_code(exc)
                self._outbox.mark_failed(row.message_id, failure)
                await self._edit_failure(
                    channel_id=row.channel_id,
                    acknowledgement_message_id=acknowledgement_message_id,
                    failure=failure,
                )
                continue
            self._outbox.mark_accepted(row.message_id)
            accepted += 1
        for interaction in self._outbox.pending_interactions(limit=limit):
            try:
                await self._process_interaction(interaction)
            except Exception:
                logger.exception(
                    "Failed to replay Discord clarification interaction %s",
                    interaction.interaction_id,
                )
            else:
                accepted += 1
        return accepted

    async def drain_interaction_tasks(self) -> None:
        if self._interaction_tasks:
            await asyncio.gather(*tuple(self._interaction_tasks.values()), return_exceptions=True)

    def prune_outbox(self) -> int:
        return self._outbox.prune_terminal(
            older_than=timedelta(seconds=self._settings.outbox_retention_seconds)
        )

    async def _ensure_wake_ready(self) -> None:
        async with self._wake_lock:
            task = self._wake_task
            if task is None or task.done():
                task = asyncio.create_task(self._wake_sequence(), name="lifeagent-host-wake")
                self._wake_task = task
        try:
            await asyncio.shield(task)
        finally:
            if task.done():
                async with self._wake_lock:
                    if self._wake_task is task:
                        self._wake_task = None

    async def _wake_sequence(self) -> None:
        await asyncio.gather(self._docker.ensure_ready(), self._ollama.ensure_ready())
        if self._deployment is not None:
            await self._deployment.ensure_ready()
        await self._compose.ensure_ready()
        await self._backend_live.ensure_ready()

    async def _submit_row(self, row: WakeOutboxRow) -> None:
        event = DiscordHostHandoffEvent(
            message_id=row.message_id,
            channel_id=row.channel_id,
            author_id=row.author_id,
            event_timestamp=row.event_timestamp,
            acknowledgement_message_id=row.acknowledgement_message_id,
            handoff_timestamp=datetime.now(UTC),
            nonce=handoff_nonce(row.message_id),
        )
        await self._handoff.submit(event)

    async def _process_interaction(self, row: InteractionOutboxRow) -> None:
        try:
            await self._ensure_wake_ready()
            await self._handoff.submit(
                DiscordHostInteractionHandoffEvent(
                    interaction_id=row.interaction_id,
                    channel_id=row.channel_id,
                    user_id=row.user_id,
                    clarification_id=row.clarification_id,
                    action=cast(DiscordClarificationAction, row.action),
                    event_timestamp=row.event_timestamp,
                    handoff_timestamp=datetime.now(UTC),
                    nonce=handoff_nonce(row.interaction_id),
                )
            )
        except Exception as exc:
            self._outbox.mark_interaction_failed(row.interaction_id, _failure_code(exc))
            raise
        self._outbox.mark_interaction_accepted(row.interaction_id)

    async def _edit_failure(
        self,
        *,
        channel_id: str,
        acknowledgement_message_id: str | None,
        failure: DiscordWakeFailure,
    ) -> None:
        if acknowledgement_message_id is None:
            return
        try:
            await self._discord.edit_acknowledgement(
                channel_id=channel_id,
                acknowledgement_message_id=acknowledgement_message_id,
                content=safe_failure_content(failure),
            )
        except Exception:
            return

    def _request_kind(
        self,
        message: DiscordAcademicMessageCreate,
    ) -> Literal["mention", "command", "continuation"] | None:
        if message.channel_id != self._settings.discord_academic_channel_id:
            return None
        if message.author_id not in self._settings.discord_academic_authorized_user_ids:
            return None
        if _EXACT_COMMAND.fullmatch(message.content.get_secret_value().strip()) is not None:
            return "command"
        # The private channel and owner allowlist are the application boundary.
        # Ordinary input is deliberately not classified here: the agent harness
        # must see it intact instead of a deterministic pre-router deciding what
        # kind of conversation it is.
        return "mention"


def _failure_code(exc: Exception) -> DiscordWakeFailure:
    if isinstance(exc, HostWakeError):
        if exc.code in {"docker_timeout", "docker_unavailable"}:
            return "docker_timeout"
        if exc.code == "image_stale":
            return "image_stale"
        if exc.code in {"compose_unhealthy", "api_unhealthy"}:
            return "compose_unhealthy"
        if exc.code == "ollama_unavailable":
            return "ollama_unavailable"
    return "handoff_failed"


_EXACT_COMMAND = re.compile(
    r"(?:confirm|reject) [0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)


def _discord_snowflake_timestamp(event_id: str) -> datetime:
    discord_epoch_ms = 1_420_070_400_000
    timestamp_ms = (int(event_id) >> 22) + discord_epoch_ms
    return datetime.fromtimestamp(timestamp_ms / 1_000, UTC)


__all__ = ["HOST_WAKE_ACKNOWLEDGEMENT", "HostWakeCoordinator", "WakeResult"]
