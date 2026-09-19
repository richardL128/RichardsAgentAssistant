from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from inspect import getsource
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, select, text
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
    abort_state = importlib.import_module(
        "app.db.migrations.versions.0027_discord_wake_abort_state"
    )

    assert '"enqueued_at"' not in getsource(original.upgrade)
    assert forward.revision == "0019_discord_enqueued_at"
    assert forward.down_revision == "0018_discord_continuation"
    assert '"enqueued_at"' in getsource(forward.upgrade)
    assert abort_state.revision == "0027_discord_wake_abort_state"
    assert abort_state.down_revision == "0026_semantic_calendar_events"


def test_0027_migration_adds_abort_fields_and_downgrades_abort_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = importlib.import_module("app.db.migrations.versions.0027_discord_wake_abort_state")
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'discord-wake-0027.db'}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE discord_wake_inbound ("
                    "id CHAR(32) PRIMARY KEY, "
                    "discord_event_id VARCHAR(255) NOT NULL, "
                    "handoff_nonce VARCHAR(64) NOT NULL, "
                    "event_kind VARCHAR(32) NOT NULL, "
                    "action VARCHAR(64) NOT NULL, "
                    "discord_channel_id VARCHAR(32), "
                    "discord_user_id VARCHAR(32), "
                    "content_artifact_key VARCHAR(512) NOT NULL, "
                    "state VARCHAR(32) NOT NULL, "
                    "retry_count INTEGER NOT NULL DEFAULT 0, "
                    "last_error_code VARCHAR(128), "
                    "received_at DATETIME NOT NULL, "
                    "failed_at DATETIME, "
                    "created_at DATETIME NOT NULL, "
                    "updated_at DATETIME NOT NULL, "
                    "CONSTRAINT state_valid CHECK "
                    "(state IN ('queued','running','completed','failed')))"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO discord_wake_inbound "
                    "(id, discord_event_id, handoff_nonce, event_kind, action, "
                    "discord_channel_id, discord_user_id, content_artifact_key, state, "
                    "received_at, created_at, updated_at) "
                    "VALUES "
                    "('wake-1', 'event-1', 'nonce-1', 'message', 'academic_checkin', "
                    "'channel-1', 'user-1', 'artifact-1', 'queued', "
                    "'2026-09-09 12:00:00', '2026-09-09 12:00:00', '2026-09-09 12:00:00')"
                )
            )
            context = MigrationContext.configure(connection)
            monkeypatch.setattr(migration, "op", Operations(context))

            migration.upgrade()

            assert "discord_abort_requests" in inspect(connection).get_table_names()

            columns = {
                row[1]
                for row in connection.exec_driver_sql("PRAGMA table_info(discord_wake_inbound)")
            }
            assert {
                "queue_job_id",
                "abort_requested_at",
                "abort_requested_by_event_id",
                "abort_requested_prior_state",
                "abort_terminal_at",
                "abort_reason_code",
                "activity_phase",
                "activity_model_turn",
                "activity_tool_name",
                "activity_tool_status",
                "activity_side_effect_class",
                "activity_updated_at",
            }.issubset(columns)
            connection.execute(
                text(
                    "UPDATE discord_wake_inbound "
                    "SET state = 'aborted', "
                    "    abort_requested_at = '2026-09-09 12:01:00', "
                    "    abort_requested_prior_state = 'queued', "
                    "    abort_terminal_at = '2026-09-09 12:01:01', "
                    "    abort_reason_code = 'user_abort'"
                )
            )
            assert (
                connection.execute(text("SELECT state FROM discord_wake_inbound")).scalar_one()
                == "aborted"
            )

            migration.downgrade()

            downgraded_columns = {
                row[1]
                for row in connection.exec_driver_sql("PRAGMA table_info(discord_wake_inbound)")
            }
            assert "queue_job_id" not in downgraded_columns
            assert "activity_tool_name" not in downgraded_columns
            downgraded = connection.execute(
                text("SELECT state, last_error_code, failed_at FROM discord_wake_inbound")
            ).one()
            assert downgraded.state == "failed"
            assert downgraded.last_error_code == "user_abort"
            assert downgraded.failed_at is not None
            assert "discord_abort_requests" not in inspect(connection).get_table_names()
    finally:
        engine.dispose()


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


