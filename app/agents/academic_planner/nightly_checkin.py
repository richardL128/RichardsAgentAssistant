"""Focused Toronto-local nightly academic check-in runtime."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from app.agents.academic_planner.nightly_conversation import (
    NightlyChecklistItem,
    NightlySemanticDecision,
    NightlyTaskDateRange,
    build_nightly_checkpoint,
    export_nightly_checkpoint,
    parse_nightly_checkpoint,
    render_completion_question,
    stable_nightly_item_id,
)
from app.agents.academic_planner.nightly_task_semantics import (
    NightlyTaskEligibilityStatus,
    NightlyTaskEvidenceFragment,
    NightlyTaskEvidenceSourceKind,
    NightlyTaskSemanticInput,
    with_nightly_task_title_evidence,
)
from app.artifacts.store import ArtifactStore
from app.connectors.discord import DiscordAcademicPlannerAdapter, DiscordAcademicPlannerDelivery
from app.core.config import Settings, get_settings
from app.core.errors import ErrorCode, LifeAgentError, permanent_error, transient_error
from app.db.session import Database
from app.queue.idempotency import build_idempotency_key
from app.queue.periodic import PeriodicOccurrence, stable_period_key

NIGHTLY_CHECKIN_NAMESPACE = "academic-end-of-day"
NIGHTLY_CHECKIN_PROMPT_VERSION = "academic-nightly-checkin-v2"
NIGHTLY_CHECKIN_KIND = "academic_nightly_task_checklist"
NIGHTLY_NATIVE_CHECKPOINT_VERSION = "academic-discord-native-tools.v3"
MAX_NIGHTLY_CANDIDATES = 40
MAX_NIGHTLY_CLASSIFICATION_SECONDS = 120.0


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


class NightlyCatalogSyncer(Protocol):
    async def sync(self, *, now: datetime | None = None) -> Any: ...


class NightlyCandidateCatalog(Protocol):
    def load_nightly_current_day_assessment_candidates(
        self,
        *,
        local_date: date,
        as_of: datetime,
        timezone: str = "America/Toronto",
        max_source_age: timedelta = timedelta(hours=26),
    ) -> Any: ...


class NightlySemanticInterpreter(Protocol):
    async def analyze(self, item: NightlyTaskSemanticInput) -> Any: ...


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
    catalog_syncer: NightlyCatalogSyncer | None = None
    candidate_catalog: NightlyCandidateCatalog | None = None
    semantic_interpreter: NightlySemanticInterpreter | None = None
    database: Database | None = None


def nightly_period_key(occurrence: PeriodicOccurrence) -> str:
    """Return the one shared identity for run, lock, delivery, and root."""

    return stable_period_key(NIGHTLY_CHECKIN_NAMESPACE, occurrence)


def nightly_delivery_key(period_key: str, occurrence: PeriodicOccurrence) -> str:
    """Derive the durable Discord delivery key from the shared local period."""

    local_date, local_time = _period_parts(period_key, occurrence)
    return build_idempotency_key("academic-eod-delivery", local_date, local_time)


def render_nightly_checkin_prompt(checkpoint: Any) -> str:
    """Render the first concrete question from a trusted v2 checkpoint."""

    return render_completion_question(checkpoint)


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
        try:
            checkpoint = parse_nightly_checkpoint(getattr(open_status, "checkpoint", None))
        except (TypeError, ValueError):
            return {
                "status": "attention",
                "error_code": "nightly_checkpoint_invalid",
                "delivery_count": 0,
                "conversation_status": "invalid_existing_session",
            }
        prompt = render_nightly_checkin_prompt(checkpoint)
        delivery = runtime.delivery
        if delivery is None:
            return {
                "status": "attention",
                "error_code": ErrorCode.AUTHORIZATION_INVALID.value,
                "delivery_count": 0,
                "conversation_status": "already_open_delivery_unavailable",
            }
        receipt = await delivery.send_scheduled_notification(
            prompt,
            idempotency_key=nightly_delivery_key(period_key, occurrence),
        )
        return {
            "status": "succeeded",
            "delivery_count": 1,
            "delivery_status": str(getattr(receipt, "status", "sent")),
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

    delivery = runtime.delivery
    if delivery is None:
        return {
            "status": "attention",
            "error_code": ErrorCode.AUTHORIZATION_INVALID.value,
            "delivery_count": 0,
            "conversation_status": "not_opened",
        }

    if (
        runtime.catalog_syncer is None
        or runtime.candidate_catalog is None
        or runtime.semantic_interpreter is None
    ):
        return await _nightly_preparation_failure(
            runtime=runtime,
            occurrence=occurrence,
            period_key=period_key,
            attempt=attempt,
            attempt_limit=attempt_limit,
            error_code="nightly_dependencies_unavailable",
        )
    try:
        sync_result = await runtime.catalog_syncer.sync(now=current)
    except Exception:
        return await _nightly_preparation_failure(
            runtime=runtime,
            occurrence=occurrence,
            period_key=period_key,
            attempt=attempt,
            attempt_limit=attempt_limit,
            error_code="nightly_catalog_refresh_failed",
        )
    if (
        str(getattr(sync_result, "status", "failed")) != "succeeded"
        or getattr(sync_result, "synced_at", None) is None
    ):
        return await _nightly_preparation_failure(
            runtime=runtime,
            occurrence=occurrence,
            period_key=period_key,
            attempt=attempt,
            attempt_limit=attempt_limit,
            error_code="nightly_catalog_freshness_unproven",
        )

    try:
        candidates = await asyncio.to_thread(
            runtime.candidate_catalog.load_nightly_current_day_assessment_candidates,
            local_date=occurrence.local_time.date(),
            as_of=current,
            timezone=config.timezone_name,
            max_source_age=timedelta(minutes=10),
        )
    except Exception:
        return await _nightly_preparation_failure(
            runtime=runtime,
            occurrence=occurrence,
            period_key=period_key,
            attempt=attempt,
            attempt_limit=attempt_limit,
            error_code="nightly_candidate_query_failed",
        )
    candidates = tuple(candidates)
    if len(candidates) > MAX_NIGHTLY_CANDIDATES:
        return await _nightly_preparation_failure(
            runtime=runtime,
            occurrence=occurrence,
            period_key=period_key,
            attempt=attempt,
            attempt_limit=attempt_limit,
            error_code="nightly_candidate_limit_exceeded",
        )

    try:
        accepted, semantic_failures = await asyncio.wait_for(
            _classify_candidates(
                runtime.semantic_interpreter,
                period_key=period_key,
                candidates=candidates,
            ),
            timeout=MAX_NIGHTLY_CLASSIFICATION_SECONDS,
        )
    except TimeoutError:
        return await _nightly_preparation_failure(
            runtime=runtime,
            occurrence=occurrence,
            period_key=period_key,
            attempt=attempt,
            attempt_limit=attempt_limit,
            error_code="nightly_semantics_timeout",
        )

    if not accepted:
        if candidates and semantic_failures:
            return await _nightly_preparation_failure(
                runtime=runtime,
                occurrence=occurrence,
                period_key=period_key,
                attempt=attempt,
                attempt_limit=attempt_limit,
                error_code="nightly_semantics_unavailable",
            )
        message = "No movable course tasks are scheduled for tonight's check-in."
        receipt = await delivery.send_scheduled_notification(
            message,
            idempotency_key=nightly_delivery_key(period_key, occurrence),
        )
        return {
            "status": "succeeded",
            "delivery_count": 1,
            "delivery_status": str(getattr(receipt, "status", "sent")),
            "conversation_status": "not_opened_no_tasks",
            "period_key": period_key,
        }

    checkpoint = build_nightly_checkpoint(
        period_key=period_key,
        local_date=occurrence.local_time.date(),
        items=accepted,
        timezone_name=config.timezone_name,
        builder_model_identity=getattr(runtime.semantic_interpreter, "model_identity", None),
        eligibility_prompt_version=getattr(accepted[0].semantic_decision, "prompt_version", None),
        critic_version=getattr(accepted[0].semantic_decision, "critic_version", None),
        created_at=current,
    )
    prompt = render_nightly_checkin_prompt(checkpoint)
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
        initial_checkpoint={
            "version": NIGHTLY_NATIVE_CHECKPOINT_VERSION,
            "nightly_checkin": export_nightly_checkpoint(checkpoint),
        },
        now=current,
    )
    status = str(getattr(proactive, "status", "opened"))
    if status in {"in_progress", "failed", "corrupt"}:
        raise permanent_error(ErrorCode.INTERNAL, "proactive_conversation_not_opened")
    receipt = await delivery.send_scheduled_notification(
        prompt,
        idempotency_key=nightly_delivery_key(period_key, occurrence),
    )
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
    from app.agents.academic_planner.nightly_task_semantics import (
        NightlyTaskSemanticInterpreter,
    )
    from app.agents.academic_planner.sync import AcademicNotionSync
    from app.connectors.google_calendar import GoogleCalendarConnector
    from app.connectors.notion import NotionConnector
    from app.db.academic import SQLAlchemyAcademicPlannerStore
    from app.llm.gateway import LLMGateway

    store = SQLAlchemyAcademicPlannerStore(
        database.engine,
        confirmation_ttl_hours=settings.academic_confirmation_ttl_hours,
        default_practice_minutes=settings.academic_memory_default_practice_minutes,
    )
    notion_connector = None
    if settings.notion_token is not None and settings.notion_courses_database_id is not None:
        try:
            notion_connector = NotionConnector(
                token=settings.notion_token,
                courses_database_id=settings.notion_courses_database_id,
                timeout_seconds=settings.connector_timeout_seconds,
            )
        except (LifeAgentError, ValueError):
            notion_connector = None
    schedule_connector = None
    if settings.academic_schedule_ical_url is not None:
        try:
            schedule_connector = GoogleCalendarConnector(
                ical_url=settings.academic_schedule_ical_url,
                timeout_seconds=settings.academic_schedule_ical_timeout_seconds,
                max_response_bytes=settings.academic_schedule_ical_max_bytes,
            )
        except (LifeAgentError, ValueError):
            schedule_connector = None
    syncer = AcademicNotionSync(
        connector=notion_connector,
        store=store,
        discord=None,
        discord_channel_id=None,
        timezone=settings.app_timezone,
        clarification_ttl_hours=settings.academic_confirmation_ttl_hours,
        setup_condition_code=(
            "notion_configuration_missing"
            if settings.notion_token is None or settings.notion_courses_database_id is None
            else "notion_configuration_invalid"
        ),
        material_enqueuer=None,
        schedule_connector=schedule_connector,
        schedule_lookback_days=settings.academic_sync_lookback_days,
        schedule_horizon_days=max(11, settings.academic_plan_horizon_days),
    )
    semantic_gateway = LLMGateway(settings)
    semantic_interpreter = NightlyTaskSemanticInterpreter(
        semantic_gateway,
        max_prompt_chars=min(14_000, settings.calendar_semantic_prompt_max_chars),
    )
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
        catalog_syncer=syncer,
        candidate_catalog=store,
        semantic_interpreter=semantic_interpreter,
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


async def _nightly_preparation_failure(
    *,
    runtime: NightlyCheckinRuntime,
    occurrence: PeriodicOccurrence,
    period_key: str,
    attempt: int,
    attempt_limit: int,
    error_code: str,
) -> dict[str, object]:
    delivery_count = 0
    if attempt >= attempt_limit and runtime.delivery is not None:
        await runtime.delivery.send_scheduled_notification(
            "I couldn't safely prepare tonight's course-task check-in. Nothing was changed.",
            idempotency_key=build_idempotency_key(
                "academic-eod-failure",
                occurrence.local_time.date().isoformat(),
                occurrence.local_time.strftime("%H%M"),
            ),
        )
        delivery_count = 1
    return {
        "status": "failed",
        "error_code": error_code,
        "delivery_count": delivery_count,
        "conversation_status": "not_opened",
    }


async def _classify_candidates(
    interpreter: NightlySemanticInterpreter,
    *,
    period_key: str,
    candidates: tuple[Any, ...],
) -> tuple[list[NightlyChecklistItem], int]:
    accepted: list[NightlyChecklistItem] = []
    semantic_failures = 0
    for candidate in candidates:
        semantic_input = _semantic_input(candidate)
        try:
            outcome = await interpreter.analyze(semantic_input)
        except Exception:
            semantic_failures += 1
            continue
        if getattr(outcome, "status", None) in {
            NightlyTaskEligibilityStatus.UNAVAILABLE,
            NightlyTaskEligibilityStatus.INVALID,
        }:
            semantic_failures += 1
            continue
        if not bool(getattr(outcome, "movable", False)):
            continue
        result = getattr(outcome, "result", None)
        if result is None:
            semantic_failures += 1
            continue
        accepted.append(_checklist_item(period_key, candidate, outcome, semantic_input))
    return accepted, semantic_failures


def _semantic_input(candidate: Any) -> NightlyTaskSemanticInput:
    event_id = str(candidate.assessment_id)
    fingerprint_payload = {
        "assessment_id": event_id,
        "course_id": str(candidate.course_id),
        "title": str(candidate.title),
        "starts_at": candidate.starts_at.isoformat(),
        "ends_at": candidate.ends_at.isoformat() if candidate.ends_at is not None else None,
        "edited_at": candidate.source_last_edited_at.isoformat(),
        "semantic_fingerprint": candidate.semantic_source_fingerprint,
    }
    source_fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    fragments: list[NightlyTaskEvidenceFragment] = [
        NightlyTaskEvidenceFragment(
            fragment_id=f"{event_id}:host:course",
            event_id=event_id,
            source_kind=NightlyTaskEvidenceSourceKind.HOST_PROPERTY,
            source_label="Trusted course identity",
            text=f"{candidate.course_code} — {candidate.course_title}",
            ordinal=1,
        ),
        NightlyTaskEvidenceFragment(
            fragment_id=f"{event_id}:host:assessment-type-context",
            event_id=event_id,
            source_kind=NightlyTaskEvidenceSourceKind.HOST_PROPERTY,
            source_label="Assessment type context (not a routing rule)",
            text=str(candidate.assessment_type),
            ordinal=2,
        ),
    ]
    for label, value in (
        ("Validated semantic overview", candidate.semantic_overview),
        ("Validated semantic description", candidate.semantic_description),
        ("Validated semantic intent rationale", candidate.semantic_intent_rationale),
    ):
        if isinstance(value, str) and value.strip():
            fragments.append(
                NightlyTaskEvidenceFragment(
                    fragment_id=f"{event_id}:semantic:{len(fragments)}",
                    event_id=event_id,
                    source_kind=NightlyTaskEvidenceSourceKind.VALIDATED_SEMANTIC_CONTEXT,
                    source_label=label,
                    text=value.strip(),
                    ordinal=len(fragments) + 1,
                )
            )
    evidence = with_nightly_task_title_evidence(
        event_id=event_id,
        title=str(candidate.title),
        fragments=tuple(fragments),
    )
    return NightlyTaskSemanticInput(
        event_id=event_id,
        course_id=str(candidate.course_id),
        course_code=str(candidate.course_code),
        course_title=str(candidate.course_title),
        title=str(candidate.title),
        local_date_label=candidate.local_date.isoformat(),
        local_start_label=str(candidate.local_start_label),
        local_end_label=(
            str(candidate.local_end_label) if candidate.local_end_label is not None else None
        ),
        is_all_day=bool(candidate.is_all_day),
        is_range=candidate.ends_at is not None,
        source_fingerprint=source_fingerprint,
        source_last_edited_at=candidate.source_last_edited_at,
        evidence_fragments=evidence,
    )


def _checklist_item(
    period_key: str,
    candidate: Any,
    outcome: Any,
    semantic_input: NightlyTaskSemanticInput,
) -> NightlyChecklistItem:
    if candidate.is_all_day:
        date_range = NightlyTaskDateRange.from_values(
            start=candidate.starts_at.date(),
            ends_at=(candidate.ends_at.date() if candidate.ends_at is not None else None),
            all_day=True,
        )
    else:
        date_range = NightlyTaskDateRange.from_values(
            start=candidate.starts_at,
            ends_at=candidate.ends_at,
            all_day=False,
        )
    result = outcome.result
    item_id = stable_nightly_item_id(
        period_key=period_key,
        source_kind="notion_assessment",
        source_id=str(candidate.assessment_id),
        title=str(candidate.title),
        date_range=date_range,
    )
    return NightlyChecklistItem(
        item_id=item_id,
        course_id=str(candidate.course_id),
        course_code=str(candidate.course_code),
        course_title=str(candidate.course_title),
        source_kind="notion_assessment",
        source_id=str(candidate.assessment_id),
        title=str(candidate.title),
        date_range=date_range,
        semantic_decision=NightlySemanticDecision(
            kind="movable_work_task",
            accepted_by_critic=True,
            evidence_citations=tuple(result.evidence_fragment_ids),
            rationale=result.rationale,
            model_identity=outcome.model_identity or "unavailable",
            prompt_version=outcome.prompt_version,
            critic_version=outcome.critic_version,
            source_fingerprint=semantic_input.source_fingerprint,
        ),
        source_fingerprint=semantic_input.source_fingerprint,
        expected_last_edited_at=candidate.source_last_edited_at,
    )


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
