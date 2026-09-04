"""Unit coverage for idempotent Discord finance briefing delivery."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.agents.finance.contracts import (
    BriefingPayload,
    EventCard,
    ExposureMapping,
    ImpactLabel,
    QuantValue,
)
from app.connectors.discord import (
    DiscordFinanceBriefingAdapter,
    DiscordFinanceBriefingDelivery,
    deliver_finance_briefing,
)
from app.core.errors import ErrorCategory, LifeAgentError, authorization_error
from app.db.models import Base, Delivery, DeliveryStatus
from app.db.repositories import RunRepository

CHANNEL_ID = "987654321012345678"
OTHER_CHANNEL_ID = "111112222233333"
TOKEN = "never-print-this-finance-token"
NOW = datetime(2026, 9, 4, 13, tzinfo=UTC)
KEY = "finance:2026-09-04:market-open:v1"

Responder = Callable[[httpx.Request], httpx.Response]


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


@pytest.fixture
def run_id(engine: Engine) -> UUID:
    with Session(engine) as session, session.begin():
        run = RunRepository.create_or_get(
            session,
            idempotency_key=KEY,
            agent_name="finance",
            trigger="schedule",
        )
        return run.id


class _Recorder:
    """Capture outbound Discord requests and reply with a scripted response."""

    def __init__(self, responder: Responder) -> None:
        self.requests: list[httpx.Request] = []
        self._responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responder(request)


def _ok(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"id": "123456789012345678", "guild_id": "42"})


def _card(index: int = 1, *, verbose: bool = False) -> EventCard:
    suffix = f" {index}"
    fact = "ACME reported a verified supply-chain update" + suffix
    uncertainty = "Timing and durability remain uncertain."
    counter_case = "The event may already be reflected in expectations."
    if verbose:
        uncertainty = " ".join(["Timing remains uncertain for the thesis monitor."] * 12)
        counter_case = " ".join(["The event may prove temporary."] * 12)
    return EventCard(
        event_id=f"event-{index}",
        title="ACME supply-chain update" + suffix,
        verified_facts=(fact,),
        uncertainty=uncertainty,
        counter_case=counter_case,
        impact_label=ImpactLabel.MONITOR,
        exposure=ExposureMapping(event_id=f"event-{index}", holding_symbols=("ACME",)),
        citations=("dvids",),
        numbers=(
            QuantValue(
                label="Exposure weight",
                value=2.5,
                unit="percent",
                as_of=date(2026, 9, 4),
                source_ids=("dvids",),
            ),
        ),
    )


def _payload(run_id: UUID, *, cards: tuple[EventCard, ...] | None = None) -> BriefingPayload:
    return BriefingPayload(
        run_id=run_id,
        source_allowlist_version="finance-sources-2026.09",
        generated_at=NOW,
        status="succeeded",
        cards=cards if cards is not None else (_card(),),
        tickers=("ACME",),
        themes=("supply-chain",),
    )


async def _deliver(
    engine: Engine,
    run_id: UUID,
    handler: _Recorder,
    *,
    key: str = KEY,
    allowed: set[str] | None = None,
    payload: BriefingPayload | None = None,
) -> Delivery:
    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10",
        transport=httpx.MockTransport(handler),
    ) as client:
        adapter = DiscordFinanceBriefingAdapter(
            token=SecretStr(TOKEN),
            allowed_channel_ids={CHANNEL_ID} if allowed is None else allowed,
            client=client,
        )
        delivery = DiscordFinanceBriefingDelivery(
            engine=engine,
            run_id=run_id,
            channel_id=CHANNEL_ID,
            adapter=adapter,
        )
        return await delivery.send_briefing(
            payload or _payload(run_id),
            idempotency_key=key,
        )


def _stored_delivery(engine: Engine, key: str = KEY) -> Delivery:
    with Session(engine) as session:
        delivery = session.scalar(select(Delivery).where(Delivery.idempotency_key == key))
    assert delivery is not None
    return delivery


async def test_finance_briefing_posts_rendered_content_once_and_records_sent(
    engine: Engine,
    run_id: UUID,
) -> None:
    handler = _Recorder(_ok)
    first = await _deliver(engine, run_id, handler)
    second = await _deliver(engine, run_id, _Recorder(_ok))

    assert len(handler.requests) == 1
    assert first.id == second.id
    assert second.status == DeliveryStatus.SENT
    assert second.attempt_count == 1
    assert second.external_url == f"https://discord.com/channels/42/{CHANNEL_ID}/123456789012345678"

    request = handler.requests[0]
    assert request.url.path == f"/api/v10/channels/{CHANNEL_ID}/messages"
    assert request.headers["Authorization"] == f"Bot {TOKEN}"
    body = json.loads(request.content)
    assert body["nonce"] == str(first.id)
    assert body["enforce_nonce"] is True
    assert body["allowed_mentions"] == {"parse": []}
    assert "LifeAgent finance briefing succeeded" in body["content"]
    assert "ACME supply-chain update" in body["content"]
    assert "raw_body" not in body["content"]
    assert TOKEN not in request.content.decode()


async def test_non_allowlisted_finance_channel_records_failed_without_http(
    engine: Engine,
    run_id: UUID,
) -> None:
    handler = _Recorder(_ok)

    with pytest.raises(ValueError, match="allowlisted"):
        await _deliver(engine, run_id, handler, allowed={OTHER_CHANNEL_ID})

    assert len(handler.requests) == 0
    delivery = _stored_delivery(engine)
    assert delivery.status == DeliveryStatus.FAILED
    assert delivery.error_code == "input_invalid"


async def test_transient_finance_discord_failure_records_uncertain(
    engine: Engine,
    run_id: UUID,
) -> None:
    handler = _Recorder(lambda _: httpx.Response(503))

    with pytest.raises(LifeAgentError) as raised:
        await _deliver(engine, run_id, handler)

    assert raised.value.record.category is ErrorCategory.TRANSIENT
    delivery = _stored_delivery(engine)
    assert delivery.status == DeliveryStatus.UNCERTAIN
    assert delivery.error_code == "delivery_uncertain"


async def test_finance_idempotency_key_must_match_briefing_date(
    engine: Engine,
    run_id: UUID,
) -> None:
    bad_key = "finance:2026-09-05:market-open:v1"
    handler = _Recorder(_ok)

    with pytest.raises(ValueError, match="idempotency key"):
        await _deliver(engine, run_id, handler, key=bad_key)

    assert len(handler.requests) == 0
    delivery = _stored_delivery(engine, bad_key)
    assert delivery.status == DeliveryStatus.FAILED
    assert delivery.error_code == "input_invalid"


async def test_long_finance_briefing_is_clamped_to_one_discord_message(
    engine: Engine,
    run_id: UUID,
) -> None:
    handler = _Recorder(_ok)
    cards = tuple(_card(index, verbose=True) for index in range(1, 8))
    payload = _payload(run_id, cards=cards)

    await _deliver(engine, run_id, handler, payload=payload)

    body = json.loads(handler.requests[0].content)
    assert len(body["content"]) == 2_000
    assert body["content"].endswith("[truncated; see persisted finance briefing payload]")


async def test_helper_can_deliver_without_the_workflow_wrapper(
    engine: Engine,
    run_id: UUID,
) -> None:
    handler = _Recorder(_ok)
    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10",
        transport=httpx.MockTransport(handler),
    ) as client:
        adapter = DiscordFinanceBriefingAdapter(
            token=SecretStr(TOKEN),
            allowed_channel_ids={CHANNEL_ID},
            client=client,
        )
        delivery = await deliver_finance_briefing(
            engine=engine,
            run_id=run_id,
            channel_id=CHANNEL_ID,
            payload=_payload(run_id),
            idempotency_key=KEY,
            adapter=adapter,
        )

    assert delivery.status == DeliveryStatus.SENT
    assert len(handler.requests) == 1


async def test_settings_adapter_failure_records_failed_intent(
    engine: Engine,
    run_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(_: str) -> DiscordFinanceBriefingAdapter:
        raise authorization_error("Discord finance channel is not configured")

    monkeypatch.setattr(
        "app.connectors.discord._finance_adapter_from_settings",
        unavailable,
    )

    with pytest.raises(LifeAgentError) as raised:
        await deliver_finance_briefing(
            engine=engine,
            run_id=run_id,
            channel_id=CHANNEL_ID,
            payload=_payload(run_id),
            idempotency_key=KEY,
        )

    assert raised.value.record.category is ErrorCategory.AUTHORIZATION
    delivery = _stored_delivery(engine)
    assert delivery.status == DeliveryStatus.FAILED
    assert delivery.error_code == "authorization_invalid"
