"""Focused Toronto-local nightly academic check-in runtime."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from app.artifacts.store import ArtifactStore
from app.connectors.discord import DiscordAcademicPlannerAdapter, DiscordAcademicPlannerDelivery
from app.core.config import Settings, get_settings
from app.core.errors import ErrorCode, permanent_error, transient_error
from app.db.session import Database
from app.queue.idempotency import build_idempotency_key
from app.queue.periodic import PeriodicOccurrence, stable_period_key

NIGHTLY_CHECKIN_NAMESPACE = "academic-end-of-day"
NIGHTLY_CHECKIN_PROMPT_VERSION = "academic-nightly-checkin-v1"
NIGHTLY_CHECKIN_KIND = "academic_end_of_day_reflection"


class NightlyDelivery(Protocol):
    async def send_scheduled_notification(
        self,
        content: str,
        *,
        idempotency_key: str,
    ) -> object: ...


class NightlyConversationService(Protocol):
    def inspect_open(
        self,
        *,
        discord_channel_id: str,
        owner_discord_user_id: str,
        now: datetime | None = None,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class NightlyCheckinConfig:
    channel_id: str | None
    proactive_owner_id: str | None
    authorized_user_ids: frozenset[str]
    message_content_enabled: bool
    discord_delivery_enabled: bool
    model_identity: str | None
    prompt_config_version: str | None
    session_ttl_hours: int
    catchup_grace_minutes: int
    timezone_name: str


@dataclass(slots=True)
class NightlyCheckinRuntime:
    config: NightlyCheckinConfig
    delivery: NightlyDelivery | None
    conversation_service: NightlyConversationService
    database: Database | None = None


def nightly_period_key(occurrence: PeriodicOccurrence) -> str:
    """Return the one shared identity for run, lock, delivery, and root."""

    return stable_period_key(NIGHTLY_CHECKIN_NAMESPACE, occurrence)


def nightly_delivery_key(period_key: str, occurrence: PeriodicOccurrence) -> str:
    """Derive the durable Discord delivery key from the shared local period."""

    local_date, local_time = _period_parts(period_key, occurrence)
    return build_idempotency_key("academic-eod-delivery", local_date, local_time)


def render_nightly_checkin_prompt(occurrence: PeriodicOccurrence) -> str:
    """Render the consent-accurate owner-facing nightly prompt."""

    label = occurrence.local_time.strftime("%A, %B %d").replace(" 0", " ")
    return (
        f"Evening check-in for {label}:\n\n"
        "- What did you finish today?\n"
        "- What slipped, and what got in the way?\n"
        "- What felt difficult or needs more practice?\n"
        "- Any new tasks, deadlines, tests, or events I should prepare for Notion?\n\n"
        "Reply naturally. I will use study-related reflections to improve your private "
        "academic memory and may prepare study-session or calendar proposals for your "
        'review. Calendar changes still require your confirmation. Reply "skip" if you '
        "do not want to check in tonight."
    )


async def execute_nightly_checkin(
    *,
    runtime: NightlyCheckinRuntime,
    occurrence: PeriodicOccurrence,
    period_key: str,
    executed_at: datetime,
    catchup_grace_minutes: int,
    attempt: int = 1,
    attempt_limit: int = 3,
) -> dict[str, object]:
    """Deliver and recover one nightly proactive prompt without private diagnostics."""

    current = _aware(executed_at, "executed_at").astimezone(UTC)
    _period_parts(period_key, occurrence)
    if not 1 <= catchup_grace_minutes <= 180:
        raise ValueError("catchup_grace_minutes must be between 1 and 180")
    if attempt < 1 or attempt_limit < attempt:
        raise ValueError("attempt values are invalid")
    if current < occurrence.scheduled_at:
        raise ValueError("nightly check-in cannot execute before its occurrence")

    setup_error = _configuration_error(runtime)
    if setup_error is not None:
        return {
            "status": "attention",
            "error_code": setup_error.value,
            "delivery_count": 0,
            "conversation_status": "not_opened",
        }
    open_prompt = getattr(runtime.conversation_service, "open_proactive_prompt", None)
    if not callable(open_prompt):
        return {
            "status": "failed",
            "error_code": ErrorCode.INTERNAL.value,
            "delivery_count": 0,
            "conversation_status": "api_missing",
            "required_api": "NativeConversationService.open_proactive_prompt",
        }

    if current > occurrence.scheduled_at + timedelta(minutes=catchup_grace_minutes):
        return {
            "status": "attention",
            "error_code": ErrorCode.SCHEDULE_LATE.value,
            "delivery_count": 0,
            "conversation_status": "stale_not_opened",
        }

    config = runtime.config
    assert config.channel_id is not None
    assert config.proactive_owner_id is not None

    open_status = runtime.conversation_service.inspect_open(
        discord_channel_id=config.channel_id,
        owner_discord_user_id=config.proactive_owner_id,
        now=current,
    )
    open_root = getattr(open_status, "root_event_id", None)
    if open_root == period_key:
        return {
            "status": "succeeded",
            "delivery_count": 1,
            "conversation_status": "already_open",
            "replayed": True,
            "period_key": period_key,
        }
    if _is_open_conversation(open_status):
        if current <= occurrence.scheduled_at + timedelta(minutes=catchup_grace_minutes):
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT,
                "native_conversation_busy",
            )
        return {
            "status": "attention",
            "error_code": ErrorCode.SCHEDULE_LATE.value,
            "delivery_count": 0,
            "conversation_status": "busy_after_grace",
        }

    prompt = render_nightly_checkin_prompt(occurrence)
    delivery = runtime.delivery
    if delivery is None:
        return {
            "status": "attention",
            "error_code": ErrorCode.AUTHORIZATION_INVALID.value,
            "delivery_count": 0,
            "conversation_status": "not_opened",
        }
    receipt = await delivery.send_scheduled_notification(
        prompt,
        idempotency_key=nightly_delivery_key(period_key, occurrence),
    )
    expires_at = current + timedelta(hours=min(max(config.session_ttl_hours, 1), 24))
    proactive = open_prompt(
        root_event_id=period_key,
        discord_channel_id=config.channel_id,
        owner_discord_user_id=config.proactive_owner_id,
        prompt_text=prompt,
        expires_at=expires_at,
        model_identity=config.model_identity,
        prompt_config_version=config.prompt_config_version or NIGHTLY_CHECKIN_PROMPT_VERSION,
        proactive_kind=NIGHTLY_CHECKIN_KIND,
        proactive_period=period_key,
        now=current,
    )
    status = str(getattr(proactive, "status", "opened"))
    if status in {"in_progress", "failed", "corrupt"}:
        raise permanent_error(ErrorCode.INTERNAL, "proactive_conversation_not_opened")
    return {
        "status": "succeeded",
        "delivery_count": 1,
        "delivery_status": str(getattr(receipt, "status", "sent")),
        "conversation_status": status,
        "period_key": period_key,
    }


def run_scheduled_nightly_checkin_runtime_factory(run_id: uuid.UUID) -> NightlyCheckinRuntime:
    settings = get_settings()
    database = Database(settings)
    artifact_store = ArtifactStore(
        settings.artifact_root,
        retention_days_by_class={
            "native_context_manifest": settings.conversation_context_manifest_retention_days,
            "user_memory_content": None,
            "user_memory_evidence": None,
        },
        default_retention_days=settings.artifact_retention_days,
    )
    from app.agents.conversation.service import NativeConversationService

    conversation_service = NativeConversationService(
        engine=database.engine,
        artifact_store=artifact_store,
        session_ttl_hours=settings.academic_confirmation_ttl_hours,
    )
    delivery = None
    if settings.discord_bot_token is not None and settings.discord_academic_channel_id is not None:
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
    return NightlyCheckinRuntime(
        config=_config_from_settings(settings),
        delivery=delivery,
        conversation_service=conversation_service,
        database=database,
    )


RuntimeFactory = Callable[[uuid.UUID], NightlyCheckinRuntime]
_runtime_factory: RuntimeFactory = run_scheduled_nightly_checkin_runtime_factory


async def run_scheduled_nightly_checkin(
    occurrence_at_iso: str,
    run_id: str,
    period_key: str,
    attempt: int,
    attempt_limit: int,
) -> dict[str, object]:
    """Worker boundary accepting only durable occurrence/run identities."""

    occurrence_at = _aware(datetime.fromisoformat(occurrence_at_iso), "occurrence_at").astimezone(
        UTC
    )
    parsed_run_id = uuid.UUID(run_id)
    runtime = _runtime_factory(parsed_run_id)
    try:
        zone = ZoneInfo(runtime.config.timezone_name)
        occurrence = PeriodicOccurrence(
            local_time=occurrence_at.astimezone(zone),
            scheduled_at=occurrence_at,
        )
        return await execute_nightly_checkin(
            runtime=runtime,
            occurrence=occurrence,
            period_key=period_key,
            executed_at=datetime.now(UTC),
            catchup_grace_minutes=runtime.config.catchup_grace_minutes,
            attempt=attempt,
            attempt_limit=attempt_limit,
        )
    finally:
        database = getattr(runtime, "database", None)
        close = getattr(database, "dispose", None)
        if callable(close):
            close()


def _config_from_settings(settings: Settings) -> NightlyCheckinConfig:
    owner = settings.discord_academic_proactive_user_id
    return NightlyCheckinConfig(
        channel_id=settings.discord_academic_channel_id,
        proactive_owner_id=str(owner) if owner is not None else None,
        authorized_user_ids=frozenset(
            str(user_id) for user_id in settings.discord_academic_authorized_user_ids
        ),
        message_content_enabled=settings.discord_academic_message_content_enabled,
        discord_delivery_enabled=settings.discord_bot_token is not None,
        model_identity=settings.ollama_model,
        prompt_config_version=NIGHTLY_CHECKIN_PROMPT_VERSION,
        session_ttl_hours=settings.academic_confirmation_ttl_hours,
        catchup_grace_minutes=settings.academic_end_of_day_catchup_grace_minutes,
        timezone_name=settings.app_timezone,
    )


def _configuration_error(runtime: NightlyCheckinRuntime) -> ErrorCode | None:
    config = runtime.config
    if (
        not config.discord_delivery_enabled
        or config.channel_id is None
        or config.proactive_owner_id is None
        or not config.message_content_enabled
    ):
        return ErrorCode.AUTHORIZATION_INVALID
    if config.proactive_owner_id not in config.authorized_user_ids:
        return ErrorCode.AUTHORIZATION_INVALID
    return None


def _is_open_conversation(result: Any) -> bool:
    return getattr(result, "state", None) in {"processing", "awaiting_user"} or str(
        getattr(result, "status", "")
    ) in {"in_progress", "resumed"}


def _period_parts(period_key: str, occurrence: PeriodicOccurrence) -> tuple[str, str]:
    expected = nightly_period_key(occurrence)
    if period_key != expected:
        raise ValueError("academic nightly period key does not match the scheduled occurrence")
    local = occurrence.local_time
    return local.date().isoformat(), local.strftime("%H%M")


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value


__all__ = [
    "NIGHTLY_CHECKIN_KIND",
    "NIGHTLY_CHECKIN_NAMESPACE",
    "NIGHTLY_CHECKIN_PROMPT_VERSION",
    "NightlyCheckinConfig",
    "NightlyCheckinRuntime",
    "execute_nightly_checkin",
    "nightly_delivery_key",
    "nightly_period_key",
    "render_nightly_checkin_prompt",
    "run_scheduled_nightly_checkin",
]
