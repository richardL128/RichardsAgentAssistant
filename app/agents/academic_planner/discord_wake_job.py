"""Durable worker entry point for authenticated host Discord handoffs."""

from __future__ import annotations

import asyncio
import atexit
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC
from typing import Protocol, cast
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.agents.academic_planner.clarification_job import create_academic_clarification_job
from app.agents.academic_planner.discord_service import (
    AcademicDiscordService,
    create_academic_discord_service,
)
from app.agents.harness import UserAbortRequested
from app.artifacts.store import ArtifactStore
from app.connectors.discord_gateway import (
    DiscordAcademicMessageCreate,
    DiscordClarificationAction,
)
from app.core.config import Settings, get_settings
from app.db.discord_wake import (
    DISCORD_WAKE_ACTIVITY_PHASES,
    DISCORD_WAKE_SIDE_EFFECT_CLASSES,
    DISCORD_WAKE_TOOL_STATUSES,
    DiscordWakeActivityPhase,
    DiscordWakeRepository,
    DiscordWakeSideEffectClass,
    DiscordWakeToolStatus,
)
from app.db.models import DiscordWakeInbound
from app.db.session import Database


async def run_discord_wake(
    wake_id: str,
    attempt: int,
    attempt_limit: int,
) -> dict[str, object]:
    """Run one durable row; queue arguments contain only its UUID."""

    job = await _get_worker_job()
    return await job(wake_id, attempt, attempt_limit)


_worker_job: DiscordWakeJob | None = None
_worker_job_lock = asyncio.Lock()


async def _get_worker_job() -> DiscordWakeJob:
    """Return the one lazily constructed Discord wake runtime for this worker."""

    global _worker_job
    if _worker_job is not None:
        return _worker_job
    async with _worker_job_lock:
        if _worker_job is None:
            _worker_job = DiscordWakeJob(get_settings())
        return _worker_job


def set_worker_discord_wake_job(job: DiscordWakeJob | None) -> None:
    """Inject or reset the worker runtime explicitly for tests."""

    global _worker_job
    if _worker_job is not None and _worker_job is not job:
        _worker_job.close()
    _worker_job = job


def close_worker_discord_wake_job() -> None:
    """Dispose worker-scoped Discord resources at process shutdown."""

    set_worker_discord_wake_job(None)


class _ScopedServiceHandler(Protocol):
    def __call__(
        self,
        message: DiscordAcademicMessageCreate,
        *,
        abort_check: Callable[[], None] | None = None,
        activity_sink: Callable[[Mapping[str, object]], object] | None = None,
    ) -> Awaitable[object]: ...


class _InjectedDiscordService(Protocol):
    handler: Callable[[DiscordAcademicMessageCreate], Awaitable[object]]

    def close(self) -> None: ...


