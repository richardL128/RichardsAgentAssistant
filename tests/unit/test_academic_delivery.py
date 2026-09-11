from __future__ import annotations

import json
from collections.abc import Iterator
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.agents.academic_planner.contracts import MorningBriefing
from app.connectors.discord import (
    AcademicDiscordMessage,
    DiscordAcademicPlannerAdapter,
    DiscordAcademicPlannerDelivery,
    DiscordAcademicProgressEvent,
    DiscordAcademicResponseDelivery,
)
from app.core.errors import ErrorCategory, ErrorCode, LifeAgentError
from app.db.models import AgentRun, Delivery, DeliveryStatus
from app.db.repositories import RunRepository

CHANNEL = "987654321012345678"


async def _record_sleep(sleeps: list[float], seconds: float) -> None:
    sleeps.append(seconds)


def test_morning_briefing_contract_enforces_discord_content_limit() -> None:
    with pytest.raises(ValidationError):
        MorningBriefing(message_text="x" * 2_001)


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
    key = "academic-plan:2026-09-03:v2"
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
        delivery = DiscordAcademicPlannerDelivery(
            engine=engine,
            run_id=run_id,
            channel_id=CHANNEL,
            adapter=adapter,
        )
        first = await delivery.send_morning_plan(
            MorningBriefing(
                message_text=(
                    "Good morning, Richard. Review calculus for 30 minutes. "
                    "@everyone should remain inert. Have a good day!"
                ),
                referenced_block_ids=("block-calculus",),
            ),
            idempotency_key=key,
        )
        second = await delivery.send_morning_plan(
            MorningBriefing(message_text="This must not be sent again."),
            idempotency_key=key,
        )

    assert len(calls) == 1
    body = json.loads(calls[0].read())
    assert body["content"].startswith("Good morning, Richard.")
    assert "Today's academic plan:" not in body["content"]
    assert len(body["content"]) <= 2_000
    assert body["nonce"]
    assert len(body["nonce"]) <= 25
    assert body["enforce_nonce"] is True
    assert body["allowed_mentions"] == {"parse": []}
    assert first.id == second.id
    with Session(engine) as session:
        stored = session.scalar(select(Delivery).where(Delivery.id == first.id))
        assert stored is not None
        assert stored.status == DeliveryStatus.SENT.value


@pytest.mark.asyncio
async def test_scheduled_academic_notification_delivers_exact_content_once(
    engine: Engine,
) -> None:
    key = "academic-morning:2026-09-10:0800:v1"
    content = (
        "Good morning, Richard. Today's plan:\n"
        "- 09:00 - ECE 250 Quiz 1 review (45 minutes)\n"
        "@everyone should remain inert.\n"
        "Have a good day!"
    )
    with Session(engine) as session, session.begin():
        run = RunRepository.create_or_get(
            session,
            idempotency_key=key,
            agent_name="academic_planner",
            trigger="schedule",
            schedule="academic_morning",
        )
        run_id = run.id
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"id": "123456789012345678", "guild_id": "42"})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        delivery = DiscordAcademicPlannerDelivery(
            engine=engine,
            run_id=run_id,
            channel_id=CHANNEL,
            adapter=DiscordAcademicPlannerAdapter(
                token=SecretStr("academic-token"),
                allowed_channel_ids={CHANNEL},
                client=client,
            ),
        )
        first = await delivery.send_scheduled_notification(content, idempotency_key=key)
        second = await delivery.send_scheduled_notification(
            "This replay must not be posted.",
            idempotency_key=key,
        )

    assert len(calls) == 1
    assert first.id == second.id
    body = json.loads(calls[0].content)
    assert body["content"] == content
    assert body["nonce"] == first.id.hex[:25]
    assert body["enforce_nonce"] is True
    assert body["allowed_mentions"] == {"parse": []}
    with Session(engine) as session:
        stored = session.scalar(select(Delivery).where(Delivery.id == first.id))
        assert stored is not None
        assert stored.status == DeliveryStatus.SENT.value
        assert stored.error_code is None


