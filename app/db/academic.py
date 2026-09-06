"""Transaction-friendly persistence for the Phase 5 academic planner.

Only normalized planner facts and bounded document chunks cross this boundary.
Original Notion/PDF bodies are represented by artifact keys and are never
accepted by these repository APIs.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError, NoResultFound
from sqlalchemy.orm import Session

from app.db.models import (
    AcademicCheckIn,
    AcademicClarification,
    AcademicCourseCalendar,
    AcademicDocument,
    AcademicDocumentChunk,
    AcademicProposedChange,
    AcademicSetupReminder,
    AcademicSyncCursor,
    Assessment,
    AuditEvent,
    Course,
    FixedCommitment,
    PlanningPreference,
    StudyBlock,
    StudyPlan,
)
from app.db.repositories import AuditRepository

FactState = Literal["unconfirmed", "confirmed", "ambiguous", "rejected"]
CommitmentKind = Literal[
    "class",
    "test",
    "quiz",
    "midterm",
    "final",
    "deadline",
    "sleep",
    "commute",
    "personal",
    "event",
]
CHUNK_MAX_CHARS = 20_000
BOUNDED_TEXT_CHARS = 255
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TORONTO = ZoneInfo("America/Toronto")


@dataclass(frozen=True, slots=True)
class SourceCitation:
    """Page/block provenance attached to every extracted academic fact."""

    page: int | None = None
    block: str | None = None
    url: str | None = None

    def values(self) -> dict[str, Any]:
        if self.page is not None and self.page < 1:
            raise ValueError("source page must be positive")
        if self.block is not None and not self.block.strip():
            raise ValueError("source block must not be empty")
        return {
            "source_page": self.page,
            "source_block": self.block,
            "source_url": self.url,
        }


@dataclass(frozen=True, slots=True)
class DocumentChunkInput:
    """Bounded extracted text and its page/block citation."""

    ordinal: int
    content: str
    citation: SourceCitation = SourceCitation()
    heading: str | None = None
    token_count: int | None = None
    content_hash: str | None = None


@dataclass(frozen=True, slots=True)
class CourseCalendarInput:
    """Normalized discovered Notion Assessments source for one course page."""

    course_id: uuid.UUID
    course_page_id: str
    child_database_id: str | None
    child_data_source_id: str | None
    title_property_id: str | None = None
    title_property_name: str | None = None
    date_property_id: str | None = None
    date_property_name: str | None = None
    discovery_status: str = "valid"
    diagnostic_code: str | None = None
    diagnostic_fingerprint: str | None = None
    last_discovered_at: datetime | None = None
    last_synced_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AssessmentSourceTrace:
    """Trace fields preserved for guarded title-only Notion writes."""

    source_id: str
    source_scope: str
    notion_last_edited_at: datetime
    title_property_id: str
    label_source: str | None = None
    source_url: str | None = None
    active: bool = True
    archived: bool = False


@dataclass(frozen=True, slots=True)
class ClarificationInput:
    """Bounded durable Discord clarification request."""

    event_notion_id: str
    original_title: str
    quiz_preview_title: str
    assignment_preview_title: str
    expected_edited_at: datetime
    expires_at: datetime
    idempotency_key: str
    course_id: uuid.UUID | None = None
    assessment_id: uuid.UUID | None = None
    raw_label: str | None = None
    title_property_id: str | None = None


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _validate_fact(confidence: float, fact_state: FactState) -> None:
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    if fact_state not in {"unconfirmed", "confirmed", "ambiguous", "rejected"}:
        raise ValueError("invalid fact state")


def _upsert(
    session: Session,
    model: type[Any],
    filters: Sequence[Any],
    values: Mapping[str, Any],
) -> Any:
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
        return existing
    return instance


class AcademicRepository:
    """Idempotent upserts and guarded state transitions for planner workers."""

    @staticmethod
    def upsert_course(
        session: Session,
        *,
        notion_id: str,
        course_code: str,
        title: str,
        term: str,
        timezone: str = "America/Toronto",
        priority: int = 50,
        active: bool = True,
    ) -> Course:
        if (
            not notion_id.strip()
            or not course_code.strip()
            or not title.strip()
            or not term.strip()
        ):
            raise ValueError("course identity and title fields must not be empty")
        if not 0 <= priority <= 100:
            raise ValueError("course priority must be between 0 and 100")
        return _upsert(
            session,
            Course,
            [Course.notion_id == notion_id],
            {
                "notion_id": notion_id,
                "course_code": course_code,
                "title": title,
                "term": term,
                "timezone": timezone,
                "priority": priority,
                "active": active,
            },
        )

    @staticmethod
    def upsert_course_calendar(
        session: Session,
        *,
        calendar: CourseCalendarInput,
    ) -> AcademicCourseCalendar:
        allowed_status = {"valid", "missing", "inaccessible", "malformed", "duplicate"}
        if calendar.discovery_status not in allowed_status:
            raise ValueError("invalid course calendar discovery status")
        if not calendar.course_page_id.strip():
            raise ValueError("course page identifier must not be empty")
        if calendar.discovery_status == "valid" and (
            not calendar.child_database_id or not calendar.child_data_source_id
        ):
            raise ValueError("valid course calendars require database and data-source identifiers")
        values = {
            "course_id": calendar.course_id,
            "course_page_id": _bounded(calendar.course_page_id),
            "child_database_id": _bounded_optional(calendar.child_database_id),
            "child_data_source_id": _bounded_optional(calendar.child_data_source_id),
            "title_property_id": _bounded_optional(calendar.title_property_id),
            "title_property_name": _bounded_optional(calendar.title_property_name),
            "date_property_id": _bounded_optional(calendar.date_property_id),
            "date_property_name": _bounded_optional(calendar.date_property_name),
            "discovery_status": calendar.discovery_status,
            "diagnostic_code": _bounded_optional(calendar.diagnostic_code, 128),
            "diagnostic_fingerprint": _bounded_optional(calendar.diagnostic_fingerprint, 128),
            "last_discovered_at": (
                _utc(calendar.last_discovered_at, "last_discovered_at")
                if calendar.last_discovered_at is not None
                else None
            ),
            "last_synced_at": (
                _utc(calendar.last_synced_at, "last_synced_at")
                if calendar.last_synced_at is not None
                else None
            ),
        }
        return _upsert(
            session,
            AcademicCourseCalendar,
            [AcademicCourseCalendar.course_id == calendar.course_id],
            values,
        )

    @staticmethod
    def upsert_assessment(
        session: Session,
        *,
        notion_id: str,
        course_id: uuid.UUID,
        title: str,
        assessment_type: str,
        due_at: datetime | None,
        grade_weight_percent: float | None,
        estimated_minutes: int = 60,
        confidence_gap: float = 0.5,
        scope_size: float = 0.0,
        scope: str | None = None,
        confidence: float,
        fact_state: FactState,
        citation: SourceCitation,
        ambiguity_reason: str | None = None,
        completed: bool = False,
        trace: AssessmentSourceTrace | None = None,
    ) -> Assessment:
        _validate_fact(confidence, fact_state)
        if due_at is not None:
            due_at = _utc(due_at, "due_at")
        if grade_weight_percent is not None and not 0 <= grade_weight_percent <= 100:
            raise ValueError("grade weight must be between 0 and 100")
        if estimated_minutes <= 0 or not 0 <= confidence_gap <= 1 or not 0 <= scope_size <= 100:
            raise ValueError("assessment effort and uncertainty fields are out of bounds")
        values = {
            "course_id": course_id,
            "notion_id": notion_id,
            "title": title,
            "assessment_type": assessment_type,
            "due_at": due_at,
            "grade_weight_percent": grade_weight_percent,
            "estimated_minutes": estimated_minutes,
            "confidence_gap": confidence_gap,
            "scope_size": scope_size,
            "scope": scope,
            "confidence": confidence,
            "fact_state": fact_state,
            "ambiguity_reason": ambiguity_reason,
            "completed": completed,
            **citation.values(),
        }
        if trace is not None:
            values.update(
                {
                    "source_id": _bounded(trace.source_id),
                    "source_scope": _bounded(trace.source_scope),
                    "notion_last_edited_at": _utc(
                        trace.notion_last_edited_at, "notion_last_edited_at"
                    ),
                    "title_property_id": _bounded(trace.title_property_id),
                    "label_source": _bounded_optional(trace.label_source),
                    "source_url": trace.source_url or values["source_url"],
                    "active": trace.active,
                    "archived": trace.archived,
                }
            )
        return _upsert(session, Assessment, [Assessment.notion_id == notion_id], values)

    @staticmethod
    def reconcile_assessment_source(
        session: Session,
        *,
        source_id: str,
        seen_notion_ids: Iterable[str],
        synced_at: datetime,
    ) -> int:
        if not source_id.strip():
            raise ValueError("source_id must not be empty")
        seen = {item for item in seen_notion_ids if item.strip()}
        statement = select(Assessment).where(
            Assessment.source_id == source_id,
            Assessment.active.is_(True),
        )
        if seen:
            statement = statement.where(Assessment.notion_id.not_in(seen))
        rows = list(session.scalars(statement))
        current = _utc(synced_at, "synced_at")
        for row in rows:
            row.active = False
            row.archived = True
            row.notion_last_edited_at = row.notion_last_edited_at or current
        session.flush()
        return len(rows)

    @staticmethod
    def create_or_get_clarification(
        session: Session,
        *,
        request: ClarificationInput,
    ) -> AcademicClarification:
        if not request.event_notion_id.strip() or not request.idempotency_key.strip():
            raise ValueError("clarification event and idempotency identifiers must not be empty")
        expected = _utc(request.expected_edited_at, "expected_edited_at")
        expires = _utc(request.expires_at, "expires_at")
        if expires <= expected:
            raise ValueError("clarification expiry must be after the expected edit timestamp")
        existing = session.scalar(
            select(AcademicClarification).where(
                AcademicClarification.idempotency_key == request.idempotency_key
            )
        )
        if existing is not None:
            return existing
        values = {
            "course_id": request.course_id,
            "assessment_id": request.assessment_id,
            "event_notion_id": _bounded(request.event_notion_id),
            "original_title": _bounded(request.original_title, 1_024),
            "raw_label": _bounded_optional(request.raw_label, 1_024),
            "quiz_preview_title": _bounded(request.quiz_preview_title, 1_024),
            "assignment_preview_title": _bounded(request.assignment_preview_title, 1_024),
            "expected_edited_at": expected,
            "title_property_id": _bounded_optional(request.title_property_id),
            "idempotency_key": request.idempotency_key,
            "expires_at": expires,
        }
        return _upsert(
            session,
            AcademicClarification,
            [AcademicClarification.idempotency_key == request.idempotency_key],
            values,
        )

    @staticmethod
    def mark_clarification_delivered(
        session: Session,
        *,
        clarification_id: uuid.UUID,
        delivery_id: str,
        delivered_at: datetime,
    ) -> AcademicClarification:
        row = session.get(AcademicClarification, clarification_id)
        if row is None:
            raise NoResultFound(f"academic clarification {clarification_id} was not found")
        if row.state in {"pending", "delivered"}:
            row.state = "delivered"
            row.delivery_id = _bounded(delivery_id)
            row.delivered_at = _utc(delivered_at, "delivered_at")
        session.flush()
        return row

    @staticmethod
    def expire_clarifications(session: Session, *, now: datetime) -> int:
        current = _utc(now, "now")
        rows = list(
            session.scalars(
                select(AcademicClarification)
                .where(
                    AcademicClarification.state.in_(["pending", "delivered"]),
                    AcademicClarification.expires_at <= current,
                )
                .with_for_update()
            )
        )
        for row in rows:
            row.state = "expired"
        session.flush()
        return len(rows)

    @staticmethod
    def claim_clarification(
        session: Session,
        *,
        clarification_id: uuid.UUID,
        action: Literal["quiz", "assignment", "ignore"],
        actor_id: int,
        now: datetime,
    ) -> tuple[str, AcademicClarification]:
        row = session.scalar(
            select(AcademicClarification)
            .where(AcademicClarification.id == clarification_id)
            .with_for_update()
        )
        if row is None:
            raise NoResultFound(f"academic clarification {clarification_id} was not found")
        current = _utc(now, "now")
        if row.state in {"applied", "conflict", "failed", "ignored", "expired"}:
            return row.state, row
        if _aware_db(row.expires_at) <= current:
            row.state = "expired"
            session.flush()
            return "expired", row
        if row.state == "claimed":
            return "claimed", row
        row.decision = action
        row.decision_user_id = actor_id
        row.decision_at = current
        if action == "ignore":
            row.state = "ignored"
            row.write_status = "skipped"
            _add_clarification_audit(session, row, result="skipped")
            session.flush()
            return "ignored", row
        row.state = "claimed"
        row.write_status = "pending"
        session.flush()
        return "ready", row

    @staticmethod
    def mark_clarification_applied(
        session: Session,
        *,
        clarification_id: uuid.UUID,
        applied_at: datetime,
    ) -> AcademicClarification:
        row = session.get(AcademicClarification, clarification_id)
        if row is None:
            raise NoResultFound(f"academic clarification {clarification_id} was not found")
        if row.state == "applied":
            return row
        if row.state != "claimed" or row.decision not in {"quiz", "assignment"}:
            raise ValueError("clarification must be claimed for a write before applying")
        row.state = "applied"
        row.write_status = "applied"
        row.write_error_code = None
        row.decision_at = row.decision_at or _utc(applied_at, "applied_at")
        _add_clarification_audit(session, row, result="applied")
        session.flush()
        return row

    @staticmethod
    def mark_clarification_conflict(
        session: Session,
        *,
        clarification_id: uuid.UUID,
        error_code: str = "notion_precondition_failed",
    ) -> AcademicClarification:
        row = session.get(AcademicClarification, clarification_id)
        if row is None:
            raise NoResultFound(f"academic clarification {clarification_id} was not found")
        row.state = "conflict"
        row.write_status = "conflict"
        row.write_error_code = _bounded(error_code, 128)
        _add_clarification_audit(session, row, result="conflict")
        session.flush()
        return row

    @staticmethod
    def mark_clarification_failed(
        session: Session,
        *,
        clarification_id: uuid.UUID,
        error_code: str,
    ) -> AcademicClarification:
        row = session.get(AcademicClarification, clarification_id)
        if row is None:
            raise NoResultFound(f"academic clarification {clarification_id} was not found")
        row.state = "failed"
        row.write_status = "failed"
        row.write_error_code = _bounded(error_code, 128)
        _add_clarification_audit(session, row, result="failed")
        session.flush()
        return row

    @staticmethod
    def setup_reminder_due(
        session: Session,
        *,
        condition_code: str,
        fingerprint: str,
        reminder_day: date,
    ) -> bool:
        row = session.scalar(
            select(AcademicSetupReminder).where(
                AcademicSetupReminder.condition == _bounded(condition_code, 128),
                AcademicSetupReminder.schema_fingerprint == _bounded(fingerprint, 128),
                AcademicSetupReminder.reminder_day == reminder_day,
            )
        )
        return row is None

    @staticmethod
    def record_setup_reminder(
        session: Session,
        *,
        condition_code: str,
        fingerprint: str,
        reminder_day: date,
        affected_course_codes: Sequence[str] = (),
        delivered_at: datetime | None = None,
        delivery_id: str | None = None,
        error_code: str | None = None,
    ) -> AcademicSetupReminder:
        state = "failed" if error_code else "delivered"
        values = {
            "condition": _bounded(condition_code, 128),
            "schema_fingerprint": _bounded(fingerprint, 128),
            "reminder_day": reminder_day,
            "affected_course_codes": [_bounded(code, 64) for code in affected_course_codes[:20]],
            "state": state,
            "delivered_at": _utc(delivered_at, "delivered_at") if delivered_at else None,
            "delivery_id": _bounded_optional(delivery_id),
            "error_code": _bounded_optional(error_code, 128),
        }
        return _upsert(
            session,
            AcademicSetupReminder,
            [
                AcademicSetupReminder.condition == values["condition"],
                AcademicSetupReminder.schema_fingerprint == values["schema_fingerprint"],
                AcademicSetupReminder.reminder_day == reminder_day,
            ],
            values,
        )

    @staticmethod
    def clear_setup_reminders(
        session: Session,
        *,
        condition_code: str | None = None,
        fingerprint: str | None = None,
    ) -> int:
        statement = select(AcademicSetupReminder).where(AcademicSetupReminder.state != "cleared")
        if condition_code is not None:
            statement = statement.where(
                AcademicSetupReminder.condition == _bounded(condition_code, 128)
            )
        if fingerprint is not None:
            statement = statement.where(
                AcademicSetupReminder.schema_fingerprint == _bounded(fingerprint, 128)
            )
        rows = list(session.scalars(statement))
        for row in rows:
            row.state = "cleared"
        session.flush()
        return len(rows)

    @staticmethod
    def academic_notion_health(session: Session) -> dict[str, Any]:
        calendars = list(session.scalars(select(AcademicCourseCalendar)))
        invalid_calendars = sum(row.discovery_status != "valid" for row in calendars)
        last_sync = max(
            (row.last_synced_at for row in calendars if row.last_synced_at is not None),
            default=None,
        )
        pending_clarifications = session.scalar(
            select(func.count())
            .select_from(AcademicClarification)
            .where(AcademicClarification.state.in_(["pending", "delivered", "claimed"]))
        )
        write_failures = session.scalar(
            select(func.count())
            .select_from(AcademicClarification)
            .where(AcademicClarification.write_status.in_(["conflict", "failed"]))
        )
        reminder_rows = list(
            session.scalars(
                select(AcademicSetupReminder).where(AcademicSetupReminder.state != "cleared")
            )
        )
        active_assessments = session.scalar(
            select(func.count()).select_from(Assessment).where(Assessment.active.is_(True))
        )
        return {
            "course_count": session.scalar(select(func.count()).select_from(Course)) or 0,
            "calendar_count": len(calendars),
            "invalid_calendar_count": invalid_calendars,
            "active_assessment_count": active_assessments or 0,
            "pending_clarification_count": pending_clarifications or 0,
            "write_failure_count": write_failures or 0,
            "setup_reminder_count": len(reminder_rows),
            "setup_condition_codes": sorted({row.condition for row in reminder_rows})[:10],
            "last_sync_at": _aware_db(last_sync).isoformat() if last_sync is not None else None,
            "migration": "0009_notion_course_calendars",
        }

    @staticmethod
    def upsert_fixed_commitment(
        session: Session,
        *,
        notion_id: str,
        title: str,
        commitment_type: str,
        starts_at: datetime,
        ends_at: datetime,
        timezone: str,
        citation: SourceCitation,
        course_id: uuid.UUID | None = None,
        confidence: float = 0.0,
        fact_state: FactState = "unconfirmed",
        ambiguity_reason: str | None = None,
    ) -> FixedCommitment:
        _validate_fact(confidence, fact_state)
        starts_at = _utc(starts_at, "starts_at")
        ends_at = _utc(ends_at, "ends_at")
        if ends_at <= starts_at:
            raise ValueError("fixed commitment must end after it starts")
        values = {
            "course_id": course_id,
            "notion_id": notion_id,
            "title": title,
            "commitment_type": commitment_type,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "timezone": timezone,
            "confidence": confidence,
            "fact_state": fact_state,
            "ambiguity_reason": ambiguity_reason,
            **citation.values(),
        }
        return _upsert(
            session,
            FixedCommitment,
            [FixedCommitment.notion_id == notion_id],
            values,
        )

    @staticmethod
    def upsert_preferences(
        session: Session,
        *,
        scope: str,
        timezone: str,
        availability: Mapping[str, Any],
        daily_capacity_minutes: int,
        buffer_minutes: int,
        sleep_schedule: Mapping[str, Any] | None = None,
        version: str = "v1",
    ) -> PlanningPreference:
        if daily_capacity_minutes <= 0 or buffer_minutes < 0:
            raise ValueError("capacity must be positive and buffer must be nonnegative")
        values = {
            "scope": scope,
            "timezone": timezone,
            "availability": dict(availability),
            "daily_capacity_minutes": daily_capacity_minutes,
            "buffer_minutes": buffer_minutes,
            "sleep_schedule": dict(sleep_schedule or {}),
            "version": version,
        }
        return _upsert(session, PlanningPreference, [PlanningPreference.scope == scope], values)

    @staticmethod
    def upsert_document(
        session: Session,
        *,
        notion_id: str,
        document_version: str,
        title: str,
        document_type: str,
        retrieved_at: datetime,
        artifact_key: str,
        content_hash: str,
        source_url: str | None = None,
        course_id: uuid.UUID | None = None,
        access_classification: str = "private",
        extraction_status: str = "pending",
    ) -> AcademicDocument:
        if not _SHA256.fullmatch(artifact_key) or not _SHA256.fullmatch(content_hash):
            raise ValueError("artifact_key and content_hash must be SHA-256 hex values")
        values = {
            "notion_id": notion_id,
            "document_version": document_version,
            "title": title,
            "document_type": document_type,
            "retrieved_at": _utc(retrieved_at, "retrieved_at"),
            "artifact_key": artifact_key,
            "content_hash": content_hash,
            "source_url": source_url,
            "course_id": course_id,
            "access_classification": access_classification,
            "extraction_status": extraction_status,
        }
        return _upsert(
            session,
            AcademicDocument,
            [
                AcademicDocument.notion_id == notion_id,
                AcademicDocument.document_version == document_version,
            ],
            values,
        )

    @staticmethod
    def replace_document_chunks(
        session: Session,
        *,
        document_id: uuid.UUID,
        chunks: Iterable[DocumentChunkInput],
    ) -> list[AcademicDocumentChunk]:
        materialized = list(chunks)
        ordinals = [chunk.ordinal for chunk in materialized]
        if len(ordinals) != len(set(ordinals)) or any(ordinal < 0 for ordinal in ordinals):
            raise ValueError("chunk ordinals must be unique and nonnegative")
        delete_statement = delete(AcademicDocumentChunk).where(
            AcademicDocumentChunk.document_id == document_id
        )
        if ordinals:
            delete_statement = delete_statement.where(
                AcademicDocumentChunk.ordinal.not_in(ordinals)
            )
        session.execute(delete_statement)
        result: list[AcademicDocumentChunk] = []
        for chunk in materialized:
            if not chunk.content.strip() or len(chunk.content) > CHUNK_MAX_CHARS:
                raise ValueError("document chunks must be non-empty and bounded")
            if chunk.token_count is not None and chunk.token_count < 0:
                raise ValueError("token_count must not be negative")
            digest = chunk.content_hash or _sha256(chunk.content)
            if not _SHA256.fullmatch(digest):
                raise ValueError("chunk content_hash must be SHA-256 hex")
            values = {
                "document_id": document_id,
                "ordinal": chunk.ordinal,
                "heading": chunk.heading,
                "content": chunk.content,
                "token_count": chunk.token_count,
                "content_hash": digest,
                **chunk.citation.values(),
            }
            result.append(
                _upsert(
                    session,
                    AcademicDocumentChunk,
                    [
                        AcademicDocumentChunk.document_id == document_id,
                        AcademicDocumentChunk.ordinal == chunk.ordinal,
                    ],
                    values,
                )
            )
        return result

    @staticmethod
    def search_document_chunks(
        session: Session,
        *,
        query: str,
        course_id: uuid.UUID | None = None,
        term: str | None = None,
        document_type: str | None = None,
        access_classification: str = "private",
        limit: int = 12,
    ) -> list[AcademicDocumentChunk]:
        """Run bounded lexical retrieval with source-scope filters.

        PostgreSQL uses the generated ``tsvector`` column and SQLite uses a
        deterministic substring fallback solely for unit tests.
        """

        if not query.strip() or limit < 1:
            return []
        statement = (
            select(AcademicDocumentChunk)
            .join(AcademicDocument, AcademicDocument.id == AcademicDocumentChunk.document_id)
            .outerjoin(Course, Course.id == AcademicDocument.course_id)
        )
        if course_id is not None:
            statement = statement.where(AcademicDocument.course_id == course_id)
        if term is not None:
            statement = statement.where(Course.term == term)
        if document_type is not None:
            statement = statement.where(AcademicDocument.document_type == document_type)
        statement = statement.where(AcademicDocument.access_classification == access_classification)
        if session.get_bind().dialect.name == "postgresql":
            from sqlalchemy import func

            statement = statement.where(
                AcademicDocumentChunk.search_vector.op("@@")(
                    func.websearch_to_tsquery("simple", query)
                )
            )
        else:
            for word in re.findall(r"[\w-]+", query):
                pattern = f"%{word}%"
                statement = statement.where(
                    or_(
                        AcademicDocumentChunk.content.ilike(pattern),
                        AcademicDocumentChunk.heading.ilike(pattern),
                    )
                )
        statement = statement.order_by(
            AcademicDocumentChunk.document_id, AcademicDocumentChunk.ordinal
        )
        return list(session.scalars(statement.limit(limit)))

    @staticmethod
    def upsert_sync_cursor(
        session: Session,
        *,
        scope: str,
        source_version: str,
        cursor: str | None,
        status: str = "idle",
        last_synced_at: datetime | None = None,
        error_code: str | None = None,
    ) -> AcademicSyncCursor:
        values = {
            "scope": scope,
            "source": "notion",
            "source_version": source_version,
            "cursor": cursor,
            "status": status,
            "last_synced_at": _utc(last_synced_at, "last_synced_at")
            if last_synced_at is not None
            else None,
            "error_code": error_code,
        }
        return _upsert(session, AcademicSyncCursor, [AcademicSyncCursor.scope == scope], values)

    @staticmethod
    def upsert_study_plan(
        session: Session,
        *,
        plan_key: str,
        starts_on: date,
        ends_on: date,
        timezone: str,
        status: Literal["draft", "published", "superseded"] = "draft",
        preference_version: str | None = None,
    ) -> StudyPlan:
        if ends_on < starts_on:
            raise ValueError("study plan must end on or after its start")
        values = {
            "plan_key": plan_key,
            "starts_on": starts_on,
            "ends_on": ends_on,
            "timezone": timezone,
            "status": status,
            "preference_version": preference_version,
        }
        return _upsert(session, StudyPlan, [StudyPlan.plan_key == plan_key], values)

    @staticmethod
    def upsert_study_block(
        session: Session,
        *,
        plan_id: uuid.UUID,
        block_key: str,
        title: str,
        starts_at: datetime,
        ends_at: datetime,
        allocated_minutes: int,
        status: Literal[
            "planned", "in_progress", "completed", "incomplete", "carried_forward"
        ] = "planned",
        assessment_id: uuid.UUID | None = None,
        carry_forward_from_id: uuid.UUID | None = None,
        notes: str | None = None,
    ) -> StudyBlock:
        starts_at = _utc(starts_at, "starts_at")
        ends_at = _utc(ends_at, "ends_at")
        if ends_at <= starts_at or allocated_minutes <= 0:
            raise ValueError("study block must have positive duration and allocation")
        values = {
            "plan_id": plan_id,
            "block_key": block_key,
            "title": title,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "allocated_minutes": allocated_minutes,
            "status": status,
            "assessment_id": assessment_id,
            "carry_forward_from_id": carry_forward_from_id,
            "notes": notes,
        }
        return _upsert(
            session,
            StudyBlock,
            [StudyBlock.plan_id == plan_id, StudyBlock.block_key == block_key],
            values,
        )

    @staticmethod
    def carry_forward_incomplete_blocks(
        session: Session,
        *,
        source_plan_id: uuid.UUID,
        target_plan_id: uuid.UUID,
        starts_at: datetime,
        gap_minutes: int = 0,
    ) -> list[StudyBlock]:
        if gap_minutes < 0:
            raise ValueError("gap_minutes must not be negative")
        cursor = _utc(starts_at, "starts_at")
        source_blocks = list(
            session.scalars(
                select(StudyBlock)
                .where(
                    StudyBlock.plan_id == source_plan_id,
                    StudyBlock.status.in_(["incomplete", "in_progress"]),
                )
                .order_by(StudyBlock.starts_at, StudyBlock.id)
            )
        )
        result: list[StudyBlock] = []
        for source in source_blocks:
            duration = source.ends_at - source.starts_at
            key = f"{source.block_key}:carry:{target_plan_id}"
            carried = AcademicRepository.upsert_study_block(
                session,
                plan_id=target_plan_id,
                block_key=key[:255],
                assessment_id=source.assessment_id,
                title=source.title,
                starts_at=cursor,
                ends_at=cursor + duration,
                allocated_minutes=source.allocated_minutes,
                status="carried_forward",
                carry_forward_from_id=source.id,
                notes=source.notes,
            )
            result.append(carried)
            cursor = carried.ends_at + timedelta(minutes=gap_minutes)
        return result

    @staticmethod
    def create_checkin(
        session: Session,
        *,
        idempotency_key: str,
        external_event_id: str,
        channel: str,
        received_at: datetime,
        content_artifact_key: str | None = None,
        redacted_summary: str | None = None,
        status: Literal[
            "received", "questioned", "planned", "proposal_pending", "completed", "failed"
        ] = "received",
        plan_id: uuid.UUID | None = None,
    ) -> AcademicCheckIn:
        values = {
            "idempotency_key": idempotency_key,
            "external_event_id": external_event_id,
            "channel": channel,
            "received_at": _utc(received_at, "received_at"),
            "content_artifact_key": content_artifact_key,
            "redacted_summary": redacted_summary,
            "status": status,
            "plan_id": plan_id,
        }
        existing = session.scalar(
            select(AcademicCheckIn).where(AcademicCheckIn.idempotency_key == idempotency_key)
        )
        if existing is not None:
            if existing.external_event_id != external_event_id:
                raise ValueError("idempotency key is associated with another check-in event")
            return existing
        return _upsert(
            session,
            AcademicCheckIn,
            [AcademicCheckIn.external_event_id == external_event_id],
            values,
        )

    @staticmethod
    def create_proposed_change(
        session: Session,
        *,
        checkin_id: uuid.UUID,
        idempotency_key: str,
        operation: str,
        target_type: str,
        target_id: str,
        payload: Mapping[str, Any],
        redacted_preview: str,
        confirmation_token: str,
        expires_at: datetime | None = None,
    ) -> AcademicProposedChange:
        if not confirmation_token.strip():
            raise ValueError("confirmation token must not be empty")
        values = {
            "checkin_id": checkin_id,
            "idempotency_key": idempotency_key,
            "operation": operation,
            "target_type": target_type,
            "target_id": target_id,
            "payload": dict(payload),
            "redacted_preview": redacted_preview,
            "confirmation_token": confirmation_token,
            "expires_at": _utc(expires_at, "expires_at") if expires_at else None,
        }
        return _upsert(
            session,
            AcademicProposedChange,
            [AcademicProposedChange.idempotency_key == idempotency_key],
            values,
        )

    @staticmethod
    def confirm_proposed_change(
        session: Session,
        *,
        proposal_id: uuid.UUID,
        confirmation_event: str,
    ) -> AcademicProposedChange:
        proposal = session.get(AcademicProposedChange, proposal_id)
        if proposal is None:
            raise NoResultFound(f"academic proposal {proposal_id} was not found")
        if proposal.state in {"confirmed", "applying"} and (
            proposal.confirmation_event == confirmation_event
        ):
            return proposal
        if proposal.state != "pending":
            raise ValueError("academic proposal is no longer pending")
        if confirmation_event != proposal.confirmation_token:
            raise ValueError("confirmation event does not exactly match the proposal token")
        proposal.state = "confirmed"
        proposal.confirmation_event = confirmation_event
        session.flush()
        return proposal

    @staticmethod
    def begin_confirmed_change(
        session: Session,
        *,
        proposal_id: uuid.UUID,
        confirmation_event: str,
        now: datetime | None = None,
    ) -> tuple[str, AcademicProposedChange]:
        """Atomically claim one exactly confirmed external write.

        The durable ``applying`` state prevents concurrent confirmations or a
        retry after an uncertain network outcome from issuing another PATCH.
        """

        proposal = session.scalar(
            select(AcademicProposedChange)
            .where(AcademicProposedChange.id == proposal_id)
            .with_for_update()
        )
        if proposal is None:
            raise NoResultFound(f"academic proposal {proposal_id} was not found")
        current = now or datetime.now(UTC)
        if proposal.state == "applied":
            return "already_applied", proposal
        if proposal.state == "applying":
            return "in_progress", proposal
        if proposal.expires_at is not None and _aware_db(proposal.expires_at) <= current:
            proposal.state = "expired"
            session.flush()
            return "expired", proposal
        if proposal.state != "pending":
            return "confirmation_required", proposal
        if confirmation_event != proposal.confirmation_token:
            return "confirmation_required", proposal
        proposal.confirmation_event = confirmation_event
        proposal.state = "applying"
        session.flush()
        return "ready", proposal

    @staticmethod
    def mark_proposed_change_applied(
        session: Session,
        *,
        proposal_id: uuid.UUID,
        confirmation_event: str,
    ) -> AcademicProposedChange:
        proposal = session.get(AcademicProposedChange, proposal_id)
        if proposal is None:
            raise NoResultFound(f"academic proposal {proposal_id} was not found")
        if proposal.state == "applied":
            return proposal
        if proposal.state != "applying" or proposal.confirmation_event != confirmation_event:
            raise ValueError("proposal must be exactly confirmed before applying")
        proposal.state = "applied"
        proposal.applied_at = datetime.now(UTC)
        session.flush()
        return proposal


class SQLAlchemyAcademicPlannerStore:
    """Adapter implementing the academic planner's persistence protocol.

    The planner workflow stores Pydantic plans, while this adapter stores only
    their typed fields. It is intentionally synchronous so the workflow can
    choose its own worker-thread boundary around database calls.
    """

    def __init__(self, engine: Any, *, confirmation_ttl_hours: int = 24) -> None:
        if confirmation_ttl_hours < 1 or confirmation_ttl_hours > 168:
            raise ValueError("confirmation_ttl_hours must be between 1 and 168")
        self.engine = engine
        self.confirmation_ttl_hours = confirmation_ttl_hours

    def get_sync_cursor(self, database: str) -> str | None:
        scope = f"notion:{database}"
        with Session(self.engine) as session:
            row = session.scalar(
                select(AcademicSyncCursor).where(AcademicSyncCursor.scope == scope)
            )
            return row.cursor if row is not None else None

    def save_sync_cursor(self, database: str, cursor: str | None) -> None:
        with Session(self.engine) as session, session.begin():
            AcademicRepository.upsert_sync_cursor(
                session,
                scope=f"notion:{database}",
                source_version="notion-2025-09-03",
                cursor=cursor,
                last_synced_at=datetime.now(UTC),
            )

    def upsert_notion_page(self, page: Any) -> None:
        """Persist normalized Notion page properties without storing raw JSON."""

        props = page.properties
        if page.database == "courses":
            code = _notion_property(props, "course") or page.page_id
            with Session(self.engine) as session, session.begin():
                AcademicRepository.upsert_course(
                    session,
                    notion_id=page.page_id,
                    course_code=code,
                    title=code,
                    term=_notion_property(props, "term") or "unspecified",
                    priority=int(_notion_number(props, "priority") or 50),
                )
            return
        if page.database == "assessments":
            code = _notion_property(props, "course") or "unspecified"
            term = _notion_property(props, "term") or "unspecified"
            with Session(self.engine) as session, session.begin():
                course = session.scalar(
                    select(Course).where(Course.course_code == code, Course.term == term)
                )
                if course is None:
                    course = AcademicRepository.upsert_course(
                        session,
                        notion_id=f"notion-course:{code}:{term}",
                        course_code=code,
                        title=code,
                        term=term,
                    )
                due = _notion_datetime(props, "due")
                state: FactState = "confirmed" if due is not None else "ambiguous"
                AcademicRepository.upsert_assessment(
                    session,
                    notion_id=page.page_id,
                    course_id=course.id,
                    title=_notion_property(props, "title") or page.page_id,
                    assessment_type=_notion_property(props, "type") or "event",
                    due_at=due,
                    grade_weight_percent=_notion_number(props, "grade_weight"),
                    estimated_minutes=int(_notion_number(props, "estimated_time") or 60),
                    scope=_notion_property(props, "scope"),
                    confidence=1.0 if due is not None else 0.0,
                    fact_state=state,
                    ambiguity_reason=(
                        None if due is not None else "Notion assessment has no confirmed due date."
                    ),
                    citation=SourceCitation(url=page.url),
                    completed=(_notion_property(props, "status") or "").lower() == "completed",
                )

    def upsert_document(
        self,
        document: Any,
        *,
        course: str | None,
        term: str | None,
        source_page_id: str | None,
    ) -> str:
        from hashlib import sha256

        with Session(self.engine) as session, session.begin():
            course_id = None
            if course:
                course_row = session.scalar(
                    select(Course).where(
                        Course.course_code == course,
                        Course.term == (term or "unspecified"),
                    )
                )
                course_id = course_row.id if course_row else None
            row = AcademicRepository.upsert_document(
                session,
                notion_id=document.document_id,
                document_version=document.document_version,
                title=document.title,
                document_type=document.media_type,
                retrieved_at=datetime.now(UTC),
                artifact_key=sha256(document.text.encode()).hexdigest(),
                content_hash=sha256(document.text.encode()).hexdigest(),
                course_id=course_id,
            )
            return str(row.id)

    def replace_document_chunks(self, document_id: str, chunks: Sequence[Any]) -> None:
        inputs = [
            DocumentChunkInput(
                ordinal=int(chunk.chunk_index),
                content=chunk.content,
                heading=chunk.heading,
                citation=SourceCitation(page=chunk.page),
            )
            for chunk in chunks
        ]
        with Session(self.engine) as session, session.begin():
            AcademicRepository.replace_document_chunks(
                session, document_id=uuid.UUID(document_id), chunks=inputs
            )

    async def search_document_chunks(self, *, query: Any) -> list[dict[str, Any]]:
        params = query.parameters
        course_id = params.get("course_id")
        with Session(self.engine) as session:
            rows = AcademicRepository.search_document_chunks(
                session,
                query=str(params.get("question", "")),
                course_id=uuid.UUID(str(course_id)) if course_id else None,
                term=params.get("term"),
                document_type=params.get("document_type"),
                access_classification=str(params.get("access_classification", "private")),
                limit=int(params.get("limit", 8)),
            )
            results: list[dict[str, Any]] = []
            for row in rows:
                document = session.get(AcademicDocument, row.document_id)
                if document is None:
                    continue
                results.append(
                    {
                        "document_id": str(row.document_id),
                        "ordinal": row.ordinal,
                        "content": row.content,
                        "source_page": row.source_page,
                        "source_block": row.source_block,
                        "heading": row.heading,
                        "document_version": document.document_version,
                        "access_classification": document.access_classification,
                    }
                )
            return results

    def load_planner_facts(self, *, now: datetime, horizon_days: int) -> Any:
        from app.agents.academic_planner.contracts import (
            AmbiguousFact,
            AssessmentType,
            IncompleteBlock,
            PlannerFacts,
        )
        from app.agents.academic_planner.contracts import (
            Assessment as PlannerAssessment,
        )
        from app.agents.academic_planner.contracts import (
            FixedCommitment as PlannerCommitment,
        )

        current = _utc(now, "now")
        horizon = current + timedelta(days=horizon_days)
        with Session(self.engine) as session:
            courses = {
                course.id: course
                for course in session.scalars(select(Course).where(Course.active.is_(True)))
            }
            assessment_rows = list(
                session.scalars(
                    select(Assessment)
                    .where(
                        or_(
                            Assessment.due_at.is_(None),
                            and_(
                                Assessment.due_at > current,
                                Assessment.due_at <= horizon + timedelta(days=1),
                            ),
                        ),
                        Assessment.course_id.in_(courses) if courses else Assessment.id.is_(None),
                        Assessment.active.is_(True),
                    )
                    .order_by(Assessment.due_at, Assessment.notion_id)
                )
            )
            assessments: list[Any] = []
            ambiguous: list[Any] = []
            for row in assessment_rows:
                course = courses[row.course_id]
                citation = _citation_text(row.source_page, row.source_block, row.source_url)
                if row.fact_state == "ambiguous" or row.due_at is None:
                    ambiguous.append(
                        AmbiguousFact(
                            id=row.notion_id,
                            question=(
                                row.ambiguity_reason or f"Confirm the deadline for {row.title}."
                            ),
                            source_citation=citation,
                            candidate_value=row.due_at.isoformat() if row.due_at else None,
                        )
                    )
                    continue
                assessments.append(
                    PlannerAssessment(
                        id=row.notion_id,
                        course=course.course_code,
                        title=row.title,
                        assessment_type=_planner_assessment_type(
                            AssessmentType, row.assessment_type
                        ),
                        due_at=_aware_db(row.due_at),
                        estimated_minutes=row.estimated_minutes,
                        weight_percent=row.grade_weight_percent or 0,
                        course_priority=course.priority,
                        confidence_gap=row.confidence_gap,
                        scope_size=row.scope_size,
                        completed=row.completed,
                        ambiguous=row.fact_state != "confirmed",
                        citations=(citation,),
                    )
                )
            commitments = [
                PlannerCommitment(
                    id=row.notion_id,
                    title=row.title,
                    start_at=_aware_db(row.starts_at),
                    end_at=_aware_db(row.ends_at),
                    kind=_planner_commitment_kind(row.commitment_type),
                )
                for row in session.scalars(
                    select(FixedCommitment)
                    .where(
                        FixedCommitment.starts_at < horizon,
                        FixedCommitment.ends_at > current,
                        FixedCommitment.fact_state == "confirmed",
                    )
                    .order_by(FixedCommitment.starts_at)
                )
            ]
            incomplete_rows = list(
                session.scalars(
                    select(StudyBlock).where(StudyBlock.status.in_(["incomplete", "in_progress"]))
                )
            )
            linked_assessments = {
                assessment.id: assessment.notion_id
                for assessment in session.scalars(
                    select(Assessment).where(
                        Assessment.id.in_(
                            {
                                row.assessment_id
                                for row in incomplete_rows
                                if row.assessment_id is not None
                            }
                        )
                    )
                )
            }
            incomplete = [
                IncompleteBlock(
                    id=str(row.id),
                    assessment_id=linked_assessments.get(row.assessment_id, str(row.assessment_id)),
                    title=row.title,
                    remaining_minutes=row.allocated_minutes,
                    original_due_at=None,
                )
                for row in incomplete_rows
                if row.assessment_id is not None
            ]
            preference = session.scalar(
                select(PlanningPreference).order_by(PlanningPreference.updated_at.desc())
            )
            availability = _availability_windows(preference.availability if preference else {})
            buffer_minutes = preference.buffer_minutes if preference else 15
        return PlannerFacts(
            assessments=tuple(assessments),
            commitments=tuple(commitments),
            availability=tuple(availability),
            incomplete_blocks=tuple(incomplete),
            ambiguous_facts=tuple(ambiguous),
            buffer_minutes=buffer_minutes,
            horizon_days=horizon_days,
        )

    def save_daily_plan(self, plan: Any) -> None:
        local_day = _aware_db(plan.created_at).astimezone(_TORONTO).date()
        with Session(self.engine) as session, session.begin():
            stored = AcademicRepository.upsert_study_plan(
                session,
                plan_key=str(plan.plan_id),
                starts_on=local_day,
                ends_on=local_day,
                timezone="America/Toronto",
                status="published",
            )
            for block in plan.blocks:
                assessment = session.scalar(
                    select(Assessment).where(Assessment.notion_id == block.assessment_id)
                )
                AcademicRepository.upsert_study_block(
                    session,
                    plan_id=stored.id,
                    block_key=str(block.id),
                    assessment_id=assessment.id if assessment else None,
                    title=block.title,
                    starts_at=block.start_at,
                    ends_at=block.end_at,
                    allocated_minutes=max(
                        1, int((block.end_at - block.start_at).total_seconds() // 60)
                    ),
                    status="carried_forward" if block.carried_over else "planned",
                    notes=block.rationale,
                )

    def get_latest_daily_plan(self) -> Any | None:
        from app.agents.academic_planner.contracts import DailyPlan
        from app.agents.academic_planner.contracts import StudyBlock as PlannerBlock

        with Session(self.engine) as session:
            plan = session.scalar(
                select(StudyPlan)
                .order_by(StudyPlan.created_at.desc(), StudyPlan.starts_on.desc())
                .limit(1)
            )
            if plan is None:
                return None
            rows = list(
                session.scalars(
                    select(StudyBlock)
                    .where(StudyBlock.plan_id == plan.id)
                    .order_by(StudyBlock.starts_at, StudyBlock.id)
                )
            )
            blocks: list[Any] = []
            assessment_ids = {row.assessment_id for row in rows if row.assessment_id is not None}
            assessment_notion_ids = {
                assessment.id: assessment.notion_id
                for assessment in session.scalars(
                    select(Assessment).where(Assessment.id.in_(assessment_ids))
                )
            }
            for row in rows:
                assessment_id = (
                    assessment_notion_ids.get(row.assessment_id, str(row.assessment_id))
                    if row.assessment_id
                    else row.block_key
                )
                blocks.append(
                    PlannerBlock(
                        id=row.block_key,
                        assessment_id=assessment_id,
                        title=row.title,
                        start_at=_aware_db(row.starts_at),
                        end_at=_aware_db(row.ends_at),
                        carried_over=row.status == "carried_forward",
                        priority_score=0,
                        rationale=row.notes or "Persisted deterministic study block.",
                    )
                )
            return DailyPlan(
                plan_id=uuid.UUID(plan.plan_key),
                created_at=_aware_db(plan.created_at),
                blocks=tuple(blocks),
            )

    def save_checkin_proposal(self, proposal: Any) -> None:
        with Session(self.engine) as session, session.begin():
            checkin = AcademicRepository.create_checkin(
                session,
                idempotency_key=f"academic-checkin:{proposal.proposal_id}",
                external_event_id=f"proposal:{proposal.proposal_id}",
                channel="discord",
                received_at=datetime.now(UTC),
                redacted_summary="Academic check-in proposal pending confirmation.",
                status="proposal_pending",
                plan_id=proposal.source_plan_id,
            )
            AcademicRepository.create_proposed_change(
                session,
                checkin_id=checkin.id,
                idempotency_key=f"academic-proposal:{proposal.proposal_id}",
                operation="notion_update",
                target_type="academic_checkin",
                target_id=str(proposal.proposal_id),
                payload={
                    "changes": [change.model_dump(mode="json") for change in proposal.changes]
                },
                redacted_preview="Academic planner proposed changes; confirmation required.",
                confirmation_token=proposal.confirmation_event,
                expires_at=datetime.now(UTC) + timedelta(hours=self.confirmation_ttl_hours),
            )

    def get_checkin_proposal(self, proposal_id: uuid.UUID) -> Any | None:
        from app.agents.academic_planner.contracts import CheckinProposal, ProposedChange

        with Session(self.engine) as session:
            row = session.scalar(
                select(AcademicProposedChange).where(
                    AcademicProposedChange.idempotency_key == f"academic-proposal:{proposal_id}"
                )
            )
            if row is None:
                return None
            changes = tuple(ProposedChange(**value) for value in row.payload.get("changes", []))
            return CheckinProposal(
                proposal_id=proposal_id,
                confirmation_event=row.confirmation_token,
                changes=changes,
            )

    def prepare_checkin_application(
        self, proposal_id: uuid.UUID, confirmation_event: str
    ) -> tuple[str, Any | None]:
        """Validate, expire, and claim a proposal before its Notion write."""

        from app.agents.academic_planner.contracts import CheckinProposal, ProposedChange

        with Session(self.engine) as session, session.begin():
            row = session.scalar(
                select(AcademicProposedChange)
                .where(AcademicProposedChange.idempotency_key == f"academic-proposal:{proposal_id}")
                .with_for_update()
            )
            if row is None:
                return "not_found", None
            status, row = AcademicRepository.begin_confirmed_change(
                session,
                proposal_id=row.id,
                confirmation_event=confirmation_event,
            )
            changes = tuple(ProposedChange(**value) for value in row.payload.get("changes", []))
            proposal = CheckinProposal(
                proposal_id=proposal_id,
                confirmation_event=row.confirmation_token,
                changes=changes,
            )
            return status, proposal

    def mark_checkin_applied(
        self, proposal_id: uuid.UUID, confirmation_event: str | None = None
    ) -> None:
        with Session(self.engine) as session, session.begin():
            row = session.scalar(
                select(AcademicProposedChange).where(
                    AcademicProposedChange.idempotency_key == f"academic-proposal:{proposal_id}"
                )
            )
            if row is None:
                raise NoResultFound(f"academic proposal {proposal_id} was not found")
            AcademicRepository.mark_proposed_change_applied(
                session,
                proposal_id=row.id,
                confirmation_event=confirmation_event or row.confirmation_token,
            )
            checkin = session.get(AcademicCheckIn, row.checkin_id)
            if checkin is not None:
                checkin.status = "completed"
            AuditRepository.append(
                session,
                actor="academic_planner",
                action="notion.confirmed_write",
                target_type="academic_proposal",
                target_id=str(proposal_id),
                result="applied",
            )

    def upsert_course_calendar(
        self,
        course: Any,
        *,
        status: str = "valid",
        diagnostic_code: str | None = None,
        schema_fingerprint: str | None = None,
    ) -> str:
        """Persist one discovered course Assessments calendar mapping."""

        with Session(self.engine) as session, session.begin():
            course_row = _resolve_course(session, course)
            calendar = AcademicRepository.upsert_course_calendar(
                session,
                calendar=CourseCalendarInput(
                    course_id=course_row.id,
                    course_page_id=str(
                        _field(course, "course_page_id", "page_id", "notion_id")
                        or course_row.notion_id
                    ),
                    child_database_id=_field(
                        course,
                        "child_database_id",
                        "assessments_database_id",
                        "database_id",
                        "calendar_database_id",
                    ),
                    child_data_source_id=_field(
                        course,
                        "child_data_source_id",
                        "assessments_source_id",
                        "data_source_id",
                        "source_id",
                    ),
                    title_property_id=_field(course, "title_property_id"),
                    title_property_name=_field(course, "title_property_name"),
                    date_property_id=_field(course, "date_property_id"),
                    date_property_name=_field(course, "date_property_name"),
                    discovery_status=status,
                    diagnostic_code=diagnostic_code,
                    diagnostic_fingerprint=schema_fingerprint,
                    last_discovered_at=datetime.now(UTC),
                    last_synced_at=datetime.now(UTC)
                    if status == "valid"
                    else _field_datetime(course, "last_synced_at"),
                ),
            )
            return str(calendar.id)

    def upsert_synced_assessment(
        self,
        course: Any,
        assessment: Any,
        *,
        kind: str,
        label_source: str | None = None,
    ) -> str:
        """Persist one normalized assessment event and its Notion trace fields."""

        with Session(self.engine) as session, session.begin():
            course_row = _resolve_course(session, course)
            due_at = _field_datetime(assessment, "due_at", "date", "start")
            source_id = str(
                _field(assessment, "source_id", "assessments_source_id", "data_source_id") or ""
            )
            title_property_id = str(_field(assessment, "title_property_id") or "")
            edited_at = _field_datetime(assessment, "notion_last_edited_at", "last_edited_at")
            trace = None
            if source_id and title_property_id and edited_at is not None:
                trace = AssessmentSourceTrace(
                    source_id=source_id,
                    source_scope=f"notion:{source_id}",
                    notion_last_edited_at=edited_at,
                    title_property_id=title_property_id,
                    label_source=label_source,
                    source_url=_field(assessment, "source_url", "url"),
                    active=bool(
                        _field(assessment, "active")
                        if _field(assessment, "active") is not None
                        else True
                    ),
                    archived=bool(_field(assessment, "archived") or False),
                )
            row = AcademicRepository.upsert_assessment(
                session,
                notion_id=str(_field(assessment, "notion_id", "page_id", "id")),
                course_id=course_row.id,
                title=str(_field(assessment, "title", "current_title") or "Untitled assessment"),
                assessment_type=kind,
                due_at=due_at,
                grade_weight_percent=_field_number(assessment, "grade_weight_percent", "weight"),
                estimated_minutes=int(_field_number(assessment, "estimated_minutes") or 60),
                scope=_field(assessment, "scope"),
                confidence=float(
                    _field_number(assessment, "confidence") or (1.0 if due_at else 0.0)
                ),
                fact_state=_fact_state(_field(assessment, "fact_state"), due_at=due_at),
                ambiguity_reason=_field(assessment, "ambiguity_reason"),
                citation=SourceCitation(url=_field(assessment, "source_url", "url")),
                completed=bool(_field(assessment, "completed") or False),
                trace=trace,
            )
            return str(row.id)

    def reconcile_assessment_source(
        self,
        source_id: str,
        seen_ids: Iterable[str],
        *,
        synced_at: datetime | None = None,
    ) -> int:
        with Session(self.engine) as session, session.begin():
            return AcademicRepository.reconcile_assessment_source(
                session,
                source_id=source_id,
                seen_notion_ids=seen_ids,
                synced_at=synced_at or datetime.now(UTC),
            )

    def create_or_get_clarification(self, **kwargs: Any) -> str:
        with Session(self.engine) as session, session.begin():
            request = ClarificationInput(
                event_notion_id=str(kwargs["event_notion_id"]),
                original_title=str(kwargs["original_title"]),
                raw_label=kwargs.get("raw_label"),
                quiz_preview_title=str(kwargs["quiz_preview_title"]),
                assignment_preview_title=str(kwargs["assignment_preview_title"]),
                expected_edited_at=_utc(kwargs["expected_edited_at"], "expected_edited_at"),
                expires_at=_utc(kwargs["expires_at"], "expires_at"),
                idempotency_key=str(kwargs["idempotency_key"]),
                course_id=_uuid_optional(kwargs.get("course_id")),
                assessment_id=_uuid_optional(kwargs.get("assessment_id")),
                title_property_id=kwargs.get("title_property_id"),
            )
            return str(AcademicRepository.create_or_get_clarification(session, request=request).id)

    def get_clarification(self, clarification_id: uuid.UUID | str) -> dict[str, Any] | None:
        with Session(self.engine) as session:
            row = session.get(AcademicClarification, uuid.UUID(str(clarification_id)))
            return _clarification_public(row) if row is not None else None

    def claim_clarification(
        self,
        clarification_id: uuid.UUID | str,
        action: Literal["quiz", "assignment", "ignore"],
        actor_id: int,
        *,
        now: datetime | None = None,
    ) -> tuple[str, dict[str, Any]]:
        with Session(self.engine) as session, session.begin():
            status, row = AcademicRepository.claim_clarification(
                session,
                clarification_id=uuid.UUID(str(clarification_id)),
                action=action,
                actor_id=actor_id,
                now=now or datetime.now(UTC),
            )
            return status, _clarification_public(row)

    def expire_clarifications(self, *, now: datetime | None = None) -> int:
        with Session(self.engine) as session, session.begin():
            return AcademicRepository.expire_clarifications(
                session,
                now=now or datetime.now(UTC),
            )

    def mark_clarification_delivered(
        self,
        clarification_id: uuid.UUID | str,
        *,
        delivery_id: str,
        delivered_at: datetime | None = None,
    ) -> None:
        with Session(self.engine) as session, session.begin():
            AcademicRepository.mark_clarification_delivered(
                session,
                clarification_id=uuid.UUID(str(clarification_id)),
                delivery_id=delivery_id,
                delivered_at=delivered_at or datetime.now(UTC),
            )

    def mark_clarification_applied(
        self, clarification_id: uuid.UUID | str, *, applied_at: datetime | None = None
    ) -> None:
        with Session(self.engine) as session, session.begin():
            AcademicRepository.mark_clarification_applied(
                session,
                clarification_id=uuid.UUID(str(clarification_id)),
                applied_at=applied_at or datetime.now(UTC),
            )

    def mark_clarification_conflict(
        self, clarification_id: uuid.UUID | str, *, error_code: str = "notion_precondition_failed"
    ) -> None:
        with Session(self.engine) as session, session.begin():
            AcademicRepository.mark_clarification_conflict(
                session,
                clarification_id=uuid.UUID(str(clarification_id)),
                error_code=error_code,
            )

    def mark_clarification_failed(
        self, clarification_id: uuid.UUID | str, *, error_code: str
    ) -> None:
        with Session(self.engine) as session, session.begin():
            AcademicRepository.mark_clarification_failed(
                session,
                clarification_id=uuid.UUID(str(clarification_id)),
                error_code=error_code,
            )

    def setup_reminder_due(self, condition_code: str, fingerprint: str, day: date) -> bool:
        with Session(self.engine) as session:
            return AcademicRepository.setup_reminder_due(
                session,
                condition_code=condition_code,
                fingerprint=fingerprint,
                reminder_day=day,
            )

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
    ) -> None:
        with Session(self.engine) as session, session.begin():
            AcademicRepository.record_setup_reminder(
                session,
                condition_code=condition_code,
                fingerprint=fingerprint,
                reminder_day=day,
                affected_course_codes=affected_course_codes,
                delivered_at=delivered_at or datetime.now(UTC),
                delivery_id=delivery_id,
                error_code=error_code,
            )

    def clear_setup_reminders(
        self, condition_code: str | None = None, fingerprint: str | None = None
    ) -> int:
        with Session(self.engine) as session, session.begin():
            return AcademicRepository.clear_setup_reminders(
                session,
                condition_code=condition_code,
                fingerprint=fingerprint,
            )

    def academic_notion_health(self) -> dict[str, Any]:
        with Session(self.engine) as session:
            return AcademicRepository.academic_notion_health(session)

    # Short aliases are useful to host workers that use the generic store API.
    def save(self, plan: Any) -> None:
        self.save_daily_plan(plan)

    def get(self) -> Any | None:
        return self.get_latest_daily_plan()

    def mark_checkin(self, proposal_id: uuid.UUID) -> None:
        self.mark_checkin_applied(proposal_id)


def _aware_db(value: datetime) -> datetime:
    """Normalize SQLite's naive timestamp reads as UTC for planner contracts."""

    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _citation_text(page: int | None, block: str | None, url: str | None) -> str:
    parts: list[str] = []
    if page is not None:
        parts.append(f"page {page}")
    if block:
        parts.append(f"block {block}")
    if url:
        parts.append(url)
    return ", ".join(parts) or "source citation unavailable"


