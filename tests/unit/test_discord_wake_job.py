from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.agents.academic_planner import discord_wake_job
from app.core.config import Settings
from app.db.discord_wake import DiscordWakeInboundInput, DiscordWakeRepository
from app.db.models import Base, DiscordWakeInbound


@pytest.mark.parametrize("handler_status", ["handled", "failed"])
@pytest.mark.parametrize("event_kind", ["message", "interaction"])
async def test_wake_job_persists_handler_outcome(monkeypatch, handler_status, event_kind):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(discord_wake_job, "Database", lambda _: SimpleNamespace(engine=engine))
    job = discord_wake_job.DiscordWakeJob(Settings(_env_file=None))
    handler = AsyncMock(return_value=handler_status)
    monkeypatch.setattr(job, "_run_message", handler)
    monkeypatch.setattr(job, "_run_interaction", handler)
    try:
        with Session(engine) as session, session.begin():
            accepted = DiscordWakeRepository.accept_verified_event(
                session,
                DiscordWakeInboundInput(
                    discord_event_id="123456789012345678",
                    handoff_nonce="test-wake-nonce",
                    event_kind=event_kind,
                    action="academic_checkin" if event_kind == "message" else "agent_clarification",
                    interaction_action="quiz" if event_kind == "interaction" else None,
                    content_artifact_key="test-artifact",
                    received_at=datetime(2026, 9, 9, tzinfo=UTC),
                ),
            )
        result = await job(str(accepted.wake_id), 1, 3)
        assert result["status"] == handler_status
        handler.assert_awaited_once()
        with Session(engine) as session:
            row = session.get(DiscordWakeInbound, accepted.wake_id)
            assert row is not None
            if handler_status == "failed":
                assert row.state == "failed"
                assert row.last_error_code == "discord_wake_handler_failed"
                assert row.failed_at is not None
                assert row.completed_at is None
            else:
                assert row.state == "completed"
                assert row.last_error_code is None
                assert row.completed_at is not None
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_message_worker_preserves_content_without_synthetic_mention(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    monkeypatch.setattr(discord_wake_job, "Database", lambda _: SimpleNamespace(engine=engine))
    job = discord_wake_job.DiscordWakeJob(Settings(_env_file=None))
    raw_content = "ordinary prose without a mention"
    monkeypatch.setattr(job, "_load_content", lambda _: raw_content)
    captured = []
    closed = False

    async def handler(message):
        captured.append(message)
        return SimpleNamespace(status="handled")

    def close() -> None:
        nonlocal closed
        closed = True

    service = SimpleNamespace(handler=handler, close=close)
    monkeypatch.setattr(discord_wake_job, "create_academic_discord_service", lambda _: service)
    row = SimpleNamespace(
        event_kind="message",
        action="academic_checkin",
        interaction_action=None,
        clarification_id=None,
        discord_channel_id="222222222222222222",
        discord_user_id="333333333333333333",
        discord_message_id="111111111111111111",
        ack_message_id="444444444444444444",
        content_artifact_key="artifact-key",
        received_at=datetime(2026, 9, 9, tzinfo=UTC),
    )

    status = await job._run_message(discord_wake_job._WakeSnapshot(row))

    assert status == "handled"
    assert closed is True
    assert len(captured) == 1
    assert captured[0].content.get_secret_value() == raw_content
    assert captured[0].mentioned_user_ids == ()
