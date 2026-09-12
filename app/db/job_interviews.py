"""Persistence helpers for the career interview preparation domain."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, and_, func, or_, select
from sqlalchemy.exc import IntegrityError, NoResultFound
from sqlalchemy.orm import Session

from app.agents.calendar_briefing.contracts import CalendarEventSemanticStatus
from app.agents.job_interviews.contracts import (
    ApplicationInterpretation,
    ApplicationRowSnapshot,
    CareerClarificationRequest,
    InterviewApplicationLinkEvidence,
    InterviewEventSnapshot,
    PreparationPlanSnapshot,
    PreparationPlanStatus,
    UrlCandidate,
)
from app.db.models import (
    CareerApplicationInterpretation,
    CareerApplicationRow,
    CareerApplicationTable,
    CareerClarification,
    CareerInterviewApplicationLink,
    CareerInterviewEvent,
    CareerJobsWorkspace,
    CareerPreparationPlan,
    CareerPreparationPlanRevision,
    CareerReminderDelivery,
    CareerResearchSnapshot,
    CareerSyncCursor,
    CareerWriteProposal,
    CareerWriteReceipt,
)

CareerProposalStatus = Literal[
    "confirmed",
    "already_confirmed",
    "applied",
    "expired",
    "rejected",
    "token_mismatch",
]
CareerReceiptStatus = Literal["ready", "already_applied", "in_progress", "uncertain", "failed"]
CalendarSemanticStatus = Literal["valid", "not_substantive", "unavailable", "invalid"]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT_FIELDS = frozenset(("proposal_id", "page_id", "url", "notion_request_id", "edited_at"))


@dataclass(frozen=True, slots=True)
class JobsWorkspaceInput:
    scope: str = "default"
    jobs_page_id: str | None = None
    jobs_page_title: str | None = None
    discovery_status: str = "missing"
    diagnostic_code: str | None = None
    diagnostic_fingerprint: str | None = None
    interviews_database_id: str | None = None
    interviews_data_source_id: str | None = None
    title_property_id: str | None = None
    title_property_name: str | None = None
    date_property_id: str | None = None
    date_property_name: str | None = None
    discovered_at: datetime | None = None
    synced_at: datetime | None = None
    active: bool = True


@dataclass(frozen=True, slots=True)
class ApplicationRowInput:
    row_block_id: str
    row_order: int
    cells: Sequence[str]
    normalized_cells: Sequence[str]
    content_fingerprint: str
    last_seen_at: datetime
    is_header: bool = False
    active: bool = True


@dataclass(frozen=True, slots=True)
class ApplicationTableInput:
    table_block_id: str
    table_order: int
    has_column_header: bool
    content_fingerprint: str
    last_seen_at: datetime
    rows: Sequence[ApplicationRowInput] = ()
    active: bool = True


@dataclass(frozen=True, slots=True)
class ApplicationInterpretationInput:
    row_block_id: str
    company_name: str | None
    role_title: str | None
    status: str | None
    confidence: float
    evidence: Sequence[Mapping[str, Any]]
    interpreted_at: datetime
    model_version: str | None = None


@dataclass(frozen=True, slots=True)
class InterviewEventInput:
    interview_page_id: str
    title: str
    local_date: date
    notion_last_edited_at: datetime
    content_fingerprint: str
    date_start: datetime | None = None
    is_all_day: bool = False
    timezone: str = "America/Toronto"
    interviews_database_id: str | None = None
    interviews_data_source_id: str | None = None
    source_url: str | None = None
    tags: Sequence[str] = ()
    property_snapshot: Mapping[str, Any] | None = None
    url_candidates: Sequence[Mapping[str, Any]] = ()
    evidence_fragments: Sequence[Mapping[str, Any]] = ()
    content_artifact_key: str | None = None
    active: bool = True
    archived: bool = False


@dataclass(frozen=True, slots=True)
class CalendarSemanticResultInput:
    """Bounded semantic cache payload for one calendar event."""

    status: CalendarSemanticStatus
    source_fingerprint: str
    source_last_edited_at: datetime | None
    model_identity: str
    config_version: str
    prompt_version: str
    analyzed_at: datetime
    overview: str | None = None
    description: str | None = None
    evidence_ids: Sequence[str] = ()
    description_evidence_ids: Sequence[str] = ()


@dataclass(frozen=True, slots=True)
class InterviewLinkInput:
    interview_page_id: str
    state: str
    confidence: float
    rationale: str
    evidence: Sequence[Mapping[str, Any]]
    resolved_at: datetime
    row_block_id: str | None = None
    clarification_id: uuid.UUID | None = None
    interview_content_fingerprint: str | None = None
    application_content_fingerprint: str | None = None
    resolution_source: str = "model"
    active: bool = True


@dataclass(frozen=True, slots=True)
class ResearchSnapshotInput:
    interview_page_id: str
    source_url: str
    status: str
    retrieved_at: datetime
    canonical_url: str | None = None
    company_name: str | None = None
    content_fingerprint: str | None = None
    excerpt_artifact_key: str | None = None
    failure_code: str | None = None
    freshness_expires_at: datetime | None = None
    source_metadata: Mapping[str, Any] | None = None
    active: bool = True


@dataclass(frozen=True, slots=True)
class PreparationPlanInput:
    interview_page_id: str
    generated_at: datetime
    plan_hash: str
    summary: str
    next_actions: Sequence[str]
    evidence: Sequence[str]
    plan_payload: Mapping[str, Any]
    status: str = "current"
    research_snapshot_id: uuid.UUID | None = None
    artifact_key: str | None = None
    material_change_reason: str | None = None


@dataclass(frozen=True, slots=True)
class CareerClarificationInput:
    kind: str
    subject_type: str
    subject_id: str
    question: str
    idempotency_key: str
    choices: Sequence[str] = ()
    partial_state: Mapping[str, Any] | None = None
    discord_channel_id: str | None = None
    discord_user_id: str | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CareerWriteProposalInput:
    idempotency_key: str
    operation: str
    target_page_id: str
    payload: Mapping[str, Any]
    redacted_preview: str
    confirmation_token: str
    interview_page_id: str | None = None
    expected_last_edited_at: datetime | None = None
    requester: str | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReminderDeliveryInput:
    interview_page_id: str
    reminder_date: date
    reminder_kind: str
    days_until: int
    status: str = "pending"
    delivery_id: uuid.UUID | None = None
    included_at: datetime | None = None
    error_code: str | None = None


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _aware_db(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _bounded(value: str, limit: int = 255) -> str:
    text = value.strip()
    if not text:
        raise ValueError("required text field must not be empty")
    return text[:limit]


def _bounded_optional(value: str | None, limit: int = 255) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text[:limit] if text else None


def _bounded_string_list(values: Sequence[str], *, item_limit: int, count_limit: int) -> list[str]:
    return [item.strip()[:item_limit] for item in values[:count_limit] if item.strip()]


def _bounded_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    return dict(value)


def _bounded_receipt(receipt: Mapping[str, Any]) -> dict[str, str]:
    bounded: dict[str, str] = {}
    for key in _RECEIPT_FIELDS:
        value = receipt.get(key)
        if value is not None:
            bounded[key] = str(value)[:1_000]
    return bounded


def _upsert(session: Session, model: type[Any], filters: Sequence[Any], values: Mapping[str, Any]):
    existing = session.scalar(select(model).where(*filters))
    if existing is not None:
        for key, value in values.items():
            setattr(existing, key, value)
        session.flush()
        return existing
    instance = model(**values)
    try:
        with session.begin_nested():
            session.add(instance)
            session.flush()
    except IntegrityError:
        existing = session.scalar(select(model).where(*filters))
        if existing is None:
            raise
        for key, value in values.items():
            setattr(existing, key, value)
        session.flush()
        return existing
    return instance


class JobInterviewRepository:
    """Idempotent persistence boundary for career interview workflows."""

    @staticmethod
    def upsert_sync_cursor(
        session: Session,
        *,
        scope: str,
        source_version: str = "notion",
        cursor: str | None,
        last_synced_at: datetime | None,
        status: str = "succeeded",
        error_code: str | None = None,
    ) -> CareerSyncCursor:
        if status not in {"idle", "running", "succeeded", "failed"}:
            raise ValueError("invalid career sync cursor status")
        values = {
            "scope": _bounded(scope, 255),
            "source_version": _bounded(source_version, 128),
            "cursor": _bounded_optional(cursor, 512),
            "last_synced_at": (
                _utc(last_synced_at, "last_synced_at") if last_synced_at is not None else None
            ),
            "status": status,
            "error_code": _bounded_optional(error_code, 128),
        }
        return cast(
            CareerSyncCursor,
            _upsert(
                session,
                CareerSyncCursor,
                [CareerSyncCursor.scope == values["scope"]],
                values,
            ),
        )

    @staticmethod
    def upsert_jobs_workspace(
        session: Session,
        *,
        snapshot: JobsWorkspaceInput,
    ) -> CareerJobsWorkspace:
        if snapshot.discovery_status not in {
            "valid",
            "missing",
            "duplicate",
            "inaccessible",
            "malformed",
        }:
            raise ValueError("invalid Jobs discovery status")
        values = {
            "scope": _bounded(snapshot.scope, 128),
            "jobs_page_id": _bounded_optional(snapshot.jobs_page_id),
            "jobs_page_title": _bounded_optional(snapshot.jobs_page_title),
            "discovery_status": snapshot.discovery_status,
            "diagnostic_code": _bounded_optional(snapshot.diagnostic_code, 128),
            "diagnostic_fingerprint": _bounded_optional(snapshot.diagnostic_fingerprint, 128),
            "interviews_database_id": _bounded_optional(snapshot.interviews_database_id),
            "interviews_data_source_id": _bounded_optional(snapshot.interviews_data_source_id),
            "title_property_id": _bounded_optional(snapshot.title_property_id),
            "title_property_name": _bounded_optional(snapshot.title_property_name),
            "date_property_id": _bounded_optional(snapshot.date_property_id),
            "date_property_name": _bounded_optional(snapshot.date_property_name),
            "last_discovered_at": (
                _utc(snapshot.discovered_at, "discovered_at")
                if snapshot.discovered_at is not None
                else None
            ),
            "last_synced_at": (
                _utc(snapshot.synced_at, "synced_at") if snapshot.synced_at is not None else None
            ),
            "active": snapshot.active,
        }
        return cast(
            CareerJobsWorkspace,
            _upsert(
                session,
                CareerJobsWorkspace,
                [CareerJobsWorkspace.scope == values["scope"]],
                values,
            ),
        )

    @staticmethod
    def upsert_application_table(
        session: Session,
        *,
        workspace_id: uuid.UUID,
        snapshot: ApplicationTableInput,
    ) -> CareerApplicationTable:
        if snapshot.table_order < 0:
            raise ValueError("application table order must be nonnegative")
        seen_rows = {row.row_block_id for row in snapshot.rows if row.active}
        row_count = len(snapshot.rows)
        column_count = max((len(row.cells) for row in snapshot.rows), default=0)
        table = cast(
            CareerApplicationTable,
            _upsert(
                session,
                CareerApplicationTable,
                [CareerApplicationTable.table_block_id == snapshot.table_block_id],
                {
                    "workspace_id": workspace_id,
                    "table_block_id": _bounded(snapshot.table_block_id),
                    "table_order": snapshot.table_order,
                    "has_column_header": snapshot.has_column_header,
                    "row_count": row_count,
                    "column_count": column_count,
                    "content_fingerprint": _bounded(snapshot.content_fingerprint, 128),
                    "last_seen_at": _utc(snapshot.last_seen_at, "last_seen_at"),
                    "active": snapshot.active,
                },
            ),
        )
        for row in snapshot.rows:
            if row.row_order < 0:
                raise ValueError("application row order must be nonnegative")
            _upsert(
                session,
                CareerApplicationRow,
                [CareerApplicationRow.row_block_id == row.row_block_id],
                {
                    "table_id": table.id,
                    "row_block_id": _bounded(row.row_block_id),
                    "row_order": row.row_order,
                    "is_header": row.is_header,
                    "cells": _bounded_string_list(row.cells, item_limit=2_000, count_limit=200),
                    "normalized_cells": _bounded_string_list(
                        row.normalized_cells, item_limit=2_000, count_limit=200
                    ),
                    "content_fingerprint": _bounded(row.content_fingerprint, 128),
                    "last_seen_at": _utc(row.last_seen_at, "last_seen_at"),
                    "active": row.active,
                },
            )
        stale_rows = list(
            session.scalars(
                select(CareerApplicationRow).where(
                    CareerApplicationRow.table_id == table.id,
                    CareerApplicationRow.active.is_(True),
                )
            )
        )
        for row in stale_rows:
            if row.row_block_id not in seen_rows:
                row.active = False
        session.flush()
        return table

    @staticmethod
    def list_active_application_rows(session: Session) -> list[dict[str, Any]]:
        rows = session.execute(
            select(CareerApplicationRow, CareerApplicationTable.table_block_id)
            .join(
                CareerApplicationTable,
                CareerApplicationRow.table_id == CareerApplicationTable.id,
            )
            .where(
                CareerApplicationRow.active.is_(True),
                CareerApplicationRow.is_header.is_(False),
                CareerApplicationTable.active.is_(True),
            )
            .order_by(CareerApplicationTable.table_order, CareerApplicationRow.row_order)
        )
        result: list[dict[str, Any]] = []
        for row, table_block_id in rows:
            interpretation = session.scalar(
                select(CareerApplicationInterpretation).where(
                    CareerApplicationInterpretation.row_id == row.id
                )
            )
            result.append(
                {
                    "id": row.id,
                    "row_block_id": row.row_block_id,
                    "table_id": row.table_id,
                    "table_block_id": table_block_id,
                    "row_order": row.row_order,
                    "cells": row.cells,
                    "normalized_cells": row.normalized_cells,
                    "content_fingerprint": row.content_fingerprint,
                    "interpretation": (
                        _application_interpretation_public(interpretation)
                        if interpretation is not None
                        else None
                    ),
                }
            )
        return result

    @staticmethod
    def upsert_application_interpretation(
        session: Session,
        *,
        interpretation: ApplicationInterpretationInput,
    ) -> CareerApplicationInterpretation:
        if not 0 <= interpretation.confidence <= 1:
            raise ValueError("application interpretation confidence must be between 0 and 1")
        row = _application_row_by_block(session, interpretation.row_block_id)
        return cast(
            CareerApplicationInterpretation,
            _upsert(
                session,
                CareerApplicationInterpretation,
                [CareerApplicationInterpretation.row_id == row.id],
                {
                    "row_id": row.id,
                    "company_name": _bounded_optional(interpretation.company_name),
                    "role_title": _bounded_optional(interpretation.role_title, 500),
                    "status": _bounded_optional(interpretation.status),
                    "confidence": interpretation.confidence,
                    "evidence": [dict(item) for item in interpretation.evidence[:12]],
                    "model_version": _bounded_optional(interpretation.model_version),
                    "interpreted_at": _utc(interpretation.interpreted_at, "interpreted_at"),
                },
            ),
        )

    @staticmethod
    def upsert_interview_event(
        session: Session,
        *,
        workspace_id: uuid.UUID,
        event: InterviewEventInput,
    ) -> CareerInterviewEvent:
        values = {
            "workspace_id": workspace_id,
            "interview_page_id": _bounded(event.interview_page_id),
            "interviews_database_id": _bounded_optional(event.interviews_database_id),
            "interviews_data_source_id": _bounded_optional(event.interviews_data_source_id),
            "title": _bounded(event.title, 500),
            "date_start": _utc(event.date_start, "date_start") if event.date_start else None,
            "local_date": event.local_date,
            "is_all_day": event.is_all_day,
            "timezone": _bounded(event.timezone, 64),
            "notion_last_edited_at": _utc(event.notion_last_edited_at, "notion_last_edited_at"),
            "source_url": _bounded_optional(event.source_url, 2_048),
            "tags": _bounded_string_list(event.tags, item_limit=255, count_limit=50),
            "property_snapshot": _bounded_mapping(event.property_snapshot),
            "url_candidates": [dict(item) for item in event.url_candidates[:25]],
            "content_fingerprint": _bounded(event.content_fingerprint, 128),
            "content_artifact_key": _bounded_optional(event.content_artifact_key, 512),
            "active": event.active,
            "archived": event.archived,
        }
        return cast(
            CareerInterviewEvent,
            _upsert(
                session,
                CareerInterviewEvent,
                [CareerInterviewEvent.interview_page_id == values["interview_page_id"]],
                values,
            ),
        )

    @staticmethod
    def deactivate_missing_interviews(
        session: Session,
        *,
        workspace_id: uuid.UUID,
        seen_page_ids: set[str],
    ) -> int:
        rows = list(
            session.scalars(
                select(CareerInterviewEvent).where(
                    CareerInterviewEvent.workspace_id == workspace_id,
                    CareerInterviewEvent.active.is_(True),
                )
            )
        )
        count = 0
        for row in rows:
            if row.interview_page_id not in seen_page_ids:
                row.active = False
                row.archived = True
                count += 1
        session.flush()
        return count

    @staticmethod
    def load_upcoming_interviews(
        session: Session,
        *,
        now: datetime,
        timezone: str = "America/Toronto",
    ) -> list[dict[str, Any]]:
        local_today = _utc(now, "now").astimezone(ZoneInfo(timezone)).date()
        rows = session.scalars(
            select(CareerInterviewEvent)
            .where(
                CareerInterviewEvent.active.is_(True),
                CareerInterviewEvent.archived.is_(False),
                CareerInterviewEvent.local_date >= local_today,
            )
            .order_by(CareerInterviewEvent.local_date, CareerInterviewEvent.date_start)
        )
        return [_interview_public(session, row) for row in rows]

    @staticmethod
    def load_upcoming_calendar_items(
        session: Session,
        *,
        occurrence: date | datetime,
        timezone: str = "America/Toronto",
    ) -> list[dict[str, Any]]:
        tz = ZoneInfo(timezone)
        window_start, window_end = _calendar_window(occurrence, tz)
        query_start = window_start.astimezone(UTC) - timedelta(days=1)
        query_end = window_end.astimezone(UTC) + timedelta(days=1)
        rows = list(
            session.scalars(
                select(CareerInterviewEvent)
                .where(
                    CareerInterviewEvent.active.is_(True),
                    CareerInterviewEvent.archived.is_(False),
                    CareerInterviewEvent.local_date.is_not(None),
                    CareerInterviewEvent.local_date >= window_start.date(),
                    CareerInterviewEvent.local_date <= window_end.date(),
                    or_(
                        CareerInterviewEvent.date_start.is_(None),
                        and_(
                            CareerInterviewEvent.date_start >= query_start,
                            CareerInterviewEvent.date_start <= query_end,
                        ),
                    ),
                )
                .order_by(CareerInterviewEvent.local_date, CareerInterviewEvent.date_start)
            )
        )
        items: list[tuple[datetime, str, str, str, dict[str, Any]]] = []
        for row in rows:
            local_start = _interview_local_start(row, timezone=tz)
            if local_start is None or not window_start <= local_start <= window_end:
                continue
            item = _interview_calendar_item(
                row,
                local_start=local_start,
                window_start=window_start,
                timezone=tz,
            )
            items.append(
                (
                    local_start,
                    "jobs",
                    "jobs/interviews",
                    row.title.casefold(),
                    item,
                )
            )
        items.sort(key=lambda item: (item[0], item[1], item[2], item[3], item[4]["event_id"]))
        return [item[-1] for item in items]

    @staticmethod
    def save_interview_calendar_semantics(
        session: Session,
        *,
        interview_page_id: str,
        semantics: CalendarSemanticResultInput,
    ) -> bool:
        row = session.scalar(
            select(CareerInterviewEvent)
            .where(CareerInterviewEvent.interview_page_id == _bounded(interview_page_id))
            .with_for_update()
        )
        if row is None:
            return False
        source_edit = (
            _utc(semantics.source_last_edited_at, "source_last_edited_at")
            if semantics.source_last_edited_at is not None
            else None
        )
        if source_edit is not None and _aware_db(row.notion_last_edited_at) != source_edit:
            return False
        analyzed_at = _utc(semantics.analyzed_at, "analyzed_at")
        if (
            row.calendar_semantic_analyzed_at is not None
            and _aware_db(row.calendar_semantic_analyzed_at) > analyzed_at
        ):
            return False
        _apply_calendar_semantics(row, semantics, source_edit=source_edit, analyzed_at=analyzed_at)
        session.flush()
        return True

    @staticmethod
    def search_interviews(
        session: Session,
        *,
        query: str,
        now: datetime,
        timezone: str = "America/Toronto",
    ) -> list[dict[str, Any]]:
        terms = [term for term in query.casefold().split() if term]
        interviews = JobInterviewRepository.load_upcoming_interviews(
            session, now=now, timezone=timezone
        )
        if not terms:
            return interviews
        return [
            item
            for item in interviews
            if all(term in f"{item['title']} {' '.join(item['tags'])}".casefold() for term in terms)
        ]

    @staticmethod
    def save_interview_link(
        session: Session,
        *,
        link: InterviewLinkInput,
    ) -> CareerInterviewApplicationLink:
        if link.state not in {"matched", "ambiguous", "needs_clarification", "rejected"}:
            raise ValueError("invalid interview/application link state")
        if link.resolution_source not in {"model", "user"}:
            raise ValueError("invalid interview/application resolution source")
        if not 0 <= link.confidence <= 1:
            raise ValueError("interview/application link confidence must be between 0 and 1")
        interview = _interview_by_page(session, link.interview_page_id)
        row = _application_row_by_block(session, link.row_block_id) if link.row_block_id else None
        values = {
            "interview_id": interview.id,
            "application_row_id": row.id if row is not None else None,
            "state": link.state,
            "confidence": link.confidence,
            "rationale": _bounded(link.rationale, 1_000),
            "evidence": [dict(item) for item in link.evidence[:12]],
            "clarification_id": link.clarification_id,
            "interview_content_fingerprint": _bounded_optional(
                link.interview_content_fingerprint, 128
            ),
            "application_content_fingerprint": _bounded_optional(
                link.application_content_fingerprint, 128
            ),
            "resolution_source": link.resolution_source,
            "resolved_at": _utc(link.resolved_at, "resolved_at"),
            "active": link.active,
        }
        return cast(
            CareerInterviewApplicationLink,
            _upsert(
                session,
                CareerInterviewApplicationLink,
                [CareerInterviewApplicationLink.interview_id == interview.id],
                values,
            ),
        )

    @staticmethod
    def get_interview_link(
        session: Session,
        *,
        interview_page_id: str,
    ) -> dict[str, Any] | None:
        interview = _interview_by_page(session, interview_page_id)
        link = session.scalar(
            select(CareerInterviewApplicationLink).where(
                CareerInterviewApplicationLink.interview_id == interview.id
            )
        )
        if link is None:
            return None
        row = (
            session.get(CareerApplicationRow, link.application_row_id)
            if link.application_row_id
            else None
        )
        return {
            "id": link.id,
            "interview_page_id": interview.interview_page_id,
            "row_block_id": row.row_block_id if row else None,
            "state": link.state,
            "confidence": link.confidence,
            "rationale": link.rationale,
            "evidence": link.evidence,
            "clarification_id": link.clarification_id,
            "interview_content_fingerprint": link.interview_content_fingerprint,
            "application_content_fingerprint": link.application_content_fingerprint,
            "resolution_source": link.resolution_source,
            "resolved_at": link.resolved_at,
            "active": link.active,
        }

    @staticmethod
    def save_research_snapshot(
        session: Session,
        *,
        snapshot: ResearchSnapshotInput,
    ) -> CareerResearchSnapshot:
        if snapshot.status not in {"pending", "fetched", "partial", "failed", "stale"}:
            raise ValueError("invalid research status")
        interview = _interview_by_page(session, snapshot.interview_page_id)
        row = CareerResearchSnapshot(
            interview_id=interview.id,
            source_url=_bounded(snapshot.source_url, 2_048),
            canonical_url=_bounded_optional(snapshot.canonical_url, 2_048),
            company_name=_bounded_optional(snapshot.company_name),
            status=snapshot.status,
            content_fingerprint=_bounded_optional(snapshot.content_fingerprint, 128),
            excerpt_artifact_key=_bounded_optional(snapshot.excerpt_artifact_key, 512),
            failure_code=_bounded_optional(snapshot.failure_code, 128),
            retrieved_at=_utc(snapshot.retrieved_at, "retrieved_at"),
            freshness_expires_at=(
                _utc(snapshot.freshness_expires_at, "freshness_expires_at")
                if snapshot.freshness_expires_at
                else None
            ),
            source_metadata=_bounded_mapping(snapshot.source_metadata),
            active=snapshot.active,
        )
        session.add(row)
        session.flush()
        return row

    @staticmethod
    def save_current_plan(
        session: Session,
        *,
        plan: PreparationPlanInput,
    ) -> CareerPreparationPlan:
        if plan.status not in {"draft", "current", "stale", "failed"}:
            raise ValueError("invalid preparation plan status")
        interview = _interview_by_page(session, plan.interview_page_id)
        current = session.scalar(
            select(CareerPreparationPlan)
            .where(CareerPreparationPlan.interview_id == interview.id)
            .with_for_update()
        )
        generated = _utc(plan.generated_at, "generated_at")
        values = {
            "interview_id": interview.id,
            "status": plan.status,
            "generated_at": generated,
            "plan_hash": _bounded(plan.plan_hash, 128),
            "summary": _bounded(plan.summary, 1_000),
            "next_actions": _bounded_string_list(plan.next_actions, item_limit=500, count_limit=20),
            "evidence": _bounded_string_list(plan.evidence, item_limit=255, count_limit=50),
            "research_snapshot_id": plan.research_snapshot_id,
            "artifact_key": _bounded_optional(plan.artifact_key, 512),
            "material_change_reason": _bounded_optional(plan.material_change_reason, 1_000),
            "plan_payload": dict(plan.plan_payload),
        }
        if current is None:
            current = CareerPreparationPlan(revision=1, **values)
            session.add(current)
            session.flush()
            _create_plan_revision(session, current)
            return current
        if current.plan_hash == values["plan_hash"]:
            for key, value in values.items():
                setattr(current, key, value)
            session.flush()
            return current
        current.revision += 1
        for key, value in values.items():
            setattr(current, key, value)
        session.flush()
        _create_plan_revision(session, current)
        return current

    @staticmethod
    def get_current_plan(
        session: Session,
        *,
        interview_page_id: str,
    ) -> dict[str, Any] | None:
        interview = _interview_by_page(session, interview_page_id)
        row = session.scalar(
            select(CareerPreparationPlan).where(CareerPreparationPlan.interview_id == interview.id)
        )
        return (
            _plan_public(row, interview_page_id=interview.interview_page_id)
            if row is not None
            else None
        )

    @staticmethod
    def save_career_clarification(
        session: Session,
        *,
        request: CareerClarificationInput,
    ) -> CareerClarification:
        values = {
            "kind": _bounded(request.kind, 64),
            "subject_type": _bounded(request.subject_type, 64),
            "subject_id": _bounded(request.subject_id),
            "question": _bounded(request.question, 1_000),
            "choices": _bounded_string_list(request.choices, item_limit=500, count_limit=20),
            "idempotency_key": _bounded(request.idempotency_key, 512),
            "partial_state": _bounded_mapping(request.partial_state),
            "discord_channel_id": _bounded_optional(request.discord_channel_id, 32),
            "discord_user_id": _bounded_optional(request.discord_user_id, 32),
            "expires_at": _utc(request.expires_at, "expires_at") if request.expires_at else None,
        }
        return cast(
            CareerClarification,
            _upsert(
                session,
                CareerClarification,
                [CareerClarification.idempotency_key == values["idempotency_key"]],
                values,
            ),
        )

    @staticmethod
    def list_career_clarifications(
        session: Session,
        *,
        states: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        statement = select(CareerClarification).order_by(CareerClarification.created_at)
        if states:
            statement = statement.where(CareerClarification.state.in_(states))
        return [_clarification_public(row) for row in session.scalars(statement)]

    @staticmethod
    def save_write_proposal(
        session: Session,
        *,
        proposal: CareerWriteProposalInput,
    ) -> CareerWriteProposal:
        if proposal.operation not in {"interview_date", "preparation_plan"}:
            raise ValueError("invalid career write proposal operation")
        interview = (
            _interview_by_page(session, proposal.interview_page_id)
            if proposal.interview_page_id is not None
            else session.scalar(
                select(CareerInterviewEvent).where(
                    CareerInterviewEvent.interview_page_id == proposal.target_page_id
                )
            )
        )
        values = {
            "interview_id": interview.id if interview else None,
            "idempotency_key": _bounded(proposal.idempotency_key, 512),
            "operation": proposal.operation,
            "target_page_id": _bounded(proposal.target_page_id),
            "expected_last_edited_at": (
                _utc(proposal.expected_last_edited_at, "expected_last_edited_at")
                if proposal.expected_last_edited_at
                else None
            ),
            "payload": dict(proposal.payload),
            "redacted_preview": _bounded(proposal.redacted_preview, 4_000),
            "confirmation_token": _bounded(proposal.confirmation_token),
            "requester": _bounded_optional(proposal.requester),
            "expires_at": _utc(proposal.expires_at, "expires_at") if proposal.expires_at else None,
        }
        return cast(
            CareerWriteProposal,
            _upsert(
                session,
                CareerWriteProposal,
                [CareerWriteProposal.idempotency_key == values["idempotency_key"]],
                values,
            ),
        )

    @staticmethod
    def get_write_proposal(
        session: Session,
        *,
        proposal_id: uuid.UUID,
    ) -> dict[str, Any] | None:
        row = session.get(CareerWriteProposal, proposal_id)
        return _proposal_public(row) if row is not None else None

    @staticmethod
    def confirm_write_proposal(
        session: Session,
        *,
        proposal_id: uuid.UUID,
        confirmation_token: str,
        confirmation_event: str,
        now: datetime,
    ) -> tuple[CareerProposalStatus, CareerWriteProposal]:
        row = session.scalar(
            select(CareerWriteProposal)
            .where(CareerWriteProposal.id == proposal_id)
            .with_for_update()
        )
        if row is None:
            raise NoResultFound(f"career write proposal {proposal_id} was not found")
        current = _utc(now, "now")
        if row.state in {"confirmed", "applying"}:
            return "already_confirmed", row
        if row.state == "applied":
            return "applied", row
        if row.state == "rejected":
            return "rejected", row
        if row.expires_at is not None and _aware_db(row.expires_at) <= current:
            row.state = "expired"
            session.flush()
            return "expired", row
        if confirmation_token.strip() != row.confirmation_token:
            return "token_mismatch", row
        row.state = "confirmed"
        row.confirmation_event = _bounded(confirmation_event)
        session.flush()
        return "confirmed", row

    @staticmethod
    def reject_write_proposal(
        session: Session,
        *,
        proposal_id: uuid.UUID,
        confirmation_event: str,
    ) -> CareerWriteProposal:
        row = session.get(CareerWriteProposal, proposal_id)
        if row is None:
            raise NoResultFound(f"career write proposal {proposal_id} was not found")
        if row.state in {"pending", "confirmed"}:
            row.state = "rejected"
            row.confirmation_event = _bounded(confirmation_event)
        session.flush()
        return row

    @staticmethod
    def begin_write_receipt(
        session: Session,
        *,
        proposal_id: uuid.UUID,
        operation_id: str,
        payload_hash: str,
    ) -> tuple[CareerReceiptStatus, CareerWriteReceipt]:
        if not _SHA256.fullmatch(payload_hash):
            raise ValueError("career write payload hash must be SHA-256 hex")
        row = session.scalar(
            select(CareerWriteReceipt)
            .where(
                CareerWriteReceipt.proposal_id == proposal_id,
                CareerWriteReceipt.operation_id == operation_id,
            )
            .with_for_update()
        )
        if row is not None:
            if row.payload_hash != payload_hash:
                raise ValueError("career write payload hash changed")
            if row.state == "applied":
                return "already_applied", row
            return cast(CareerReceiptStatus, row.state), row
        row = CareerWriteReceipt(
            proposal_id=proposal_id,
            operation_id=_bounded(operation_id),
            payload_hash=payload_hash,
            state="in_progress",
        )
        session.add(row)
        session.flush()
        return "ready", row

    @staticmethod
    def mark_write_receipt_applied(
        session: Session,
        *,
        proposal_id: uuid.UUID,
        operation_id: str,
        payload_hash: str,
        receipt: Mapping[str, Any],
        applied_at: datetime,
    ) -> CareerWriteReceipt:
        row = _lock_write_receipt(session, proposal_id, operation_id, payload_hash)
        row.state = "applied"
        row.receipt = _bounded_receipt(receipt)
        row.error_code = None
        proposal = session.get(CareerWriteProposal, proposal_id)
        if proposal is not None:
            proposal.state = "applied"
            proposal.applied_at = _utc(applied_at, "applied_at")
        session.flush()
        return row

    @staticmethod
    def mark_write_receipt_uncertain(
        session: Session,
        *,
        proposal_id: uuid.UUID,
        operation_id: str,
        payload_hash: str,
        error_code: str,
    ) -> CareerWriteReceipt:
        row = _lock_write_receipt(session, proposal_id, operation_id, payload_hash)
        if row.state != "applied":
            row.state = "uncertain"
            row.error_code = _bounded(error_code, 128)
        session.flush()
        return row

    @staticmethod
    def record_reminder_delivery(
        session: Session,
        *,
        reminder: ReminderDeliveryInput,
    ) -> CareerReminderDelivery:
        interview = _interview_by_page(session, reminder.interview_page_id)
        values = {
            "interview_id": interview.id,
            "reminder_date": reminder.reminder_date,
            "reminder_kind": _bounded(reminder.reminder_kind, 32),
            "days_until": reminder.days_until,
            "delivery_id": reminder.delivery_id,
            "status": reminder.status,
            "included_at": _utc(reminder.included_at, "included_at")
            if reminder.included_at
            else None,
            "error_code": _bounded_optional(reminder.error_code, 128),
        }
        return cast(
            CareerReminderDelivery,
            _upsert(
                session,
                CareerReminderDelivery,
                [
                    CareerReminderDelivery.interview_id == interview.id,
                    CareerReminderDelivery.reminder_date == reminder.reminder_date,
                    CareerReminderDelivery.reminder_kind == values["reminder_kind"],
                ],
                values,
            ),
        )

    @staticmethod
    def health_summary(session: Session) -> dict[str, Any]:
        workspace = session.scalar(
            select(CareerJobsWorkspace).order_by(CareerJobsWorkspace.updated_at.desc())
        )
        active_rows = session.scalar(
            select(func.count())
            .select_from(CareerApplicationRow)
            .where(CareerApplicationRow.active.is_(True), CareerApplicationRow.is_header.is_(False))
        )
        active_interviews = session.scalar(
            select(func.count())
            .select_from(CareerInterviewEvent)
            .where(CareerInterviewEvent.active.is_(True), CareerInterviewEvent.archived.is_(False))
        )
        unresolved = session.scalar(
            select(func.count())
            .select_from(CareerClarification)
            .where(CareerClarification.state.in_(["pending", "delivered", "answered"]))
        )
        pending_proposals = session.scalar(
            select(func.count())
            .select_from(CareerWriteProposal)
            .where(CareerWriteProposal.state.in_(["pending", "confirmed", "applying"]))
        )
        failed_or_stale_plans = session.scalar(
            select(func.count())
            .select_from(CareerPreparationPlan)
            .where(CareerPreparationPlan.status.in_(["failed", "stale"]))
        )
        last_reminder = session.scalar(
            select(CareerReminderDelivery)
            .where(CareerReminderDelivery.status.in_(["included", "sent"]))
            .order_by(CareerReminderDelivery.included_at.desc().nullslast())
        )
        return {
            "jobs_discovery_status": workspace.discovery_status if workspace else "missing",
            "last_successful_jobs_sync": workspace.last_synced_at if workspace else None,
            "active_application_row_count": active_rows or 0,
            "upcoming_interview_count": active_interviews or 0,
            "unresolved_clarification_count": unresolved or 0,
            "failed_or_stale_plan_count": failed_or_stale_plans or 0,
            "pending_write_proposal_count": pending_proposals or 0,
            "last_interview_reminder_at": last_reminder.included_at if last_reminder else None,
        }


class SQLAlchemyJobInterviewStore:
    """Engine-backed adapter used by sync, morning, and preparation workflows."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def get_sync_cursor(self, source_id: str) -> str | None:
        with Session(self.engine) as session:
            row = session.scalar(
                select(CareerSyncCursor).where(CareerSyncCursor.scope == _cursor_scope(source_id))
            )
            return row.cursor if row is not None else None

    def save_sync_cursor(self, source_id: str, cursor: str | None) -> None:
        with Session(self.engine) as session, session.begin():
            JobInterviewRepository.upsert_sync_cursor(
                session,
                scope=_cursor_scope(source_id),
                cursor=cursor,
                last_synced_at=datetime.now(UTC),
            )

    def record_jobs_diagnostic(self, diagnostic: Any, *, synced_at: datetime) -> None:
        status = _diagnostic_status(getattr(diagnostic, "code", None))
        with Session(self.engine) as session, session.begin():
            workspace = session.scalar(
                select(CareerJobsWorkspace).where(CareerJobsWorkspace.scope == "default")
            )
            if workspace is None:
                JobInterviewRepository.upsert_jobs_workspace(
                    session,
                    snapshot=JobsWorkspaceInput(
                        discovery_status=status,
                        diagnostic_code=getattr(diagnostic, "code", None),
                        diagnostic_fingerprint=getattr(diagnostic, "source_id", None)
                        or getattr(diagnostic, "property_id", None)
                        or getattr(diagnostic, "code", None),
                        synced_at=None,
                        discovered_at=synced_at,
                        active=status == "valid",
                    ),
                )
                return
            if status in {"missing", "duplicate", "malformed"}:
                workspace.discovery_status = status
            workspace.diagnostic_code = _bounded_optional(getattr(diagnostic, "code", None), 128)
            workspace.diagnostic_fingerprint = _bounded_optional(
                getattr(diagnostic, "source_id", None)
                or getattr(diagnostic, "property_id", None)
                or getattr(diagnostic, "code", None),
                128,
            )
            workspace.last_discovered_at = _utc(synced_at, "synced_at")

    def upsert_jobs_workspace(self, workspace: Any) -> str:
        if isinstance(workspace, JobsWorkspaceInput):
            with Session(self.engine) as session, session.begin():
                row = JobInterviewRepository.upsert_jobs_workspace(
                    session,
                    snapshot=workspace,
                )
                return str(row.id)
        status = getattr(workspace, "status", "valid")
        if status == "setup_required":
            status = "malformed"
        with Session(self.engine) as session, session.begin():
            row = JobInterviewRepository.upsert_jobs_workspace(
                session,
                snapshot=JobsWorkspaceInput(
                    jobs_page_id=getattr(workspace, "jobs_page_id", None),
                    jobs_page_title=getattr(workspace, "jobs_title", None),
                    discovery_status=status,
                    interviews_database_id=getattr(workspace, "interviews_database_id", None),
                    interviews_data_source_id=getattr(workspace, "interviews_source_id", None),
                    discovered_at=getattr(workspace, "synced_at", None),
                    synced_at=getattr(workspace, "synced_at", None),
                    active=status == "valid",
                ),
            )
            return str(row.id)

    def upsert_application_table(self, workspace_id: str, table: Any) -> str:
        if isinstance(table, ApplicationTableInput):
            with Session(self.engine) as session, session.begin():
                row = JobInterviewRepository.upsert_application_table(
                    session,
                    workspace_id=uuid.UUID(str(workspace_id)),
                    snapshot=table,
                )
                return str(row.id)
        with Session(self.engine) as session, session.begin():
            row = cast(
                CareerApplicationTable,
                _upsert(
                    session,
                    CareerApplicationTable,
                    [CareerApplicationTable.table_block_id == table.table_block_id],
                    {
                        "workspace_id": uuid.UUID(workspace_id),
                        "table_block_id": _bounded(table.table_block_id),
                        "table_order": table.table_order,
                        "has_column_header": table.has_column_header,
                        "row_count": getattr(table, "row_count", 0),
                        "column_count": getattr(table, "column_count", 0),
                        "content_fingerprint": _bounded(table.content_fingerprint, 128),
                        "last_seen_at": _utc(table.last_seen_at, "last_seen_at"),
                        "active": True,
                    },
                ),
            )
            return str(row.id)

    def upsert_application_row(self, table_id: str, row: Any) -> str:
        cells = tuple(getattr(row, "cells", ()))
        normalized = tuple(cell.strip().casefold() for cell in cells)
        with Session(self.engine) as session, session.begin():
            stored = cast(
                CareerApplicationRow,
                _upsert(
                    session,
                    CareerApplicationRow,
                    [CareerApplicationRow.row_block_id == row.row_block_id],
                    {
                        "table_id": uuid.UUID(table_id),
                        "row_block_id": _bounded(row.row_block_id),
                        "row_order": row.row_order,
                        "is_header": row.is_header,
                        "cells": _bounded_string_list(cells, item_limit=2_000, count_limit=200),
                        "normalized_cells": _bounded_string_list(
                            normalized, item_limit=2_000, count_limit=200
                        ),
                        "content_fingerprint": _bounded(row.content_fingerprint, 128),
                        "last_seen_at": _utc(row.last_seen_at, "last_seen_at"),
                        "active": getattr(row, "active", True),
                    },
                ),
            )
            return str(stored.id)

    def reconcile_application_table(
        self,
        table_block_id: str,
        seen_row_ids: Sequence[str],
        *,
        synced_at: datetime | None = None,
    ) -> int:
        del synced_at
        seen = set(seen_row_ids)
        with Session(self.engine) as session, session.begin():
            table = session.scalar(
                select(CareerApplicationTable).where(
                    CareerApplicationTable.table_block_id == table_block_id
                )
            )
            if table is None:
                return 0
            rows = list(
                session.scalars(
                    select(CareerApplicationRow).where(
                        CareerApplicationRow.table_id == table.id,
                        CareerApplicationRow.active.is_(True),
                    )
                )
            )
            count = 0
            for row in rows:
                if row.row_block_id not in seen:
                    row.active = False
                    count += 1
            return count

    def upsert_interview_event(self, workspace_id: str, interview: Any) -> str:
        if isinstance(interview, InterviewEventInput):
            with Session(self.engine) as session, session.begin():
                row = JobInterviewRepository.upsert_interview_event(
                    session,
                    workspace_id=uuid.UUID(str(workspace_id)),
                    event=interview,
                )
                return str(row.id)
        local_date = getattr(interview, "local_date", None)
        starts_at = getattr(interview, "starts_at", None)
        with Session(self.engine) as session, session.begin():
            row = cast(
                CareerInterviewEvent,
                _upsert(
                    session,
                    CareerInterviewEvent,
                    [CareerInterviewEvent.interview_page_id == interview.interview_page_id],
                    {
                        "workspace_id": uuid.UUID(workspace_id),
                        "interview_page_id": _bounded(interview.interview_page_id),
                        "interviews_database_id": _bounded_optional(
                            interview.interviews_database_id
                        ),
                        "interviews_data_source_id": _bounded_optional(
                            interview.interviews_source_id
                        ),
                        "title": _bounded(interview.title, 500),
                        "date_start": _utc(starts_at, "starts_at") if starts_at else None,
                        "local_date": local_date,
                        "is_all_day": getattr(interview, "all_day", False),
                        "timezone": "America/Toronto",
                        "notion_last_edited_at": _utc(interview.last_edited_at, "last_edited_at"),
                        "source_url": _bounded_optional(interview.source_url, 2_048),
                        "tags": [],
                        "property_snapshot": dict(getattr(interview, "properties", {}) or {}),
                        "url_candidates": [
                            dict(item) for item in getattr(interview, "url_candidates", ())[:25]
                        ],
                        "content_fingerprint": _bounded(
                            _legacy_interview_fingerprint(interview),
                            128,
                        ),
                        "content_artifact_key": None,
                        "active": getattr(interview, "active", True),
                        "archived": getattr(interview, "archived", False),
                    },
                ),
            )
            return str(row.id)

    def deactivate_missing_interviews(
        self,
        workspace_id: str,
        seen_page_ids: set[str],
    ) -> int:
        with Session(self.engine) as session, session.begin():
            return JobInterviewRepository.deactivate_missing_interviews(
                session,
                workspace_id=uuid.UUID(str(workspace_id)),
                seen_page_ids=seen_page_ids,
            )

    def reconcile_interview_source(
        self,
        interviews_source_id: str,
        seen_page_ids: Sequence[str],
        *,
        synced_at: datetime | None = None,
    ) -> int:
        del synced_at
        seen = set(seen_page_ids)
        with Session(self.engine) as session, session.begin():
            rows = list(
                session.scalars(
                    select(CareerInterviewEvent).where(
                        CareerInterviewEvent.interviews_data_source_id == interviews_source_id,
                        CareerInterviewEvent.active.is_(True),
                    )
                )
            )
            count = 0
            for row in rows:
                if row.interview_page_id not in seen:
                    row.active = False
                    row.archived = True
                    count += 1
            return count

    def load_upcoming_interviews(self, *, now: datetime) -> tuple[InterviewEventSnapshot, ...]:
        with Session(self.engine) as session:
            rows = JobInterviewRepository.load_upcoming_interviews(session, now=now)
            return tuple(_interview_contract(row) for row in rows)

    def load_upcoming_calendar_items(
        self,
        *,
        occurrence: date | datetime,
        timezone: str = "America/Toronto",
    ) -> tuple[Mapping[str, Any], ...]:
        with Session(self.engine) as session:
            return tuple(
                JobInterviewRepository.load_upcoming_calendar_items(
                    session,
                    occurrence=occurrence,
                    timezone=timezone,
                )
            )

    def save_interview_calendar_semantics(
        self,
        interview_page_id: str,
        semantics: CalendarSemanticResultInput,
    ) -> bool:
        with Session(self.engine) as session, session.begin():
            return JobInterviewRepository.save_interview_calendar_semantics(
                session,
                interview_page_id=interview_page_id,
                semantics=semantics,
            )

    def search_interviews(self, query: str, *, now: datetime) -> tuple[InterviewEventSnapshot, ...]:
        with Session(self.engine) as session:
            rows = JobInterviewRepository.search_interviews(session, query=query, now=now)
            return tuple(_interview_contract(row) for row in rows)

    def list_active_application_rows(self) -> list[dict[str, Any]]:
        with Session(self.engine) as session:
            return JobInterviewRepository.list_active_application_rows(session)

    def application_row_snapshots(self) -> tuple[ApplicationRowSnapshot, ...]:
        now = datetime.now(UTC)
        return tuple(
            ApplicationRowSnapshot(
                table_block_id=str(row["table_block_id"]),
                row_block_id=str(row["row_block_id"]),
                row_order=int(row["row_order"]),
                cells=tuple(cast(Sequence[str], row.get("cells") or ())),
                normalized_cells=tuple(
                    cast(Sequence[str], row.get("normalized_cells") or row.get("cells") or ())
                ),
                content_fingerprint=str(row["content_fingerprint"]),
                last_seen_at=now,
            )
            for row in self.list_active_application_rows()
        )

    def save_application_interpretation(
        self,
        interpretation: ApplicationInterpretation,
    ) -> None:
        with Session(self.engine) as session, session.begin():
            JobInterviewRepository.upsert_application_interpretation(
                session,
                interpretation=ApplicationInterpretationInput(
                    row_block_id=interpretation.row_block_id,
                    company_name=interpretation.company_name,
                    role_title=interpretation.role_title,
                    status=interpretation.status,
                    confidence=interpretation.confidence,
                    evidence=tuple(
                        item.model_dump(mode="json") for item in interpretation.evidence
                    ),
                    model_version=interpretation.model_version,
                    interpreted_at=interpretation.interpreted_at,
                ),
            )

    def get_interview_link(self, interview_page_id: str) -> dict[str, Any] | None:
        with Session(self.engine) as session:
            return JobInterviewRepository.get_interview_link(
                session,
                interview_page_id=interview_page_id,
            )

    def save_interview_link(self, link: InterviewApplicationLinkEvidence) -> None:
        with Session(self.engine) as session, session.begin():
            JobInterviewRepository.save_interview_link(
                session,
                link=InterviewLinkInput(
                    interview_page_id=link.interview_page_id,
                    row_block_id=link.row_block_id,
                    state=str(link.state),
                    confidence=link.confidence,
                    rationale=link.rationale,
                    evidence=tuple(item.model_dump(mode="json") for item in link.evidence),
                    interview_content_fingerprint=link.interview_content_fingerprint,
                    application_content_fingerprint=link.application_content_fingerprint,
                    resolution_source=link.resolution_source,
                    resolved_at=link.resolved_at,
                ),
            )

    def save_clarification(self, request: CareerClarificationRequest) -> str:
        with Session(self.engine) as session, session.begin():
            row = JobInterviewRepository.save_career_clarification(
                session,
                request=CareerClarificationInput(
                    kind=str(request.kind),
                    subject_type=request.subject_type,
                    subject_id=request.subject_id,
                    question=request.question,
                    choices=request.choices,
                    idempotency_key=request.idempotency_key,
                    partial_state=request.partial_state,
                    expires_at=request.expires_at,
                ),
            )
            return str(row.id)

    def get_current_plan(self, interview_page_id: str) -> PreparationPlanSnapshot | None:
        with Session(self.engine) as session:
            row = JobInterviewRepository.get_current_plan(
                session,
                interview_page_id=interview_page_id,
            )
            return _plan_contract(row) if row is not None else None

    def get_current_preparation_plan(
        self,
        interview_page_id: str,
    ) -> PreparationPlanSnapshot | None:
        return self.get_current_plan(interview_page_id)

    def save_preparation_plan(self, plan: PreparationPlanSnapshot) -> str:
        return self.save_current_plan(plan)

    def save_current_plan(self, plan: PreparationPlanSnapshot) -> str:
        with Session(self.engine) as session, session.begin():
            row = JobInterviewRepository.save_current_plan(
                session,
                plan=PreparationPlanInput(
                    interview_page_id=plan.interview_page_id,
                    generated_at=plan.generated_at,
                    plan_hash=plan.plan_hash,
                    summary=plan.summary,
                    next_actions=plan.next_actions,
                    evidence=plan.evidence,
                    plan_payload=plan.plan,
                    status=plan.status,
                    artifact_key=plan.artifact_key,
                    material_change_reason=plan.material_change_reason,
                ),
            )
            return str(row.id)

    def record_reminder_delivery(
        self,
        reminder: Any,
        *,
        status: str,
        included_at: datetime,
        delivery_id: uuid.UUID | None = None,
    ) -> str:
        """Persist one bounded reminder audit row after the combined delivery."""

        with Session(self.engine) as session, session.begin():
            row = JobInterviewRepository.record_reminder_delivery(
                session,
                reminder=ReminderDeliveryInput(
                    interview_page_id=str(reminder.interview_page_id),
                    reminder_date=reminder.reminder_date,
                    reminder_kind=str(reminder.emphasis),
                    days_until=int(reminder.days_until),
                    delivery_id=delivery_id,
                    status=status,
                    included_at=included_at,
                ),
            )
            return str(row.id)

    def health_summary(self) -> dict[str, Any]:
        with Session(self.engine) as session:
            return JobInterviewRepository.health_summary(session)


