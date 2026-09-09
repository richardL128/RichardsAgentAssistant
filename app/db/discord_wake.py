"""Durable Discord wake handoff persistence.

This boundary stores only verified Discord IDs, allowlisted routing metadata,
and an artifact key for redacted inbound content. It deliberately has no queue
imports so gateway and worker code can compose it inside their own transactions.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, NoResultFound
from sqlalchemy.orm import Session

from app.db.models import DiscordWakeInbound

DiscordWakeEventKind = Literal["message", "interaction"]
DiscordWakeAction = Literal[
    "academic_checkin",
    "academic_continuation",
    "agent_clarification",
    "proposal_confirmation",
    "proposal_rejection",
]
DiscordWakeState = Literal["queued", "running", "completed", "failed"]
DiscordWakeAcceptStatus = Literal["created", "replayed"]

DISCORD_WAKE_ACTIONS: frozenset[str] = frozenset(
    (
        "academic_checkin",
        "academic_continuation",
        "agent_clarification",
        "proposal_confirmation",
        "proposal_rejection",
    )
)
DISCORD_WAKE_EVENT_KINDS: frozenset[str] = frozenset(("message", "interaction"))
DISCORD_WAKE_STATES: frozenset[str] = frozenset(("queued", "running", "completed", "failed"))
DISCORD_WAKE_NONCE_MAX_LENGTH = 64


class DiscordWakeNonceReplayError(ValueError):
    """Raised when a handoff nonce is reused by a different Discord event."""


@dataclass(frozen=True, slots=True)
class DiscordWakeInboundInput:
    """Verified inbound Discord wake handoff metadata.

    Raw Discord message or interaction content is not accepted here. Callers
    must write redacted content to the artifact store first and pass only the
    resulting key.
    """

    discord_event_id: str
    handoff_nonce: str
    event_kind: DiscordWakeEventKind
    action: DiscordWakeAction
    content_artifact_key: str
    received_at: datetime
    action_id: uuid.UUID | None = None
    clarification_id: uuid.UUID | None = None
    interaction_action: str | None = None
    discord_channel_id: str | None = None
    discord_user_id: str | None = None
    discord_message_id: str | None = None
    discord_interaction_id: str | None = None
    ack_message_id: str | None = None


@dataclass(frozen=True, slots=True)
class DiscordWakeAcceptResult:
    """Result returned after creating or replaying an inbound handoff."""

    status: DiscordWakeAcceptStatus
    wake_id: uuid.UUID
    state: str
    enqueued: bool


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _bounded(value: str, field: str, max_length: int) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} must not be empty")
    if len(cleaned) > max_length:
        raise ValueError(f"{field} must be at most {max_length} characters")
    return cleaned


def _optional_bounded(value: str | None, field: str, max_length: int) -> str | None:
    if value is None:
        return None
    return _bounded(value, field, max_length)


def _validate_event(event: DiscordWakeInboundInput) -> dict[str, object]:
    if event.event_kind not in DISCORD_WAKE_EVENT_KINDS:
        raise ValueError("Discord wake event kind is invalid")
    if event.action not in DISCORD_WAKE_ACTIONS:
        raise ValueError("Discord wake action is invalid")
    interaction_action = event.interaction_action
    if event.event_kind == "interaction":
        if interaction_action not in {
            "quiz",
            "assignment",
            "tutorial",
            "lab",
            "studying_block",
            "ignore",
        }:
            raise ValueError("Discord wake interaction action is invalid")
    elif interaction_action is not None:
        raise ValueError("Discord message wake cannot contain an interaction action")
    return {
        "discord_event_id": _bounded(event.discord_event_id, "discord_event_id", 255),
        "handoff_nonce": _bounded(
            event.handoff_nonce, "handoff_nonce", DISCORD_WAKE_NONCE_MAX_LENGTH
        ),
        "event_kind": event.event_kind,
        "action": event.action,
        "action_id": event.action_id,
        "clarification_id": event.clarification_id,
        "interaction_action": interaction_action,
        "discord_channel_id": _optional_bounded(event.discord_channel_id, "discord_channel_id", 32),
        "discord_user_id": _optional_bounded(event.discord_user_id, "discord_user_id", 32),
        "discord_message_id": _optional_bounded(event.discord_message_id, "discord_message_id", 32),
        "discord_interaction_id": _optional_bounded(
            event.discord_interaction_id, "discord_interaction_id", 32
        ),
        "ack_message_id": _optional_bounded(event.ack_message_id, "ack_message_id", 32),
        "content_artifact_key": _bounded(event.content_artifact_key, "content_artifact_key", 512),
        "received_at": _utc(event.received_at, "received_at"),
    }


def _wake_or_raise(session: Session, wake_id: uuid.UUID) -> DiscordWakeInbound:
    wake = session.get(DiscordWakeInbound, wake_id, with_for_update=True)
    if wake is None:
        raise NoResultFound(f"Discord wake inbound handoff {wake_id} was not found")
    return wake


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DiscordWakeRepository:
    """Accept and advance verified Discord wake handoffs without committing."""

    @staticmethod
    def accept_verified_event(
        session: Session,
        event: DiscordWakeInboundInput,
    ) -> DiscordWakeAcceptResult:
        """Create one inbound handoff, idempotently replaying the same event ID."""

        values = _validate_event(event)
        existing_event = session.scalar(
            select(DiscordWakeInbound)
            .where(DiscordWakeInbound.discord_event_id == values["discord_event_id"])
            .with_for_update()
        )
        if existing_event is not None:
            if existing_event.handoff_nonce != values["handoff_nonce"]:
                raise ValueError("Discord event ID replay used a different handoff nonce")
            return DiscordWakeAcceptResult(
                status="replayed",
                wake_id=existing_event.id,
                state=existing_event.state,
                enqueued=existing_event.enqueued_at is not None,
            )

        existing_nonce = session.scalar(
            select(DiscordWakeInbound)
            .where(DiscordWakeInbound.handoff_nonce == values["handoff_nonce"])
            .with_for_update()
        )
        if existing_nonce is not None:
            raise DiscordWakeNonceReplayError(
                "Discord wake handoff nonce was already used by another event"
            )

        wake = DiscordWakeInbound(**values)
        try:
            with session.begin_nested():
                session.add(wake)
                session.flush()
        except IntegrityError:
            existing_event = session.scalar(
                select(DiscordWakeInbound)
                .where(DiscordWakeInbound.discord_event_id == values["discord_event_id"])
                .with_for_update()
            )
            if existing_event is not None:
                if existing_event.handoff_nonce != values["handoff_nonce"]:
                    raise ValueError(
                        "Discord event ID replay used a different handoff nonce"
                    ) from None
                return DiscordWakeAcceptResult(
                    status="replayed",
                    wake_id=existing_event.id,
                    state=existing_event.state,
                    enqueued=existing_event.enqueued_at is not None,
                )
            existing_nonce = session.scalar(
                select(DiscordWakeInbound)
                .where(DiscordWakeInbound.handoff_nonce == values["handoff_nonce"])
                .with_for_update()
            )
            if existing_nonce is not None:
                raise DiscordWakeNonceReplayError(
                    "Discord wake handoff nonce was already used by another event"
                ) from None
            raise

        return DiscordWakeAcceptResult(
            status="created",
            wake_id=wake.id,
            state=wake.state,
            enqueued=False,
        )

    @staticmethod
    def mark_enqueued(session: Session, wake_id: uuid.UUID) -> DiscordWakeInbound:
        """Record successful queue acceptance so later handoffs cannot add another job."""

        wake = _wake_or_raise(session, wake_id)
        if wake.enqueued_at is None:
            wake.enqueued_at = _utc_now()
            session.flush()
        return wake

    @staticmethod
    def get_by_id(session: Session, wake_id: uuid.UUID) -> DiscordWakeInbound | None:
        return session.get(DiscordWakeInbound, wake_id)

    @staticmethod
    def get_by_event_id(session: Session, discord_event_id: str) -> DiscordWakeInbound | None:
        event_id = _bounded(discord_event_id, "discord_event_id", 255)
        return session.scalar(
            select(DiscordWakeInbound).where(DiscordWakeInbound.discord_event_id == event_id)
        )

    @staticmethod
    def mark_running(session: Session, wake_id: uuid.UUID) -> DiscordWakeInbound:
        wake = _wake_or_raise(session, wake_id)
        if wake.state == "completed":
            raise ValueError("completed Discord wake handoff cannot return to running")
        wake.state = "running"
        wake.retry_count += 1
        wake.started_at = _utc_now()
        wake.last_error_code = None
        wake.failed_at = None
        session.flush()
        return wake

    @staticmethod
    def mark_completed(session: Session, wake_id: uuid.UUID) -> DiscordWakeInbound:
        wake = _wake_or_raise(session, wake_id)
        if wake.state == "failed":
            raise ValueError("failed Discord wake handoff must be marked running before completion")
        wake.state = "completed"
        wake.completed_at = _utc_now()
        wake.last_error_code = None
        session.flush()
        return wake

    @staticmethod
    def mark_failed(
        session: Session,
        wake_id: uuid.UUID,
        *,
        error_code: str,
    ) -> DiscordWakeInbound:
        wake = _wake_or_raise(session, wake_id)
        if wake.state == "completed":
            raise ValueError("completed Discord wake handoff cannot be marked failed")
        wake.state = "failed"
        wake.last_error_code = _bounded(error_code, "error_code", 128)
        wake.failed_at = _utc_now()
        session.flush()
        return wake


__all__ = [
    "DISCORD_WAKE_ACTIONS",
    "DISCORD_WAKE_EVENT_KINDS",
    "DISCORD_WAKE_NONCE_MAX_LENGTH",
    "DISCORD_WAKE_STATES",
    "DiscordWakeAcceptResult",
    "DiscordWakeAction",
    "DiscordWakeEventKind",
    "DiscordWakeInboundInput",
    "DiscordWakeNonceReplayError",
    "DiscordWakeRepository",
    "DiscordWakeState",
]
