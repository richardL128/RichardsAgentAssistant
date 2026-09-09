"""Restrictive ID-only SQLite outbox for host wake handoff retries."""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

OutboxState = Literal["pending", "acknowledged", "accepted", "failed"]
WakeRequestKind = Literal["mention", "command", "continuation"]
_DISCORD_ID = re.compile(r"^[0-9]{5,24}$")
_SAFE_ERROR = re.compile(r"^[a-z0-9_]{1,64}$")


@dataclass(frozen=True, slots=True)
class WakeOutboxRow:
    message_id: str
    channel_id: str
    author_id: str
    event_timestamp: datetime
    request_kind: WakeRequestKind
    acknowledgement_message_id: str | None
    state: OutboxState
    retry_count: int
    safe_error_code: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class InteractionOutboxRow:
    interaction_id: str
    channel_id: str
    user_id: str
    clarification_id: UUID
    action: str
    event_timestamp: datetime
    state: OutboxState
    retry_count: int
    safe_error_code: str | None
    created_at: datetime
    updated_at: datetime


class WakeOutbox:
    """Small SQLite store that never accepts raw Discord content."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._ensure_store()

    @property
    def path(self) -> Path:
        return self._path

    def record_event(
        self,
        *,
        message_id: str,
        channel_id: str,
        author_id: str,
        event_timestamp: datetime,
        request_kind: WakeRequestKind = "mention",
    ) -> WakeOutboxRow:
        _require_id(message_id)
        _require_id(channel_id)
        _require_id(author_id)
        if request_kind not in {"mention", "command", "continuation"}:
            raise ValueError("wake request kind is invalid")
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO wake_events (
                    message_id, channel_id, author_id, event_timestamp, request_kind,
                    state, retry_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (
                    message_id,
                    channel_id,
                    author_id,
                    _to_text(event_timestamp),
                    request_kind,
                    _to_text(now),
                    _to_text(now),
                ),
            )
            connection.commit()
        row = self.get(message_id)
        if (
            row.channel_id != channel_id
            or row.author_id != author_id
            or row.event_timestamp != event_timestamp.astimezone(UTC)
            or row.request_kind != request_kind
        ):
            raise ValueError("Discord wake replay metadata does not match")
        return row

    def mark_acknowledged(self, message_id: str, acknowledgement_message_id: str) -> WakeOutboxRow:
        _require_id(message_id)
        _require_id(acknowledgement_message_id)
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE wake_events
                   SET acknowledgement_message_id = ?,
                       state = CASE WHEN state = 'accepted' THEN state ELSE 'acknowledged' END,
                       updated_at = ?
                 WHERE message_id = ?
                """,
                (acknowledgement_message_id, _to_text(now), message_id),
            )
            connection.commit()
        return self.get(message_id)

    def mark_accepted(self, message_id: str) -> WakeOutboxRow:
        _require_id(message_id)
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE wake_events
                   SET state = 'accepted',
                       safe_error_code = NULL,
                       updated_at = ?
                 WHERE message_id = ?
                """,
                (_to_text(now), message_id),
            )
            connection.commit()
        return self.get(message_id)

    def mark_failed(self, message_id: str, safe_error_code: str) -> WakeOutboxRow:
        _require_id(message_id)
        if _SAFE_ERROR.fullmatch(safe_error_code) is None:
            raise ValueError("safe error code is invalid")
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE wake_events
                   SET state = 'failed',
                       retry_count = retry_count + 1,
                       safe_error_code = ?,
                       updated_at = ?
                 WHERE message_id = ?
                """,
                (safe_error_code, _to_text(now), message_id),
            )
            connection.commit()
        return self.get(message_id)

    def record_interaction(
        self,
        *,
        interaction_id: str,
        channel_id: str,
        user_id: str,
        clarification_id: UUID,
        action: str,
        event_timestamp: datetime,
    ) -> InteractionOutboxRow:
        _require_id(interaction_id)
        _require_id(channel_id)
        _require_id(user_id)
        if action not in {"quiz", "assignment", "tutorial", "lab", "studying_block", "ignore"}:
            raise ValueError("interaction action is invalid")
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO interaction_events (
                    interaction_id, channel_id, user_id, clarification_id, action,
                    event_timestamp, state, retry_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (
                    interaction_id,
                    channel_id,
                    user_id,
                    str(clarification_id),
                    action,
                    _to_text(event_timestamp),
                    _to_text(now),
                    _to_text(now),
                ),
            )
            connection.commit()
        row = self.get_interaction(interaction_id)
        if (
            row.channel_id != channel_id
            or row.user_id != user_id
            or row.clarification_id != clarification_id
            or row.action != action
            or row.event_timestamp != event_timestamp.astimezone(UTC)
        ):
            raise ValueError("Discord interaction replay metadata does not match")
        return row

    def mark_interaction_accepted(self, interaction_id: str) -> InteractionOutboxRow:
        _require_id(interaction_id)
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE interaction_events
                   SET state = 'accepted', safe_error_code = NULL, updated_at = ?
                 WHERE interaction_id = ?
                """,
                (_to_text(now), interaction_id),
            )
            connection.commit()
        return self.get_interaction(interaction_id)

    def mark_interaction_failed(
        self,
        interaction_id: str,
        safe_error_code: str,
    ) -> InteractionOutboxRow:
        _require_id(interaction_id)
        if _SAFE_ERROR.fullmatch(safe_error_code) is None:
            raise ValueError("safe error code is invalid")
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE interaction_events
                   SET state = 'failed', retry_count = retry_count + 1,
                       safe_error_code = ?, updated_at = ?
                 WHERE interaction_id = ?
                """,
                (safe_error_code, _to_text(now), interaction_id),
            )
            connection.commit()
        return self.get_interaction(interaction_id)

    def pending_interactions(self, *, limit: int = 100) -> tuple[InteractionOutboxRow, ...]:
        if limit <= 0 or limit > 1_000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT interaction_id, channel_id, user_id, clarification_id, action,
                       event_timestamp, state, retry_count, safe_error_code,
                       created_at, updated_at
                  FROM interaction_events
                 WHERE state = 'pending'
                 ORDER BY created_at ASC
                 LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(_interaction_row_from_sqlite(row) for row in rows)

    def get_interaction(self, interaction_id: str) -> InteractionOutboxRow:
        _require_id(interaction_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT interaction_id, channel_id, user_id, clarification_id, action,
                       event_timestamp, state, retry_count, safe_error_code,
                       created_at, updated_at
                  FROM interaction_events
                 WHERE interaction_id = ?
                """,
                (interaction_id,),
            ).fetchone()
        if row is None:
            raise KeyError(interaction_id)
        return _interaction_row_from_sqlite(row)

    def pending_rows(self, *, limit: int = 100) -> tuple[WakeOutboxRow, ...]:
        if limit <= 0 or limit > 1_000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT message_id, channel_id, author_id, event_timestamp, request_kind,
                       acknowledgement_message_id, state, retry_count, safe_error_code,
                       created_at, updated_at
                  FROM wake_events
                 WHERE state IN ('pending', 'acknowledged')
                 ORDER BY created_at ASC
                 LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(_row_from_sqlite(row) for row in rows)

    def prune_terminal(self, *, older_than: timedelta) -> int:
        if older_than.total_seconds() <= 0:
            raise ValueError("retention must be positive")
        threshold = _utc_now() - older_than
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM wake_events
                 WHERE state IN ('accepted', 'failed') AND updated_at < ?
                """,
                (_to_text(threshold),),
            )
            interaction_cursor = connection.execute(
                """
                DELETE FROM interaction_events
                 WHERE state IN ('accepted', 'failed') AND updated_at < ?
                """,
                (_to_text(threshold),),
            )
            connection.commit()
            return cursor.rowcount + interaction_cursor.rowcount

    def get(self, message_id: str) -> WakeOutboxRow:
        _require_id(message_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT message_id, channel_id, author_id, event_timestamp, request_kind,
                       acknowledgement_message_id, state, retry_count, safe_error_code,
                       created_at, updated_at
                  FROM wake_events
                 WHERE message_id = ?
                """,
                (message_id,),
            ).fetchone()
        if row is None:
            raise KeyError(message_id)
        return _row_from_sqlite(row)

    def _ensure_store(self) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._path.parent, 0o700)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS wake_events (
                    message_id TEXT PRIMARY KEY,
                    channel_id TEXT NOT NULL,
                    author_id TEXT NOT NULL,
                    event_timestamp TEXT NOT NULL,
                    request_kind TEXT NOT NULL DEFAULT 'mention' CHECK (
                        request_kind IN ('mention', 'command', 'continuation')
                    ),
                    acknowledgement_message_id TEXT,
                    state TEXT NOT NULL CHECK (
                        state IN ('pending', 'acknowledged', 'accepted', 'failed')
                    ),
                    retry_count INTEGER NOT NULL CHECK (retry_count >= 0),
                    safe_error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(wake_events)")}
            if "request_kind" not in columns:
                connection.execute(
                    "ALTER TABLE wake_events ADD COLUMN request_kind TEXT NOT NULL "
                    "DEFAULT 'mention'"
                )
            schema_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'wake_events'"
            ).fetchone()
            schema = str(schema_row[0]) if schema_row is not None else ""
            if "'continuation'" not in schema:
                connection.execute("ALTER TABLE wake_events RENAME TO wake_events_legacy_kind")
                connection.execute(
                    """
                    CREATE TABLE wake_events (
                        message_id TEXT PRIMARY KEY,
                        channel_id TEXT NOT NULL,
                        author_id TEXT NOT NULL,
                        event_timestamp TEXT NOT NULL,
                        request_kind TEXT NOT NULL DEFAULT 'mention' CHECK (
                            request_kind IN ('mention', 'command', 'continuation')
                        ),
                        acknowledgement_message_id TEXT,
                        state TEXT NOT NULL CHECK (
                            state IN ('pending', 'acknowledged', 'accepted', 'failed')
                        ),
                        retry_count INTEGER NOT NULL CHECK (retry_count >= 0),
                        safe_error_code TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO wake_events (
                        message_id, channel_id, author_id, event_timestamp, request_kind,
                        acknowledgement_message_id, state, retry_count, safe_error_code,
                        created_at, updated_at
                    )
                    SELECT message_id, channel_id, author_id, event_timestamp, request_kind,
                           acknowledgement_message_id, state, retry_count, safe_error_code,
                           created_at, updated_at
                      FROM wake_events_legacy_kind
                    """
                )
                connection.execute("DROP TABLE wake_events_legacy_kind")
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_wake_events_state_created
                    ON wake_events(state, created_at)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS interaction_events (
                    interaction_id TEXT PRIMARY KEY,
                    channel_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    clarification_id TEXT NOT NULL,
                    action TEXT NOT NULL CHECK (
                        action IN ('quiz','assignment','tutorial','lab','studying_block','ignore')
                    ),
                    event_timestamp TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('pending', 'accepted', 'failed')
                    ),
                    retry_count INTEGER NOT NULL CHECK (retry_count >= 0),
                    safe_error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_interaction_events_state_created
                    ON interaction_events(state, created_at)
                """
            )
            connection.commit()
        os.chmod(self._path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)


def _row_from_sqlite(row: sqlite3.Row | tuple[object, ...]) -> WakeOutboxRow:
    request_kind = str(row[4])
    if request_kind not in {"mention", "command", "continuation"}:
        raise ValueError("invalid outbox request kind")
    state = str(row[6])
    if state not in {"pending", "acknowledged", "accepted", "failed"}:
        raise ValueError("invalid outbox state")
    return WakeOutboxRow(
        message_id=str(row[0]),
        channel_id=str(row[1]),
        author_id=str(row[2]),
        event_timestamp=_from_text(str(row[3])),
        request_kind=cast(WakeRequestKind, request_kind),
        acknowledgement_message_id=str(row[5]) if row[5] is not None else None,
        state=cast(OutboxState, state),
        retry_count=int(str(row[7])),
        safe_error_code=str(row[8]) if row[8] is not None else None,
        created_at=_from_text(str(row[9])),
        updated_at=_from_text(str(row[10])),
    )


def _interaction_row_from_sqlite(
    row: sqlite3.Row | tuple[object, ...],
) -> InteractionOutboxRow:
    state = str(row[6])
    if state not in {"pending", "accepted", "failed"}:
        raise ValueError("invalid interaction outbox state")
    return InteractionOutboxRow(
        interaction_id=str(row[0]),
        channel_id=str(row[1]),
        user_id=str(row[2]),
        clarification_id=UUID(str(row[3])),
        action=str(row[4]),
        event_timestamp=_from_text(str(row[5])),
        state=cast(OutboxState, state),
        retry_count=int(str(row[7])),
        safe_error_code=str(row[8]) if row[8] is not None else None,
        created_at=_from_text(str(row[9])),
        updated_at=_from_text(str(row[10])),
    )


def _require_id(value: str) -> None:
    if _DISCORD_ID.fullmatch(value) is None:
        raise ValueError("Discord ID is invalid")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _to_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("outbox timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _from_text(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


__all__ = [
    "InteractionOutboxRow",
    "OutboxState",
    "WakeOutbox",
    "WakeOutboxRow",
    "WakeRequestKind",
]