def _application_row_by_block(session: Session, row_block_id: str) -> CareerApplicationRow:
    row = session.scalar(
        select(CareerApplicationRow).where(CareerApplicationRow.row_block_id == row_block_id)
    )
    if row is None:
        raise NoResultFound(f"career application row {row_block_id} was not found")
    return row


def _interview_by_page(session: Session, interview_page_id: str) -> CareerInterviewEvent:
    row = session.scalar(
        select(CareerInterviewEvent).where(
            CareerInterviewEvent.interview_page_id == interview_page_id
        )
    )
    if row is None:
        raise NoResultFound(f"career interview {interview_page_id} was not found")
    return row


def _create_plan_revision(session: Session, plan: CareerPreparationPlan) -> None:
    revision = CareerPreparationPlanRevision(
        plan_id=plan.id,
        interview_id=plan.interview_id,
        revision=plan.revision,
        status=plan.status,
        generated_at=plan.generated_at,
        plan_hash=plan.plan_hash,
        summary=plan.summary,
        next_actions=plan.next_actions,
        evidence=plan.evidence,
        research_snapshot_id=plan.research_snapshot_id,
        artifact_key=plan.artifact_key,
        material_change_reason=plan.material_change_reason,
        plan_payload=plan.plan_payload,
    )
    session.add(revision)
    session.flush()