def _event_at(
    event_id: str,
    message_id: str,
    received_at: datetime,
    *,
    channel_id: str = "123456789012345678",
    user_id: str = "234567890123456789",
) -> DiscordWakeInboundInput:
    return DiscordWakeInboundInput(
        discord_event_id=event_id,
        handoff_nonce=f"nonce-{event_id}",
        event_kind="message",
        action="academic_checkin",
        content_artifact_key=f"artifact:{event_id}",
        received_at=received_at,
        discord_channel_id=channel_id,
        discord_user_id=user_id,
        discord_message_id=message_id,
        ack_message_id=f"ack-{message_id}",
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


def test_bind_queue_job_id_reports_abort_enqueue_race(engine) -> None:
    with Session(engine) as session, session.begin():
        accepted = DiscordWakeRepository.accept_verified_event(session, _event())
        decision = DiscordWakeRepository.enqueue_decision(session, accepted.wake_id)
        assert decision.should_enqueue
        assert not decision.abort_requested

        DiscordWakeRepository.request_abort_for_scope(
            session,
            channel_id="123456789012345678",
            user_id="234567890123456789",
            abort_event_id="abort-event-1",
            abort_received_at=datetime(2026, 9, 9, 12, 1, tzinfo=UTC),
        )
        binding = DiscordWakeRepository.bind_queue_job_id(session, accepted.wake_id, 1234)

        assert binding.queue_job_id == 1234
        assert binding.state == "abort_requested"
        assert binding.should_cancel_queue_job
        row = session.get(DiscordWakeInbound, accepted.wake_id)
        assert row is not None
        assert row.enqueued_at is not None
        assert row.queue_job_id == 1234


def test_durable_abort_request_covers_late_earlier_handoff_and_replays_receipt(engine) -> None:
    with Session(engine) as session, session.begin():
        created = DiscordWakeRepository.record_abort_request(
            session,
            abort_event_id="999999999999999999",
            handoff_nonce="abort-request-nonce",
            channel_id="123456789012345678",
            user_id="234567890123456789",
            ack_message_id="456789012345678901",
            received_at=NOW,
        )
        assert created.created
        finalized = DiscordWakeRepository.finalize_abort_request(
            session,
            abort_event_id=created.abort_event_id,
            status="no_active",
            target_count=0,
            running_count=0,
            queued_count=0,
            safe_activity_label=None,
            safe_tool_status="none",
        )
        assert finalized.status == "no_active"

    with Session(engine) as session, session.begin():
        replay = DiscordWakeRepository.record_abort_request(
            session,
            abort_event_id="999999999999999999",
            handoff_nonce="abort-request-nonce",
            channel_id="123456789012345678",
            user_id="234567890123456789",
            ack_message_id="456789012345678901",
            received_at=NOW,
        )
        late_earlier = DiscordWakeRepository.accept_verified_event(
            session,
            _event_at(
                "late-earlier-event",
                "345678901234567891",
                NOW - timedelta(seconds=1),
            ),
        )
        later = DiscordWakeRepository.accept_verified_event(
            session,
            _event_at(
                "later-event",
                "345678901234567892",
                NOW + timedelta(seconds=1),
            ),
        )

        assert not replay.created
        assert replay.status == "no_active"
        assert late_earlier.state == "aborted"
        assert not DiscordWakeRepository.enqueue_decision(
            session, late_earlier.wake_id
        ).should_enqueue
        assert later.state == "queued"
        assert DiscordWakeRepository.enqueue_decision(session, later.wake_id).should_enqueue


def test_request_abort_for_scope_is_owner_channel_scoped_and_idempotent(engine) -> None:
    with Session(engine) as session, session.begin():
        first = DiscordWakeRepository.accept_verified_event(
            session,
            _event_at(
                "discord-event-1",
                "message-1",
                datetime(2026, 9, 9, 12, 0, tzinfo=UTC),
            ),
        )
        second = DiscordWakeRepository.accept_verified_event(
            session,
            _event_at(
                "discord-event-2",
                "message-2",
                datetime(2026, 9, 9, 12, 2, tzinfo=UTC),
            ),
        )
        unrelated = DiscordWakeRepository.accept_verified_event(
            session,
            _event_at(
                "discord-event-3",
                "message-3",
                datetime(2026, 9, 9, 12, 0, tzinfo=UTC),
                channel_id="999999999999999999",
            ),
        )
        completed = DiscordWakeRepository.accept_verified_event(
            session,
            _event_at(
                "discord-event-4",
                "message-4",
                datetime(2026, 9, 9, 12, 0, tzinfo=UTC),
            ),
        )
        DiscordWakeRepository.bind_queue_job_id(session, first.wake_id, 1001)
        DiscordWakeRepository.bind_queue_job_id(session, second.wake_id, 1002)
        DiscordWakeRepository.bind_queue_job_id(session, unrelated.wake_id, 1003)
        DiscordWakeRepository.mark_completed(session, completed.wake_id)

        result = DiscordWakeRepository.request_abort_for_scope(
            session,
            channel_id="123456789012345678",
            user_id="234567890123456789",
            abort_event_id="abort-event-1",
            abort_received_at=datetime(2026, 9, 9, 12, 1, tzinfo=UTC),
        )
        replay = DiscordWakeRepository.request_abort_for_scope(
            session,
            channel_id="123456789012345678",
            user_id="234567890123456789",
            abort_event_id="abort-event-1",
            abort_received_at=datetime(2026, 9, 9, 12, 1, tzinfo=UTC),
        )

        assert result.selected_count == 1
        assert result.newly_requested_count == 1
        assert result.queued_count == 1
        assert result.running_count == 0
        assert replay.selected_count == 1
        assert replay.newly_requested_count == 0
        assert replay.queued_count == 1
        assert result.targets[0].wake_id == first.wake_id
        assert result.targets[0].prior_state == "queued"
        assert result.targets[0].state == "abort_requested"
        assert result.targets[0].queue_job_id == 1001
        DiscordWakeRepository.mark_queue_cancelled_aborted(
            session,
            first.wake_id,
            abort_event_id="abort-event-1",
        )
        terminal_replay = DiscordWakeRepository.request_abort_for_scope(
            session,
            channel_id="123456789012345678",
            user_id="234567890123456789",
            abort_event_id="abort-event-1",
            abort_received_at=datetime(2026, 9, 9, 12, 1, tzinfo=UTC),
        )
        assert terminal_replay.targets[0].prior_state == "queued"
        assert terminal_replay.targets[0].state == "aborted"
        assert terminal_replay.queued_count == 1
        assert session.get(DiscordWakeInbound, first.wake_id).state == "aborted"
        assert session.get(DiscordWakeInbound, second.wake_id).state == "queued"
        assert session.get(DiscordWakeInbound, unrelated.wake_id).state == "queued"
        assert session.get(DiscordWakeInbound, completed.wake_id).state == "completed"


def test_abort_status_snapshot_uses_safe_activity_only(engine) -> None:
    with Session(engine) as session, session.begin():
        accepted = DiscordWakeRepository.accept_verified_event(session, _event())
        DiscordWakeRepository.mark_running(session, accepted.wake_id)
        DiscordWakeRepository.record_activity(
            session,
            accepted.wake_id,
            phase="tool_started",
            model_turn=2,
            tool_name="search_courses",
            tool_status="running",
            side_effect_class="read_only",
        )
        result = DiscordWakeRepository.request_abort_for_scope(
            session,
            channel_id="123456789012345678",
            user_id="234567890123456789",
            abort_event_id="abort-event-1",
            abort_received_at=datetime(2026, 9, 9, 12, 1, tzinfo=UTC),
        )
        DiscordWakeRepository.mark_aborted(
            session,
            accepted.wake_id,
            abort_event_id="abort-event-1",
        )

        snapshot = DiscordWakeRepository.abort_status_snapshot(
            session,
            [target.wake_id for target in result.targets],
        )

        assert snapshot.total_count == 1
        assert snapshot.aborted_count == 1
        assert snapshot.activity is not None
        assert snapshot.activity.activity_phase == "terminal"
        assert snapshot.activity.activity_tool_name == "search_courses"
        assert snapshot.activity.activity_tool_status == "running"
        assert snapshot.activity.activity_side_effect_class == "read_only"


def test_activity_store_rejects_model_invented_tool_names(engine) -> None:
    with Session(engine) as session, session.begin():
        accepted = DiscordWakeRepository.accept_verified_event(session, _event())
        with pytest.raises(ValueError, match="tool name"):
            DiscordWakeRepository.record_activity(
                session,
                accepted.wake_id,
                phase="tool_started",
                tool_name="model_invented_private_tool",
                tool_status="running",
                side_effect_class="unknown",
            )


def test_completed_after_abort_request_is_not_overwritten(engine) -> None:
    with Session(engine) as session, session.begin():
        accepted = DiscordWakeRepository.accept_verified_event(session, _event())
        DiscordWakeRepository.request_abort_for_scope(
            session,
            channel_id="123456789012345678",
            user_id="234567890123456789",
            abort_event_id="abort-event-1",
            abort_received_at=datetime(2026, 9, 9, 12, 1, tzinfo=UTC),
        )
        DiscordWakeRepository.mark_completed(session, accepted.wake_id)
        DiscordWakeRepository.mark_aborted(session, accepted.wake_id)

        row = session.get(DiscordWakeInbound, accepted.wake_id)
        assert row is not None
        assert row.state == "completed"
        assert row.abort_requested_by_event_id == "abort-event-1"
        assert row.completed_at is not None


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
        assert row.interaction_action == "assignment"

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
