"""Notion Jobs/interview synchronization boundary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal, Protocol, cast
from zoneinfo import ZoneInfo

from app.connectors.notion import (
    NotionConnector,
    NotionDiscoveryDiagnostic,
    NotionInterviewEvent,
    NotionJobsDiscoveryResult,
)
from app.core.errors import ErrorCode, LifeAgentError
from app.db.job_interviews import (
    ApplicationRowInput,
    ApplicationTableInput,
    InterviewEventInput,
    JobsWorkspaceInput,
)


class JobInterviewSyncStore(Protocol):
    """Adapter around the career repository/session used by synchronization."""

    def save_sync_cursor(self, source_id: str, cursor: str | None) -> None: ...

    def upsert_jobs_workspace(self, workspace: JobsWorkspaceInput) -> Any: ...

    def upsert_application_table(self, workspace_id: Any, table: ApplicationTableInput) -> Any: ...

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
            result = await self._connector.discover_jobs_workspace()
        except LifeAgentError as exc:
            diagnostic = NotionDiscoveryDiagnostic(
                code="jobs_notion_sync_failed",
                severity="error",
                message="Jobs workspace synchronization failed",
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
                message="Jobs workspace synchronization failed",
            )
            _record_diagnostic(self._store, diagnostic, synced_at=current)
            return JobInterviewSyncResult(
                status="setup_required",
                diagnostic_codes=(diagnostic.code,),
                error_code=ErrorCode.SOURCE_SETUP_REQUIRED.value,
                synced_at=current,
            )
        return self._persist_result(result)

    def _persist_result(self, result: NotionJobsDiscoveryResult) -> JobInterviewSyncResult:
        diagnostics = list(result.diagnostics)
        for diagnostic in diagnostics:
            _record_diagnostic(self._store, diagnostic, synced_at=result.synced_at)
        if result.courses_source_id is not None:
            self._store.save_sync_cursor(result.courses_source_id, result.synced_at.isoformat())
        if result.jobs_page_id is None:
            codes = _error_codes(diagnostics)
            self._store.upsert_jobs_workspace(
                _workspace_record(result, status=_workspace_status(diagnostics))
            )
            return JobInterviewSyncResult(
                status="setup_required",
                diagnostic_codes=codes,
                synced_at=result.synced_at,
                error_code=ErrorCode.SOURCE_SETUP_REQUIRED.value,
            )

        workspace = _workspace_record(result, status=_workspace_status(diagnostics))
        workspace_result = self._store.upsert_jobs_workspace(workspace)
        workspace_id = _store_identity(workspace_result)

        application_row_count = 0
        for table in result.application_tables:
            table_input = _table_record(table)
            self._store.upsert_application_table(workspace_id, table_input)
            application_row_count += sum(1 for row in table_input.rows if not row.is_header)

        interview_count = 0
        unscheduled_interview_count = 0
        seen_interview_ids: set[str] = set()
        for interview in result.interviews:
            record, schedule_diagnostic = _interview_record(
                interview,
                timezone=self._timezone,
            )
            if schedule_diagnostic is not None or record is None:
                if schedule_diagnostic is not None:
                    diagnostics.append(schedule_diagnostic)
                    _record_diagnostic(self._store, schedule_diagnostic, synced_at=result.synced_at)
                unscheduled_interview_count += 1
                continue
            self._store.upsert_interview_event(workspace_id, record)
            seen_interview_ids.add(interview.page_id)
            interview_count += 1
        inactive_interview_count = self._store.deactivate_missing_interviews(
            workspace_id,
            seen_interview_ids,
        )
        if result.interviews_source_id is not None:
            self._store.save_sync_cursor(result.interviews_source_id, result.synced_at.isoformat())

        codes = _error_codes(diagnostics)
        warning_codes = _warning_codes(diagnostics)
        if codes:
            status: Literal["succeeded", "partial", "setup_required"] = "setup_required"
        elif warning_codes or unscheduled_interview_count:
            status = "partial"
        else:
            status = "succeeded"
        return JobInterviewSyncResult(
            status=status,
            table_count=len(result.application_tables),
            application_row_count=application_row_count,
            interview_count=interview_count,
            unscheduled_interview_count=unscheduled_interview_count,
            inactive_application_row_count=0,
            inactive_interview_count=inactive_interview_count,
            diagnostic_codes=tuple(sorted(set(codes + warning_codes))),
            synced_at=result.synced_at,
            error_code=(ErrorCode.SOURCE_SYNC_PARTIAL.value if status == "partial" else None),
        )


def _workspace_record(result: NotionJobsDiscoveryResult, *, status: str) -> JobsWorkspaceInput:
    primary_diagnostic = next(
        (item for item in result.diagnostics if item.severity == "error"),
        next(iter(result.diagnostics), None),
    )
    return JobsWorkspaceInput(
        jobs_page_id=result.jobs_page_id,
        jobs_page_title=result.jobs_title or "Jobs",
        discovery_status=status,
        diagnostic_code=primary_diagnostic.code if primary_diagnostic is not None else None,
        diagnostic_fingerprint=(
            _diagnostic_fingerprint(primary_diagnostic) if primary_diagnostic is not None else None
        ),
        interviews_database_id=result.interviews_database_id,
        interviews_data_source_id=result.interviews_source_id,
        title_property_id=result.interview_title_property_id,
        title_property_name=result.interview_title_property_name,
        date_property_id=result.interview_date_property_id,
        date_property_name=result.interview_date_property_name,
        discovered_at=result.synced_at,
        synced_at=result.synced_at,
    )


def _table_record(table: Any) -> ApplicationTableInput:
    rows = tuple(_row_record(row) for row in table.rows)
    return ApplicationTableInput(
        table_block_id=table.table_block_id,
        table_order=table.table_order,
        has_column_header=table.has_column_header,
        content_fingerprint=table.content_fingerprint,
        last_seen_at=table.last_seen_at,
        rows=rows,
    )


def _row_record(row: Any) -> ApplicationRowInput:
    return ApplicationRowInput(
        row_block_id=row.row_block_id,
        row_order=row.row_order,
        cells=row.cells,
        normalized_cells=row.cells,
        content_fingerprint=row.content_fingerprint,
        last_seen_at=row.last_seen_at,
        is_header=row.is_header,
        active=True,
    )


def _interview_record(
    interview: NotionInterviewEvent,
    *,
    timezone: ZoneInfo,
) -> tuple[InterviewEventInput | None, NotionDiscoveryDiagnostic | None]:
    starts_at, local_day, all_day, state, reason = _parse_interview_date(interview, timezone)
    if state != "scheduled" or local_day is None:
        return None, NotionDiscoveryDiagnostic(
            code=f"interview_{state}",
            severity="warning",
            message=reason or "Interview date needs attention",
            source_id=interview.interviews_source_id,
            source_type=interview.interviews_source_type,
            course_page_id=interview.jobs_page_id,
            course_title=interview.title[:255] or None,
            property_id=interview.date_property_id,
            property_name=interview.date_property_name,
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
            interviews_database_id=interview.interviews_database_id,
            interviews_data_source_id=interview.interviews_source_id,
            source_url=interview.source_url,
            tags=_tags(interview.properties),
            property_snapshot=interview.properties,
            url_candidates=_url_candidates(interview),
            evidence_fragments=tuple(
                fragment.model_dump(mode="json") for fragment in interview.evidence_fragments
            ),
            active=not archived,
            archived=archived,
        ),
        None,
    )


def _parse_interview_date(
    interview: NotionInterviewEvent,
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


def _url_candidates(interview: NotionInterviewEvent) -> tuple[Mapping[str, Any], ...]:
    return tuple(
        {
            "url": candidate.url,
            "source_kind": "block" if candidate.source_kind == "page_body" else "property",
            "source_id": candidate.source_id,
            "label": candidate.source_name,
        }
        for candidate in interview.url_candidates[:25]
    )


def _interview_fingerprint(interview: NotionInterviewEvent) -> str:
    payload = {
        "page_id": interview.page_id,
        "title": interview.title,
        "date": interview.date.model_dump(mode="python") if interview.date else None,
        "last_edited_at": interview.last_edited_at.isoformat(),
        "url_candidates": [item.url for item in interview.url_candidates],
        "evidence_fragments": [
            {
                "fragment_id": item.fragment_id,
                "source_kind": item.source_kind,
                "source_label": item.source_label,
                "text": item.text,
                "ordinal": item.ordinal,
            }
            for item in interview.evidence_fragments
        ],
    }
    body = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
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


def _error_codes(diagnostics: Sequence[NotionDiscoveryDiagnostic]) -> tuple[str, ...]:
    return tuple(sorted({item.code for item in diagnostics if item.severity == "error"}))


def _warning_codes(diagnostics: Sequence[NotionDiscoveryDiagnostic]) -> tuple[str, ...]:
    return tuple(sorted({item.code for item in diagnostics if item.severity == "warning"}))


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
