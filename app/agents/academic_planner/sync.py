"""Notion course-calendar synchronization and Discord clarification orchestration."""

from __future__ import annotations

import hashlib
import re
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy.exc import NoResultFound

from app.agents.academic_planner.classification import (
    AssessmentKind,
    canonical_title_previews,
    classify_assessment_label,
)
from app.connectors.discord import (
    AcademicClarificationMessage,
    AcademicSetupReminderMessage,
    DiscordDeliveryReceipt,
)
from app.connectors.discord_gateway import (
    DiscordClarificationAction,
    DiscordClarificationCallbackResult,
    DiscordClarificationInteraction,
)
from app.connectors.notion import (
    NotionAssessment,
    NotionConnector,
    NotionCourse,
    NotionDiscoveryDiagnostic,
    NotionWriteConflict,
)
from app.core.errors import ErrorCode, LifeAgentError
from app.queue.retry import RetryClassification, classify_retry_error

_DELIVERY_NAMESPACE = uuid.UUID("b31aee13-f134-4980-93bd-6e86d493f36a")
_SAFE_COURSE_CODE = re.compile(r"[A-Za-z0-9._ -]{1,40}")
_CLARIFICATION_ACTIONS: tuple[DiscordClarificationAction, ...] = (
    "quiz",
    "assignment",
    "tutorial",
    "lab",
    "studying_block",
)
_CLARIFICATION_LABELS: dict[str, str] = {
    "quiz": "Quiz",
    "assignment": "Assignment",
    "tutorial": "Tutorial",
    "lab": "Lab",
    "studying_block": "Studying Block",
}
_SETUP_LABELS: dict[str, str] = {
    "notion_configuration_missing": "missing Notion token or Courses database ID",
    "notion_configuration_invalid": "invalid Courses database configuration",
    "courses_database_unavailable": "inaccessible or unshared Courses database",
    "courses_data_source_missing": "missing Courses data source",
    "courses_data_source_duplicate": "duplicate Courses data sources",
    "data_source_missing": "missing database data source",
    "data_source_duplicate": "duplicate database data sources",
    "assessment_calendar_missing": "missing seeded Assessments calendar",
    "assessment_calendar_duplicate": "duplicate matching child calendars",
    "assessment_calendar_inaccessible": "inaccessible seeded Assessments calendar",
    "assessment_schema_malformed": "malformed Assessments calendar schema",
    "assessment_name_property_invalid": "missing or invalid Name property",
    "assessment_date_property_invalid": "missing or invalid Date property",
    "course_persistence_failed": "course calendar persistence failed",
    "course_discovery_failed": "course calendar discovery failed",
    "notion_sync_failed": "Courses database synchronization failed",
}


class AcademicSyncStore(Protocol):
    """Durable persistence required by the synchronization boundary."""

    def save_sync_cursor(self, database: str, cursor: str | None) -> None: ...

    def upsert_course_calendar(
        self,
        course: Any,
        *,
        status: str = "valid",
        diagnostic_code: str | None = None,
        schema_fingerprint: str | None = None,
    ) -> str: ...

    def upsert_synced_assessment(
        self,
        course: Any,
        assessment: Any,
        *,
        kind: str,
        label_source: str | None = None,
    ) -> str: ...

    def reconcile_assessment_source(
        self,
        source_id: str,
        seen_ids: Sequence[str],
        *,
        synced_at: datetime | None = None,
    ) -> int: ...

    def expire_clarifications(self, *, now: datetime | None = None) -> int: ...

    def create_or_get_clarification(self, **kwargs: Any) -> str: ...

    def get_clarification(self, clarification_id: uuid.UUID | str) -> Mapping[str, Any] | None: ...

    def mark_clarification_delivered(
        self,
        clarification_id: uuid.UUID | str,
        *,
        delivery_id: str,
        delivered_at: datetime | None = None,
    ) -> None: ...

    def claim_clarification(
        self,
        clarification_id: uuid.UUID | str,
        action: DiscordClarificationAction,
        actor_id: int,
        *,
        now: datetime | None = None,
    ) -> tuple[str, dict[str, Any]]: ...

    def mark_clarification_applied(
        self, clarification_id: uuid.UUID | str, *, applied_at: datetime | None = None
    ) -> None: ...

    def mark_clarification_conflict(
        self, clarification_id: uuid.UUID | str, *, error_code: str
    ) -> None: ...

    def mark_clarification_failed(
        self, clarification_id: uuid.UUID | str, *, error_code: str
    ) -> None: ...

    def setup_reminder_due(self, condition_code: str, fingerprint: str, day: date) -> bool: ...

    def record_setup_reminder(
        self,
        condition_code: str,
        fingerprint: str,
        day: date,
        *,
        affected_course_codes: Sequence[str] = (),
        delivered_at: datetime | None = None,
        delivery_id: str | None = None,
        error_code: str | None = None,
    ) -> None: ...

    def clear_setup_reminders(
        self, condition_code: str | None = None, fingerprint: str | None = None
    ) -> int: ...


