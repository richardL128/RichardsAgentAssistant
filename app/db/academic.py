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

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError, NoResultFound
from sqlalchemy.orm import Session

from app.agents.academic_planner.calendar_roles import (
    AcademicCalendarRole,
    academic_calendar_role,
)
from app.db.models import (
    AcademicAssessmentMaterialProfile,
    AcademicCheckIn,
    AcademicClarification,
    AcademicCourseCalendar,
    AcademicDiscourseSession,
    AcademicDiscourseTurn,
    AcademicDocument,
    AcademicDocumentChunk,
    AcademicInboundMaterial,
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
AcademicInboundMaterialCreateStatus = Literal["created", "replayed"]
AcademicInboundMaterialState = Literal[
    "captured",
    "awaiting_target",
    "proposal_pending",
    "seeding",
    "seeded",
    "failed",
    "uncertain",
    "expired",
]
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
CalendarSemanticStatus = Literal["valid", "not_substantive", "unavailable", "invalid"]
AcademicClarificationWriteAction = Literal[
    "quiz",
    "assignment",
    "tutorial",
    "lab",
    "event",
]
AcademicClarificationAction = Literal[
    "quiz",
    "assignment",
    "tutorial",
    "lab",
    "event",
    "ignore",
]


class AcademicSemanticUnavailableError(RuntimeError):
    """A semantic lookup could not run; an empty corpus is a different result."""

    code = "embeddings_unavailable"

    def __init__(self) -> None:
        super().__init__("academic semantic embeddings are unavailable")


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
ACADEMIC_MATERIAL_EMBEDDING_DIMENSIONS = 1024
AGENT_CLARIFICATION_SESSION_KIND = "agent_clarification"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TORONTO = ZoneInfo("America/Toronto")
_ACADEMIC_CLARIFICATION_WRITE_ACTIONS = frozenset(
    ("quiz", "assignment", "tutorial", "lab", "event")
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
_ACADEMIC_INBOUND_MATERIAL_STATES = frozenset(
    (
        "captured",
        "awaiting_target",
        "proposal_pending",
        "seeding",
        "seeded",
        "failed",
        "uncertain",
        "expired",
    )
)
_ACADEMIC_INBOUND_MATERIAL_PENDING_STATES = frozenset(
    ("captured", "awaiting_target", "proposal_pending", "uncertain")
)
_ACADEMIC_INBOUND_MATERIAL_TERMINAL_STATES = frozenset(("seeded", "failed", "expired"))
_ACADEMIC_INBOUND_MATERIAL_STATE_RANK = {
    "captured": 0,
    "awaiting_target": 1,
    "uncertain": 2,
    "proposal_pending": 3,
    "seeding": 4,
    "seeded": 5,
    "failed": 5,
    "expired": 5,
}
_DISCORD_ID = re.compile(r"^[0-9]{1,32}$")
_SAFE_PDF_FILENAME = re.compile(r"^[^/\\:\x00-\x1f\x7f]{1,255}$")
_SIGNED_NOTION_URL_MARKERS = (
    "prod-files-secure.s3.",
    "prod-files-secure.notion-static.com",
    "x-amz-signature=",
    "x-amz-credential=",
    "x-amz-security-token=",
)
_ABORTABLE_DISCOURSE_SESSION_KINDS = frozenset(
    ("learning_focus", "memory_review", AGENT_CLARIFICATION_SESSION_KIND)
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
class MaterialEmbeddingBackfillCandidate:
    """One active material chunk that needs a current-model embedding."""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    content: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class ReflectionEmbeddingBackfillCandidate:
    """One reflection memory that needs a current-model embedding."""

    memory_id: uuid.UUID
    raw_text: str
    raw_text_hash: str


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
    event_preview_title: str | None = None
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
class AcademicInboundMaterialInput:
    """Validated Discord PDF metadata ready for durable owner-scoped capture."""

    discord_message_id: str
    discord_attachment_id: str
    owner_discord_user_id: str
    discord_channel_id: str
    filename: str
    media_type: str | None
    declared_byte_size: int | None
    observed_byte_size: int
    content_hash: str
    raw_artifact_key: str
    captured_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AcademicInboundMaterialPersistResult:
    """Create-or-replay result for one Discord attachment identity."""

    status: AcademicInboundMaterialCreateStatus
    row: AcademicInboundMaterial


@dataclass(frozen=True, slots=True)
class AcademicInboundMaterialSnapshot:
    """Owner-authorized intake metadata without raw Discord URLs or PDF text."""

    id: uuid.UUID
    discord_message_id: str
    discord_attachment_id: str
    owner_discord_user_id: str
    discord_channel_id: str
    filename: str
    media_type: str | None
    declared_byte_size: int | None
    observed_byte_size: int
    content_hash: str
    raw_artifact_key: str
    state: str
    assessment_id: uuid.UUID | None
    proposal_id: uuid.UUID | None
    notion_page_id: str | None
    notion_block_id: str | None
    notion_upload_id: str | None
    error_code: str | None
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class LearningFocusMemoryInput:
    """Raw reflection text plus optional embedding payload for one focus turn."""

    raw_text: str
    embedding: Sequence[float] | None = None
    embedding_model: str | None = None
    embedding_metadata: Mapping[str, Any] | None = None
    redacted_summary: str | None = None


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
    intent_value: str | None = None
    intent_status: CalendarSemanticStatus | None = None
    intent_rationale: str | None = None
    intent_evidence_ids: Sequence[str] = ()
    evidence_ids: Sequence[str] = ()
    description_evidence_ids: Sequence[str] = ()


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


class AcademicInboundMaterialRepository:
    """Durable owner-scoped intake state for Discord PDF material."""

    @staticmethod
    def create_or_replay(
        session: Session,
        material: AcademicInboundMaterialInput,
    ) -> AcademicInboundMaterialPersistResult:
        values = _inbound_material_values(material)
        row = AcademicInboundMaterial(**values)
        try:
            with session.begin_nested():
                session.add(row)
                session.flush()
            return AcademicInboundMaterialPersistResult(status="created", row=row)
        except IntegrityError:
            existing = session.scalar(
                select(AcademicInboundMaterial)
                .where(
                    AcademicInboundMaterial.discord_message_id == values["discord_message_id"],
                    AcademicInboundMaterial.discord_attachment_id
                    == values["discord_attachment_id"],
                )
                .with_for_update()
            )
            if existing is None:
                raise
            _validate_inbound_material_replay(existing, values)
            return AcademicInboundMaterialPersistResult(status="replayed", row=existing)

    @staticmethod
    def get_owned(
        session: Session,
        material_id: uuid.UUID,
        *,
        owner_discord_user_id: str,
        discord_channel_id: str,
        now: datetime | None = None,
        include_expired: bool = False,
    ) -> AcademicInboundMaterial | None:
        row = session.scalar(
            select(AcademicInboundMaterial)
            .where(
                AcademicInboundMaterial.id == material_id,
                AcademicInboundMaterial.owner_discord_user_id
                == _discord_id(owner_discord_user_id, "owner_discord_user_id"),
                AcademicInboundMaterial.discord_channel_id
                == _discord_id(discord_channel_id, "discord_channel_id"),
            )
            .with_for_update()
        )
        if row is None:
            return None
        AcademicInboundMaterialRepository.expire_if_needed(session, row, now=now)
        if not include_expired and row.state == "expired":
            return None
        return row

    @staticmethod
    def get_owned_snapshot(
        session: Session,
        material_id: uuid.UUID,
        *,
        owner_discord_user_id: str,
        discord_channel_id: str,
        now: datetime | None = None,
        include_expired: bool = False,
    ) -> AcademicInboundMaterialSnapshot | None:
        row = AcademicInboundMaterialRepository.get_owned(
            session,
            material_id,
            owner_discord_user_id=owner_discord_user_id,
            discord_channel_id=discord_channel_id,
            now=now,
            include_expired=include_expired,
        )
        return _inbound_material_snapshot(row) if row is not None else None

    @staticmethod
    def find_recent_pending(
        session: Session,
        *,
        owner_discord_user_id: str,
        discord_channel_id: str,
        now: datetime | None = None,
        limit: int = 10,
        states: Iterable[str] | None = None,
    ) -> list[AcademicInboundMaterial]:
        bounded_limit = max(1, min(limit, 50))
        current = _utc(now or datetime.now(UTC), "now")
        selected_states = frozenset(states or _ACADEMIC_INBOUND_MATERIAL_PENDING_STATES)
        if not selected_states <= _ACADEMIC_INBOUND_MATERIAL_STATES:
            raise ValueError("invalid inbound material state filter")
        return list(
            session.scalars(
                select(AcademicInboundMaterial)
                .where(
                    AcademicInboundMaterial.owner_discord_user_id
                    == _discord_id(owner_discord_user_id, "owner_discord_user_id"),
                    AcademicInboundMaterial.discord_channel_id
                    == _discord_id(discord_channel_id, "discord_channel_id"),
                    AcademicInboundMaterial.state.in_(selected_states),
                    or_(
                        AcademicInboundMaterial.expires_at.is_(None),
                        AcademicInboundMaterial.expires_at > current,
                    ),
                )
                .order_by(AcademicInboundMaterial.created_at.desc(), AcademicInboundMaterial.id)
                .limit(bounded_limit)
            )
        )

    @staticmethod
    def find_duplicate_available(
        session: Session,
        *,
        owner_discord_user_id: str,
        discord_channel_id: str,
        content_hash: str,
        observed_byte_size: int,
        now: datetime | None = None,
        exclude_material_id: uuid.UUID | None = None,
        limit: int = 10,
    ) -> list[AcademicInboundMaterial]:
        if not _SHA256.fullmatch(content_hash):
            raise ValueError("content hash must be SHA-256 hex")
        if observed_byte_size <= 0:
            raise ValueError("observed byte size must be positive")
        current = _utc(now or datetime.now(UTC), "now")
        conditions = [
            AcademicInboundMaterial.owner_discord_user_id
            == _discord_id(owner_discord_user_id, "owner_discord_user_id"),
            AcademicInboundMaterial.discord_channel_id
            == _discord_id(discord_channel_id, "discord_channel_id"),
            AcademicInboundMaterial.content_hash == content_hash,
            AcademicInboundMaterial.observed_byte_size == observed_byte_size,
            AcademicInboundMaterial.state.in_(
                _ACADEMIC_INBOUND_MATERIAL_PENDING_STATES | {"seeded"}
            ),
            or_(
                AcademicInboundMaterial.expires_at.is_(None),
                AcademicInboundMaterial.expires_at > current,
                AcademicInboundMaterial.state == "seeded",
            ),
        ]
        if exclude_material_id is not None:
            conditions.append(AcademicInboundMaterial.id != exclude_material_id)
        return list(
            session.scalars(
                select(AcademicInboundMaterial)
                .where(*conditions)
                .order_by(AcademicInboundMaterial.created_at.desc(), AcademicInboundMaterial.id)
                .limit(max(1, min(limit, 50)))
            )
        )

    @staticmethod
    def validate_for_proposal(
        session: Session,
        material_ids: Sequence[uuid.UUID],
        *,
        owner_discord_user_id: str,
        discord_channel_id: str,
        now: datetime | None = None,
        proposal_id: uuid.UUID | None = None,
    ) -> tuple[uuid.UUID, ...]:
        if not material_ids:
            return ()
        if len(material_ids) > 5:
            raise ValueError("at most five inbound materials can be attached to one proposal")
        unique_ids = tuple(dict.fromkeys(material_ids))
        if len(unique_ids) != len(material_ids):
            raise ValueError("duplicate inbound material IDs are not allowed")
        for material_id in unique_ids:
            row = AcademicInboundMaterialRepository.get_owned(
                session,
                material_id,
                owner_discord_user_id=owner_discord_user_id,
                discord_channel_id=discord_channel_id,
                now=now,
            )
            if row is None:
                raise NoResultFound(f"academic inbound material {material_id} was not found")
            if row.state not in {"captured", "awaiting_target", "proposal_pending"}:
                raise ValueError("inbound material is not available for a proposal")
            if row.proposal_id is not None and row.proposal_id != proposal_id:
                linked = session.get(AcademicProposedChange, row.proposal_id)
                if linked is None or linked.state in {"pending", "confirmed", "applying"}:
                    raise ValueError("inbound material is bound to a competing live proposal")
        return unique_ids

    @staticmethod
    def bind_to_proposal(
        session: Session,
        material_id: uuid.UUID,
        *,
        owner_discord_user_id: str,
        discord_channel_id: str,
        proposal_id: uuid.UUID,
        assessment_id: uuid.UUID | str | None = None,
        now: datetime | None = None,
    ) -> AcademicInboundMaterial:
        row = AcademicInboundMaterialRepository.get_owned(
            session,
            material_id,
            owner_discord_user_id=owner_discord_user_id,
            discord_channel_id=discord_channel_id,
            now=now,
        )
        if row is None:
            raise NoResultFound(f"academic inbound material {material_id} was not found")
        if row.state not in {"captured", "awaiting_target", "proposal_pending"}:
            raise ValueError("inbound material is not available for proposal binding")
        if row.proposal_id is not None and row.proposal_id != proposal_id:
            raise ValueError("inbound material is already bound to another live proposal")
        bound_assessment_id = _parse_uuid(assessment_id) if assessment_id is not None else None
        if assessment_id is not None and bound_assessment_id is None:
            raise ValueError("assessment_id must be a UUID")
        if bound_assessment_id is not None and row.assessment_id not in (
            None,
            bound_assessment_id,
        ):
            raise ValueError("inbound material is already bound to another assessment")
        proposal = session.get(AcademicProposedChange, proposal_id)
        if proposal is None:
            raise NoResultFound(f"academic proposal {proposal_id} was not found")
        if proposal.state != "pending":
            raise ValueError("proposal must be pending before material binding")
        row.proposal_id = proposal_id
        if bound_assessment_id is not None:
            row.assessment_id = bound_assessment_id
        _advance_inbound_material_state(row, "proposal_pending")
        session.flush()
        return row

    @staticmethod
    def bind_to_assessment(
        session: Session,
        material_id: uuid.UUID,
        *,
        owner_discord_user_id: str,
        discord_channel_id: str,
        assessment_id: uuid.UUID,
        now: datetime | None = None,
    ) -> AcademicInboundMaterial:
        row = AcademicInboundMaterialRepository.get_owned(
            session,
            material_id,
            owner_discord_user_id=owner_discord_user_id,
            discord_channel_id=discord_channel_id,
            now=now,
        )
        if row is None:
            raise NoResultFound(f"academic inbound material {material_id} was not found")
        if row.assessment_id is not None and row.assessment_id != assessment_id:
            raise ValueError("inbound material is already bound to another assessment")
        row.assessment_id = assessment_id
        _advance_inbound_material_state(row, "awaiting_target")
        session.flush()
        return row

    @staticmethod
    def advance_state(
        session: Session,
        material_id: uuid.UUID,
        *,
        owner_discord_user_id: str,
        discord_channel_id: str,
        state: AcademicInboundMaterialState,
        error_code: str | None = None,
        now: datetime | None = None,
    ) -> AcademicInboundMaterial:
        row = AcademicInboundMaterialRepository.get_owned(
            session,
            material_id,
            owner_discord_user_id=owner_discord_user_id,
            discord_channel_id=discord_channel_id,
            now=now,
            include_expired=True,
        )
        if row is None:
            raise NoResultFound(f"academic inbound material {material_id} was not found")
        _advance_inbound_material_state(row, state)
        if error_code is not None or state != "failed":
            row.error_code = _bounded_optional(error_code, 128)
        session.flush()
        return row

    @staticmethod
    def mark_seeded(
        session: Session,
        material_id: uuid.UUID,
        *,
        owner_discord_user_id: str,
        discord_channel_id: str,
        notion_page_id: str,
        notion_block_id: str | None,
        notion_upload_id: str | None,
        proposal_id: uuid.UUID | None = None,
        assessment_id: uuid.UUID | None = None,
    ) -> AcademicInboundMaterial:
        row = AcademicInboundMaterialRepository.get_owned(
            session,
            material_id,
            owner_discord_user_id=owner_discord_user_id,
            discord_channel_id=discord_channel_id,
            include_expired=True,
        )
        if row is None:
            raise NoResultFound(f"academic inbound material {material_id} was not found")
        if proposal_id is not None and row.proposal_id not in (None, proposal_id):
            raise ValueError("seeded receipt proposal does not match bound material")
        if assessment_id is not None and row.assessment_id not in (None, assessment_id):
            raise ValueError("seeded receipt assessment does not match bound material")
        row.proposal_id = proposal_id or row.proposal_id
        row.assessment_id = assessment_id or row.assessment_id
        row.notion_page_id = _bounded(notion_page_id, 255)
        row.notion_block_id = _bounded_optional(notion_block_id, 255)
        row.notion_upload_id = _bounded_optional(notion_upload_id, 255)
        row.error_code = None
        _advance_inbound_material_state(row, "seeded")
        session.flush()
        return row

    @staticmethod
    def expire_if_needed(
        session: Session,
        row: AcademicInboundMaterial,
        *,
        now: datetime | None = None,
    ) -> bool:
        if row.state in _ACADEMIC_INBOUND_MATERIAL_TERMINAL_STATES:
            return False
        if row.expires_at is None:
            return False
        if _aware_db(row.expires_at) > _utc(now or datetime.now(UTC), "now"):
            return False
        row.state = "expired"
        row.error_code = "intake_expired"
        session.flush()
        return True

    @staticmethod
    def expire_unresolved_for_abort(
        session: Session,
        *,
        owner_discord_user_id: str,
        discord_channel_id: str,
        now: datetime,
    ) -> int:
        current = _utc(now, "now")
        rows = list(
            session.scalars(
                select(AcademicInboundMaterial)
                .where(
                    AcademicInboundMaterial.owner_discord_user_id
                    == _discord_id(owner_discord_user_id, "owner_discord_user_id"),
                    AcademicInboundMaterial.discord_channel_id
                    == _discord_id(discord_channel_id, "discord_channel_id"),
                    AcademicInboundMaterial.state.in_(("captured", "awaiting_target")),
                    AcademicInboundMaterial.proposal_id.is_(None),
                    AcademicInboundMaterial.created_at < current,
                )
                .with_for_update()
            )
        )
        for row in rows:
            row.state = "expired"
            row.error_code = "user_abort"
            row.expires_at = current
        session.flush()
        return len(rows)

    @staticmethod
    def find_pending_create_proposals(
        session: Session,
        *,
        owner_discord_user_id: str,
        discord_channel_id: str,
        now: datetime | None = None,
        limit: int = 10,
    ) -> list[AcademicProposedChange]:
        current = _utc(now or datetime.now(UTC), "now")
        rows = session.execute(
            select(AcademicProposedChange, AcademicCheckIn)
            .join(AcademicCheckIn, AcademicCheckIn.id == AcademicProposedChange.checkin_id)
            .where(
                AcademicProposedChange.state == "pending",
                or_(
                    AcademicProposedChange.expires_at.is_(None),
                    AcademicProposedChange.expires_at > current,
                ),
            )
            .order_by(AcademicProposedChange.created_at.desc(), AcademicProposedChange.id)
            .limit(max(1, min(limit * 4, 100)))
        )
        scoped: list[AcademicProposedChange] = []
        for proposal, checkin in rows:
            if not _proposal_matches_owner_channel(
                proposal,
                checkin,
                owner_discord_user_id=owner_discord_user_id,
                discord_channel_id=discord_channel_id,
            ):
                continue
            if not _proposal_is_creation_only(proposal):
                continue
            scoped.append(proposal)
            if len(scoped) >= max(1, min(limit, 50)):
                break
        return scoped

    @staticmethod
    def find_single_pending_create_proposal(
        session: Session,
        *,
        owner_discord_user_id: str,
        discord_channel_id: str,
        now: datetime | None = None,
    ) -> tuple[Literal["found", "not_found", "ambiguous"], AcademicProposedChange | None]:
        rows = AcademicInboundMaterialRepository.find_pending_create_proposals(
            session,
            owner_discord_user_id=owner_discord_user_id,
            discord_channel_id=discord_channel_id,
            now=now,
            limit=2,
        )
        if len(rows) == 1:
            return "found", rows[0]
        if not rows:
            return "not_found", None
        return "ambiguous", None

    @staticmethod
    def supersede_pending_create_with_materials(
        session: Session,
        *,
        old_proposal_id: uuid.UUID,
        new_proposal_id: uuid.UUID,
        material_ids: Sequence[uuid.UUID],
        owner_discord_user_id: str,
        discord_channel_id: str,
        superseded_reason: str = "replacement_with_inbound_material",
        now: datetime | None = None,
    ) -> AcademicProposedChange:
        AcademicInboundMaterialRepository.validate_for_proposal(
            session,
            material_ids,
            owner_discord_user_id=owner_discord_user_id,
            discord_channel_id=discord_channel_id,
            now=now,
            proposal_id=new_proposal_id,
        )
        old = AcademicInboundMaterialRepository.mark_proposal_superseded(
            session,
            old_proposal_id=old_proposal_id,
            new_proposal_id=new_proposal_id,
            owner_discord_user_id=owner_discord_user_id,
            discord_channel_id=discord_channel_id,
            superseded_reason=superseded_reason,
            now=now,
        )
        for material_id in material_ids:
            AcademicInboundMaterialRepository.bind_to_proposal(
                session,
                material_id,
                owner_discord_user_id=owner_discord_user_id,
                discord_channel_id=discord_channel_id,
                proposal_id=new_proposal_id,
                now=now,
            )
        return old

    @staticmethod
    def mark_proposal_superseded(
        session: Session,
        *,
        old_proposal_id: uuid.UUID,
        new_proposal_id: uuid.UUID,
        owner_discord_user_id: str,
        discord_channel_id: str,
        superseded_reason: str = "replacement_with_inbound_material",
        actor: str = "academic_planner",
        now: datetime | None = None,
    ) -> AcademicProposedChange:
        old = session.scalar(
            select(AcademicProposedChange)
            .where(AcademicProposedChange.id == old_proposal_id)
            .with_for_update()
        )
        if old is None:
            raise NoResultFound(f"academic proposal {old_proposal_id} was not found")
        new = session.get(AcademicProposedChange, new_proposal_id)
        if new is None:
            raise NoResultFound(f"academic proposal {new_proposal_id} was not found")
        checkin = session.get(AcademicCheckIn, old.checkin_id)
        if checkin is None or not _proposal_matches_owner_channel(
            old,
            checkin,
            owner_discord_user_id=owner_discord_user_id,
            discord_channel_id=discord_channel_id,
        ):
            raise ValueError("proposal is not scoped to the owner/channel")
        current = _utc(now or datetime.now(UTC), "now")
        if old.expires_at is not None and _aware_db(old.expires_at) <= current:
            old.state = "expired"
            session.flush()
            return old
        if old.state != "pending":
            raise ValueError("only pending proposals can be superseded")
        old.state = "superseded"
        old.superseded_by_id = new_proposal_id
        old.superseded_reason = _bounded(superseded_reason, 128)
        old.superseded_at = current
        AuditRepository.append(
            session,
            actor=_bounded(actor, 128),
            action="academic_proposal.superseded",
            target_type="academic_proposal",
            target_id=str(old_proposal_id),
            result="superseded",
        )
        session.flush()
        return old


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
        is_all_day: bool = False,
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
            "is_all_day": is_all_day,
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
    def save_assessment_calendar_semantics(
        session: Session,
        *,
        notion_id: str,
        semantics: CalendarSemanticResultInput,
    ) -> bool:
        row = session.scalar(
            select(Assessment).where(Assessment.notion_id == _bounded(notion_id)).with_for_update()
        )
        if row is None:
            return False
        source_edit = (
            _utc(semantics.source_last_edited_at, "source_last_edited_at")
            if semantics.source_last_edited_at is not None
            else None
        )
        if (
            source_edit is not None
            and row.notion_last_edited_at is not None
            and _aware_db(row.notion_last_edited_at) != source_edit
        ):
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
            "event_preview_title": _bounded_optional(request.event_preview_title, 1_024),
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
        inbound_state_counts = {
            state: int(count)
            for state, count in session.execute(
                select(AcademicInboundMaterial.state, func.count()).group_by(
                    AcademicInboundMaterial.state
                )
            )
        }
        orphan_upload_warnings = session.scalar(
            select(func.count())
            .select_from(AcademicInboundMaterial)
            .where(
                AcademicInboundMaterial.notion_upload_id.is_not(None),
                AcademicInboundMaterial.notion_block_id.is_(None),
                AcademicInboundMaterial.state.in_(["seeding", "uncertain", "failed"]),
            )
        )
        seeded_intakes = list(
            session.scalars(
                select(AcademicInboundMaterial).where(
                    AcademicInboundMaterial.state == "seeded",
                    AcademicInboundMaterial.notion_page_id.is_not(None),
                )
            )
        )
        indexing_delayed = 0
        for intake in seeded_intakes:
            source_matches = [
                AcademicDocument.notion_id == intake.notion_page_id,
                AcademicDocument.source_page_id == intake.notion_page_id,
            ]
            if intake.notion_block_id is not None:
                source_matches.append(AcademicDocument.source_block_id == intake.notion_block_id)
            has_active_document = session.scalar(
                select(func.count())
                .select_from(AcademicDocument)
                .where(
                    or_(*source_matches),
                    AcademicDocument.active.is_(True),
                    AcademicDocument.extraction_status.in_(_ACADEMIC_DOCUMENT_USABLE_STATUSES),
                )
            )
            if not has_active_document:
                indexing_delayed += 1
        profile_state_counts = {
            state: int(count)
            for state, count in session.execute(
                select(AcademicAssessmentMaterialProfile.state, func.count()).group_by(
                    AcademicAssessmentMaterialProfile.state
                )
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
            "inbound_material_awaiting_target_count": inbound_state_counts.get(
                "awaiting_target", 0
            ),
            "inbound_material_proposal_pending_count": inbound_state_counts.get(
                "proposal_pending", 0
            ),
            "inbound_material_uncertain_count": inbound_state_counts.get("uncertain", 0),
            "inbound_material_seeding_count": inbound_state_counts.get("seeding", 0),
            "inbound_material_orphan_upload_warning_count": orphan_upload_warnings or 0,
            "inbound_material_indexing_delayed_count": indexing_delayed,
            "material_profile_active_count": profile_state_counts.get("active", 0),
            "material_profile_rejected_count": profile_state_counts.get("rejected", 0),
            "last_sync_at": _aware_db(last_sync).isoformat() if last_sync is not None else None,
            "migration": "0022_academic_material_profiles",
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
    def count_material_embedding_backfill_candidates(
        session: Session,
        *,
        embedding_model: str,
        access_classification: str = "private",
    ) -> int:
        """Count active material chunks missing the exact current-model embedding."""

        model = _bounded(embedding_model, 255)
        candidate_conditions = [
            AcademicDocumentChunk.embedding.is_(None),
            AcademicDocumentChunk.embedding_model != model,
            AcademicDocumentChunk.embedding_model.is_(None),
        ]
        if session.get_bind().dialect.name == "postgresql":
            candidate_conditions.append(
                AcademicDocumentChunk.embedding_dimensions != ACADEMIC_MATERIAL_EMBEDDING_DIMENSIONS
            )
        return int(
            session.scalar(
                select(func.count())
                .select_from(AcademicDocumentChunk)
                .join(AcademicDocument, AcademicDocument.id == AcademicDocumentChunk.document_id)
                .where(
                    AcademicDocument.assessment_id.is_not(None),
                    AcademicDocument.active.is_(True),
                    AcademicDocument.extraction_status.in_(_ACADEMIC_DOCUMENT_USABLE_STATUSES),
                    AcademicDocument.access_classification == access_classification,
                    AcademicDocumentChunk.content != "",
                    or_(*candidate_conditions),
                )
            )
            or 0
        )

    @staticmethod
    def count_active_assessment_material_chunks(
        session: Session,
        *,
        access_classification: str = "private",
    ) -> int:
        """Count active usable private assessment-material chunks."""

        return int(
            session.scalar(
                select(func.count())
                .select_from(AcademicDocumentChunk)
                .join(AcademicDocument, AcademicDocument.id == AcademicDocumentChunk.document_id)
                .where(
                    AcademicDocument.assessment_id.is_not(None),
                    AcademicDocument.active.is_(True),
                    AcademicDocument.extraction_status.in_(_ACADEMIC_DOCUMENT_USABLE_STATUSES),
                    AcademicDocument.access_classification == access_classification,
                    AcademicDocumentChunk.content != "",
                )
            )
            or 0
        )

    @staticmethod
    def list_material_embedding_backfill_candidates(
        session: Session,
        *,
        embedding_model: str,
        limit: int = 100,
        after_chunk_id: uuid.UUID | None = None,
        access_classification: str = "private",
    ) -> list[MaterialEmbeddingBackfillCandidate]:
        """Return bounded, id-ordered active material chunks needing current vectors."""

        if limit < 1 or limit > 500:
            raise ValueError("material embedding backfill limit must be between 1 and 500")
        model = _bounded(embedding_model, 255)
        candidate_conditions = [
            AcademicDocumentChunk.embedding.is_(None),
            AcademicDocumentChunk.embedding_model != model,
            AcademicDocumentChunk.embedding_model.is_(None),
        ]
        if session.get_bind().dialect.name == "postgresql":
            candidate_conditions.append(
                AcademicDocumentChunk.embedding_dimensions != ACADEMIC_MATERIAL_EMBEDDING_DIMENSIONS
            )
        statement = (
            select(
                AcademicDocumentChunk.id,
                AcademicDocumentChunk.document_id,
                AcademicDocumentChunk.content,
                AcademicDocumentChunk.content_hash,
            )
            .join(AcademicDocument, AcademicDocument.id == AcademicDocumentChunk.document_id)
            .where(
                AcademicDocument.assessment_id.is_not(None),
                AcademicDocument.active.is_(True),
                AcademicDocument.extraction_status.in_(_ACADEMIC_DOCUMENT_USABLE_STATUSES),
                AcademicDocument.access_classification == access_classification,
                AcademicDocumentChunk.content != "",
                or_(*candidate_conditions),
            )
            .order_by(AcademicDocumentChunk.id)
            .limit(limit)
        )
        if after_chunk_id is not None:
            statement = statement.where(AcademicDocumentChunk.id > after_chunk_id)
        return [
            MaterialEmbeddingBackfillCandidate(
                chunk_id=row_id,
                document_id=document_id,
                content=content,
                content_hash=content_hash,
            )
            for row_id, document_id, content, content_hash in session.execute(statement)
        ]

    @staticmethod
    def update_material_chunk_embedding(
        session: Session,
        *,
        chunk_id: uuid.UUID,
        content_hash: str,
        embedding: Sequence[float],
        embedding_model: str,
    ) -> bool:
        """Persist a derived embedding only if the chunk content is unchanged."""

        if not _SHA256.fullmatch(content_hash):
            raise ValueError("chunk content_hash must be SHA-256 hex")
        vector = _embedding_vector(embedding)
        if vector is None:
            raise ValueError("embedding must not be empty")
        if (
            session.get_bind().dialect.name == "postgresql"
            and len(vector) != ACADEMIC_MATERIAL_EMBEDDING_DIMENSIONS
        ):
            raise ValueError("material embeddings must be 1024-dimensional on PostgreSQL")
        model = _bounded(embedding_model, 255)
        chunk = session.get(AcademicDocumentChunk, chunk_id)
        if chunk is None or chunk.content_hash != content_hash:
            return False
        chunk.embedding = vector
        chunk.embedding_model = model
        chunk.embedding_dimensions = len(vector)
        session.flush()
        return True

    @staticmethod
    def count_reflection_embedding_backfill_candidates(
        session: Session,
        *,
        embedding_model: str,
    ) -> int:
        """Count reflection memories missing the exact current-model embedding."""

        model = _bounded(embedding_model, 255)
        candidate_conditions = [
            AcademicReflectionMemory.embedding.is_(None),
            AcademicReflectionMemory.embedding_model != model,
            AcademicReflectionMemory.embedding_model.is_(None),
        ]
        if session.get_bind().dialect.name == "postgresql":
            candidate_conditions.append(
                AcademicReflectionMemory.embedding_dimensions
                != ACADEMIC_MATERIAL_EMBEDDING_DIMENSIONS
            )
        return int(
            session.scalar(
                select(func.count())
                .select_from(AcademicReflectionMemory)
                .where(
                    AcademicReflectionMemory.raw_text != "",
                    or_(*candidate_conditions),
                )
            )
            or 0
        )

    @staticmethod
    def count_reflection_memories(session: Session) -> int:
        """Count persisted reflection memories with raw text available for embedding."""

        return int(
            session.scalar(
                select(func.count())
                .select_from(AcademicReflectionMemory)
                .where(AcademicReflectionMemory.raw_text != "")
            )
            or 0
        )

    @staticmethod
    def list_reflection_embedding_backfill_candidates(
        session: Session,
        *,
        embedding_model: str,
        limit: int = 100,
        after_memory_id: uuid.UUID | None = None,
    ) -> list[ReflectionEmbeddingBackfillCandidate]:
        """Return bounded, id-ordered reflection memories needing current vectors."""

        if limit < 1 or limit > 500:
            raise ValueError("reflection embedding backfill limit must be between 1 and 500")
        model = _bounded(embedding_model, 255)
        candidate_conditions = [
            AcademicReflectionMemory.embedding.is_(None),
            AcademicReflectionMemory.embedding_model != model,
            AcademicReflectionMemory.embedding_model.is_(None),
        ]
        if session.get_bind().dialect.name == "postgresql":
            candidate_conditions.append(
                AcademicReflectionMemory.embedding_dimensions
                != ACADEMIC_MATERIAL_EMBEDDING_DIMENSIONS
            )
        statement = (
            select(AcademicReflectionMemory.id, AcademicReflectionMemory.raw_text)
            .where(
                AcademicReflectionMemory.raw_text != "",
                or_(*candidate_conditions),
            )
            .order_by(AcademicReflectionMemory.id)
            .limit(limit)
        )
        if after_memory_id is not None:
            statement = statement.where(AcademicReflectionMemory.id > after_memory_id)
        return [
            ReflectionEmbeddingBackfillCandidate(
                memory_id=memory_id,
                raw_text=raw_text,
                raw_text_hash=_sha256(raw_text),
            )
            for memory_id, raw_text in session.execute(statement)
        ]

    @staticmethod
    def update_reflection_memory_embedding(
        session: Session,
        *,
        memory_id: uuid.UUID,
        raw_text_hash: str,
        embedding: Sequence[float],
        embedding_model: str,
    ) -> bool:
        """Persist a derived reflection embedding only if raw text is unchanged."""

        if not _SHA256.fullmatch(raw_text_hash):
            raise ValueError("raw_text_hash must be SHA-256 hex")
        vector = _embedding_vector(embedding)
        if vector is None:
            raise ValueError("embedding must not be empty")
        if (
            session.get_bind().dialect.name == "postgresql"
            and len(vector) != ACADEMIC_MATERIAL_EMBEDDING_DIMENSIONS
        ):
            raise ValueError("reflection embeddings must be 1024-dimensional on PostgreSQL")
        model = _bounded(embedding_model, 255)
        memory = session.get(AcademicReflectionMemory, memory_id)
        if memory is None or _sha256(memory.raw_text) != raw_text_hash:
            return False
        memory.embedding = vector
        memory.embedding_model = model
        memory.embedding_dimensions = len(vector)
        session.flush()
        return True

    @staticmethod
    def list_material_planning_chunks(
        session: Session,
        *,
        assessment_id: uuid.UUID,
        public_assessment_id: str,
        limit: int,
    ) -> list[Mapping[str, Any]]:
        if limit < 1 or limit > 100:
            raise ValueError("material planning chunk limit must be between 1 and 100")
        rows = session.execute(
            select(AcademicDocumentChunk, AcademicDocument)
            .join(AcademicDocument, AcademicDocument.id == AcademicDocumentChunk.document_id)
            .where(
                AcademicDocument.assessment_id == assessment_id,
                AcademicDocument.active.is_(True),
                AcademicDocument.extraction_status.in_(_ACADEMIC_DOCUMENT_USABLE_STATUSES),
                AcademicDocument.access_classification == "private",
            )
            .order_by(AcademicDocument.retrieved_at.desc(), AcademicDocumentChunk.ordinal)
            .limit(limit)
        )
        return [
            _material_planning_chunk_mapping(chunk, document, public_assessment_id)
            for chunk, document in rows
        ]

    @staticmethod
    def read_material_planning_chunks(
        session: Session,
        chunk_ids: Sequence[str],
    ) -> list[Mapping[str, Any]]:
        ids = tuple(dict.fromkeys(uuid.UUID(str(chunk_id)) for chunk_id in chunk_ids))
        if not ids:
            return []
        if len(ids) > 100:
            raise ValueError("too many material planning chunk ids requested")
        rows = session.execute(
            select(AcademicDocumentChunk, AcademicDocument, Assessment)
            .join(AcademicDocument, AcademicDocument.id == AcademicDocumentChunk.document_id)
            .join(Assessment, Assessment.id == AcademicDocument.assessment_id)
            .where(
                AcademicDocumentChunk.id.in_(ids),
                AcademicDocument.access_classification == "private",
            )
        )
        by_id = {
            chunk.id: _material_planning_chunk_mapping(chunk, document, assessment.notion_id)
            for chunk, document, assessment in rows
        }
        return [by_id[chunk_id] for chunk_id in ids if chunk_id in by_id]

    @staticmethod
    def get_active_material_planning_profile(
        session: Session,
        *,
        assessment_id: uuid.UUID,
    ) -> AcademicAssessmentMaterialProfile | None:
        return session.scalar(
            select(AcademicAssessmentMaterialProfile).where(
                AcademicAssessmentMaterialProfile.assessment_id == assessment_id,
                AcademicAssessmentMaterialProfile.state == "active",
            )
        )

    @staticmethod
    def save_material_planning_profile(
        session: Session,
        *,
        profile: Any,
    ) -> AcademicAssessmentMaterialProfile:
        assessment_scope = _resolve_assessment_scope(session, profile.assessment_id)
        if assessment_scope is None:
            raise NoResultFound(f"academic assessment {profile.assessment_id} was not found")
        assessment_id, public_assessment_id = assessment_scope
        if profile.state == "active":
            _validate_material_planning_profile_evidence(
                session,
                profile,
                assessment_id=assessment_id,
                public_assessment_id=public_assessment_id,
            )
        values = _material_planning_profile_values(profile, assessment_id=assessment_id)
        existing = session.scalar(
            select(AcademicAssessmentMaterialProfile).where(
                AcademicAssessmentMaterialProfile.profile_version == profile.profile_version
            )
        )
        if existing is not None:
            preserved_state = (
                "active"
                if existing.state == "active" and profile.state == "active"
                else values["state"]
            )
            for key, value in values.items():
                if key == "state":
                    setattr(existing, key, preserved_state)
                else:
                    setattr(existing, key, value)
            session.flush()
            return existing
        row = AcademicAssessmentMaterialProfile(**values)
        session.add(row)
        session.flush()
        return row

    @staticmethod
    def activate_material_planning_profile(
        session: Session,
        *,
        profile: Any,
    ) -> AcademicAssessmentMaterialProfile:
        if profile.state != "active":
            raise ValueError("only accepted profiles can be activated")
        assessment_scope = _resolve_assessment_scope(session, profile.assessment_id)
        if assessment_scope is None:
            raise NoResultFound(f"academic assessment {profile.assessment_id} was not found")
        assessment_id, public_assessment_id = assessment_scope
        _validate_material_planning_profile_evidence(
            session,
            profile,
            assessment_id=assessment_id,
            public_assessment_id=public_assessment_id,
        )
        row = AcademicRepository.save_material_planning_profile(session, profile=profile)
        session.execute(
            update(AcademicAssessmentMaterialProfile)
            .where(
                AcademicAssessmentMaterialProfile.assessment_id == assessment_id,
                AcademicAssessmentMaterialProfile.state == "active",
                AcademicAssessmentMaterialProfile.id != row.id,
            )
            .values(state="inactive")
        )
        row.state = "active"
        row.activated_at = datetime.now(UTC)
        session.flush()
        return row

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
        if (
            session.get_bind().dialect.name == "postgresql"
            and len(vector) != ACADEMIC_MATERIAL_EMBEDDING_DIMENSIONS
        ):
            return []
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
    def abort_owner_channel_continuations(
        session: Session,
        *,
        discord_channel_id: str,
        discord_user_id: str,
        abort_event_id: str,
        aborted_at: datetime,
    ) -> dict[str, int]:
        """Close owner/channel resumable state without deleting history."""

        current = _utc(aborted_at, "aborted_at")
        event_id = _bounded(abort_event_id, 255)
        discourse_rows = list(
            session.scalars(
                select(AcademicDiscourseSession)
                .where(
                    AcademicDiscourseSession.state == "open",
                    AcademicDiscourseSession.discord_channel_id == _bounded(discord_channel_id, 24),
                    AcademicDiscourseSession.discord_user_id == _bounded(discord_user_id, 24),
                    AcademicDiscourseSession.last_turn_at < current,
                    AcademicDiscourseSession.session_kind.in_(_ABORTABLE_DISCOURSE_SESSION_KINDS),
                )
                .with_for_update()
            )
        )
        final_state = {
            "outcome": "aborted",
            "abort_discord_event_id": event_id,
            "aborted_at": current.isoformat(),
        }
        for row in discourse_rows:
            row.state = "completed"
            row.completed_at = current
            row.last_turn_at = current
            row.partial_state = dict(final_state)
        material_count = AcademicInboundMaterialRepository.expire_unresolved_for_abort(
            session,
            owner_discord_user_id=discord_user_id,
            discord_channel_id=discord_channel_id,
            now=current,
        )
        session.flush()
        return {
            "discourse_sessions_closed": len(discourse_rows),
            "inbound_materials_expired": material_count,
        }

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
    ) -> AcademicCheckIn:
        values = {
            "idempotency_key": idempotency_key,
            "external_event_id": external_event_id,
            "channel": channel,
            "received_at": _utc(received_at, "received_at"),
            "content_artifact_key": content_artifact_key,
            "redacted_summary": redacted_summary,
            "status": status,
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
        owner_discord_user_id: str | None = None,
        discord_channel_id: str | None = None,
    ) -> AcademicProposedChange:
        if not confirmation_token.strip():
            raise ValueError("confirmation token must not be empty")
        values = {
            "checkin_id": checkin_id,
            "idempotency_key": idempotency_key,
            "operation": operation,
            "target_type": target_type,
            "target_id": target_id,
            "owner_discord_user_id": (
                _discord_id(owner_discord_user_id, "owner_discord_user_id")
                if owner_discord_user_id is not None
                else None
            ),
            "discord_channel_id": (
                _discord_id(discord_channel_id, "discord_channel_id")
                if discord_channel_id is not None
                else None
            ),
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


def _material_planning_chunk_mapping(
    chunk: AcademicDocumentChunk,
    document: AcademicDocument,
    public_assessment_id: str,
) -> Mapping[str, Any]:
    return {
        "chunk_id": str(chunk.id),
        "assessment_id": public_assessment_id,
        "document_id": str(document.id),
        "document_version": document.document_version,
        "content_hash": document.content_hash,
        "active": (
            document.active and document.extraction_status in _ACADEMIC_DOCUMENT_USABLE_STATUSES
        ),
        "content": chunk.content,
        "source_page": chunk.source_page,
        "source_block": chunk.source_block,
        "heading": chunk.heading,
    }


def _material_planning_profile_values(
    profile: Any,
    *,
    assessment_id: uuid.UUID,
) -> dict[str, Any]:
    state = "validated" if profile.state == "active" else "rejected"
    return {
        "assessment_id": assessment_id,
        "profile_id": _bounded(profile.profile_id, 128),
        "profile_version": _bounded(profile.profile_version, 128),
        "state": state,
        "deliverables_summary": _bounded(profile.deliverables_summary, 1_000),
        "success_criteria_summary": _bounded(profile.success_criteria_summary, 1_000),
        "study_topics_summary": _bounded(profile.study_topics_summary, 1_000),
        "explicit_grade_weight_percent": profile.explicit_grade_weight_percent,
        "effort_lower_minutes": profile.effort_lower_minutes,
        "effort_upper_minutes": profile.effort_upper_minutes,
        "scope_score": profile.scope_score,
        "dependency_risk_score": profile.dependency_risk_score,
        "evidence_chunk_ids": list(profile.evidence_chunk_ids),
        "document_versions": [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
            for item in profile.document_versions
        ],
        "model_identity": profile.model_identity.model_dump(mode="json"),
        "critique": profile.critique.model_dump(mode="json"),
        "rejection_reason": _bounded_optional(profile.rejection_reason, 500),
        "generated_at": datetime.now(UTC),
        "activated_at": None,
    }


def _validate_material_planning_profile_evidence(
    session: Session,
    profile: Any,
    *,
    assessment_id: uuid.UUID,
    public_assessment_id: str,
) -> None:
    mappings = AcademicRepository.read_material_planning_chunks(
        session,
        profile.evidence_chunk_ids,
    )
    by_id = {str(item["chunk_id"]): item for item in mappings}
    if set(by_id) != set(profile.evidence_chunk_ids):
        raise ValueError("material planning profile cites missing chunks")
    expected_versions = {
        (str(item.document_id), str(item.document_version), str(item.content_hash))
        for item in profile.document_versions
    }
    observed_versions: set[tuple[str, str, str]] = set()
    for chunk_id in profile.evidence_chunk_ids:
        item = by_id[chunk_id]
        if item["assessment_id"] != public_assessment_id:
            raise ValueError("material planning profile cites another assessment")
        if not item["active"]:
            raise ValueError("material planning profile cites inactive material")
        observed_versions.add(
            (
                str(item["document_id"]),
                str(item["document_version"]),
                str(item["content_hash"]),
            )
        )
    if expected_versions != observed_versions:
        raise ValueError("material planning profile document versions are stale")
    row = session.get(Assessment, assessment_id)
    if row is None or row.notion_id != public_assessment_id:
        raise ValueError("material planning profile assessment scope is stale")


def _material_planning_profile_from_row(
    row: AcademicAssessmentMaterialProfile,
    *,
    public_assessment_id: str,
) -> Any:
    from app.agents.academic_planner.material_planning import (
        AssessmentMaterialPlanningProfile,
        MaterialPlanningDocumentVersion,
        MaterialPlanningModelIdentity,
        MaterialPlanningProfileCritique,
    )

    return AssessmentMaterialPlanningProfile(
        profile_id=row.profile_id,
        profile_version=row.profile_version,
        assessment_id=public_assessment_id,
        state="active" if row.state == "active" else "rejected",
        deliverables_summary=row.deliverables_summary,
        success_criteria_summary=row.success_criteria_summary,
        study_topics_summary=row.study_topics_summary,
        effort_lower_minutes=row.effort_lower_minutes,
        effort_upper_minutes=row.effort_upper_minutes,
        scope_score=row.scope_score,
        dependency_risk_score=row.dependency_risk_score,
        explicit_grade_weight_percent=row.explicit_grade_weight_percent,
        evidence_chunk_ids=tuple(row.evidence_chunk_ids),
        document_versions=tuple(
            MaterialPlanningDocumentVersion.model_validate(item) for item in row.document_versions
        ),
        model_identity=MaterialPlanningModelIdentity.model_validate(row.model_identity),
        critique=MaterialPlanningProfileCritique.model_validate(row.critique),
        rejection_reason=row.rejection_reason,
    )


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


def _inbound_material_values(material: AcademicInboundMaterialInput) -> dict[str, Any]:
    filename = _safe_pdf_filename(material.filename)
    media_type = material.media_type.strip().lower() if material.media_type else None
    if media_type is not None and media_type != "application/pdf":
        raise ValueError("inbound material media type must be application/pdf when present")
    if material.declared_byte_size is not None and material.declared_byte_size <= 0:
        raise ValueError("declared byte size must be positive")
    if material.observed_byte_size <= 0:
        raise ValueError("observed byte size must be positive")
    if (
        material.declared_byte_size is not None
        and material.declared_byte_size != material.observed_byte_size
    ):
        raise ValueError("declared and observed byte size must match")
    if not _SHA256.fullmatch(material.content_hash):
        raise ValueError("content hash must be SHA-256 hex")
    if not _SHA256.fullmatch(material.raw_artifact_key):
        raise ValueError("raw artifact key must be SHA-256 hex")
    return {
        "discord_message_id": _discord_id(material.discord_message_id, "discord_message_id"),
        "discord_attachment_id": _discord_id(
            material.discord_attachment_id, "discord_attachment_id"
        ),
        "owner_discord_user_id": _discord_id(
            material.owner_discord_user_id, "owner_discord_user_id"
        ),
        "discord_channel_id": _discord_id(material.discord_channel_id, "discord_channel_id"),
        "filename": filename,
        "media_type": media_type,
        "declared_byte_size": material.declared_byte_size,
        "observed_byte_size": material.observed_byte_size,
        "content_hash": material.content_hash,
        "raw_artifact_key": material.raw_artifact_key,
        "state": "captured",
        "expires_at": _utc(material.expires_at, "expires_at"),
        "created_at": _utc(material.captured_at, "captured_at"),
        "updated_at": _utc(material.captured_at, "captured_at"),
    }


def _validate_inbound_material_replay(
    row: AcademicInboundMaterial,
    values: Mapping[str, Any],
) -> None:
    immutable_fields = (
        "owner_discord_user_id",
        "discord_channel_id",
        "filename",
        "media_type",
        "declared_byte_size",
        "observed_byte_size",
        "content_hash",
        "raw_artifact_key",
    )
    for field in immutable_fields:
        if getattr(row, field) != values[field]:
            raise ValueError("discord attachment replay conflicts with persisted intake metadata")


def _advance_inbound_material_state(
    row: AcademicInboundMaterial,
    state: AcademicInboundMaterialState | str,
) -> None:
    if state not in _ACADEMIC_INBOUND_MATERIAL_STATES:
        raise ValueError("invalid inbound material state")
    if row.state == state:
        return
    if row.state in _ACADEMIC_INBOUND_MATERIAL_TERMINAL_STATES:
        raise ValueError("terminal inbound material state cannot advance")
    if row.state == "seeding" and state == "uncertain":
        row.state = state
        return
    current_rank = _ACADEMIC_INBOUND_MATERIAL_STATE_RANK[row.state]
    next_rank = _ACADEMIC_INBOUND_MATERIAL_STATE_RANK[state]
    if next_rank < current_rank:
        raise ValueError("inbound material state cannot move backwards")
    row.state = state


def _inbound_material_snapshot(row: AcademicInboundMaterial) -> AcademicInboundMaterialSnapshot:
    return AcademicInboundMaterialSnapshot(
        id=row.id,
        discord_message_id=row.discord_message_id,
        discord_attachment_id=row.discord_attachment_id,
        owner_discord_user_id=row.owner_discord_user_id,
        discord_channel_id=row.discord_channel_id,
        filename=row.filename,
        media_type=row.media_type,
        declared_byte_size=row.declared_byte_size,
        observed_byte_size=row.observed_byte_size,
        content_hash=row.content_hash,
        raw_artifact_key=row.raw_artifact_key,
        state=row.state,
        assessment_id=row.assessment_id,
        proposal_id=row.proposal_id,
        notion_page_id=row.notion_page_id,
        notion_block_id=row.notion_block_id,
        notion_upload_id=row.notion_upload_id,
        error_code=row.error_code,
        expires_at=_aware_db(row.expires_at) if row.expires_at is not None else None,
        created_at=_aware_db(row.created_at),
        updated_at=_aware_db(row.updated_at),
    )


def _proposal_matches_owner_channel(
    proposal: AcademicProposedChange,
    checkin: AcademicCheckIn,
    *,
    owner_discord_user_id: str,
    discord_channel_id: str,
) -> bool:
    expected_owner = _discord_id(owner_discord_user_id, "owner_discord_user_id")
    expected_channel = _discord_id(discord_channel_id, "discord_channel_id")
    payload = proposal.payload or {}
    proposal_owner = proposal.owner_discord_user_id or _payload_string(
        payload,
        "owner_discord_user_id",
        "discord_user_id",
    )
    proposal_channel = proposal.discord_channel_id or _payload_string(
        payload,
        "discord_channel_id",
        "channel_id",
    )
    if proposal_channel is None and checkin.channel.startswith("discord:"):
        proposal_channel = checkin.channel.removeprefix("discord:")
    return proposal_owner == expected_owner and proposal_channel == expected_channel


def _proposal_is_creation_only(proposal: AcademicProposedChange) -> bool:
    payload = proposal.payload or {}
    changes_raw = payload.get("changes")
    if isinstance(changes_raw, list):
        changes = cast(list[object], changes_raw)
        return len(changes) == 1 and _payload_string(changes[0], "field") == "create_assessment"
    return proposal.operation in {"create_assessment", "notion_create_assessment"}


def _payload_string(payload: Any, *keys: str) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    mapping = cast(Mapping[str, Any], payload)
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _inbound_material_ids_from_changes(
    changes: Sequence[Any],
    explicit_ids: Sequence[uuid.UUID],
) -> tuple[uuid.UUID, ...]:
    ids: list[uuid.UUID] = list(explicit_ids)
    for change in changes:
        raw_ids = _field(change, "inbound_material_ids")
        if raw_ids is None:
            continue
        if not isinstance(raw_ids, Sequence) or isinstance(raw_ids, (str, bytes)):
            raise ValueError("inbound material IDs must be a sequence")
        ids.extend(uuid.UUID(str(value)) for value in cast(Sequence[object], raw_ids))
    return tuple(dict.fromkeys(ids))


def _superseded_public_proposal_id_from_changes(changes: Sequence[Any]) -> uuid.UUID | None:
    superseded_ids = {
        uuid.UUID(str(value))
        for change in changes
        if (value := _field(change, "supersedes_proposal_id")) is not None
    }
    if len(superseded_ids) > 1:
        raise ValueError("only one superseded proposal can be replaced at a time")
    return next(iter(superseded_ids), None)


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
                    calendar_role=academic_calendar_role(course.title),
                )
                for course, _calendar in rows
            ]
        return tuple(options)

    def search_misc_courses(self) -> Sequence[Any]:
        """Return at most two valid reserved misc targets for uniqueness checks."""

        from app.agents.academic_planner.contracts import AcademicCourseOption

        with Session(self.engine) as session:
            rows = session.execute(
                select(Course, AcademicCourseCalendar)
                .join(AcademicCourseCalendar, AcademicCourseCalendar.course_id == Course.id)
                .where(
                    Course.active.is_(True),
                    func.lower(func.trim(Course.title)) == AcademicCalendarRole.MISC.value,
                    AcademicCourseCalendar.discovery_status == "valid",
                    AcademicCourseCalendar.child_data_source_id.is_not(None),
                    AcademicCourseCalendar.title_property_id.is_not(None),
                    AcademicCourseCalendar.date_property_id.is_not(None),
                )
                .order_by(Course.id)
                .limit(2)
            )
            return tuple(
                AcademicCourseOption(
                    course_id=str(course.id),
                    course_code=course.course_code,
                    title=course.title,
                    calendar_role=AcademicCalendarRole.MISC,
                )
                for course, _calendar in rows
            )

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

        if limit < 1:
            return ()
        if self.embedding_gateway is None:
            raise AcademicSemanticUnavailableError
        result = await self.embedding_gateway.embed_reflection_text(query)
        if result.status is not EmbeddingStatus.VALID or result.embedding is None:
            raise AcademicSemanticUnavailableError
        vector = result.embedding.vector
        embedding_model = _bounded(result.model_identity, 255)
        with Session(self.engine) as session:
            if session.get_bind().dialect.name == "postgresql":
                if len(vector) != ACADEMIC_MATERIAL_EMBEDDING_DIMENSIONS:
                    raise AcademicSemanticUnavailableError
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
                        AcademicReflectionMemory.embedding_model == embedding_model,
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
                    AcademicReflectionMemory.embedding_model == embedding_model,
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

    def list_material_chunks(
        self,
        assessment_id: str,
        *,
        limit: int,
    ) -> Sequence[Mapping[str, Any]]:
        with Session(self.engine) as session:
            scope = _resolve_assessment_scope(session, assessment_id)
            if scope is None:
                return []
            internal_assessment_id, public_assessment_id = scope
            return AcademicRepository.list_material_planning_chunks(
                session,
                assessment_id=internal_assessment_id,
                public_assessment_id=public_assessment_id,
                limit=limit,
            )

    def read_material_chunks(
        self,
        chunk_ids: Sequence[str],
    ) -> Sequence[Mapping[str, Any]]:
        with Session(self.engine) as session:
            return AcademicRepository.read_material_planning_chunks(session, chunk_ids)

    def get_active_profile(self, assessment_id: str) -> Any | None:
        with Session(self.engine) as session:
            scope = _resolve_assessment_scope(session, assessment_id)
            if scope is None:
                return None
            internal_assessment_id, public_assessment_id = scope
            row = AcademicRepository.get_active_material_planning_profile(
                session,
                assessment_id=internal_assessment_id,
            )
            if row is None:
                return None
            return _material_planning_profile_from_row(
                row,
                public_assessment_id=public_assessment_id,
            )

    def save_profile(self, profile: Any) -> None:
        with Session(self.engine) as session, session.begin():
            AcademicRepository.save_material_planning_profile(session, profile=profile)

    def activate_profile(self, profile: Any) -> None:
        with Session(self.engine) as session, session.begin():
            AcademicRepository.activate_material_planning_profile(session, profile=profile)

    async def search_semantic_assessment_materials(
        self,
        assessment_id: uuid.UUID | str,
        query: str,
        *,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Embed a query and search only active chunks owned by one assessment."""

        from app.llm.embeddings import EmbeddingStatus

        if limit < 1:
            return []
        if self.embedding_gateway is None:
            raise AcademicSemanticUnavailableError
        result = await self.embedding_gateway.embed_academic_text(query)
        if result.status is not EmbeddingStatus.VALID or result.embedding is None:
            raise AcademicSemanticUnavailableError
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

    def load_calendar_availability(self, *, now: datetime, horizon_days: int) -> Any:
        from app.agents.academic_planner.contracts import CalendarAvailabilityFacts
        from app.agents.academic_planner.contracts import (
            FixedCommitment as PlannerCommitment,
        )

        current = _utc(now, "now")
        horizon = current + timedelta(days=horizon_days)
        with Session(self.engine) as session:
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
            commitments.extend(
                PlannerCommitment(
                    id=row.notion_id,
                    title=row.title,
                    start_at=_aware_db(cast(datetime, row.due_at)),
                    end_at=_aware_db(cast(datetime, row.ends_at)),
                    kind="event",
                )
                for row in session.scalars(
                    select(Assessment)
                    .where(
                        Assessment.due_at < horizon,
                        Assessment.ends_at > current,
                        Assessment.fact_state == "confirmed",
                        Assessment.active.is_(True),
                        Assessment.archived.is_(False),
                    )
                    .order_by(Assessment.due_at)
                )
            )
            commitments.sort(key=lambda item: item.start_at)
            preference = session.scalar(
                select(PlanningPreference).order_by(PlanningPreference.updated_at.desc())
            )
            availability = _availability_windows(preference.availability if preference else {})
            buffer_minutes = preference.buffer_minutes if preference else 15
        return CalendarAvailabilityFacts(
            commitments=tuple(commitments),
            availability=tuple(availability),
            buffer_minutes=buffer_minutes,
            horizon_days=horizon_days,
        )

    def load_upcoming_calendar_items(
        self,
        *,
        occurrence: date | datetime,
        timezone: str = "America/Toronto",
    ) -> tuple[Mapping[str, Any], ...]:
        tz = ZoneInfo(timezone)
        window_start, window_end = _calendar_window(occurrence, tz)
        query_start = window_start.astimezone(UTC) - timedelta(days=1)
        query_end = window_end.astimezone(UTC) + timedelta(days=1)
        with Session(self.engine) as session:
            rows = list(
                session.execute(
                    select(Assessment, Course)
                    .join(Course, Assessment.course_id == Course.id)
                    .where(
                        Course.active.is_(True),
                        Assessment.active.is_(True),
                        Assessment.archived.is_(False),
                        Assessment.due_at.is_not(None),
                        Assessment.due_at >= query_start,
                        Assessment.due_at <= query_end,
                    )
                )
            )
            items: list[tuple[datetime, str, str, str, Mapping[str, Any]]] = []
            for row, course in rows:
                due_at = _aware_db(cast(datetime, row.due_at))
                local_start = _calendar_local_start(
                    due_at,
                    is_all_day=row.is_all_day,
                    timezone=tz,
                )
                if not window_start <= local_start <= window_end:
                    continue
                item = _assessment_calendar_item(
                    row,
                    course,
                    local_start=local_start,
                    window_start=window_start,
                    timezone=tz,
                )
                items.append(
                    (
                        local_start,
                        "course",
                        course.course_code.casefold(),
                        row.title.casefold(),
                        item,
                    )
                )
            items.sort(key=lambda item: (item[0], item[1], item[2], item[3], item[4]["event_id"]))
            return tuple(item[-1] for item in items)

    def save_assessment_calendar_semantics(
        self,
        notion_id: str,
        semantics: CalendarSemanticResultInput,
    ) -> bool:
        with Session(self.engine) as session, session.begin():
            return AcademicRepository.save_assessment_calendar_semantics(
                session,
                notion_id=notion_id,
                semantics=semantics,
            )

    def save_checkin_proposal(self, proposal: Any) -> None:
        with Session(self.engine) as session, session.begin():
            now = datetime.now(UTC)
            checkin = AcademicRepository.create_checkin(
                session,
                idempotency_key=f"academic-checkin:{proposal.proposal_id}",
                external_event_id=f"proposal:{proposal.proposal_id}",
                channel="discord",
                received_at=now,
                redacted_summary="Academic check-in proposal pending confirmation.",
                status="proposal_pending",
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
        owner_discord_user_id: str | None = None,
        inbound_material_ids: Sequence[uuid.UUID] = (),
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
            material_ids = _inbound_material_ids_from_changes(changes, inbound_material_ids)
            supersedes_public_id = _superseded_public_proposal_id_from_changes(changes)
            proposal_owner = (
                _discord_id(owner_discord_user_id, "owner_discord_user_id")
                if owner_discord_user_id is not None
                else None
            )
            proposal_channel = None
            if proposal_owner is not None or material_ids:
                proposal_channel = _discord_id(channel, "discord_channel_id")
            if material_ids and proposal_owner is None:
                raise ValueError("owner_discord_user_id is required for inbound material binding")
            superseded_row_id: uuid.UUID | None = None
            if supersedes_public_id is not None:
                if not material_ids:
                    raise ValueError("superseded create replacement requires inbound materials")
                if proposal_owner is None or proposal_channel is None:
                    raise ValueError("owner/channel scope is required for proposal supersession")
                status, pending_create = (
                    AcademicInboundMaterialRepository.find_single_pending_create_proposal(
                        session,
                        owner_discord_user_id=proposal_owner,
                        discord_channel_id=proposal_channel,
                    )
                )
                requested_old = _proposal_row_for_public_id(session, supersedes_public_id)
                if status != "found" or pending_create is None or requested_old is None:
                    raise ValueError("no single compatible pending create proposal to supersede")
                if pending_create.id != requested_old.id:
                    raise ValueError(
                        "superseded proposal is not the single compatible pending create"
                    )
                superseded_row_id = pending_create.id
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
                    owner_discord_user_id=proposal_owner,
                    discord_channel_id=proposal_channel,
                )
                if material_ids:
                    material_owner = proposal_owner
                    material_channel = proposal_channel
                    if material_owner is None or material_channel is None:
                        raise ValueError(
                            "owner/channel scope is required for inbound material binding"
                        )
                    if superseded_row_id is not None:
                        AcademicInboundMaterialRepository.supersede_pending_create_with_materials(
                            session,
                            old_proposal_id=superseded_row_id,
                            new_proposal_id=proposal_row.id,
                            material_ids=material_ids,
                            owner_discord_user_id=material_owner,
                            discord_channel_id=material_channel,
                        )
                    else:
                        AcademicInboundMaterialRepository.validate_for_proposal(
                            session,
                            material_ids,
                            owner_discord_user_id=material_owner,
                            discord_channel_id=material_channel,
                            proposal_id=proposal_row.id,
                        )
                        for material_id in material_ids:
                            AcademicInboundMaterialRepository.bind_to_proposal(
                                session,
                                material_id,
                                owner_discord_user_id=material_owner,
                                discord_channel_id=material_channel,
                                proposal_id=proposal_row.id,
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
                is_all_day=bool(_field(assessment, "is_all_day") or False),
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

    def create_or_replay_inbound_material(
        self, material: AcademicInboundMaterialInput
    ) -> tuple[AcademicInboundMaterialCreateStatus, AcademicInboundMaterialSnapshot]:
        with Session(self.engine) as session, session.begin():
            result = AcademicInboundMaterialRepository.create_or_replay(session, material)
            return result.status, _inbound_material_snapshot(result.row)

    def validate_for_proposal(
        self,
        ids: Sequence[uuid.UUID],
        owner_discord_user_id: str,
        discord_channel_id: str,
        now: datetime | None = None,
    ) -> tuple[uuid.UUID, ...]:
        with Session(self.engine) as session, session.begin():
            return AcademicInboundMaterialRepository.validate_for_proposal(
                session,
                ids,
                owner_discord_user_id=owner_discord_user_id,
                discord_channel_id=discord_channel_id,
                now=now,
            )

    def get_inbound_material(
        self,
        material_id: uuid.UUID,
        *,
        owner_discord_user_id: str | None = None,
        discord_channel_id: str | None = None,
    ) -> AcademicInboundMaterialSnapshot | None:
        with Session(self.engine) as session:
            if owner_discord_user_id is not None and discord_channel_id is not None:
                return AcademicInboundMaterialRepository.get_owned_snapshot(
                    session,
                    material_id,
                    owner_discord_user_id=owner_discord_user_id,
                    discord_channel_id=discord_channel_id,
                    include_expired=True,
                )
            row = session.get(AcademicInboundMaterial, material_id)
            return _inbound_material_snapshot(row) if row is not None else None

    def load_inbound_material_snapshot(
        self,
        *,
        material_id: uuid.UUID,
        proposal_id: uuid.UUID,
    ) -> AcademicInboundMaterialSnapshot | None:
        with Session(self.engine) as session:
            proposal_row = _proposal_row_for_public_id(session, proposal_id)
            if proposal_row is None:
                return None
            row = session.get(AcademicInboundMaterial, material_id)
            if row is None or row.proposal_id != proposal_row.id:
                return None
            return _inbound_material_snapshot(row)

    def mark_inbound_material_proposal_pending(
        self,
        material_id: uuid.UUID,
        *,
        owner_discord_user_id: str,
        discord_channel_id: str,
        proposal_id: uuid.UUID,
        assessment_id: uuid.UUID | None = None,
        now: datetime | None = None,
    ) -> AcademicInboundMaterialSnapshot:
        with Session(self.engine) as session, session.begin():
            proposal_row = _proposal_row_for_id(session, proposal_id)
            if proposal_row is None:
                raise NoResultFound(f"academic proposal {proposal_id} was not found")
            resolved_assessment_id = _resolve_material_assessment_id(session, assessment_id)
            row = AcademicInboundMaterialRepository.bind_to_proposal(
                session,
                material_id,
                owner_discord_user_id=owner_discord_user_id,
                discord_channel_id=discord_channel_id,
                proposal_id=proposal_row.id,
                assessment_id=resolved_assessment_id,
                now=now,
            )
            return _inbound_material_snapshot(row)

    def mark_inbound_material_seeding(
        self,
        material_id: uuid.UUID,
        *,
        owner_discord_user_id: str | None = None,
        discord_channel_id: str | None = None,
        proposal_id: uuid.UUID | None = None,
        assessment_id: uuid.UUID | str | None = None,
        notion_upload_id: str | None = None,
        now: datetime | None = None,
    ) -> AcademicInboundMaterialSnapshot:
        with Session(self.engine) as session, session.begin():
            if proposal_id is not None:
                row = _proposal_bound_material(session, material_id, proposal_id)
                resolved_assessment_id = _resolve_material_assessment_id(session, assessment_id)
                if resolved_assessment_id is not None:
                    row.assessment_id = resolved_assessment_id
                row.notion_upload_id = _bounded_optional(notion_upload_id, 255)
                _advance_inbound_material_state(row, "seeding")
                session.flush()
            else:
                if owner_discord_user_id is None or discord_channel_id is None:
                    raise ValueError("owner/channel or proposal_id is required")
                row = AcademicInboundMaterialRepository.advance_state(
                    session,
                    material_id,
                    owner_discord_user_id=owner_discord_user_id,
                    discord_channel_id=discord_channel_id,
                    state="seeding",
                    now=now,
                )
                row.notion_upload_id = _bounded_optional(notion_upload_id, 255)
            return _inbound_material_snapshot(row)

    def mark_inbound_material_seeded(
        self,
        material_id: uuid.UUID,
        *,
        owner_discord_user_id: str | None = None,
        discord_channel_id: str | None = None,
        notion_page_id: str,
        notion_block_id: str | None = None,
        notion_upload_id: str | None = None,
        proposal_id: uuid.UUID | None = None,
        assessment_id: uuid.UUID | str | None = None,
    ) -> AcademicInboundMaterialSnapshot:
        with Session(self.engine) as session, session.begin():
            proposal_row_id = None
            if proposal_id is not None:
                proposal_row = _proposal_row_for_id(session, proposal_id)
                if proposal_row is None:
                    raise NoResultFound(f"academic proposal {proposal_id} was not found")
                proposal_row_id = proposal_row.id
            resolved_assessment_id = _resolve_material_assessment_id(session, assessment_id)
            if proposal_row_id is not None and (
                owner_discord_user_id is None or discord_channel_id is None
            ):
                row = _proposal_bound_material(session, material_id, proposal_row_id)
                if resolved_assessment_id is not None and row.assessment_id not in (
                    None,
                    resolved_assessment_id,
                ):
                    raise ValueError("seeded receipt assessment does not match bound material")
                row.assessment_id = resolved_assessment_id or row.assessment_id
                row.notion_page_id = _bounded(notion_page_id, 255)
                row.notion_block_id = _bounded_optional(notion_block_id, 255)
                row.notion_upload_id = _bounded_optional(notion_upload_id, 255)
                row.error_code = None
                _advance_inbound_material_state(row, "seeded")
                session.flush()
            else:
                if owner_discord_user_id is None or discord_channel_id is None:
                    raise ValueError("owner/channel or proposal_id is required")
                row = AcademicInboundMaterialRepository.mark_seeded(
                    session,
                    material_id,
                    owner_discord_user_id=owner_discord_user_id,
                    discord_channel_id=discord_channel_id,
                    notion_page_id=notion_page_id,
                    notion_block_id=notion_block_id,
                    notion_upload_id=notion_upload_id,
                    proposal_id=proposal_row_id,
                    assessment_id=resolved_assessment_id,
                )
            return _inbound_material_snapshot(row)

    def mark_inbound_material_uncertain(
        self,
        material_id: uuid.UUID,
        *,
        owner_discord_user_id: str | None = None,
        discord_channel_id: str | None = None,
        proposal_id: uuid.UUID | None = None,
        assessment_id: uuid.UUID | str | None = None,
        error_code: str,
        now: datetime | None = None,
    ) -> AcademicInboundMaterialSnapshot:
        with Session(self.engine) as session, session.begin():
            if proposal_id is not None:
                row = _proposal_bound_material(session, material_id, proposal_id)
                resolved_assessment_id = _resolve_material_assessment_id(session, assessment_id)
                if resolved_assessment_id is not None:
                    row.assessment_id = resolved_assessment_id
                _advance_inbound_material_state(row, "uncertain")
                row.error_code = _bounded_optional(error_code, 128)
                session.flush()
            else:
                if owner_discord_user_id is None or discord_channel_id is None:
                    raise ValueError("owner/channel or proposal_id is required")
                row = AcademicInboundMaterialRepository.advance_state(
                    session,
                    material_id,
                    owner_discord_user_id=owner_discord_user_id,
                    discord_channel_id=discord_channel_id,
                    state="uncertain",
                    error_code=error_code,
                    now=now,
                )
            return _inbound_material_snapshot(row)

    def mark_inbound_material_failed(
        self,
        material_id: uuid.UUID,
        *,
        owner_discord_user_id: str | None = None,
        discord_channel_id: str | None = None,
        proposal_id: uuid.UUID | None = None,
        assessment_id: uuid.UUID | str | None = None,
        error_code: str,
        now: datetime | None = None,
    ) -> AcademicInboundMaterialSnapshot:
        with Session(self.engine) as session, session.begin():
            if proposal_id is not None:
                row = _proposal_bound_material(session, material_id, proposal_id)
                resolved_assessment_id = _resolve_material_assessment_id(session, assessment_id)
                if resolved_assessment_id is not None:
                    row.assessment_id = resolved_assessment_id
                _advance_inbound_material_state(row, "failed")
                row.error_code = _bounded_optional(error_code, 128)
                session.flush()
            else:
                if owner_discord_user_id is None or discord_channel_id is None:
                    raise ValueError("owner/channel or proposal_id is required")
                row = AcademicInboundMaterialRepository.advance_state(
                    session,
                    material_id,
                    owner_discord_user_id=owner_discord_user_id,
                    discord_channel_id=discord_channel_id,
                    state="failed",
                    error_code=error_code,
                    now=now,
                )
            return _inbound_material_snapshot(row)

    def find_single_pending_create_proposal(
        self,
        *,
        owner_discord_user_id: str,
        discord_channel_id: str,
        now: datetime | None = None,
    ) -> tuple[Literal["found", "not_found", "ambiguous"], uuid.UUID | None]:
        with Session(self.engine) as session:
            status, row = AcademicInboundMaterialRepository.find_single_pending_create_proposal(
                session,
                owner_discord_user_id=owner_discord_user_id,
                discord_channel_id=discord_channel_id,
                now=now,
            )
            return status, row.id if row is not None else None

    def supersede_pending_create_with_materials(
        self,
        *,
        old_proposal_id: uuid.UUID,
        new_proposal_id: uuid.UUID,
        material_ids: Sequence[uuid.UUID],
        owner_discord_user_id: str,
        discord_channel_id: str,
        superseded_reason: str = "replacement_with_inbound_material",
        now: datetime | None = None,
    ) -> None:
        with Session(self.engine) as session, session.begin():
            AcademicInboundMaterialRepository.supersede_pending_create_with_materials(
                session,
                old_proposal_id=old_proposal_id,
                new_proposal_id=new_proposal_id,
                material_ids=material_ids,
                owner_discord_user_id=owner_discord_user_id,
                discord_channel_id=discord_channel_id,
                superseded_reason=superseded_reason,
                now=now,
            )

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
                event_preview_title=(
                    str(kwargs["event_preview_title"])
                    if kwargs.get("event_preview_title") is not None
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

    def abort_owner_channel_continuations(
        self,
        *,
        discord_channel_id: str,
        discord_user_id: str,
        abort_event_id: str,
        aborted_at: datetime | None = None,
    ) -> dict[str, int]:
        with Session(self.engine) as session, session.begin():
            return AcademicRepository.abort_owner_channel_continuations(
                session,
                discord_channel_id=discord_channel_id,
                discord_user_id=discord_user_id,
                abort_event_id=abort_event_id,
                aborted_at=aborted_at or datetime.now(UTC),
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
    intent_value = _bounded_optional(semantics.intent_value, 128)
    if intent_value is not None and intent_value not in {"study", "regular"}:
        raise ValueError("invalid calendar semantic intent value")
    intent_status = semantics.intent_status
    intent_rationale = _bounded_optional(semantics.intent_rationale, 500)
    intent_evidence_ids = _bounded_semantic_ids(semantics.intent_evidence_ids)
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
    if intent_status is not None and intent_status not in {"valid", "unavailable", "invalid"}:
        raise ValueError("invalid calendar semantic intent status")
    if intent_status in {"unavailable", "invalid"}:
        intent_value = None
        intent_rationale = None
        intent_evidence_ids = []
    row.calendar_semantic_overview = overview
    row.calendar_semantic_description = description
    row.calendar_semantic_status = semantics.status
    row.calendar_semantic_intent_value = intent_value
    row.calendar_semantic_intent_status = intent_status
    row.calendar_semantic_intent_rationale = intent_rationale
    row.calendar_semantic_intent_evidence_ids = intent_evidence_ids
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


def _calendar_local_start(value: datetime, *, is_all_day: bool, timezone: ZoneInfo) -> datetime:
    local = _aware_db(value).astimezone(timezone)
    if is_all_day:
        return datetime.combine(local.date(), datetime.min.time(), tzinfo=timezone)
    return local


def _assessment_calendar_item(
    row: Assessment,
    course: Course,
    *,
    local_start: datetime,
    window_start: datetime,
    timezone: ZoneInfo,
) -> Mapping[str, Any]:
    due_at = _aware_db(cast(datetime, row.due_at))
    local_due = due_at.astimezone(timezone)
    local_end = _aware_db(row.ends_at).astimezone(timezone) if row.ends_at is not None else None
    semantic_status = row.calendar_semantic_status or "unavailable"
    overview = (
        row.calendar_semantic_overview if semantic_status in {"valid", "not_substantive"} else None
    )
    description = row.calendar_semantic_description if semantic_status == "valid" else None
    return {
        "event_id": row.notion_id,
        "source_area": academic_calendar_role(course.title).value,
        "source_label": course.course_code,
        "title": row.title,
        "display_kind": _display_kind(row.assessment_type),
        "local_start_label": _calendar_label(local_due, is_all_day=row.is_all_day),
        "local_end_label": (
            _calendar_label(local_end, is_all_day=False) if local_end is not None else None
        ),
        "relative_date_label": _relative_day_label(local_start.date(), window_start.date()),
        "is_all_day": row.is_all_day,
        "completed": row.completed,
        "semantic_status": semantic_status,
        "semantic_intent_value": row.calendar_semantic_intent_value,
        "semantic_intent_status": row.calendar_semantic_intent_status,
        "semantic_intent_rationale": row.calendar_semantic_intent_rationale,
        "semantic_intent_evidence_fragment_ids": tuple(
            row.calendar_semantic_intent_evidence_ids or ()
        ),
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
    }


def _calendar_semantic_cache(row: Any) -> Mapping[str, Any]:
    return {
        "source_fingerprint": row.calendar_semantic_source_fingerprint,
        "source_last_edited_at": row.calendar_semantic_source_last_edited_at,
        "model_identity": row.calendar_semantic_model_identity,
        "config_version": row.calendar_semantic_config_version,
        "prompt_version": row.calendar_semantic_prompt_version,
        "analyzed_at": row.calendar_semantic_analyzed_at,
        "intent_value": row.calendar_semantic_intent_value,
        "intent_status": row.calendar_semantic_intent_status,
        "intent_rationale": row.calendar_semantic_intent_rationale,
        "intent_evidence_ids": tuple(row.calendar_semantic_intent_evidence_ids or ()),
    }


def _display_kind(value: str) -> str:
    return " ".join(part.capitalize() for part in value.replace("-", "_").split("_")) or "Event"


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


def _proposal_row_for_public_id(
    session: Session,
    proposal_id: uuid.UUID,
) -> AcademicProposedChange | None:
    return session.scalar(
        select(AcademicProposedChange).where(
            AcademicProposedChange.idempotency_key == f"academic-proposal:{proposal_id}"
        )
    )


def _proposal_row_for_id(
    session: Session,
    proposal_id: uuid.UUID,
) -> AcademicProposedChange | None:
    return _proposal_row_for_public_id(session, proposal_id) or session.get(
        AcademicProposedChange,
        proposal_id,
    )


def _proposal_bound_material(
    session: Session,
    material_id: uuid.UUID,
    proposal_id: uuid.UUID,
) -> AcademicInboundMaterial:
    proposal_row = _proposal_row_for_id(session, proposal_id)
    if proposal_row is None:
        raise NoResultFound(f"academic proposal {proposal_id} was not found")
    row = session.scalar(
        select(AcademicInboundMaterial)
        .where(
            AcademicInboundMaterial.id == material_id,
            AcademicInboundMaterial.proposal_id == proposal_row.id,
        )
        .with_for_update()
    )
    if row is None:
        raise NoResultFound(f"academic inbound material {material_id} was not found")
    return row


def _resolve_material_assessment_id(
    session: Session,
    assessment_id: uuid.UUID | str | None,
) -> uuid.UUID | None:
    if assessment_id is None:
        return None
    if isinstance(assessment_id, uuid.UUID):
        return assessment_id
    scope = _resolve_assessment_scope(session, assessment_id)
    if scope is not None:
        return scope[0]
    parsed = _parse_uuid(assessment_id)
    if parsed is not None:
        return parsed
    raise NoResultFound(f"academic assessment {assessment_id} was not found")


def _aware_db(value: datetime) -> datetime:
    """Normalize SQLite's naive timestamp reads as UTC for planner contracts."""

    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


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


def _discord_id(value: str, field: str) -> str:
    normalized = value.strip()
    if not _DISCORD_ID.fullmatch(normalized):
        raise ValueError(f"{field} must be a Discord snowflake")
    return normalized


def _safe_pdf_filename(value: str) -> str:
    normalized = value.strip()
    if not _SAFE_PDF_FILENAME.fullmatch(normalized):
        raise ValueError("inbound PDF filename is not safe")
    if not normalized.lower().endswith(".pdf"):
        raise ValueError("inbound PDF filename must end with .pdf")
    return normalized


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
        "event": row.event_preview_title,
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
        "event_preview_title": row.event_preview_title,
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
    "AcademicSemanticUnavailableError",
    "AssessmentSourceTrace",
    "ClarificationInput",
    "CourseCalendarInput",
    "DocumentChunkInput",
    "FactState",
    "SQLAlchemyAcademicPlannerStore",
    "SourceCitation",
]
