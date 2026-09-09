"""Transaction-friendly persistence for the Phase 5 academic planner.

Only normalized planner facts and bounded document chunks cross this boundary.
Original Notion/PDF bodies are represented by artifact keys and are never
accepted by these repository APIs.
"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError, NoResultFound
from sqlalchemy.orm import Session

from app.db.models import (
    AcademicCheckIn,
    AcademicClarification,
    AcademicCourseCalendar,
    AcademicDiscourseSession,
    AcademicDiscourseTurn,
    AcademicDocument,
    AcademicDocumentChunk,
    AcademicLearningFocus,
    AcademicLearningFocusEvent,
    AcademicProposalOperationJournal,
    AcademicProposedChange,
    AcademicReflectionMemory,
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
ProposalRejectionStatus = Literal[
    "rejected",
    "already_rejected",
    "expired",
    "already_applied",
    "in_progress",
    "not_pending",
]
ProposalOperationStatus = Literal["ready", "already_applied", "in_progress", "uncertain", "failed"]
LearningFocusReminderStatus = Literal["remind", "snoozed_and_remind", "delete"]
LearningFocusMutationStatus = Literal["applied", "not_found", "stale"]
AcademicDocumentSourceKind = Literal[
    "notion_page_body",
    "notion_property_file",
    "notion_block_file",
]
AcademicDocumentExtractionStatus = Literal[
    "pending",
    "extracted",
    "partial",
    "ocr_required",
    "ocr_processing",
    "unsupported",
    "failed",
    "inactive",
]
AcademicClarificationWriteAction = Literal[
    "quiz",
    "assignment",
    "tutorial",
    "lab",
    "studying_block",
]
AcademicClarificationAction = Literal[
    "quiz",
    "assignment",
    "tutorial",
    "lab",
    "studying_block",
    "ignore",
]
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
AGENT_CLARIFICATION_SESSION_KIND = "agent_clarification"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TORONTO = ZoneInfo("America/Toronto")
_ACADEMIC_CLARIFICATION_WRITE_ACTIONS = frozenset(
    ("quiz", "assignment", "tutorial", "lab", "studying_block")
)
_ACADEMIC_CLARIFICATION_ACTIONS = _ACADEMIC_CLARIFICATION_WRITE_ACTIONS | {"ignore"}
_ACADEMIC_DOCUMENT_SOURCE_KINDS = frozenset(
    ("notion_page_body", "notion_property_file", "notion_block_file")
)
_ACADEMIC_DOCUMENT_STATUSES = frozenset(
    (
        "pending",
        "extracted",
        "partial",
        "ocr_required",
        "ocr_processing",
        "unsupported",
        "failed",
        "inactive",
    )
)
_ACADEMIC_DOCUMENT_USABLE_STATUSES = frozenset(("extracted", "partial"))
_SIGNED_NOTION_URL_MARKERS = (
    "prod-files-secure.s3.",
    "prod-files-secure.notion-static.com",
    "x-amz-signature=",
    "x-amz-credential=",
    "x-amz-security-token=",
)


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
    embedding: Sequence[float] | None = None
    embedding_model: str | None = None


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
class AcademicCourseMutationTarget:
    """A discovered, writable per-course Assessments data source."""

    course_id: str
    course_code: str
    data_source_id: str
    title_property_id: str
    date_property_id: str


@dataclass(frozen=True, slots=True)
class AcademicAssessmentMutationTarget:
    """A synchronized assessment target with optimistic-write preconditions."""

    assessment_id: str
    course_id: str
    page_id: str
    title: str
    last_edited_at: datetime
    title_property_id: str
    date_property_id: str


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
    tutorial_preview_title: str | None = None
    lab_preview_title: str | None = None
    studying_block_preview_title: str | None = None
    course_id: uuid.UUID | None = None
    assessment_id: uuid.UUID | None = None
    raw_label: str | None = None
    title_property_id: str | None = None


@dataclass(frozen=True, slots=True)
class InboundCheckinPersistResult:
    """Durable inbound Discord dedupe result without private message content."""

    status: Literal["created", "replayed"]
    checkin_id: uuid.UUID
    checkin_status: str
    proposal_row_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class LearningFocusMemoryInput:
    """Raw reflection text plus optional embedding payload for one focus turn."""

    raw_text: str
    embedding: Sequence[float] | None = None
    embedding_model: str | None = None
    embedding_metadata: Mapping[str, Any] | None = None
    redacted_summary: str | None = None


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
        ends_at: datetime | None = None,
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
        if ends_at is not None:
            ends_at = _utc(ends_at, "ends_at")
            if due_at is None or ends_at <= due_at:
                raise ValueError("assessment end must be after its start")
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
            "ends_at": ends_at,
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
    def begin_proposal_operation(
        session: Session,
        *,
        proposal_id: uuid.UUID,
        ordinal: int,
        payload_hash: str,
        operation_id: str,
    ) -> tuple[ProposalOperationStatus, AcademicProposalOperationJournal]:
        if ordinal < 0:
            raise ValueError("proposal operation ordinal must be nonnegative")
        if not _SHA256.fullmatch(payload_hash):
            raise ValueError("proposal operation payload hash must be SHA-256 hex")
        normalized_operation_id = _bounded(operation_id, 255)
        row = session.scalar(
            select(AcademicProposalOperationJournal)
            .where(
                AcademicProposalOperationJournal.proposal_id == proposal_id,
                AcademicProposalOperationJournal.ordinal == ordinal,
            )
            .with_for_update()
        )
        if row is not None:
            if row.payload_hash != payload_hash:
                raise ValueError("proposal operation payload hash changed")
            if row.state == "applied":
                return "already_applied", row
            return cast(ProposalOperationStatus, row.state), row
        row = AcademicProposalOperationJournal(
            proposal_id=proposal_id,
            ordinal=ordinal,
            operation_id=normalized_operation_id,
            payload_hash=payload_hash,
            state="in_progress",
            receipt=None,
            error_code=None,
        )
        session.add(row)
        session.flush()
        return "ready", row

    @staticmethod
    def mark_proposal_operation_applied(
        session: Session,
        *,
        proposal_id: uuid.UUID,
        ordinal: int,
        payload_hash: str,
        receipt: Mapping[str, Any],
    ) -> AcademicProposalOperationJournal:
        row = _lock_proposal_operation(session, proposal_id, ordinal, payload_hash)
        row.state = "applied"
        row.receipt = _bounded_receipt(receipt)
        row.error_code = None
        session.flush()
        return row

    @staticmethod
    def mark_proposal_operation_uncertain(
        session: Session,
        *,
        proposal_id: uuid.UUID,
        ordinal: int,
        payload_hash: str,
        error_code: str,
    ) -> AcademicProposalOperationJournal:
        row = _lock_proposal_operation(session, proposal_id, ordinal, payload_hash)
        if row.state != "applied":
            row.state = "uncertain"
            row.error_code = _bounded_optional(error_code, 128)
        session.flush()
        return row

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
            "tutorial_preview_title": _bounded_optional(request.tutorial_preview_title, 1_024),
            "lab_preview_title": _bounded_optional(request.lab_preview_title, 1_024),
            "studying_block_preview_title": _bounded_optional(
                request.studying_block_preview_title, 1_024
            ),
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
        action: AcademicClarificationAction,
        actor_id: int,
        now: datetime,
    ) -> tuple[str, AcademicClarification]:
        if action not in _ACADEMIC_CLARIFICATION_ACTIONS:
            raise ValueError("academic clarification action is invalid")
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
        if row.state != "claimed" or row.decision not in _ACADEMIC_CLARIFICATION_WRITE_ACTIONS:
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
        material_status_counts = {
            status: int(count)
            for status, count in session.execute(
                select(AcademicDocument.extraction_status, func.count())
                .where(AcademicDocument.assessment_id.is_not(None))
                .group_by(AcademicDocument.extraction_status)
            )
        }
        return {
            "course_count": session.scalar(select(func.count()).select_from(Course)) or 0,
            "calendar_count": len(calendars),
            "invalid_calendar_count": invalid_calendars,
            "active_assessment_count": active_assessments or 0,
            "pending_clarification_count": pending_clarifications or 0,
            "write_failure_count": write_failures or 0,
            "setup_reminder_count": len(reminder_rows),
            "setup_condition_codes": sorted({row.condition for row in reminder_rows})[:10],
            "material_pending_count": material_status_counts.get("pending", 0)
            + material_status_counts.get("ocr_processing", 0),
            "material_failed_count": material_status_counts.get("failed", 0)
            + material_status_counts.get("unsupported", 0)
            + material_status_counts.get("ocr_required", 0),
            "material_partial_count": material_status_counts.get("partial", 0),
            "last_sync_at": _aware_db(last_sync).isoformat() if last_sync is not None else None,
            "migration": "0014_academic_materials",
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
        assessment_id: uuid.UUID | None = None,
        source_kind: AcademicDocumentSourceKind | str | None = None,
        source_page_id: str | None = None,
        source_block_id: str | None = None,
        source_property_id: str | None = None,
        source_key: str | None = None,
        original_filename: str | None = None,
        media_type: str | None = None,
        source_last_edited_at: datetime | None = None,
        access_classification: str = "private",
        extraction_status: AcademicDocumentExtractionStatus | str = "pending",
        active: bool = True,
        extraction_error_code: str | None = None,
        extraction_error_detail: str | None = None,
    ) -> AcademicDocument:
        if not _SHA256.fullmatch(artifact_key) or not _SHA256.fullmatch(content_hash):
            raise ValueError("artifact_key and content_hash must be SHA-256 hex values")
        normalized_source_kind = _document_source_kind(source_kind)
        normalized_status = _document_extraction_status(extraction_status)
        normalized_source_key = _bounded_optional(source_key, 512)
        if normalized_source_kind is not None and normalized_source_key is None:
            raise ValueError("source_key is required for assessment material documents")
        if normalized_source_key is not None and not normalized_source_key.strip():
            raise ValueError("source_key must not be empty")
        values = {
            "notion_id": notion_id,
            "document_version": document_version,
            "title": title,
            "document_type": document_type,
            "retrieved_at": _utc(retrieved_at, "retrieved_at"),
            "artifact_key": artifact_key,
            "content_hash": content_hash,
            "source_url": _durable_source_url(source_url),
            "course_id": course_id,
            "assessment_id": assessment_id,
            "source_kind": normalized_source_kind,
            "source_page_id": _bounded_optional(source_page_id),
            "source_block_id": _bounded_optional(source_block_id),
            "source_property_id": _bounded_optional(source_property_id),
            "source_key": normalized_source_key,
            "original_filename": _bounded_optional(original_filename, 500),
            "media_type": _bounded_optional(media_type, 255),
            "source_last_edited_at": (
                _utc(source_last_edited_at, "source_last_edited_at")
                if source_last_edited_at is not None
                else None
            ),
            "access_classification": access_classification,
            "extraction_status": normalized_status,
            "active": active,
            "extraction_error_code": _bounded_optional(extraction_error_code, 128),
            "extraction_error_detail": _bounded_optional(extraction_error_detail, 1_000),
        }
        filters = (
            [
                AcademicDocument.source_key == normalized_source_key,
                AcademicDocument.document_version == document_version,
            ]
            if normalized_source_key is not None
            else [
                AcademicDocument.notion_id == notion_id,
                AcademicDocument.document_version == document_version,
            ]
        )
        return _upsert(
            session,
            AcademicDocument,
            filters,
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
            embedding = _embedding_vector(chunk.embedding)
            embedding_model = _bounded_optional(chunk.embedding_model, 255)
            if embedding is not None and embedding_model is None:
                raise ValueError("embedding_model is required when an embedding is stored")
            values = {
                "document_id": document_id,
                "ordinal": chunk.ordinal,
                "heading": chunk.heading,
                "content": chunk.content,
                "token_count": chunk.token_count,
                "content_hash": digest,
                "embedding": embedding,
                "embedding_model": embedding_model,
                "embedding_dimensions": len(embedding) if embedding is not None else None,
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
        assessment_id: uuid.UUID | None = None,
        access_classification: str = "private",
        active_only: bool = False,
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
        if assessment_id is not None:
            statement = statement.where(AcademicDocument.assessment_id == assessment_id)
        if active_only:
            statement = statement.where(AcademicDocument.active.is_(True))
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
    def activate_document_version(
        session: Session,
        *,
        document_id: uuid.UUID,
    ) -> AcademicDocument:
        """Mark one usable source-keyed document as latest active and preserve history."""

        document = session.scalar(
            select(AcademicDocument).where(AcademicDocument.id == document_id).with_for_update()
        )
        if document is None:
            raise NoResultFound(f"academic document {document_id} was not found")
        if document.source_key is None:
            raise ValueError("only source-keyed material documents can be activated")
        if document.extraction_status not in _ACADEMIC_DOCUMENT_USABLE_STATUSES:
            raise ValueError("only extracted or partial documents can be activated")
        session.execute(
            update(AcademicDocument)
            .where(
                AcademicDocument.source_key == document.source_key,
                AcademicDocument.id != document.id,
            )
            .values(active=False)
        )
        document.active = True
        session.flush()
        return document

    @staticmethod
    def mark_document_source_inactive(
        session: Session,
        *,
        source_key: str,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> int:
        """Deactivate all current versions for a removed material source."""

        key = _bounded(source_key, 512)
        document_ids = list(
            session.scalars(
                select(AcademicDocument.id).where(
                    AcademicDocument.source_key == key,
                    AcademicDocument.active.is_(True),
                )
            )
        )
        if not document_ids:
            return 0
        session.execute(
            update(AcademicDocument)
            .where(AcademicDocument.id.in_(document_ids))
            .values(
                active=False,
                extraction_status="inactive",
                extraction_error_code=_bounded_optional(error_code, 128),
                extraction_error_detail=_bounded_optional(error_detail, 1_000),
            )
        )
        return len(document_ids)

    @staticmethod
    def list_assessment_materials(
        session: Session,
        *,
        assessment_id: uuid.UUID,
        active_only: bool = True,
        access_classification: str = "private",
        limit: int = 50,
    ) -> list[AcademicDocument]:
        """Return bounded material metadata for one assessment only."""

        if limit < 1 or limit > 100:
            raise ValueError("material list limit must be between 1 and 100")
        statement = (
            select(AcademicDocument)
            .where(
                AcademicDocument.assessment_id == assessment_id,
                AcademicDocument.access_classification == access_classification,
            )
            .order_by(
                AcademicDocument.source_key,
                AcademicDocument.retrieved_at.desc(),
                AcademicDocument.id,
            )
            .limit(limit)
        )
        if active_only:
            statement = statement.where(AcademicDocument.active.is_(True))
        return list(session.scalars(statement))

    @staticmethod
    def read_assessment_material_chunks(
        session: Session,
        *,
        assessment_id: uuid.UUID,
        chunk_ids: Sequence[uuid.UUID],
        access_classification: str = "private",
        active_only: bool = True,
        limit: int = 50,
    ) -> list[AcademicDocumentChunk]:
        """Read explicitly selected chunks after enforcing assessment ownership."""

        if limit < 1 or limit > 100:
            raise ValueError("chunk read limit must be between 1 and 100")
        ids = tuple(dict.fromkeys(chunk_ids))
        if not ids:
            return []
        if len(ids) > limit:
            raise ValueError("too many chunk ids requested")
        statement = (
            select(AcademicDocumentChunk)
            .join(AcademicDocument, AcademicDocument.id == AcademicDocumentChunk.document_id)
            .where(
                AcademicDocumentChunk.id.in_(ids),
                AcademicDocument.assessment_id == assessment_id,
                AcademicDocument.access_classification == access_classification,
            )
        )
        if active_only:
            statement = statement.where(AcademicDocument.active.is_(True))
        rows_by_id = {row.id: row for row in session.scalars(statement)}
        return [rows_by_id[chunk_id] for chunk_id in ids if chunk_id in rows_by_id]

    @staticmethod
    def search_semantic_document_chunks(
        session: Session,
        *,
        assessment_id: uuid.UUID,
        query_embedding: Sequence[float],
        embedding_model: str,
        access_classification: str = "private",
        limit: int = 8,
    ) -> list[tuple[AcademicDocumentChunk, float]]:
        """Run assessment-scoped exact cosine search over chunk embeddings."""

        vector = _embedding_vector(query_embedding)
        if vector is None:
            return []
        if limit < 1 or limit > 100:
            raise ValueError("semantic retrieval limit must be between 1 and 100")
        model = _bounded(embedding_model, 255)
        statement = (
            select(AcademicDocumentChunk)
            .join(AcademicDocument, AcademicDocument.id == AcademicDocumentChunk.document_id)
            .where(
                AcademicDocument.assessment_id == assessment_id,
                AcademicDocument.active.is_(True),
                AcademicDocument.access_classification == access_classification,
                AcademicDocumentChunk.embedding.is_not(None),
                AcademicDocumentChunk.embedding_model == model,
                AcademicDocumentChunk.embedding_dimensions == len(vector),
            )
        )
        if session.get_bind().dialect.name == "postgresql":
            distance = AcademicDocumentChunk.embedding.cosine_distance(vector).label("distance")
            rows = session.execute(statement.add_columns(distance).order_by(distance).limit(limit))
            return [
                (chunk, max(0.0, min(1.0, 1.0 - float(distance_value))))
                for chunk, distance_value in rows
            ]

        chunks = list(session.scalars(statement))
        ranked = sorted(
            (
                (_cosine_similarity(vector, cast(Sequence[float], chunk.embedding)), chunk)
                for chunk in chunks
            ),
            key=lambda item: item[0],
            reverse=True,
        )[:limit]
        return [(chunk, score) for score, chunk in ranked]

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
        learning_focus_id: uuid.UUID | None = None,
        block_kind: Literal["assessment", "practice"] = "assessment",
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
            "learning_focus_id": learning_focus_id,
            "block_kind": block_kind,
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
                learning_focus_id=source.learning_focus_id,
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
    def create_discourse_session(
        session: Session,
        *,
        external_event_id: str,
        started_at: datetime,
        channel: str = "discord",
        discord_channel_id: str | None = None,
        discord_user_id: str | None = None,
        session_kind: str = "learning_focus",
        partial_state: Mapping[str, Any] | None = None,
        expires_at: datetime | None = None,
    ) -> AcademicDiscourseSession:
        if channel != "discord":
            raise ValueError("academic discourse sessions are Discord-only in this phase")
        event_id = _bounded(external_event_id)
        existing = session.scalar(
            select(AcademicDiscourseSession).where(
                AcademicDiscourseSession.external_event_id == event_id
            )
        )
        if existing is not None:
            return existing
        started = _utc(started_at, "started_at")
        return _upsert(
            session,
            AcademicDiscourseSession,
            [AcademicDiscourseSession.external_event_id == event_id],
            {
                "external_event_id": event_id,
                "channel": channel,
                "discord_channel_id": _bounded_optional(discord_channel_id, 24),
                "discord_user_id": _bounded_optional(discord_user_id, 24),
                "session_kind": _bounded(session_kind, 64),
                "state": "open",
                "partial_state": dict(partial_state or {}),
                "missed_review_count": 0,
                "reminder_count": 0,
                "started_at": started,
                "last_turn_at": started,
                "expires_at": _utc(expires_at, "expires_at") if expires_at else None,
            },
        )

    @staticmethod
    def resume_discourse_session(
        session: Session,
        *,
        session_id: uuid.UUID | None = None,
        external_event_id: str | None = None,
        now: datetime,
        partial_state: Mapping[str, Any] | None = None,
    ) -> AcademicDiscourseSession:
        if (session_id is None) == (external_event_id is None):
            raise ValueError("resume requires exactly one session identifier")
        statement = select(AcademicDiscourseSession).with_for_update()
        if session_id is not None:
            statement = statement.where(AcademicDiscourseSession.id == session_id)
        else:
            statement = statement.where(
                AcademicDiscourseSession.external_event_id == _bounded(cast(str, external_event_id))
            )
        row = session.scalar(statement)
        if row is None:
            raise NoResultFound("academic discourse session was not found")
        current = _utc(now, "now")
        if row.expires_at is not None and _aware_db(row.expires_at) <= current:
            row.state = "expired"
            row.partial_state = {}
            session.flush()
            return row
        if row.state == "open":
            row.last_turn_at = current
            if partial_state is not None:
                row.partial_state = {**row.partial_state, **dict(partial_state)}
        session.flush()
        return row

    @staticmethod
    def find_open_discourse_session(
        session: Session,
        *,
        discord_channel_id: str,
        discord_user_id: str,
        now: datetime,
        session_kind: str | None = None,
    ) -> AcademicDiscourseSession | None:
        """Return the owner's latest unexpired clarification session."""

        current = _utc(now, "now")
        statement = (
            select(AcademicDiscourseSession)
            .where(
                AcademicDiscourseSession.state == "open",
                AcademicDiscourseSession.discord_channel_id == _bounded(discord_channel_id, 24),
                AcademicDiscourseSession.discord_user_id == _bounded(discord_user_id, 24),
                or_(
                    AcademicDiscourseSession.expires_at.is_(None),
                    AcademicDiscourseSession.expires_at > current,
                ),
            )
            .order_by(AcademicDiscourseSession.last_turn_at.desc())
            .limit(1)
        )
        if session_kind is not None:
            statement = statement.where(
                AcademicDiscourseSession.session_kind == _bounded(session_kind, 64)
            )
        return session.scalar(statement)

    @staticmethod
    def complete_discourse_session(
        session: Session,
        *,
        session_id: uuid.UUID,
        completed_at: datetime,
        final_state: Mapping[str, Any] | None = None,
    ) -> AcademicDiscourseSession:
        row = session.get(AcademicDiscourseSession, session_id)
        if row is None:
            raise NoResultFound(f"academic discourse session {session_id} was not found")
        current = _utc(completed_at, "completed_at")
        if row.state == "open":
            row.state = "completed"
            row.completed_at = current
            row.last_turn_at = current
            if final_state is not None:
                row.partial_state = {**row.partial_state, **dict(final_state)}
        session.flush()
        return row

    @staticmethod
    def record_discourse_turn(
        session: Session,
        *,
        session_id: uuid.UUID,
        external_event_id: str,
        received_at: datetime,
    ) -> tuple[AcademicDiscourseTurn, bool]:
        """Record one inbound Discord event and report whether it was new."""

        event_id = _bounded(external_event_id)
        existing = session.scalar(
            select(AcademicDiscourseTurn).where(AcademicDiscourseTurn.external_event_id == event_id)
        )
        if existing is not None:
            return existing, False
        row = AcademicDiscourseTurn(
            session_id=session_id,
            external_event_id=event_id,
            received_at=_utc(received_at, "received_at"),
        )
        try:
            with session.begin_nested():
                session.add(row)
                session.flush()
        except IntegrityError:
            existing = session.scalar(
                select(AcademicDiscourseTurn).where(
                    AcademicDiscourseTurn.external_event_id == event_id
                )
            )
            if existing is None:
                raise
            return existing, False
        return row, True

    @staticmethod
    def find_discourse_turn(
        session: Session,
        *,
        external_event_id: str,
    ) -> AcademicDiscourseTurn | None:
        return session.scalar(
            select(AcademicDiscourseTurn).where(
                AcademicDiscourseTurn.external_event_id == _bounded(external_event_id)
            )
        )

    @staticmethod
    def lock_open_agent_clarification_session(
        session: Session,
        *,
        discord_channel_id: str,
        discord_user_id: str,
        now: datetime,
    ) -> AcademicDiscourseSession | None:
        current = _utc(now, "now")
        return session.scalar(
            select(AcademicDiscourseSession)
            .where(
                AcademicDiscourseSession.state == "open",
                AcademicDiscourseSession.session_kind == AGENT_CLARIFICATION_SESSION_KIND,
                AcademicDiscourseSession.discord_channel_id == _bounded(discord_channel_id, 24),
                AcademicDiscourseSession.discord_user_id == _bounded(discord_user_id, 24),
                or_(
                    AcademicDiscourseSession.expires_at.is_(None),
                    AcademicDiscourseSession.expires_at > current,
                ),
            )
            .order_by(AcademicDiscourseSession.last_turn_at.desc(), AcademicDiscourseSession.id)
            .limit(1)
            .with_for_update()
        )

    @staticmethod
    def create_agent_clarification_session(
        session: Session,
        *,
        external_event_id: str,
        discord_channel_id: str,
        discord_user_id: str,
        started_at: datetime,
        expires_at: datetime,
        partial_state: Mapping[str, Any],
    ) -> AcademicDiscourseSession:
        return AcademicRepository.create_discourse_session(
            session,
            external_event_id=external_event_id,
            discord_channel_id=discord_channel_id,
            discord_user_id=discord_user_id,
            session_kind=AGENT_CLARIFICATION_SESSION_KIND,
            partial_state=partial_state,
            started_at=started_at,
            expires_at=expires_at,
        )

    @staticmethod
    def replace_agent_clarification_state(
        session: Session,
        *,
        session_id: uuid.UUID,
        now: datetime,
        partial_state: Mapping[str, Any],
    ) -> AcademicDiscourseSession:
        row = session.scalar(
            select(AcademicDiscourseSession)
            .where(
                AcademicDiscourseSession.id == session_id,
                AcademicDiscourseSession.session_kind == AGENT_CLARIFICATION_SESSION_KIND,
            )
            .with_for_update()
        )
        if row is None:
            raise NoResultFound(f"academic agent clarification {session_id} was not found")
        current = _utc(now, "now")
        if row.expires_at is not None and _aware_db(row.expires_at) <= current:
            row.state = "expired"
            row.partial_state = {}
        elif row.state == "open":
            row.partial_state = dict(partial_state)
            row.last_turn_at = current
        session.flush()
        return row

    @staticmethod
    def close_agent_clarification_session(
        session: Session,
        *,
        session_id: uuid.UUID,
        completed_at: datetime,
        final_state: Mapping[str, Any],
    ) -> AcademicDiscourseSession:
        row = session.scalar(
            select(AcademicDiscourseSession)
            .where(
                AcademicDiscourseSession.id == session_id,
                AcademicDiscourseSession.session_kind == AGENT_CLARIFICATION_SESSION_KIND,
            )
            .with_for_update()
        )
        if row is None:
            raise NoResultFound(f"academic agent clarification {session_id} was not found")
        current = _utc(completed_at, "completed_at")
        if row.state == "open":
            row.state = "completed"
            row.completed_at = current
        row.last_turn_at = current
        row.partial_state = dict(final_state)
        session.flush()
        return row

    @staticmethod
    def expire_discourse_sessions(session: Session, *, now: datetime) -> int:
        current = _utc(now, "now")
        rows = list(
            session.scalars(
                select(AcademicDiscourseSession)
                .where(
                    AcademicDiscourseSession.state == "open",
                    AcademicDiscourseSession.expires_at.is_not(None),
                    AcademicDiscourseSession.expires_at <= current,
                )
                .with_for_update()
            )
        )
        for row in rows:
            row.state = "expired"
            row.partial_state = {}
        session.flush()
        return len(rows)

    @staticmethod
    def create_learning_focus(
        session: Session,
        *,
        topic: str,
        now: datetime,
        course_id: uuid.UUID | None = None,
        assessment_id: uuid.UUID | None = None,
        course_code: str | None = None,
        source_session_id: uuid.UUID | None = None,
        source_external_event_id: str | None = None,
        next_review_at: datetime | None = None,
        practice_due_on: date | None = None,
        practice_minutes: int | None = None,
        memory: LearningFocusMemoryInput | None = None,
        actor: str = "academic_planner",
        owner_user_id: str | None = None,
        owner_channel_id: str | None = None,
    ) -> AcademicLearningFocus:
        current = _utc(now, "now")
        event_id = _bounded_optional(source_external_event_id)
        if event_id is not None:
            existing = session.scalar(
                select(AcademicLearningFocus).where(
                    AcademicLearningFocus.source_external_event_id == event_id
                )
            )
            if existing is not None:
                return existing
            event = session.scalar(
                select(AcademicLearningFocusEvent).where(
                    AcademicLearningFocusEvent.external_event_id == event_id
                )
            )
            if event is not None:
                replayed = session.get(AcademicLearningFocus, event.focus_id)
                if replayed is not None:
                    return replayed
        if practice_minutes is not None and practice_minutes <= 0:
            raise ValueError("practice_minutes must be positive")
        source_session = (
            session.get(AcademicDiscourseSession, source_session_id)
            if source_session_id is not None
            else None
        )
        resolved_owner_user_id = owner_user_id or (
            source_session.discord_user_id if source_session is not None else None
        )
        resolved_owner_channel_id = owner_channel_id or (
            source_session.discord_channel_id if source_session is not None else None
        )
        focus = AcademicLearningFocus(
            course_id=course_id,
            assessment_id=assessment_id,
            course_code=_bounded_optional(course_code, 64),
            topic=_bounded(topic),
            status="active",
            owner_user_id=_bounded_optional(resolved_owner_user_id, 24),
            owner_channel_id=_bounded_optional(resolved_owner_channel_id, 24),
            revision=1,
            source_session_id=source_session_id,
            source_external_event_id=event_id,
            reinforcement_count=1,
            next_review_at=_utc(next_review_at, "next_review_at") if next_review_at else None,
            practice_due_on=practice_due_on,
            practice_minutes=practice_minutes,
            last_reinforced_at=current,
        )
        session.add(focus)
        session.flush()
        _append_learning_focus_event(
            session,
            focus_id=focus.id,
            session_id=source_session_id,
            external_event_id=event_id,
            event_type="created",
            actor=actor,
            occurred_at=current,
            payload={
                "topic": focus.topic,
                "course_code": focus.course_code,
                "practice_due_on": practice_due_on.isoformat() if practice_due_on else None,
                "practice_minutes": practice_minutes,
            },
        )
        if memory is not None:
            _persist_reflection_memory(
                session,
                focus_id=focus.id,
                session_id=source_session_id,
                external_event_id=event_id,
                memory=memory,
                recorded_at=current,
            )
        return focus

    @staticmethod
    def reinforce_learning_focus(
        session: Session,
        *,
        focus_id: uuid.UUID,
        now: datetime,
        source_session_id: uuid.UUID | None = None,
        external_event_id: str | None = None,
        next_review_at: datetime | None = None,
        practice_due_on: date | None = None,
        practice_minutes: int | None = None,
        memory: LearningFocusMemoryInput | None = None,
        actor: str = "academic_planner",
        owner_user_id: str | None = None,
        owner_channel_id: str | None = None,
        expected_revision: int | None = None,
    ) -> AcademicLearningFocus:
        event_id = _bounded_optional(external_event_id)
        if event_id is not None:
            existing_event = session.scalar(
                select(AcademicLearningFocusEvent).where(
                    AcademicLearningFocusEvent.external_event_id == event_id
                )
            )
            if existing_event is not None:
                if existing_event.focus_id != focus_id:
                    raise ValueError("external event is associated with another learning focus")
                existing_focus = session.get(AcademicLearningFocus, focus_id)
                if existing_focus is None:
                    raise NoResultFound(f"academic learning focus {focus_id} was not found")
                return existing_focus
        filters = [AcademicLearningFocus.id == focus_id]
        if owner_user_id is not None:
            filters.append(AcademicLearningFocus.owner_user_id == _bounded(owner_user_id, 24))
        if owner_channel_id is not None:
            filters.append(AcademicLearningFocus.owner_channel_id == _bounded(owner_channel_id, 24))
        focus = session.scalar(select(AcademicLearningFocus).where(*filters).with_for_update())
        if focus is None:
            raise NoResultFound(f"academic learning focus {focus_id} was not found")
        if expected_revision is not None and focus.revision != expected_revision:
            raise NoResultFound("academic learning focus revision changed")
        if practice_minutes is not None and practice_minutes <= 0:
            raise ValueError("practice_minutes must be positive")
        current = _utc(now, "now")
        focus.status = "active"
        focus.snoozed_at = None
        focus.reinforcement_count += 1
        focus.revision += 1
        focus.last_reinforced_at = current
        focus.last_reviewed_at = current
        focus.last_review_prompted_at = None
        focus.missed_review_count = 0
        focus.reminder_count = 0
        focus.last_reminded_at = None
        if source_session_id is not None:
            focus.source_session_id = source_session_id
        if next_review_at is not None:
            focus.next_review_at = _utc(next_review_at, "next_review_at")
        if practice_due_on is not None:
            focus.practice_due_on = practice_due_on
        if practice_minutes is not None:
            focus.practice_minutes = practice_minutes
        _append_learning_focus_event(
            session,
            focus_id=focus.id,
            session_id=source_session_id,
            external_event_id=event_id,
            event_type="reinforced",
            actor=actor,
            occurred_at=current,
            payload={
                "next_review_at": focus.next_review_at.isoformat()
                if focus.next_review_at is not None
                else None,
                "practice_due_on": focus.practice_due_on.isoformat()
                if focus.practice_due_on is not None
                else None,
                "practice_minutes": focus.practice_minutes,
            },
        )
        if memory is not None:
            _persist_reflection_memory(
                session,
                focus_id=focus.id,
                session_id=source_session_id,
                external_event_id=event_id,
                memory=memory,
                recorded_at=current,
            )
        session.flush()
        return focus

    @staticmethod
    def list_active_learning_focuses(
        session: Session,
        *,
        course_id: uuid.UUID | None = None,
        include_snoozed: bool = False,
        limit: int = 50,
        owner_user_id: str | None = None,
        owner_channel_id: str | None = None,
    ) -> list[AcademicLearningFocus]:
        if limit < 1:
            return []
        statuses = ["active", "snoozed"] if include_snoozed else ["active"]
        statement = (
            select(AcademicLearningFocus)
            .where(AcademicLearningFocus.status.in_(statuses))
            .order_by(AcademicLearningFocus.next_review_at, AcademicLearningFocus.topic)
            .limit(limit)
        )
        if course_id is not None:
            statement = statement.where(AcademicLearningFocus.course_id == course_id)
        if owner_user_id is not None:
            statement = statement.where(
                AcademicLearningFocus.owner_user_id == _bounded(owner_user_id, 24)
            )
        if owner_channel_id is not None:
            statement = statement.where(
                AcademicLearningFocus.owner_channel_id == _bounded(owner_channel_id, 24)
            )
        return list(session.scalars(statement))

    @staticmethod
    def list_due_learning_focus_reviews(
        session: Session,
        *,
        now: datetime,
        limit: int = 50,
    ) -> list[AcademicLearningFocus]:
        if limit < 1:
            return []
        current = _utc(now, "now")
        return list(
            session.scalars(
                select(AcademicLearningFocus)
                .where(
                    AcademicLearningFocus.status.in_(["active", "snoozed"]),
                    AcademicLearningFocus.next_review_at.is_not(None),
                    AcademicLearningFocus.next_review_at <= current,
                )
                .order_by(AcademicLearningFocus.next_review_at, AcademicLearningFocus.topic)
                .limit(limit)
            )
        )

    @staticmethod
    def snooze_learning_focus(
        session: Session,
        *,
        focus_id: uuid.UUID,
        now: datetime,
        actor: str = "academic_planner",
        reason: str | None = None,
        owner_user_id: str | None = None,
        owner_channel_id: str | None = None,
        expected_revision: int | None = None,
    ) -> AcademicLearningFocus:
        filters = [AcademicLearningFocus.id == focus_id]
        if owner_user_id is not None:
            filters.append(AcademicLearningFocus.owner_user_id == _bounded(owner_user_id, 24))
        if owner_channel_id is not None:
            filters.append(AcademicLearningFocus.owner_channel_id == _bounded(owner_channel_id, 24))
        focus = session.scalar(select(AcademicLearningFocus).where(*filters).with_for_update())
        if focus is None:
            raise NoResultFound(f"academic learning focus {focus_id} was not found")
        if expected_revision is not None and focus.revision != expected_revision:
            raise NoResultFound("academic learning focus revision changed")
        current = _utc(now, "now")
        focus.status = "snoozed"
        focus.revision += 1
        focus.snoozed_at = current
        focus.practice_due_on = None
        focus.practice_minutes = None
        _append_learning_focus_event(
            session,
            focus_id=focus.id,
            session_id=focus.source_session_id,
            external_event_id=None,
            event_type="snoozed",
            actor=actor,
            occurred_at=current,
            payload={"reason": reason},
        )
        session.flush()
        return focus

    @staticmethod
    def mark_learning_focus_review_prompted(
        session: Session,
        *,
        focus_id: uuid.UUID,
        now: datetime,
        next_review_at: datetime,
        external_event_id: str | None = None,
    ) -> AcademicLearningFocus:
        focus = session.get(AcademicLearningFocus, focus_id)
        if focus is None:
            raise NoResultFound(f"academic learning focus {focus_id} was not found")
        current = _utc(now, "now")
        focus.last_review_prompted_at = current
        focus.revision += 1
        focus.next_review_at = _utc(next_review_at, "next_review_at")
        _append_learning_focus_event(
            session,
            focus_id=focus.id,
            session_id=focus.source_session_id,
            external_event_id=_bounded_optional(external_event_id),
            event_type="review_requested",
            actor="academic_planner",
            occurred_at=current,
        )
        session.flush()
        return focus

    @staticmethod
    def hard_delete_learning_focus(session: Session, *, focus_id: uuid.UUID) -> bool:
        focus = session.get(AcademicLearningFocus, focus_id)
        if focus is None:
            return False
        session.execute(
            delete(AcademicReflectionMemory).where(AcademicReflectionMemory.focus_id == focus_id)
        )
        session.execute(
            delete(AcademicLearningFocusEvent).where(
                AcademicLearningFocusEvent.focus_id == focus_id
            )
        )
        session.execute(
            update(StudyBlock)
            .where(StudyBlock.learning_focus_id == focus_id)
            .values(learning_focus_id=None, block_kind="practice")
        )
        session.delete(focus)
        session.flush()
        return True

    @staticmethod
    def hard_delete_owned_learning_focus(
        session: Session,
        *,
        focus_id: uuid.UUID,
        owner_user_id: str,
        owner_channel_id: str,
        expected_revision: int,
    ) -> LearningFocusMutationStatus:
        """Lock, owner-check, and hard-delete one focus at the expected revision."""

        focus = session.scalar(
            select(AcademicLearningFocus)
            .where(
                AcademicLearningFocus.id == focus_id,
                AcademicLearningFocus.owner_user_id == _bounded(owner_user_id, 24),
                AcademicLearningFocus.owner_channel_id == _bounded(owner_channel_id, 24),
            )
            .with_for_update()
        )
        if focus is None:
            return "not_found"
        if focus.revision != expected_revision:
            return "stale"
        AcademicRepository.hard_delete_learning_focus(session, focus_id=focus.id)
        return "applied"

    @staticmethod
    def replace_owned_learning_focus(
        session: Session,
        *,
        focus_id: uuid.UUID,
        owner_user_id: str,
        owner_channel_id: str,
        expected_revision: int,
        topic: str,
        raw_text: str,
        memory: LearningFocusMemoryInput,
        now: datetime,
        source_session_id: uuid.UUID,
        external_event_id: str,
        next_review_at: datetime,
        practice_due_on: date,
        practice_minutes: int,
        course_id: uuid.UUID | None,
        assessment_id: uuid.UUID | None,
        course_code: str | None,
        actor: str,
    ) -> tuple[LearningFocusMutationStatus, AcademicLearningFocus | None]:
        """Replace canonical focus text and all semantic reflection memory atomically."""

        focus = session.scalar(
            select(AcademicLearningFocus)
            .where(
                AcademicLearningFocus.id == focus_id,
                AcademicLearningFocus.owner_user_id == _bounded(owner_user_id, 24),
                AcademicLearningFocus.owner_channel_id == _bounded(owner_channel_id, 24),
            )
            .with_for_update()
        )
        if focus is None:
            return "not_found", None
        if focus.revision != expected_revision:
            return "stale", focus
        if raw_text != memory.raw_text:
            raise ValueError("replacement raw text must match the persisted memory text")
        if practice_minutes <= 0:
            raise ValueError("practice_minutes must be positive")
        current = _utc(now, "now")
        session.execute(
            delete(AcademicReflectionMemory).where(AcademicReflectionMemory.focus_id == focus.id)
        )
        session.execute(
            delete(AcademicLearningFocusEvent).where(
                AcademicLearningFocusEvent.focus_id == focus.id
            )
        )
        focus.topic = _bounded(topic)
        focus.course_id = course_id
        focus.assessment_id = assessment_id
        focus.course_code = _bounded_optional(course_code, 64)
        focus.status = "active"
        focus.revision += 1
        focus.reinforcement_count = 1
        focus.next_review_at = _utc(next_review_at, "next_review_at")
        focus.practice_due_on = practice_due_on
        focus.practice_minutes = practice_minutes
        focus.last_reviewed_at = current
        focus.last_review_prompted_at = None
        focus.last_reinforced_at = current
        focus.missed_review_count = 0
        focus.reminder_count = 0
        focus.last_reminded_at = None
        focus.snoozed_at = None
        focus.source_session_id = source_session_id
        _append_learning_focus_event(
            session,
            focus_id=focus.id,
            session_id=source_session_id,
            external_event_id=_bounded(external_event_id),
            event_type="rewritten",
            actor=actor,
            occurred_at=current,
            payload={"rewrite": True, "revision": focus.revision},
        )
        _persist_reflection_memory(
            session,
            focus_id=focus.id,
            session_id=source_session_id,
            external_event_id=_bounded(external_event_id),
            memory=memory,
            recorded_at=current,
        )
        session.flush()
        return "applied", focus

    @staticmethod
    def advance_learning_focus_reminder(
        session: Session,
        *,
        focus_id: uuid.UUID,
        now: datetime,
        next_reminder_at: datetime | None = None,
        external_event_id: str | None = None,
        actor: str = "academic_planner",
        snooze_after_missed: int = 2,
        delete_after_reminders: int = 5,
    ) -> tuple[LearningFocusReminderStatus, AcademicLearningFocus | None]:
        if snooze_after_missed < 1 or delete_after_reminders < snooze_after_missed:
            raise ValueError("invalid missed-review lifecycle thresholds")
        event_id = _bounded_optional(external_event_id)
        if event_id is not None:
            existing_event = session.scalar(
                select(AcademicLearningFocusEvent).where(
                    AcademicLearningFocusEvent.external_event_id == event_id
                )
            )
            if existing_event is not None:
                focus = session.get(AcademicLearningFocus, existing_event.focus_id)
                if focus is None:
                    return "delete", None
                status: LearningFocusReminderStatus = (
                    "snoozed_and_remind" if focus.status == "snoozed" else "remind"
                )
                return status, focus
        focus = session.scalar(
            select(AcademicLearningFocus)
            .where(AcademicLearningFocus.id == focus_id)
            .with_for_update()
        )
        if focus is None:
            raise NoResultFound(f"academic learning focus {focus_id} was not found")
        if focus.reminder_count >= delete_after_reminders:
            AcademicRepository.hard_delete_learning_focus(session, focus_id=focus_id)
            return "delete", None
        current = _utc(now, "now")
        focus.missed_review_count += 1
        focus.reminder_count += 1
        focus.revision += 1
        focus.last_reminded_at = current
        if focus.source_session_id is not None:
            discourse_session = session.get(AcademicDiscourseSession, focus.source_session_id)
            if discourse_session is not None:
                discourse_session.missed_review_count += 1
                discourse_session.reminder_count += 1
                discourse_session.last_turn_at = current
        if next_reminder_at is not None:
            focus.next_review_at = _utc(next_reminder_at, "next_reminder_at")
        event_type = "reminder_sent"
        status = "remind"
        if focus.missed_review_count >= snooze_after_missed:
            focus.status = "snoozed"
            focus.snoozed_at = focus.snoozed_at or current
            focus.practice_due_on = None
            focus.practice_minutes = None
            event_type = "snoozed"
            status = "snoozed_and_remind"
        _append_learning_focus_event(
            session,
            focus_id=focus.id,
            session_id=focus.source_session_id,
            external_event_id=event_id,
            event_type=event_type,
            actor=actor,
            occurred_at=current,
            payload={
                "missed_review_count": focus.missed_review_count,
                "reminder_count": focus.reminder_count,
                "delete_after_reminders": delete_after_reminders,
            },
        )
        session.flush()
        return cast(LearningFocusReminderStatus, status), focus

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

    @staticmethod
    def reject_proposed_change(
        session: Session,
        *,
        proposal_id: uuid.UUID,
        actor: str = "academic_planner",
        now: datetime | None = None,
    ) -> tuple[ProposalRejectionStatus, AcademicProposedChange]:
        """Atomically reject a pending proposal without any external write.

        This uses the same row lock as confirmation claiming so a concurrent
        confirm/reject pair can only produce one winning terminal path.
        """

        proposal = session.scalar(
            select(AcademicProposedChange)
            .where(AcademicProposedChange.id == proposal_id)
            .with_for_update()
        )
        if proposal is None:
            raise NoResultFound(f"academic proposal {proposal_id} was not found")
        current = now or datetime.now(UTC)
        if proposal.state == "rejected":
            return "already_rejected", proposal
        if proposal.state == "applied":
            return "already_applied", proposal
        if proposal.state == "applying":
            return "in_progress", proposal
        if proposal.expires_at is not None and _aware_db(proposal.expires_at) <= current:
            proposal.state = "expired"
            session.flush()
            return "expired", proposal
        if proposal.state != "pending":
            return "not_pending", proposal
        proposal.state = "rejected"
        checkin = session.get(AcademicCheckIn, proposal.checkin_id)
        if checkin is not None:
            checkin.status = "completed"
        AuditRepository.append(
            session,
            actor=_bounded(actor, 128),
            action="academic_proposal.rejected",
            target_type="academic_proposal",
            target_id=proposal.target_id,
            result="rejected",
        )
        session.flush()
        return "rejected", proposal