class AcademicInteractionDelivery(Protocol):
    async def send_clarification(
        self, message: AcademicClarificationMessage
    ) -> DiscordDeliveryReceipt: ...

    async def send_setup_reminder(
        self, message: AcademicSetupReminderMessage
    ) -> DiscordDeliveryReceipt: ...


@dataclass(frozen=True, slots=True)
class AcademicNotionSyncResult:
    """Bounded synchronization summary safe for APIs, jobs, and health logs."""

    status: Literal["succeeded", "partial", "setup_required"]
    course_count: int = 0
    valid_course_count: int = 0
    assessment_count: int = 0
    archived_count: int = 0
    clarification_count: int = 0
    material_job_count: int = 0
    invalid_calendar_count: int = 0
    diagnostic_codes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "course_count": self.course_count,
            "valid_course_count": self.valid_course_count,
            "assessment_count": self.assessment_count,
            "archived_count": self.archived_count,
            "clarification_count": self.clarification_count,
            "material_job_count": self.material_job_count,
            "invalid_calendar_count": self.invalid_calendar_count,
            "diagnostic_codes": list(self.diagnostic_codes),
        }


@dataclass(frozen=True, slots=True)
class _CourseRecord:
    course_id: str
    notion_id: str
    course_page_id: str
    course_code: str
    course_title: str
    title: str
    term: str
    priority: int
    active: bool
    child_database_id: str | None
    child_data_source_id: str | None
    assessments_database_id: str | None
    assessments_source_id: str | None
    title_property_id: str | None
    title_property_name: str | None
    date_property_id: str | None
    date_property_name: str | None


@dataclass(frozen=True, slots=True)
class _AssessmentRecord:
    notion_id: str
    page_id: str
    title: str
    current_title: str
    due_at: datetime | None
    ends_at: datetime | None
    weight: float | None
    estimated_minutes: int
    status: str | None
    fact_state: str
    ambiguity_reason: str | None
    confidence: float
    completed: bool
    source_id: str
    assessments_source_id: str
    title_property_id: str
    notion_last_edited_at: datetime
    last_edited_at: datetime
    source_url: str | None
    active: bool
    archived: bool