@pytest.mark.asyncio
async def test_scheduled_academic_notification_replay_after_transport_uncertainty(
    engine: Engine,
) -> None:
    key = "academic-morning:2026-09-10:0800:v1"
    content = "Good morning, Richard. It is a light academic day. Have a good day!"
    with Session(engine) as session, session.begin():
        run = RunRepository.create_or_get(
            session,
            idempotency_key=key,
            agent_name="academic_planner",
            trigger="schedule",
            schedule="academic_morning",
        )
        run_id = run.id
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("connection reset", request=request)
        return httpx.Response(200, json={"id": "123456789012345678", "guild_id": "42"})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        delivery = DiscordAcademicPlannerDelivery(
            engine=engine,
            run_id=run_id,
            channel_id=CHANNEL,
            adapter=DiscordAcademicPlannerAdapter(
                token=SecretStr("academic-token"),
                allowed_channel_ids={CHANNEL},
                client=client,
            ),
        )
        with pytest.raises(LifeAgentError) as raised:
            await delivery.send_scheduled_notification(content, idempotency_key=key)

        assert raised.value.record.category is ErrorCategory.TRANSIENT
        with Session(engine) as session:
            uncertain = session.scalar(select(Delivery).where(Delivery.idempotency_key == key))
            assert uncertain is not None
            assert uncertain.status == DeliveryStatus.UNCERTAIN.value
            assert uncertain.attempt_count == 1
            assert uncertain.error_code == ErrorCode.DELIVERY_UNCERTAIN.value
            delivery_id = uncertain.id

        final = await delivery.send_scheduled_notification(content, idempotency_key=key)

    assert final.id == delivery_id
    assert len(calls) == 2
    first_body = json.loads(calls[0].content)
    second_body = json.loads(calls[1].content)
    assert first_body == second_body
    assert first_body["content"] == content
    assert first_body["nonce"] == delivery_id.hex[:25]
    assert first_body["enforce_nonce"] is True
    assert first_body["allowed_mentions"] == {"parse": []}
    with Session(engine) as session:
        deliveries = list(session.scalars(select(Delivery).where(Delivery.idempotency_key == key)))
        assert len(deliveries) == 1
        assert deliveries[0].status == DeliveryStatus.SENT.value
        assert deliveries[0].attempt_count == 2
        assert deliveries[0].error_code is None


@pytest.mark.asyncio
async def test_academic_delivery_retries_429_with_same_nonce_and_content() -> None:
    calls: list[httpx.Request] = []
    sleeps: list[float] = []
    delivery_id = uuid4()
    content = "Final proposal preview."

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "0.25"},
                json={"retry_after": 99, "secret": "not exposed"},
            )
        return httpx.Response(200, json={"id": "123456789012345678", "guild_id": "42"})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        adapter = DiscordAcademicPlannerAdapter(
            token=SecretStr("academic-token"),
            allowed_channel_ids={CHANNEL},
            client=client,
            rate_limit_sleep=lambda seconds: _record_sleep(sleeps, seconds),
        )
        receipt = await adapter.send(
            AcademicDiscordMessage(
                delivery_id=delivery_id,
                channel_id=CHANNEL,
                content=content,
            )
        )

    assert receipt.external_id == "123456789012345678"
    assert sleeps == [0.25]
    assert len(calls) == 2
    first_body = json.loads(calls[0].content)
    second_body = json.loads(calls[1].content)
    assert first_body == second_body
    assert first_body["content"] == content
    assert first_body["nonce"] == delivery_id.hex[:25]
    assert first_body["enforce_nonce"] is True
    assert first_body["allowed_mentions"] == {"parse": []}