class DiscordWakeJob:
    """Load a verified event and route it through the existing handlers."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._database = Database(settings)
        self._service: AcademicDiscordService | _InjectedDiscordService | None = None
        self._service_lock = asyncio.Lock()
        self._closed = False

    async def _get_service(self) -> AcademicDiscordService | _InjectedDiscordService:
        if self._closed:
            raise RuntimeError("Discord wake job is closed")
        if self._service is not None:
            return self._service
        async with self._service_lock:
            if self._service is None:
                self._service = cast(
                    AcademicDiscordService | _InjectedDiscordService,
                    create_academic_discord_service(
                        self._settings,
                        database=self._database,
                    ),
                )
            return self._service

    def close(self) -> None:
        if self._closed:
            return
        if self._service is not None:
            self._service.close()
            self._service = None
        elif hasattr(self._database, "dispose"):
            self._database.dispose()
        self._closed = True

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
            if row.state in {"completed", "aborted"}:
                return {"status": "duplicate", "wake_id": wake_id}
            if _wake_abort_requested(row):
                _mark_wake_aborted(session, parsed_id)
                return {"status": "aborted", "wake_id": wake_id}
            DiscordWakeRepository.mark_running(session, parsed_id)
            snapshot = _snapshot(row)

        try:
            if snapshot.event_kind == "interaction":
                status = await self._run_interaction(snapshot, attempt, attempt_limit)
            else:
                status = await self._run_message(snapshot, wake_id=parsed_id)
        except asyncio.CancelledError:
            if self._abort_requested(parsed_id):
                with Session(self._database.engine) as session, session.begin():
                    _mark_wake_aborted(session, parsed_id)
            raise
        except Exception:
            with Session(self._database.engine) as session, session.begin():
                DiscordWakeRepository.mark_failed(
                    session,
                    parsed_id,
                    error_code="discord_wake_worker_failed",
                )
            raise

        with Session(self._database.engine) as session, session.begin():
            if status == "aborted":
                _mark_wake_aborted(session, parsed_id)
            elif status == "failed":
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

    async def _run_message(self, row: _WakeSnapshot, *, wake_id: UUID | None = None) -> str:
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
        scoped_abort_check: Callable[[], None] | None = None
        scoped_activity_sink: Callable[[Mapping[str, object]], None] | None = None
        if wake_id is not None:

            def check_abort() -> None:
                self._raise_if_abort_requested(wake_id)

            def record_activity(event: Mapping[str, object]) -> None:
                self._record_safe_activity(wake_id, event)

            scoped_abort_check = check_abort
            scoped_activity_sink = record_activity

        service = await self._get_service()
        handler = getattr(service, "handle", None)
        if callable(handler):
            scoped_handler = cast(_ScopedServiceHandler, handler)
            result = await scoped_handler(
                DiscordAcademicMessageCreate(
                    message_id=row.discord_message_id,
                    channel_id=row.discord_channel_id,
                    author_id=row.discord_user_id,
                    timestamp=row.received_at,
                    content=SecretStr(raw_content),
                    inbound_material_ids=inbound_material_ids,
                    progress_message_id=row.ack_message_id,
                ),
                abort_check=scoped_abort_check,
                activity_sink=scoped_activity_sink,
            )
        else:
            # Narrow compatibility for injected service doubles.
            legacy_service = cast(_InjectedDiscordService, service)
            result = await legacy_service.handler(
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
        return _result_status(result)

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
            "event",
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

    def _abort_requested(self, wake_id: UUID) -> bool:
        with Session(self._database.engine) as session:
            row = DiscordWakeRepository.get_by_id(session, wake_id)
            return row is not None and _wake_abort_requested(row)

    def _raise_if_abort_requested(self, wake_id: UUID | None) -> None:
        if wake_id is not None and self._abort_requested(wake_id):
            raise UserAbortRequested()

    def _record_safe_activity(self, wake_id: UUID, event: Mapping[str, object]) -> None:
        payload = _safe_activity_payload(event)
        if payload is None:
            return
        with Session(self._database.engine) as session, session.begin():
            DiscordWakeRepository.record_activity(
                session,
                wake_id,
                phase=payload.phase,
                model_turn=payload.model_turn,
                tool_name=payload.tool_name,
                tool_status=payload.tool_status,
                side_effect_class=payload.side_effect_class,
            )


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


def _wake_abort_requested(row: DiscordWakeInbound) -> bool:
    return row.state in {"abort_requested", "aborted"} or row.abort_requested_at is not None


def _mark_wake_aborted(session: Session, wake_id: UUID) -> None:
    DiscordWakeRepository.mark_aborted(session, wake_id, reason_code="user_abort")


def _result_status(result: object) -> str:
    status = getattr(result, "status", None)
    if not isinstance(status, str) or not status:
        raise RuntimeError("Discord handler returned an invalid status")
    return status


@dataclass(frozen=True, slots=True)
class _SafeActivity:
    phase: DiscordWakeActivityPhase
    model_turn: int | None
    tool_name: str | None
    tool_status: DiscordWakeToolStatus | None
    side_effect_class: DiscordWakeSideEffectClass | None


def _safe_activity_payload(event: Mapping[str, object]) -> _SafeActivity | None:
    phase_aliases = {
        "runtime_ready": "runtime_check",
        "reply_preparation": "reply_delivery",
        "terminal_aborted": "terminal",
    }
    raw_phase = str(event.get("phase", "")).strip()
    phase = phase_aliases.get(raw_phase, raw_phase)
    if phase not in DISCORD_WAKE_ACTIVITY_PHASES:
        return None
    model_turn = event.get("model_turn")
    safe_model_turn = (
        model_turn
        if isinstance(model_turn, int)
        and not isinstance(model_turn, bool)
        and 1 <= model_turn <= 50
        else None
    )

    tool_name = event.get("tool_name")
    safe_tool_name = (
        tool_name.strip()[:128] if isinstance(tool_name, str) and tool_name.strip() else None
    )

    raw_status = str(event.get("tool_status", "")).strip()
    status = "running" if raw_status == "in_flight" else raw_status
    safe_status = (
        cast(DiscordWakeToolStatus, status) if status in DISCORD_WAKE_TOOL_STATUSES else None
    )

    side_effect_class = str(event.get("side_effect_class", "")).strip()
    safe_side_effect_class = (
        cast(
            DiscordWakeSideEffectClass,
            side_effect_class,
        )
        if side_effect_class in DISCORD_WAKE_SIDE_EFFECT_CLASSES
        else None
    )
    return _SafeActivity(
        phase=cast(DiscordWakeActivityPhase, phase),
        model_turn=safe_model_turn,
        tool_name=safe_tool_name,
        tool_status=safe_status,
        side_effect_class=safe_side_effect_class,
    )


atexit.register(close_worker_discord_wake_job)


__all__ = [
    "DiscordWakeJob",
    "close_worker_discord_wake_job",
    "run_discord_wake",
    "set_worker_discord_wake_job",
]