def _planner_assessment_type(enum_type: Any, value: str) -> Any:
    try:
        return enum_type(value)
    except ValueError:
        return enum_type.EVENT


def _planner_commitment_kind(value: str) -> CommitmentKind:
    allowed = {
        "class",
        "test",
        "quiz",
        "midterm",
        "final",
        "deadline",
        "sleep",
        "commute",
        "personal",
        "event",
    }
    return cast(CommitmentKind, value) if value in allowed else "event"


def _availability_windows(value: Mapping[str, Any]) -> list[Any]:
    from app.agents.academic_planner.contracts import AvailabilityWindow

    windows_value: Any = value.get("windows", [])
    if not isinstance(windows_value, list):
        return []
    result: list[AvailabilityWindow] = []
    for item in cast(list[Any], windows_value):
        if not isinstance(item, Mapping):
            continue
        item_map = cast(Mapping[str, Any], item)
        try:
            result.append(
                AvailabilityWindow(
                    start_at=datetime.fromisoformat(str(item_map["start_at"])),
                    end_at=datetime.fromisoformat(str(item_map["end_at"])),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return result


def _notion_property(properties: Mapping[str, Any], name: str) -> str | None:
    """Read a small text value from normalized or raw Notion property JSON."""

    value: Any = properties.get(name)
    if isinstance(value, str):
        return value.strip() or None
    if not isinstance(value, Mapping):
        return None
    value_map = cast(Mapping[str, Any], value)
    for key in ("title", "rich_text", "select", "status", "formula"):
        nested: Any = value_map.get(key)
        if isinstance(nested, list) and nested:
            nested = cast(list[Any], nested)[0]
        if isinstance(nested, Mapping):
            nested_map = cast(Mapping[str, Any], nested)
            text: Any = (
                nested_map.get("plain_text") or nested_map.get("name") or nested_map.get("string")
            )
            if isinstance(text, str) and text.strip():
                return text.strip()
        elif isinstance(nested, str) and nested.strip():
            return nested.strip()
    return None


def _notion_number(properties: Mapping[str, Any], name: str) -> float | None:
    value: Any = properties.get(name)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, Mapping):
        number: Any = cast(Mapping[str, Any], value).get("number")
        if isinstance(number, (int, float)) and not isinstance(number, bool):
            return float(number)
    return None


def _notion_datetime(properties: Mapping[str, Any], name: str) -> datetime | None:
    value: Any = properties.get(name)
    if isinstance(value, str):
        candidate: Any = value
    elif isinstance(value, Mapping):
        value_map = cast(Mapping[str, Any], value)
        nested: Any = value_map.get("date") or value_map.get("formula")
        candidate = (
            cast(Mapping[str, Any], nested).get("start") if isinstance(nested, Mapping) else nested
        )
    else:
        candidate = None
    if not isinstance(candidate, str):
        return None
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        return _utc(parsed, name)
    except ValueError:
        return None


def _sha256(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bounded(value: str, limit: int = BOUNDED_TEXT_CHARS) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("bounded text must not be empty")
    return stripped[:limit]


def _bounded_optional(value: str | None, limit: int = BOUNDED_TEXT_CHARS) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped[:limit] or None


def _uuid_optional(value: Any) -> uuid.UUID | None:
    return uuid.UUID(str(value)) if value is not None else None


def _parse_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _field(value: Any, *names: str) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            mapping = cast(Mapping[str, Any], value)
            return mapping[name]
        object_value = cast(object, value)
        if hasattr(object_value, name):
            return getattr(object_value, name)
    return None


def _field_number(value: Any, *names: str) -> float | None:
    candidate = _field(value, *names)
    if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
        return float(candidate)
    if isinstance(candidate, str):
        try:
            return float(candidate)
        except ValueError:
            return None
    return None


def _field_datetime(value: Any, *names: str) -> datetime | None:
    candidate = _field(value, *names)
    if isinstance(candidate, datetime):
        return _utc(candidate, names[0])
    if isinstance(candidate, str) and candidate.strip():
        try:
            return _utc(datetime.fromisoformat(candidate.replace("Z", "+00:00")), names[0])
        except ValueError:
            return None
    return None


def _fact_state(value: Any, *, due_at: datetime | None) -> FactState:
    candidate = str(value or ("confirmed" if due_at else "ambiguous"))
    if candidate not in {"unconfirmed", "confirmed", "ambiguous", "rejected"}:
        return "ambiguous"
    return cast(FactState, candidate)


def _resolve_course(session: Session, value: Any) -> Course:
    db_course_id = _parse_uuid(_field(value, "course_id", "id"))
    if db_course_id is not None:
        existing = session.get(Course, db_course_id)
        if existing is not None:
            return existing
    notion_id = str(
        _field(value, "notion_id", "page_id", "course_page_id", "course_id") or ""
    ).strip()
    if notion_id:
        existing = session.scalar(select(Course).where(Course.notion_id == notion_id))
        if existing is not None:
            return existing
    if not notion_id:
        raise ValueError("course must include an id or Notion page id")
    return AcademicRepository.upsert_course(
        session,
        notion_id=notion_id,
        course_code=str(_field(value, "course_code", "code") or notion_id),
        title=str(_field(value, "title", "course_title", "name") or notion_id),
        term=str(_field(value, "term") or "unspecified"),
        timezone=str(_field(value, "timezone") or "America/Toronto"),
        priority=int(_field_number(value, "priority") or 50),
        active=bool(_field(value, "active") if _field(value, "active") is not None else True),
    )


def _clarification_public(row: AcademicClarification) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "event_notion_id": row.event_notion_id,
        "course_id": str(row.course_id) if row.course_id is not None else None,
        "assessment_id": str(row.assessment_id) if row.assessment_id is not None else None,
        "original_title": row.original_title,
        "quiz_preview_title": row.quiz_preview_title,
        "assignment_preview_title": row.assignment_preview_title,
        "expected_edited_at": _aware_db(row.expected_edited_at).isoformat(),
        "title_property_id": row.title_property_id,
        "decision": row.decision,
        "state": row.state,
        "delivery_id": row.delivery_id,
        "delivered_at": _aware_db(row.delivered_at).isoformat()
        if row.delivered_at is not None
        else None,
        "write_status": row.write_status,
        "expires_at": _aware_db(row.expires_at).isoformat(),
    }


def _add_clarification_audit(
    session: Session,
    row: AcademicClarification,
    *,
    result: Literal["applied", "conflict", "failed", "skipped"],
) -> None:
    """Append an allowlisted write result without Discord or Notion payload data."""

    session.add(
        AuditEvent(
            actor="discord_authorized_user",
            action="notion_title_rename",
            target_type="notion_assessment",
            target_id=row.event_notion_id,
            result=result,
        )
    )


__all__ = [
    "AcademicRepository",
    "AssessmentSourceTrace",
    "ClarificationInput",
    "CourseCalendarInput",
    "DocumentChunkInput",
    "FactState",
    "SQLAlchemyAcademicPlannerStore",
    "SourceCitation",
]
