"""Notion Jobs/interview synchronization boundary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal, Protocol, cast
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter

from app.agents.action_items import DateOnlyValue, DateTimeValue, TemporalValue
from app.connectors.notion import (
    NotionApplicationRecord,
    NotionConnector,
    NotionDateValue,
    NotionDiscoveryDiagnostic,
    NotionInterviewRecord,
)
from app.core.errors import ErrorCode, LifeAgentError
from app.db.job_interviews import (
    CareerApplicationInput,
    InterviewEventInput,
    JobsWorkspaceInput,
)

_TEMPORAL_VALUE_ADAPTER: TypeAdapter[TemporalValue] = TypeAdapter(TemporalValue)


class JobInterviewSyncStore(Protocol):
    """Adapter around the career repository/session used by synchronization."""

    def save_sync_cursor(self, source_id: str, cursor: str | None) -> None: ...

    def upsert_jobs_workspace(self, workspace: JobsWorkspaceInput) -> Any: ...

    def upsert_career_application(
        self,
        workspace_id: Any,
        application: CareerApplicationInput,
    ) -> Any: ...

    def deactivate_missing_career_applications(
        self,
        workspace_id: Any,
        seen_application_ids: set[str],
    ) -> int: ...

    def upsert_interview_event(self, workspace_id: Any, interview: InterviewEventInput) -> Any: ...

    def deactivate_missing_interviews(self, workspace_id: Any, seen_page_ids: set[str]) -> int: ...


@dataclass(frozen=True, slots=True)
class JobInterviewSyncResult:
    """Bounded synchronization summary safe for jobs, APIs, and health logs."""

    status: Literal["succeeded", "partial", "setup_required", "failed"]
    table_count: int = 0
    application_row_count: int = 0
    interview_count: int = 0
    unscheduled_interview_count: int = 0
    inactive_application_row_count: int = 0
    inactive_interview_count: int = 0
    diagnostic_codes: tuple[str, ...] = ()
    synced_at: datetime | None = None
    error_code: str | None = None
    retryable: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "table_count": self.table_count,
            "application_row_count": self.application_row_count,
            "interview_count": self.interview_count,
            "unscheduled_interview_count": self.unscheduled_interview_count,
            "inactive_application_row_count": self.inactive_application_row_count,
            "inactive_interview_count": self.inactive_interview_count,
            "diagnostic_codes": list(self.diagnostic_codes),
            "synced_at": self.synced_at.isoformat() if self.synced_at is not None else None,
            "error_code": self.error_code,
            "retryable": self.retryable,
        }


class JobInterviewNotionSync:
    """Synchronize Jobs tables and Interviews events without exposing vendor envelopes."""

    def __init__(
        self,
        *,
        connector: NotionConnector | None,
        store: JobInterviewSyncStore,
        timezone: str = "America/Toronto",
        setup_condition_code: str = "notion_configuration_missing",
    ) -> None:
        self._connector = connector
        self._store = store
        self._timezone = ZoneInfo(timezone)
        self._setup_condition_code = setup_condition_code

    async def sync(self, *, now: datetime | None = None) -> JobInterviewSyncResult:
        current = _aware(now or datetime.now(UTC))
        if self._connector is None:
            diagnostic = NotionDiscoveryDiagnostic(
                code=self._setup_condition_code,
                severity="error",
                message="Notion Courses database configuration requires attention",
            )
            _record_diagnostic(self._store, diagnostic, synced_at=current)
            return JobInterviewSyncResult(
                status="setup_required",
                diagnostic_codes=(diagnostic.code,),
                error_code=ErrorCode.SOURCE_SETUP_REQUIRED.value,
                synced_at=current,
            )
        try:
            applications = await self._connector.read_applications()
            interviews = await self._connector.read_interviews()
        except LifeAgentError as exc:
            diagnostic = NotionDiscoveryDiagnostic(
                code="jobs_notion_sync_failed",
                severity="error",
                message="Career Applications/Interviews synchronization failed",
            )
            _record_diagnostic(self._store, diagnostic, synced_at=current)
            return JobInterviewSyncResult(
                status="failed" if exc.record.retryable else "setup_required",
                diagnostic_codes=(diagnostic.code,),
                error_code=exc.record.code.value,
                retryable=exc.record.retryable,
                synced_at=current,
            )
        except (TypeError, ValueError):
            diagnostic = NotionDiscoveryDiagnostic(
                code="jobs_notion_sync_failed",
                severity="error",
                message="Career Applications/Interviews synchronization failed",
            )
            _record_diagnostic(self._store, diagnostic, synced_at=current)
            return JobInterviewSyncResult(
                status="setup_required",
                diagnostic_codes=(diagnostic.code,),
                error_code=ErrorCode.SOURCE_SETUP_REQUIRED.value,
                synced_at=current,
            )
        return self._persist_records(applications, interviews, synced_at=current)

    def _persist_records(
        self,
        applications: Sequence[NotionApplicationRecord],
        interviews: Sequence[NotionInterviewRecord],
        *,
        synced_at: datetime,
    ) -> JobInterviewSyncResult:
        diagnostics: list[NotionDiscoveryDiagnostic] = []
        workspace = _workspace_record(interviews, synced_at=synced_at)
        workspace_result = self._store.upsert_jobs_workspace(workspace)
        workspace_id = _store_identity(workspace_result)

        application_row_count = 0
        seen_application_ids: set[str] = set()
        for index, application in enumerate(applications):
            record = _application_record(application, row_order=index)
            self._store.upsert_career_application(workspace_id, record)
            seen_application_ids.add(record.application_id)
            application_row_count += 1
        inactive_application_row_count = self._store.deactivate_missing_career_applications(
            workspace_id,
            seen_application_ids,
        )

        interview_count = 0
        unscheduled_interview_count = 0
        seen_interview_ids: set[str] = set()
        for interview in interviews:
            record, schedule_diagnostic = _interview_record(
                interview,
                timezone=self._timezone,
            )
            if schedule_diagnostic is not None or record is None:
                if schedule_diagnostic is not None:
                    diagnostics.append(schedule_diagnostic)
                    _record_diagnostic(self._store, schedule_diagnostic, synced_at=synced_at)
                unscheduled_interview_count += 1
                continue
            self._store.upsert_interview_event(workspace_id, record)
            seen_interview_ids.add(interview.page_id)
            interview_count += 1
        inactive_interview_count = self._store.deactivate_missing_interviews(
            workspace_id,
            seen_interview_ids,
        )
        for source_id in _source_ids(applications, interviews):
            self._store.save_sync_cursor(source_id, synced_at.isoformat())

        warning_codes = _warning_codes(diagnostics)
        status = "partial" if warning_codes or unscheduled_interview_count else "succeeded"
        return JobInterviewSyncResult(
            status=status,
            table_count=0,
            application_row_count=application_row_count,
            interview_count=interview_count,
            unscheduled_interview_count=unscheduled_interview_count,
            inactive_application_row_count=inactive_application_row_count,
            inactive_interview_count=inactive_interview_count,
            diagnostic_codes=warning_codes,
            synced_at=synced_at,
            error_code=(ErrorCode.SOURCE_SYNC_PARTIAL.value if status == "partial" else None),
        )


def _workspace_record(
    interviews: Sequence[NotionInterviewRecord],
    *,
    synced_at: datetime,
) -> JobsWorkspaceInput:
    interview = next(iter(interviews), None)
    return JobsWorkspaceInput(
        jobs_page_title="Jobs",
        discovery_status="valid",
        interviews_database_id=interview.database_id if interview is not None else None,
        interviews_data_source_id=interview.source_id if interview is not None else None,
        title_property_id=interview.property_ids.get("Name") if interview is not None else None,
        title_property_name="Name" if interview is not None else None,
        date_property_id=interview.property_ids.get("Date") if interview is not None else None,
        date_property_name="Date" if interview is not None else None,
        discovered_at=synced_at,
        synced_at=synced_at,
    )


def _application_record(
    application: NotionApplicationRecord,
    *,
    row_order: int,
) -> CareerApplicationInput:
    timezone = (
        application.next_action_due.time_zone
        if application.next_action_due is not None and application.next_action_due.time_zone
        else "America/Toronto"
    )
    temporal = _temporal_value(
        application.next_action_due,
        timezone=timezone,
    )
    content_fingerprint = _content_fingerprint(
        application.page_id,
        application.company,
        application.role,
        application.pipeline_status,
        application.next_action,
        temporal.model_dump(mode="json") if temporal else None,
        application.posting_url,
        application.last_edited_at.isoformat(),
    )
    return CareerApplicationInput(
        application_id=application.page_id,
        applications_database_id=application.database_id,
        applications_data_source_id=application.source_id,
        company_name=application.company,
        role_title=application.role,
        status=application.pipeline_status,
        next_action=application.next_action,
        next_action_temporal=temporal,
        posting_url=application.posting_url,
        applied_on=_date_value_start(application.applied_on),
        deadline=_date_value_start(application.deadline),
        property_snapshot=_jsonable_mapping(application.properties),
        source_url=application.source_url,
        content_fingerprint=content_fingerprint,
        last_edited_at=application.last_edited_at,
        row_order=row_order,
        active=(
            (application.active is not False)
            and not application.archived
            and not application.in_trash
        ),
        archived=application.archived or application.in_trash,
    )


def _date_value_start(value: NotionDateValue | None) -> date | None:
    temporal = (
        _temporal_value(value, timezone=value.time_zone or "America/Toronto") if value else None
    )
    if isinstance(temporal, DateOnlyValue):
        return temporal.start_date
    if isinstance(temporal, DateTimeValue):
        return temporal.start_at.astimezone(ZoneInfo(temporal.timezone)).date()
    return None


def _interview_record(
    interview: NotionInterviewRecord,
    *,
    timezone: ZoneInfo,
) -> tuple[InterviewEventInput | None, NotionDiscoveryDiagnostic | None]:
    starts_at, local_day, all_day, state, reason = _parse_interview_date(interview, timezone)
    if state != "scheduled" or local_day is None:
        return None, NotionDiscoveryDiagnostic(
            code=f"interview_{state}",
            severity="warning",
            message=reason or "Interview date needs attention",
            source_id=interview.source_id,
            source_type=interview.source_type,
            course_page_id=interview.page_id,
            course_title=interview.title[:255] or None,
            property_id=interview.property_ids.get("Date"),
            property_name="Date",
        )
    archived = interview.archived or interview.in_trash
    return (
        InterviewEventInput(
            interview_page_id=interview.page_id,
            title=interview.title or "Untitled interview",
            local_date=local_day,
            notion_last_edited_at=interview.last_edited_at,
            content_fingerprint=_interview_fingerprint(interview),
            date_start=starts_at,
            is_all_day=all_day,
            timezone=str(timezone),
            temporal_value=_interview_temporal_value(
                local_day=local_day,
                starts_at=starts_at,
                all_day=all_day,
                timezone=str(timezone),
            ),
            application_id=interview.application_ids[0] if interview.application_ids else None,
            stage=interview.stage,
            interview_status=interview.status,
            preparation_status=interview.prep_status,
            interviews_database_id=interview.database_id,
            interviews_data_source_id=interview.source_id,
            source_url=interview.source_url,
            tags=(),
            property_snapshot=_jsonable_mapping(interview.properties),
            url_candidates=_url_candidates(interview),
            active=not archived,
            archived=archived,
        ),
        None,
    )


def _parse_interview_date(
    interview: NotionInterviewRecord,
    timezone: ZoneInfo,
) -> tuple[
    datetime | None,
    date | None,
    bool,
    Literal["scheduled", "date_missing", "date_invalid"],
    str | None,
]:
    if interview.date is None or interview.date.start is None:
        return None, None, False, "date_missing", "Interview Date is missing in Notion."
    raw = interview.date.start
    try:
        if "T" not in raw:
            local_day = date.fromisoformat(raw)
            return None, local_day, True, "scheduled", None
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=timezone)
        starts_at = parsed.astimezone(UTC)
        return starts_at, starts_at.astimezone(timezone).date(), False, "scheduled", None
    except ValueError:
        return (
            None,
            None,
            False,
            "date_invalid",
            "Interview Date is malformed or ambiguous in Notion.",
        )


def _first_text(source: Any, *names: str) -> str | None:
    for name in names:
        value = getattr(source, name, None)
        if value is None and isinstance(source, Mapping):
            value = source.get(name)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text[:2_048]
    return None


def _first_datetime(source: Any, *names: str) -> datetime | None:
    for name in names:
        value = getattr(source, name, None)
        if value is None and isinstance(source, Mapping):
            value = source.get(name)
        if isinstance(value, datetime):
            return _aware(value)
        if isinstance(value, str) and value.strip():
            try:
                return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
            except ValueError:
                continue
    return None


def _temporal_value(value: Any, *, timezone: str) -> TemporalValue | None:
    if value is None:
        return None
    if isinstance(value, DateOnlyValue | DateTimeValue):
        return value
    if isinstance(value, NotionDateValue):
        return _temporal_value(value.start, timezone=value.time_zone or timezone)
    if isinstance(value, Mapping):
        try:
            return _TEMPORAL_VALUE_ADAPTER.validate_python(value)
        except ValueError:
            return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return DateOnlyValue(start_date=value)
    if isinstance(value, datetime):
        parsed = (
            value.replace(tzinfo=ZoneInfo(timezone))
            if value.tzinfo is None or value.utcoffset() is None
            else value
        )
        return DateTimeValue(
            start_at=_aware(parsed),
            timezone=timezone,
        )
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            if "T" not in text:
                return DateOnlyValue(start_date=date.fromisoformat(text))
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
            return DateTimeValue(
                start_at=parsed,
                timezone=timezone,
            )
        except ValueError:
            return None
    return None


def _interview_temporal_value(
    *,
    local_day: date,
    starts_at: datetime | None,
    all_day: bool,
    timezone: str,
) -> TemporalValue:
    if all_day or starts_at is None:
        return DateOnlyValue(start_date=local_day)
    return DateTimeValue(
        start_at=starts_at,
        timezone=timezone,
    )


def _workspace_status(diagnostics: Sequence[NotionDiscoveryDiagnostic]) -> str:
    codes = {item.code for item in diagnostics if item.severity == "error"}
    if "jobs_page_duplicate" in codes:
        return "duplicate"
    if "jobs_page_missing" in codes:
        return "missing"
    if "courses_database_unavailable" in codes:
        return "inaccessible"
    if codes:
        return "malformed"
    return "valid"


def _tags(properties: Mapping[str, Any]) -> tuple[str, ...]:
    value = properties.get("Tags")
    if not isinstance(value, tuple):
        return ()
    items = cast(tuple[object, ...], value)
    return tuple(item[:255] for item in items if isinstance(item, str) and item.strip())[:50]


def _url_candidates(interview: NotionInterviewRecord) -> tuple[Mapping[str, Any], ...]:
    if not interview.meeting_url:
        return ()
    return (
        {
            "url": interview.meeting_url,
            "source_kind": "property",
            "source_id": interview.property_ids.get("Meeting URL") or interview.page_id,
            "label": "Meeting URL",
        },
    )


def _jsonable_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    converted = _jsonable(value)
    return cast(dict[str, Any], converted) if isinstance(converted, dict) else {}


def _jsonable(value: Any) -> Any:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {str(key): _jsonable(item) for key, item in mapping.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        sequence = cast(Sequence[object], value)
        return [_jsonable(item) for item in sequence]
    if isinstance(value, datetime | date):
        return value.isoformat()
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _interview_fingerprint(interview: NotionInterviewRecord) -> str:
    payload = {
        "page_id": interview.page_id,
        "title": interview.title,
        "date": interview.date.model_dump(mode="python") if interview.date else None,
        "application_ids": interview.application_ids,
        "stage": interview.stage,
        "interview_status": interview.status,
        "preparation_status": interview.prep_status,
        "meeting_url": interview.meeting_url,
        "last_edited_at": interview.last_edited_at.isoformat(),
    }
    body = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _content_fingerprint(*values: Any) -> str:
    body = json.dumps(
        _jsonable(values),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _diagnostic_fingerprint(diagnostic: NotionDiscoveryDiagnostic) -> str:
    payload = (
        diagnostic.code,
        diagnostic.source_id,
        diagnostic.source_type,
        diagnostic.course_page_id,
        diagnostic.property_id,
        diagnostic.property_name,
    )
    body = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _record_diagnostic(
    store: JobInterviewSyncStore,
    diagnostic: NotionDiscoveryDiagnostic,
    *,
    synced_at: datetime,
) -> None:
    recorder = getattr(store, "record_jobs_diagnostic", None)
    if callable(recorder):
        recorder(diagnostic, synced_at=synced_at)


def _store_identity(value: Any) -> Any:
    return getattr(value, "id", value)


def _source_ids(
    applications: Sequence[NotionApplicationRecord],
    interviews: Sequence[NotionInterviewRecord],
) -> tuple[str, ...]:
    seen: list[str] = []
    for record in (*applications, *interviews):
        if record.source_id not in seen:
            seen.append(record.source_id)
    return tuple(seen)


def _error_codes(diagnostics: Sequence[NotionDiscoveryDiagnostic]) -> tuple[str, ...]:
    return tuple(sorted({item.code for item in diagnostics if item.severity == "error"}))


def _warning_codes(diagnostics: Sequence[NotionDiscoveryDiagnostic]) -> tuple[str, ...]:
    return tuple(sorted({item.code for item in diagnostics if item.severity == "warning"}))


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