def _lock_write_receipt(
    session: Session,
    proposal_id: uuid.UUID,
    operation_id: str,
    payload_hash: str,
) -> CareerWriteReceipt:
    row = session.scalar(
        select(CareerWriteReceipt)
        .where(
            CareerWriteReceipt.proposal_id == proposal_id,
            CareerWriteReceipt.operation_id == operation_id,
        )
        .with_for_update()
    )
    if row is None:
        raise NoResultFound(f"career write receipt {proposal_id}/{operation_id} was not found")
    if row.payload_hash != payload_hash:
        raise ValueError("career write payload hash changed")
    return row


def _application_interpretation_public(row: CareerApplicationInterpretation) -> dict[str, Any]:
    return {
        "company_name": row.company_name,
        "role_title": row.role_title,
        "status": row.status,
        "confidence": row.confidence,
        "evidence": row.evidence,
        "model_version": row.model_version,
        "interpreted_at": row.interpreted_at,
    }


def _apply_calendar_semantics(
    row: Any,
    semantics: CalendarSemanticResultInput,
    *,
    source_edit: datetime | None,
    analyzed_at: datetime,
) -> None:
    if semantics.status not in {"valid", "not_substantive", "unavailable", "invalid"}:
        raise ValueError("invalid calendar semantic status")
    overview = _bounded_optional(semantics.overview, 700)
    description = _bounded_optional(semantics.description, 1_500)
    evidence_ids = _bounded_semantic_ids(semantics.evidence_ids)
    description_ids = _bounded_semantic_ids(semantics.description_evidence_ids)
    if semantics.status in {"unavailable", "invalid"}:
        overview = None
        description = None
        evidence_ids = []
        description_ids = []
    elif semantics.status == "not_substantive":
        description = None
        description_ids = []
    row.calendar_semantic_overview = overview
    row.calendar_semantic_description = description
    row.calendar_semantic_status = semantics.status
    row.calendar_semantic_evidence_ids = evidence_ids
    row.calendar_semantic_description_evidence_ids = description_ids
    row.calendar_semantic_source_fingerprint = _bounded(semantics.source_fingerprint, 128)
    row.calendar_semantic_source_last_edited_at = source_edit
    row.calendar_semantic_model_identity = _bounded(semantics.model_identity, 128)
    row.calendar_semantic_config_version = _bounded(semantics.config_version, 128)
    row.calendar_semantic_prompt_version = _bounded(semantics.prompt_version, 128)
    row.calendar_semantic_analyzed_at = analyzed_at


