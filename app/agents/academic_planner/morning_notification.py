"""Deterministic, model-free scheduled academic morning notification."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from app.agents.academic_planner.contracts import (
    DailyPlan,
    PlannerFacts,
    ScheduledMorningBlock,
    ScheduledMorningNotification,
)
from app.agents.academic_planner.sync import AcademicNotionSync, AcademicNotionSyncResult
from app.agents.academic_planner.workflow import build_daily_plan
from app.core.config import Settings, get_settings
from app.core.errors import ErrorCode, LifeAgentError, transient_error
from app.db.academic import SQLAlchemyAcademicPlannerStore
from app.db.session import Database
from app.queue.idempotency import build_idempotency_key
from app.queue.periodic import PeriodicOccurrence, stable_period_key

_SOURCE_FRESHNESS = timedelta(minutes=5)
_DISCORD_CONTENT_LIMIT = 2_000


class ScheduledMorningSyncer(Protocol):
    async def sync(self, *, now: datetime | None = None) -> AcademicNotionSyncResult: ...


class ScheduledMorningStore(Protocol):
    def load_planner_facts(self, *, now: datetime, horizon_days: int) -> PlannerFacts: ...

    def save_daily_plan(self, plan: DailyPlan) -> None: ...


class ScheduledMorningDelivery(Protocol):
    async def send_scheduled_notification(
        self,
        content: str,
        *,
        idempotency_key: str,
    ) -> object: ...


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value


def _period_parts(period_key: str, occurrence: PeriodicOccurrence) -> tuple[str, str]:
    expected = stable_period_key("academic-morning", occurrence)
    if period_key != expected:
        raise ValueError("academic morning period key does not match the scheduled occurrence")
    local = occurrence.local_time
    return local.date().isoformat(), local.strftime("%H%M")


def scheduled_delivery_key(period_key: str, occurrence: PeriodicOccurrence) -> str:
    """Derive a stable delivery identity from the same local scheduled period."""

    local_date, local_time = _period_parts(period_key, occurrence)
    return build_idempotency_key("academic-morning-delivery", local_date, local_time)


def scheduled_attention_key(
    period_key: str,
    occurrence: PeriodicOccurrence,
    error_code: ErrorCode,
) -> str:
    """Derive one idempotent operational notice per period and failure class."""

    local_date, local_time = _period_parts(period_key, occurrence)
    return build_idempotency_key(
        "academic-morning-alert",
        local_date,
        local_time,
        error_code.value,
    )


def build_scheduled_morning_notification(
    plan: DailyPlan,
    *,
    period_key: str,
    occurrence: PeriodicOccurrence,
    source_synced_at: datetime,
    timezone_name: str,
) -> ScheduledMorningNotification:
    """Select the intended local day and render every selected block exactly once."""

    zone = ZoneInfo(timezone_name)
    source_synced_at = _aware(source_synced_at, "source_synced_at").astimezone(UTC)
    intended_date = occurrence.local_time.astimezone(zone).date()
    selected: list[ScheduledMorningBlock] = []
    for block in sorted(plan.blocks, key=lambda item: (item.start_at, item.id)):
        local_start = block.start_at.astimezone(zone)
        if local_start.date() != intended_date:
            continue
        seconds = int((block.end_at - block.start_at).total_seconds())
        if seconds <= 0 or seconds % 60:
            raise ValueError("scheduled block duration must be a positive whole minute")
        selected.append(
            ScheduledMorningBlock(
                block_id=block.id,
                title=block.title,
                local_start=local_start,
                duration_minutes=seconds // 60,
                block_kind=block.block_kind,
                carried_over=block.carried_over,
            )
        )

    date_label = intended_date.strftime("%A, %B %d, %Y").replace(" 0", " ")
    if selected:
        lines = [f"Good morning, Richard. Today's plan for {date_label}:"]
        for block in selected:
            details = [f"{block.duration_minutes} minutes", block.block_kind]
            if block.carried_over:
                details.append("carried forward")
            lines.append(
                f"- {block.local_start.strftime('%H:%M')} — {block.title} ({', '.join(details)})"
            )
        lines.append("Have a good day!")
    else:
        lines = [
            f"Good morning, Richard. Your academic plan for {date_label} is clear—there are "
            "no scheduled study blocks today. Have a good day!"
        ]
    message = "\n".join(lines)
    if len(message) > _DISCORD_CONTENT_LIMIT:
        raise ValueError("scheduled academic notification exceeds Discord's content limit")
    return ScheduledMorningNotification(
        period_key=period_key,
        intended_local_date=intended_date,
        scheduled_at=occurrence.scheduled_at,
        source_synced_at=source_synced_at,
        blocks=tuple(selected),
        message_text=message,
    )


def _attention_message(error_code: ErrorCode, occurrence: PeriodicOccurrence) -> str:
    schedule_label = occurrence.local_time.strftime("%H:%M")
    if error_code is ErrorCode.SCHEDULE_LATE:
        return (
            f"LifeAgent missed the {schedule_label} academic notification window, so no stale "
            "morning plan was sent. Please check the academic worker and queue health."
        )
    if error_code is ErrorCode.SOURCE_SETUP_REQUIRED:
        return (
            "LifeAgent could not refresh the Notion academic source, so it did not send a "
            "possibly stale plan. Please check the Notion token, Courses database, and sharing."
        )
    if error_code is ErrorCode.SOURCE_SYNC_PARTIAL:
        return (
            "LifeAgent found an incomplete Notion academic refresh, so it did not send a "
            "possibly incomplete plan. Please review the academic source diagnostics."
        )
    if error_code is ErrorCode.SOURCE_STALE:
        return (
            "LifeAgent could not prove the academic source was freshly synchronized, so it did "
            "not send a morning plan. Please check Notion sync health."
        )
    if error_code is ErrorCode.DELIVERY_CONTENT_TOO_LONG:
        return (
            "LifeAgent's deterministic academic plan does not fit safely in one Discord message, "
            "so nothing was omitted. Please review today's plan in the operations console."
        )
    return (
        "LifeAgent could not refresh the Notion academic source, so it did not send a possibly "
        "stale plan. Please check Notion and academic worker health."
    )


async def _send_attention(
    delivery: ScheduledMorningDelivery | None,
    *,
    period_key: str,
    occurrence: PeriodicOccurrence,
    error_code: ErrorCode,
) -> int:
    if delivery is None:
        return 0
    await delivery.send_scheduled_notification(
        _attention_message(error_code, occurrence),
        idempotency_key=scheduled_attention_key(period_key, occurrence, error_code),
    )
    return 1


def _sync_error_code(result: AcademicNotionSyncResult) -> ErrorCode:
    if result.status == "partial":
        return ErrorCode.SOURCE_SYNC_PARTIAL
    if result.error_code:
        try:
            return ErrorCode(result.error_code)
        except ValueError:
            pass
    if result.status == "setup_required":
        return ErrorCode.SOURCE_SETUP_REQUIRED
    return ErrorCode.SOURCE_SYNC_FAILED


async def execute_scheduled_morning_notification(
    *,
    store: ScheduledMorningStore,
    syncer: ScheduledMorningSyncer,
    delivery: ScheduledMorningDelivery | None,
    occurrence: PeriodicOccurrence,
    period_key: str,
    executed_at: datetime,
    timezone_name: str = "America/Toronto",
    catchup_grace_minutes: int = 30,
    horizon_days: int = 14,
    attempt: int = 1,
    attempt_limit: int = 3,
) -> dict[str, object]:
    """Refresh, allocate, persist, format, and deliver one scheduled local period."""

    current = _aware(executed_at, "executed_at").astimezone(UTC)
    _period_parts(period_key, occurrence)
    if not 1 <= catchup_grace_minutes <= 180:
        raise ValueError("catchup_grace_minutes must be between 1 and 180")
    if not 7 <= horizon_days <= 14:
        raise ValueError("horizon_days must be between 7 and 14")
    if attempt < 1 or attempt_limit < attempt:
        raise ValueError("attempt values are invalid")
    if current < occurrence.scheduled_at:
        raise ValueError("scheduled notification cannot execute before its occurrence")
    if current > occurrence.scheduled_at + timedelta(minutes=catchup_grace_minutes):
        count = await _send_attention(
            delivery,
            period_key=period_key,
            occurrence=occurrence,
            error_code=ErrorCode.SCHEDULE_LATE,
        )
        return {
            "status": "attention",
            "error_code": ErrorCode.SCHEDULE_LATE.value,
            "delivery_count": count,
            "block_count": 0,
        }

    sync_result = await syncer.sync(now=current)
    if sync_result.status != "succeeded":
        error_code = _sync_error_code(sync_result)
        if sync_result.retryable and attempt < attempt_limit:
            raise transient_error(error_code, "academic source refresh is temporarily unavailable")
        count = await _send_attention(
            delivery,
            period_key=period_key,
            occurrence=occurrence,
            error_code=error_code,
        )
        return {
            "status": "failed" if sync_result.status == "failed" else "attention",
            "error_code": error_code.value,
            "sync_status": sync_result.status,
            "delivery_count": count,
            "block_count": 0,
        }
    synced_at = sync_result.synced_at
    if synced_at is None or abs(current - _aware(synced_at, "synced_at").astimezone(UTC)) > (
        _SOURCE_FRESHNESS
    ):
        count = await _send_attention(
            delivery,
            period_key=period_key,
            occurrence=occurrence,
            error_code=ErrorCode.SOURCE_STALE,
        )
        return {
            "status": "attention",
            "error_code": ErrorCode.SOURCE_STALE.value,
            "sync_status": sync_result.status,
            "delivery_count": count,
            "block_count": 0,
        }

    facts = store.load_planner_facts(now=occurrence.scheduled_at, horizon_days=horizon_days)
    plan = build_daily_plan(facts, now=occurrence.scheduled_at)
    store.save_daily_plan(plan)
    try:
        notification = build_scheduled_morning_notification(
            plan,
            period_key=period_key,
            occurrence=occurrence,
            source_synced_at=synced_at,
            timezone_name=timezone_name,
        )
    except ValueError as exc:
        if "content limit" not in str(exc):
            raise
        count = await _send_attention(
            delivery,
            period_key=period_key,
            occurrence=occurrence,
            error_code=ErrorCode.DELIVERY_CONTENT_TOO_LONG,
        )
        return {
            "status": "attention",
            "error_code": ErrorCode.DELIVERY_CONTENT_TOO_LONG.value,
            "sync_status": sync_result.status,
            "delivery_count": count,
            "block_count": 0,
        }
    if delivery is None:
        return {
            "status": "failed",
            "error_code": ErrorCode.AUTHORIZATION_INVALID.value,
            "sync_status": sync_result.status,
            "delivery_count": 0,
            "block_count": len(notification.blocks),
            "plan_id": str(plan.plan_id),
        }
    receipt = await delivery.send_scheduled_notification(
        notification.message_text,
        idempotency_key=scheduled_delivery_key(period_key, occurrence),
    )
    return {
        "status": "succeeded",
        "plan_id": str(plan.plan_id),
        "block_count": len(notification.blocks),
        "deferred_count": len(plan.deferred_assessment_ids),
        "deferred_practice_count": len(plan.deferred_practice_focus_ids),
        "ambiguous_count": len(plan.ambiguous_questions),
        "sync_status": sync_result.status,
        "delivery_count": 1,
        "delivery_status": str(getattr(receipt, "status", "sent")),
    }


class _Runtime:
    def __init__(
        self,
        *,
        database: Database,
        settings: Settings,
        store: ScheduledMorningStore,
        syncer: ScheduledMorningSyncer,
        delivery: ScheduledMorningDelivery | None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.store = store
        self.syncer = syncer
        self.delivery = delivery


RuntimeFactory = Callable[[uuid.UUID], _Runtime]


def _load_runtime(run_id: uuid.UUID) -> _Runtime:
    settings = get_settings()
    database = Database(settings)
    store = SQLAlchemyAcademicPlannerStore(
        database.engine,
        confirmation_ttl_hours=settings.academic_confirmation_ttl_hours,
        default_practice_minutes=settings.academic_memory_default_practice_minutes,
    )
    connector = None
    setup_condition = "notion_configuration_missing"
    if settings.notion_token is not None and settings.notion_courses_database_id is not None:
        from app.connectors.notion import NotionConnector

        try:
            connector = NotionConnector(
                token=settings.notion_token,
                courses_database_id=settings.notion_courses_database_id,
                timeout_seconds=settings.connector_timeout_seconds,
            )
        except (LifeAgentError, ValueError):
            setup_condition = "notion_configuration_invalid"
    syncer = AcademicNotionSync(
        connector=connector,
        store=store,
        discord=None,
        discord_channel_id=None,
        timezone=settings.app_timezone,
        clarification_ttl_hours=settings.academic_confirmation_ttl_hours,
        setup_condition_code=setup_condition,
        material_enqueuer=None,
    )
    delivery = None
    if settings.discord_bot_token is not None and settings.discord_academic_channel_id is not None:
        from app.connectors.discord import (
            DiscordAcademicPlannerAdapter,
            DiscordAcademicPlannerDelivery,
        )

        adapter = DiscordAcademicPlannerAdapter(
            token=settings.discord_bot_token,
            allowed_channel_ids={settings.discord_academic_channel_id},
            base_url=settings.discord_api_url,
        )
        delivery = DiscordAcademicPlannerDelivery(
            engine=database.engine,
            run_id=run_id,
            channel_id=settings.discord_academic_channel_id,
            adapter=adapter,
        )
    return _Runtime(
        database=database,
        settings=settings,
        store=store,
        syncer=syncer,
        delivery=delivery,
    )


_runtime_factory: RuntimeFactory = _load_runtime


async def run_scheduled_morning_notification(
    occurrence_at_iso: str,
    run_id: str,
    period_key: str,
    attempt: int,
    attempt_limit: int,
) -> dict[str, object]:
    """Worker boundary accepting only durable occurrence/run identities."""

    occurrence_at = datetime.fromisoformat(occurrence_at_iso)
    occurrence_at = _aware(occurrence_at, "occurrence_at").astimezone(UTC)
    parsed_run_id = uuid.UUID(run_id)
    runtime = _runtime_factory(parsed_run_id)
    zone = ZoneInfo(runtime.settings.app_timezone)
    occurrence = PeriodicOccurrence(
        local_time=occurrence_at.astimezone(zone),
        scheduled_at=occurrence_at,
    )
    try:
        return await execute_scheduled_morning_notification(
            store=runtime.store,
            syncer=runtime.syncer,
            delivery=runtime.delivery,
            occurrence=occurrence,
            period_key=period_key,
            executed_at=datetime.now(UTC),
            timezone_name=runtime.settings.app_timezone,
            catchup_grace_minutes=runtime.settings.academic_morning_catchup_grace_minutes,
            horizon_days=runtime.settings.academic_plan_horizon_days,
            attempt=attempt,
            attempt_limit=attempt_limit,
        )
    finally:
        runtime.database.dispose()


__all__ = [
    "ScheduledMorningDelivery",
    "ScheduledMorningStore",
    "ScheduledMorningSyncer",
    "build_scheduled_morning_notification",
    "execute_scheduled_morning_notification",
    "run_scheduled_morning_notification",
    "scheduled_attention_key",
    "scheduled_delivery_key",
]
