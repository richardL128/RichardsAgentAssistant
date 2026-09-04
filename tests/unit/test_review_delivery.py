"""Unit coverage for idempotent Discord code-review summary delivery."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import date
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.connectors.discord import (
    DiscordDailyReviewAdapter,
    DiscordReviewSummaryAdapter,
    ReviewSummary,
    deliver_daily_review_report,
    deliver_review_summary,
)
from app.core.errors import ErrorCategory, LifeAgentError
from app.db.models import Base, Delivery, DeliveryStatus
from app.db.repositories import RunRepository

CHANNEL_ID = "987654321012345678"
OTHER_CHANNEL_ID = "111112222233333"
REPOSITORY = "acme/example"
HEAD_SHA = "a" * 40
KEY = f"code-review:{REPOSITORY}:{HEAD_SHA}"
TOKEN = "never-print-this-review-token"
FINDING_COUNTS = {"block": 2, "important": 1, "suggestion": 5}

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
            agent_name="code_review",
            trigger="github_push",
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
    return httpx.Response(200, json={"id": "123456789012345678", "guild_id": "42424242424242424"})


async def _deliver(
    engine: Engine,
    run_id: UUID,
    handler: _Recorder,
    *,
    allowed: set[str] | None = None,
) -> Delivery:
    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10",
        transport=httpx.MockTransport(handler),
    ) as client:
        adapter = DiscordReviewSummaryAdapter(
            token=SecretStr(TOKEN),
            allowed_channel_ids={CHANNEL_ID} if allowed is None else allowed,
            client=client,
        )
        return await deliver_review_summary(
            engine=engine,
            run_id=run_id,
            channel_id=CHANNEL_ID,
            repository=REPOSITORY,
            head_sha=HEAD_SHA,
            risk="high",
            status="attention",
            finding_counts=FINDING_COUNTS,
            report_artifact_key="report/" + "b" * 32,
            adapter=adapter,
        )


def _stored_delivery(engine: Engine) -> Delivery:
    with Session(engine) as session:
        delivery = session.scalar(select(Delivery).where(Delivery.idempotency_key == KEY))
    assert delivery is not None
    return delivery


async def test_first_delivery_posts_once_and_records_sent(engine: Engine, run_id: UUID) -> None:
    handler = _Recorder(_ok)
    delivery = await _deliver(engine, run_id, handler)

    assert len(handler.requests) == 1
    assert delivery.status == DeliveryStatus.SENT
    assert delivery.attempt_count == 1
    assert delivery.external_url == (
        f"https://discord.com/channels/42424242424242424/{CHANNEL_ID}/123456789012345678"
    )

    request = handler.requests[0]
    assert request.url.path == f"/api/v10/channels/{CHANNEL_ID}/messages"
    body = json.loads(request.content)
    assert body["nonce"] == str(delivery.id)
    assert body["enforce_nonce"] is True
    assert body["allowed_mentions"] == {"parse": []}
    assert TOKEN not in request.content.decode()


async def test_replay_with_same_repo_and_sha_does_not_repost(engine: Engine, run_id: UUID) -> None:
    first_handler = _Recorder(_ok)
    first = await _deliver(engine, run_id, first_handler)

    second_handler = _Recorder(_ok)
    second = await _deliver(engine, run_id, second_handler)

    assert len(first_handler.requests) == 1
    assert len(second_handler.requests) == 0
    assert first.id == second.id
    assert second.status == DeliveryStatus.SENT
    assert second.attempt_count == 1


async def test_posted_body_carries_counts_and_no_finding_text(engine: Engine, run_id: UUID) -> None:
    handler = _Recorder(_ok)
    delivery = await _deliver(engine, run_id, handler)

    content = json.loads(handler.requests[0].content)["content"]
    assert "block=2" in content
    assert "important=1" in content
    assert "suggestion=5" in content
    assert str(delivery.id) not in content
    for leak in ("title", "explanation", "patch", "@@", "def ", "Secret"):
        assert leak not in content


async def test_authorization_failure_records_failed_without_retry(
    engine: Engine, run_id: UUID
) -> None:
    handler = _Recorder(lambda _: httpx.Response(401))

    with pytest.raises(LifeAgentError) as raised:
        await _deliver(engine, run_id, handler)

    assert raised.value.record.category is ErrorCategory.AUTHORIZATION
    assert len(handler.requests) == 1
    assert TOKEN not in str(raised.value)

    delivery = _stored_delivery(engine)
    assert delivery.status == DeliveryStatus.FAILED
    assert delivery.error_code == "authorization_invalid"


async def test_server_error_records_uncertain(engine: Engine, run_id: UUID) -> None:
    handler = _Recorder(lambda _: httpx.Response(503))

    with pytest.raises(LifeAgentError) as raised:
        await _deliver(engine, run_id, handler)

    assert raised.value.record.category is ErrorCategory.TRANSIENT
    delivery = _stored_delivery(engine)
    assert delivery.status == DeliveryStatus.UNCERTAIN
    assert delivery.error_code == "delivery_uncertain"


async def test_transport_failure_records_uncertain(engine: Engine, run_id: UUID) -> None:
    def boom(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    handler = _Recorder(boom)

    with pytest.raises(LifeAgentError):
        await _deliver(engine, run_id, handler)

    assert _stored_delivery(engine).status == DeliveryStatus.UNCERTAIN


async def test_non_allowlisted_channel_is_rejected_before_any_http_call(
    engine: Engine, run_id: UUID
) -> None:
    handler = _Recorder(_ok)

    with pytest.raises(ValueError, match="allowlisted"):
        await _deliver(engine, run_id, handler, allowed={OTHER_CHANNEL_ID})

    assert len(handler.requests) == 0
    delivery = _stored_delivery(engine)
    assert delivery.status == DeliveryStatus.FAILED
    assert delivery.error_code == "input_invalid"


def test_review_summary_rejects_unknown_finding_keys() -> None:
    with pytest.raises(ValueError, match="taxonomy"):
        ReviewSummary(
            delivery_id=uuid4(),
            run_id=uuid4(),
            channel_id=CHANNEL_ID,
            repository=REPOSITORY,
            head_sha=HEAD_SHA,
            risk="low",
            status="succeeded",
            finding_counts={"block": 1, "nitpick": 3},
            report_artifact_key=None,
        )


async def test_daily_report_delivery_is_idempotent_and_persists_receipt(
    engine: Engine, run_id: UUID
) -> None:
    handler = _Recorder(_ok)
    key = "code-review-daily:2026-09-03:v1"
    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10",
        transport=httpx.MockTransport(handler),
    ) as client:
        adapter = DiscordDailyReviewAdapter(
            token=SecretStr(TOKEN),
            allowed_channel_ids={CHANNEL_ID},
            client=client,
        )
        first = await deliver_daily_review_report(
            engine=engine,
            run_id=run_id,
            channel_id=CHANNEL_ID,
            report_date=date(2026, 9, 3),
            report_artifact_key="d" * 64,
            idempotency_key=key,
            adapter=adapter,
        )
        replay = await deliver_daily_review_report(
            engine=engine,
            run_id=run_id,
            channel_id=CHANNEL_ID,
            report_date=date(2026, 9, 3),
            report_artifact_key="d" * 64,
            idempotency_key=key,
            adapter=adapter,
        )

    assert len(handler.requests) == 1
    assert replay.id == first.id
    assert first.status == DeliveryStatus.SENT
    assert first.external_url is not None
    content = json.loads(handler.requests[0].content)["content"]
    assert "2026-09-03" in content
    assert "d" * 64 in content