@pytest.mark.asyncio
async def test_academic_delivery_uses_json_retry_after_when_header_is_missing() -> None:
    calls: list[httpx.Request] = []
    sleeps: list[float] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, json={"retry_after": 0.1})
        return httpx.Response(200, json={"id": "123456789012345678"})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        adapter = DiscordAcademicPlannerAdapter(
            token=SecretStr("academic-token"),
            allowed_channel_ids={CHANNEL},
            client=client,
            rate_limit_sleep=lambda seconds: _record_sleep(sleeps, seconds),
        )
        await adapter.send(
            AcademicDiscordMessage(
                delivery_id=uuid4(),
                channel_id=CHANNEL,
                content="Final response.",
            )
        )

    assert sleeps == [0.1]
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(429, headers={"Retry-After": "not-a-number"}, json={"secret": "body"}),
        httpx.Response(429, headers={"Retry-After": "6"}, json={"secret": "body"}),
        httpx.Response(429, json={"retry_after": -1, "secret": "body"}),
        httpx.Response(429, json={"retry_after": "nan", "secret": "body"}),
    ],
)
async def test_academic_delivery_rate_limit_delay_must_be_bounded_and_valid(
    response: httpx.Response,
) -> None:
    calls: list[httpx.Request] = []
    sleeps: list[float] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        adapter = DiscordAcademicPlannerAdapter(
            token=SecretStr("academic-token"),
            allowed_channel_ids={CHANNEL},
            client=client,
            rate_limit_sleep=lambda seconds: _record_sleep(sleeps, seconds),
        )
        with pytest.raises(LifeAgentError) as raised:
            await adapter.send(
                AcademicDiscordMessage(
                    delivery_id=uuid4(),
                    channel_id=CHANNEL,
                    content="Final response.",
                )
            )

    assert raised.value.record.category is ErrorCategory.TRANSIENT
    assert raised.value.record.code is ErrorCode.CONNECTOR_TRANSIENT
    assert "secret" not in str(raised.value)
    assert sleeps == []
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_academic_delivery_exhausted_429_remains_transient() -> None:
    calls: list[httpx.Request] = []
    sleeps: list[float] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "0.1"}, json={"secret": "body"})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        adapter = DiscordAcademicPlannerAdapter(
            token=SecretStr("academic-token"),
            allowed_channel_ids={CHANNEL},
            client=client,
            rate_limit_sleep=lambda seconds: _record_sleep(sleeps, seconds),
        )
        with pytest.raises(LifeAgentError) as raised:
            await adapter.send(
                AcademicDiscordMessage(
                    delivery_id=uuid4(),
                    channel_id=CHANNEL,
                    content="Final response.",
                )
            )

    assert raised.value.record.category is ErrorCategory.TRANSIENT
    assert raised.value.record.code is ErrorCode.CONNECTOR_TRANSIENT
    assert "secret" not in str(raised.value)
    assert sleeps == [0.1, 0.1]
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_academic_delivery_does_not_retry_ambiguous_5xx() -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503, json={"secret": "body"})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        adapter = DiscordAcademicPlannerAdapter(
            token=SecretStr("academic-token"),
            allowed_channel_ids={CHANNEL},
            client=client,
            rate_limit_sleep=lambda _seconds: _record_sleep([], _seconds),
        )
        with pytest.raises(LifeAgentError) as raised:
            await adapter.send(
                AcademicDiscordMessage(
                    delivery_id=uuid4(),
                    channel_id=CHANNEL,
                    content="Final response.",
                )
            )

    assert raised.value.record.category is ErrorCategory.TRANSIENT
    assert raised.value.record.code is ErrorCode.CONNECTOR_TRANSIENT
    assert "secret" not in str(raised.value)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_academic_message_edit_is_bounded_and_mention_safe() -> None:
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
        receipt = await adapter.edit_academic_message(
            channel_id=CHANNEL,
            message_id="123456789012345678",
            content=("@everyone " + "x" * 2_100),
        )

    assert receipt.external_id == "123456789012345678"
    assert len(calls) == 1
    assert calls[0].method == "PATCH"
    assert calls[0].url.path == f"/api/v10/channels/{CHANNEL}/messages/123456789012345678"
    body = json.loads(calls[0].content)
    assert body["content"].startswith("@everyone ")
    assert len(body["content"]) <= 2_000
    assert body["content"].endswith("[truncated]")
    assert body["allowed_mentions"] == {"parse": []}
    assert body["components"] == []


