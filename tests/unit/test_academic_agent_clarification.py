from __future__ import annotations

import importlib
import inspect
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents.academic_planner.agent_clarification import (
    AGENT_CONTEXT_DATA_CLASS,
    AcademicAgentClarificationService,
    AgentClarificationSessionState,
)
from app.artifacts import ArtifactStore
from app.db.academic import AGENT_CLARIFICATION_SESSION_KIND, AcademicRepository
from app.db.models import AcademicDiscourseSession, AcademicDiscourseTurn, Base

CHANNEL = "987654321012345678"
OWNER = "123456789012345678"
NOW = datetime(2026, 9, 9, 15, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path):
    created = create_engine(f"sqlite+pysqlite:///{tmp_path / 'agent-clarification.db'}")
    Base.metadata.create_all(created)
    try:
        yield created
    finally:
        created.dispose()


@pytest.fixture
def artifacts(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifacts", default_retention_days=1, clock=lambda: NOW)


def _service(engine, artifacts: ArtifactStore) -> AcademicAgentClarificationService:
    return AcademicAgentClarificationService(
        engine=engine,
        artifact_store=artifacts,
        session_ttl_hours=24,
        clock=lambda: NOW,
    )


def _session_state(engine) -> tuple[AcademicDiscourseSession, AgentClarificationSessionState]:
    with Session(engine) as session:
        row = session.scalar(select(AcademicDiscourseSession))
        assert row is not None
        state = AgentClarificationSessionState.model_validate(row.partial_state)
        session.expunge(row)
        return row, state


def test_inspect_does_not_consume_attempt_until_prepare_turn(engine, artifacts) -> None:
    service = _service(engine, artifacts)
    private_request = "create secret-token quiz for ECE 250 tomorrow"

    inspected = service.inspect_message(
        external_event_id="event-1",
        channel_id=CHANNEL,
        user_id=OWNER,
        raw_text=private_request,
        received_at=NOW,
    )

    assert inspected.status == "start_new"
    assert not service.has_pending_clarification(
        channel_id=CHANNEL,
        user_id=OWNER,
        received_at=NOW,
    )
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AcademicDiscourseSession)) == 0
        assert session.scalar(select(func.count()).select_from(AcademicDiscourseTurn)) == 0

    turn = service.prepare_turn(
        external_event_id="event-1",
        channel_id=CHANNEL,
        user_id=OWNER,
        raw_text=private_request,
        received_at=NOW,
    )

    assert turn.status == "started"
    assert turn.attempt_number == 1
    assert turn.context is not None
    assert turn.context.original_user_request.get_secret_value() == private_request
    assert private_request not in repr(turn)
    assert private_request not in repr(turn.context)

    row, state = _session_state(engine)
    assert row.session_kind == AGENT_CLARIFICATION_SESSION_KIND
    assert row.discord_channel_id == CHANNEL
    assert row.discord_user_id == OWNER
    assert state.root_event_id == "event-1"
    assert state.attempt_number == 1
    assert state.outcome == "awaiting_model"
    assert state.context_artifact_key is not None
    assert private_request not in json.dumps(row.partial_state, sort_keys=True)
    assert set(row.partial_state) == {
        "attempt_limit",
        "attempt_number",
        "context_artifact_key",
        "last_clarification_question",
        "outcome",
        "root_event_id",
        "schema_version",
    }

    metadata = artifacts.get_metadata(state.context_artifact_key)
    assert metadata.data_class == AGENT_CONTEXT_DATA_CLASS
    assert metadata.media_type == "application/json"
    assert metadata.expires_at == NOW + timedelta(days=1)
    payload = json.loads(artifacts.get(state.context_artifact_key))
    assert payload["original_user_request"] == private_request
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AcademicDiscourseTurn)) == 1


def test_resume_attempts_are_recorded_after_readiness_and_exhaust_on_third_question(
    engine,
    artifacts,
) -> None:
    service = _service(engine, artifacts)
    first = service.prepare_turn(
        external_event_id="event-1",
        channel_id=CHANNEL,
        user_id=OWNER,
        raw_text="schedule a quiz",
        received_at=NOW,
    )
    assert first.session_id is not None
    saved = service.persist_clarification_or_exhaust(
        session_id=first.session_id,
        question="Which course is the quiz for? Mention me in your reply.",
        now=NOW,
    )
    assert saved.status == "clarification_saved"
    assert service.has_pending_clarification(
        channel_id=CHANNEL,
        user_id=OWNER,
        received_at=NOW,
    )
    assert not service.has_pending_clarification(
        channel_id=CHANNEL,
        user_id="223456789012345678",
        received_at=NOW,
    )

    inspected = service.inspect_message(
        external_event_id="event-2",
        channel_id=CHANNEL,
        user_id=OWNER,
        raw_text="ECE 250",
        received_at=NOW + timedelta(minutes=1),
    )
    assert inspected.status == "resume_pending"
    assert inspected.attempt_number == 2
    assert inspected.context is not None
    assert inspected.context.prior_clarification_questions == (
        "Which course is the quiz for? Mention me in your reply.",
    )

    second = service.prepare_turn(
        external_event_id="event-2",
        channel_id=CHANNEL,
        user_id=OWNER,
        raw_text="ECE 250",
        received_at=NOW + timedelta(minutes=1),
    )
    assert second.status == "resumed"
    assert second.attempt_number == 2
    assert second.context is not None
    assert [item.get_secret_value() for item in second.context.clarification_answers] == ["ECE 250"]
    service.persist_clarification_or_exhaust(
        session_id=first.session_id,
        question="What date should I use? Mention me in your reply.",
        now=NOW + timedelta(minutes=2),
    )
    third = service.prepare_turn(
        external_event_id="event-3",
        channel_id=CHANNEL,
        user_id=OWNER,
        raw_text="tomorrow",
        received_at=NOW + timedelta(minutes=3),
    )
    assert third.status == "resumed"
    assert third.attempt_number == 3

    exhausted = service.persist_clarification_or_exhaust(
        session_id=first.session_id,
        question="One more question should exhaust.",
        now=NOW + timedelta(minutes=4),
    )

    assert exhausted.status == "exhausted"
    row, state = _session_state(engine)
    assert row.state == "completed"
    assert state.outcome == "exhausted"
    assert state.context_artifact_key is None
    with pytest.raises(ValueError, match="not open"):
        service.load_open_context(session_id=first.session_id, now=NOW + timedelta(minutes=5))
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AcademicDiscourseTurn)) == 3


