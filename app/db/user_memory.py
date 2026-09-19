"""Owner-scoped persistence for generic durable user memory."""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, cast

from sqlalchemy import Engine, select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from app.agents.memory.contracts import (
    UserMemoryCreate,
    UserMemoryOwnerScope,
    UserMemoryRecord,
)
from app.agents.memory.contracts import (
    UserMemoryKind as AgentUserMemoryKind,
)
from app.agents.memory.contracts import (
    UserMemorySensitivity as AgentUserMemorySensitivity,
)
from app.agents.memory.contracts import (
    UserMemoryStatus as AgentUserMemoryStatus,
)
from app.artifacts.store import ArtifactStore
from app.db.models import UserMemoryEvent, UserMemoryFact

UserMemoryKind = Literal[
    "preference",
    "profile",
    "standing_instruction",
    "constraint",
    "personal_fact",
]
UserMemoryStatus = Literal["active", "pending_confirmation", "superseded", "deleted"]
UserMemoryEventType = Literal[
    "create",
    "confirm",
    "correct",
    "supersede",
    "delete",
    "retrieval_feedback",
]
UserMemorySensitivity = Literal["low", "medium", "high"]

USER_MEMORY_EMBEDDING_DIMENSIONS = 1024

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_KINDS = frozenset(("preference", "profile", "standing_instruction", "constraint", "personal_fact"))
_STATUSES = frozenset(("active", "pending_confirmation", "superseded", "deleted"))
_EVENT_TYPES = frozenset(
    ("create", "confirm", "correct", "supersede", "delete", "retrieval_feedback")
)
_SENSITIVITIES = frozenset(("low", "medium", "high"))


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _bounded(value: str, field: str, max_length: int) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} must not be empty")
    if len(cleaned) > max_length:
        raise ValueError(f"{field} must be at most {max_length} characters")
    return cleaned


def _optional_bounded(value: str | None, field: str, max_length: int) -> str | None:
    if value is None:
        return None
    return _bounded(value, field, max_length)