@pytest.mark.asyncio
async def test_academic_message_edit_validates_target_and_message_id() -> None:
    adapter = DiscordAcademicPlannerAdapter(
        token=SecretStr("academic-token"),
        allowed_channel_ids={CHANNEL},
    )

    with pytest.raises(ValueError, match="allowlisted"):
        await adapter.edit_academic_message(
            channel_id="111111111111111111",
            message_id="123456789012345678",
            content="Progress.",
        )
    with pytest.raises(ValueError, match="message id"):
        await adapter.edit_academic_message(
            channel_id=CHANNEL,
            message_id="not-a-message-id",
            content="Progress.",
        )
    with pytest.raises(ValueError, match="content"):
        await adapter.edit_academic_message(
            channel_id=CHANNEL,
            message_id="123456789012345678",
            content="",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "category", "code"),
    [
        (401, ErrorCategory.AUTHORIZATION, ErrorCode.AUTHORIZATION_INVALID),
        (429, ErrorCategory.TRANSIENT, ErrorCode.CONNECTOR_TRANSIENT),
        (500, ErrorCategory.TRANSIENT, ErrorCode.CONNECTOR_TRANSIENT),
        (400, ErrorCategory.PERMANENT, ErrorCode.INPUT_INVALID),
    ],
)
async def test_academic_message_edit_classifies_failures_without_body_leak(
    status_code: int,
    category: ErrorCategory,
    code: ErrorCode,
) -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "secret response body"})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        adapter = DiscordAcademicPlannerAdapter(
            token=SecretStr("academic-token"),
            allowed_channel_ids={CHANNEL},
            client=client,
        )
        with pytest.raises(LifeAgentError) as raised:
            await adapter.edit_academic_message(
                channel_id=CHANNEL,
                message_id="123456789012345678",
                content="Progress.",
            )

    assert raised.value.record.category is category
    assert raised.value.record.code is code
    assert "secret response body" not in raised.value.record.diagnostic
    assert "secret response body" not in str(raised.value)


@pytest.mark.asyncio
async def test_academic_message_edit_rejects_invalid_receipt_without_body_leak() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "not-a-discord-id", "secret": "token"})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        adapter = DiscordAcademicPlannerAdapter(
            token=SecretStr("academic-token"),
            allowed_channel_ids={CHANNEL},
            client=client,
        )
        with pytest.raises(LifeAgentError) as raised:
            await adapter.edit_academic_message(
                channel_id=CHANNEL,
                message_id="123456789012345678",
                content="Progress.",
            )

    assert raised.value.record.category is ErrorCategory.TRANSIENT
    assert raised.value.record.code is ErrorCode.CONNECTOR_TRANSIENT
    assert "token" not in raised.value.record.diagnostic


@pytest.mark.asyncio
async def test_progress_reporter_posts_once_and_edits_ordered_coalesced_stages(
    engine: Engine,
) -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"id": "123456789012345678", "guild_id": "42"})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        delivery = DiscordAcademicResponseDelivery(
            engine=engine,
            channel_id=CHANNEL,
            adapter=DiscordAcademicPlannerAdapter(
                token=SecretStr("academic-token"),
                allowed_channel_ids={CHANNEL},
                client=client,
            ),
        )
        reporter = delivery.create_progress_reporter(
            root_event_id="333333333333333333",
            edit_every_n_updates=2,
        )
        first_handle = await reporter.start()
        assert await reporter.start() == first_handle
        replay = delivery.create_progress_reporter(root_event_id="333333333333333333")
        replay_handle = await replay.start()
        await reporter.update(
            DiscordAcademicProgressEvent(
                phase="model_turn",
                model_turn_number=1,
                model_turn_limit=10,
            )
        )
        await reporter.update(
            DiscordAcademicProgressEvent(
                phase="model_turn",
                model_turn_number=1,
                model_turn_limit=10,
            )
        )
        await reporter.update(
            {
                "phase": "course_lookup",
                "lookup_kind": "course",
                "result_count": 2,
                "query": "private course name",
            }
        )
        await reporter.update({"phase": "proposal_validation"})
        await reporter.finish_proposal_ready()

    assert first_handle is not None
    assert replay_handle is not None
    assert replay_handle.message_id == first_handle.message_id
    assert [request.method for request in calls] == ["POST", "PATCH", "PATCH"]
    post_body = json.loads(calls[0].content)
    assert post_body["content"] == "- Waking Qwen."
    assert post_body["nonce"]
    assert len(post_body["nonce"]) <= 25
    assert post_body["enforce_nonce"] is True
    assert post_body["allowed_mentions"] == {"parse": []}

    first_patch = json.loads(calls[1].content)["content"]
    assert first_patch.splitlines() == [
        "- Waking Qwen.",
        "- Qwen is interpreting your request (agent turn 1 of 10).",
        "- Looking up matching courses. (2 results.)",
    ]
    final_patch = json.loads(calls[2].content)["content"]
    assert final_patch.endswith("- Proposal ready.")
    assert "private course name" not in final_patch


