"""Model-reasoned, host-authoritative scheduled morning calendar briefing."""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.agents.academic_planner.sync import AcademicNotionSync, AcademicNotionSyncResult
from app.agents.calendar_briefing import (
    ActiveMorningCourse,
    CalendarActivityIntentStatus,
    CalendarEventEvidenceFragment,
    CalendarEventSemanticInput,
    CalendarEventSemanticInterpreter,
    CalendarEventSemanticStatus,
    CalendarEventSourceArea,
    CalendarEventSourceKind,
    MorningBriefingComposer,
    MorningBriefingDeliveryManifest,
    MorningCategory,
    MorningEmbedPayload,
    ScheduledMorningCalendarItem,
    build_morning_briefing_manifest,
    fingerprint_event_evidence,
    with_title_evidence_fragment,
)
from app.agents.calendar_briefing.semantic_interpreter import CALENDAR_SEMANTIC_PROMPT_VERSION
from app.agents.job_interviews.contracts import InterviewEventSnapshot, InterviewReminderFact
from app.agents.job_interviews.morning import (
    build_interview_reminder_facts,
    load_reminder_inputs,
)
from app.agents.job_interviews.sync import JobInterviewNotionSync
from app.artifacts.store import ArtifactStore
from app.core.config import Settings, get_settings
from app.core.errors import ErrorCode, LifeAgentError, transient_error
from app.db.academic import CalendarSemanticResultInput as AcademicCalendarSemanticResultInput
from app.db.academic import SQLAlchemyAcademicPlannerStore
from app.db.job_interviews import CalendarSemanticResultInput as JobCalendarSemanticResultInput
from app.db.job_interviews import SQLAlchemyJobInterviewStore
from app.db.models import AgentRun, StepStatus
from app.db.repositories import RunRepository
from app.db.session import Database
from app.llm.gateway import LLMGateway
from app.llm.ollama_runtime import OllamaRuntime, OllamaRuntimeError
from app.queue.idempotency import build_idempotency_key
from app.queue.periodic import PeriodicOccurrence, stable_period_key

_SOURCE_FRESHNESS = timedelta(minutes=5)


class ScheduledMorningSyncer(Protocol):
    async def sync(self, *, now: datetime | None = None) -> AcademicNotionSyncResult: ...


class ScheduledCareerSyncer(Protocol):
    async def sync(self, *, now: datetime | None = None) -> Any: ...


class ScheduledMorningStore(Protocol):
    def load_morning_calendar_items(
        self,
        *,
        occurrence: datetime,
        timezone: str,
    ) -> tuple[Mapping[str, Any], ...]: ...

    def load_active_morning_courses(self) -> tuple[Mapping[str, str], ...]: ...


class ScheduledMorningDelivery(Protocol):
    async def send_scheduled_notification(
        self,
        content: str,
        *,
        idempotency_key: str,
    ) -> object: ...

    async def send_scheduled_embed(
        self,
        embed: MorningEmbedPayload,
        *,
        idempotency_key: str,
    ) -> object: ...


class ScheduledMorningManifestStore(Protocol):
    def load(
        self, *, period_key: str
    ) -> tuple[MorningBriefingDeliveryManifest, set[int]] | None: ...

    def save(
        self,
        manifest: MorningBriefingDeliveryManifest,
        *,
        period_key: str,
        delivered_ordinals: set[int],
    ) -> None: ...


class CalendarEvidenceConnector(Protocol):
    async def retrieve_calendar_event_evidence(self, page_id: str) -> Any: ...


class ScheduledMorningProgress(Protocol):
    def record(
        self,
        phase: str,
        status: Literal["running", "succeeded", "failed"],
        *,
        attempt: int,
        diagnostic: str,
    ) -> None: ...


class _DatabaseProgressRecorder:
    """Persist bounded phase state without source text or model responses."""

    def __init__(self, *, engine: Engine, run_id: uuid.UUID) -> None:
        self._engine = engine
        self._run_id = run_id

    def record(
        self,
        phase: str,
        status: Literal["running", "succeeded", "failed"],
        *,
        attempt: int,
        diagnostic: str,
    ) -> None:
        step_status = {
            "running": StepStatus.RUNNING,
            "succeeded": StepStatus.SUCCEEDED,
            "failed": StepStatus.FAILED,
        }[status]
        now = datetime.now(UTC)
        with suppress(Exception), Session(self._engine) as session, session.begin():
            step = RunRepository.create_step_attempt(
                session,
                run_id=self._run_id,
                node_name=f"calendar_briefing.{phase}"[:128],
                attempt=attempt,
                status=step_status,
                diagnostic=diagnostic[:2_000],
            )
            RunRepository.update_step(
                session,
                step.id,
                step_status,
                started_at=now if status == "running" else None,
                ended_at=now if status != "running" else None,
                diagnostic=diagnostic[:2_000],
            )