class AcademicNotionSync:
    """Synchronize discovered course calendars without exposing vendor envelopes."""

    def __init__(
        self,
        *,
        connector: NotionConnector | None,
        store: AcademicSyncStore,
        discord: AcademicInteractionDelivery | None = None,
        discord_channel_id: str | None = None,
        timezone: str = "America/Toronto",
        clarification_ttl_hours: int = 24,
        setup_condition_code: str = "notion_configuration_missing",
        material_enqueuer: Callable[[str, str], Awaitable[object]] | None = None,
    ) -> None:
        self._connector = connector
        self._store = store
        self._discord = discord
        self._discord_channel_id = discord_channel_id
        self._timezone = ZoneInfo(timezone)
        self._clarification_ttl = timedelta(hours=clarification_ttl_hours)
        self._setup_condition_code = setup_condition_code
        self._material_enqueuer = material_enqueuer

    @property
    def connector(self) -> NotionConnector | None:
        return self._connector

    async def sync(self, *, now: datetime | None = None) -> AcademicNotionSyncResult:
        current = _aware(now or datetime.now(UTC))
        self._store.expire_clarifications(now=current)
        if self._connector is None:
            diagnostic = NotionDiscoveryDiagnostic(
                code=self._setup_condition_code,
                severity="error",
                message="Notion Courses database configuration requires attention",
            )
            await self._deliver_setup_conditions((diagnostic,), current)
            return AcademicNotionSyncResult(
                status="setup_required",
                invalid_calendar_count=1,
                diagnostic_codes=(diagnostic.code,),
            )
        try:
            result = await self._connector.discover_course_assessments()
        except (LifeAgentError, ValueError, TypeError):
            diagnostic = NotionDiscoveryDiagnostic(
                code="notion_sync_failed",
                severity="error",
                message="Courses database synchronization failed",
            )
            await self._deliver_setup_conditions((diagnostic,), current)
            return AcademicNotionSyncResult(
                status="setup_required",
                invalid_calendar_count=1,
                diagnostic_codes=(diagnostic.code,),
            )

        diagnostics = list(result.diagnostics)
        if result.courses_source_id is not None:
            self._store.save_sync_cursor(
                result.courses_source_id,
                result.synced_at.isoformat(),
            )
        course_diagnostics = _diagnostics_by_course(diagnostics)
        valid_courses = 0
        assessment_count = 0
        archived_count = 0
        clarification_count = 0
        material_job_count = 0
        invalid_calendars = 0
        for course in result.courses:
            course_record = _course_record(course)
            invalid = _course_setup_diagnostic(
                course,
                course_diagnostics.get(course.course_page_id, ()),
            )
            try:
                if invalid is not None:
                    invalid_calendars += 1
                    self._store.upsert_course_calendar(
                        course_record,
                        status=_calendar_status(invalid.code),
                        diagnostic_code=invalid.code,
                        schema_fingerprint=_diagnostic_fingerprint(invalid),
                    )
                    continue
                self._store.upsert_course_calendar(course_record)
                valid_courses += 1
                seen: list[str] = []
                for assessment in course.assessments:
                    seen.append(assessment.page_id)
                    record, classification = _assessment_record(
                        assessment,
                        timezone=self._timezone,
                    )
                    assessment_row_id = self._store.upsert_synced_assessment(
                        course_record,
                        record,
                        kind=_synced_kind_value(classification.kind),
                        label_source=classification.source,
                    )
                    assessment_count += 1
                    if self._material_enqueuer is not None and record.active:
                        from app.agents.academic_planner.material_ingestion import (
                            assessment_material_fingerprint,
                        )

                        try:
                            await self._material_enqueuer(
                                assessment.page_id,
                                assessment_material_fingerprint(
                                    assessment.page_id,
                                    assessment.last_edited_at,
                                ),
                            )
                            material_job_count += 1
                        except Exception:
                            diagnostics.append(
                                NotionDiscoveryDiagnostic(
                                    code="assessment_material_enqueue_failed",
                                    severity="warning",
                                    message="Assessment material ingestion could not be queued",
                                    course_page_id=course.course_page_id,
                                    course_title=course.course_title,
                                )
                            )
                    if (
                        _classification_kind_value(classification.kind)
                        == AssessmentKind.UNKNOWN.value
                        and record.active
                        and record.current_title
                    ):
                        created = await self._create_and_deliver_clarification(
                            course=course_record,
                            assessment=assessment,
                            assessment_row_id=assessment_row_id,
                            now=current,
                        )
                        clarification_count += int(created)
                source_id = course.assessments_source_id or course_record.child_data_source_id
                if source_id:
                    archived_count += self._store.reconcile_assessment_source(
                        source_id,
                        seen,
                        synced_at=result.synced_at,
                    )
                    self._store.save_sync_cursor(source_id, result.synced_at.isoformat())
            except Exception:
                invalid_calendars += 1
                diagnostics.append(
                    NotionDiscoveryDiagnostic(
                        code="course_persistence_failed",
                        severity="error",
                        message="A discovered course could not be persisted",
                        course_page_id=course.course_page_id,
                        course_title=course.course_title,
                    )
                )
                continue

        await self._deliver_setup_conditions(tuple(diagnostics), current)
        codes = tuple(sorted({item.code for item in diagnostics if item.severity == "error"}))
        top_level_setup_failure = any(
            code
            in {
                "courses_database_unavailable",
                "courses_data_source_missing",
                "courses_data_source_duplicate",
            }
            for code in codes
        )
        if top_level_setup_failure or (not result.courses and codes):
            status: Literal["succeeded", "partial", "setup_required"] = "setup_required"
        elif invalid_calendars or codes:
            status = "partial"
        else:
            status = "succeeded"
        return AcademicNotionSyncResult(
            status=status,
            course_count=len(result.courses),
            valid_course_count=valid_courses,
            assessment_count=assessment_count,
            archived_count=archived_count,
            clarification_count=clarification_count,
            material_job_count=material_job_count,
            invalid_calendar_count=invalid_calendars,
            diagnostic_codes=codes,
        )

    async def _create_and_deliver_clarification(
        self,
        *,
        course: _CourseRecord,
        assessment: NotionAssessment,
        assessment_row_id: str,
        now: datetime,
    ) -> bool:
        previews = _canonical_clarification_previews(assessment.current_title)
        idempotency_key = (
            "academic-label:"
            + hashlib.sha256(
                "\x1f".join(
                    (
                        assessment.page_id,
                        assessment.current_title,
                        assessment.last_edited_at.isoformat(),
                        assessment.title_property_id,
                    )
                ).encode("utf-8")
            ).hexdigest()
        )
        clarification_id = self._store.create_or_get_clarification(
            event_notion_id=assessment.page_id,
            original_title=assessment.current_title,
            raw_label=assessment.current_title,
            quiz_preview_title=previews["quiz"],
            assignment_preview_title=previews["assignment"],
            tutorial_preview_title=previews["tutorial"],
            lab_preview_title=previews["lab"],
            studying_block_preview_title=previews["studying_block"],
            expected_edited_at=assessment.last_edited_at,
            expires_at=now + self._clarification_ttl,
            idempotency_key=idempotency_key,
            course_id=None,
            assessment_id=assessment_row_id,
            title_property_id=assessment.title_property_id,
        )
        stored = self._store.get_clarification(clarification_id)
        if stored is None or stored.get("delivered_at") is not None:
            return False
        if self._discord is None or self._discord_channel_id is None:
            return True
        clarification_uuid = uuid.UUID(clarification_id)
        try:
            receipt = await self._discord.send_clarification(
                AcademicClarificationMessage(
                    delivery_id=uuid.uuid5(
                        _DELIVERY_NAMESPACE, f"clarification:{clarification_id}"
                    ),
                    clarification_id=clarification_uuid,
                    channel_id=self._discord_channel_id,
                    current_title=assessment.current_title[:500],
                    quiz_title_preview=previews["quiz"][:500],
                    assignment_title_preview=previews["assignment"][:500],
                    tutorial_title_preview=previews["tutorial"][:500],
                    lab_title_preview=previews["lab"][:500],
                    studying_block_title_preview=previews["studying_block"][:500],
                )
            )
        except LifeAgentError:
            return True
        self._store.mark_clarification_delivered(
            clarification_uuid,
            delivery_id=receipt.external_id,
            delivered_at=now,
        )
        return True

    async def _deliver_setup_conditions(
        self,
        diagnostics: Sequence[NotionDiscoveryDiagnostic],
        now: datetime,
    ) -> None:
        grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
        for diagnostic in diagnostics:
            if diagnostic.severity != "error" or diagnostic.code not in _SETUP_LABELS:
                continue
            fingerprint = _diagnostic_fingerprint(diagnostic)
            if diagnostic.course_title:
                code = diagnostic.course_title.strip()
                if _SAFE_COURSE_CODE.fullmatch(code):
                    grouped[(diagnostic.code, fingerprint)].append(code)
                else:
                    grouped[(diagnostic.code, fingerprint)]
            else:
                grouped[(diagnostic.code, fingerprint)]
        active_codes = {code for code, _ in grouped}
        for resolved_code in _SETUP_LABELS.keys() - active_codes:
            self._store.clear_setup_reminders(resolved_code)
        local_day = now.astimezone(self._timezone).date()
        for (code, fingerprint), course_codes in grouped.items():
            if not self._store.setup_reminder_due(code, fingerprint, local_day):
                continue
            bounded_codes = tuple(sorted(set(course_codes)))[:10]
            delivery_id = uuid.uuid5(
                _DELIVERY_NAMESPACE,
                f"setup:{code}:{fingerprint}:{local_day.isoformat()}",
            )
            if self._discord is None or self._discord_channel_id is None:
                self._store.record_setup_reminder(
                    code,
                    fingerprint,
                    local_day,
                    affected_course_codes=bounded_codes,
                    delivery_id=str(delivery_id),
                    error_code="discord_unavailable",
                )
                continue
            try:
                receipt = await self._discord.send_setup_reminder(
                    AcademicSetupReminderMessage(
                        delivery_id=delivery_id,
                        channel_id=self._discord_channel_id,
                        condition=_SETUP_LABELS[code],
                        affected_course_codes=bounded_codes,
                    )
                )
            except LifeAgentError as exc:
                self._store.record_setup_reminder(
                    code,
                    fingerprint,
                    local_day,
                    affected_course_codes=bounded_codes,
                    delivery_id=str(delivery_id),
                    error_code=exc.record.code.value,
                )
                continue
            self._store.record_setup_reminder(
                code,
                fingerprint,
                local_day,
                affected_course_codes=bounded_codes,
                delivered_at=now,
                delivery_id=receipt.external_id,
            )


