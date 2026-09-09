from __future__ import annotations

import importlib
from datetime import UTC, datetime
from inspect import getsource
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from app.db.discord_wake import (
    DiscordWakeAction,
    DiscordWakeInboundInput,
    DiscordWakeNonceReplayError,
    DiscordWakeRepository,
)
from app.db.models import Base, DiscordWakeInbound

NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)


def test_enqueued_timestamp_uses_forward_migration_after_deployed_0018() -> None:
    original = importlib.import_module("app.db.migrations.versions.0017_discord_wake_inbound")
    forward = importlib.import_module("app.db.migrations.versions.0019_discord_wake_enqueued_at")

    assert '"enqueued_at"' not in getsource(original.upgrade)
    assert forward.revision == "0019_discord_enqueued_at"
    assert forward.down_revision == "0018_discord_continuation"
    assert '"enqueued_at"' in getsource(forward.upgrade)


@pytest.fixture
def engine(tmp_path: Path):
    created = create_engine(f"sqlite+pysqlite:///{tmp_path / 'discord-wake.db'}")
    Base.metadata.create_all(created)
    try:
        yield created
    finally:
        created.dispose()


def _event(
    *,
    discord_event_id: str = "discord-event-1",
    handoff_nonce: str = "wake-nonce-1",
) -> DiscordWakeInboundInput:
    return DiscordWakeInboundInput(
        discord_event_id=discord_event_id,
        handoff_nonce=handoff_nonce,
        event_kind="message",
        action="academic_checkin",
        content_artifact_key="artifact:discord-wake:sha256",
        received_at=NOW,
        discord_channel_id="123456789012345678",
        discord_user_id="234567890123456789",
        discord_message_id="345678901234567890",
        ack_message_id="456789012345678901",
    )


def test_accept_verified_event_dedupes_discord_event_id(engine) -> None:
    with Session(engine) as session, session.begin():
        first = DiscordWakeRepository.accept_verified_event(session, _event())
        replay = DiscordWakeRepository.accept_verified_event(session, _event())

        rows = session.scalars(select(DiscordWakeInbound)).all()
        assert first.status == "created"
        assert replay.status == "replayed"
        assert replay.wake_id == first.wake_id
        assert not replay.enqueued
        assert len(rows) == 1
        assert rows[0].content_artifact_key == "artifact:discord-wake:sha256"
        assert rows[0].ack_message_id == "456789012345678901"

        DiscordWakeRepository.mark_enqueued(session, first.wake_id)
        enqueued_replay = DiscordWakeRepository.accept_verified_event(session, _event())
        assert enqueued_replay.enqueued


def test_accept_verified_event_rejects_nonce_replay_for_different_event(engine) -> None:
    with Session(engine) as session, session.begin():
        DiscordWakeRepository.accept_verified_event(session, _event())

        with pytest.raises(DiscordWakeNonceReplayError, match="nonce"):
            DiscordWakeRepository.accept_verified_event(
                session,
                _event(discord_event_id="discord-event-2", handoff_nonce="wake-nonce-1"),
            )


def test_accept_verified_event_persists_only_allowlisted_uuid_targets(engine) -> None:
    action_id = uuid4()
    clarification_id = uuid4()
    with Session(engine) as session, session.begin():
        accepted = DiscordWakeRepository.accept_verified_event(
            session,
            DiscordWakeInboundInput(
                discord_event_id="discord-interaction-1",
                handoff_nonce="wake-nonce-2",
                event_kind="interaction",
                action="agent_clarification",
                action_id=action_id,
                clarification_id=clarification_id,
                interaction_action="assignment",
                content_artifact_key="artifact:discord-wake:clarification",
                received_at=NOW,
                discord_interaction_id="567890123456789012",
            ),
        )
        row = session.get(DiscordWakeInbound, accepted.wake_id)
        assert row is not None
        assert row.action == "agent_clarification"
        assert row.action_id == action_id
        assert row.clarification_id == clarification_id

        with pytest.raises(ValueError, match="action"):
            DiscordWakeRepository.accept_verified_event(
                session,
                DiscordWakeInboundInput(
                    discord_event_id="discord-interaction-2",
                    handoff_nonce="wake-nonce-3",
                    event_kind="interaction",
                    action=cast(DiscordWakeAction, "raw_untrusted_action"),
                    content_artifact_key="artifact:discord-wake:invalid",
                    received_at=NOW,
                ),
            )


def test_discord_wake_store_has_no_raw_content_column_or_repr(engine) -> None:
    mapper_columns = {column.name for column in DiscordWakeInbound.__table__.columns}
    database_columns = {
        column["name"] for column in inspect(engine).get_columns("discord_wake_inbound")
    }
    forbidden = {"raw_content", "content", "message_content", "body", "text", "payload"}

    assert forbidden.isdisjoint(mapper_columns)
    assert forbidden.isdisjoint(database_columns)

    row = DiscordWakeInbound(
        discord_event_id="discord-event-1",
        handoff_nonce="wake-nonce-1",
        event_kind="message",
        action="academic_checkin",
        content_artifact_key="artifact:discord-wake:sha256",
        received_at=NOW,
    )
    representation = repr(row)
    assert "secret message body" not in representation
    assert "content_artifact_key" not in representation


def test_discord_wake_state_transitions_are_retryable_and_terminal(engine) -> None:
    with Session(engine) as session, session.begin():
        accepted = DiscordWakeRepository.accept_verified_event(session, _event())
        running = DiscordWakeRepository.mark_running(session, accepted.wake_id)
        assert running.state == "running"
        assert running.retry_count == 1
        assert running.started_at is not None

        failed = DiscordWakeRepository.mark_failed(
            session,
            accepted.wake_id,
            error_code="ollama_runtime_unavailable",
        )
        assert failed.state == "failed"
        assert failed.last_error_code == "ollama_runtime_unavailable"
        assert failed.failed_at is not None

        rerun = DiscordWakeRepository.mark_running(session, accepted.wake_id)
        assert rerun.state == "running"
        assert rerun.retry_count == 2
        assert rerun.last_error_code is None
        assert rerun.failed_at is None

        completed = DiscordWakeRepository.mark_completed(session, accepted.wake_id)
        assert completed.state == "completed"
        assert completed.completed_at is not None

        by_id = DiscordWakeRepository.get_by_id(session, accepted.wake_id)
        by_event = DiscordWakeRepository.get_by_event_id(session, "discord-event-1")
        assert by_id is not None
        assert by_event is not None
        assert by_id.id == by_event.id == accepted.wake_id

        with pytest.raises(ValueError, match="completed"):
            DiscordWakeRepository.mark_running(session, accepted.wake_id)