class _ArtifactManifestStore:
    """Persist a complete rendered manifest on the durable run before any send."""

    def __init__(self, *, engine: Engine, run_id: uuid.UUID, artifact_root: Path) -> None:
        self._engine = engine
        self._run_id = run_id
        self._artifacts = ArtifactStore(artifact_root)

    def load(self, *, period_key: str) -> tuple[MorningBriefingDeliveryManifest, set[int]] | None:
        with Session(self._engine) as session:
            run = session.get(AgentRun, self._run_id)
            artifact_key = run.artifact_key if run is not None else None
        if not artifact_key:
            return None
        try:
            raw_payload: object = json.loads(self._artifacts.get(artifact_key))
            if not isinstance(raw_payload, dict):
                return None
            payload = cast(dict[str, object], raw_payload)
            if payload.get("period_key") != period_key:
                return None
            manifest = MorningBriefingDeliveryManifest.model_validate(payload.get("manifest"))
            raw_delivered = payload.get("delivered_ordinals", [])
            if not isinstance(raw_delivered, list):
                return None
            delivered_values = cast(list[object], raw_delivered)
            delivered = {
                int(value)
                for value in delivered_values
                if isinstance(value, int | str) and str(value).isdigit()
            }
            return manifest, delivered
        except (OSError, TypeError, ValueError, ValidationError, json.JSONDecodeError):
            return None

    def save(
        self,
        manifest: MorningBriefingDeliveryManifest,
        *,
        period_key: str,
        delivered_ordinals: set[int],
    ) -> None:
        payload = json.dumps(
            {
                "period_key": period_key,
                "manifest": manifest.model_dump(mode="json"),
                "delivered_ordinals": sorted(delivered_ordinals),
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        artifact = self._artifacts.put(
            payload,
            media_type="application/json",
            data_class="calendar_briefing_manifest",
            already_redacted=True,
        )
        with Session(self._engine) as session, session.begin():
            session.execute(
                update(AgentRun)
                .where(AgentRun.id == self._run_id)
                .values(artifact_key=artifact.key)
            )


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
    return build_idempotency_key("planner-morning-four-v3", local_date, local_time)


async def _deliver_manifest(
    delivery: ScheduledMorningDelivery,
    manifest: MorningBriefingDeliveryManifest,
    *,
    manifest_store: ScheduledMorningManifestStore | None,
    period_key: str,
    delivered_ordinals: set[int] | None = None,
) -> tuple[list[object], set[int]]:
    delivered = set(delivered_ordinals or ())
    receipts: list[object] = []
    for entry in manifest.entries:
        if entry.ordinal in delivered:
            continue
        receipt = await delivery.send_scheduled_embed(
            entry.embed,
            idempotency_key=entry.delivery_key,
        )
        receipts.append(receipt)
        delivered.add(entry.ordinal)
        if manifest_store is not None:
            manifest_store.save(
                manifest,
                period_key=period_key,
                delivered_ordinals=delivered,
            )
    return receipts, delivered


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


_SCHEDULED_ITEM_FIELDS = frozenset(ScheduledMorningCalendarItem.model_fields)


def _scheduled_item(
    raw: Mapping[str, Any],
    *,
    status: CalendarEventSemanticStatus | None = None,
    overview: str | None = None,
    description: str | None = None,
    evidence_ids: Sequence[str] = (),
    description_evidence_ids: Sequence[str] = (),
    intent_status: CalendarActivityIntentStatus | None = None,
    activity_intent: object = None,
    intent_evidence_ids: Sequence[str] = (),
    intent_rationale: str | None = None,
    schedule_context: str | None = None,
) -> ScheduledMorningCalendarItem:
    values = {key: value for key, value in raw.items() if key in _SCHEDULED_ITEM_FIELDS}
    if status is not None:
        values.update(
            {
                "semantic_status": status,
                "semantic_overview": overview,
                "semantic_description": description,
                "semantic_evidence_fragment_ids": tuple(evidence_ids),
                "semantic_description_fragment_ids": tuple(description_evidence_ids),
            }
        )
    if intent_status is not None:
        values.update(
            {
                "activity_intent": activity_intent,
                "intent_status": intent_status,
                "intent_evidence_fragment_ids": tuple(intent_evidence_ids),
                "intent_rationale": intent_rationale,
            }
        )
    if schedule_context is not None:
        values["schedule_context"] = schedule_context
    return ScheduledMorningCalendarItem.model_validate(values)


def _cache_is_exact(raw: Mapping[str, Any], interpreter: CalendarEventSemanticInterpreter) -> bool:
    raw_cache: object = raw.get("semantic_cache")
    source_edit = raw.get("source_last_edited_at")
    if not isinstance(raw_cache, Mapping):
        return False
    cache = cast(Mapping[str, object], raw_cache)
    status = raw.get("semantic_status")
    if status not in {CalendarEventSemanticStatus.VALID, "valid"}:
        return False
    return bool(
        cache.get("source_fingerprint")
        and cache.get("source_fingerprint") == raw.get("source_fingerprint")
        and cache.get("source_last_edited_at") == source_edit
        and cache.get("model_identity") == interpreter.model_identity
        and cache.get("config_version") == interpreter.config_version
        and cache.get("prompt_version") == CALENDAR_SEMANTIC_PROMPT_VERSION
    )


def _record_progress(
    progress: ScheduledMorningProgress | None,
    phase: str,
    status: Literal["running", "succeeded", "failed"],
    *,
    attempt: int,
    diagnostic: str,
) -> None:
    if progress is not None:
        progress.record(phase, status, attempt=attempt, diagnostic=diagnostic)


def _event_progress_phase(ordinal: int, phase: str) -> str:
    """Return a stable per-event phase without exposing calendar content."""

    if ordinal < 1:
        raise ValueError("calendar event progress ordinal must be positive")
    return f"event_{ordinal:03d}.{phase}"


async def _refresh_calendar_semantics(
    raw_items: Sequence[Mapping[str, Any]],
    *,
    connector: CalendarEvidenceConnector | None,
    interpreter: CalendarEventSemanticInterpreter | None,
    ollama_runtime: OllamaRuntime | None,
    academic_store: Any,
    career_store: Any,
    event_timeout_seconds: float,
    total_timeout_seconds: float,
    progress: ScheduledMorningProgress | None = None,
    attempt: int = 1,
) -> tuple[tuple[ScheduledMorningCalendarItem, ...], dict[str, int]]:
    counts = {
        "semantic_cache_hits": 0,
        "semantic_calls": 0,
        "valid_descriptions": 0,
        "no_description_decisions": 0,
        "invalid_semantics": 0,
        "unavailable_semantics": 0,
    }
    items: list[ScheduledMorningCalendarItem] = []
    pending = [
        raw for raw in raw_items if interpreter is None or not _cache_is_exact(raw, interpreter)
    ]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + total_timeout_seconds
    ready = (
        interpreter is not None
        and ollama_runtime is not None
        and (connector is not None or any(raw.get("inline_evidence") for raw in pending))
    )
    if ready and pending:
        try:
            await asyncio.wait_for(
                cast(OllamaRuntime, ollama_runtime).ensure_ready(),
                timeout=min(event_timeout_seconds, max(0.001, deadline - loop.time())),
            )
        except (OllamaRuntimeError, TimeoutError):
            ready = False

    for event_ordinal, raw in enumerate(raw_items, start=1):
        evidence_phase = _event_progress_phase(event_ordinal, "evidence_collection")
        semantic_phase = _event_progress_phase(event_ordinal, "semantic_interpretation")
        validation_phase = _event_progress_phase(event_ordinal, "semantic_validation")
        if (
            interpreter is not None
            and raw.get("source_area") != CalendarEventSourceArea.LEARN.value
            and _cache_is_exact(raw, interpreter)
        ):
            item = _scheduled_item(raw)
            items.append(item)
            counts["semantic_cache_hits"] += 1
            _record_progress(
                progress,
                semantic_phase,
                "succeeded",
                attempt=attempt,
                diagnostic=(
                    f"semantic_status:{item.semantic_status.value};error_code:none;source:cache"
                ),
            )
            if item.semantic_status == CalendarEventSemanticStatus.VALID:
                if item.semantic_description is None:
                    counts["no_description_decisions"] += 1
                else:
                    counts["valid_descriptions"] += 1
            else:
                counts["unavailable_semantics"] += 1
            continue
        inline_evidence = str(raw.get("inline_evidence") or "").strip()
        if (
            not ready
            or interpreter is None
            or (connector is None and not inline_evidence)
            or loop.time() >= deadline
        ):
            items.append(_scheduled_item(raw, status=CalendarEventSemanticStatus.UNAVAILABLE))
            counts["unavailable_semantics"] += 1
            _record_progress(
                progress,
                semantic_phase,
                "failed",
                attempt=attempt,
                diagnostic=(
                    "semantic_status:unavailable;error_code:calendar_semantic_model_unavailable"
                ),
            )
            continue

        event_id = str(raw["event_id"])
        source_edit = raw.get("source_last_edited_at")
        try:
            remaining = max(0.001, deadline - loop.time())
            async with asyncio.timeout(min(event_timeout_seconds, remaining)):
                _record_progress(
                    progress,
                    evidence_phase,
                    "running",
                    attempt=attempt,
                    diagnostic="collecting_event_evidence",
                )
                if inline_evidence:
                    fragments = (
                        CalendarEventEvidenceFragment(
                            fragment_id=f"{event_id}:google-ical:details",
                            event_id=event_id,
                            source_kind=CalendarEventSourceKind.PROPERTY,
                            source_label="Google Calendar details",
                            text=inline_evidence[:4_000],
                            ordinal=0,
                        ),
                    )
                else:
                    if connector is None:
                        raise ValueError("calendar evidence connector is unavailable")
                    collected = await connector.retrieve_calendar_event_evidence(event_id)
                    if (
                        getattr(collected, "event_id", None) != event_id
                        or getattr(collected, "last_edited_at", None) != source_edit
                    ):
                        raise ValueError("calendar source changed after metadata synchronization")
                    fragments = tuple(
                        CalendarEventEvidenceFragment.model_validate(
                            fragment.model_dump(mode="json")
                            if callable(getattr(fragment, "model_dump", None))
                            else fragment
                        )
                        for fragment in getattr(collected, "fragments", ())
                    )
                fragments = with_title_evidence_fragment(
                    event_id=event_id,
                    title=str(raw["title"]),
                    fragments=fragments,
                )
                schedule_context = next(
                    (
                        fragment.text
                        for fragment in fragments
                        if fragment.source_label.casefold()
                        in {"learn context", "google calendar details"}
                    ),
                    None,
                )
                _record_progress(
                    progress,
                    evidence_phase,
                    "succeeded",
                    attempt=attempt,
                    diagnostic=f"event_evidence_collected:{len(fragments)}",
                )
                fingerprint = fingerprint_event_evidence(fragments, event_id=event_id)
                semantic_input = CalendarEventSemanticInput(
                    event_id=event_id,
                    source_area=CalendarEventSourceArea(str(raw["source_area"])),
                    source_label=str(raw["source_label"]),
                    title=str(raw["title"]),
                    event_kind=str(raw["display_kind"]),
                    local_date_label=str(raw["local_start_label"]),
                    local_time_label=(
                        None if bool(raw.get("is_all_day")) else str(raw["local_start_label"])
                    ),
                    is_all_day=bool(raw.get("is_all_day")),
                    source_fingerprint=fingerprint,
                    source_last_edited_at=cast(datetime | None, source_edit),
                    evidence_fragments=fragments,
                )
                counts["semantic_calls"] += 1
                _record_progress(
                    progress,
                    semantic_phase,
                    "running",
                    attempt=attempt,
                    diagnostic="qwen_event_interpretation_started",
                )
                outcome = await interpreter.analyze(semantic_input)
        except (LifeAgentError, TimeoutError, TypeError, ValueError, ValidationError):
            _record_progress(
                progress,
                evidence_phase,
                "failed",
                attempt=attempt,
                diagnostic="error_code:calendar_semantic_evidence_unavailable",
            )
            _record_progress(
                progress,
                semantic_phase,
                "failed",
                attempt=attempt,
                diagnostic=(
                    "semantic_status:unavailable;error_code:calendar_semantic_evidence_unavailable"
                ),
            )
            items.append(_scheduled_item(raw, status=CalendarEventSemanticStatus.UNAVAILABLE))
            counts["unavailable_semantics"] += 1
            continue

        result = outcome.result
        _record_progress(
            progress,
            semantic_phase,
            ("succeeded" if outcome.status == CalendarEventSemanticStatus.VALID else "failed"),
            attempt=attempt,
            diagnostic=(
                f"semantic_status:{outcome.status.value};error_code:{outcome.error_code or 'none'}"
            ),
        )
        _record_progress(
            progress,
            validation_phase,
            "succeeded" if result is not None else "failed",
            attempt=attempt,
            diagnostic=(
                f"semantic_status:{outcome.status.value};error_code:{outcome.error_code or 'none'}"
            ),
        )
        prose_available = outcome.status == CalendarEventSemanticStatus.VALID
        overview = result.overview if result is not None and prose_available else None
        description = result.description if result is not None and prose_available else None
        evidence_ids = (
            result.evidence_fragment_ids if result is not None and prose_available else ()
        )
        description_ids = (
            result.description_fragment_ids if result is not None and prose_available else ()
        )
        item = _scheduled_item(
            raw,
            status=outcome.status,
            overview=overview,
            description=description,
            evidence_ids=evidence_ids,
            description_evidence_ids=description_ids,
            activity_intent=outcome.activity_intent,
            intent_status=outcome.intent_status,
            intent_evidence_ids=outcome.intent_evidence_fragment_ids,
            intent_rationale=outcome.intent_rationale,
            schedule_context=schedule_context,
        )
        items.append(item)
        if outcome.status == CalendarEventSemanticStatus.VALID:
            if description is None:
                counts["no_description_decisions"] += 1
            else:
                counts["valid_descriptions"] += 1
        elif outcome.status == CalendarEventSemanticStatus.INVALID:
            counts["invalid_semantics"] += 1
        else:
            counts["unavailable_semantics"] += 1

        if raw.get("source_area") in {"course", "misc", "learn"}:
            saver = getattr(academic_store, "save_assessment_calendar_semantics", None)
            if callable(saver):
                with suppress(Exception):
                    saver(
                        event_id,
                        _semantic_result_input(
                            AcademicCalendarSemanticResultInput,
                            outcome=outcome,
                            semantic_input=semantic_input,
                            overview=overview,
                            description=description,
                            evidence_ids=evidence_ids,
                            description_ids=description_ids,
                            analyzed_at=datetime.now(UTC),
                        ),
                    )
        else:
            saver = getattr(career_store, "save_interview_calendar_semantics", None)
            if callable(saver):
                with suppress(Exception):
                    saver(
                        event_id,
                        _semantic_result_input(
                            JobCalendarSemanticResultInput,
                            outcome=outcome,
                            semantic_input=semantic_input,
                            overview=overview,
                            description=description,
                            evidence_ids=evidence_ids,
                            description_ids=description_ids,
                            analyzed_at=datetime.now(UTC),
                        ),
                    )
    return tuple(items), counts


def _semantic_result_input(
    input_type: type[Any],
    *,
    outcome: Any,
    semantic_input: CalendarEventSemanticInput,
    overview: str | None,
    description: str | None,
    evidence_ids: Sequence[str],
    description_ids: Sequence[str],
    analyzed_at: datetime,
) -> Any:
    intent_value = outcome.activity_intent.value if outcome.activity_intent is not None else None
    kwargs: dict[str, Any] = {
        "status": outcome.status.value,
        "source_fingerprint": semantic_input.source_fingerprint,
        "source_last_edited_at": semantic_input.source_last_edited_at,
        "model_identity": outcome.model_identity or "unknown",
        "config_version": outcome.config_version or "unknown",
        "prompt_version": outcome.prompt_version,
        "analyzed_at": analyzed_at,
        "overview": overview,
        "description": description,
        "evidence_ids": evidence_ids,
        "description_evidence_ids": description_ids,
        "activity_intent": intent_value,
        "intent_value": intent_value,
        "intent_status": outcome.intent_status.value,
        "intent_rationale": outcome.intent_rationale,
        "intent_evidence_ids": outcome.intent_evidence_fragment_ids,
    }
    try:
        accepted = set(inspect.signature(input_type).parameters)
    except (TypeError, ValueError):
        accepted = set(kwargs)
    return input_type(**{key: value for key, value in kwargs.items() if key in accepted})


def _attention_message(error_code: ErrorCode, occurrence: PeriodicOccurrence) -> str:
    schedule_label = occurrence.local_time.strftime("%H:%M")
    if error_code is ErrorCode.SCHEDULE_LATE:
        return (
            f"LifeAgent missed the {schedule_label} academic notification window, so no stale "
            "morning calendar was sent. Please check the academic worker and queue health."
        )
    if error_code is ErrorCode.SOURCE_SETUP_REQUIRED:
        return (
            "LifeAgent could not refresh the Notion academic source, so it did not send a "
            "possibly stale calendar. Please check the Notion token, Courses database, and sharing."
        )
    if error_code is ErrorCode.SOURCE_SYNC_PARTIAL:
        return (
            "LifeAgent found an incomplete Notion academic refresh, so it did not send a "
            "possibly incomplete calendar. Please review the academic source diagnostics."
        )
    if error_code is ErrorCode.SOURCE_STALE:
        return (
            "LifeAgent could not prove the academic source was freshly synchronized, so it did "
            "not send a morning calendar. Please check Notion sync health."
        )
    if error_code is ErrorCode.DELIVERY_CONTENT_TOO_LONG:
        return (
            "LifeAgent's morning calendar does not fit safely in one Discord message, "
            "so nothing was omitted. Please review today's calendar in the operations console."
        )
    return (
        "LifeAgent could not refresh the Notion academic source, so it did not send a possibly "
        "stale calendar. Please check Notion and academic worker health."
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


def _load_morning_calendar_items(
    owner: object,
    *,
    occurrence: datetime,
    timezone: str,
) -> tuple[Mapping[str, Any], ...]:
    loader = getattr(owner, "load_morning_calendar_items", None)
    if not callable(loader):
        return ()
    loaded: object = loader(occurrence=occurrence, timezone=timezone)
    if not isinstance(loaded, Sequence):
        return ()
    loaded_items = cast(Sequence[object], loaded)
    return tuple(
        cast(Mapping[str, Any], item) for item in loaded_items if isinstance(item, Mapping)
    )


def _load_active_courses(owner: object) -> tuple[ActiveMorningCourse, ...]:
    loader = getattr(owner, "load_active_morning_courses", None)
    if not callable(loader):
        return ()
    loaded: object = loader()
    if not isinstance(loaded, Sequence):
        return ()
    return tuple(
        ActiveMorningCourse.model_validate(item)
        for item in cast(Sequence[object], loaded)
        if isinstance(item, Mapping)
    )


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
    attempt: int = 1,
    attempt_limit: int = 3,
    career_store: Any | None = None,
    career_syncer: ScheduledCareerSyncer | None = None,
    evidence_connector: CalendarEvidenceConnector | None = None,
    semantic_interpreter: CalendarEventSemanticInterpreter | None = None,
    ollama_runtime: OllamaRuntime | None = None,
    manifest_store: ScheduledMorningManifestStore | None = None,
    semantic_event_timeout_seconds: float = 180.0,
    semantic_total_timeout_seconds: float = 600.0,
    progress: ScheduledMorningProgress | None = None,
    morning_composer: MorningBriefingComposer | None = None,
) -> dict[str, object]:
    """Refresh, allocate, persist, format, and deliver one scheduled local period."""

    current = _aware(executed_at, "executed_at").astimezone(UTC)
    _period_parts(period_key, occurrence)
    if not 1 <= catchup_grace_minutes <= 180:
        raise ValueError("catchup_grace_minutes must be between 1 and 180")
    if attempt < 1 or attempt_limit < attempt:
        raise ValueError("attempt values are invalid")
    if current < occurrence.scheduled_at:
        raise ValueError("scheduled notification cannot execute before its occurrence")
    existing_manifest = manifest_store.load(period_key=period_key) if manifest_store else None
    if existing_manifest is not None:
        if delivery is None:
            return {
                "status": "failed",
                "error_code": ErrorCode.AUTHORIZATION_INVALID.value,
                "delivery_count": 0,
                "part_count": len(existing_manifest[0].entries),
            }
        manifest, delivered_ordinals = existing_manifest
        _record_progress(
            progress,
            "delivery",
            "running",
            attempt=attempt,
            diagnostic="resuming_persisted_manifest",
        )
        try:
            receipts, delivered = await _deliver_manifest(
                delivery,
                manifest,
                manifest_store=manifest_store,
                period_key=period_key,
                delivered_ordinals=delivered_ordinals,
            )
        except Exception:
            _record_progress(
                progress,
                "delivery",
                "failed",
                attempt=attempt,
                diagnostic="multipart_delivery_failed",
            )
            raise
        _record_progress(
            progress,
            "delivery",
            "succeeded",
            attempt=attempt,
            diagnostic=f"manifest_parts_delivered:{len(delivered)}",
        )
        return {
            "status": "succeeded",
            "resumed_manifest": True,
            "part_count": len(manifest.entries),
            "delivery_count": len(receipts),
            "delivered_part_count": len(delivered),
        }
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
        }

    _record_progress(
        progress,
        "source_refresh",
        "running",
        attempt=attempt,
        diagnostic="academic_source_refresh_started",
    )
    sync_result: AcademicNotionSyncResult | None = None
    academic_available = False
    academic_condition = "Fresh Notion academic data was unavailable."
    unavailable_academic_roles: set[str] = set()
    try:
        sync_result = await syncer.sync(now=current)
    except Exception:
        if attempt < attempt_limit:
            raise
    if sync_result is not None and sync_result.status not in {"succeeded", "partial"}:
        error_code = _sync_error_code(sync_result)
        if sync_result.retryable and attempt < attempt_limit:
            raise transient_error(error_code, "academic source refresh is temporarily unavailable")
        academic_condition = "The Notion academic refresh did not complete safely."
    elif sync_result is not None and sync_result.synced_at is not None:
        synced_at = _aware(sync_result.synced_at, "synced_at").astimezone(UTC)
        academic_available = abs(current - synced_at) <= _SOURCE_FRESHNESS
        if not academic_available:
            academic_condition = "Freshness of the Notion academic source could not be proven."
        elif sync_result.status == "partial":
            unavailable_academic_roles = {role.value for role in sync_result.unavailable_roles}
            if not unavailable_academic_roles:
                academic_available = False
                academic_condition = "The Notion academic refresh was incomplete."
    _record_progress(
        progress,
        "source_refresh",
        "succeeded" if academic_available else "failed",
        attempt=attempt,
        diagnostic=(
            "academic_source_refresh_succeeded"
            if academic_available
            else "academic_source_unavailable_no_stale_data_used"
        ),
    )

    active_courses: tuple[ActiveMorningCourse, ...] | None = None
    raw_academic_items: tuple[Mapping[str, Any], ...] = ()
    if academic_available:
        try:
            active_courses = _load_active_courses(store)
        except Exception:
            unavailable_academic_roles.add("course")
        try:
            raw_academic_items = _load_morning_calendar_items(
                store,
                occurrence=occurrence.scheduled_at,
                timezone=timezone_name,
            )
        except Exception:
            academic_available = False
            academic_condition = "Fresh Notion calendar facts could not be loaded."
    interview_reminders: tuple[InterviewReminderFact, ...] = ()
    interview_events: tuple[InterviewEventSnapshot, ...] = ()
    raw_job_items: tuple[Mapping[str, Any], ...] = ()
    career_condition: str | None = None
    career_sync_status = "unconfigured"
    if career_store is not None:
        try:
            if career_syncer is not None:
                career_result = await career_syncer.sync(now=current)
                career_sync_status = str(getattr(career_result, "status", "failed"))
            else:
                career_sync_status = "cached"
            if career_sync_status in {"succeeded", "cached"}:
                raw_job_items = _load_morning_calendar_items(
                    career_store,
                    occurrence=occurrence.scheduled_at,
                    timezone=timezone_name,
                )
                loaded_interviews, loaded_plans = load_reminder_inputs(
                    career_store,
                    now=occurrence.scheduled_at,
                )
                in_window_ids = {str(item["event_id"]) for item in raw_job_items}
                interview_events = tuple(
                    item for item in loaded_interviews if item.interview_page_id in in_window_ids
                )
                interview_reminders = build_interview_reminder_facts(
                    interview_events,
                    now=occurrence.scheduled_at,
                    plans=loaded_plans,
                    timezone_name=timezone_name,
                )
            else:
                career_condition = "The Jobs Notion refresh did not complete safely."
        except Exception:
            career_sync_status = "failed"
            career_condition = "Fresh Jobs data was unavailable."
    calendar_items, semantic_counts = await _refresh_calendar_semantics(
        (*raw_academic_items, *raw_job_items),
        connector=evidence_connector,
        interpreter=semantic_interpreter,
        ollama_runtime=ollama_runtime,
        academic_store=store,
        career_store=career_store,
        event_timeout_seconds=semantic_event_timeout_seconds,
        total_timeout_seconds=semantic_total_timeout_seconds,
        progress=progress,
        attempt=attempt,
    )
    academic_items = tuple(item for item in calendar_items if item.source_area.value == "course")
    misc_items = tuple(item for item in calendar_items if item.source_area.value == "misc")
    job_items = tuple(item for item in calendar_items if item.source_area.value == "jobs")
    schedule_items = tuple(item for item in calendar_items if item.source_area.value == "learn")

    course_composition = None
    job_composition = None
    misc_composition = None
    schedule_composition = None
    if morning_composer is not None:
        task_categories = (
            (
                MorningCategory.COURSES,
                academic_items,
                academic_available and "course" not in unavailable_academic_roles,
            ),
            (MorningCategory.JOBS, job_items, career_condition is None),
            (
                MorningCategory.MISC,
                misc_items,
                academic_available and "misc" not in unavailable_academic_roles,
            ),
        )
        task_compositions: dict[MorningCategory, object] = {}
        for category, items, source_available in task_categories:
            eligible = tuple(
                item
                for item in items
                if item.semantic_status is CalendarEventSemanticStatus.VALID
                and item.semantic_overview is not None
            )
            if not source_available or not eligible:
                continue
            phase = f"spoken_composition.{category.value}"
            _record_progress(
                progress,
                phase,
                "running",
                attempt=attempt,
                diagnostic=f"spoken_task_generation_started:{len(eligible)}",
            )
            try:
                composition = await morning_composer.compose_spoken_tasks(category, eligible)
            except Exception:
                composition = None
            if composition is None:
                _record_progress(
                    progress,
                    phase,
                    "failed",
                    attempt=attempt,
                    diagnostic=f"spoken_task_generation_unavailable:{len(eligible)}",
                )
                continue
            task_compositions[category] = composition
            accepted_count = len(
                tuple(
                    clause
                    for clause in composition.clauses
                    if clause.action_phrase is not None
                )
            )
            _record_progress(
                progress,
                phase,
                "succeeded",
                attempt=attempt,
                diagnostic=(
                    f"spoken_tasks_accepted:{accepted_count};"
                    f"spoken_tasks_total:{len(eligible)}"
                ),
            )
        course_composition = task_compositions.get(MorningCategory.COURSES)
        job_composition = task_compositions.get(MorningCategory.JOBS)
        misc_composition = task_compositions.get(MorningCategory.MISC)
        if academic_available and "learn" not in unavailable_academic_roles and schedule_items:
            with suppress(Exception):
                schedule_composition = await morning_composer.compose_schedule(schedule_items)

    unavailable: dict[MorningCategory, str] = {}
    if not academic_available:
        unavailable.update(
            {
                MorningCategory.COURSES: academic_condition,
                MorningCategory.MISC: academic_condition,
                MorningCategory.SCHEDULE: academic_condition,
            }
        )
    else:
        role_categories = {
            "course": MorningCategory.COURSES,
            "misc": MorningCategory.MISC,
            "learn": MorningCategory.SCHEDULE,
        }
        for role in unavailable_academic_roles:
            category = role_categories.get(role)
            if category is not None:
                unavailable[category] = (
                    "The fresh Google iCal schedule did not validate completely."
                    if role == "learn"
                    else f"The fresh Notion {role} calendar did not validate completely."
                )
    if career_condition is not None:
        unavailable[MorningCategory.JOBS] = career_condition

    if delivery is None:
        return {
            "status": "failed",
            "error_code": ErrorCode.AUTHORIZATION_INVALID.value,
            "sync_status": sync_result.status if sync_result is not None else "failed",
            "delivery_count": 0,
            "interview_count": len(interview_reminders),
            "academic_event_count": len(academic_items),
            "misc_event_count": len(misc_items),
            "job_event_count": len(job_items),
        }
    _record_progress(
        progress,
        "manifest_creation",
        "running",
        attempt=attempt,
        diagnostic="rendering_delivery_manifest",
    )
    try:
        manifest = build_morning_briefing_manifest(
            local_date=occurrence.local_time.astimezone(ZoneInfo(timezone_name)).date(),
            delivery_key_prefix=scheduled_delivery_key(period_key, occurrence),
            timezone_name=timezone_name,
            active_courses=active_courses,
            course_items=academic_items,
            job_items=job_items,
            misc_items=misc_items,
            schedule_items=schedule_items,
            course_composition=course_composition,
            job_composition=job_composition,
            misc_composition=misc_composition,
            schedule_composition=schedule_composition,
            unavailable=unavailable,
        )
        if manifest_store is not None:
            manifest_store.save(manifest, period_key=period_key, delivered_ordinals=set())
    except Exception:
        _record_progress(
            progress,
            "manifest_creation",
            "failed",
            attempt=attempt,
            diagnostic="delivery_manifest_failed",
        )
        raise
    _record_progress(
        progress,
        "manifest_creation",
        "succeeded",
        attempt=attempt,
        diagnostic=f"persisted_manifest_categories:{len(manifest.entries)}",
    )
    _record_progress(
        progress,
        "delivery",
        "running",
        attempt=attempt,
        diagnostic="four_embed_delivery_started",
    )
    try:
        receipts, delivered_ordinals = await _deliver_manifest(
            delivery,
            manifest,
            manifest_store=manifest_store,
            period_key=period_key,
        )
    except Exception:
        _record_progress(
            progress,
            "delivery",
            "failed",
            attempt=attempt,
            diagnostic="four_embed_delivery_failed",
        )
        raise
    _record_progress(
        progress,
        "delivery",
        "succeeded",
        attempt=attempt,
        diagnostic=f"manifest_parts_delivered:{len(delivered_ordinals)}",
    )
    reminder_audit_status = "not_applicable"
    if interview_reminders and career_store is not None:
        recorder = getattr(career_store, "record_reminder_delivery", None)
        if callable(recorder):
            reminder_audit_status = "recorded"
            try:
                raw_delivery_id = getattr(receipts[-1], "id", None) if receipts else None
                delivery_id = raw_delivery_id if isinstance(raw_delivery_id, uuid.UUID) else None
                for item in interview_reminders:
                    recorder(
                        item,
                        status="sent",
                        included_at=current,
                        delivery_id=delivery_id,
                    )
            except Exception:
                reminder_audit_status = "failed"
    return {
        "status": "succeeded",
        "interview_count": len(interview_reminders),
        "academic_event_count": len(academic_items),
        "misc_event_count": len(misc_items),
        "job_event_count": len(job_items),
        "schedule_event_count": len(schedule_items),
        "career_sync_status": career_sync_status,
        "sync_status": sync_result.status if sync_result is not None else "failed",
        "part_count": len(manifest.entries),
        "delivery_count": len(receipts),
        "delivered_part_count": len(delivered_ordinals),
        "delivery_status": str(getattr(receipts[-1], "status", "sent")),
        "reminder_audit_status": reminder_audit_status,
        **semantic_counts,
    }


class _Runtime:
    def __init__(
        self,
        *,
        database: Database,
        settings: Settings,
        store: ScheduledMorningStore,
        syncer: ScheduledMorningSyncer,
        career_store: SQLAlchemyJobInterviewStore,
        career_syncer: JobInterviewNotionSync,
        delivery: ScheduledMorningDelivery | None,
        evidence_connector: CalendarEvidenceConnector | None,
        semantic_interpreter: CalendarEventSemanticInterpreter,
        ollama_runtime: OllamaRuntime,
        manifest_store: ScheduledMorningManifestStore,
        progress: ScheduledMorningProgress,
        morning_composer: MorningBriefingComposer,
    ) -> None:
        self.database = database
        self.settings = settings
        self.store = store
        self.syncer = syncer
        self.career_store = career_store
        self.career_syncer = career_syncer
        self.delivery = delivery
        self.evidence_connector = evidence_connector
        self.semantic_interpreter = semantic_interpreter
        self.ollama_runtime = ollama_runtime
        self.manifest_store = manifest_store
        self.progress = progress
        self.morning_composer = morning_composer


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
    schedule_connector = None
    if settings.academic_schedule_ical_url is not None:
        from app.connectors.google_calendar import GoogleCalendarConnector

        try:
            schedule_connector = GoogleCalendarConnector(
                ical_url=settings.academic_schedule_ical_url,
                timeout_seconds=settings.academic_schedule_ical_timeout_seconds,
                max_response_bytes=settings.academic_schedule_ical_max_bytes,
            )
        except (LifeAgentError, ValueError):
            schedule_connector = None
    syncer = AcademicNotionSync(
        connector=connector,
        store=store,
        discord=None,
        discord_channel_id=None,
        timezone=settings.app_timezone,
        clarification_ttl_hours=settings.academic_confirmation_ttl_hours,
        setup_condition_code=setup_condition,
        material_enqueuer=None,
        schedule_connector=schedule_connector,
        schedule_lookback_days=settings.academic_sync_lookback_days,
        schedule_horizon_days=max(11, settings.academic_plan_horizon_days),
    )
    career_store = SQLAlchemyJobInterviewStore(database.engine)
    career_syncer = JobInterviewNotionSync(
        connector=connector,
        store=career_store,
        timezone=settings.app_timezone,
        setup_condition_code=setup_condition,
    )
    gateway = LLMGateway(settings)
    semantic_interpreter = CalendarEventSemanticInterpreter(
        gateway,
        max_prompt_chars=settings.calendar_semantic_prompt_max_chars,
    )
    morning_composer = MorningBriefingComposer(gateway)
    ollama_runtime = OllamaRuntime(settings)
    manifest_store = _ArtifactManifestStore(
        engine=database.engine,
        run_id=run_id,
        artifact_root=settings.artifact_root,
    )
    progress = _DatabaseProgressRecorder(engine=database.engine, run_id=run_id)
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
        career_store=career_store,
        career_syncer=career_syncer,
        delivery=delivery,
        evidence_connector=connector,
        semantic_interpreter=semantic_interpreter,
        ollama_runtime=ollama_runtime,
        manifest_store=manifest_store,
        progress=progress,
        morning_composer=morning_composer,
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
            attempt=attempt,
            attempt_limit=attempt_limit,
            career_store=runtime.career_store,
            career_syncer=runtime.career_syncer,
            evidence_connector=runtime.evidence_connector,
            semantic_interpreter=runtime.semantic_interpreter,
            ollama_runtime=runtime.ollama_runtime,
            manifest_store=runtime.manifest_store,
            semantic_event_timeout_seconds=(
                runtime.settings.calendar_semantic_event_timeout_seconds
            ),
            semantic_total_timeout_seconds=(
                runtime.settings.calendar_semantic_total_timeout_seconds
            ),
            progress=runtime.progress,
            morning_composer=runtime.morning_composer,
        )
    finally:
        runtime.database.dispose()


__all__ = [
    "ScheduledCareerSyncer",
    "ScheduledMorningDelivery",
    "ScheduledMorningStore",
    "ScheduledMorningSyncer",
    "execute_scheduled_morning_notification",
    "run_scheduled_morning_notification",
    "scheduled_attention_key",
    "scheduled_delivery_key",
]