def _append_learning_focus_event(
    session: Session,
    *,
    focus_id: uuid.UUID,
    session_id: uuid.UUID | None,
    external_event_id: str | None,
    event_type: str,
    actor: str,
    occurred_at: datetime,
    payload: Mapping[str, Any] | None = None,
) -> AcademicLearningFocusEvent:
    values = {
        "focus_id": focus_id,
        "session_id": session_id,
        "external_event_id": _bounded_optional(external_event_id),
        "event_type": _bounded(event_type, 64),
        "actor": _bounded_optional(actor),
        "occurred_at": _utc(occurred_at, "occurred_at"),
        "payload": dict(payload or {}),
    }
    row = AcademicLearningFocusEvent(**values)
    session.add(row)
    session.flush()
    return row


def _persist_reflection_memory(
    session: Session,
    *,
    focus_id: uuid.UUID,
    session_id: uuid.UUID | None,
    external_event_id: str | None,
    memory: LearningFocusMemoryInput,
    recorded_at: datetime,
) -> AcademicReflectionMemory:
    raw_text = memory.raw_text
    if not raw_text.strip() or len(raw_text) > CHUNK_MAX_CHARS:
        raise ValueError("reflection text must be non-empty and bounded")
    embedding = _embedding_vector(memory.embedding)
    model = _bounded_optional(memory.embedding_model)
    if embedding is not None and model is None:
        raise ValueError("embedding_model is required when an embedding is stored")
    existing = None
    event_id = _bounded_optional(external_event_id)
    if event_id is not None:
        existing = session.scalar(
            select(AcademicReflectionMemory).where(
                AcademicReflectionMemory.external_event_id == event_id
            )
        )
    if existing is not None:
        return existing
    row = AcademicReflectionMemory(
        focus_id=focus_id,
        session_id=session_id,
        external_event_id=event_id,
        raw_text=raw_text,
        redacted_summary=_bounded_optional(memory.redacted_summary, 2_000),
        embedding=embedding,
        embedding_model=model,
        embedding_dimensions=len(embedding) if embedding is not None else None,
        embedding_metadata=dict(memory.embedding_metadata or {}),
        recorded_at=_utc(recorded_at, "recorded_at"),
    )
    session.add(row)
    session.flush()
    return row


