from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.agents.academic_planner.nightly_checkin import (
    NIGHTLY_CHECKIN_KIND,
    NightlyCheckinConfig,
    NightlyCheckinRuntime,
    execute_nightly_checkin,
    nightly_delivery_key,
    nightly_period_key,
    render_nightly_checkin_prompt,
)
from app.core.errors import LifeAgentError
from app.queue.periodic import PeriodicOccurrence

OCCURRENCE = PeriodicOccurrence(
    local_time=datetime(2026, 9, 19, 21, 0, tzinfo=ZoneInfo("America/Toronto")),
    scheduled_at=datetime(2026, 9, 20, 1, 0, tzinfo=UTC),
)
PERIOD_KEY = nightly_period_key(OCCURRENCE)
CHANNEL = "222222222222222222"
OWNER = "333333333333333333"


class Delivery:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def send_scheduled_notification(self, content: str, *, idempotency_key: str):
        self.messages.append((content, idempotency_key))
        return SimpleNamespace(status="sent")


class DurableDelivery(Delivery):
    def __init__(self) -> None:
        super().__init__()
        self.delivered_keys: set[str] = set()
        self.external_send_count = 0

    async def send_scheduled_notification(self, content: str, *, idempotency_key: str):
        self.messages.append((content, idempotency_key))
        if idempotency_key not in self.delivered_keys:
            self.delivered_keys.add(idempotency_key)
            self.external_send_count += 1
        return SimpleNamespace(status="sent")


class ConversationService:
    def __init__(self, *, open_result: object | None = None) -> None:
        self.open_result = open_result or SimpleNamespace(status="no_open", state=None)
        self.opened: list[dict[str, object]] = []

    def inspect_open(
        self,
        *,
        discord_channel_id: str,
        owner_discord_user_id: str,
        now: datetime | None = None,
    ):
        assert discord_channel_id == CHANNEL
        assert owner_discord_user_id == OWNER
        assert now is not None
        return self.open_result

    def open_proactive_prompt(self, **kwargs: object):
        self.opened.append(kwargs)
        return SimpleNamespace(status="started", state="awaiting_user")


class MissingOpenConversationService:
    def inspect_open(self, **kwargs: object):
        del kwargs
        return SimpleNamespace(status="no_open", state=None)


class CrashAfterDeliveryConversationService(ConversationService):
    def __init__(self) -> None:
        super().__init__()
        self.open_attempts = 0

    def open_proactive_prompt(self, **kwargs: object):
        self.open_attempts += 1
        if self.open_attempts == 1:
            raise RuntimeError("simulated crash after durable delivery")
        return super().open_proactive_prompt(**kwargs)


def _runtime(
    *,
    delivery: Delivery | None = None,
    conversation_service: object | None = None,
    owner: str | None = OWNER,
    authorized: frozenset[str] = frozenset({OWNER}),
    message_content_enabled: bool = True,
) -> NightlyCheckinRuntime:
    return NightlyCheckinRuntime(
        config=NightlyCheckinConfig(
            channel_id=CHANNEL,
            proactive_owner_id=owner,
            authorized_user_ids=authorized,
            message_content_enabled=message_content_enabled,
            discord_delivery_enabled=delivery is not None,
            model_identity="qwen-test",
            prompt_config_version="prompt-test",
            session_ttl_hours=48,
            catchup_grace_minutes=30,
            timezone_name="America/Toronto",
        ),
        delivery=delivery,
        conversation_service=conversation_service or ConversationService(),
    )


def test_prompt_discloses_memory_scope_confirmation_and_skip() -> None:
    prompt = render_nightly_checkin_prompt(OCCURRENCE)

    assert "Evening check-in for Saturday, September 19" in prompt
    assert "study-related reflections" in prompt
    assert "private academic memory" in prompt
    assert "Calendar changes still require your confirmation" in prompt
    assert "skip" in prompt
    assert len(prompt) <= 2_000


@pytest.mark.asyncio
async def test_nightly_checkin_delivers_once_and_opens_proactive_session() -> None:
    delivery = Delivery()
    conversation = ConversationService()

    result = await execute_nightly_checkin(
        runtime=_runtime(delivery=delivery, conversation_service=conversation),
        occurrence=OCCURRENCE,
        period_key=PERIOD_KEY,
        executed_at=OCCURRENCE.scheduled_at + timedelta(minutes=2),
        catchup_grace_minutes=30,
    )

    assert result["status"] == "succeeded"
    assert result["period_key"] == PERIOD_KEY
    assert len(delivery.messages) == 1
    assert delivery.messages[0][1] == nightly_delivery_key(PERIOD_KEY, OCCURRENCE)
    assert len(conversation.opened) == 1
    opened = conversation.opened[0]
    assert opened["root_event_id"] == PERIOD_KEY
    assert opened["discord_channel_id"] == CHANNEL
    assert opened["owner_discord_user_id"] == OWNER
    assert opened["proactive_kind"] == NIGHTLY_CHECKIN_KIND
    assert opened["proactive_period"] == PERIOD_KEY
    assert opened["expires_at"] == OCCURRENCE.scheduled_at + timedelta(minutes=2, hours=24)


