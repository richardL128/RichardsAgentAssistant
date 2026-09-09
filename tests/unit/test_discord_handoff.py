from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.discord_handoff import router
from app.connectors.discord import DiscordFetchedAuthor, DiscordFetchedMessage
from app.core.config import Settings
from app.db.models import Base, DiscordWakeInbound
from app.host.handoff import (
    DiscordHostHandoffEvent,
    DiscordHostInteractionHandoffEvent,
    canonical_handoff_body,
    handoff_nonce,
    sign_handoff_body,
)

MESSAGE_ID = "111111111111111111"
CHANNEL_ID = "222222222222222222"
USER_ID = "333333333333333333"
BOT_ID = "444444444444444444"
ACK_ID = "555555555555555555"
INTERACTION_ID = "666666666666666666"
EVENT_TIME = datetime(2026, 9, 9, 12, tzinfo=UTC)


@pytest.fixture
def handoff_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    settings = Settings(
        _env_file=None,
        artifact_root=tmp_path / "artifacts",
        discord_bot_token="discord-token",
        discord_application_id=BOT_ID,
        discord_academic_channel_id=CHANNEL_ID,
        discord_academic_authorized_user_ids=[int(USER_ID)],
        discord_academic_message_content_enabled=True,
        discord_host_handoff_secret="handoff-secret",
    )
    app = FastAPI()
    app.include_router(router)
    app.state.settings = settings
    app.state.database = SimpleNamespace(engine=engine)
    app.state.academic_agent_clarification_service = SimpleNamespace(
        has_pending_clarification=lambda **_kwargs: False
    )
    queued: list[str] = []

    async def defer(wake_id: str) -> int:
        queued.append(wake_id)
        return len(queued)

    monkeypatch.setattr("app.api.discord_handoff.queue_tasks.defer_discord_wake", defer)
    try:
        yield app, settings, engine, queued
    finally:
        engine.dispose()


def _message_event(
    *, now: datetime | None = None, acknowledgement_message_id: str | None = ACK_ID
) -> DiscordHostHandoffEvent:
    return DiscordHostHandoffEvent(
        message_id=MESSAGE_ID,
        channel_id=CHANNEL_ID,
        author_id=USER_ID,
        event_timestamp=EVENT_TIME,
        acknowledgement_message_id=acknowledgement_message_id,
        handoff_timestamp=now or datetime.now(UTC),
        nonce=handoff_nonce(MESSAGE_ID),
    )


def _signed_request(event, secret: SecretStr) -> tuple[bytes, dict[str, str]]:
    body = canonical_handoff_body(event)
    return body, {"x-lifeagent-handoff-signature": sign_handoff_body(body, secret)}


