"""Durable Discord orchestration for academic learning-focus discourse."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from app.agents.academic_planner.contracts import (
    AcademicDiscourseContinuationState,
    AcademicLearningFocusOption,
    AcademicMemoryReviewDecision,
    CreateLearningFocusAction,
    LearningFocusStatus,
    MemoryManagementOutcome,
    ReinforceLearningFocusAction,
    ResolveLearningFocusAction,
)
from app.agents.academic_planner.discourse_loop import (
    decide_academic_memory_review,
    run_academic_discourse_loop,
    summarize_academic_memory,
)
from app.db.academic import AcademicRepository, LearningFocusMemoryInput
from app.db.models import (
    AcademicDiscourseSession,
    AcademicDiscourseTurn,
    AcademicLearningFocus,
    Course,
)
from app.llm.embeddings import EmbeddingStatus

_MEMORY_REVIEW_KIND = "memory_review"
_EMPTY_MEMORY_RESPONSE = (
    "I do not currently have any active or snoozed academic learning focuses stored for you."
)
_CANCEL_RESPONSE = "No problem—nothing was changed."
_CANCEL_PHRASES = frozenset(
    {
        "never mind",
        "nevermind",
        "cancel",
        "cancel that",
        "please cancel",
        "ignore that",
        "forget it",
        "drop it",
        "leave it",
        "stop",
        "please stop",
    }
)
_YES_PHRASES = frozenset({"yes", "yes please", "correct", "that one"})
_NO_PHRASES = frozenset({"no", "nope", "do not", "don't"})
_VIEW_PHRASES = frozenset(
    {
        "see memory",
        "show me your memory",
        "what do you remember about my coursework",
        "show my learning focuses",
    }
)
_UUID_TEXT = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class AcademicMemoryHandleResult:
    status: Literal[
        "not_applicable",
        "duplicate",
        "clarification",
        "applied",
        "summarized",
        "cancelled",
        "no_change",
    ]
    response: str | None = None


class _OwnerScopedCatalog:
    """Bind model-selected focus reads to one authorized Discord owner."""

    def __init__(self, store: Any, *, owner_user_id: str, owner_channel_id: str) -> None:
        self._store = store
        self._owner_user_id = owner_user_id
        self._owner_channel_id = owner_channel_id

    def search_courses(self, query: str) -> Sequence[Any]:
        return self._store.search_courses(query)

    def search_assessments(self, query: str, course_id: str | None = None) -> Sequence[Any]:
        return self._store.search_assessments(query, course_id)

    def search_learning_focuses(self, query: str | None, statuses: Sequence[Any]) -> Sequence[Any]:
        return self._store.search_learning_focuses(
            query,
            statuses,
            owner_user_id=self._owner_user_id,
            owner_channel_id=self._owner_channel_id,
        )

    async def search_semantic_focuses(self, query: str, *, limit: int) -> Sequence[Any]:
        return await self._store.search_semantic_focuses(
            query,
            limit=limit,
            owner_user_id=self._owner_user_id,
            owner_channel_id=self._owner_channel_id,
        )


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

    async def handle_memory_review(
        self,
        *,
        external_event_id: str,
        channel_id: str,
        user_id: str,
        raw_text: str,
        received_at: datetime,
    ) -> AcademicMemoryHandleResult:
        """Start or resume the durable owner-scoped see-memory workflow."""

        current = _aware(received_at)
        normalized = _normalized_phrase(raw_text)
        with Session(self._store.engine) as session, session.begin():
            AcademicRepository.expire_discourse_sessions(session, now=current)
            if (
                session.scalar(
                    select(AcademicDiscourseTurn.id).where(
                        AcademicDiscourseTurn.external_event_id == external_event_id
                    )
                )
                is not None
            ):
                return AcademicMemoryHandleResult(status="duplicate")
            review = AcademicRepository.find_open_discourse_session(
                session,
                discord_channel_id=channel_id,
                discord_user_id=user_id,
                now=current,
                session_kind=_MEMORY_REVIEW_KIND,
            )
            review_id = review.id if review is not None else None
            review_state = dict(review.partial_state) if review is not None else None

        if review_id is not None:
            if normalized in _CANCEL_PHRASES:
                return self._cancel_review(
                    session_id=review_id,
                    external_event_id=external_event_id,
                    received_at=current,
                )
            return await self._resume_memory_review(
                session_id=review_id,
                state=review_state or {},
                external_event_id=external_event_id,
                channel_id=channel_id,
                user_id=user_id,
                raw_text=raw_text,
                received_at=current,
            )
        if not _is_memory_view_intent(normalized):
            return AcademicMemoryHandleResult(status="not_applicable")
        return await self._start_memory_review(
            external_event_id=external_event_id,
            channel_id=channel_id,
            user_id=user_id,
            received_at=current,
        )

    async def _start_memory_review(
        self,
        *,
        external_event_id: str,
        channel_id: str,
        user_id: str,
        received_at: datetime,
    ) -> AcademicMemoryHandleResult:
        raw_focuses, truncated = self._store.list_memory_focuses_for_owner(
            owner_user_id=user_id,
            owner_channel_id=channel_id,
        )
        focuses = tuple(AcademicLearningFocusOption.model_validate(item) for item in raw_focuses)
        initial_state: dict[str, Any] = {
            "focuses": [item.model_dump(mode="json") for item in focuses],
            "pending_action": None,
            "candidate_focus_ids": [],
            "candidate_revisions": {},
            "clarification_question": None,
            "memory_set_truncated": truncated,
            "inbound_event_ids": [external_event_id],
        }
        with Session(self._store.engine) as session, session.begin():
            review = AcademicRepository.create_discourse_session(
                session,
                external_event_id=external_event_id,
                discord_channel_id=channel_id,
                discord_user_id=user_id,
                session_kind=_MEMORY_REVIEW_KIND,
                partial_state=initial_state,
                started_at=received_at,
                expires_at=received_at + timedelta(hours=self._session_ttl_hours),
            )
            _, created = AcademicRepository.record_discourse_turn(
                session,
                session_id=review.id,
                external_event_id=external_event_id,
                received_at=received_at,
            )
            if not created:
                return AcademicMemoryHandleResult(status="duplicate")
            if not focuses:
                AcademicRepository.complete_discourse_session(
                    session,
                    session_id=review.id,
                    completed_at=received_at,
                    final_state=_cleared_review_state(external_event_id),
                )
                return AcademicMemoryHandleResult(
                    status="summarized", response=_EMPTY_MEMORY_RESPONSE
                )
            review_id = review.id

        summary = await summarize_academic_memory(
            gateway=self._model_gateway,
            focuses=focuses,
            truncated=truncated,
        )
        if summary is None:
            summary_text = _fallback_summary(focuses, truncated=truncated)
            covered_ids = tuple(item.focus_id for item in focuses)
        else:
            summary_text = summary.summary_text
            covered_ids = summary.covered_focus_ids
        if _UUID_TEXT.search(summary_text):
            summary_text = _fallback_summary(focuses, truncated=truncated)
            covered_ids = tuple(item.focus_id for item in focuses)
        covered = tuple(item for item in focuses if item.focus_id in set(covered_ids))
        persisted_state: dict[str, Any] = {
            **initial_state,
            "focuses": [item.model_dump(mode="json") for item in covered],
        }
        with Session(self._store.engine) as session, session.begin():
            review = AcademicRepository.resume_discourse_session(
                session,
                session_id=review_id,
                now=received_at,
                partial_state=persisted_state,
            )
            if review.state != "open":
                return AcademicMemoryHandleResult(status="duplicate")
        return AcademicMemoryHandleResult(status="summarized", response=summary_text)

    def _cancel_review(
        self,
        *,
        session_id: uuid.UUID,
        external_event_id: str,
        received_at: datetime,
    ) -> AcademicMemoryHandleResult:
        with Session(self._store.engine) as session, session.begin():
            review = AcademicRepository.resume_discourse_session(
                session, session_id=session_id, now=received_at
            )
            if review.state != "open":
                return AcademicMemoryHandleResult(status="duplicate")
            _, created = AcademicRepository.record_discourse_turn(
                session,
                session_id=review.id,
                external_event_id=external_event_id,
                received_at=received_at,
            )
            if not created:
                return AcademicMemoryHandleResult(status="duplicate")
            AcademicRepository.complete_discourse_session(
                session,
                session_id=review.id,
                completed_at=received_at,
                final_state=_cleared_review_state(external_event_id),
            )
        return AcademicMemoryHandleResult(status="cancelled", response=_CANCEL_RESPONSE)

    async def _resume_memory_review(
        self,
        *,
        session_id: uuid.UUID,
        state: Mapping[str, Any],
        external_event_id: str,
        channel_id: str,
        user_id: str,
        raw_text: str,
        received_at: datetime,
    ) -> AcademicMemoryHandleResult:
        focuses = _review_focuses(state)
        with Session(self._store.engine) as session, session.begin():
            review = AcademicRepository.resume_discourse_session(
                session, session_id=session_id, now=received_at
            )
            if review.state != "open":
                return AcademicMemoryHandleResult(status="not_applicable")
            _, created = AcademicRepository.record_discourse_turn(
                session,
                session_id=review.id,
                external_event_id=external_event_id,
                received_at=received_at,
            )
            if not created:
                return AcademicMemoryHandleResult(status="duplicate")
            event_ids = [str(item) for item in state.get("inbound_event_ids", [])][-19:]
            review.partial_state = {
                **review.partial_state,
                "inbound_event_ids": [*event_ids, external_event_id],
            }

        normalized = _normalized_phrase(raw_text)
        pending_value = state.get("pending_action")
        pending = (
            cast(Mapping[str, Any], pending_value) if isinstance(pending_value, dict) else None
        )
        if pending is not None and normalized in _YES_PHRASES:
            return await self._apply_pending_action(
                session_id=session_id,
                state=state,
                pending=pending,
                external_event_id=external_event_id,
                channel_id=channel_id,
                user_id=user_id,
                raw_text=raw_text,
                received_at=received_at,
            )
        if pending is not None and normalized in _NO_PHRASES:
            return self._close_without_change(
                session_id=session_id,
                external_event_id=external_event_id,
                received_at=received_at,
                response="Okay—nothing was changed.",
            )
        if _is_subjectless_delete(normalized):
            return self._persist_delete_clarification(
                session_id=session_id,
                focuses=focuses,
                external_event_id=external_event_id,
                received_at=received_at,
            )

        decision = await decide_academic_memory_review(
            gateway=self._model_gateway,
            message=raw_text,
            focuses=focuses,
            pending_action=pending,
        )
        if decision is None:
            return AcademicMemoryHandleResult(
                status="no_change",
                response=(
                    "I could not safely match that request to the learning focuses I showed. "
                    "Please name the course topic you want to change."
                ),
            )
        if decision.outcome is MemoryManagementOutcome.SUMMARIZE_MEMORY:
            truncated = state.get("memory_set_truncated") is True
            summary = await summarize_academic_memory(
                gateway=self._model_gateway,
                focuses=focuses,
                truncated=truncated,
            )
            response = (
                summary.summary_text
                if summary is not None
                else _fallback_summary(focuses, truncated=truncated)
            )
            if _UUID_TEXT.search(response):
                response = _fallback_summary(focuses, truncated=truncated)
            return AcademicMemoryHandleResult(status="summarized", response=response)
        if decision.outcome is MemoryManagementOutcome.CANCEL:
            return self._close_without_change(
                session_id=session_id,
                external_event_id=external_event_id,
                received_at=received_at,
                response=_CANCEL_RESPONSE,
            )
        if decision.outcome is MemoryManagementOutcome.CLARIFY:
            return self._persist_model_clarification(
                session_id=session_id,
                focuses=focuses,
                decision=decision,
                external_event_id=external_event_id,
                received_at=received_at,
            )
        if decision.outcome is MemoryManagementOutcome.DELETE_FOCUS:
            if decision.subject_text is None:
                return self._persist_delete_clarification(
                    session_id=session_id,
                    focuses=focuses,
                    external_event_id=external_event_id,
                    received_at=received_at,
                )
            matched = _subject_matches(decision.subject_text, focuses)
            if len(matched) != 1 or matched[0].focus_id != decision.focus_id:
                return self._persist_delete_clarification(
                    session_id=session_id,
                    focuses=matched or focuses,
                    external_event_id=external_event_id,
                    received_at=received_at,
                )
            return self._apply_delete(
                session_id=session_id,
                focuses=focuses,
                focus_id=str(decision.focus_id),
                external_event_id=external_event_id,
                channel_id=channel_id,
                user_id=user_id,
                received_at=received_at,
            )
        if decision.outcome is MemoryManagementOutcome.REPLACE_FOCUS:
            return await self._apply_replacement(
                session_id=session_id,
                focuses=focuses,
                decision=decision,
                external_event_id=external_event_id,
                channel_id=channel_id,
                user_id=user_id,
                raw_text=raw_text,
                received_at=received_at,
            )
        if decision.outcome is MemoryManagementOutcome.REINFORCE_FOCUS:
            return self._apply_reinforcement(
                session_id=session_id,
                focuses=focuses,
                decision=decision,
                external_event_id=external_event_id,
                channel_id=channel_id,
                user_id=user_id,
                raw_text=raw_text,
                received_at=received_at,
            )
        return AcademicMemoryHandleResult(
            status="no_change", response="I left your academic learning focuses unchanged."
        )

    async def _apply_pending_action(
        self,
        *,
        session_id: uuid.UUID,
        state: Mapping[str, Any],
        pending: Mapping[str, Any],
        external_event_id: str,
        channel_id: str,
        user_id: str,
        raw_text: str,
        received_at: datetime,
    ) -> AcademicMemoryHandleResult:
        del raw_text
        candidate_ids = tuple(str(item) for item in pending.get("candidate_focus_ids", ()))
        if pending.get("operation") != "delete_focus" or len(candidate_ids) != 1:
            return AcademicMemoryHandleResult(
                status="clarification",
                response="Please name the specific course topic you want me to delete.",
            )
        return self._apply_delete(
            session_id=session_id,
            focuses=_review_focuses(state),
            focus_id=candidate_ids[0],
            external_event_id=external_event_id,
            channel_id=channel_id,
            user_id=user_id,
            received_at=received_at,
        )

    def _persist_delete_clarification(
        self,
        *,
        session_id: uuid.UUID,
        focuses: Sequence[AcademicLearningFocusOption],
        external_event_id: str,
        received_at: datetime,
    ) -> AcademicMemoryHandleResult:
        if not focuses:
            return self._close_without_change(
                session_id=session_id,
                external_event_id=external_event_id,
                received_at=received_at,
                response="I could not find a stored learning focus to change.",
            )
        candidates = tuple(focuses)
        question = _candidate_question(candidates)
        pending = {
            "operation": "delete_focus",
            "candidate_focus_ids": [item.focus_id for item in candidates],
            "candidate_revisions": {item.focus_id: item.revision for item in candidates},
            "clarification_question": question,
        }
        with Session(self._store.engine) as session, session.begin():
            review = AcademicRepository.resume_discourse_session(
                session,
                session_id=session_id,
                now=received_at,
                partial_state={
                    "pending_action": pending,
                    "candidate_focus_ids": pending["candidate_focus_ids"],
                    "candidate_revisions": pending["candidate_revisions"],
                    "clarification_question": question,
                },
            )
            if review.state != "open":
                return AcademicMemoryHandleResult(status="not_applicable")
        return AcademicMemoryHandleResult(status="clarification", response=question)

    def _persist_model_clarification(
        self,
        *,
        session_id: uuid.UUID,
        focuses: Sequence[AcademicLearningFocusOption],
        decision: AcademicMemoryReviewDecision,
        external_event_id: str,
        received_at: datetime,
    ) -> AcademicMemoryHandleResult:
        del external_event_id
        selected = tuple(
            item
            for item in focuses
            if decision.focus_id is None or item.focus_id == decision.focus_id
        )
        question = decision.clarification_question or _candidate_question(selected or focuses)
        if _UUID_TEXT.search(question):
            question = _candidate_question(selected or focuses)
        with Session(self._store.engine) as session, session.begin():
            AcademicRepository.resume_discourse_session(
                session,
                session_id=session_id,
                now=received_at,
                partial_state={
                    "pending_action": None,
                    "candidate_focus_ids": [item.focus_id for item in selected],
                    "candidate_revisions": {item.focus_id: item.revision for item in selected},
                    "clarification_question": question,
                },
            )
        return AcademicMemoryHandleResult(status="clarification", response=question)

    def _apply_delete(
        self,
        *,
        session_id: uuid.UUID,
        focuses: Sequence[AcademicLearningFocusOption],
        focus_id: str,
        external_event_id: str,
        channel_id: str,
        user_id: str,
        received_at: datetime,
    ) -> AcademicMemoryHandleResult:
        selected = next((item for item in focuses if item.focus_id == focus_id), None)
        if selected is None:
            return AcademicMemoryHandleResult(
                status="no_change",
                response="I could not verify that learning focus, so nothing was changed.",
            )
        with Session(self._store.engine) as session, session.begin():
            status = AcademicRepository.hard_delete_owned_learning_focus(
                session,
                focus_id=uuid.UUID(selected.focus_id),
                owner_user_id=user_id,
                owner_channel_id=channel_id,
                expected_revision=selected.revision,
            )
            if status == "applied":
                AcademicRepository.complete_discourse_session(
                    session,
                    session_id=session_id,
                    completed_at=received_at,
                    final_state=_cleared_review_state(external_event_id),
                )
        if status != "applied":
            return self._memory_changed(
                session_id=session_id,
                channel_id=channel_id,
                user_id=user_id,
                received_at=received_at,
            )
        return AcademicMemoryHandleResult(
            status="applied",
            response=f"Deleted {selected.topic} and its stored reflection memory.",
        )

    async def _apply_replacement(
        self,
        *,
        session_id: uuid.UUID,
        focuses: Sequence[AcademicLearningFocusOption],
        decision: AcademicMemoryReviewDecision,
        external_event_id: str,
        channel_id: str,
        user_id: str,
        raw_text: str,
        received_at: datetime,
    ) -> AcademicMemoryHandleResult:
        selected = next((item for item in focuses if item.focus_id == decision.focus_id), None)
        matched = (
            _subject_matches(decision.subject_text, focuses)
            if decision.subject_text is not None
            else ()
        )
        if (
            selected is None
            or decision.replacement_topic is None
            or len(matched) != 1
            or matched[0].focus_id != selected.focus_id
            or _UUID_TEXT.search(decision.replacement_topic) is not None
        ):
            return AcademicMemoryHandleResult(
                status="no_change",
                response="I could not verify that correction, so nothing was changed.",
            )
        embedding_result = await self._embedding_gateway.embed_reflection_text(raw_text)
        vector = (
            embedding_result.embedding.vector
            if embedding_result.status is EmbeddingStatus.VALID
            and embedding_result.embedding is not None
            else None
        )
        memory = _memory_input(
            raw_text,
            vector,
            embedding_result,
            summary=f"Corrected academic learning focus: {decision.replacement_topic}",
        )
        updated_topic = decision.replacement_topic
        updated_minutes = (
            decision.target_minutes or selected.target_minutes or self._default_practice_minutes
        )
        with Session(self._store.engine) as session, session.begin():
            current_focus = session.get(AcademicLearningFocus, uuid.UUID(selected.focus_id))
            course_id = current_focus.course_id if current_focus is not None else None
            assessment_id = current_focus.assessment_id if current_focus is not None else None
            course_code = decision.replacement_course_code or selected.course_code
            if (
                decision.replacement_course_code is not None
                and decision.replacement_course_code != selected.course_code
            ):
                matches = list(
                    session.scalars(
                        select(Course)
                        .where(
                            Course.course_code == decision.replacement_course_code,
                            Course.active.is_(True),
                        )
                        .limit(2)
                    )
                )
                course_id = matches[0].id if len(matches) == 1 else None
                assessment_id = None
            status, updated = AcademicRepository.replace_owned_learning_focus(
                session,
                focus_id=uuid.UUID(selected.focus_id),
                owner_user_id=user_id,
                owner_channel_id=channel_id,
                expected_revision=selected.revision,
                topic=decision.replacement_topic,
                raw_text=raw_text,
                memory=memory,
                now=received_at,
                source_session_id=session_id,
                external_event_id=f"{external_event_id}:rewrite",
                next_review_at=self._next_daily_review(received_at),
                practice_due_on=(received_at.astimezone(self._zone).date() + timedelta(days=1)),
                practice_minutes=updated_minutes,
                course_id=course_id,
                assessment_id=assessment_id,
                course_code=course_code,
                actor=f"discord:{user_id}",
            )
            if status == "applied" and updated is not None:
                AcademicRepository.complete_discourse_session(
                    session,
                    session_id=session_id,
                    completed_at=received_at,
                    final_state=_cleared_review_state(external_event_id),
                )
        if status != "applied" or updated is None:
            return self._memory_changed(
                session_id=session_id,
                channel_id=channel_id,
                user_id=user_id,
                received_at=received_at,
            )
        return AcademicMemoryHandleResult(
            status="applied",
            response=(
                f"Updated that learning focus to {updated_topic}. I cleared the stale reflection "
                f"memory and scheduled a separate {updated_minutes}-minute practice "
                "block for tomorrow."
            ),
        )

    def _apply_reinforcement(
        self,
        *,
        session_id: uuid.UUID,
        focuses: Sequence[AcademicLearningFocusOption],
        decision: AcademicMemoryReviewDecision,
        external_event_id: str,
        channel_id: str,
        user_id: str,
        raw_text: str,
        received_at: datetime,
    ) -> AcademicMemoryHandleResult:
        del raw_text
        selected = next((item for item in focuses if item.focus_id == decision.focus_id), None)
        if selected is None:
            return AcademicMemoryHandleResult(
                status="no_change", response="I could not verify that learning focus."
            )
        updated_topic = selected.topic
        updated_minutes = decision.target_minutes or selected.target_minutes
        try:
            with Session(self._store.engine) as session, session.begin():
                AcademicRepository.reinforce_learning_focus(
                    session,
                    focus_id=uuid.UUID(selected.focus_id),
                    now=received_at,
                    source_session_id=session_id,
                    external_event_id=f"{external_event_id}:reinforce",
                    next_review_at=self._next_daily_review(received_at),
                    practice_due_on=(received_at.astimezone(self._zone).date() + timedelta(days=1)),
                    practice_minutes=updated_minutes,
                    actor=f"discord:{user_id}",
                    owner_user_id=user_id,
                    owner_channel_id=channel_id,
                    expected_revision=selected.revision,
                )
                AcademicRepository.complete_discourse_session(
                    session,
                    session_id=session_id,
                    completed_at=received_at,
                    final_state=_cleared_review_state(external_event_id),
                )
        except NoResultFound:
            return self._memory_changed(
                session_id=session_id,
                channel_id=channel_id,
                user_id=user_id,
                received_at=received_at,
            )
        return AcademicMemoryHandleResult(
            status="applied",
            response=(
                f"Kept {updated_topic} active and scheduled a separate "
                f"{updated_minutes}-minute practice block for tomorrow."
            ),
        )

    def _memory_changed(
        self,
        *,
        session_id: uuid.UUID,
        channel_id: str,
        user_id: str,
        received_at: datetime,
    ) -> AcademicMemoryHandleResult:
        raw_focuses, truncated = self._store.list_memory_focuses_for_owner(
            owner_user_id=user_id,
            owner_channel_id=channel_id,
        )
        focuses = tuple(AcademicLearningFocusOption.model_validate(item) for item in raw_focuses)
        question = (
            "That memory changed after I showed it to you, so I did not modify it. "
            "Please name the current course topic you want to change."
        )
        with Session(self._store.engine) as session, session.begin():
            AcademicRepository.resume_discourse_session(
                session,
                session_id=session_id,
                now=received_at,
                partial_state={
                    "focuses": [item.model_dump(mode="json") for item in focuses],
                    "pending_action": None,
                    "candidate_focus_ids": [],
                    "candidate_revisions": {},
                    "clarification_question": question,
                    "memory_set_truncated": truncated,
                },
            )
        return AcademicMemoryHandleResult(status="clarification", response=question)

    def _close_without_change(
        self,
        *,
        session_id: uuid.UUID,
        external_event_id: str,
        received_at: datetime,
        response: str,
    ) -> AcademicMemoryHandleResult:
        with Session(self._store.engine) as session, session.begin():
            AcademicRepository.complete_discourse_session(
                session,
                session_id=session_id,
                completed_at=received_at,
                final_state=_cleared_review_state(external_event_id),
            )
        return AcademicMemoryHandleResult(status="no_change", response=response)

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
                session_kind="learning_focus",
            )
            open_session_id = open_session.id if open_session is not None else None
            open_partial_state = dict(open_session.partial_state) if open_session else None
            continuation = _continuation(open_partial_state)

        result = await run_academic_discourse_loop(
            gateway=self._model_gateway,
            catalog=_OwnerScopedCatalog(
                self._store,
                owner_user_id=user_id,
                owner_channel_id=channel_id,
            ),
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
                        owner_user_id=user_id,
                        owner_channel_id=channel_id,
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
                    if (
                        existing is None
                        or existing.owner_user_id != user_id
                        or existing.owner_channel_id != channel_id
                    ):
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
                        owner_user_id=user_id,
                        owner_channel_id=channel_id,
                        expected_revision=existing.revision,
                    )
                    responses.append(
                        f"Kept {focus.course_code + ' ' if focus.course_code else ''}{focus.topic} "
                        f"active. Tomorrow's planner will reserve a separate {minutes}-minute "
                        "practice block."
                    )
                    continue
                if isinstance(action, ResolveLearningFocusAction):
                    existing = session.get(AcademicLearningFocus, focus_id)
                    if (
                        existing is None
                        or existing.owner_user_id != user_id
                        or existing.owner_channel_id != channel_id
                    ):
                        raise ValueError("verified learning focus no longer exists")
                    label = existing.topic
                    status = AcademicRepository.hard_delete_owned_learning_focus(
                        session,
                        focus_id=focus_id,
                        owner_user_id=user_id,
                        owner_channel_id=channel_id,
                        expected_revision=existing.revision,
                    )
                    if status != "applied":
                        raise ValueError("verified learning focus changed")
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
                    owner_user_id=user_id,
                    owner_channel_id=channel_id,
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


def _normalized_phrase(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9']+", " ", value.casefold()).split())


def _is_memory_view_intent(normalized: str) -> bool:
    if normalized in _VIEW_PHRASES:
        return True
    return bool(
        re.fullmatch(
            r"(?:please )?(?:show|tell) me (?:what you remember|my (?:academic )?learning focuses)"
            r"(?: about my coursework)?",
            normalized,
        )
    )


def _is_subjectless_delete(normalized: str) -> bool:
    return normalized in {
        "i am no longer struggling",
        "i'm no longer struggling",
        "im no longer struggling",
        "i am not struggling anymore",
        "i'm not struggling anymore",
        "not struggling anymore",
    }


def _review_focuses(state: Mapping[str, Any]) -> tuple[AcademicLearningFocusOption, ...]:
    values = state.get("focuses", ())
    if not isinstance(values, list):
        return ()
    typed_values = cast(list[Any], values)
    validated: list[AcademicLearningFocusOption] = []
    for value in typed_values[:20]:
        try:
            validated.append(AcademicLearningFocusOption.model_validate(value))
        except ValueError:
            continue
    return tuple(validated)


def _candidate_question(focuses: Sequence[AcademicLearningFocusOption]) -> str:
    if not focuses:
        return "Which course topic do you mean?"
    if len(focuses) == 1:
        return f"Do you mean {focuses[0].topic}?"
    labels = ", ".join(
        f"{item.course_code + ' ' if item.course_code else ''}{item.topic}" for item in focuses[:5]
    )
    return f"Which learning focus do you mean: {labels}?"


def _subject_matches(
    subject: str, focuses: Sequence[AcademicLearningFocusOption]
) -> tuple[AcademicLearningFocusOption, ...]:
    normalized_subject = _normalized_phrase(subject)
    matches: list[AcademicLearningFocusOption] = []
    for focus in focuses:
        topic = _normalized_phrase(focus.topic)
        course_topic = _normalized_phrase(f"{focus.course_code or ''} {focus.topic}")
        if f" {topic} " in f" {normalized_subject} " or normalized_subject in {topic, course_topic}:
            matches.append(focus)
    return tuple(matches)


def _fallback_summary(focuses: Sequence[AcademicLearningFocusOption], *, truncated: bool) -> str:
    clauses: list[str] = []
    for focus in focuses:
        label = f"{focus.course_code + ' ' if focus.course_code else ''}{focus.topic}"
        status = "active" if focus.status is LearningFocusStatus.ACTIVE else "snoozed"
        timing = (
            f" with {focus.target_minutes}-minute practice blocks" if focus.target_minutes else ""
        )
        clauses.append(f"{label} is {status}{timing}")
    text = "Your stored academic learning focuses are: " + "; ".join(clauses) + "."
    if truncated:
        text += " This is a bounded summary; additional focuses were not included."
    return text


def _cleared_review_state(external_event_id: str) -> dict[str, Any]:
    return {
        "focuses": [],
        "pending_action": None,
        "candidate_focus_ids": [],
        "candidate_revisions": {},
        "clarification_question": None,
        "inbound_event_ids": [external_event_id],
    }


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