class AcademicClarificationService:
    """Apply one authorized Discord selection as a guarded title-only rename."""

    def __init__(
        self,
        *,
        store: AcademicSyncStore,
        connector: NotionConnector,
        syncer: AcademicNotionSync | None = None,
    ) -> None:
        self._store = store
        self._connector = connector
        self._syncer = syncer

    async def __call__(
        self,
        interaction: DiscordClarificationInteraction,
    ) -> DiscordClarificationCallbackResult:
        return await self.apply_choice(
            clarification_id=str(interaction.clarification_id),
            action=interaction.action,
            user_id=interaction.user_id,
        )

    async def apply_choice(
        self,
        *,
        clarification_id: str,
        action: DiscordClarificationAction,
        user_id: str,
        attempt: int = 1,
        attempt_limit: int = 1,
    ) -> DiscordClarificationCallbackResult:
        if attempt < 1 or attempt_limit < 1:
            raise ValueError("attempt and attempt_limit must be positive")
        try:
            actor_id = int(user_id)
        except ValueError:
            return DiscordClarificationCallbackResult(status="invalid")
        try:
            status, request = self._store.claim_clarification(
                clarification_id,
                action,
                actor_id,
            )
        except (KeyError, ValueError, NoResultFound):
            return DiscordClarificationCallbackResult(status="invalid")
        if status == "ignored":
            return DiscordClarificationCallbackResult(status="ignored")
        if status == "claimed" and request.get("decision") == action:
            status = "ready"
        if status != "ready":
            return DiscordClarificationCallbackResult(status="duplicate")
        title_property_id = request.get("title_property_id")
        original_title = request.get("original_title")
        expected_edited_at = request.get("expected_edited_at")
        new_title = _clarification_preview_title(request, action)
        page_id = request.get("event_notion_id")
        if not (
            isinstance(title_property_id, str)
            and title_property_id
            and isinstance(original_title, str)
            and original_title
            and isinstance(new_title, str)
            and new_title
            and isinstance(page_id, str)
            and page_id
        ):
            self._store.mark_clarification_failed(
                clarification_id,
                error_code="clarification_state_invalid",
            )
            return DiscordClarificationCallbackResult(status="failed")
        try:
            edited_at = _aware(
                datetime.fromisoformat(str(expected_edited_at).replace("Z", "+00:00"))
            )
        except ValueError:
            self._store.mark_clarification_failed(
                clarification_id,
                error_code="clarification_state_invalid",
            )
            return DiscordClarificationCallbackResult(status="failed")
        try:
            await self._connector.rename_assessment_title(
                page_id=page_id,
                title_property_id=title_property_id,
                expected_title=original_title,
                expected_last_edited_at=edited_at,
                new_title=new_title,
            )
        except NotionWriteConflict as exc:
            if exc.current is not None and exc.current.current_title == new_title:
                self._store.mark_clarification_applied(clarification_id)
                return DiscordClarificationCallbackResult(status="handled")
            self._store.mark_clarification_conflict(
                clarification_id,
                error_code="notion_precondition_failed",
            )
            return DiscordClarificationCallbackResult(status="failed")
        except LifeAgentError as exc:
            if (
                classify_retry_error(exc) is RetryClassification.TRANSIENT
                and attempt < attempt_limit
            ):
                raise
            self._store.mark_clarification_failed(
                clarification_id,
                error_code=exc.record.code.value,
            )
            return DiscordClarificationCallbackResult(status="failed")
        except Exception as exc:
            if (
                classify_retry_error(exc) is RetryClassification.TRANSIENT
                and attempt < attempt_limit
            ):
                raise
            self._store.mark_clarification_failed(
                clarification_id,
                error_code=ErrorCode.INTERNAL.value,
            )
            return DiscordClarificationCallbackResult(status="failed")
        self._store.mark_clarification_applied(clarification_id)
        return DiscordClarificationCallbackResult(status="handled")


