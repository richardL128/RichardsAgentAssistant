from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Literal
from uuid import UUID

import pytest

from app.agents.academic_planner.contracts import (
    Assessment,
    AssessmentType,
    AvailabilityWindow,
    DailyPlan,
    IncompleteBlock,
    PlannerFacts,
    PracticeNeed,
    StudyBlock,
)
from app.agents.academic_planner.morning_notification import (
    build_scheduled_morning_notification,
    execute_scheduled_morning_notification,
    scheduled_delivery_key,
)
from app.agents.academic_planner.sync import AcademicNotionSync, AcademicNotionSyncResult
from app.core.errors import (
    ErrorCode,
    LifeAgentError,
    authorization_error,
    transient_error,
)
from app.queue.periodic import PeriodicOccurrence, stable_period_key

OCCURRENCE = PeriodicOccurrence(
    local_time=datetime.fromisoformat("2026-09-10T08:00:00-04:00"),
    scheduled_at=datetime(2026, 9, 10, 12, tzinfo=UTC),
)
PERIOD_KEY = stable_period_key("academic-morning", OCCURRENCE)


class Store:
    def __init__(self, facts: PlannerFacts | None = None) -> None:
        self.facts = facts or PlannerFacts(horizon_days=14)
        self.saved: list[DailyPlan] = []
        self.loaded_at: list[datetime] = []

    def load_planner_facts(self, *, now: datetime, horizon_days: int) -> PlannerFacts:
        self.loaded_at.append(now)
        return self.facts.model_copy(update={"horizon_days": horizon_days})

    def save_daily_plan(self, plan: DailyPlan) -> None:
        self.saved.append(plan)


class Syncer:
    def __init__(self, result: AcademicNotionSyncResult) -> None:
        self.result = result
        self.calls: list[datetime | None] = []

    async def sync(self, *, now: datetime | None = None) -> AcademicNotionSyncResult:
        self.calls.append(now)
        return self.result


class Delivery:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def send_scheduled_notification(
        self,
        content: str,
        *,
        idempotency_key: str,
    ) -> object:
        self.messages.append((content, idempotency_key))
        return SimpleNamespace(status="sent")