def _bounded_semantic_ids(values: Sequence[str]) -> list[str]:
    return [_bounded(str(item), 255) for item in values[:12] if str(item).strip()]


def _calendar_window(occurrence: date | datetime, timezone: ZoneInfo) -> tuple[datetime, datetime]:
    if isinstance(occurrence, datetime):
        local_day = _aware_db(occurrence).astimezone(timezone).date()
    else:
        local_day = occurrence
    window_start = datetime.combine(local_day, datetime.min.time(), tzinfo=timezone)
    return window_start, window_start + timedelta(days=10, hours=12)


def _interview_local_start(
    row: CareerInterviewEvent,
    *,
    timezone: ZoneInfo,
) -> datetime | None:
    if row.local_date is None:
        return None
    if row.is_all_day or row.date_start is None:
        return datetime.combine(row.local_date, datetime.min.time(), tzinfo=timezone)
    return _aware_db(row.date_start).astimezone(timezone)


def _interview_calendar_item(
    row: CareerInterviewEvent,
    *,
    local_start: datetime,
    window_start: datetime,
    timezone: ZoneInfo,
) -> dict[str, Any]:
    local_end = None
    semantic_status = row.calendar_semantic_status or CalendarEventSemanticStatus.UNAVAILABLE.value
    overview = (
        row.calendar_semantic_overview if semantic_status in {"valid", "not_substantive"} else None
    )
    description = row.calendar_semantic_description if semantic_status == "valid" else None
    return {
        "event_id": row.interview_page_id,
        "source_area": "jobs",
        "source_label": "Jobs/Interviews",
        "title": row.title,
        "display_kind": "Interview",
        "local_start_label": _calendar_label(local_start, is_all_day=row.is_all_day),
        "local_end_label": (
            _calendar_label(local_end, is_all_day=False) if local_end is not None else None
        ),
        "relative_date_label": _relative_day_label(local_start.date(), window_start.date()),
        "is_all_day": row.is_all_day,
        "completed": False,
        "semantic_status": semantic_status,
        "semantic_overview": overview,
        "semantic_description": description,
        "semantic_evidence_fragment_ids": tuple(row.calendar_semantic_evidence_ids or ()),
        "semantic_description_fragment_ids": tuple(
            row.calendar_semantic_description_evidence_ids or ()
        ),
        "semantic_cache": _calendar_semantic_cache(row),
        "source_url": row.source_url,
        "source_last_edited_at": row.notion_last_edited_at,
        "source_fingerprint": row.calendar_semantic_source_fingerprint,
        "url_candidates": row.url_candidates,
    }