def _course_record(course: NotionCourse) -> _CourseRecord:
    priority = _bounded_number(course.priority, minimum=0, maximum=100, default=50)
    source_id = getattr(course, "child_data_source_id", None) or course.assessments_source_id
    database_id = getattr(course, "child_database_id", None) or course.assessments_database_id
    return _CourseRecord(
        course_id=course.course_page_id,
        notion_id=course.course_page_id,
        course_page_id=course.course_page_id,
        course_code=course.course_title,
        course_title=course.course_title,
        title=course.course_title,
        term=course.term or "unspecified",
        priority=int(priority),
        active=not (course.archived or course.in_trash),
        child_database_id=database_id,
        child_data_source_id=source_id,
        assessments_database_id=database_id,
        assessments_source_id=source_id,
        title_property_id=getattr(course, "title_property_id", None),
        title_property_name=getattr(course, "title_property_name", None),
        date_property_id=getattr(course, "date_property_id", None),
        date_property_name=getattr(course, "date_property_name", None),
    )


def _assessment_record(
    assessment: NotionAssessment,
    *,
    timezone: ZoneInfo,
) -> tuple[_AssessmentRecord, Any]:
    existing_kind = _normalized_property(assessment.properties, "type")
    classification = classify_assessment_label(
        assessment.current_title,
        trusted_existing_kind=existing_kind if isinstance(existing_kind, str) else None,
    )
    due_at = _parse_due(assessment, timezone=timezone)
    ends_at = _parse_end(assessment, timezone=timezone)
    reasons: list[str] = []
    if _classification_kind_value(classification.kind) == AssessmentKind.UNKNOWN.value:
        reasons.append(
            "Confirm whether this assessment is a Quiz, Assignment, Tutorial, Lab, "
            "or Studying Block."
        )
    if due_at is None:
        reasons.append("Confirm the assessment deadline in Notion.")
        ends_at = None
    elif ends_at is not None and ends_at <= due_at:
        reasons.append(
            "Confirm the assessment date range in Notion; the end must be after the start."
        )
        ends_at = None
    ambiguous = bool(reasons)
    estimated = _bounded_number(
        assessment.estimated_minutes,
        minimum=1,
        maximum=10_080,
        default=60,
    )
    weight = _optional_bounded_number(assessment.weight, minimum=0, maximum=100)
    archived = assessment.archived or assessment.in_trash
    return (
        _AssessmentRecord(
            notion_id=assessment.page_id,
            page_id=assessment.page_id,
            title=assessment.current_title,
            current_title=assessment.current_title,
            due_at=due_at,
            ends_at=ends_at,
            weight=weight,
            estimated_minutes=int(estimated),
            status=assessment.status,
            fact_state="ambiguous" if ambiguous else "confirmed",
            ambiguity_reason=" ".join(reasons) or None,
            confidence=0.0 if ambiguous else 1.0,
            completed=(assessment.status or "").casefold() in {"completed", "done"},
            source_id=assessment.assessments_source_id,
            assessments_source_id=assessment.assessments_source_id,
            title_property_id=assessment.title_property_id,
            notion_last_edited_at=assessment.last_edited_at,
            last_edited_at=assessment.last_edited_at,
            source_url=assessment.source_url,
            active=not archived,
            archived=archived,
        ),
        classification,
    )