def _embedding_vector(value: Sequence[float] | None) -> list[float] | None:
    if value is None:
        return None
    result: list[float] = []
    for item in value:
        if isinstance(item, bool):
            raise ValueError("embedding values must be numeric")
        numeric = float(item)
        if not math.isfinite(numeric):
            raise ValueError("embedding values must be finite")
        result.append(numeric)
    if not result:
        raise ValueError("embedding must not be empty")
    return result


def _document_source_kind(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if normalized not in _ACADEMIC_DOCUMENT_SOURCE_KINDS:
        raise ValueError("academic document source kind is invalid")
    return normalized


def _document_extraction_status(value: str) -> str:
    normalized = value.strip()
    if normalized not in _ACADEMIC_DOCUMENT_STATUSES:
        raise ValueError("academic document extraction status is invalid")
    return normalized


def _durable_source_url(value: str | None) -> str | None:
    """Drop temporary signed Notion attachment URLs before relational persistence."""

    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    lowered = normalized.lower()
    if any(marker in lowered for marker in _SIGNED_NOTION_URL_MARKERS):
        return None
    return _bounded(normalized, 1_000)


def _checkin_proposal_from_row(
    proposal_type: Any,
    proposal_id: uuid.UUID,
    row: AcademicProposedChange,
    changes: Sequence[Any],
) -> Any:
    values: dict[str, Any] = {
        "proposal_id": proposal_id,
        "confirmation_event": row.confirmation_token,
        "changes": tuple(changes),
    }
    if "expires_at" in getattr(proposal_type, "model_fields", {}):
        values["expires_at"] = _aware_db(row.expires_at) if row.expires_at is not None else None
    return proposal_type(**values)


def _resolve_study_plan_id(
    session: Session,
    source_plan_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """Resolve a planner-facing plan key to the internal foreign-key ID."""

    if source_plan_id is None:
        return None
    plan = session.get(StudyPlan, source_plan_id)
    if plan is None:
        plan = session.scalar(select(StudyPlan).where(StudyPlan.plan_key == str(source_plan_id)))
    return plan.id if plan is not None else None


def _lock_proposal_operation(
    session: Session,
    proposal_id: uuid.UUID,
    ordinal: int,
    payload_hash: str,
) -> AcademicProposalOperationJournal:
    if ordinal < 0:
        raise ValueError("proposal operation ordinal must be nonnegative")
    if not _SHA256.fullmatch(payload_hash):
        raise ValueError("proposal operation payload hash must be SHA-256 hex")
    row = session.scalar(
        select(AcademicProposalOperationJournal)
        .where(
            AcademicProposalOperationJournal.proposal_id == proposal_id,
            AcademicProposalOperationJournal.ordinal == ordinal,
        )
        .with_for_update()
    )
    if row is None:
        raise NoResultFound(f"academic proposal operation {proposal_id}:{ordinal} was not found")
    if row.payload_hash != payload_hash:
        raise ValueError("proposal operation payload hash changed")
    return row


def _bounded_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "proposal_id": 255,
        "page_id": 255,
        "url": 1_000,
        "property_id": 255,
    }
    result: dict[str, Any] = {}
    for key, limit in allowed.items():
        value = receipt.get(key)
        if isinstance(value, str) and value.strip():
            result[key] = value.strip()[:limit]
    return result


def _proposal_operation_snapshot(row: AcademicProposalOperationJournal) -> dict[str, Any]:
    return {
        "proposal_id": str(row.proposal_id),
        "ordinal": row.ordinal,
        "operation_id": row.operation_id,
        "payload_hash": row.payload_hash,
        "state": row.state,
        "receipt": dict(row.receipt or {}),
        "error_code": row.error_code,
    }


def _learning_focus_option(session: Session, focus: AcademicLearningFocus) -> Any:
    from app.agents.academic_planner.contracts import (
        AcademicLearningFocusOption,
        LearningFocusStatus,
    )

    assessment = (
        session.get(Assessment, focus.assessment_id) if focus.assessment_id is not None else None
    )
    latest_memory = session.scalar(
        select(AcademicReflectionMemory)
        .where(AcademicReflectionMemory.focus_id == focus.id)
        .order_by(AcademicReflectionMemory.recorded_at.desc())
        .limit(1)
    )
    return AcademicLearningFocusOption(
        focus_id=str(focus.id),
        status=LearningFocusStatus(focus.status),
        topic=focus.topic,
        course_id=str(focus.course_id) if focus.course_id is not None else None,
        course_code=focus.course_code,
        assessment_id=str(focus.assessment_id) if focus.assessment_id is not None else None,
        assessment_title=assessment.title if assessment is not None else None,
        target_minutes=focus.practice_minutes or 30,
        next_review_at=(
            _aware_db(focus.next_review_at) if focus.next_review_at is not None else None
        ),
        missed_checkin_count=focus.missed_review_count,
        snoozed_until=None,
        revision=focus.revision,
        current_reflection_summary=(
            latest_memory.redacted_summary[:500]
            if latest_memory is not None and latest_memory.redacted_summary
            else None
        ),
    )


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


class SQLAlchemyAcademicPlannerStore:
    """Adapter implementing the academic planner's persistence protocol.

    The planner workflow stores Pydantic plans, while this adapter stores only
    their typed fields. It is intentionally synchronous so the workflow can
    choose its own worker-thread boundary around database calls.
    """

    def __init__(
        self,
        engine: Any,
        *,
        confirmation_ttl_hours: int = 24,
        embedding_gateway: Any | None = None,
        default_practice_minutes: int = 30,
    ) -> None:
        if confirmation_ttl_hours < 1 or confirmation_ttl_hours > 168:
            raise ValueError("confirmation_ttl_hours must be between 1 and 168")
        self.engine = engine
        self.confirmation_ttl_hours = confirmation_ttl_hours
        self.embedding_gateway = embedding_gateway
        self.default_practice_minutes = default_practice_minutes

    def search_courses(self, query: str) -> Sequence[Any]:
        """Return a bounded writable course inventory for semantic model selection."""

        from app.agents.academic_planner.contracts import AcademicCourseOption

        del query
        with Session(self.engine) as session:
            rows = session.execute(
                select(Course, AcademicCourseCalendar)
                .join(AcademicCourseCalendar, AcademicCourseCalendar.course_id == Course.id)
                .where(
                    Course.active.is_(True),
                    AcademicCourseCalendar.discovery_status == "valid",
                    AcademicCourseCalendar.child_data_source_id.is_not(None),
                    AcademicCourseCalendar.title_property_id.is_not(None),
                    AcademicCourseCalendar.date_property_id.is_not(None),
                )
                .order_by(Course.course_code, Course.term, Course.id)
                .limit(20)
            )
            options = [
                AcademicCourseOption(
                    course_id=str(course.id),
                    course_code=course.course_code,
                    title=course.title,
                )
                for course, _calendar in rows
            ]
        return tuple(options)

    def search_assessments(
        self,
        query: str,
        course_id: str | None = None,
    ) -> Sequence[Any]:
        """Return a bounded active assessment inventory for semantic model selection."""

        from app.agents.academic_planner.contracts import (
            AcademicAssessmentOption,
            AssessmentType,
        )

        del query
        selected_course_id = _parse_uuid(course_id) if course_id is not None else None
        if course_id is not None and selected_course_id is None:
            return ()
        with Session(self.engine) as session:
            statement = (
                select(Assessment, Course)
                .join(Course, Course.id == Assessment.course_id)
                .join(AcademicCourseCalendar, AcademicCourseCalendar.course_id == Course.id)
                .where(
                    Course.active.is_(True),
                    Assessment.active.is_(True),
                    Assessment.archived.is_(False),
                    Assessment.notion_last_edited_at.is_not(None),
                    Assessment.title_property_id.is_not(None),
                    AcademicCourseCalendar.discovery_status == "valid",
                    AcademicCourseCalendar.date_property_id.is_not(None),
                )
                .order_by(Assessment.due_at, Assessment.title, Assessment.id)
                .limit(20)
            )
            if selected_course_id is not None:
                statement = statement.where(Course.id == selected_course_id)
            rows = session.execute(statement)
            options = [
                AcademicAssessmentOption(
                    assessment_id=str(assessment.id),
                    course_id=str(course.id),
                    course_code=course.course_code,
                    title=assessment.title,
                    due_at=(
                        _aware_db(assessment.due_at) if assessment.due_at is not None else None
                    ),
                    assessment_type=_planner_assessment_type(
                        AssessmentType,
                        assessment.assessment_type,
                    ),
                    expected_last_edited_at=_aware_db(assessment.notion_last_edited_at),
                )
                for assessment, course in rows
            ]
        return tuple(options)

    def search_learning_focuses(
        self,
        query: str | None,
        statuses: Sequence[Any],
        *,
        owner_user_id: str | None = None,
        owner_channel_id: str | None = None,
    ) -> Sequence[Any]:
        """Return bounded active/snoozed focus options for model-selected reads."""

        from app.agents.academic_planner.contracts import LearningFocusStatus

        allowed = {
            item.value if isinstance(item, LearningFocusStatus) else str(item) for item in statuses
        }
        allowed &= {"active", "snoozed"}
        if not allowed:
            return ()
        needle = _academic_search_text(query or "")
        with Session(self.engine) as session:
            statement = (
                select(AcademicLearningFocus)
                .where(AcademicLearningFocus.status.in_(sorted(allowed)))
                .order_by(
                    AcademicLearningFocus.next_review_at,
                    AcademicLearningFocus.topic,
                )
                .limit(100)
            )
            if owner_user_id is not None:
                statement = statement.where(
                    AcademicLearningFocus.owner_user_id == _bounded(owner_user_id, 24)
                )
            if owner_channel_id is not None:
                statement = statement.where(
                    AcademicLearningFocus.owner_channel_id == _bounded(owner_channel_id, 24)
                )
            rows = list(session.scalars(statement))
            return tuple(
                _learning_focus_option(session, row)
                for row in rows
                if _academic_option_matches(
                    needle,
                    str(row.id),
                    row.topic,
                    row.course_code or "",
                )
            )[:20]

    def list_memory_focuses_for_owner(
        self,
        *,
        owner_user_id: str,
        owner_channel_id: str,
        limit: int = 20,
    ) -> tuple[tuple[Any, ...], bool]:
        """Return bounded active/snoozed facts and a host-computed truncation flag."""

        if limit < 1 or limit > 20:
            raise ValueError("memory focus limit must be between 1 and 20")
        with Session(self.engine) as session:
            rows = AcademicRepository.list_active_learning_focuses(
                session,
                include_snoozed=True,
                limit=limit + 1,
                owner_user_id=owner_user_id,
                owner_channel_id=owner_channel_id,
            )
            return tuple(_learning_focus_option(session, row) for row in rows[:limit]), (
                len(rows) > limit
            )

    async def search_semantic_focuses(
        self,
        query: str,
        *,
        limit: int,
        owner_user_id: str | None = None,
        owner_channel_id: str | None = None,
    ) -> Sequence[Any]:
        """Embed a query and run owner-local exact cosine search in pgvector."""

        from app.agents.academic_planner.contracts import AcademicSemanticCandidate
        from app.llm.embeddings import EmbeddingStatus

        if self.embedding_gateway is None or limit < 1:
            return ()
        result = await self.embedding_gateway.embed_reflection_text(query)
        if result.status is not EmbeddingStatus.VALID or result.embedding is None:
            return ()
        vector = result.embedding.vector
        with Session(self.engine) as session:
            if session.get_bind().dialect.name == "postgresql":
                distance = AcademicReflectionMemory.embedding.cosine_distance(vector).label(
                    "distance"
                )
                statement = (
                    select(AcademicReflectionMemory, AcademicLearningFocus, distance)
                    .join(
                        AcademicLearningFocus,
                        AcademicLearningFocus.id == AcademicReflectionMemory.focus_id,
                    )
                    .where(
                        AcademicReflectionMemory.embedding.is_not(None),
                        AcademicReflectionMemory.embedding_dimensions == len(vector),
                        AcademicLearningFocus.status.in_(["active", "snoozed"]),
                    )
                    .order_by(distance)
                    .limit(limit)
                )
                if owner_user_id is not None:
                    statement = statement.where(
                        AcademicLearningFocus.owner_user_id == _bounded(owner_user_id, 24)
                    )
                if owner_channel_id is not None:
                    statement = statement.where(
                        AcademicLearningFocus.owner_channel_id == _bounded(owner_channel_id, 24)
                    )
                rows = session.execute(statement)
                candidates = [
                    AcademicSemanticCandidate(
                        candidate_id=str(memory.id),
                        source_kind="reflection",
                        source_id=str(memory.id),
                        text=memory.raw_text[:1_000],
                        score=max(0.0, min(1.0, 1.0 - float(distance_value))),
                        focus=_learning_focus_option(session, focus),
                    )
                    for memory, focus, distance_value in rows
                ]
                return tuple(candidates)

            statement = (
                select(AcademicReflectionMemory, AcademicLearningFocus)
                .join(
                    AcademicLearningFocus,
                    AcademicLearningFocus.id == AcademicReflectionMemory.focus_id,
                )
                .where(
                    AcademicReflectionMemory.embedding.is_not(None),
                    AcademicReflectionMemory.embedding_dimensions == len(vector),
                    AcademicLearningFocus.status.in_(["active", "snoozed"]),
                )
            )
            if owner_user_id is not None:
                statement = statement.where(
                    AcademicLearningFocus.owner_user_id == _bounded(owner_user_id, 24)
                )
            if owner_channel_id is not None:
                statement = statement.where(
                    AcademicLearningFocus.owner_channel_id == _bounded(owner_channel_id, 24)
                )
            rows = session.execute(statement)
            ranked = sorted(
                (
                    (
                        _cosine_similarity(vector, cast(Sequence[float], memory.embedding)),
                        memory,
                        focus,
                    )
                    for memory, focus in rows
                ),
                key=lambda item: item[0],
                reverse=True,
            )[:limit]
            return tuple(
                AcademicSemanticCandidate(
                    candidate_id=str(memory.id),
                    source_kind="reflection",
                    source_id=str(memory.id),
                    text=memory.raw_text[:1_000],
                    score=max(0.0, min(1.0, score)),
                    focus=_learning_focus_option(session, focus),
                )
                for score, memory, focus in ranked
            )

    def prepare_learning_focus_checkin(
        self,
        *,
        now: datetime,
        next_review_at: datetime,
        idempotency_key: str,
        snooze_after_missed: int,
        delete_after_reminders: int,
    ) -> tuple[dict[str, Any], ...]:
        """Advance due focus reviews and return bounded Discord prompt facts."""

        results: list[dict[str, Any]] = []
        with Session(self.engine) as session, session.begin():
            due = AcademicRepository.list_due_learning_focus_reviews(session, now=now)
            for focus in due:
                event_key = f"{idempotency_key}:focus:{focus.id}"
                if focus.last_review_prompted_at is None:
                    AcademicRepository.mark_learning_focus_review_prompted(
                        session,
                        focus_id=focus.id,
                        now=now,
                        next_review_at=next_review_at,
                        external_event_id=f"{event_key}:initial",
                    )
                    results.append(
                        {
                            "focus_id": str(focus.id),
                            "course_code": focus.course_code,
                            "topic": focus.topic,
                            "kind": "review",
                            "reminder_count": 0,
                            "delete_after_reminders": delete_after_reminders,
                        }
                    )
                    continue
                status, updated = AcademicRepository.advance_learning_focus_reminder(
                    session,
                    focus_id=focus.id,
                    now=now,
                    next_reminder_at=next_review_at,
                    external_event_id=f"{event_key}:reminder",
                    snooze_after_missed=snooze_after_missed,
                    delete_after_reminders=delete_after_reminders,
                )
                if status == "delete" or updated is None:
                    results.append(
                        {
                            "focus_id": str(focus.id),
                            "course_code": focus.course_code,
                            "topic": focus.topic,
                            "kind": "deleted",
                            "reminder_count": delete_after_reminders,
                            "delete_after_reminders": delete_after_reminders,
                        }
                    )
                    continue
                results.append(
                    {
                        "focus_id": str(updated.id),
                        "course_code": updated.course_code,
                        "topic": updated.topic,
                        "kind": status,
                        "reminder_count": updated.reminder_count,
                        "delete_after_reminders": delete_after_reminders,
                    }
                )
        return tuple(results)

    def resolve_course_mutation_target(self, course_id: str) -> AcademicCourseMutationTarget | None:
        """Resolve only a valid discovered course calendar to a write target."""

        parsed = _parse_uuid(course_id)
        if parsed is None:
            return None
        with Session(self.engine) as session:
            row = session.execute(
                select(Course, AcademicCourseCalendar)
                .join(AcademicCourseCalendar, AcademicCourseCalendar.course_id == Course.id)
                .where(
                    Course.id == parsed,
                    Course.active.is_(True),
                    AcademicCourseCalendar.discovery_status == "valid",
                )
            ).one_or_none()
            if row is None:
                return None
            course, calendar = row
            if not all(
                (
                    calendar.child_data_source_id,
                    calendar.title_property_id,
                    calendar.date_property_id,
                )
            ):
                return None
            return AcademicCourseMutationTarget(
                course_id=str(course.id),
                course_code=course.course_code,
                data_source_id=cast(str, calendar.child_data_source_id),
                title_property_id=cast(str, calendar.title_property_id),
                date_property_id=cast(str, calendar.date_property_id),
            )

    def resolve_assessment_mutation_target(
        self, assessment_id: str
    ) -> AcademicAssessmentMutationTarget | None:
        """Resolve an active synchronized assessment with guarded-write metadata."""

        parsed = _parse_uuid(assessment_id)
        if parsed is None:
            return None
        with Session(self.engine) as session:
            row = session.execute(
                select(Assessment, AcademicCourseCalendar)
                .join(
                    AcademicCourseCalendar,
                    AcademicCourseCalendar.course_id == Assessment.course_id,
                )
                .where(
                    Assessment.id == parsed,
                    Assessment.active.is_(True),
                    Assessment.archived.is_(False),
                    AcademicCourseCalendar.discovery_status == "valid",
                )
            ).one_or_none()
            if row is None:
                return None
            assessment, calendar = row
            if not all(
                (
                    assessment.notion_id,
                    assessment.notion_last_edited_at,
                    assessment.title_property_id,
                    calendar.date_property_id,
                )
            ):
                return None
            return AcademicAssessmentMutationTarget(
                assessment_id=str(assessment.id),
                course_id=str(assessment.course_id),
                page_id=assessment.notion_id,
                title=assessment.title,
                last_edited_at=_aware_db(cast(datetime, assessment.notion_last_edited_at)),
                title_property_id=cast(str, assessment.title_property_id),
                date_property_id=cast(str, calendar.date_property_id),
            )

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
                source_page_id=source_page_id,
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
        assessment_id = params.get("assessment_id")
        with Session(self.engine) as session:
            rows = AcademicRepository.search_document_chunks(
                session,
                query=str(params.get("question", "")),
                course_id=uuid.UUID(str(course_id)) if course_id else None,
                term=params.get("term"),
                document_type=params.get("document_type"),
                assessment_id=uuid.UUID(str(assessment_id)) if assessment_id else None,
                access_classification=str(params.get("access_classification", "private")),
                active_only=bool(params.get("active_only", False)),
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
                        "chunk_id": str(row.id),
                        "source_page": row.source_page,
                        "source_block": row.source_block,
                        "heading": row.heading,
                        "document_version": document.document_version,
                        "assessment_id": str(document.assessment_id)
                        if document.assessment_id is not None
                        else None,
                        "source_key": document.source_key,
                        "source_kind": document.source_kind,
                        "access_classification": document.access_classification,
                    }
                )
            return results

    def list_assessment_materials(
        self,
        assessment_id: uuid.UUID | str,
        *,
        active_only: bool = True,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return safe material metadata for one assessment without raw text."""

        with Session(self.engine) as session:
            scope = _resolve_assessment_scope(session, assessment_id)
            if scope is None:
                return []
            internal_assessment_id, public_assessment_id = scope
            rows = AcademicRepository.list_assessment_materials(
                session,
                assessment_id=internal_assessment_id,
                active_only=active_only,
                limit=limit,
            )
            return [
                {
                    "document_id": str(row.id),
                    "assessment_id": public_assessment_id,
                    "source_kind": row.source_kind,
                    "source_page_id": row.source_page_id,
                    "source_block_id": row.source_block_id,
                    "source_property_id": row.source_property_id,
                    "source_key": row.source_key,
                    "document_version": row.document_version,
                    "title": row.title,
                    "document_type": row.document_type,
                    "media_type": row.media_type,
                    "retrieved_at": _aware_db(row.retrieved_at).isoformat(),
                    "source_last_edited_at": (
                        _aware_db(row.source_last_edited_at).isoformat()
                        if row.source_last_edited_at is not None
                        else None
                    ),
                    "active": row.active,
                    "extraction_status": row.extraction_status,
                    "extraction_error_code": row.extraction_error_code,
                    "chunk_count": int(
                        session.scalar(
                            select(func.count())
                            .select_from(AcademicDocumentChunk)
                            .where(AcademicDocumentChunk.document_id == row.id)
                        )
                        or 0
                    ),
                }
                for row in rows
            ]

    def read_assessment_material_chunks(
        self,
        assessment_id: uuid.UUID | str,
        chunk_ids: Sequence[uuid.UUID | str],
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Read owned material chunks for model tools after host-side ID checks."""

        parsed_ids = [uuid.UUID(str(chunk_id)) for chunk_id in chunk_ids]
        with Session(self.engine) as session:
            scope = _resolve_assessment_scope(session, assessment_id)
            if scope is None:
                return []
            internal_assessment_id, public_assessment_id = scope
            rows = AcademicRepository.read_assessment_material_chunks(
                session,
                assessment_id=internal_assessment_id,
                chunk_ids=parsed_ids,
                limit=limit,
            )
            results: list[dict[str, Any]] = []
            for row in rows:
                document = session.get(AcademicDocument, row.document_id)
                if document is None:
                    continue
                results.append(
                    {
                        "chunk_id": str(row.id),
                        "document_id": str(row.document_id),
                        "assessment_id": public_assessment_id,
                        "ordinal": row.ordinal,
                        "content": row.content,
                        "source_page": row.source_page,
                        "source_block": row.source_block,
                        "source_url": _durable_source_url(row.source_url),
                        "heading": row.heading,
                        "document_version": document.document_version,
                        "source_key": document.source_key,
                    }
                )
            return results

    async def search_semantic_assessment_materials(
        self,
        assessment_id: uuid.UUID | str,
        query: str,
        *,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Embed a query and search only active chunks owned by one assessment."""

        from app.llm.embeddings import EmbeddingStatus

        if self.embedding_gateway is None or limit < 1:
            return []
        result = await self.embedding_gateway.embed_academic_text(query)
        if result.status is not EmbeddingStatus.VALID or result.embedding is None:
            return []
        with Session(self.engine) as session:
            scope = _resolve_assessment_scope(session, assessment_id)
            if scope is None:
                return []
            internal_assessment_id, public_assessment_id = scope
            rows = AcademicRepository.search_semantic_document_chunks(
                session,
                assessment_id=internal_assessment_id,
                query_embedding=result.embedding.vector,
                embedding_model=result.model_identity,
                limit=limit,
            )
            output: list[dict[str, Any]] = []
            for row, score in rows:
                document = session.get(AcademicDocument, row.document_id)
                if document is None:
                    continue
                output.append(
                    {
                        "chunk_id": str(row.id),
                        "document_id": str(row.document_id),
                        "assessment_id": public_assessment_id,
                        "ordinal": row.ordinal,
                        "content": row.content,
                        "source_page": row.source_page,
                        "source_block": row.source_block,
                        "heading": row.heading,
                        "document_version": document.document_version,
                        "source_key": document.source_key,
                        "score": score,
                    }
                )
            return output

    async def semantic_search_assessment_materials(
        self,
        assessment_id: uuid.UUID | str,
        query: str,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Material-agent alias with a positional bounded limit."""

        return await self.search_semantic_assessment_materials(
            assessment_id,
            query,
            limit=limit,
        )

    def load_planner_facts(self, *, now: datetime, horizon_days: int) -> Any:
        from app.agents.academic_planner.contracts import (
            AmbiguousFact,
            AssessmentType,
            IncompleteBlock,
            PlannerFacts,
            PracticeNeed,
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
            local_day = current.astimezone(_TORONTO).date()
            practice_needs: list[Any] = []
            focus_rows = session.scalars(
                select(AcademicLearningFocus)
                .where(
                    AcademicLearningFocus.status == "active",
                    AcademicLearningFocus.practice_due_on.is_not(None),
                    AcademicLearningFocus.practice_due_on <= local_day,
                )
                .order_by(
                    AcademicLearningFocus.next_review_at,
                    AcademicLearningFocus.topic,
                )
            )
            for row in focus_rows:
                linked_assessment = (
                    session.get(Assessment, row.assessment_id)
                    if row.assessment_id is not None
                    else None
                )
                practice_needs.append(
                    PracticeNeed(
                        focus_id=str(row.id),
                        course_id=str(row.course_id) if row.course_id is not None else None,
                        course_code=row.course_code,
                        assessment_id=(
                            str(row.assessment_id) if row.assessment_id is not None else None
                        ),
                        assessment_title=(linked_assessment.title if linked_assessment else None),
                        topic=row.topic,
                        target_minutes=row.practice_minutes or self.default_practice_minutes,
                        next_review_at=(
                            _aware_db(row.next_review_at)
                            if row.next_review_at is not None
                            else current + timedelta(days=1)
                        ),
                        source_action="reinforce_focus",
                        rationale=(
                            "Scheduled as a separate practice block because this academic topic "
                            "is an active learning focus from the latest reflection."
                        ),
                    )
                )
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
            practice_needs=tuple(practice_needs),
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
                    learning_focus_id=_parse_uuid(_field(block, "learning_focus_id")),
                    block_kind=_field(block, "block_kind") or "assessment",
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
                        learning_focus_id=(
                            str(row.learning_focus_id)
                            if row.learning_focus_id is not None
                            else None
                        ),
                        block_kind=cast(
                            Literal["assessment", "practice"],
                            row.block_kind,
                        ),
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
            now = datetime.now(UTC)
            plan_id = _resolve_study_plan_id(session, proposal.source_plan_id)
            checkin = AcademicRepository.create_checkin(
                session,
                idempotency_key=f"academic-checkin:{proposal.proposal_id}",
                external_event_id=f"proposal:{proposal.proposal_id}",
                channel="discord",
                received_at=now,
                redacted_summary="Academic check-in proposal pending confirmation.",
                status="proposal_pending",
                plan_id=plan_id,
            )
            AcademicRepository.create_proposed_change(
                session,
                checkin_id=checkin.id,
                idempotency_key=f"academic-proposal:{proposal.proposal_id}",
                operation="notion_update",
                target_type="academic_checkin",
                target_id=str(proposal.proposal_id),
                payload={
                    "changes": [
                        change.model_dump(mode="json", exclude_none=True)
                        for change in proposal.changes
                    ]
                },
                redacted_preview="Academic planner proposed changes; confirmation required.",
                confirmation_token=proposal.confirmation_event,
                expires_at=getattr(proposal, "expires_at", None)
                or now + timedelta(hours=self.confirmation_ttl_hours),
            )

    def save_discord_checkin(
        self,
        proposal: Any,
        *,
        external_event_id: str,
        channel: str,
        received_at: datetime,
    ) -> InboundCheckinPersistResult:
        """Persist one authorized Discord message as a deduplicated check-in.

        The raw Discord message body is deliberately not accepted. A message
        with typed changes gets one pending proposal; a message with no typed
        changes is recorded as questioned and cannot write to Notion.
        """

        event_id = _bounded(external_event_id)
        event_key = f"discord-message:{event_id}"
        with Session(self.engine) as session, session.begin():
            changes = tuple(proposal.changes)
            plan_id = _resolve_study_plan_id(session, proposal.source_plan_id)
            checkin_status: Literal["questioned", "proposal_pending"] = (
                "proposal_pending" if changes else "questioned"
            )
            checkin_values = {
                "idempotency_key": event_key,
                "external_event_id": event_id,
                "channel": _bounded(channel, 64),
                "received_at": _utc(received_at, "received_at"),
                "content_artifact_key": None,
                "redacted_summary": (
                    "Academic check-in proposal pending confirmation."
                    if changes
                    else "Academic check-in needs clarification."
                ),
                "status": checkin_status,
                "plan_id": plan_id,
            }
            try:
                checkin = AcademicCheckIn(**checkin_values)
                with session.begin_nested():
                    session.add(checkin)
                    session.flush()
            except IntegrityError:
                existing_checkin = session.scalar(
                    select(AcademicCheckIn)
                    .where(
                        or_(
                            AcademicCheckIn.idempotency_key == event_key,
                            AcademicCheckIn.external_event_id == event_id,
                        )
                    )
                    .with_for_update()
                )
                if existing_checkin is None:
                    raise
                existing_proposal = session.scalar(
                    select(AcademicProposedChange).where(
                        AcademicProposedChange.checkin_id == existing_checkin.id
                    )
                )
                return InboundCheckinPersistResult(
                    status="replayed",
                    checkin_id=existing_checkin.id,
                    checkin_status=existing_checkin.status,
                    proposal_row_id=existing_proposal.id if existing_proposal is not None else None,
                )
            proposal_row: AcademicProposedChange | None = None
            if changes:
                now = datetime.now(UTC)
                proposal_row = AcademicRepository.create_proposed_change(
                    session,
                    checkin_id=checkin.id,
                    idempotency_key=f"academic-proposal:{proposal.proposal_id}",
                    operation="notion_update",
                    target_type="academic_checkin",
                    target_id=str(proposal.proposal_id),
                    payload={
                        "changes": [
                            change.model_dump(mode="json", exclude_none=True) for change in changes
                        ]
                    },
                    redacted_preview="Academic planner proposed changes; confirmation required.",
                    confirmation_token=proposal.confirmation_event,
                    expires_at=getattr(proposal, "expires_at", None)
                    or now + timedelta(hours=self.confirmation_ttl_hours),
                )
            return InboundCheckinPersistResult(
                status="created",
                checkin_id=checkin.id,
                checkin_status=checkin_status,
                proposal_row_id=proposal_row.id if proposal_row is not None else None,
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
            return _checkin_proposal_from_row(CheckinProposal, proposal_id, row, changes)

    def prepare_checkin_application(
        self,
        proposal_id: uuid.UUID,
        confirmation_event: str,
        *,
        now: datetime | None = None,
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
                now=now,
            )
            changes = tuple(ProposedChange(**value) for value in row.payload.get("changes", []))
            proposal = _checkin_proposal_from_row(CheckinProposal, proposal_id, row, changes)
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

    def reject_checkin_proposal(
        self,
        proposal_id: uuid.UUID,
        *,
        actor: str = "academic_planner",
        now: datetime | None = None,
    ) -> tuple[ProposalRejectionStatus, Any | None]:
        from app.agents.academic_planner.contracts import CheckinProposal, ProposedChange

        with Session(self.engine) as session, session.begin():
            row = session.scalar(
                select(AcademicProposedChange)
                .where(AcademicProposedChange.idempotency_key == f"academic-proposal:{proposal_id}")
                .with_for_update()
            )
            if row is None:
                return "not_pending", None
            status, row = AcademicRepository.reject_proposed_change(
                session,
                proposal_id=row.id,
                actor=actor,
                now=now,
            )
            changes = tuple(ProposedChange(**value) for value in row.payload.get("changes", []))
            proposal = _checkin_proposal_from_row(CheckinProposal, proposal_id, row, changes)
            return status, proposal

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
            ends_at = _field_datetime(assessment, "ends_at", "end_at", "date_end", "end")
            if ends_at is not None and (due_at is None or ends_at <= due_at):
                ends_at = None
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
                ends_at=ends_at,
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

    def begin_proposal_operation(
        self,
        *,
        proposal_id: uuid.UUID,
        ordinal: int,
        payload_hash: str,
        operation_id: str,
    ) -> tuple[ProposalOperationStatus, Mapping[str, Any]]:
        with Session(self.engine) as session, session.begin():
            status, row = AcademicRepository.begin_proposal_operation(
                session,
                proposal_id=proposal_id,
                ordinal=ordinal,
                payload_hash=payload_hash,
                operation_id=operation_id,
            )
            return status, _proposal_operation_snapshot(row)

    def mark_proposal_operation_applied(
        self,
        *,
        proposal_id: uuid.UUID,
        ordinal: int,
        payload_hash: str,
        receipt: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        with Session(self.engine) as session, session.begin():
            row = AcademicRepository.mark_proposal_operation_applied(
                session,
                proposal_id=proposal_id,
                ordinal=ordinal,
                payload_hash=payload_hash,
                receipt=receipt,
            )
            return _proposal_operation_snapshot(row)

    def mark_proposal_operation_uncertain(
        self,
        *,
        proposal_id: uuid.UUID,
        ordinal: int,
        payload_hash: str,
        error_code: str,
    ) -> Mapping[str, Any]:
        with Session(self.engine) as session, session.begin():
            row = AcademicRepository.mark_proposal_operation_uncertain(
                session,
                proposal_id=proposal_id,
                ordinal=ordinal,
                payload_hash=payload_hash,
                error_code=error_code,
            )
            return _proposal_operation_snapshot(row)

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
                tutorial_preview_title=(
                    str(kwargs["tutorial_preview_title"])
                    if kwargs.get("tutorial_preview_title") is not None
                    else None
                ),
                lab_preview_title=(
                    str(kwargs["lab_preview_title"])
                    if kwargs.get("lab_preview_title") is not None
                    else None
                ),
                studying_block_preview_title=(
                    str(kwargs["studying_block_preview_title"])
                    if kwargs.get("studying_block_preview_title") is not None
                    else None
                ),
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
        action: AcademicClarificationAction,
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


def _resolve_assessment_scope(
    session: Session,
    assessment_id: uuid.UUID | str,
) -> tuple[uuid.UUID, str] | None:
    """Resolve planner-facing Notion IDs without weakening the internal FK scope."""

    raw_id = _bounded(str(assessment_id))
    parsed_id = _parse_uuid(raw_id)
    row = session.get(Assessment, parsed_id) if parsed_id is not None else None
    if row is None:
        row = session.scalar(select(Assessment).where(Assessment.notion_id == raw_id))
    if row is None:
        return None
    return row.id, row.notion_id


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


def _academic_search_text(value: str) -> str:
    bounded = value.strip()[:300].casefold()
    if bounded in {"*", "all", "any", "everything"}:
        return ""
    return re.sub(r"[^a-z0-9]+", "", bounded)


def _academic_option_matches(needle: str, *values: str) -> bool:
    if not needle:
        return True
    return any(needle in re.sub(r"[^a-z0-9]+", "", value.casefold()) for value in values)


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
    preview_titles = {
        "quiz": row.quiz_preview_title,
        "assignment": row.assignment_preview_title,
        "tutorial": row.tutorial_preview_title,
        "lab": row.lab_preview_title,
        "studying_block": row.studying_block_preview_title,
    }
    return {
        "id": str(row.id),
        "event_notion_id": row.event_notion_id,
        "course_id": str(row.course_id) if row.course_id is not None else None,
        "assessment_id": str(row.assessment_id) if row.assessment_id is not None else None,
        "original_title": row.original_title,
        "quiz_preview_title": row.quiz_preview_title,
        "assignment_preview_title": row.assignment_preview_title,
        "tutorial_preview_title": row.tutorial_preview_title,
        "lab_preview_title": row.lab_preview_title,
        "studying_block_preview_title": row.studying_block_preview_title,
        "preview_titles": {
            action: title for action, title in preview_titles.items() if title is not None
        },
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