def _calendar_semantic_cache(row: Any) -> Mapping[str, Any]:
    return {
        "source_fingerprint": row.calendar_semantic_source_fingerprint,
        "source_last_edited_at": row.calendar_semantic_source_last_edited_at,
        "model_identity": row.calendar_semantic_model_identity,
        "config_version": row.calendar_semantic_config_version,
        "prompt_version": row.calendar_semantic_prompt_version,
        "analyzed_at": row.calendar_semantic_analyzed_at,
    }


def _calendar_label(value: datetime, *, is_all_day: bool) -> str:
    date_text = f"{value.strftime('%A, %B')} {value.day}, {value.year}"
    if is_all_day:
        return date_text
    return f"{date_text} at {value.strftime('%H:%M %Z')}".strip()


def _relative_day_label(target: date, anchor: date) -> str:
    days = (target - anchor).days
    if days == 0:
        return "Today"
    if days == 1:
        return "Tomorrow"
    if days > 1:
        return f"In {days} days"
    if days == -1:
        return "Yesterday"
    return f"{abs(days)} days ago"


def _interview_public(session: Session, row: CareerInterviewEvent) -> dict[str, Any]:
    plan = session.scalar(
        select(CareerPreparationPlan).where(CareerPreparationPlan.interview_id == row.id)
    )
    link = session.scalar(
        select(CareerInterviewApplicationLink).where(
            CareerInterviewApplicationLink.interview_id == row.id
        )
    )
    return {
        "id": row.id,
        "interview_page_id": row.interview_page_id,
        "title": row.title,
        "date_start": row.date_start,
        "local_date": row.local_date,
        "is_all_day": row.is_all_day,
        "timezone": row.timezone,
        "notion_last_edited_at": row.notion_last_edited_at,
        "source_url": row.source_url,
        "tags": row.tags,
        "url_candidates": row.url_candidates,
        "content_fingerprint": row.content_fingerprint,
        "calendar_semantic_status": row.calendar_semantic_status,
        "calendar_semantic_overview": row.calendar_semantic_overview,
        "calendar_semantic_description": row.calendar_semantic_description,
        "calendar_semantic_evidence_ids": row.calendar_semantic_evidence_ids,
        "calendar_semantic_description_evidence_ids": (
            row.calendar_semantic_description_evidence_ids
        ),
        "calendar_semantic_source_fingerprint": row.calendar_semantic_source_fingerprint,
        "calendar_semantic_source_last_edited_at": row.calendar_semantic_source_last_edited_at,
        "calendar_semantic_model_identity": row.calendar_semantic_model_identity,
        "calendar_semantic_config_version": row.calendar_semantic_config_version,
        "calendar_semantic_prompt_version": row.calendar_semantic_prompt_version,
        "calendar_semantic_analyzed_at": row.calendar_semantic_analyzed_at,
        "active": row.active,
        "archived": row.archived,
        "plan_revision": plan.revision if plan else None,
        "plan_next_actions": plan.next_actions if plan else [],
        "link_state": link.state if link else None,
    }