@pytest.mark.asyncio
async def test_existing_same_period_session_is_replay_safe_without_redelivery() -> None:
    delivery = Delivery()
    conversation = ConversationService(
        open_result=SimpleNamespace(
            status="resumed",
            state="awaiting_user",
            root_event_id=PERIOD_KEY,
        )
    )

    result = await execute_nightly_checkin(
        runtime=_runtime(delivery=delivery, conversation_service=conversation),
        occurrence=OCCURRENCE,
        period_key=PERIOD_KEY,
        executed_at=OCCURRENCE.scheduled_at + timedelta(minutes=5),
        catchup_grace_minutes=30,
    )

    assert result["status"] == "succeeded"
    assert result["conversation_status"] == "already_open"
    assert result["delivery_count"] == 1
    assert result["replayed"] is True
    assert delivery.messages == []
    assert conversation.opened == []


@pytest.mark.asyncio
async def test_retry_after_persisted_delivery_opens_session_without_duplicate_send() -> None:
    delivery = DurableDelivery()
    conversation = CrashAfterDeliveryConversationService()
    runtime = _runtime(delivery=delivery, conversation_service=conversation)

    with pytest.raises(RuntimeError, match="simulated crash"):
        await execute_nightly_checkin(
            runtime=runtime,
            occurrence=OCCURRENCE,
            period_key=PERIOD_KEY,
            executed_at=OCCURRENCE.scheduled_at + timedelta(minutes=2),
            catchup_grace_minutes=30,
        )

    result = await execute_nightly_checkin(
        runtime=runtime,
        occurrence=OCCURRENCE,
        period_key=PERIOD_KEY,
        executed_at=OCCURRENCE.scheduled_at + timedelta(minutes=3),
        catchup_grace_minutes=30,
    )

    assert result["status"] == "succeeded"
    assert delivery.external_send_count == 1
    assert len(delivery.messages) == 2
    assert conversation.open_attempts == 2
    assert len(conversation.opened) == 1


@pytest.mark.asyncio
async def test_busy_conversation_retries_inside_grace_without_sending() -> None:
    delivery = Delivery()
    conversation = ConversationService(
        open_result=SimpleNamespace(
            status="resumed",
            state="awaiting_user",
            root_event_id="discord:other",
        )
    )

    with pytest.raises(LifeAgentError) as exc:
        await execute_nightly_checkin(
            runtime=_runtime(delivery=delivery, conversation_service=conversation),
            occurrence=OCCURRENCE,
            period_key=PERIOD_KEY,
            executed_at=OCCURRENCE.scheduled_at + timedelta(minutes=10),
            catchup_grace_minutes=30,
        )

    assert exc.value.record.diagnostic == "native_conversation_busy"
    assert delivery.messages == []
    assert conversation.opened == []


@pytest.mark.asyncio
async def test_stale_occurrence_reports_attention_without_sending() -> None:
    delivery = Delivery()

    result = await execute_nightly_checkin(
        runtime=_runtime(delivery=delivery),
        occurrence=OCCURRENCE,
        period_key=PERIOD_KEY,
        executed_at=OCCURRENCE.scheduled_at + timedelta(minutes=31),
        catchup_grace_minutes=30,
    )

    assert result == {
        "status": "attention",
        "error_code": "schedule_late",
        "delivery_count": 0,
        "conversation_status": "stale_not_opened",
    }
    assert delivery.messages == []


@pytest.mark.asyncio
async def test_configuration_fails_closed_for_missing_or_unauthorized_owner() -> None:
    delivery = Delivery()

    missing = await execute_nightly_checkin(
        runtime=_runtime(delivery=delivery, owner=None),
        occurrence=OCCURRENCE,
        period_key=PERIOD_KEY,
        executed_at=OCCURRENCE.scheduled_at,
        catchup_grace_minutes=30,
    )
    unauthorized = await execute_nightly_checkin(
        runtime=_runtime(delivery=delivery, authorized=frozenset({"444444444444444444"})),
        occurrence=OCCURRENCE,
        period_key=PERIOD_KEY,
        executed_at=OCCURRENCE.scheduled_at,
        catchup_grace_minutes=30,
    )
    no_content = await execute_nightly_checkin(
        runtime=_runtime(delivery=delivery, message_content_enabled=False),
        occurrence=OCCURRENCE,
        period_key=PERIOD_KEY,
        executed_at=OCCURRENCE.scheduled_at,
        catchup_grace_minutes=30,
    )

    assert missing["error_code"] == "authorization_invalid"
    assert unauthorized["error_code"] == "authorization_invalid"
    assert no_content["error_code"] == "authorization_invalid"
    assert delivery.messages == []


@pytest.mark.asyncio
async def test_missing_proactive_api_fails_before_delivery() -> None:
    delivery = Delivery()

    result = await execute_nightly_checkin(
        runtime=_runtime(
            delivery=delivery,
            conversation_service=MissingOpenConversationService(),
        ),
        occurrence=OCCURRENCE,
        period_key=PERIOD_KEY,
        executed_at=OCCURRENCE.scheduled_at,
        catchup_grace_minutes=30,
    )

    assert result["status"] == "failed"
    assert result["conversation_status"] == "api_missing"
    assert result["required_api"] == "NativeConversationService.open_proactive_prompt"
    assert delivery.messages == []
