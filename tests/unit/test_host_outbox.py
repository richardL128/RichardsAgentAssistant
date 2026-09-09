import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from app.host.outbox import WakeOutbox


def test_outbox_uses_restrictive_permissions_and_id_only_schema(tmp_path: Path) -> None:
    outbox = WakeOutbox(tmp_path / ".artifacts" / "discord-wake" / "outbox.sqlite3")

    row = outbox.record_event(
        message_id="111111111111111111",
        channel_id="222222222222222222",
        author_id="333333333333333333",
        event_timestamp=datetime(2026, 9, 9, tzinfo=UTC),
    )

    assert row.state == "pending"
    assert oct(os.stat(outbox.path.parent).st_mode & 0o777) == "0o700"
    assert oct(os.stat(outbox.path).st_mode & 0o777) == "0o600"
    with sqlite3.connect(outbox.path) as connection:
        columns = [item[1] for item in connection.execute("PRAGMA table_info(wake_events)")]
    assert "content" not in columns
    assert "authorization" not in columns
    assert "token" not in columns


def test_outbox_dedupes_acknowledges_replays_and_prunes(tmp_path: Path) -> None:
    outbox = WakeOutbox(tmp_path / "wake.sqlite3")
    timestamp = datetime(2026, 9, 9, tzinfo=UTC)

    first = outbox.record_event(
        message_id="111111111111111111",
        channel_id="222222222222222222",
        author_id="333333333333333333",
        event_timestamp=timestamp,
    )
    second = outbox.record_event(
        message_id="111111111111111111",
        channel_id="222222222222222222",
        author_id="333333333333333333",
        event_timestamp=timestamp,
    )
    acknowledged = outbox.mark_acknowledged("111111111111111111", "444444444444444444")

    assert first.message_id == second.message_id
    assert acknowledged.state == "acknowledged"
    assert outbox.pending_rows()[0].acknowledgement_message_id == "444444444444444444"
    outbox.mark_accepted("111111111111111111")
    assert outbox.pending_rows() == ()
    assert outbox.prune_terminal(older_than=timedelta(microseconds=1)) == 1


def test_outbox_rejects_invalid_ids_and_error_codes(tmp_path: Path) -> None:
    outbox = WakeOutbox(tmp_path / "wake.sqlite3")

    with pytest.raises(ValueError, match="Discord ID"):
        outbox.record_event(
            message_id="not-an-id",
            channel_id="222222222222222222",
            author_id="333333333333333333",
            event_timestamp=datetime.now(UTC),
        )
    outbox.record_event(
        message_id="111111111111111111",
        channel_id="222222222222222222",
        author_id="333333333333333333",
        event_timestamp=datetime.now(UTC),
    )
    with pytest.raises(ValueError, match="safe error"):
        outbox.mark_failed("111111111111111111", "unsafe error text")


def test_outbox_rejects_changed_replay_metadata(tmp_path: Path) -> None:
    outbox = WakeOutbox(tmp_path / "wake.sqlite3")
    timestamp = datetime.now(UTC)
    outbox.record_event(
        message_id="111111111111111111",
        channel_id="222222222222222222",
        author_id="333333333333333333",
        event_timestamp=timestamp,
    )

    with pytest.raises(ValueError, match="replay metadata"):
        outbox.record_event(
            message_id="111111111111111111",
            channel_id="222222222222222222",
            author_id="444444444444444444",
            event_timestamp=timestamp,
        )


def test_outbox_migrates_existing_request_kind_constraint_for_continuations(
    tmp_path: Path,
) -> None:
    path = tmp_path / "wake.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE wake_events (
                message_id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                author_id TEXT NOT NULL,
                event_timestamp TEXT NOT NULL,
                request_kind TEXT NOT NULL DEFAULT 'mention' CHECK (
                    request_kind IN ('mention', 'command')
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
            INSERT INTO wake_events VALUES (
                '111111111111111111', '222222222222222222', '333333333333333333',
                '2026-09-09T00:00:00+00:00', 'mention', NULL, 'accepted', 0, NULL,
                '2026-09-09T00:00:00+00:00', '2026-09-09T00:00:00+00:00'
            )
            """
        )
        connection.commit()

    outbox = WakeOutbox(path)
    continuation = outbox.record_event(
        message_id="444444444444444444",
        channel_id="222222222222222222",
        author_id="333333333333333333",
        event_timestamp=datetime(2026, 9, 9, 0, 1, tzinfo=UTC),
        request_kind="continuation",
    )

    assert outbox.get("111111111111111111").state == "accepted"
    assert continuation.request_kind == "continuation"


def test_interaction_outbox_is_token_free_and_replayable(tmp_path: Path) -> None:
    outbox = WakeOutbox(tmp_path / "wake.sqlite3")
    interaction = outbox.record_interaction(
        interaction_id="555555555555555555",
        channel_id="222222222222222222",
        user_id="333333333333333333",
        clarification_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        action="lab",
        event_timestamp=datetime.now(UTC),
    )

    assert interaction.state == "pending"
    assert outbox.pending_interactions() == (interaction,)
    with sqlite3.connect(outbox.path) as connection:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(interaction_events)")
        }
    assert "token" not in columns
    assert "content" not in columns
    outbox.mark_interaction_accepted(interaction.interaction_id)
    assert outbox.pending_interactions() == ()