def _plan_public(
    row: CareerPreparationPlan,
    *,
    interview_page_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": row.id,
        "interview_id": row.interview_id,
        "interview_page_id": interview_page_id,
        "revision": row.revision,
        "status": row.status,
        "generated_at": row.generated_at,
        "plan_hash": row.plan_hash,
        "summary": row.summary,
        "next_actions": row.next_actions,
        "evidence": row.evidence,
        "research_snapshot_id": row.research_snapshot_id,
        "artifact_key": row.artifact_key,
        "material_change_reason": row.material_change_reason,
        "plan_payload": row.plan_payload,
    }


def _clarification_public(row: CareerClarification) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "subject_type": row.subject_type,
        "subject_id": row.subject_id,
        "question": row.question,
        "choices": row.choices,
        "state": row.state,
        "partial_state": row.partial_state,
        "discord_channel_id": row.discord_channel_id,
        "discord_user_id": row.discord_user_id,
        "expires_at": row.expires_at,
    }


def _proposal_public(row: CareerWriteProposal) -> dict[str, Any]:
    return {
        "id": row.id,
        "interview_id": row.interview_id,
        "operation": row.operation,
        "target_page_id": row.target_page_id,
        "expected_last_edited_at": row.expected_last_edited_at,
        "payload": row.payload,
        "redacted_preview": row.redacted_preview,
        "confirmation_token": row.confirmation_token,
        "state": row.state,
        "expires_at": row.expires_at,
        "applied_at": row.applied_at,
    }


