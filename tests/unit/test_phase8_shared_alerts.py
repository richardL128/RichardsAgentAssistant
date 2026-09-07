from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.connectors.discord import DiscordFailureAlertAdapter, deliver_failure_alert
from app.core.config import Settings
from app.core.errors import ErrorCode
from app.db.models import Base, Delivery, DeliveryStatus
from app.health.checks import HealthCheck, HealthState
from app.health.evaluator import OperationalHealth
from app.queue import tasks

CHANNEL_ID = "987654321012345678"
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine() -> Iterator[Engine]:
    created = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(created)
    try:
        yield created
    finally:
        created.dispose()


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            200,
            json={"id": "123456789012345678", "guild_id": "42424242424242424"},
        )


def _failed_health() -> OperationalHealth:
    return OperationalHealth(
        component="shared_services",
        state=HealthState.FAILED,
        rule="connector_unauthenticated",
        diagnostic="discord_authentication=failed",
        evaluated_at=NOW,
        last_success_at=None,
        next_expected_at=NOW,
    )


def _healthy_health() -> OperationalHealth:
    return OperationalHealth(
        component="shared_services",
        state=HealthState.HEALTHY,
        rule="processing_and_delivery_succeeded",
        diagnostic="all shared-service probes are healthy",
        evaluated_at=NOW,
        last_success_at=NOW,
        next_expected_at=NOW,
    )


async def test_deliver_failure_alert_is_idempotent(engine: Engine) -> None:
    recorder = _Recorder()
    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10",
        transport=httpx.MockTransport(recorder),
    ) as client:
        adapter = DiscordFailureAlertAdapter(
            token=SecretStr("never-print-this-token"),
            allowed_channel_ids={CHANNEL_ID},
            client=client,
        )
        first = await deliver_failure_alert(
            engine=engine,
            run_id=UUID("11111111-1111-4111-8111-111111111111"),
            channel_id=CHANNEL_ID,
            component="shared_services",
            state=HealthState.FAILED,
            error_code=ErrorCode.AUTHORIZATION_INVALID,
            attempt=0,
            attempt_limit=3,
            idempotency_key="shared-services-alert:failed:connector_unauthenticated:abcd:v1",
            adapter=adapter,
        )
        second = await deliver_failure_alert(
            engine=engine,
            run_id=UUID("11111111-1111-4111-8111-111111111111"),
            channel_id=CHANNEL_ID,
            component="shared_services",
            state=HealthState.FAILED,
            error_code=ErrorCode.AUTHORIZATION_INVALID,
            attempt=0,
            attempt_limit=3,
            idempotency_key="shared-services-alert:failed:connector_unauthenticated:abcd:v1",
            adapter=adapter,
        )

    assert first.id == second.id
    assert second.status == DeliveryStatus.SENT.value
    assert len(recorder.requests) == 1
    body = json.loads(recorder.requests[0].content)
    assert body["nonce"] == first.id.hex[:25]
    assert body["enforce_nonce"] is True
    assert "never-print-this-token" not in recorder.requests[0].content.decode()


async def test_shared_services_alert_hook_skips_healthy_state(engine: Engine) -> None:
    async def fail_if_called(**_kwargs: object) -> Delivery:
        raise AssertionError("healthy shared-services checks must not send Discord alerts")

    result = await tasks._maybe_send_shared_services_alert(
        database=SimpleNamespace(engine=engine),
        settings=Settings(discord_bot_token="discord-secret", discord_target_channels=[CHANNEL_ID]),
        health=_healthy_health(),
        checks=(),
        alert_sender=fail_if_called,
    )

    assert result == {"status": "not_required"}


async def test_shared_services_alert_hook_uses_injected_sender(engine: Engine) -> None:
    calls: list[dict[str, object]] = []

    async def fake_sender(**kwargs: object) -> Delivery:
        calls.append(kwargs)
        with Session(engine) as session, session.begin():
            delivery = Delivery(
                run_id=kwargs["run_id"],
                channel="discord",
                target=CHANNEL_ID,
                idempotency_key=kwargs["idempotency_key"],
                status=DeliveryStatus.SENT,
            )
            session.add(delivery)
            session.flush()
            session.expunge(delivery)
            return delivery

    result = await tasks._maybe_send_shared_services_alert(
        database=SimpleNamespace(engine=engine),
        settings=Settings(discord_bot_token="discord-secret", discord_target_channels=[CHANNEL_ID]),
        health=_failed_health(),
        checks=(
            HealthCheck(
                name="discord_authentication",
                state=HealthState.FAILED,
                diagnostic="invalid",
            ),
        ),
        alert_sender=fake_sender,
    )

    assert result["status"] == "sent"
    assert len(calls) == 1
    assert calls[0]["channel_id"] == CHANNEL_ID
    assert calls[0]["error_code"] is ErrorCode.AUTHORIZATION_INVALID