@pytest.mark.parametrize("phrase", ["cancel", "never mind", "start over"])
def test_cancellation_is_model_free_and_does_not_consume_an_attempt(
    engine,
    artifacts,
    phrase: str,
) -> None:
    service = _service(engine, artifacts)
    first = service.prepare_turn(
        external_event_id=f"event-{phrase}-1",
        channel_id=CHANNEL,
        user_id=OWNER,
        raw_text="schedule an assignment",
        received_at=NOW,
    )
    assert first.session_id is not None
    service.persist_clarification_or_exhaust(
        session_id=first.session_id,
        question="Which course? Mention me in your reply.",
        now=NOW,
    )

    cancelled = service.inspect_message(
        external_event_id=f"event-{phrase}-2",
        channel_id=CHANNEL,
        user_id=OWNER,
        raw_text=phrase,
        received_at=NOW + timedelta(minutes=1),
    )

    assert cancelled.status == "cancelled"
    row, state = _session_state(engine)
    assert row.state == "completed"
    assert state.outcome == "cancelled"
    assert state.attempt_number == 1
    assert state.context_artifact_key is None
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AcademicDiscourseTurn)) == 2
        assert (
            service.inspect_message(
                external_event_id=f"event-{phrase}-2",
                channel_id=CHANNEL,
                user_id=OWNER,
                raw_text=phrase,
                received_at=NOW + timedelta(minutes=1),
            ).status
            == "duplicate"
        )


def test_wrong_class_context_artifact_fails_closed_and_clears_relational_reference(
    engine,
    artifacts,
) -> None:
    service = _service(engine, artifacts)
    first = service.prepare_turn(
        external_event_id="event-1",
        channel_id=CHANNEL,
        user_id=OWNER,
        raw_text="schedule a lab",
        received_at=NOW,
    )
    assert first.session_id is not None
    service.persist_clarification_or_exhaust(
        session_id=first.session_id,
        question="Which course? Mention me in your reply.",
        now=NOW,
    )
    wrong = artifacts.put("{}", media_type="application/json", data_class="tool_log")
    with Session(engine) as session, session.begin():
        row = session.get(AcademicDiscourseSession, first.session_id)
        assert row is not None
        state = AgentClarificationSessionState.model_validate(row.partial_state)
        row.partial_state = state.model_copy(update={"context_artifact_key": wrong.key}).model_dump(
            mode="json"
        )

    failed = service.inspect_message(
        external_event_id="event-2",
        channel_id=CHANNEL,
        user_id=OWNER,
        raw_text="ECE 250",
        received_at=NOW + timedelta(minutes=1),
    )

    assert failed.status == "failed"
    row, state = _session_state(engine)
    assert row.state == "completed"
    assert state.outcome == "failed"
    assert state.context_artifact_key is None


def test_partial_unique_index_allows_one_open_agent_clarification_per_owner(
    engine,
) -> None:
    state = AgentClarificationSessionState(
        root_event_id="event-1",
        attempt_number=1,
        context_artifact_key="a" * 64,
        outcome="awaiting_model",
    ).model_dump(mode="json")
    with Session(engine) as session, session.begin():
        AcademicRepository.create_agent_clarification_session(
            session,
            external_event_id="event-1",
            discord_channel_id=CHANNEL,
            discord_user_id=OWNER,
            started_at=NOW,
            expires_at=NOW + timedelta(hours=1),
            partial_state=state,
        )

    with pytest.raises(IntegrityError), Session(engine) as session, session.begin():
        AcademicRepository.create_agent_clarification_session(
            session,
            external_event_id="event-2",
            discord_channel_id=CHANNEL,
            discord_user_id=OWNER,
            started_at=NOW,
            expires_at=NOW + timedelta(hours=1),
            partial_state=state,
        )

    with Session(engine) as session, session.begin():
        row = session.scalar(select(AcademicDiscourseSession))
        assert row is not None
        row.state = "completed"

    with Session(engine) as session, session.begin():
        AcademicRepository.create_agent_clarification_session(
            session,
            external_event_id="event-3",
            discord_channel_id=CHANNEL,
            discord_user_id=OWNER,
            started_at=NOW,
            expires_at=NOW + timedelta(hours=1),
            partial_state=state,
        )


def test_0015_migration_extends_current_head_with_partial_unique_index() -> None:
    migration = importlib.import_module(
        "app.db.migrations.versions.0015_academic_agent_clarification"
    )

    assert migration.revision == "0015_academic_agent_clarify"
    assert migration.down_revision == "0014_academic_materials"
    source = inspect.getsource(migration.upgrade)
    assert "uq_academic_discourse_open_agent_clarification_owner" in source
    assert "agent_clarification" in source
    assert "unique=True" in source