def _cursor_scope(source_id: str) -> str:
    return f"notion:{_bounded(source_id, 255)}"


def _legacy_interview_fingerprint(interview: Any) -> str:
    last_edited_at = getattr(interview, "last_edited_at", None)
    payload = {
        "page_id": getattr(interview, "interview_page_id", None),
        "last_edited_at": (
            last_edited_at.isoformat() if isinstance(last_edited_at, datetime) else None
        ),
        "url_candidates": [dict(item) for item in getattr(interview, "url_candidates", ())[:25]],
        "evidence_fragments": [
            dict(item) for item in getattr(interview, "evidence_fragments", ())[:40]
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _diagnostic_status(code: str | None) -> str:
    if code in {"jobs_page_missing", "notion_configuration_missing"}:
        return "missing"
    if code == "jobs_page_duplicate":
        return "duplicate"
    if code in {None, "interview_date_missing", "interview_date_invalid"}:
        return "valid"
    return "malformed"


def _interview_contract(row: Mapping[str, Any]) -> InterviewEventSnapshot:
    local_date = row.get("local_date")
    if local_date is None:
        raise ValueError("cannot convert unscheduled interview to reminder contract")
    return InterviewEventSnapshot(
        interview_page_id=str(row["interview_page_id"]),
        title=str(row["title"]),
        date_start=(
            _aware_db(cast(datetime, row["date_start"]))
            if row.get("date_start") is not None
            else None
        ),
        local_date=cast(date, local_date),
        is_all_day=bool(row.get("is_all_day", False)),
        timezone=str(row.get("timezone") or "America/Toronto"),
        last_edited_at=_aware_db(cast(datetime, row["notion_last_edited_at"])),
        source_url=cast(str | None, row.get("source_url")),
        tags=tuple(cast(Sequence[str], row.get("tags") or ())),
        url_candidates=tuple(_url_candidate_contracts(row.get("url_candidates") or ())),
        content_fingerprint=str(row["content_fingerprint"]),
        calendar_semantic_status=cast(str | None, row.get("calendar_semantic_status")),
        calendar_semantic_overview=cast(str | None, row.get("calendar_semantic_overview")),
        calendar_semantic_description=cast(str | None, row.get("calendar_semantic_description")),
        calendar_semantic_evidence_ids=tuple(
            cast(Sequence[str], row.get("calendar_semantic_evidence_ids") or ())
        ),
        calendar_semantic_description_evidence_ids=tuple(
            cast(Sequence[str], row.get("calendar_semantic_description_evidence_ids") or ())
        ),
        calendar_semantic_source_fingerprint=cast(
            str | None,
            row.get("calendar_semantic_source_fingerprint"),
        ),
        calendar_semantic_source_last_edited_at=(
            _aware_db(cast(datetime, row["calendar_semantic_source_last_edited_at"]))
            if row.get("calendar_semantic_source_last_edited_at") is not None
            else None
        ),
        calendar_semantic_model_identity=cast(
            str | None,
            row.get("calendar_semantic_model_identity"),
        ),
        calendar_semantic_config_version=cast(
            str | None,
            row.get("calendar_semantic_config_version"),
        ),
        calendar_semantic_prompt_version=cast(
            str | None,
            row.get("calendar_semantic_prompt_version"),
        ),
        calendar_semantic_analyzed_at=(
            _aware_db(cast(datetime, row["calendar_semantic_analyzed_at"]))
            if row.get("calendar_semantic_analyzed_at") is not None
            else None
        ),
        active=bool(row.get("active", True)),
        archived=bool(row.get("archived", False)),
    )


def _plan_contract(row: Mapping[str, Any]) -> PreparationPlanSnapshot:
    interview_page_id = row.get("interview_page_id")
    if not interview_page_id:
        raise ValueError("preparation plan is missing its interview page identity")
    return PreparationPlanSnapshot(
        interview_page_id=str(interview_page_id),
        revision=int(row["revision"]),
        status=PreparationPlanStatus(str(row["status"])),
        generated_at=_aware_db(cast(datetime, row["generated_at"])),
        plan_hash=str(row["plan_hash"]),
        summary=str(row["summary"]),
        next_actions=tuple(cast(Sequence[str], row.get("next_actions") or ())),
        evidence=tuple(cast(Sequence[str], row.get("evidence") or ())),
        research_snapshot_id=str(row["research_snapshot_id"])
        if row.get("research_snapshot_id") is not None
        else None,
        artifact_key=cast(str | None, row.get("artifact_key")),
        material_change_reason=cast(str | None, row.get("material_change_reason")),
        plan=dict(cast(Mapping[str, Any], row.get("plan_payload") or {})),
    )


def _url_candidate_contracts(candidates: Sequence[Any]) -> list[UrlCandidate]:
    normalized: list[UrlCandidate] = []
    for index, candidate in enumerate(candidates):
        item = dict(candidate)
        item.setdefault(
            "source_id",
            item.get("source_block_id") or item.get("property_id") or str(index),
        )
        item.setdefault("source_kind", "block")
        normalized.append(UrlCandidate.model_validate(item))
    return normalized
