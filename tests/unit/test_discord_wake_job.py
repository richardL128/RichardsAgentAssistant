import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.agents.academic_planner import discord_wake_job
from app.agents.harness import UserAbortRequested
from app.artifacts.store import ArtifactStore
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
    monkeypatch.setattr(
        discord_wake_job,
        "create_academic_discord_service",
        lambda _, *, database: service,
    )
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
    assert closed is False
    job.close()
    assert closed is True
    assert len(captured) == 1
    assert captured[0].content.get_secret_value() == raw_content
    assert captured[0].mentioned_user_ids == ()


@pytest.mark.asyncio
async def test_message_worker_reconstructs_attachment_only_manifest(monkeypatch, tmp_path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    settings = Settings(_env_file=None, artifact_root=tmp_path / "artifacts")
    material_id = UUID("e50b8b53-8d14-49f8-b7aa-c2f2235b50b7")
    artifact = ArtifactStore(settings.artifact_root).put(
        json.dumps(
            {
                "version": "discord-academic-inbound-v2",
                "message_text": "",
                "inbound_material_ids": [str(material_id)],
            }
        ),
        media_type="application/json",
        data_class="discord_inbound_manifest",
        already_redacted=True,
    )
    monkeypatch.setattr(discord_wake_job, "Database", lambda _: SimpleNamespace(engine=engine))
    job = discord_wake_job.DiscordWakeJob(settings)
    captured = []

    async def handler(message):
        captured.append(message)
        return SimpleNamespace(status="handled")

    service = SimpleNamespace(handler=handler, close=lambda: None)
    monkeypatch.setattr(
        discord_wake_job,
        "create_academic_discord_service",
        lambda _, *, database: service,
    )
    row = SimpleNamespace(
        event_kind="message",
        action="academic_checkin",
        interaction_action=None,
        clarification_id=None,
        discord_channel_id="222222222222222222",
        discord_user_id="333333333333333333",
        discord_message_id="111111111111111111",
        ack_message_id="444444444444444444",
        content_artifact_key=artifact.key,
        received_at=datetime(2026, 9, 9, tzinfo=UTC),
    )

    status = await job._run_message(discord_wake_job._WakeSnapshot(row))

    assert status == "handled"
    assert captured[0].content.get_secret_value() == ""
    assert captured[0].attachments == ()
    assert captured[0].inbound_material_ids == (material_id,)


@pytest.mark.asyncio
async def test_message_worker_reuses_one_lazy_service_across_wakes(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    monkeypatch.setattr(discord_wake_job, "Database", lambda _: SimpleNamespace(engine=engine))
    job = discord_wake_job.DiscordWakeJob(Settings(_env_file=None))
    monkeypatch.setattr(job, "_load_content", lambda _: "wake text")
    created: list[object] = []
    captured: list[str] = []

    class ReusedService:
        def __init__(self) -> None:
            self.close_count = 0

        async def handle(self, message, *, abort_check=None, activity_sink=None):
            captured.append(message.message_id)
            assert abort_check is None
            assert activity_sink is None
            return SimpleNamespace(status="handled")

        def close(self) -> None:
            self.close_count += 1

    def factory(settings, *, database):
        assert database is job._database
        service = ReusedService()
        created.append(service)
        return service

    monkeypatch.setattr(discord_wake_job, "create_academic_discord_service", factory)
    base = {
        "event_kind": "message",
        "action": "academic_checkin",
        "interaction_action": None,
        "clarification_id": None,
        "discord_channel_id": "222222222222222222",
        "discord_user_id": "333333333333333333",
        "ack_message_id": None,
        "content_artifact_key": "artifact-key",
        "received_at": datetime(2026, 9, 9, tzinfo=UTC),
    }

    first = await job._run_message(
        discord_wake_job._WakeSnapshot(
            SimpleNamespace(**base, discord_message_id="111111111111111111")
        )
    )
    second = await job._run_message(
        discord_wake_job._WakeSnapshot(
            SimpleNamespace(**base, discord_message_id="111111111111111112")
        )
    )

    assert first == "handled"
    assert second == "handled"
    assert len(created) == 1
    assert captured == ["111111111111111111", "111111111111111112"]
    assert created[0].close_count == 0
    job.close()
    assert created[0].close_count == 1


@pytest.mark.asyncio
async def test_run_discord_wake_reuses_injected_worker_job_and_reset_closes_it() -> None:
    calls: list[tuple[str, int, int]] = []

    class InjectedJob:
        def __init__(self) -> None:
            self.closed = 0

        async def __call__(
            self,
            wake_id: str,
            attempt: int,
            attempt_limit: int,
        ) -> dict[str, object]:
            calls.append((wake_id, attempt, attempt_limit))
            return {"status": "handled", "wake_id": wake_id}

        def close(self) -> None:
            self.closed += 1

    injected = InjectedJob()
    discord_wake_job.set_worker_discord_wake_job(injected)  # type: ignore[arg-type]
    try:
        first = await discord_wake_job.run_discord_wake(
            "00000000-0000-0000-0000-000000000001",
            1,
            3,
        )
        second = await discord_wake_job.run_discord_wake(
            "00000000-0000-0000-0000-000000000002",
            2,
            3,
        )
    finally:
        discord_wake_job.close_worker_discord_wake_job()

    assert first["status"] == "handled"
    assert second["status"] == "handled"
    assert calls == [
        ("00000000-0000-0000-0000-000000000001", 1, 3),
        ("00000000-0000-0000-0000-000000000002", 2, 3),
    ]
    assert injected.closed == 1


@pytest.mark.asyncio
async def test_wake_job_marks_user_abort_and_reraises_queue_cancellation(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(discord_wake_job, "Database", lambda _: SimpleNamespace(engine=engine))
    job = discord_wake_job.DiscordWakeJob(Settings(_env_file=None))
    monkeypatch.setattr(job, "_abort_requested", lambda _wake_id: True)
    monkeypatch.setattr(job, "_run_message", AsyncMock(side_effect=UserAbortRequested()))
    with Session(engine) as session, session.begin():
        accepted = DiscordWakeRepository.accept_verified_event(
            session,
            DiscordWakeInboundInput(
                discord_event_id="123456789012345678",
                handoff_nonce="test-wake-abort-nonce",
                event_kind="message",
                action="academic_checkin",
                content_artifact_key="test-artifact",
                received_at=datetime(2026, 9, 9, tzinfo=UTC),
            ),
        )

    try:
        with pytest.raises(UserAbortRequested):
            await job(str(accepted.wake_id), 1, 3)

        with Session(engine) as session:
            row = session.get(DiscordWakeInbound, accepted.wake_id)
            assert row is not None
            assert row.state == "aborted"
            assert row.abort_reason_code == "user_abort"
    finally:
        engine.dispose()


def test_wake_job_persists_allowlisted_activity_without_private_tool_data(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(discord_wake_job, "Database", lambda _: SimpleNamespace(engine=engine))
    job = discord_wake_job.DiscordWakeJob(Settings(_env_file=None))
    try:
        with Session(engine) as session, session.begin():
            accepted = DiscordWakeRepository.accept_verified_event(
                session,
                DiscordWakeInboundInput(
                    discord_event_id="123456789012345679",
                    handoff_nonce="test-wake-activity-nonce",
                    event_kind="message",
                    action="academic_checkin",
                    content_artifact_key="test-artifact",
                    received_at=datetime(2026, 9, 9, tzinfo=UTC),
                ),
            )

        job._record_safe_activity(
            accepted.wake_id,
            {
                "phase": "tool_started",
                "model_turn": 2,
                "tool_name": "search_courses",
                "tool_activity": "course_data",
                "tool_status": "in_flight",
                "side_effect_class": "read_only",
                "args": {"private": "must not persist"},
            },
        )

        with Session(engine) as session:
            row = session.get(DiscordWakeInbound, accepted.wake_id)
            assert row is not None
            assert row.activity_phase == "tool_started"
            assert row.activity_model_turn == 2
            assert row.activity_tool_name == "search_courses"
            assert row.activity_tool_status == "running"
            assert row.activity_side_effect_class == "read_only"
            assert "private" not in str(row.__dict__)
    finally:
        engine.dispose()
