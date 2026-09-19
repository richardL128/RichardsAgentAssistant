from __future__ import annotations

import importlib
import inspect as pyinspect
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents.conversation import NativeConversationService
from app.agents.conversation.contracts import NativeTranscriptManifest
from app.artifacts.store import ArtifactStore
from app.db.conversation import NativeConversationRepository
from app.db.models import (
    Base,
    NativeConversationInboundEvent,
    NativeConversationSession,
)

NOW = datetime(2026, 9, 9, 14, tzinfo=UTC)
CHANNEL = "222222222222222222"
OWNER = "333333333333333333"


@pytest.fixture
def engine(tmp_path: Path):
    created = create_engine(f"sqlite+pysqlite:///{tmp_path / 'native-conversations.db'}")
    Base.metadata.create_all(created)
    try:
        yield created
    finally:
        created.dispose()


@pytest.fixture
def artifacts(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifacts", default_retention_days=1, clock=lambda: NOW)


def _service(engine, artifacts: ArtifactStore) -> NativeConversationService:
    return NativeConversationService(
        engine=engine,
        artifact_store=artifacts,
        session_ttl_hours=24,
        clock=lambda: NOW,
    )


def test_begin_append_checkpoint_finish_and_resume_round_trip_native_messages(
    engine,
    artifacts: ArtifactStore,
) -> None:
    service = _service(engine, artifacts)

    started = service.begin_turn(
        external_event_id="event-1",
        discord_channel_id=CHANNEL,
        owner_discord_user_id=OWNER,
        content="schedule my private review; token=not-a-real-secret",
        model_identity="qwen@test",
        prompt_config_version="policy-v1",
        now=NOW,
    )

    assert started.status == "started"
    assert started.session_id is not None
    assert [message.type for message in started.transcript_messages] == ["human"]
    assert started.transcript_messages[0].content == (
        "schedule my private review; token=not-a-real-secret"
    )

    service.append_message(
        session_id=started.session_id,
        message=AIMessage(
            content="I need one detail.",
            additional_kwargs={"reasoning_content": "private model scratchpad"},
            tool_calls=[
                {
                    "id": "call-1",
                    "name": "search_courses",
                    "args": {"query": "ECE"},
                }
            ],
        ),
        now=NOW + timedelta(seconds=1),
    )
    service.append_message(
        session_id=started.session_id,
        message=ToolMessage(
            content='{"status":"succeeded","content":{"course":"ECE 250"}}',
            tool_call_id="call-1",
            name="search_courses",
        ),
        now=NOW + timedelta(seconds=2),
    )
    checkpoint = service.save_checkpoint(
        session_id=started.session_id,
        checkpoint={"academic": {"course_ids": ["course-1"]}},
        now=NOW + timedelta(seconds=3),
    )
    assert checkpoint == {"academic": {"course_ids": ["course-1"]}}

    awaiting = service.finish_turn(
        session_id=started.session_id,
        disposition="awaiting_user",
        content="Which course should I use?",
        metadata={"turn": 1},
        now=NOW + timedelta(seconds=4),
    )
    assert awaiting.status == "resumed"
    assert awaiting.state == "awaiting_user"

    resumed = service.begin_turn(
        external_event_id="event-2",
        discord_channel_id=CHANNEL,
        owner_discord_user_id=OWNER,
        content="ECE 250",
        model_identity="qwen@test",
        prompt_config_version="policy-v1",
        now=NOW + timedelta(minutes=1),
    )

    assert resumed.status == "resumed"
    assert resumed.session_id == started.session_id
    assert resumed.checkpoint == {"academic": {"course_ids": ["course-1"]}}
    assert [message.type for message in resumed.transcript_messages] == [
        "human",
        "ai",
        "tool",
        "human",
    ]
    assert resumed.transcript_messages[1].additional_kwargs == {
        "reasoning_content": "private model scratchpad"
    }
    assert resumed.transcript_messages[1].tool_calls == [
        {
            "name": "search_courses",
            "args": {"query": "ECE"},
            "id": "call-1",
            "type": "tool_call",
        }
    ]
    assert resumed.transcript_messages[-1].content == "ECE 250"

    duplicate_while_processing = service.begin_turn(
        external_event_id="event-2",
        discord_channel_id=CHANNEL,
        owner_discord_user_id=OWNER,
        content="ECE 250",
        model_identity="qwen@test",
        prompt_config_version="policy-v1",
        now=NOW + timedelta(minutes=1),
    )
    assert duplicate_while_processing.status == "resumed"
    assert duplicate_while_processing.duplicate is True
    assert duplicate_while_processing.response is None
    assert duplicate_while_processing.checkpoint == {"academic": {"course_ids": ["course-1"]}}
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(NativeConversationInboundEvent)) == 2
        row = session.get(NativeConversationSession, started.session_id)
        assert row is not None
        assert row.state == "processing"
        assert row.last_disposition == "awaiting_user"
        assert "schedule my private review" not in json.dumps(
            {
                "root_event_id": row.root_event_id,
                "transcript_artifact_key": row.transcript_artifact_key,
                "tool_checkpoint_artifact_key": row.tool_checkpoint_artifact_key,
            },
            sort_keys=True,
        )
        manifest = NativeTranscriptManifest.model_validate(
            json.loads(artifacts.get(row.transcript_artifact_key).decode("utf-8"))
        )
        assert manifest.to_messages()[0].content == (
            "schedule my private review; token=not-a-real-secret"
        )
        assert [block.kind for block in manifest.blocks] == [
            "user",
            "assistant_reasoning",
            "assistant_visible",
            "assistant_tool_call",
            "tool_result",
            "host_lifecycle",
            "user",
        ]
        assert manifest.blocks[1].kind == "assistant_reasoning"
        assert manifest.blocks[1].content == "private model scratchpad"
        assert manifest.blocks[3].kind == "assistant_tool_call"
        assert manifest.blocks[3].call_id == "call-1"
        assert manifest.blocks[3].tool_name == "search_courses"
        assert manifest.blocks[3].arguments == {"query": "ECE"}

    completed = service.finish_turn(
        session_id=started.session_id,
        disposition="completed",
        content="Done.",
        metadata={"turn": 2},
        now=NOW + timedelta(minutes=2),
    )
    assert completed.status == "finished"
    assert completed.state == "completed"

    duplicate_completed = service.begin_turn(
        external_event_id="event-2",
        discord_channel_id=CHANNEL,
        owner_discord_user_id=OWNER,
        content="ECE 250",
        model_identity="qwen@test",
        prompt_config_version="policy-v1",
        now=NOW + timedelta(minutes=3),
    )
    assert duplicate_completed.status == "duplicate"
    assert duplicate_completed.duplicate is True
    assert duplicate_completed.response == "Done."