def _artifact_key(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    cleaned = _bounded(value, field, 64)
    if not _SHA256.fullmatch(cleaned):
        raise ValueError(f"{field} must be a SHA-256 artifact key")
    return cleaned


def _owner(value: str, field: str) -> str:
    return _bounded(value, field, 32)


def _kind(value: UserMemoryKind) -> str:
    if value not in _KINDS:
        raise ValueError("user memory kind is invalid")
    return value


def _status(value: UserMemoryStatus) -> str:
    if value not in _STATUSES:
        raise ValueError("user memory status is invalid")
    return value


def _event_type(value: UserMemoryEventType) -> str:
    if value not in _EVENT_TYPES:
        raise ValueError("user memory event type is invalid")
    return value


def _sensitivity(value: UserMemorySensitivity) -> str:
    if value not in _SENSITIVITIES:
        raise ValueError("user memory sensitivity is invalid")
    return value


def _confidence(value: float | None) -> float | None:
    if value is None:
        return None
    if not 0.0 <= value <= 1.0:
        raise ValueError("confidence must be between 0.0 and 1.0")
    return value


def _embedding_vector(value: Sequence[float] | None) -> list[float] | None:
    if value is None:
        return None
    vector = [float(item) for item in value]
    if not vector:
        raise ValueError("embedding must not be empty")
    if not all(math.isfinite(item) for item in vector):
        raise ValueError("embedding values must be finite")
    return vector


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        return -1.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return -1.0
    return dot / (left_norm * right_norm)


def _payload(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(value or {})


class UserMemoryRepository:
    """Low-level generic memory repository; callers own transactions."""

    @staticmethod
    def create_memory(
        session: Session,
        *,
        owner_user_id: str,
        owner_channel_id: str,
        external_event_id: str,
        kind: UserMemoryKind,
        status: Literal["active", "pending_confirmation"],
        content_artifact_key: str,
        redacted_preview: str,
        normalized_subject: str,
        occurred_at: datetime,
        sensitivity: UserMemorySensitivity = "medium",
        confidence: float | None = None,
        source_conversation_id: uuid.UUID | None = None,
        source_external_event_id: str | None = None,
        evidence_artifact_key: str | None = None,
        embedding: Sequence[float] | None = None,
        embedding_model: str | None = None,
        actor: str | None = "owner",
        event_payload: Mapping[str, Any] | None = None,
    ) -> tuple[UserMemoryFact, bool]:
        owner = _owner(owner_user_id, "owner_user_id")
        channel = _owner(owner_channel_id, "owner_channel_id")
        event_id = _bounded(external_event_id, "external_event_id", 255)
        existing = UserMemoryRepository._memory_for_event(
            session,
            owner_user_id=owner,
            owner_channel_id=channel,
            external_event_id=event_id,
        )
        if existing is not None:
            return existing, False

        vector = _embedding_vector(embedding)
        if vector is not None and embedding_model is None:
            raise ValueError("embedding_model is required when an embedding is stored")
        if (
            session.get_bind().dialect.name == "postgresql"
            and vector is not None
            and len(vector) != USER_MEMORY_EMBEDDING_DIMENSIONS
        ):
            raise ValueError("user memory embeddings must be 1024-dimensional on PostgreSQL")
        row = UserMemoryFact(
            owner_user_id=owner,
            owner_channel_id=channel,
            kind=_kind(kind),
            status=_status(status),
            content_artifact_key=_artifact_key(content_artifact_key, "content_artifact_key"),
            redacted_preview=_bounded(redacted_preview, "redacted_preview", 2_000),
            normalized_subject=_bounded(normalized_subject, "normalized_subject", 255),
            confidence=_confidence(confidence),
            sensitivity=_sensitivity(sensitivity),
            source_conversation_id=source_conversation_id,
            source_external_event_id=_optional_bounded(
                source_external_event_id,
                "source_external_event_id",
                255,
            ),
            evidence_artifact_key=_artifact_key(evidence_artifact_key, "evidence_artifact_key"),
            embedding=vector,
            embedding_model=_optional_bounded(embedding_model, "embedding_model", 255),
            embedding_dimensions=len(vector) if vector is not None else None,
            revision=1,
        )
        session.add(row)
        session.flush()
        UserMemoryRepository._append_event(
            session,
            memory=row,
            external_event_id=event_id,
            event_type="create",
            occurred_at=occurred_at,
            actor=actor,
            payload=event_payload,
        )
        return row, True

    @staticmethod
    def get_memory(
        session: Session,
        *,
        owner_user_id: str,
        owner_channel_id: str,
        memory_id: uuid.UUID,
        for_update: bool = False,
    ) -> UserMemoryFact:
        statement = select(UserMemoryFact).where(
            UserMemoryFact.id == memory_id,
            UserMemoryFact.owner_user_id == _owner(owner_user_id, "owner_user_id"),
            UserMemoryFact.owner_channel_id == _owner(owner_channel_id, "owner_channel_id"),
        )
        if for_update:
            statement = statement.with_for_update()
        row = session.scalar(statement)
        if row is None:
            raise NoResultFound(f"user memory {memory_id} was not found for owner/channel")
        return row

    @staticmethod
    def list_active_memories(
        session: Session,
        *,
        owner_user_id: str,
        owner_channel_id: str,
        limit: int,
        kind: UserMemoryKind | None = None,
        normalized_subject: str | None = None,
    ) -> list[UserMemoryFact]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        statement = (
            select(UserMemoryFact)
            .where(
                UserMemoryFact.owner_user_id == _owner(owner_user_id, "owner_user_id"),
                UserMemoryFact.owner_channel_id == _owner(owner_channel_id, "owner_channel_id"),
                UserMemoryFact.status == "active",
            )
            .order_by(UserMemoryFact.updated_at.desc(), UserMemoryFact.created_at.desc())
            .limit(limit)
        )
        if kind is not None:
            statement = statement.where(UserMemoryFact.kind == _kind(kind))
        if normalized_subject is not None:
            statement = statement.where(
                UserMemoryFact.normalized_subject
                == _bounded(normalized_subject, "normalized_subject", 255)
            )
        return list(session.scalars(statement))

    @staticmethod
    def search_active_memories(
        session: Session,
        *,
        owner_user_id: str,
        owner_channel_id: str,
        limit: int,
        query_embedding: Sequence[float] | None = None,
        embedding_model: str | None = None,
        kind: UserMemoryKind | None = None,
    ) -> list[UserMemoryFact]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        vector = _embedding_vector(query_embedding)
        if vector is None:
            return UserMemoryRepository.list_active_memories(
                session,
                owner_user_id=owner_user_id,
                owner_channel_id=owner_channel_id,
                limit=limit,
                kind=kind,
            )
        if embedding_model is None:
            raise ValueError("embedding_model is required for semantic retrieval")
        model = _bounded(embedding_model, "embedding_model", 255)
        base_filters = (
            UserMemoryFact.owner_user_id == _owner(owner_user_id, "owner_user_id"),
            UserMemoryFact.owner_channel_id == _owner(owner_channel_id, "owner_channel_id"),
            UserMemoryFact.status == "active",
            UserMemoryFact.embedding.is_not(None),
            UserMemoryFact.embedding_model == model,
            UserMemoryFact.embedding_dimensions == len(vector),
        )
        if session.get_bind().dialect.name == "postgresql":
            distance = UserMemoryFact.embedding.cosine_distance(vector).label("distance")
            statement = select(UserMemoryFact).where(*base_filters).order_by(distance).limit(limit)
            if kind is not None:
                statement = statement.where(UserMemoryFact.kind == _kind(kind))
            return list(session.scalars(statement))

        statement = select(UserMemoryFact).where(*base_filters)
        if kind is not None:
            statement = statement.where(UserMemoryFact.kind == _kind(kind))
        rows = list(session.scalars(statement))
        rows.sort(
            key=lambda memory: _cosine_similarity(vector, memory.embedding or []),
            reverse=True,
        )
        return rows[:limit]

    @staticmethod
    def confirm_memory(
        session: Session,
        *,
        owner_user_id: str,
        owner_channel_id: str,
        memory_id: uuid.UUID,
        external_event_id: str,
        occurred_at: datetime,
        actor: str | None = "owner",
        event_payload: Mapping[str, Any] | None = None,
    ) -> UserMemoryFact:
        existing = UserMemoryRepository._memory_for_event(
            session,
            owner_user_id=_owner(owner_user_id, "owner_user_id"),
            owner_channel_id=_owner(owner_channel_id, "owner_channel_id"),
            external_event_id=_bounded(external_event_id, "external_event_id", 255),
        )
        if existing is not None:
            return existing
        row = UserMemoryRepository.get_memory(
            session,
            owner_user_id=owner_user_id,
            owner_channel_id=owner_channel_id,
            memory_id=memory_id,
            for_update=True,
        )
        if row.status == "pending_confirmation":
            row.status = "active"
            row.revision += 1
        UserMemoryRepository._append_event(
            session,
            memory=row,
            external_event_id=external_event_id,
            event_type="confirm",
            occurred_at=occurred_at,
            actor=actor,
            payload=event_payload,
        )
        session.flush()
        return row

    @staticmethod
    def correct_memory(
        session: Session,
        *,
        owner_user_id: str,
        owner_channel_id: str,
        memory_id: uuid.UUID,
        external_event_id: str,
        content_artifact_key: str,
        redacted_preview: str,
        normalized_subject: str,
        occurred_at: datetime,
        evidence_artifact_key: str | None = None,
        embedding: Sequence[float] | None = None,
        embedding_model: str | None = None,
        actor: str | None = "owner",
        event_payload: Mapping[str, Any] | None = None,
    ) -> UserMemoryFact:
        existing = UserMemoryRepository._memory_for_event(
            session,
            owner_user_id=_owner(owner_user_id, "owner_user_id"),
            owner_channel_id=_owner(owner_channel_id, "owner_channel_id"),
            external_event_id=_bounded(external_event_id, "external_event_id", 255),
        )
        if existing is not None:
            return existing
        row = UserMemoryRepository.get_memory(
            session,
            owner_user_id=owner_user_id,
            owner_channel_id=owner_channel_id,
            memory_id=memory_id,
            for_update=True,
        )
        if row.status == "deleted":
            raise ValueError("deleted user memory cannot be corrected")
        vector = _embedding_vector(embedding)
        if vector is not None and embedding_model is None:
            raise ValueError("embedding_model is required when an embedding is stored")
        resolved_content_key = _artifact_key(content_artifact_key, "content_artifact_key")
        assert resolved_content_key is not None
        row.content_artifact_key = resolved_content_key
        row.redacted_preview = _bounded(redacted_preview, "redacted_preview", 2_000)
        row.normalized_subject = _bounded(normalized_subject, "normalized_subject", 255)
        row.evidence_artifact_key = _artifact_key(evidence_artifact_key, "evidence_artifact_key")
        row.embedding = vector
        row.embedding_model = _optional_bounded(embedding_model, "embedding_model", 255)
        row.embedding_dimensions = len(vector) if vector is not None else None
        row.revision += 1
        UserMemoryRepository._append_event(
            session,
            memory=row,
            external_event_id=external_event_id,
            event_type="correct",
            occurred_at=occurred_at,
            actor=actor,
            payload=event_payload,
        )
        session.flush()
        return row

    @staticmethod
    def mark_memory_status(
        session: Session,
        *,
        owner_user_id: str,
        owner_channel_id: str,
        memory_id: uuid.UUID,
        external_event_id: str,
        status: Literal["superseded", "deleted"],
        occurred_at: datetime,
        actor: str | None = "owner",
        event_payload: Mapping[str, Any] | None = None,
    ) -> UserMemoryFact:
        existing = UserMemoryRepository._memory_for_event(
            session,
            owner_user_id=_owner(owner_user_id, "owner_user_id"),
            owner_channel_id=_owner(owner_channel_id, "owner_channel_id"),
            external_event_id=_bounded(external_event_id, "external_event_id", 255),
        )
        if existing is not None:
            return existing
        row = UserMemoryRepository.get_memory(
            session,
            owner_user_id=owner_user_id,
            owner_channel_id=owner_channel_id,
            memory_id=memory_id,
            for_update=True,
        )
        if row.status != status:
            row.status = _status(status)
            row.revision += 1
        UserMemoryRepository._append_event(
            session,
            memory=row,
            external_event_id=external_event_id,
            event_type="delete" if status == "deleted" else "supersede",
            occurred_at=occurred_at,
            actor=actor,
            payload=event_payload,
        )
        session.flush()
        return row

    @staticmethod
    def record_retrieval_feedback(
        session: Session,
        *,
        owner_user_id: str,
        owner_channel_id: str,
        memory_id: uuid.UUID,
        external_event_id: str,
        occurred_at: datetime,
        payload: Mapping[str, Any],
        actor: str | None = "system",
    ) -> UserMemoryFact:
        existing = UserMemoryRepository._memory_for_event(
            session,
            owner_user_id=_owner(owner_user_id, "owner_user_id"),
            owner_channel_id=_owner(owner_channel_id, "owner_channel_id"),
            external_event_id=_bounded(external_event_id, "external_event_id", 255),
        )
        if existing is not None:
            return existing
        row = UserMemoryRepository.get_memory(
            session,
            owner_user_id=owner_user_id,
            owner_channel_id=owner_channel_id,
            memory_id=memory_id,
        )
        UserMemoryRepository._append_event(
            session,
            memory=row,
            external_event_id=external_event_id,
            event_type="retrieval_feedback",
            occurred_at=occurred_at,
            actor=actor,
            payload=payload,
        )
        session.flush()
        return row

    @staticmethod
    def _append_event(
        session: Session,
        *,
        memory: UserMemoryFact,
        external_event_id: str,
        event_type: UserMemoryEventType,
        occurred_at: datetime,
        actor: str | None,
        payload: Mapping[str, Any] | None,
    ) -> UserMemoryEvent:
        event = UserMemoryEvent(
            memory_id=memory.id,
            owner_user_id=memory.owner_user_id,
            owner_channel_id=memory.owner_channel_id,
            external_event_id=_bounded(external_event_id, "external_event_id", 255),
            event_type=_event_type(event_type),
            actor=_optional_bounded(actor, "actor", 255),
            occurred_at=_utc(occurred_at, "occurred_at"),
            payload=_payload(payload),
        )
        session.add(event)
        session.flush()
        return event

    @staticmethod
    def _memory_for_event(
        session: Session,
        *,
        owner_user_id: str,
        owner_channel_id: str,
        external_event_id: str,
    ) -> UserMemoryFact | None:
        event = session.scalar(
            select(UserMemoryEvent)
            .where(
                UserMemoryEvent.owner_user_id == owner_user_id,
                UserMemoryEvent.owner_channel_id == owner_channel_id,
                UserMemoryEvent.external_event_id == external_event_id,
            )
            .with_for_update()
        )
        if event is None or event.memory_id is None:
            return None
        return session.get(UserMemoryFact, event.memory_id)


class SQLAlchemyUserMemoryStore:
    """Artifact-backed adapter from the generic memory service to SQLAlchemy."""

    def __init__(self, *, engine: Engine, artifact_store: ArtifactStore) -> None:
        self._engine = engine
        self._artifacts = artifact_store

    def create_memory(self, memory: UserMemoryCreate) -> UserMemoryRecord:
        content_key = self._store_content(memory.content)
        event_id = _event_id(memory.source_external_event_id, memory, "create")
        with Session(self._engine) as session, session.begin():
            row, _created = UserMemoryRepository.create_memory(
                session,
                owner_user_id=memory.owner_scope.owner_user_id,
                owner_channel_id=memory.owner_scope.owner_channel_id,
                external_event_id=event_id,
                kind=cast_memory_kind(memory.kind),
                status=cast(
                    Literal["active", "pending_confirmation"],
                    cast_memory_status(memory.status),
                ),
                content_artifact_key=content_key,
                redacted_preview=memory.redacted_preview,
                normalized_subject=_subject(memory.normalized_subject, memory.content),
                occurred_at=memory.created_at,
                sensitivity=_persistence_sensitivity(memory.sensitivity),
                source_conversation_id=_optional_uuid(memory.source_conversation_id),
                source_external_event_id=memory.source_external_event_id,
                evidence_artifact_key=content_key,
                embedding=memory.embedding.vector if memory.embedding is not None else None,
                embedding_model=(
                    memory.embedding.model_identity if memory.embedding is not None else None
                ),
            )
            return self._record(row)

    def list_memories(
        self,
        *,
        owner_scope: UserMemoryOwnerScope,
        statuses: Sequence[AgentUserMemoryStatus],
        kinds: Sequence[AgentUserMemoryKind] | None,
        limit: int,
    ) -> Sequence[UserMemoryRecord]:
        statement = (
            select(UserMemoryFact)
            .where(
                UserMemoryFact.owner_user_id == owner_scope.owner_user_id,
                UserMemoryFact.owner_channel_id == owner_scope.owner_channel_id,
                UserMemoryFact.status.in_([item.value for item in statuses]),
            )
            .order_by(UserMemoryFact.updated_at.desc(), UserMemoryFact.created_at.desc())
            .limit(limit)
        )
        if kinds:
            statement = statement.where(UserMemoryFact.kind.in_([item.value for item in kinds]))
        with Session(self._engine) as session:
            return tuple(self._record(row) for row in session.scalars(statement))

    def search_exact_memories(
        self,
        *,
        owner_scope: UserMemoryOwnerScope,
        query: str,
        statuses: Sequence[AgentUserMemoryStatus],
        kinds: Sequence[AgentUserMemoryKind] | None,
        limit: int,
    ) -> Sequence[UserMemoryRecord]:
        query_tokens = set(re.findall(r"[a-z0-9]+", query.casefold()))
        statement = (
            select(UserMemoryFact)
            .where(
                UserMemoryFact.owner_user_id == owner_scope.owner_user_id,
                UserMemoryFact.owner_channel_id == owner_scope.owner_channel_id,
                UserMemoryFact.status.in_([item.value for item in statuses]),
            )
            .order_by(UserMemoryFact.updated_at.desc())
            .limit(min(100, max(limit * 8, limit)))
        )
        if kinds:
            statement = statement.where(UserMemoryFact.kind.in_([item.value for item in kinds]))
        with Session(self._engine) as session:
            records = [self._record(row) for row in session.scalars(statement)]
        scored = [
            (
                len(
                    query_tokens
                    & set(
                        re.findall(
                            r"[a-z0-9]+",
                            " ".join(
                                filter(
                                    None,
                                    (
                                        record.normalized_subject,
                                        record.redacted_preview,
                                        record.content,
                                    ),
                                )
                            ).casefold(),
                        )
                    )
                ),
                record,
            )
            for record in records
        ]
        scored.sort(key=lambda item: (item[0], item[1].updated_at), reverse=True)
        return tuple(record for score, record in scored if score > 0)[:limit]

    def search_semantic_memories(
        self,
        *,
        owner_scope: UserMemoryOwnerScope,
        query_embedding: Sequence[float],
        embedding_model: str,
        statuses: Sequence[AgentUserMemoryStatus],
        kinds: Sequence[AgentUserMemoryKind] | None,
        limit: int,
    ) -> Sequence[UserMemoryRecord]:
        if tuple(statuses) != (AgentUserMemoryStatus.ACTIVE,):
            raise ValueError("semantic user-memory retrieval supports active records only")
        kind = cast_memory_kind(kinds[0]) if kinds and len(kinds) == 1 else None
        with Session(self._engine) as session:
            rows = UserMemoryRepository.search_active_memories(
                session,
                owner_user_id=owner_scope.owner_user_id,
                owner_channel_id=owner_scope.owner_channel_id,
                query_embedding=query_embedding,
                embedding_model=embedding_model,
                kind=kind,
                limit=limit,
            )
            if kinds and len(kinds) > 1:
                allowed = {item.value for item in kinds}
                rows = [row for row in rows if row.kind in allowed]
            return tuple(self._record(row) for row in rows)

    def correct_memory(
        self,
        *,
        owner_scope: UserMemoryOwnerScope,
        replacement: UserMemoryCreate,
        memory_id: str | None,
        query: str | None,
    ) -> UserMemoryRecord | None:
        target = self._resolve_target(owner_scope, memory_id=memory_id, query=query)
        if target is None:
            return None
        content_key = self._store_content(replacement.content)
        event_id = _event_id(replacement.source_external_event_id, replacement, "correct")
        with Session(self._engine) as session, session.begin():
            row = UserMemoryRepository.correct_memory(
                session,
                owner_user_id=owner_scope.owner_user_id,
                owner_channel_id=owner_scope.owner_channel_id,
                memory_id=target,
                external_event_id=event_id,
                content_artifact_key=content_key,
                redacted_preview=replacement.redacted_preview,
                normalized_subject=_subject(replacement.normalized_subject, replacement.content),
                occurred_at=replacement.created_at,
                evidence_artifact_key=content_key,
                embedding=(
                    replacement.embedding.vector if replacement.embedding is not None else None
                ),
                embedding_model=(
                    replacement.embedding.model_identity
                    if replacement.embedding is not None
                    else None
                ),
            )
            return self._record(row)

    def forget_memories(
        self,
        *,
        owner_scope: UserMemoryOwnerScope,
        memory_id: str | None,
        query: str | None,
        source_external_event_id: str | None,
    ) -> Sequence[UserMemoryRecord]:
        targets: list[uuid.UUID] = []
        if memory_id is not None:
            try:
                targets = [uuid.UUID(memory_id)]
            except ValueError:
                return ()
        elif query:
            records = self.search_exact_memories(
                owner_scope=owner_scope,
                query=query,
                statuses=(AgentUserMemoryStatus.ACTIVE,),
                kinds=None,
                limit=20,
            )
            targets = [uuid.UUID(record.id) for record in records]
        if not targets:
            return ()
        deleted: list[UserMemoryRecord] = []
        with Session(self._engine) as session, session.begin():
            for target in targets:
                event_id = f"{source_external_event_id or 'memory-forget'}:{target}"
                row = UserMemoryRepository.mark_memory_status(
                    session,
                    owner_user_id=owner_scope.owner_user_id,
                    owner_channel_id=owner_scope.owner_channel_id,
                    memory_id=target,
                    external_event_id=event_id[:255],
                    status="deleted",
                    occurred_at=datetime.now(UTC),
                )
                deleted.append(self._record(row))
        return tuple(deleted)

    def _resolve_target(
        self,
        owner_scope: UserMemoryOwnerScope,
        *,
        memory_id: str | None,
        query: str | None,
    ) -> uuid.UUID | None:
        if memory_id is not None:
            try:
                target = uuid.UUID(memory_id)
            except ValueError:
                return None
            with Session(self._engine) as session:
                try:
                    UserMemoryRepository.get_memory(
                        session,
                        owner_user_id=owner_scope.owner_user_id,
                        owner_channel_id=owner_scope.owner_channel_id,
                        memory_id=target,
                    )
                except NoResultFound:
                    return None
            return target
        if not query:
            return None
        matches = self.search_exact_memories(
            owner_scope=owner_scope,
            query=query,
            statuses=(AgentUserMemoryStatus.ACTIVE,),
            kinds=None,
            limit=2,
        )
        return uuid.UUID(matches[0].id) if len(matches) == 1 else None

    def _store_content(self, content: str) -> str:
        return self._artifacts.put(
            content,
            media_type="text/plain",
            data_class="user_memory_content",
            preserve_private_content=True,
        ).key

    def _record(self, row: UserMemoryFact) -> UserMemoryRecord:
        content: str | None = None
        if row.status in {"active", "pending_confirmation"}:
            metadata = self._artifacts.get_metadata(row.content_artifact_key)
            if metadata.data_class != "user_memory_content" or metadata.size > 16_384:
                raise ValueError("user memory content artifact is invalid")
            content = self._artifacts.get(row.content_artifact_key).decode("utf-8")
        return UserMemoryRecord(
            id=str(row.id),
            owner_scope=UserMemoryOwnerScope(
                owner_user_id=row.owner_user_id,
                owner_channel_id=row.owner_channel_id,
            ),
            kind=AgentUserMemoryKind(row.kind),
            status=AgentUserMemoryStatus(row.status),
            content=content,
            redacted_preview=row.redacted_preview[:500],
            normalized_subject=row.normalized_subject,
            sensitivity=_agent_sensitivity(row.sensitivity),
            revision=row.revision,
            created_at=_aware_row_time(row.created_at),
            updated_at=_aware_row_time(row.updated_at),
        )


def cast_memory_kind(value: AgentUserMemoryKind) -> UserMemoryKind:
    return value.value  # type: ignore[return-value]


def cast_memory_status(value: AgentUserMemoryStatus) -> UserMemoryStatus:
    return value.value  # type: ignore[return-value]


def _persistence_sensitivity(value: AgentUserMemorySensitivity) -> UserMemorySensitivity:
    return cast(
        UserMemorySensitivity,
        {
            AgentUserMemorySensitivity.STANDARD: "low",
            AgentUserMemorySensitivity.PRIVATE: "medium",
            AgentUserMemorySensitivity.SENSITIVE: "high",
        }[value],
    )


def _agent_sensitivity(value: str) -> AgentUserMemorySensitivity:
    return {
        "low": AgentUserMemorySensitivity.STANDARD,
        "medium": AgentUserMemorySensitivity.PRIVATE,
        "high": AgentUserMemorySensitivity.SENSITIVE,
    }[value]


def _subject(value: str | None, content: str) -> str:
    if value is not None and value.strip():
        return value.strip()[:255]
    normalized = " ".join(re.findall(r"[a-z0-9]+", content.casefold()))
    return (normalized or "memory")[:255]


def _event_id(source: str | None, memory: UserMemoryCreate, action: str) -> str:
    if source:
        return f"{source}:{action}"[:255]
    seed = (
        f"{memory.owner_scope.owner_user_id}:{memory.owner_scope.owner_channel_id}:"
        f"{memory.created_at.isoformat()}:{memory.content}:{action}"
    )
    return f"memory:{uuid.uuid5(uuid.NAMESPACE_URL, seed)}"


def _optional_uuid(value: str | None) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise ValueError("source_conversation_id must be a UUID") from exc


def _aware_row_time(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


__all__ = [
    "USER_MEMORY_EMBEDDING_DIMENSIONS",
    "SQLAlchemyUserMemoryStore",
    "UserMemoryEventType",
    "UserMemoryKind",
    "UserMemoryRepository",
    "UserMemorySensitivity",
    "UserMemoryStatus",
]
