"""Durable continuation state for mentioned academic agent clarifications."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.artifacts.store import ArtifactMetadata, ArtifactStore
from app.db.academic import (
    AGENT_CLARIFICATION_SESSION_KIND,
    AcademicRepository,
)
from app.db.models import AcademicDiscourseSession

MAX_AGENT_ATTEMPTS = 3
AGENT_CONTEXT_DATA_CLASS = "academic_agent_context"

_SESSION_SCHEMA_VERSION = "academic_agent_clarification.v1"
_CONTEXT_SCHEMA_VERSION = "academic_agent_context.v1"
_CONTEXT_MEDIA_TYPE = "application/json"
_CONTEXT_ARTIFACT_MAX_BYTES = 16_384
_DISCORD_ID_PATTERN = re.compile(r"^[0-9]{5,24}$")
_CANCEL_PHRASES = frozenset(
    {
        "cancel",
        "never mind",
        "nevermind",
        "start over",
    }
)

AgentClarificationInspectStatus = Literal[
    "start_new",
    "resume_pending",
    "duplicate",
    "cancelled",
    "failed",
    "exhausted",
    "in_progress",
]
AgentClarificationTurnStatus = Literal[
    "started",
    "resumed",
    "duplicate",
    "cancelled",
    "failed",
    "exhausted",
    "in_progress",
]
AgentClarificationCompletionStatus = Literal[
    "clarification_saved",
    "exhausted",
    "resolved",
    "failed",
    "cancelled",
]
AgentClarificationOutcome = Literal[
    "awaiting_model",
    "awaiting_clarification",
    "resolved",
    "failed",
    "cancelled",
    "exhausted",
]


class AgentClarificationContext(BaseModel):
    """Private user-provided continuation fields loaded only for Qwen prompts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["academic_agent_context.v1"] = _CONTEXT_SCHEMA_VERSION
    original_user_request: SecretStr = Field(repr=False)
    prior_clarification_questions: tuple[str, ...] = Field(default=(), max_length=2)
    clarification_answers: tuple[SecretStr, ...] = Field(default=(), max_length=2, repr=False)

    @field_validator("original_user_request")
    @classmethod
    def original_request_is_bounded(cls, value: SecretStr) -> SecretStr:
        text = value.get_secret_value().strip()
        if not text or len(text) > 4_000:
            raise ValueError("original request must be non-empty and bounded")
        return SecretStr(text)

    @field_validator("prior_clarification_questions")
    @classmethod
    def questions_are_bounded(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for question in value:
            if not question.strip() or len(question) > 1_000:
                raise ValueError("clarification questions must be non-empty and bounded")
        return tuple(question.strip() for question in value)

    @field_validator("clarification_answers")
    @classmethod
    def answers_are_bounded(cls, value: tuple[SecretStr, ...]) -> tuple[SecretStr, ...]:
        cleaned: list[SecretStr] = []
        for answer in value:
            text = answer.get_secret_value().strip()
            if not text or len(text) > 4_000:
                raise ValueError("clarification answers must be non-empty and bounded")
            cleaned.append(SecretStr(text))
        return tuple(cleaned)

    def model_post_init(self, __context: object) -> None:
        if len(self.clarification_answers) > len(self.prior_clarification_questions):
            raise ValueError("answers cannot outnumber clarification questions")

    def with_question(self, question: str) -> AgentClarificationContext:
        return AgentClarificationContext(
            original_user_request=self.original_user_request,
            prior_clarification_questions=(
                *self.prior_clarification_questions,
                question.strip(),
            ),
            clarification_answers=self.clarification_answers,
        )

    def with_answer(self, answer: str) -> AgentClarificationContext:
        return AgentClarificationContext(
            original_user_request=self.original_user_request,
            prior_clarification_questions=self.prior_clarification_questions,
            clarification_answers=(*self.clarification_answers, SecretStr(answer)),
        )


class AgentClarificationSessionState(BaseModel):
    """Bounded relational metadata; no raw Discord text is allowed here."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["academic_agent_clarification.v1"] = _SESSION_SCHEMA_VERSION
    root_event_id: str = Field(min_length=1, max_length=255)
    attempt_number: int = Field(ge=1, le=MAX_AGENT_ATTEMPTS)
    attempt_limit: Literal[3] = MAX_AGENT_ATTEMPTS
    last_clarification_question: str | None = Field(default=None, max_length=1_000)
    context_artifact_key: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    outcome: AgentClarificationOutcome


@dataclass(frozen=True, slots=True)
class AgentClarificationInspectResult:
    status: AgentClarificationInspectStatus
    attempt_number: int
    attempt_limit: int = MAX_AGENT_ATTEMPTS
    session_id: uuid.UUID | None = None
    root_event_id: str | None = None
    response: str | None = None
    context: AgentClarificationContext | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class AgentClarificationTurnResult:
    status: AgentClarificationTurnStatus
    attempt_number: int
    attempt_limit: int = MAX_AGENT_ATTEMPTS
    session_id: uuid.UUID | None = None
    root_event_id: str | None = None
    response: str | None = None
    context: AgentClarificationContext | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class AgentClarificationCompletionResult:
    status: AgentClarificationCompletionStatus
    attempt_number: int
    session_id: uuid.UUID
    root_event_id: str
    attempt_limit: int = MAX_AGENT_ATTEMPTS
    response: str | None = None


class AcademicAgentClarificationService:
    """Two-phase durable state manager for academic agent clarification reruns."""

    def __init__(
        self,
        *,
        engine: Any,
        artifact_store: ArtifactStore,
        session_ttl_hours: int = 24,
        clock: Any | None = None,
    ) -> None:
        if session_ttl_hours < 1 or session_ttl_hours > 168:
            raise ValueError("session_ttl_hours must be between 1 and 168")
        self._engine = engine
        self._artifacts = artifact_store
        self._session_ttl_hours = session_ttl_hours
        self._clock = clock or (lambda: datetime.now(UTC))

    def has_pending_clarification(
        self,
        *,
        channel_id: str,
        user_id: str,
        received_at: datetime,
    ) -> bool:
        """Check owner-scoped pending state without reading or persisting message text."""

        current = _aware(received_at)
        channel = _discord_id(channel_id, "channel_id")
        user = _discord_id(user_id, "user_id")
        with Session(self._engine) as session:
            row = AcademicRepository.find_open_discourse_session(
                session,
                discord_channel_id=channel,
                discord_user_id=user,
                now=current,
                session_kind=AGENT_CLARIFICATION_SESSION_KIND,
            )
            if row is None:
                return False
            try:
                state = _state_from_row(row)
            except ValueError:
                return False
            return state.outcome == "awaiting_clarification"

    def inspect_message(
        self,
        *,
        external_event_id: str,
        channel_id: str,
        user_id: str,
        raw_text: str,
        received_at: datetime,
    ) -> AgentClarificationInspectResult:
        """Check duplicate/cancel/resume state without consuming a Qwen attempt."""

        event_id = _bounded_external_event(external_event_id)
        current = _aware(received_at)
        channel = _discord_id(channel_id, "channel_id")
        user = _discord_id(user_id, "user_id")
        with Session(self._engine) as session, session.begin():
            AcademicRepository.expire_discourse_sessions(session, now=current)
            if AcademicRepository.find_discourse_turn(session, external_event_id=event_id):
                return AgentClarificationInspectResult(status="duplicate", attempt_number=1)
            row = AcademicRepository.lock_open_agent_clarification_session(
                session,
                discord_channel_id=channel,
                discord_user_id=user,
                now=current,
            )
            if _is_cancel(raw_text):
                if row is not None:
                    state = _state_from_row(row)
                    AcademicRepository.record_discourse_turn(
                        session,
                        session_id=row.id,
                        external_event_id=event_id,
                        received_at=current,
                    )
                    final_state = _terminal_state(state, outcome="cancelled")
                    AcademicRepository.close_agent_clarification_session(
                        session,
                        session_id=row.id,
                        completed_at=current,
                        final_state=final_state.model_dump(mode="json"),
                    )
                    return AgentClarificationInspectResult(
                        status="cancelled",
                        attempt_number=state.attempt_number,
                        session_id=row.id,
                        root_event_id=state.root_event_id,
                        response="No problem. Nothing was changed.",
                    )
                return AgentClarificationInspectResult(
                    status="cancelled",
                    attempt_number=1,
                    response="No active clarification was waiting. Nothing was changed.",
                )
            if row is None:
                return AgentClarificationInspectResult(
                    status="start_new",
                    attempt_number=1,
                    root_event_id=event_id,
                )
            state = _state_from_row(row)
            if state.attempt_number >= MAX_AGENT_ATTEMPTS:
                final_state = _terminal_state(state, outcome="exhausted")
                AcademicRepository.close_agent_clarification_session(
                    session,
                    session_id=row.id,
                    completed_at=current,
                    final_state=final_state.model_dump(mode="json"),
                )
                return AgentClarificationInspectResult(
                    status="exhausted",
                    attempt_number=state.attempt_number,
                    session_id=row.id,
                    root_event_id=state.root_event_id,
                    response=_EXHAUSTED_RESPONSE,
                )
            try:
                if state.outcome == "awaiting_model":
                    return AgentClarificationInspectResult(
                        status="in_progress",
                        attempt_number=state.attempt_number,
                        session_id=row.id,
                        root_event_id=state.root_event_id,
                        response="I am still working on the current academic request.",
                    )
                if state.outcome != "awaiting_clarification":
                    return AgentClarificationInspectResult(
                        status="failed",
                        attempt_number=state.attempt_number,
                        session_id=row.id,
                        root_event_id=state.root_event_id,
                        response=(
                            "I could not safely resume that clarification. "
                            "Start with a new mention."
                        ),
                    )
                context = self._load_context_for_state(state, now=current)
            except (OSError, ValueError, json.JSONDecodeError):
                final_state = _terminal_state(state, outcome="failed")
                AcademicRepository.close_agent_clarification_session(
                    session,
                    session_id=row.id,
                    completed_at=current,
                    final_state=final_state.model_dump(mode="json"),
                )
                return AgentClarificationInspectResult(
                    status="failed",
                    attempt_number=state.attempt_number,
                    session_id=row.id,
                    root_event_id=state.root_event_id,
                    response=(
                        "I could not safely resume that clarification. Start with a new mention."
                    ),
                )
            return AgentClarificationInspectResult(
                status="resume_pending",
                attempt_number=state.attempt_number + 1,
                session_id=row.id,
                root_event_id=state.root_event_id,
                context=context,
            )

    def prepare_turn(
        self,
        *,
        external_event_id: str,
        channel_id: str,
        user_id: str,
        raw_text: str,
        received_at: datetime,
    ) -> AgentClarificationTurnResult:
        """Record the accepted Qwen turn after readiness and return prompt context."""

        event_id = _bounded_external_event(external_event_id)
        current = _aware(received_at)
        channel = _discord_id(channel_id, "channel_id")
        user = _discord_id(user_id, "user_id")
        if _is_cancel(raw_text):
            inspected = self.inspect_message(
                external_event_id=event_id,
                channel_id=channel_id,
                user_id=user_id,
                raw_text=raw_text,
                received_at=current,
            )
            return AgentClarificationTurnResult(
                status="cancelled",
                attempt_number=inspected.attempt_number,
                session_id=inspected.session_id,
                root_event_id=inspected.root_event_id,
                response=inspected.response,
            )

        with Session(self._engine) as session, session.begin():
            AcademicRepository.expire_discourse_sessions(session, now=current)
            if AcademicRepository.find_discourse_turn(session, external_event_id=event_id):
                return AgentClarificationTurnResult(status="duplicate", attempt_number=1)
            row = AcademicRepository.lock_open_agent_clarification_session(
                session,
                discord_channel_id=channel,
                discord_user_id=user,
                now=current,
            )
            if row is None:
                context = AgentClarificationContext(original_user_request=SecretStr(raw_text))
                metadata = self._store_context(context)
                state = AgentClarificationSessionState(
                    root_event_id=event_id,
                    attempt_number=1,
                    context_artifact_key=metadata.key,
                    outcome="awaiting_model",
                )
                try:
                    row = AcademicRepository.create_agent_clarification_session(
                        session,
                        external_event_id=event_id,
                        discord_channel_id=channel,
                        discord_user_id=user,
                        started_at=current,
                        expires_at=current + timedelta(hours=self._session_ttl_hours),
                        partial_state=state.model_dump(mode="json"),
                    )
                except IntegrityError:
                    raise ValueError("an agent clarification session is already open") from None
                AcademicRepository.record_discourse_turn(
                    session,
                    session_id=row.id,
                    external_event_id=event_id,
                    received_at=current,
                )
                return AgentClarificationTurnResult(
                    status="started",
                    attempt_number=1,
                    session_id=row.id,
                    root_event_id=event_id,
                    context=context,
                )

            state = _state_from_row(row)
            if state.outcome == "awaiting_model":
                return AgentClarificationTurnResult(
                    status="in_progress",
                    attempt_number=state.attempt_number,
                    session_id=row.id,
                    root_event_id=state.root_event_id,
                    response="I am still working on the current academic request.",
                )
            if state.outcome != "awaiting_clarification":
                return AgentClarificationTurnResult(
                    status="failed",
                    attempt_number=state.attempt_number,
                    session_id=row.id,
                    root_event_id=state.root_event_id,
                    response=(
                        "I could not safely resume that clarification. Start with a new mention."
                    ),
                )
            if state.attempt_number >= MAX_AGENT_ATTEMPTS:
                final_state = _terminal_state(state, outcome="exhausted")
                AcademicRepository.close_agent_clarification_session(
                    session,
                    session_id=row.id,
                    completed_at=current,
                    final_state=final_state.model_dump(mode="json"),
                )
                return AgentClarificationTurnResult(
                    status="exhausted",
                    attempt_number=state.attempt_number,
                    session_id=row.id,
                    root_event_id=state.root_event_id,
                    response=_EXHAUSTED_RESPONSE,
                )
            context = self._load_context_for_state(state, now=current).with_answer(raw_text)
            metadata = self._store_context(context)
            next_attempt = state.attempt_number + 1
            next_state = state.model_copy(
                update={
                    "attempt_number": next_attempt,
                    "context_artifact_key": metadata.key,
                    "outcome": "awaiting_model",
                }
            )
            AcademicRepository.replace_agent_clarification_state(
                session,
                session_id=row.id,
                now=current,
                partial_state=next_state.model_dump(mode="json"),
            )
            turn, created = AcademicRepository.record_discourse_turn(
                session,
                session_id=row.id,
                external_event_id=event_id,
                received_at=current,
            )
            if not created:
                return AgentClarificationTurnResult(
                    status="duplicate",
                    attempt_number=next_attempt,
                    session_id=turn.session_id,
                    root_event_id=state.root_event_id,
                )
            return AgentClarificationTurnResult(
                status="resumed",
                attempt_number=next_attempt,
                session_id=row.id,
                root_event_id=state.root_event_id,
                context=context,
            )

    def persist_clarification_or_exhaust(
        self,
        *,
        session_id: uuid.UUID,
        question: str,
        now: datetime | None = None,
    ) -> AgentClarificationCompletionResult:
        """Persist an answerable question unless the third attempt has been spent."""

        current = _aware(now or self._clock())
        bounded_question = _bounded_question(question)
        with Session(self._engine) as session, session.begin():
            row = _lock_agent_session(session, session_id)
            state = _state_from_row(row)
            if row.state != "open":
                return AgentClarificationCompletionResult(
                    status="failed",
                    attempt_number=state.attempt_number,
                    session_id=row.id,
                    root_event_id=state.root_event_id,
                    response="This clarification is no longer open.",
                )
            if state.outcome != "awaiting_model":
                return AgentClarificationCompletionResult(
                    status="failed",
                    attempt_number=state.attempt_number,
                    session_id=row.id,
                    root_event_id=state.root_event_id,
                    response="This clarification is not awaiting a model result.",
                )
            if state.attempt_number >= MAX_AGENT_ATTEMPTS:
                final_state = _terminal_state(
                    state,
                    outcome="exhausted",
                    last_clarification_question=bounded_question,
                )
                AcademicRepository.close_agent_clarification_session(
                    session,
                    session_id=row.id,
                    completed_at=current,
                    final_state=final_state.model_dump(mode="json"),
                )
                return AgentClarificationCompletionResult(
                    status="exhausted",
                    attempt_number=state.attempt_number,
                    session_id=row.id,
                    root_event_id=state.root_event_id,
                    response=_EXHAUSTED_RESPONSE,
                )
            context = self._load_context_for_state(state, now=current).with_question(
                bounded_question
            )
            metadata = self._store_context(context)
            next_state = state.model_copy(
                update={
                    "last_clarification_question": bounded_question,
                    "context_artifact_key": metadata.key,
                    "outcome": "awaiting_clarification",
                }
            )
            AcademicRepository.replace_agent_clarification_state(
                session,
                session_id=row.id,
                now=current,
                partial_state=next_state.model_dump(mode="json"),
            )
            return AgentClarificationCompletionResult(
                status="clarification_saved",
                attempt_number=state.attempt_number,
                session_id=row.id,
                root_event_id=state.root_event_id,
            )

    def complete_resolved(
        self,
        *,
        session_id: uuid.UUID,
        now: datetime | None = None,
    ) -> AgentClarificationCompletionResult:
        return self._complete_terminal(session_id=session_id, outcome="resolved", now=now)

    def complete_failed(
        self,
        *,
        session_id: uuid.UUID,
        now: datetime | None = None,
    ) -> AgentClarificationCompletionResult:
        return self._complete_terminal(session_id=session_id, outcome="failed", now=now)

    def load_open_context(
        self,
        *,
        session_id: uuid.UUID,
        now: datetime | None = None,
    ) -> AgentClarificationContext:
        """Load context only for an unexpired open agent-clarification session."""

        current = _aware(now or self._clock())
        with Session(self._engine) as session, session.begin():
            row = _lock_agent_session(session, session_id)
            if row.state != "open":
                raise ValueError("agent clarification session is not open")
            if row.expires_at is not None and _aware(row.expires_at) <= current:
                row.state = "expired"
                row.partial_state = {}
                session.flush()
                raise ValueError("agent clarification session expired")
            return self._load_context_for_state(_state_from_row(row), now=current)

    def _complete_terminal(
        self,
        *,
        session_id: uuid.UUID,
        outcome: Literal["resolved", "failed"],
        now: datetime | None,
    ) -> AgentClarificationCompletionResult:
        current = _aware(now or self._clock())
        with Session(self._engine) as session, session.begin():
            row = _lock_agent_session(session, session_id)
            state = _state_from_row(row)
            final_state = _terminal_state(state, outcome=outcome)
            AcademicRepository.close_agent_clarification_session(
                session,
                session_id=row.id,
                completed_at=current,
                final_state=final_state.model_dump(mode="json"),
            )
            return AgentClarificationCompletionResult(
                status=outcome,
                attempt_number=state.attempt_number,
                session_id=row.id,
                root_event_id=state.root_event_id,
            )

    def _store_context(self, context: AgentClarificationContext) -> ArtifactMetadata:
        payload = json.dumps(
            _context_to_artifact_payload(context),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(payload.encode("utf-8")) > _CONTEXT_ARTIFACT_MAX_BYTES:
            raise ValueError("agent clarification context artifact is too large")
        metadata = self._artifacts.put(
            payload,
            media_type=_CONTEXT_MEDIA_TYPE,
            data_class=AGENT_CONTEXT_DATA_CLASS,
        )
        _validate_context_metadata(metadata, now=_aware(self._clock()))
        return metadata

    def _load_context_for_state(
        self,
        state: AgentClarificationSessionState,
        *,
        now: datetime,
    ) -> AgentClarificationContext:
        if not state.context_artifact_key:
            raise ValueError("agent clarification context is unavailable")
        metadata = self._artifacts.get_metadata(state.context_artifact_key)
        _validate_context_metadata(metadata, now=now)
        payload = json.loads(self._artifacts.get(state.context_artifact_key).decode("utf-8"))
        return AgentClarificationContext.model_validate(payload)


def _lock_agent_session(session: Session, session_id: uuid.UUID) -> AcademicDiscourseSession:
    row = session.get(AcademicDiscourseSession, session_id, with_for_update=True)
    if row is None or row.session_kind != AGENT_CLARIFICATION_SESSION_KIND:
        raise ValueError("agent clarification session was not found")
    return row


def _state_from_row(row: AcademicDiscourseSession) -> AgentClarificationSessionState:
    return AgentClarificationSessionState.model_validate(row.partial_state)


def _terminal_state(
    state: AgentClarificationSessionState,
    *,
    outcome: Literal["resolved", "failed", "cancelled", "exhausted"],
    last_clarification_question: str | None = None,
) -> AgentClarificationSessionState:
    return state.model_copy(
        update={
            "context_artifact_key": None,
            "outcome": outcome,
            "last_clarification_question": (
                last_clarification_question
                if last_clarification_question is not None
                else state.last_clarification_question
            ),
        }
    )


def _context_to_artifact_payload(context: AgentClarificationContext) -> dict[str, object]:
    return {
        "schema_version": context.schema_version,
        "original_user_request": context.original_user_request.get_secret_value(),
        "prior_clarification_questions": list(context.prior_clarification_questions),
        "clarification_answers": [
            item.get_secret_value() for item in context.clarification_answers
        ],
    }


def _validate_context_metadata(metadata: ArtifactMetadata, *, now: datetime) -> None:
    if metadata.data_class != AGENT_CONTEXT_DATA_CLASS:
        raise ValueError("agent clarification context artifact has wrong data class")
    if metadata.media_type != _CONTEXT_MEDIA_TYPE:
        raise ValueError("agent clarification context artifact has wrong media type")
    if metadata.size > _CONTEXT_ARTIFACT_MAX_BYTES:
        raise ValueError("agent clarification context artifact is too large")
    if metadata.expires_at is None:
        raise ValueError("agent clarification context artifact must be expiring")
    if metadata.expires_at <= now:
        raise ValueError("agent clarification context artifact expired")


def _bounded_external_event(value: str) -> str:
    if not value.strip() or len(value) > 255:
        raise ValueError("external event id must be non-empty and bounded")
    return value.strip()


def _bounded_question(value: str) -> str:
    question = value.strip()
    if not question or len(question) > 1_000:
        raise ValueError("clarification question must be non-empty and bounded")
    return question


def _discord_id(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not _DISCORD_ID_PATTERN.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a Discord snowflake")
    return normalized


def _is_cancel(value: str) -> bool:
    return value.strip().casefold() in _CANCEL_PHRASES


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


_EXHAUSTED_RESPONSE = (
    "The automatic clarification limit was reached. Start over with a new complete mention."
)

__all__ = [
    "AGENT_CONTEXT_DATA_CLASS",
    "MAX_AGENT_ATTEMPTS",
    "AcademicAgentClarificationService",
    "AgentClarificationCompletionResult",
    "AgentClarificationContext",
    "AgentClarificationInspectResult",
    "AgentClarificationSessionState",
    "AgentClarificationTurnResult",
]