def test_partial_unique_index_allows_one_open_conversation_per_owner_channel(
    engine,
    artifacts: ArtifactStore,
) -> None:
    first_key = artifacts.put(
        "{}",
        media_type="application/json",
        data_class="native_conversation_transcript",
    ).key
    second_key = artifacts.put(
        '{"messages":[]}',
        media_type="application/json",
        data_class="native_conversation_transcript",
    ).key

    with Session(engine) as session, session.begin():
        NativeConversationRepository.create_session(
            session,
            root_event_id="event-1",
            discord_channel_id=CHANNEL,
            owner_discord_user_id=OWNER,
            transcript_artifact_key=first_key,
            started_at=NOW,
            expires_at=NOW + timedelta(hours=1),
            model_identity="qwen@test",
            prompt_config_version="policy-v1",
        )

    with pytest.raises(IntegrityError), Session(engine) as session, session.begin():
        NativeConversationRepository.create_session(
            session,
            root_event_id="event-2",
            discord_channel_id=CHANNEL,
            owner_discord_user_id=OWNER,
            transcript_artifact_key=second_key,
            started_at=NOW,
            expires_at=NOW + timedelta(hours=1),
            model_identity="qwen@test",
            prompt_config_version="policy-v1",
        )

    with Session(engine) as session, session.begin():
        row = session.scalar(select(NativeConversationSession))
        assert row is not None
        NativeConversationRepository.update_artifacts(
            session,
            conversation_id=row.id,
            transcript_artifact_key=row.transcript_artifact_key,
            now=NOW,
            state="completed",
            last_disposition="completed",
        )
    with Session(engine) as session, session.begin():
        NativeConversationRepository.create_session(
            session,
            root_event_id="event-3",
            discord_channel_id=CHANNEL,
            owner_discord_user_id=OWNER,
            transcript_artifact_key=second_key,
            started_at=NOW,
            expires_at=NOW + timedelta(hours=1),
            model_identity="qwen@test",
            prompt_config_version="policy-v1",
        )


