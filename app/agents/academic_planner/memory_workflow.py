"""Durable Discord orchestration for academic learning-focus discourse."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.academic_planner.contracts import (
    AcademicDiscourseContinuationState,
    CreateLearningFocusAction,
    ReinforceLearningFocusAction,
    ResolveLearningFocusAction,
)
from app.agents.academic_planner.discourse_loop import run_academic_discourse_loop
from app.db.academic import AcademicRepository, LearningFocusMemoryInput
from app.db.models import (
    AcademicDiscourseSession,
    AcademicDiscourseTurn,
    AcademicLearningFocus,
    Course,
)
from app.llm.embeddings import EmbeddingStatus


@dataclass(frozen=True, slots=True)
class AcademicMemoryHandleResult:
    status: Literal["not_applicable", "duplicate", "clarification", "applied"]
    response: str | None = None


class AcademicMemoryService:
    """Resume discourse, apply validated local focus transitions, and retain reflections."""

    def __init__(
        self,
        *,
        store: Any,
        model_gateway: Any,
        embedding_gateway: Any,
        timezone: str = "America/Toronto",
        default_practice_minutes: int = 30,
        end_of_day_time: time = time(21, 0),
        session_ttl_hours: int = 24,
    ) -> None:
        self._store = store
        self._model_gateway = model_gateway
        self._embedding_gateway = embedding_gateway
        self._timezone = timezone
        self._zone = ZoneInfo(timezone)
        self._default_practice_minutes = default_practice_minutes
        self._end_of_day_time = end_of_day_time
        self._session_ttl_hours = session_ttl_hours

    async def handle_reflection(
        self,
        *,
        external_event_id: str,
        channel_id: str,
        user_id: str,
        raw_text: str,
        received_at: datetime,
    ) -> AcademicMemoryHandleResult:
        current = _aware(received_at)
        with Session(self._store.engine) as session, session.begin():
            AcademicRepository.expire_discourse_sessions(session, now=current)
            duplicate = session.scalar(
                select(AcademicDiscourseTurn).where(
                    AcademicDiscourseTurn.external_event_id == external_event_id
                )
            )
            if duplicate is not None:
                return AcademicMemoryHandleResult(status="duplicate")
            open_session = AcademicRepository.find_open_discourse_session(
                session,
                discord_channel_id=channel_id,
                discord_user_id=user_id,
                now=current,
            )
            open_session_id = open_session.id if open_session is not None else None
            open_partial_state = dict(open_session.partial_state) if open_session else None
            continuation = _continuation(open_partial_state)

        result = await run_academic_discourse_loop(
            gateway=self._model_gateway,
            catalog=self._store,
            message=raw_text,
            now=current,
            continuation_state=continuation,
            timezone=self._timezone,
        )
        if not result.applicable and open_session_id is None:
            return AcademicMemoryHandleResult(status="not_applicable")
        if not result.applicable:
            question = str(
                (open_partial_state or {}).get("question")
                or "Please answer the pending academic focus question."
            )
            return AcademicMemoryHandleResult(status="clarification", response=question)

        if result.clarification is not None:
            with Session(self._store.engine) as session, session.begin():
                discourse = (
                    session.get(AcademicDiscourseSession, open_session_id)
                    if open_session_id is not None
                    else None
                )
                if discourse is None:
                    discourse = AcademicRepository.create_discourse_session(
                        session,
                        external_event_id=external_event_id,
                        discord_channel_id=channel_id,
                        discord_user_id=user_id,
                        started_at=current,
                        expires_at=current + timedelta(hours=self._session_ttl_hours),
                    )
                _, created = AcademicRepository.record_discourse_turn(
                    session,
                    session_id=discourse.id,
                    external_event_id=external_event_id,
                    received_at=current,
                )
                if not created:
                    return AcademicMemoryHandleResult(status="duplicate")
                AcademicRepository.resume_discourse_session(
                    session,
                    session_id=discourse.id,
                    now=current,
                    partial_state={
                        "continuation": result.continuation_state.model_dump(mode="json")
                        if result.continuation_state is not None
                        else None,
                        "question": result.clarification.question,
                    },
                )
            return AcademicMemoryHandleResult(
                status="clarification",
                response=result.clarification.question,
            )

        messages = (
            (*continuation.prior_user_messages, raw_text)
            if continuation is not None
            else (raw_text,)
        )
        combined_text = "\n".join(messages)
        embedding_result = await self._embedding_gateway.embed_reflection_text(combined_text)
        vector = (
            embedding_result.embedding.vector
            if embedding_result.status is EmbeddingStatus.VALID
            and embedding_result.embedding is not None
            else None
        )
        responses: list[str] = []
        with Session(self._store.engine) as session, session.begin():
            discourse = (
                session.get(AcademicDiscourseSession, open_session_id)
                if open_session_id is not None
                else None
            )
            if discourse is None:
                discourse = AcademicRepository.create_discourse_session(
                    session,
                    external_event_id=external_event_id,
                    discord_channel_id=channel_id,
                    discord_user_id=user_id,
                    started_at=current,
                    expires_at=current + timedelta(hours=self._session_ttl_hours),
                )
            _, created = AcademicRepository.record_discourse_turn(
                session,
                session_id=discourse.id,
                external_event_id=external_event_id,
                received_at=current,
            )
            if not created:
                return AcademicMemoryHandleResult(status="duplicate")
            for index, action in enumerate(result.actions):
                event_key = f"{external_event_id}:focus:{index}"
                if isinstance(action, CreateLearningFocusAction):
                    course_id = _uuid_or_none(action.course_id)
                    assessment_id = _uuid_or_none(action.assessment_id)
                    course = session.get(Course, course_id) if course_id is not None else None
                    minutes = action.target_minutes or self._default_practice_minutes
                    focus = AcademicRepository.create_learning_focus(
                        session,
                        topic=action.topic,
                        now=current,
                        course_id=course_id,
                        assessment_id=assessment_id,
                        course_code=course.course_code if course is not None else None,
                        source_session_id=discourse.id,
                        source_external_event_id=event_key,
                        next_review_at=self._next_daily_review(current),
                        practice_due_on=current.astimezone(self._zone).date() + timedelta(days=1),
                        practice_minutes=minutes,
                        memory=_memory_input(
                            combined_text,
                            vector,
                            embedding_result,
                            summary=f"Academic learning focus: {action.topic}",
                        ),
                        actor=f"discord:{user_id}",
                    )
                    responses.append(
                        "Added "
                        f"{focus.course_code + ' ' if focus.course_code else ''}{focus.topic} "
                        f"as an active focus. Tomorrow's planner will reserve a separate "
                        f"{minutes}-minute practice block."
                    )
                    continue
                focus_id = uuid.UUID(action.focus_id)
                if isinstance(action, ReinforceLearningFocusAction):
                    existing = session.get(AcademicLearningFocus, focus_id)
                    if existing is None:
                        raise ValueError("verified learning focus no longer exists")
                    minutes = (
                        action.target_minutes
                        or existing.practice_minutes
                        or self._default_practice_minutes
                    )
                    focus = AcademicRepository.reinforce_learning_focus(
                        session,
                        focus_id=focus_id,
                        now=current,
                        source_session_id=discourse.id,
                        external_event_id=event_key,
                        next_review_at=self._next_daily_review(current),
                        practice_due_on=current.astimezone(self._zone).date() + timedelta(days=1),
                        practice_minutes=minutes,
                        memory=_memory_input(
                            combined_text,
                            vector,
                            embedding_result,
                            summary=f"Reinforced academic learning focus: {existing.topic}",
                        ),
                        actor=f"discord:{user_id}",
                    )
                    responses.append(
                        f"Kept {focus.course_code + ' ' if focus.course_code else ''}{focus.topic} "
                        f"active. Tomorrow's planner will reserve a separate {minutes}-minute "
                        "practice block."
                    )
                    continue
                if isinstance(action, ResolveLearningFocusAction):
                    existing = session.get(AcademicLearningFocus, focus_id)
                    label = existing.topic if existing is not None else "that learning focus"
                    AcademicRepository.hard_delete_learning_focus(session, focus_id=focus_id)
                    responses.append(
                        f"Deleted {label}, including its stored reflection text and embedding."
                    )
                    continue
                focus = AcademicRepository.snooze_learning_focus(
                    session,
                    focus_id=focus_id,
                    now=current,
                    actor=f"discord:{user_id}",
                    reason=action.reason,
                )
                focus.next_review_at = action.snoozed_until
                responses.append(f"Snoozed {focus.topic} until {action.snoozed_until.isoformat()}.")
            AcademicRepository.complete_discourse_session(
                session,
                session_id=discourse.id,
                completed_at=current,
                final_state={"continuation": None, "question": None},
            )
        return AcademicMemoryHandleResult(status="applied", response="\n".join(responses))

    def _next_daily_review(self, current: datetime) -> datetime:
        local = current.astimezone(self._zone)
        next_local = datetime.combine(
            local.date() + timedelta(days=1),
            self._end_of_day_time,
            tzinfo=self._zone,
        )
        return next_local.astimezone(UTC)


def _continuation(value: dict[str, Any] | None) -> AcademicDiscourseContinuationState | None:
    if not value or value.get("continuation") is None:
        return None
    return AcademicDiscourseContinuationState.model_validate(value["continuation"])


def _memory_input(
    raw_text: str,
    vector: list[float] | None,
    result: Any,
    *,
    summary: str,
) -> LearningFocusMemoryInput:
    return LearningFocusMemoryInput(
        raw_text=raw_text,
        embedding=vector,
        embedding_model=result.model_identity if vector is not None else None,
        embedding_metadata={
            "status": result.status.value,
            "error_code": result.error_code.value if result.error_code is not None else None,
            "config_version": result.config_version,
        },
        redacted_summary=summary,
    )


def _uuid_or_none(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value is not None else None


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("received_at must be timezone-aware")
    return value.astimezone(UTC)


__all__ = ["AcademicMemoryHandleResult", "AcademicMemoryService"]