class FailureSyncStore:
    def __init__(self) -> None:
        self.reminders: list[tuple[str, str | None]] = []

    def expire_clarifications(self, *, now: datetime | None = None) -> int:
        return 0

    def clear_setup_reminders(
        self,
        condition_code: str | None = None,
        fingerprint: str | None = None,
    ) -> int:
        return 0

    def setup_reminder_due(self, condition_code: str, fingerprint: str, day: object) -> bool:
        return True

    def record_setup_reminder(
        self,
        condition_code: str,
        fingerprint: str,
        day: object,
        *,
        affected_course_codes: object = (),
        delivered_at: datetime | None = None,
        delivery_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        self.reminders.append((condition_code, error_code))


class FailingConnector:
    def __init__(self, error: LifeAgentError) -> None:
        self.error = error

    async def discover_course_assessments(self) -> object:
        raise self.error


def _block(
    block_id: str,
    title: str,
    start_at: datetime,
    minutes: int,
    *,
    kind: Literal["assessment", "practice"] = "assessment",
    carried: bool = False,
) -> StudyBlock:
    return StudyBlock(
        id=block_id,
        assessment_id=f"assessment-{block_id}",
        learning_focus_id="focus-1" if kind == "practice" else None,
        block_kind=kind,
        title=title,
        start_at=start_at,
        end_at=start_at + timedelta(minutes=minutes),
        carried_over=carried,
        priority_score=10,
        rationale="deterministic test allocation",
    )


def test_formatter_includes_only_intended_local_day_with_exact_allocator_facts() -> None:
    plan = DailyPlan(
        plan_id=UUID("38da2acc-f0f8-4eff-a16c-2bbc160624cf"),
        created_at=OCCURRENCE.scheduled_at,
        blocks=(
            _block("later", "Tomorrow task", datetime(2026, 9, 11, 13, tzinfo=UTC), 30),
            _block(
                "carry",
                "MATH 239 Assignment 2",
                datetime(2026, 9, 10, 18, tzinfo=UTC),
                60,
                carried=True,
            ),
            _block(
                "practice",
                "Practice ECE 250 Graph traversals",
                datetime(2026, 9, 10, 13, tzinfo=UTC),
                45,
                kind="practice",
            ),
        ),
    )

    notification = build_scheduled_morning_notification(
        plan,
        period_key=PERIOD_KEY,
        occurrence=OCCURRENCE,
        source_synced_at=OCCURRENCE.scheduled_at,
        timezone_name="America/Toronto",
    )

    assert notification.intended_local_date.isoformat() == "2026-09-10"
    assert [block.block_id for block in notification.blocks] == ["practice", "carry"]
    assert "Thursday, September 10, 2026" in notification.message_text
    assert "09:00 — Practice ECE 250 Graph traversals (45 minutes, practice)" in (
        notification.message_text
    )
    assert "14:00 — MATH 239 Assignment 2 (60 minutes, assessment, carried forward)" in (
        notification.message_text
    )
    assert "Tomorrow task" not in notification.message_text


async def test_successful_empty_refresh_sends_natural_light_day_message() -> None:
    store = Store()
    delivery = Delivery()
    syncer = Syncer(AcademicNotionSyncResult(status="succeeded", synced_at=OCCURRENCE.scheduled_at))

    result = await execute_scheduled_morning_notification(
        store=store,
        syncer=syncer,
        delivery=delivery,
        occurrence=OCCURRENCE,
        period_key=PERIOD_KEY,
        executed_at=OCCURRENCE.scheduled_at,
    )

    assert result["status"] == "succeeded"
    assert result["block_count"] == 0
    assert store.loaded_at == [OCCURRENCE.scheduled_at]
    assert len(store.saved) == 1
    assert "no scheduled study blocks today" in delivery.messages[0][0]
    assert delivery.messages[0][1] == scheduled_delivery_key(PERIOD_KEY, OCCURRENCE)


async def test_allocator_output_is_authoritative_for_populated_notification() -> None:
    facts = PlannerFacts(
        assessments=(
            Assessment(
                id="quiz-1",
                course="ECE 250",
                title="ECE 250 Quiz 1 review",
                assessment_type=AssessmentType.QUIZ,
                due_at=datetime(2026, 9, 11, 20, tzinfo=UTC),
                estimated_minutes=45,
                weight_percent=10,
            ),
            Assessment(
                id="assignment-2",
                course="MATH 239",
                title="MATH 239 Assignment 2",
                assessment_type=AssessmentType.ASSIGNMENT,
                due_at=datetime(2026, 9, 12, 20, tzinfo=UTC),
                estimated_minutes=60,
                weight_percent=20,
            ),
        ),
        incomplete_blocks=(
            IncompleteBlock(
                id="old",
                assessment_id="assignment-2",
                title="MATH 239 Assignment 2",
                remaining_minutes=60,
            ),
        ),
        practice_needs=(
            PracticeNeed(
                focus_id="focus-1",
                course_code="ECE 250",
                assessment_id="quiz-1",
                topic="graph traversals",
                target_minutes=30,
                next_review_at=datetime(2026, 9, 11, 12, tzinfo=UTC),
                source_action="reinforce_focus",
                rationale="verified reflection focus",
            ),
        ),
        availability=(
            AvailabilityWindow(
                start_at=datetime(2026, 9, 10, 13, tzinfo=UTC),
                end_at=datetime(2026, 9, 10, 21, tzinfo=UTC),
            ),
        ),
        horizon_days=14,
    )
    store = Store(facts)
    delivery = Delivery()

    result = await execute_scheduled_morning_notification(
        store=store,
        syncer=Syncer(
            AcademicNotionSyncResult(status="succeeded", synced_at=OCCURRENCE.scheduled_at)
        ),
        delivery=delivery,
        occurrence=OCCURRENCE,
        period_key=PERIOD_KEY,
        executed_at=OCCURRENCE.scheduled_at,
    )

    assert result["status"] == "succeeded"
    assert result["block_count"] == 3
    message = delivery.messages[0][0]
    assert "09:00 — Practice ECE 250 graph traversals (30 minutes, practice)" in message
    assert "carried forward" in message
    assert "ECE 250 Quiz 1 review" in message


@pytest.mark.parametrize(
    ("sync_result", "expected_code", "expected_status"),
    [
        (
            AcademicNotionSyncResult(
                status="setup_required",
                diagnostic_codes=("notion_configuration_missing",),
                error_code=ErrorCode.SOURCE_SETUP_REQUIRED.value,
            ),
            ErrorCode.SOURCE_SETUP_REQUIRED,
            "attention",
        ),
        (
            AcademicNotionSyncResult(
                status="partial",
                diagnostic_codes=("assessment_calendar_missing",),
                error_code=ErrorCode.SOURCE_SYNC_PARTIAL.value,
            ),
            ErrorCode.SOURCE_SYNC_PARTIAL,
            "attention",
        ),
        (
            AcademicNotionSyncResult(
                status="succeeded",
                synced_at=OCCURRENCE.scheduled_at - timedelta(minutes=6),
            ),
            ErrorCode.SOURCE_STALE,
            "attention",
        ),
    ],
)
async def test_untrusted_source_states_never_send_light_day(
    sync_result: AcademicNotionSyncResult,
    expected_code: ErrorCode,
    expected_status: str,
) -> None:
    store = Store()
    delivery = Delivery()

    result = await execute_scheduled_morning_notification(
        store=store,
        syncer=Syncer(sync_result),
        delivery=delivery,
        occurrence=OCCURRENCE,
        period_key=PERIOD_KEY,
        executed_at=OCCURRENCE.scheduled_at,
    )

    assert result["status"] == expected_status
    assert result["error_code"] == expected_code.value
    assert store.loaded_at == []
    assert len(delivery.messages) == 1
    assert "no scheduled study blocks" not in delivery.messages[0][0]
    assert expected_code.value in delivery.messages[0][1]


async def test_transient_source_failure_retries_then_surfaces_final_failure() -> None:
    result = AcademicNotionSyncResult(
        status="failed",
        diagnostic_codes=("notion_sync_failed",),
        error_code=ErrorCode.CONNECTOR_TRANSIENT.value,
        retryable=True,
    )
    delivery = Delivery()

    with pytest.raises(LifeAgentError, match=ErrorCode.CONNECTOR_TRANSIENT.value):
        await execute_scheduled_morning_notification(
            store=Store(),
            syncer=Syncer(result),
            delivery=delivery,
            occurrence=OCCURRENCE,
            period_key=PERIOD_KEY,
            executed_at=OCCURRENCE.scheduled_at,
            attempt=1,
            attempt_limit=2,
        )
    assert delivery.messages == []

    final = await execute_scheduled_morning_notification(
        store=Store(),
        syncer=Syncer(result),
        delivery=delivery,
        occurrence=OCCURRENCE,
        period_key=PERIOD_KEY,
        executed_at=OCCURRENCE.scheduled_at,
        attempt=2,
        attempt_limit=2,
    )
    assert final["status"] == "failed"
    assert len(delivery.messages) == 1


async def test_retry_after_grace_does_not_send_stale_morning_greeting() -> None:
    delivery = Delivery()
    store = Store()

    result = await execute_scheduled_morning_notification(
        store=store,
        syncer=Syncer(
            AcademicNotionSyncResult(status="succeeded", synced_at=OCCURRENCE.scheduled_at)
        ),
        delivery=delivery,
        occurrence=OCCURRENCE,
        period_key=PERIOD_KEY,
        executed_at=OCCURRENCE.scheduled_at + timedelta(minutes=31),
        catchup_grace_minutes=30,
    )

    assert result["status"] == "attention"
    assert result["error_code"] == ErrorCode.SCHEDULE_LATE.value
    assert store.loaded_at == []
    assert "no stale morning plan was sent" in delivery.messages[0][0]


def test_period_key_must_match_explicit_occurrence() -> None:
    with pytest.raises(ValueError, match="period key"):
        scheduled_delivery_key("academic-morning:2026-09-11:0800:v1", OCCURRENCE)


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code", "retryable"),
    [
        (
            transient_error(ErrorCode.CONNECTOR_TRANSIENT, "temporary Notion timeout"),
            "failed",
            ErrorCode.CONNECTOR_TRANSIENT.value,
            True,
        ),
        (
            authorization_error("Notion token rejected"),
            "setup_required",
            ErrorCode.AUTHORIZATION_INVALID.value,
            False,
        ),
    ],
)
async def test_notion_sync_preserves_retry_classification_without_exposing_raw_errors(
    error: LifeAgentError,
    expected_status: str,
    expected_code: str,
    retryable: bool,
) -> None:
    store = FailureSyncStore()
    syncer = AcademicNotionSync(
        connector=FailingConnector(error),  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
    )

    result = await syncer.sync(now=OCCURRENCE.scheduled_at)

    assert result.status == expected_status
    assert result.error_code == expected_code
    assert result.retryable is retryable
    assert result.diagnostic_codes == ("notion_sync_failed",)
    assert error.record.diagnostic not in repr(result)