def _classification_kind_value(kind: Any) -> str:
    value = getattr(kind, "value", kind)
    return str(value)


def _synced_kind_value(kind: Any) -> str:
    value = _classification_kind_value(kind)
    return "event" if value == AssessmentKind.UNKNOWN.value else value


def _canonical_clarification_previews(label: str) -> dict[str, str]:
    previews: dict[str, str] = {}
    try:
        raw_previews = canonical_title_previews(label)
    except Exception:
        raw_previews = {}
    for key, value in getattr(raw_previews, "items", lambda: ())():
        preview_key = _classification_kind_value(key)
        if preview_key in _CLARIFICATION_LABELS:
            previews[preview_key] = str(value)[:500]
    body = _clarification_title_body(label)
    for action in _CLARIFICATION_ACTIONS:
        previews.setdefault(action, f"{_CLARIFICATION_LABELS[action]} — {body}"[:500])
    return previews


def _clarification_preview_title(
    request: Mapping[str, Any],
    action: DiscordClarificationAction,
) -> str | None:
    if action == "ignore":
        return None
    preview = request.get(f"{action}_preview_title")
    if isinstance(preview, str) and preview:
        return preview
    original_title = request.get("original_title")
    if not isinstance(original_title, str) or not original_title:
        return None
    return _canonical_clarification_previews(original_title)[action]


