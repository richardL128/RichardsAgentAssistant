from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.agents.academic_planner.morning_notification import (
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
    def __init__(self) -> None:
        self.loaded_at: list[datetime] = []

    def load_morning_calendar_items(self, *, occurrence: datetime, timezone: str):
        self.loaded_at.append(occurrence)
        return ()

    def load_active_morning_courses(self):
        return ()


class Syncer:
    def __init__(self, result: AcademicNotionSyncResult) -> None:
        self.result = result

    async def sync(self, *, now: datetime | None = None) -> AcademicNotionSyncResult:
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
        return object()


class FailureSyncStore:
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
        return None


class FailingConnector:
    def __init__(self, error: LifeAgentError) -> None:
        self.error = error

    async def discover_course_assessments(self) -> object:
        raise self.error


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
    assert "no stale morning calendar was sent" in delivery.messages[0][0]


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
