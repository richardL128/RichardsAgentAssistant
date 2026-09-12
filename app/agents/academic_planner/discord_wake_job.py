"""Durable worker entry point for authenticated host Discord handoffs."""

from __future__ import annotations

import json
from datetime import UTC
from typing import cast
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.agents.academic_planner.clarification_job import create_academic_clarification_job
from app.agents.academic_planner.discord_service import create_academic_discord_service
from app.artifacts.store import ArtifactStore
from app.connectors.discord_gateway import (
    DiscordAcademicMessageCreate,
    DiscordClarificationAction,
)
from app.core.config import Settings, get_settings
from app.db.discord_wake import DiscordWakeRepository
from app.db.models import DiscordWakeInbound
from app.db.session import Database


async def run_discord_wake(
    wake_id: str,
    attempt: int,
    attempt_limit: int,
) -> dict[str, object]:
    """Run one durable row; queue arguments contain only its UUID."""

    return await DiscordWakeJob(get_settings())(wake_id, attempt, attempt_limit)


class DiscordWakeJob:
    """Load a verified event and route it through the existing handlers."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._database = Database(settings)

    async def __call__(
        self,
        wake_id: str,
        attempt: int,
        attempt_limit: int,
    ) -> dict[str, object]:
        parsed_id = UUID(wake_id)
        with Session(self._database.engine) as session, session.begin():
            row = DiscordWakeRepository.get_by_id(session, parsed_id)
            if row is None:
                return {"status": "missing", "wake_id": wake_id}
            if row.state == "completed":
                return {"status": "duplicate", "wake_id": wake_id}
            DiscordWakeRepository.mark_running(session, parsed_id)
            snapshot = _snapshot(row)

        try:
            if snapshot.event_kind == "interaction":
                status = await self._run_interaction(snapshot, attempt, attempt_limit)
            else:
                status = await self._run_message(snapshot)
        except Exception:
            with Session(self._database.engine) as session, session.begin():
                DiscordWakeRepository.mark_failed(
                    session,
                    parsed_id,
                    error_code="discord_wake_worker_failed",
                )
            raise

        with Session(self._database.engine) as session, session.begin():
            if status == "failed":
                # The handler has already delivered a bounded failure to Discord.
                # Preserve that outcome without automatically repeating the turn.
                DiscordWakeRepository.mark_failed(
                    session,
                    parsed_id,
                    error_code="discord_wake_handler_failed",
                )
            else:
                DiscordWakeRepository.mark_completed(session, parsed_id)
        return {"status": status, "wake_id": wake_id}

    async def _run_message(self, row: _WakeSnapshot) -> str:
        if (
            row.discord_message_id is None
            or row.discord_channel_id is None
            or row.discord_user_id is None
        ):
            raise ValueError("Discord message handoff is incomplete")
        loaded_content = self._load_content(row.content_artifact_key)
        if isinstance(loaded_content, tuple):
            raw_content, inbound_material_ids = loaded_content
        else:
            # Preserve compatibility with legacy/custom loaders that return text.
            raw_content, inbound_material_ids = loaded_content, ()
        service = create_academic_discord_service(self._settings)
        try:
            result = await service.handler(
                DiscordAcademicMessageCreate(
                    message_id=row.discord_message_id,
                    channel_id=row.discord_channel_id,
                    author_id=row.discord_user_id,
                    timestamp=row.received_at,
                    content=SecretStr(raw_content),
                    inbound_material_ids=inbound_material_ids,
                    progress_message_id=row.ack_message_id,
                )
            )
            return result.status
        finally:
            service.close()

    async def _run_interaction(
        self,
        row: _WakeSnapshot,
        attempt: int,
        attempt_limit: int,
    ) -> str:
        if row.clarification_id is None or row.discord_user_id is None:
            raise ValueError("Discord interaction handoff is incomplete")
        if row.interaction_action not in {
            "quiz",
            "assignment",
            "tutorial",
            "lab",
            "studying_block",
            "ignore",
        }:
            raise ValueError("Discord interaction action is invalid")
        job = create_academic_clarification_job(self._settings)
        result = await job(
            str(row.clarification_id),
            cast(DiscordClarificationAction, row.interaction_action),
            row.discord_user_id,
            attempt,
            attempt_limit,
        )
        return str(result.get("status", "failed"))

    def _load_content(self, artifact_key: str) -> str | tuple[str, tuple[UUID, ...]]:
        store = ArtifactStore(
            self._settings.artifact_root,
            default_retention_days=self._settings.artifact_retention_days,
        )
        raw = store.get(artifact_key).decode("utf-8")
        try:
            value: object = json.loads(raw)
        except json.JSONDecodeError:
            value = None
        if (
            isinstance(value, dict)
            and cast(dict[str, object], value).get("version") == "discord-academic-inbound-v2"
        ):
            payload = cast(dict[str, object], value)
            content = payload.get("message_text")
            material_values = payload.get("inbound_material_ids", [])
            if not isinstance(content, str) or len(content) > 2_000:
                raise ValueError("Discord inbound manifest text is invalid")
            if not isinstance(material_values, list):
                raise ValueError("Discord inbound manifest material references are invalid")
            material_list = cast(list[object], material_values)
            if len(material_list) > 5:
                raise ValueError("Discord inbound manifest material references are invalid")
            try:
                material_ids = tuple(UUID(str(item)) for item in material_list)
            except ValueError:
                raise ValueError(
                    "Discord inbound manifest material references are invalid"
                ) from None
            if not content.strip() and not material_ids:
                raise ValueError("Discord inbound manifest is empty")
            return content, material_ids
        if not raw or len(raw) > 2_000:
            raise ValueError("Discord inbound content artifact is invalid")
        return raw, ()


class _WakeSnapshot:
    def __init__(self, row: DiscordWakeInbound) -> None:
        self.event_kind = row.event_kind
        self.action = row.action
        self.interaction_action = row.interaction_action
        self.clarification_id = row.clarification_id
        self.discord_channel_id = row.discord_channel_id
        self.discord_user_id = row.discord_user_id
        self.discord_message_id = row.discord_message_id
        self.ack_message_id = row.ack_message_id
        self.content_artifact_key = row.content_artifact_key
        received_at = row.received_at
        self.received_at = (
            received_at.replace(tzinfo=UTC)
            if received_at.tzinfo is None or received_at.utcoffset() is None
            else received_at.astimezone(UTC)
        )


def _snapshot(row: DiscordWakeInbound) -> _WakeSnapshot:
    return _WakeSnapshot(row)


__all__ = ["DiscordWakeJob", "run_discord_wake"]