def _clarification_title_body(label: str) -> str:
    prefixes = "|".join(re.escape(value) for value in _CLARIFICATION_LABELS.values())
    without_prefix = re.sub(
        rf"^\s*(?:{prefixes})\s+[—-]\s*",
        "",
        label,
        count=1,
        flags=re.IGNORECASE,
    ).strip()
    return without_prefix or "Untitled assessment"


def _parse_due(assessment: NotionAssessment, *, timezone: ZoneInfo) -> datetime | None:
    if assessment.due is None or assessment.due.start is None:
        return None
    raw = assessment.due.start
    try:
        if "T" not in raw:
            parsed_date = date.fromisoformat(raw)
            return datetime.combine(parsed_date, time(23, 59), tzinfo=timezone).astimezone(UTC)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=timezone)
        return parsed.astimezone(UTC)
    except ValueError:
        return None


def _parse_end(assessment: NotionAssessment, *, timezone: ZoneInfo) -> datetime | None:
    if assessment.due is None or assessment.due.end is None:
        return None
    raw = assessment.due.end
    try:
        if "T" not in raw:
            parsed_date = date.fromisoformat(raw)
            return datetime.combine(parsed_date, time(23, 59), tzinfo=timezone).astimezone(UTC)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=timezone)
        return parsed.astimezone(UTC)
    except ValueError:
        return None


def _normalized_property(properties: Mapping[str, Any], expected: str) -> Any:
    normalized_expected = re.sub(r"[^a-z0-9]+", "", expected.casefold())
    matches = [
        value
        for key, value in properties.items()
        if re.sub(r"[^a-z0-9]+", "", key.casefold()) == normalized_expected
    ]
    return matches[0] if len(matches) == 1 else None


def _course_setup_diagnostic(
    course: NotionCourse,
    diagnostics: Sequence[NotionDiscoveryDiagnostic],
) -> NotionDiscoveryDiagnostic | None:
    relevant = [item for item in diagnostics if item.severity == "error"]
    if relevant:
        return relevant[0]
    source_id = getattr(course, "child_data_source_id", None) or course.assessments_source_id
    database_id = getattr(course, "child_database_id", None) or course.assessments_database_id
    if not database_id or not source_id:
        return NotionDiscoveryDiagnostic(
            code="assessment_calendar_missing",
            severity="error",
            message="Course page does not contain a valid seeded Assessments calendar",
            course_page_id=course.course_page_id,
            course_title=course.course_title,
        )
    return None


def _diagnostics_by_course(
    diagnostics: Sequence[NotionDiscoveryDiagnostic],
) -> dict[str, tuple[NotionDiscoveryDiagnostic, ...]]:
    grouped: dict[str, list[NotionDiscoveryDiagnostic]] = defaultdict(list)
    for diagnostic in diagnostics:
        if diagnostic.course_page_id:
            grouped[diagnostic.course_page_id].append(diagnostic)
    return {key: tuple(value) for key, value in grouped.items()}


def _calendar_status(code: str) -> str:
    if code.endswith("duplicate"):
        return "duplicate"
    if code.endswith("inaccessible"):
        return "inaccessible"
    if "malformed" in code or "property_invalid" in code:
        return "malformed"
    return "missing"


def _diagnostic_fingerprint(diagnostic: NotionDiscoveryDiagnostic) -> str:
    return hashlib.sha256(
        "\x1f".join(
            filter(
                None,
                (
                    diagnostic.code,
                    diagnostic.source_type,
                    diagnostic.source_id,
                    diagnostic.course_page_id,
                    diagnostic.property_id,
                    diagnostic.property_name,
                ),
            )
        ).encode("utf-8")
    ).hexdigest()


def _bounded_number(
    value: str | float | int | None,
    *,
    minimum: float,
    maximum: float,
    default: float,
) -> float:
    candidate = _optional_bounded_number(value, minimum=minimum, maximum=maximum)
    return default if candidate is None else candidate


def _optional_bounded_number(
    value: str | float | int | None,
    *,
    minimum: float,
    maximum: float,
) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        candidate = float(value)
    except (TypeError, ValueError):
        return None
    return candidate if minimum <= candidate <= maximum else None


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "AcademicClarificationService",
    "AcademicInteractionDelivery",
    "AcademicNotionSync",
    "AcademicNotionSyncResult",
    "AcademicSyncStore",
]