def test_begin_turn_coalesces_partial_unique_creation_race(
    engine,
    artifacts: ArtifactStore,
    monkeypatch,
) -> None:
    service = _service(engine, artifacts)
    first = service.begin_turn(
        external_event_id="event-1",
        discord_channel_id=CHANNEL,
        owner_discord_user_id=OWNER,
        content="first",
        model_identity="qwen@test",
        prompt_config_version="policy-v1",
        now=NOW,
    )
    assert first.session_id is not None

    real_lookup = NativeConversationRepository.lock_open_for_owner
    lookup_count = 0

    def simulate_stale_initial_lookup(session, **kwargs):
        nonlocal lookup_count
        lookup_count += 1
        if lookup_count == 1:
            return None
        return real_lookup(session, **kwargs)

    monkeypatch.setattr(
        NativeConversationRepository,
        "lock_open_for_owner",
        staticmethod(simulate_stale_initial_lookup),
    )

    raced = service.begin_turn(
        external_event_id="event-2",
        discord_channel_id=CHANNEL,
        owner_discord_user_id=OWNER,
        content="second",
        model_identity="qwen@test",
        prompt_config_version="policy-v1",
        now=NOW,
    )

    assert raced.status == "in_progress"
    assert raced.session_id == first.session_id
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(NativeConversationSession)) == 1
        assert session.scalar(select(func.count()).select_from(NativeConversationInboundEvent)) == 1


def test_corrupt_transcript_artifact_fails_closed(
    engine,
    artifacts: ArtifactStore,
) -> None:
    service = _service(engine, artifacts)
    started = service.begin_turn(
        external_event_id="event-1",
        discord_channel_id=CHANNEL,
        owner_discord_user_id=OWNER,
        content="start",
        model_identity="qwen@test",
        prompt_config_version="policy-v1",
        now=NOW,
    )
    assert started.session_id is not None
    wrong = artifacts.put("{}", media_type="application/json", data_class="wrong_class")
    with Session(engine) as session, session.begin():
        row = session.get(NativeConversationSession, started.session_id)
        assert row is not None
        row.transcript_artifact_key = wrong.key
        row.state = "awaiting_user"

    result = service.begin_turn(
        external_event_id="event-2",
        discord_channel_id=CHANNEL,
        owner_discord_user_id=OWNER,
        content="resume",
        model_identity="qwen@test",
        prompt_config_version="policy-v1",
        now=NOW + timedelta(minutes=1),
    )

    assert result.status == "corrupt"
    with Session(engine) as session:
        row = session.get(NativeConversationSession, started.session_id)
        assert row is not None
        assert row.state == "failed"
        assert row.error_code == "conversation_artifact_corrupt"


def test_expire_open_conversations_marks_terminal(engine, artifacts: ArtifactStore) -> None:
    service = NativeConversationService(
        engine=engine,
        artifact_store=artifacts,
        session_ttl_hours=1,
        clock=lambda: NOW,
    )
    started = service.begin_turn(
        external_event_id="event-1",
        discord_channel_id=CHANNEL,
        owner_discord_user_id=OWNER,
        content="start",
        model_identity="qwen@test",
        prompt_config_version="policy-v1",
        now=NOW,
    )

    assert service.expire(now=NOW + timedelta(hours=2)) == 1
    with Session(engine) as session:
        row = session.get(NativeConversationSession, started.session_id)
        assert row is not None
        assert row.state == "expired"
        assert row.last_disposition == "expired"


def test_begin_turn_does_not_create_new_session_from_answer_when_open_session_expires(
    engine,
    artifacts: ArtifactStore,
) -> None:
    service = NativeConversationService(
        engine=engine,
        artifact_store=artifacts,
        session_ttl_hours=1,
        clock=lambda: NOW,
    )
    started = service.begin_turn(
        external_event_id="event-1",
        discord_channel_id=CHANNEL,
        owner_discord_user_id=OWNER,
        content="schedule my private review",
        model_identity="qwen@test",
        prompt_config_version="policy-v1",
        now=NOW,
    )
    assert started.session_id is not None
    service.finish_turn(
        session_id=started.session_id,
        disposition="awaiting_user",
        content="Which course should I use?",
        now=NOW + timedelta(minutes=1),
    )

    expired = service.begin_turn(
        external_event_id="event-2",
        discord_channel_id=CHANNEL,
        owner_discord_user_id=OWNER,
        content="ECE 250",
        model_identity="qwen@test",
        prompt_config_version="policy-v1",
        now=NOW + timedelta(hours=2),
    )

    assert expired.status == "expired"
    assert expired.session_id == started.session_id
    assert expired.response == "That conversation expired. Please resend the complete request."
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(NativeConversationSession)) == 1
        assert session.scalar(select(func.count()).select_from(NativeConversationInboundEvent)) == 2
        row = session.get(NativeConversationSession, started.session_id)
        assert row is not None
        assert row.state == "expired"
        assert row.last_disposition == "expired"

    duplicate_expired = service.begin_turn(
        external_event_id="event-2",
        discord_channel_id=CHANNEL,
        owner_discord_user_id=OWNER,
        content="ECE 250",
        model_identity="qwen@test",
        prompt_config_version="policy-v1",
        now=NOW + timedelta(hours=2, minutes=1),
    )
    assert duplicate_expired.status == "expired"
    assert duplicate_expired.duplicate is True
    assert duplicate_expired.response == expired.response
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(NativeConversationSession)) == 1
        assert session.scalar(select(func.count()).select_from(NativeConversationInboundEvent)) == 2


