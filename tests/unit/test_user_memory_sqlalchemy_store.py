from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine

from app.agents.memory import UserMemoryService
from app.agents.memory.contracts import UserMemoryOwnerScope
from app.artifacts.store import ArtifactStore
from app.db.models import Base
from app.db.user_memory import SQLAlchemyUserMemoryStore

_NOW = datetime(2026, 9, 15, 16, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_sqlalchemy_memory_store_round_trip_and_owner_scope(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'memory.db'}")
    Base.metadata.create_all(engine)
    service = UserMemoryService(
        store=SQLAlchemyUserMemoryStore(
            engine=engine,
            artifact_store=ArtifactStore(tmp_path / "artifacts"),
        )
    )

    remembered = await service.manage(
        owner_user_id="12345",
        owner_channel_id="67890",
        action="remember",
        raw_text="Please remember that I prefer concise replies.",
        content="I prefer concise replies.",
        received_at=_NOW,
        source_external_event_id="event-1",
    )
    assert remembered.status == "applied"

    found = await service.retrieve(
        owner_scope=UserMemoryOwnerScope(
            owner_user_id="12345",
            owner_channel_id="67890",
        ),
        query="concise replies",
    )
    assert [item.content for item in found.items] == ["I prefer concise replies."]
    assert all(item.id in remembered.memory_ids for item in found.items)

    other_owner = await service.retrieve(
        owner_scope=UserMemoryOwnerScope(
            owner_user_id="99999",
            owner_channel_id="67890",
        ),
        query="concise replies",
    )
    assert other_owner.items == ()

    forgotten = await service.manage(
        owner_user_id="12345",
        owner_channel_id="67890",
        action="forget",
        raw_text="Forget that preference.",
        content="concise replies",
        received_at=_NOW,
        source_external_event_id="event-2",
    )
    assert forgotten.status == "deleted"
    assert (
        await service.retrieve(
            owner_scope=UserMemoryOwnerScope(
                owner_user_id="12345",
                owner_channel_id="67890",
            ),
            query="concise replies",
        )
    ).items == ()
