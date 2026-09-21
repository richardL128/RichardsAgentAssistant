from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
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
)
from app.agents.academic_planner.nightly_task_semantics import (
    NightlyTaskEligibilityDecision,
    NightlyTaskEligibilityResult,
    NightlyTaskEligibilityStatus,
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


class FailOnceDelivery(DurableDelivery):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def send_scheduled_notification(self, content: str, *, idempotency_key: str):
        if not self.failed:
            self.failed = True
            raise RuntimeError("simulated Discord timeout")
        return await super().send_scheduled_notification(content, idempotency_key=idempotency_key)


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
        self.open_result = SimpleNamespace(
            status="resumed",
            state="awaiting_user",
            root_event_id=kwargs["root_event_id"],
            checkpoint=kwargs.get("initial_checkpoint", {}),
        )
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


class Syncer:
    async def sync(self, *, now=None):
        return SimpleNamespace(status="succeeded", synced_at=now)


class Catalog:
    def __init__(self, candidates=None) -> None:
        self.candidates = tuple(candidates or (_candidate(),))

    def load_nightly_current_day_assessment_candidates(self, **kwargs):
        assert kwargs["local_date"] == date(2026, 9, 19)
        return self.candidates


class Semantics:
    model_identity = "qwen-test"

    async def analyze(self, item):
        return SimpleNamespace(
            status=NightlyTaskEligibilityStatus.ELIGIBLE,
            movable=True,
            result=NightlyTaskEligibilityResult(
                event_id=item.event_id,
                decision=NightlyTaskEligibilityDecision.MOVABLE_WORK_TASK,
                rationale="This is owner-performable review work.",
                evidence_fragment_ids=(f"{item.event_id}:host:title",),
            ),
            model_identity="qwen-test",
            prompt_version="nightly-eligibility-v1",
            critic_version="nightly-critic-v1",
        )


class FixedSemantics(Semantics):
    async def analyze(self, item):
        return SimpleNamespace(
            status=NightlyTaskEligibilityStatus.NOT_ELIGIBLE,
            movable=False,
            result=NightlyTaskEligibilityResult(
                event_id=item.event_id,
                decision=NightlyTaskEligibilityDecision.FIXED_COMMITMENT,
                rationale="This is a fixed assessment occurrence.",
                evidence_fragment_ids=(f"{item.event_id}:host:title",),
            ),
            model_identity="qwen-test",
            prompt_version="nightly-eligibility-v1",
            critic_version="nightly-critic-v1",
        )


def _candidate():
    return SimpleNamespace(
        assessment_id="assessment-1",
        course_id="course-1",
        course_code="ECE 250",
        course_title="ECE 250",
        title="Review merge sort",
        assessment_type="task",
        starts_at=datetime(2026, 9, 19, 18, 0, tzinfo=ZoneInfo("America/Toronto")),
        ends_at=None,
        local_date=date(2026, 9, 19),
        local_start_label="September 19 at 6:00 PM",
        local_end_label=None,
        is_all_day=False,
        source_last_edited_at=datetime(2026, 9, 19, 20, 0, tzinfo=UTC),
        semantic_status="valid",
        semantic_overview="Review merge sort material.",
        semantic_description=None,
        semantic_intent_rationale="A review work session.",
        semantic_source_fingerprint="semantic-fp",
    )


def _runtime(
    *,
    delivery: Delivery | None = None,
    conversation_service: object | None = None,
    owner: str | None = OWNER,
    authorized: frozenset[str] = frozenset({OWNER}),
    message_content_enabled: bool = True,
    catalog: object | None = None,
    semantics: object | None = None,
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
        catalog_syncer=Syncer(),
        candidate_catalog=catalog or Catalog(),
        semantic_interpreter=semantics or Semantics(),
    )


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
    assert delivery.messages[0][0] == (
        'Evening check-in - ECE 250 (1/1): Did you complete "Review merge sort" today?'
    )
    assert delivery.messages[0][1] == nightly_delivery_key(PERIOD_KEY, OCCURRENCE)
    assert len(conversation.opened) == 1
    opened = conversation.opened[0]
    assert opened["root_event_id"] == PERIOD_KEY
    assert opened["discord_channel_id"] == CHANNEL
    assert opened["owner_discord_user_id"] == OWNER
    assert opened["proactive_kind"] == NIGHTLY_CHECKIN_KIND
    assert opened["proactive_period"] == PERIOD_KEY
    assert opened["initial_checkpoint"]["version"] == "academic-discord-native-tools.v3"
    assert "nightly_checkin" in opened["initial_checkpoint"]
    assert opened["expires_at"] == OCCURRENCE.scheduled_at + timedelta(minutes=2, hours=24)


@pytest.mark.asyncio
async def test_existing_same_period_session_is_replay_safe_without_redelivery() -> None:
    delivery = Delivery()
    first_conversation = ConversationService()
    first = await execute_nightly_checkin(
        runtime=_runtime(delivery=delivery, conversation_service=first_conversation),
        occurrence=OCCURRENCE,
        period_key=PERIOD_KEY,
        executed_at=OCCURRENCE.scheduled_at + timedelta(minutes=2),
        catchup_grace_minutes=30,
    )
    assert first["status"] == "succeeded"
    checkpoint = first_conversation.opened[0]["initial_checkpoint"]
    delivery.messages.clear()
    conversation = ConversationService(
        open_result=SimpleNamespace(
            status="resumed",
            state="awaiting_user",
            root_event_id=PERIOD_KEY,
            checkpoint=checkpoint,
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
    assert len(delivery.messages) == 1
    assert conversation.opened == []


@pytest.mark.asyncio
async def test_retry_after_failed_open_does_not_send_before_checkpoint_exists() -> None:
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
    assert len(delivery.messages) == 1
    assert conversation.open_attempts == 2
    assert len(conversation.opened) == 1


@pytest.mark.asyncio
async def test_retry_after_delivery_timeout_reuses_atomic_checkpoint_and_delivery_key() -> None:
    delivery = FailOnceDelivery()
    conversation = ConversationService()
    runtime = _runtime(delivery=delivery, conversation_service=conversation)

    with pytest.raises(RuntimeError, match="Discord timeout"):
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
    assert result["replayed"] is True
    assert len(conversation.opened) == 1
    assert delivery.external_send_count == 1


@pytest.mark.asyncio
async def test_no_movable_tasks_sends_terminal_message_without_open_session() -> None:
    delivery = Delivery()
    conversation = ConversationService()

    result = await execute_nightly_checkin(
        runtime=_runtime(
            delivery=delivery,
            conversation_service=conversation,
            semantics=FixedSemantics(),
        ),
        occurrence=OCCURRENCE,
        period_key=PERIOD_KEY,
        executed_at=OCCURRENCE.scheduled_at + timedelta(minutes=2),
        catchup_grace_minutes=30,
    )

    assert result["conversation_status"] == "not_opened_no_tasks"
    assert delivery.messages == [
        (
            "No movable course tasks are scheduled for tonight's check-in.",
            nightly_delivery_key(PERIOD_KEY, OCCURRENCE),
        )
    ]
    assert conversation.opened == []


@pytest.mark.asyncio
async def test_missing_nightly_dependencies_fail_closed_and_message_on_last_attempt() -> None:
    delivery = Delivery()
    runtime = _runtime(delivery=delivery)
    runtime.semantic_interpreter = None

    result = await execute_nightly_checkin(
        runtime=runtime,
        occurrence=OCCURRENCE,
        period_key=PERIOD_KEY,
        executed_at=OCCURRENCE.scheduled_at + timedelta(minutes=2),
        catchup_grace_minutes=30,
        attempt=3,
        attempt_limit=3,
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "nightly_dependencies_unavailable"
    assert delivery.messages[0][0].endswith("Nothing was changed.")


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
