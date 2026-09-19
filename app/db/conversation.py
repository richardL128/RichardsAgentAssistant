"""Transaction-friendly persistence for durable native conversations."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError, NoResultFound
from sqlalchemy.orm import Session

from app.db.models import (
    NativeConversationCompaction,
    NativeConversationInboundEvent,
    NativeConversationSession,
)

NativeConversationOpenState = Literal["processing", "awaiting_user"]
NativeConversationTerminalState = Literal["completed", "failed", "expired", "cancelled"]
NativeConversationCompactionStatus = Literal["valid", "superseded", "failed"]

_OPEN_STATES = frozenset(("processing", "awaiting_user"))
_TERMINAL_STATES = frozenset(("completed", "failed", "expired", "cancelled"))
_COMPACTION_STATUSES = frozenset(("valid", "superseded", "failed"))
_DISCORD_ID_PATTERN = re.compile(r"^[0-9]{5,32}$")


@dataclass(frozen=True, slots=True)
class NativeConversationEventRecord:
    """Idempotent result for one inbound Discord event."""

    event_id: uuid.UUID
    conversation_id: uuid.UUID
    event_sequence: int
    created: bool


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)


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


def _artifact_key(value: str, field: str) -> str:
    cleaned = _bounded(value, field, 64)
    if not re.fullmatch(r"[0-9a-f]{64}", cleaned):
        raise ValueError(f"{field} must be a SHA-256 artifact key")
    return cleaned


def _nonnegative(value: int, field: str) -> int:
    if value < 0:
        raise ValueError(f"{field} must be nonnegative")
    return value


def _discord_id(value: str, field: str) -> str:
    cleaned = value.strip()
    if not _DISCORD_ID_PATTERN.fullmatch(cleaned):
        raise ValueError(f"{field} must be a Discord snowflake")
    return cleaned


def _open_row_or_raise(session: Session, session_id: uuid.UUID) -> NativeConversationSession:
    row = session.get(NativeConversationSession, session_id, with_for_update=True)
    if row is None:
        raise NoResultFound(f"native conversation {session_id} was not found")
    return row


class NativeConversationRepository:
    """Low-level repository methods; callers own transactions."""

    @staticmethod
    def create_session(
        session: Session,
        *,
        root_event_id: str,
        discord_channel_id: str,
        owner_discord_user_id: str,
        transcript_artifact_key: str,
        started_at: datetime,
        expires_at: datetime,
        model_identity: str | None,
        prompt_config_version: str | None,
        tool_checkpoint_artifact_key: str | None = None,
    ) -> NativeConversationSession:
        row = NativeConversationSession(
            root_event_id=_bounded(root_event_id, "root_event_id", 255),
            discord_channel_id=_discord_id(discord_channel_id, "discord_channel_id"),
            owner_discord_user_id=_discord_id(owner_discord_user_id, "owner_discord_user_id"),
            state="processing",
            revision=1,
            next_event_sequence=1,
            transcript_artifact_key=_artifact_key(
                transcript_artifact_key,
                "transcript_artifact_key",
            ),
            tool_checkpoint_artifact_key=(
                _artifact_key(tool_checkpoint_artifact_key, "tool_checkpoint_artifact_key")
                if tool_checkpoint_artifact_key is not None
                else None
            ),
            model_identity=_optional_bounded(model_identity, "model_identity", 255),
            prompt_config_version=_optional_bounded(
                prompt_config_version,
                "prompt_config_version",
                255,
            ),
            started_at=_utc(started_at, "started_at"),
            last_turn_at=_utc(started_at, "started_at"),
            expires_at=_utc(expires_at, "expires_at"),
        )
        try:
            with session.begin_nested():
                session.add(row)
                session.flush()
        except IntegrityError:
            existing = session.scalar(
                select(NativeConversationSession)
                .where(NativeConversationSession.root_event_id == row.root_event_id)
                .with_for_update()
            )
            if existing is not None:
                return existing
            raise
        return row

    @staticmethod
    def lock_by_id(session: Session, session_id: uuid.UUID) -> NativeConversationSession:
        return _open_row_or_raise(session, session_id)

    @staticmethod
    def lock_by_event_id(
        session: Session,
        external_event_id: str,
    ) -> tuple[NativeConversationSession, NativeConversationInboundEvent] | None:
        event_id = _bounded(external_event_id, "external_event_id", 255)
        event = session.scalar(
            select(NativeConversationInboundEvent)
            .where(NativeConversationInboundEvent.external_event_id == event_id)
            .with_for_update()
        )
        if event is None:
            return None
        row = _open_row_or_raise(session, event.conversation_id)
        return row, event

    @staticmethod
    def lock_open_for_owner(
        session: Session,
        *,
        discord_channel_id: str,
        owner_discord_user_id: str,
        now: datetime,
    ) -> NativeConversationSession | None:
        current = _utc(now, "now")
        return session.scalar(
            select(NativeConversationSession)
            .where(
                NativeConversationSession.discord_channel_id
                == _discord_id(discord_channel_id, "discord_channel_id"),
                NativeConversationSession.owner_discord_user_id
                == _discord_id(owner_discord_user_id, "owner_discord_user_id"),
                NativeConversationSession.state.in_(_OPEN_STATES),
                or_(
                    NativeConversationSession.expires_at.is_(None),
                    NativeConversationSession.expires_at > current,
                ),
            )
            .order_by(NativeConversationSession.last_turn_at.desc())
            .limit(1)
            .with_for_update()
        )

    @staticmethod
    def expire_open_session_for_owner(
        session: Session,
        *,
        discord_channel_id: str,
        owner_discord_user_id: str,
        now: datetime,
    ) -> NativeConversationSession | None:
        current = _utc(now, "now")
        rows = list(
            session.scalars(
                select(NativeConversationSession)
                .where(
                    NativeConversationSession.discord_channel_id
                    == _discord_id(discord_channel_id, "discord_channel_id"),
                    NativeConversationSession.owner_discord_user_id
                    == _discord_id(owner_discord_user_id, "owner_discord_user_id"),
                    NativeConversationSession.state.in_(_OPEN_STATES),
                    NativeConversationSession.expires_at.is_not(None),
                    NativeConversationSession.expires_at <= current,
                )
                .order_by(NativeConversationSession.last_turn_at.desc())
                .with_for_update()
            )
        )
        for row in rows:
            row.state = "expired"
            row.last_disposition = "expired"
            row.completed_at = current
            row.last_turn_at = current
            row.revision += 1
        session.flush()
        return rows[0] if rows else None

    @staticmethod
    def record_inbound_event(
        session: Session,
        *,
        conversation_id: uuid.UUID,
        external_event_id: str,
        received_at: datetime,
    ) -> NativeConversationEventRecord:
        event_id = _bounded(external_event_id, "external_event_id", 255)
        existing = session.scalar(
            select(NativeConversationInboundEvent)
            .where(NativeConversationInboundEvent.external_event_id == event_id)
            .with_for_update()
        )
        if existing is not None:
            if existing.conversation_id != conversation_id:
                raise ValueError("native conversation event belongs to another conversation")
            return NativeConversationEventRecord(
                event_id=existing.id,
                conversation_id=existing.conversation_id,
                event_sequence=existing.event_sequence,
                created=False,
            )
        row = _open_row_or_raise(session, conversation_id)
        sequence = row.next_event_sequence
        event = NativeConversationInboundEvent(
            conversation_id=row.id,
            external_event_id=event_id,
            event_sequence=sequence,
            received_at=_utc(received_at, "received_at"),
        )
        try:
            with session.begin_nested():
                session.add(event)
                row.next_event_sequence = sequence + 1
                session.flush()
        except IntegrityError:
            existing = session.scalar(
                select(NativeConversationInboundEvent)
                .where(NativeConversationInboundEvent.external_event_id == event_id)
                .with_for_update()
            )
            if existing is None:
                raise
            if existing.conversation_id != conversation_id:
                raise ValueError(
                    "native conversation event belongs to another conversation"
                ) from None
            return NativeConversationEventRecord(
                event_id=existing.id,
                conversation_id=existing.conversation_id,
                event_sequence=existing.event_sequence,
                created=False,
            )
        return NativeConversationEventRecord(
            event_id=event.id,
            conversation_id=event.conversation_id,
            event_sequence=event.event_sequence,
            created=True,
        )

    @staticmethod
    def update_artifacts(
        session: Session,
        *,
        conversation_id: uuid.UUID,
        transcript_artifact_key: str,
        now: datetime,
        tool_checkpoint_artifact_key: str | Literal["unchanged"] | None = "unchanged",
        state: str | None = None,
        last_disposition: str | None = None,
        error_code: str | None = None,
    ) -> NativeConversationSession:
        row = _open_row_or_raise(session, conversation_id)
        current = _utc(now, "now")
        row.transcript_artifact_key = _artifact_key(
            transcript_artifact_key,
            "transcript_artifact_key",
        )
        if tool_checkpoint_artifact_key != "unchanged":
            row.tool_checkpoint_artifact_key = (
                _artifact_key(tool_checkpoint_artifact_key, "tool_checkpoint_artifact_key")
                if tool_checkpoint_artifact_key is not None
                else None
            )
        if state is not None:
            if state not in _OPEN_STATES and state not in _TERMINAL_STATES:
                raise ValueError("native conversation state is invalid")
            row.state = state
            if state in _TERMINAL_STATES:
                row.completed_at = current
        if last_disposition is not None:
            if last_disposition not in _TERMINAL_STATES and last_disposition != "awaiting_user":
                raise ValueError("native conversation disposition is invalid")
            row.last_disposition = last_disposition
        row.error_code = _optional_bounded(error_code, "error_code", 128)
        row.last_turn_at = current
        row.revision += 1
        session.flush()
        return row

    @staticmethod
    def expire_open_sessions(session: Session, *, now: datetime) -> int:
        current = _utc(now, "now")
        rows = list(
            session.scalars(
                select(NativeConversationSession)
                .where(
                    NativeConversationSession.state.in_(_OPEN_STATES),
                    NativeConversationSession.expires_at.is_not(None),
                    NativeConversationSession.expires_at <= current,
                )
                .with_for_update()
            )
        )
        for row in rows:
            row.state = "expired"
            row.last_disposition = "expired"
            row.completed_at = current
            row.last_turn_at = current
            row.revision += 1
        session.flush()
        return len(rows)

    @staticmethod
    def fail_corrupt(
        session: Session,
        *,
        conversation_id: uuid.UUID,
        error_code: str = "conversation_artifact_corrupt",
        now: datetime | None = None,
    ) -> NativeConversationSession:
        row = _open_row_or_raise(session, conversation_id)
        current = _utc(now, "now") if now is not None else _utc_now()
        row.state = "failed"
        row.last_disposition = "failed"
        row.error_code = _bounded(error_code, "error_code", 128)
        row.completed_at = current
        row.last_turn_at = current
        row.revision += 1
        session.flush()
        return row

    @staticmethod
    def latest_valid_compaction(
        session: Session,
        *,
        conversation_id: uuid.UUID,
    ) -> NativeConversationCompaction | None:
        _open_row_or_raise(session, conversation_id)
        return session.scalar(
            select(NativeConversationCompaction)
            .where(
                NativeConversationCompaction.conversation_id == conversation_id,
                NativeConversationCompaction.status == "valid",
            )
            .order_by(
                NativeConversationCompaction.covered_through_message_index.desc(),
                NativeConversationCompaction.created_at.desc(),
            )
            .limit(1)
        )

    @staticmethod
    def valid_compaction_for_range(
        session: Session,
        *,
        conversation_id: uuid.UUID,
        covered_from_message_index: int,
        covered_through_message_index: int,
    ) -> NativeConversationCompaction | None:
        _open_row_or_raise(session, conversation_id)
        start = _nonnegative(covered_from_message_index, "covered_from_message_index")
        end = _nonnegative(covered_through_message_index, "covered_through_message_index")
        if end < start:
            raise ValueError("covered_through_message_index must be >= covered_from_message_index")
        return session.scalar(
            select(NativeConversationCompaction)
            .where(
                NativeConversationCompaction.conversation_id == conversation_id,
                NativeConversationCompaction.covered_from_message_index == start,
                NativeConversationCompaction.covered_through_message_index == end,
                NativeConversationCompaction.status == "valid",
            )
            .order_by(NativeConversationCompaction.created_at.desc())
            .limit(1)
        )

    @staticmethod
    def create_compaction(
        session: Session,
        *,
        conversation_id: uuid.UUID,
        covered_from_message_index: int,
        covered_through_message_index: int,
        source_transcript_artifact_key: str,
        source_fingerprint: str,
        summary_artifact_key: str,
        summary_model_identity: str,
        summary_prompt_version: str,
        estimated_input_tokens: int,
        parent_compaction_id: uuid.UUID | None = None,
        reported_input_tokens: int | None = None,
        reported_output_tokens: int | None = None,
        status: NativeConversationCompactionStatus = "valid",
        error_code: str | None = None,
    ) -> NativeConversationCompaction:
        _open_row_or_raise(session, conversation_id)
        start = _nonnegative(covered_from_message_index, "covered_from_message_index")
        end = _nonnegative(covered_through_message_index, "covered_through_message_index")
        if end < start:
            raise ValueError("covered_through_message_index must be >= covered_from_message_index")
        if status not in _COMPACTION_STATUSES:
            raise ValueError("native conversation compaction status is invalid")
        if status != "failed" and error_code is not None:
            raise ValueError("error_code is only recorded for failed compactions")
        if reported_input_tokens is not None:
            _nonnegative(reported_input_tokens, "reported_input_tokens")
        if reported_output_tokens is not None:
            _nonnegative(reported_output_tokens, "reported_output_tokens")
        if parent_compaction_id is not None:
            parent = session.get(NativeConversationCompaction, parent_compaction_id)
            if parent is None or parent.conversation_id != conversation_id:
                raise ValueError("parent compaction does not belong to the conversation")

        fingerprint = _bounded(source_fingerprint, "source_fingerprint", 128)
        prompt_version = _bounded(summary_prompt_version, "summary_prompt_version", 128)
        existing = session.scalar(
            select(NativeConversationCompaction)
            .where(
                NativeConversationCompaction.conversation_id == conversation_id,
                NativeConversationCompaction.covered_from_message_index == start,
                NativeConversationCompaction.covered_through_message_index == end,
                NativeConversationCompaction.source_fingerprint == fingerprint,
                NativeConversationCompaction.summary_prompt_version == prompt_version,
            )
            .with_for_update()
        )
        if existing is not None:
            if existing.status == "failed" and status == "valid":
                existing.parent_compaction_id = parent_compaction_id
                existing.source_transcript_artifact_key = _artifact_key(
                    source_transcript_artifact_key,
                    "source_transcript_artifact_key",
                )
                existing.summary_artifact_key = _artifact_key(
                    summary_artifact_key,
                    "summary_artifact_key",
                )
                existing.summary_model_identity = _bounded(
                    summary_model_identity,
                    "summary_model_identity",
                    255,
                )
                existing.estimated_input_tokens = _nonnegative(
                    estimated_input_tokens,
                    "estimated_input_tokens",
                )
                existing.reported_input_tokens = reported_input_tokens
                existing.reported_output_tokens = reported_output_tokens
                existing.status = "valid"
                existing.error_code = None
                session.flush()
            return existing

        row = NativeConversationCompaction(
            conversation_id=conversation_id,
            parent_compaction_id=parent_compaction_id,
            covered_from_message_index=start,
            covered_through_message_index=end,
            source_transcript_artifact_key=_artifact_key(
                source_transcript_artifact_key,
                "source_transcript_artifact_key",
            ),
            source_fingerprint=fingerprint,
            summary_artifact_key=_artifact_key(summary_artifact_key, "summary_artifact_key"),
            summary_model_identity=_bounded(summary_model_identity, "summary_model_identity", 255),
            summary_prompt_version=prompt_version,
            estimated_input_tokens=_nonnegative(
                estimated_input_tokens,
                "estimated_input_tokens",
            ),
            reported_input_tokens=reported_input_tokens,
            reported_output_tokens=reported_output_tokens,
            status=status,
            error_code=_optional_bounded(error_code, "error_code", 128),
        )
        session.add(row)
        session.flush()
        return row

    @staticmethod
    def mark_compaction_status(
        session: Session,
        *,
        compaction_id: uuid.UUID,
        status: NativeConversationCompactionStatus,
        error_code: str | None = None,
    ) -> NativeConversationCompaction:
        if status not in _COMPACTION_STATUSES:
            raise ValueError("native conversation compaction status is invalid")
        row = session.get(NativeConversationCompaction, compaction_id, with_for_update=True)
        if row is None:
            raise NoResultFound(f"native conversation compaction {compaction_id} was not found")
        if status != "failed" and error_code is not None:
            raise ValueError("error_code is only recorded for failed compactions")
        if row.status == status and row.error_code == error_code:
            return row
        row.status = status
        row.error_code = _optional_bounded(error_code, "error_code", 128)
        session.flush()
        return row

    @staticmethod
    def supersede_valid_compactions(
        session: Session,
        *,
        conversation_id: uuid.UUID,
        through_message_index: int,
        excluding_compaction_id: uuid.UUID | None = None,
    ) -> int:
        _open_row_or_raise(session, conversation_id)
        through = _nonnegative(through_message_index, "through_message_index")
        statement = (
            select(NativeConversationCompaction)
            .where(
                NativeConversationCompaction.conversation_id == conversation_id,
                NativeConversationCompaction.status == "valid",
                NativeConversationCompaction.covered_through_message_index <= through,
            )
            .with_for_update()
        )
        if excluding_compaction_id is not None:
            statement = statement.where(
                NativeConversationCompaction.id != excluding_compaction_id,
            )
        rows = list(session.scalars(statement))
        for row in rows:
            row.status = "superseded"
            row.error_code = None
        session.flush()
        return len(rows)


__all__ = [
    "NativeConversationCompactionStatus",
    "NativeConversationEventRecord",
    "NativeConversationOpenState",
    "NativeConversationRepository",
    "NativeConversationTerminalState",
]
