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

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.exc import IntegrityError, NoResultFound
from sqlalchemy.orm import Session

from app.db.models import (
    AcademicCheckIn,
    AcademicDocument,
    AcademicDocumentChunk,
    AcademicProposedChange,
    AcademicSyncCursor,
    Assessment,
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
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


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
        return _upsert(session, Assessment, [Assessment.notion_id == notion_id], values)

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
                source_version="notion-v1",
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
            incomplete = [
                IncompleteBlock(
                    id=str(row.id),
                    assessment_id=str(row.assessment_id),
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
        local_day = _aware_db(plan.created_at).date()
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
            plan = session.scalar(select(StudyPlan).order_by(StudyPlan.created_at.desc()).limit(1))
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
            for row in rows:
                assessment_id = str(row.assessment_id) if row.assessment_id else row.block_key
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


__all__ = ["AcademicRepository", "DocumentChunkInput", "FactState", "SourceCitation"]