def test_pause_and_cancel_open_preserve_open_state_then_close(
    engine,
    artifacts: ArtifactStore,
) -> None:
    service = _service(engine, artifacts)
    started = service.begin_turn(
        external_event_id="event-1",
        discord_channel_id=CHANNEL,
        owner_discord_user_id=OWNER,
        content="start",
        model_identity="qwen@test",
        prompt_config_version="policy-v1",
        now=NOW,
    )
    assert started.session_id is not None

    paused = service.pause(
        session_id=started.session_id,
        error_code="context_budget_exceeded",
        content="This conversation is too large. Please narrow the next step.",
        now=NOW + timedelta(minutes=1),
    )
    assert paused.status == "resumed"
    assert paused.state == "awaiting_user"
    assert paused.response == "This conversation is too large. Please narrow the next step."
    inspected = service.inspect_open(
        discord_channel_id=CHANNEL,
        owner_discord_user_id=OWNER,
        now=NOW + timedelta(minutes=2),
    )
    assert inspected.status == "resumed"
    assert inspected.response == paused.response

    cancelled = service.cancel_open(
        discord_channel_id=CHANNEL,
        owner_discord_user_id=OWNER,
        external_event_id="event-cancel",
        now=NOW + timedelta(minutes=3),
    )
    assert cancelled.status == "cancelled"
    assert cancelled.state == "cancelled"
    with Session(engine) as session:
        row = session.get(NativeConversationSession, started.session_id)
        assert row is not None
        assert row.state == "cancelled"
        assert row.error_code is None
        assert session.scalar(select(func.count()).select_from(NativeConversationInboundEvent)) == 2


def test_0028_migration_adds_native_conversation_tables(tmp_path: Path, monkeypatch) -> None:
    migration = importlib.import_module(
        "app.db.migrations.versions.0028_native_conversation_sessions"
    )

    assert migration.revision == "0028_native_conversations"
    assert migration.down_revision == "0027_discord_wake_abort_state"
    source = pyinspect.getsource(migration.upgrade)
    assert "uq_native_conversations_open_owner_channel" in source
    assert "postgresql_where" in source
    assert "sqlite_where" in source

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'migration-0028.db'}")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE academic_discourse_sessions ("
                "id CHAR(32) PRIMARY KEY, "
                "state VARCHAR(32) NOT NULL, "
                "session_kind VARCHAR(64) NOT NULL, "
                "updated_at DATETIME NOT NULL)"
            )
            connection.exec_driver_sql(
                "INSERT INTO academic_discourse_sessions "
                "(id, state, session_kind, updated_at) "
                "VALUES "
                "('legacy-open', 'open', 'agent_clarification', '2026-09-09 12:00:00'), "
                "('legacy-memory', 'open', 'memory_review', '2026-09-09 12:00:00')"
            )
            context = MigrationContext.configure(connection)
            monkeypatch.setattr(migration, "op", Operations(context))
            migration.upgrade()
            legacy_rows = connection.exec_driver_sql(
                "SELECT id, state FROM academic_discourse_sessions ORDER BY id"
            ).all()
            assert legacy_rows == [("legacy-memory", "open"), ("legacy-open", "expired")]
            tables = inspect(connection).get_table_names()
            assert "native_conversation_sessions" in tables
            assert "native_conversation_inbound_events" in tables
            indexes = {
                item["name"]
                for item in inspect(connection).get_indexes("native_conversation_sessions")
            }
            assert "uq_native_conversations_open_owner_channel" in indexes
            migration.downgrade()
            assert "native_conversation_sessions" not in inspect(connection).get_table_names()
    finally:
        engine.dispose()