def _install_valid_discord_refetch(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fetch(_adapter, *, channel_id: str, message_id: str):
        assert channel_id == CHANNEL_ID
        assert message_id == MESSAGE_ID
        return DiscordFetchedMessage(
            id=MESSAGE_ID,
            channel_id=CHANNEL_ID,
            author=DiscordFetchedAuthor(id=USER_ID),
            timestamp=EVENT_TIME,
            content=SecretStr(f"<@{BOT_ID}> plan my study session"),
            mentions=(DiscordFetchedAuthor(id=BOT_ID, bot=True),),
        )

    async def validate(_adapter, *, channel_id: str, message_id: str, bot_user_id: str):
        assert (channel_id, message_id, bot_user_id) == (CHANNEL_ID, ACK_ID, BOT_ID)
        return DiscordFetchedMessage(
            id=ACK_ID,
            channel_id=CHANNEL_ID,
            author=DiscordFetchedAuthor(id=BOT_ID, bot=True),
            timestamp=EVENT_TIME,
            content=SecretStr("wake acknowledgement"),
        )

    monkeypatch.setattr(
        "app.api.discord_handoff.DiscordAcademicPlannerAdapter.fetch_message", fetch
    )
    monkeypatch.setattr(
        "app.api.discord_handoff.DiscordAcademicPlannerAdapter.validate_wake_acknowledgement",
        validate,
    )


def test_signed_message_handoff_refetches_persists_and_queues_once(
    handoff_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, settings, engine, queued = handoff_app
    _install_valid_discord_refetch(monkeypatch)
    event = _message_event()
    body, headers = _signed_request(event, settings.discord_host_handoff_secret)

    with TestClient(app) as client:
        first = client.post("/internal/discord/academic/handoff", content=body, headers=headers)
        replay = client.post("/internal/discord/academic/handoff", content=body, headers=headers)

    assert first.status_code == 202
    assert first.json() == {"status": "accepted"}
    assert replay.status_code == 200
    assert replay.json() == {"status": "duplicate"}
    assert len(queued) == 1
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(DiscordWakeInbound)) == 1
        stored = session.scalar(select(DiscordWakeInbound))
        assert stored is not None
        assert stored.discord_event_id == MESSAGE_ID
        assert stored.ack_message_id == ACK_ID
        assert stored.enqueued_at is not None


def test_handoff_rejects_bad_signature_stale_and_oversized_body(handoff_app) -> None:
    app, settings, _engine, queued = handoff_app
    event = _message_event()
    body, _headers = _signed_request(event, settings.discord_host_handoff_secret)
    stale = _message_event(now=datetime.now(UTC) - timedelta(minutes=5))
    stale_body, stale_headers = _signed_request(stale, settings.discord_host_handoff_secret)

    with TestClient(app) as client:
        bad_auth = client.post(
            "/internal/discord/academic/handoff",
            content=body,
            headers={"x-lifeagent-handoff-signature": "sha256=" + "0" * 64},
        )
        stale_response = client.post(
            "/internal/discord/academic/handoff", content=stale_body, headers=stale_headers
        )
        oversized = client.post(
            "/internal/discord/academic/handoff",
            content=b"x" * (settings.discord_handoff_max_body_bytes + 1),
        )

    assert bad_auth.status_code == 401
    assert stale_response.status_code == 408
    assert oversized.status_code == 413
    assert queued == []


def test_handoff_rejects_refetched_message_with_changed_identity(
    handoff_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, settings, engine, queued = handoff_app

    async def changed_fetch(_adapter, **_kwargs):
        return DiscordFetchedMessage(
            id=MESSAGE_ID,
            channel_id=CHANNEL_ID,
            author=DiscordFetchedAuthor(id="777777777777777777"),
            timestamp=EVENT_TIME,
            content=SecretStr(f"<@{BOT_ID}> invalid author"),
            mentions=(DiscordFetchedAuthor(id=BOT_ID, bot=True),),
        )

    monkeypatch.setattr(
        "app.api.discord_handoff.DiscordAcademicPlannerAdapter.fetch_message", changed_fetch
    )
    body, headers = _signed_request(_message_event(), settings.discord_host_handoff_secret)
    with TestClient(app) as client:
        response = client.post("/internal/discord/academic/handoff", content=body, headers=headers)

    assert response.status_code == 422
    assert queued == []
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(DiscordWakeInbound)) == 0


def test_handoff_ignores_unmentioned_prose_without_pending_session_before_persistence(
    handoff_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, settings, engine, queued = handoff_app

    async def fetch(_adapter, **_kwargs):
        return DiscordFetchedMessage(
            id=MESSAGE_ID,
            channel_id=CHANNEL_ID,
            author=DiscordFetchedAuthor(id=USER_ID),
            timestamp=EVENT_TIME,
            content=SecretStr("unrelated prose without a mention"),
        )

    monkeypatch.setattr(
        "app.api.discord_handoff.DiscordAcademicPlannerAdapter.fetch_message", fetch
    )
    body, headers = _signed_request(_message_event(), settings.discord_host_handoff_secret)
    with TestClient(app) as client:
        response = client.post("/internal/discord/academic/handoff", content=body, headers=headers)

    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert queued == []
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(DiscordWakeInbound)) == 0


def test_handoff_persists_owner_scoped_unmentioned_continuation_without_host_ack(
    handoff_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, settings, engine, queued = handoff_app
    app.state.academic_agent_clarification_service = SimpleNamespace(
        has_pending_clarification=lambda **_kwargs: True
    )

    async def fetch(_adapter, **_kwargs):
        return DiscordFetchedMessage(
            id=MESSAGE_ID,
            channel_id=CHANNEL_ID,
            author=DiscordFetchedAuthor(id=USER_ID),
            timestamp=EVENT_TIME,
            content=SecretStr("Tomorrow at 7 PM, 45 minutes each."),
        )

    monkeypatch.setattr(
        "app.api.discord_handoff.DiscordAcademicPlannerAdapter.fetch_message", fetch
    )
    event = _message_event(acknowledgement_message_id=None)
    body, headers = _signed_request(event, settings.discord_host_handoff_secret)
    with TestClient(app) as client:
        response = client.post("/internal/discord/academic/handoff", content=body, headers=headers)

    assert response.status_code == 202
    assert len(queued) == 1
    with Session(engine) as session:
        row = session.scalar(select(DiscordWakeInbound))
        assert row is not None
        assert row.action == "academic_continuation"
        assert row.ack_message_id is None


def test_signed_interaction_handoff_contains_no_token_and_queues_reference(
    handoff_app,
) -> None:
    app, settings, engine, queued = handoff_app
    event = DiscordHostInteractionHandoffEvent(
        interaction_id=INTERACTION_ID,
        channel_id=CHANNEL_ID,
        user_id=USER_ID,
        clarification_id=UUID("77777777-7777-4777-8777-777777777777"),
        action="assignment",
        event_timestamp=EVENT_TIME,
        handoff_timestamp=datetime.now(UTC),
        nonce=handoff_nonce(INTERACTION_ID),
    )
    body, headers = _signed_request(event, settings.discord_host_handoff_secret)
    assert b"token" not in body

    with TestClient(app) as client:
        response = client.post("/internal/discord/academic/handoff", content=body, headers=headers)

    assert response.status_code == 202
    assert len(queued) == 1
    with Session(engine) as session:
        stored = session.scalar(select(DiscordWakeInbound))
        assert stored is not None
        assert stored.event_kind == "interaction"
        assert stored.interaction_action == "assignment"
        assert stored.discord_interaction_id == INTERACTION_ID
