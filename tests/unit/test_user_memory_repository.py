from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from app.db.models import Base, UserMemoryEvent, UserMemoryFact
from app.db.user_memory import UserMemoryRepository

NOW = datetime(2026, 9, 15, 12, tzinfo=UTC)
OWNER = "333333333333333333"
CHANNEL = "222222222222222222"
OTHER_OWNER = "444444444444444444"
ARTIFACT_A = "a" * 64
ARTIFACT_B = "b" * 64
ARTIFACT_C = "c" * 64


@pytest.fixture
def engine(tmp_path: Path):
    created = create_engine(f"sqlite+pysqlite:///{tmp_path / 'user-memory.db'}")
    Base.metadata.create_all(created)
    try:
        yield created
    finally:
        created.dispose()


def test_create_confirm_delete_memory_is_owner_scoped_and_idempotent(engine) -> None:
    with Session(engine) as session, session.begin():
        memory, created = UserMemoryRepository.create_memory(
            session,
            owner_user_id=OWNER,
            owner_channel_id=CHANNEL,
            external_event_id="remember-1",
            kind="preference",
            status="pending_confirmation",
            content_artifact_key=ARTIFACT_A,
            redacted_preview="Prefers morning study blocks",
            normalized_subject="study_schedule",
            confidence=0.9,
            occurred_at=NOW,
        )
        replay, replay_created = UserMemoryRepository.create_memory(
            session,
            owner_user_id=OWNER,
            owner_channel_id=CHANNEL,
            external_event_id="remember-1",
            kind="preference",
            status="pending_confirmation",
            content_artifact_key=ARTIFACT_B,
            redacted_preview="Different text should not be applied",
            normalized_subject="study_schedule",
            occurred_at=NOW,
        )

        assert created is True
        assert replay_created is False
        assert replay.id == memory.id
        assert (
            UserMemoryRepository.list_active_memories(
                session,
                owner_user_id=OWNER,
                owner_channel_id=CHANNEL,
                limit=10,
            )
            == []
        )

        confirmed = UserMemoryRepository.confirm_memory(
            session,
            owner_user_id=OWNER,
            owner_channel_id=CHANNEL,
            memory_id=memory.id,
            external_event_id="confirm-1",
            occurred_at=NOW,
        )
        assert confirmed.status == "active"
        assert confirmed.revision == 2
        assert [
            item.id
            for item in UserMemoryRepository.list_active_memories(
                session,
                owner_user_id=OWNER,
                owner_channel_id=CHANNEL,
                limit=10,
            )
        ] == [memory.id]

        with pytest.raises(NoResultFound):
            UserMemoryRepository.get_memory(
                session,
                owner_user_id=OTHER_OWNER,
                owner_channel_id=CHANNEL,
                memory_id=memory.id,
            )

        deleted = UserMemoryRepository.mark_memory_status(
            session,
            owner_user_id=OWNER,
            owner_channel_id=CHANNEL,
            memory_id=memory.id,
            external_event_id="delete-1",
            status="deleted",
            occurred_at=NOW,
        )
        memory_id = memory.id
        assert deleted.status == "deleted"
        assert deleted.revision == 3
        assert session.scalar(select(func.count()).select_from(UserMemoryEvent)) == 3

    with Session(engine) as session:
        row = session.get(UserMemoryFact, memory_id)
        assert row is not None
        assert row.content_artifact_key == ARTIFACT_A
        assert row.redacted_preview == "Prefers morning study blocks"
        assert "morning study blocks" not in row.content_artifact_key


def test_search_active_memories_ranks_sqlite_embeddings_and_degrades_to_exact(engine) -> None:
    with Session(engine) as session, session.begin():
        first, _ = UserMemoryRepository.create_memory(
            session,
            owner_user_id=OWNER,
            owner_channel_id=CHANNEL,
            external_event_id="remember-vector-1",
            kind="personal_fact",
            status="active",
            content_artifact_key=ARTIFACT_A,
            redacted_preview="Likes calculus examples",
            normalized_subject="examples",
            embedding=[1.0, 0.0],
            embedding_model="embed@test",
            occurred_at=NOW,
        )
        second, _ = UserMemoryRepository.create_memory(
            session,
            owner_user_id=OWNER,
            owner_channel_id=CHANNEL,
            external_event_id="remember-vector-2",
            kind="personal_fact",
            status="active",
            content_artifact_key=ARTIFACT_B,
            redacted_preview="Likes graph theory examples",
            normalized_subject="examples",
            embedding=[0.0, 1.0],
            embedding_model="embed@test",
            occurred_at=NOW,
        )
        UserMemoryRepository.create_memory(
            session,
            owner_user_id=OTHER_OWNER,
            owner_channel_id=CHANNEL,
            external_event_id="remember-other-owner",
            kind="personal_fact",
            status="active",
            content_artifact_key=ARTIFACT_C,
            redacted_preview="Cross-owner memory",
            normalized_subject="examples",
            embedding=[1.0, 0.0],
            embedding_model="embed@test",
            occurred_at=NOW,
        )

        ranked = UserMemoryRepository.search_active_memories(
            session,
            owner_user_id=OWNER,
            owner_channel_id=CHANNEL,
            query_embedding=[0.9, 0.1],
            embedding_model="embed@test",
            limit=2,
        )
        fallback = UserMemoryRepository.search_active_memories(
            session,
            owner_user_id=OWNER,
            owner_channel_id=CHANNEL,
            limit=10,
        )

        assert [item.id for item in ranked] == [first.id, second.id]
        assert {item.id for item in fallback} == {first.id, second.id}