@pytest.mark.asyncio
async def test_progress_reporter_adopts_host_ack_without_second_post(engine: Engine) -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"id": "123456789012345678", "guild_id": "42"})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        delivery = DiscordAcademicResponseDelivery(
            engine=engine,
            channel_id=CHANNEL,
            adapter=DiscordAcademicPlannerAdapter(
                token=SecretStr("academic-token"),
                allowed_channel_ids={CHANNEL},
                client=client,
            ),
        )
        reporter = delivery.create_progress_reporter(
            root_event_id="666666666666666666",
            existing_message_id="123456789012345678",
        )
        handle = await reporter.start()
        await reporter.update(
            DiscordAcademicProgressEvent(
                phase="model_turn",
                model_turn_number=1,
                model_turn_limit=10,
            )
        )

    assert handle is not None
    assert handle.message_id == "123456789012345678"
    assert [request.method for request in calls] == ["PATCH", "PATCH"]
    assert all(
        request.url.path == f"/api/v10/channels/{CHANNEL}/messages/123456789012345678"
        for request in calls
    )
    with Session(engine) as session:
        stored = list(session.scalars(select(Delivery)))
    assert len(stored) == 1
    assert stored[0].status == DeliveryStatus.SENT.value


@pytest.mark.asyncio
async def test_progress_failures_do_not_block_final_academic_response(engine: Engine) -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "PATCH":
            return httpx.Response(500, json={"error": "secret patch failure"})
        return httpx.Response(200, json={"id": str(123456789012345678 + len(calls))})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        delivery = DiscordAcademicResponseDelivery(
            engine=engine,
            channel_id=CHANNEL,
            adapter=DiscordAcademicPlannerAdapter(
                token=SecretStr("academic-token"),
                allowed_channel_ids={CHANNEL},
                client=client,
            ),
        )
        reporter = delivery.create_progress_reporter(root_event_id="444444444444444444")
        assert await reporter.start() is not None
        await reporter.update({"phase": "model_turn", "model_turn_number": 1})
        await reporter.finish_proposal_ready()
        final = await delivery.send_response(
            "Final proposal preview.",
            idempotency_key="academic-discord-message:444444444444444444:proposal:v1",
        )

    assert final.status == DeliveryStatus.SENT.value
    assert [request.method for request in calls] == ["POST", "PATCH", "POST"]
    assert "Final proposal preview." in json.loads(calls[2].content)["content"]


@pytest.mark.asyncio
async def test_progress_post_failure_is_best_effort(engine: Engine) -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(500, json={"error": "secret post failure"})
        return httpx.Response(200, json={"id": "123456789012345678"})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        delivery = DiscordAcademicResponseDelivery(
            engine=engine,
            channel_id=CHANNEL,
            adapter=DiscordAcademicPlannerAdapter(
                token=SecretStr("academic-token"),
                allowed_channel_ids={CHANNEL},
                client=client,
            ),
        )
        reporter = delivery.create_progress_reporter(root_event_id="555555555555555555")
        assert await reporter.start() is None
        await reporter.update({"phase": "model_turn", "model_turn_number": 1})
        final = await delivery.send_response(
            "Final clarification question.",
            idempotency_key="academic-discord-message:555555555555555555:proposal:v1",
        )

    assert final.status == DeliveryStatus.SENT.value
    assert [request.method for request in calls] == ["POST", "POST"]
    with Session(engine) as session:
        deliveries = list(session.scalars(select(Delivery).order_by(Delivery.created_at)))
    assert [item.status for item in deliveries] == [
        DeliveryStatus.UNCERTAIN.value,
        DeliveryStatus.SENT.value,
    ]
