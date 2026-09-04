from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.connectors.discord import (
    DiscordAcademicPlannerAdapter,
    deliver_academic_message,
)
from app.db.models import AgentRun, Delivery, DeliveryStatus
from app.db.repositories import RunRepository

CHANNEL = "987654321012345678"


@pytest.fixture
def engine() -> Iterator[Engine]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    AgentRun.__table__.create(engine)
    Delivery.__table__.create(engine)
    yield engine
    engine.dispose()


@pytest.mark.asyncio
async def test_academic_delivery_uses_nonce_and_is_idempotent(engine: Engine) -> None:
    key = "academic-plan:2026-09-03:v1"
    with Session(engine) as session, session.begin():
        run = RunRepository.create_or_get(
            session,
            idempotency_key="academic-run:2026-09-03",
            agent_name="academic_planner",
            trigger="schedule",
        )
        run_id = run.id
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"id": "123456789012345678", "guild_id": "42"})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        adapter = DiscordAcademicPlannerAdapter(
            token=SecretStr("academic-token"),
            allowed_channel_ids={CHANNEL},
            client=client,
        )
        first = await deliver_academic_message(
            engine=engine,
            run_id=run_id,
            channel_id=CHANNEL,
            content="Your next study block is calculus.",
            idempotency_key=key,
            adapter=adapter,
        )
        second = await deliver_academic_message(
            engine=engine,
            run_id=run_id,
            channel_id=CHANNEL,
            content="This must not be sent again.",
            idempotency_key=key,
            adapter=adapter,
        )

    assert len(calls) == 1
    assert calls[0].read()  # request body was serialized
    assert first.id == second.id
    with Session(engine) as session:
        stored = session.scalar(select(Delivery).where(Delivery.id == first.id))
        assert stored is not None
        assert stored.status == DeliveryStatus.SENT.value
