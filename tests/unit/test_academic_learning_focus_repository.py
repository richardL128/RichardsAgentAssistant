"""Focused persistence tests for academic learning focus memory."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db.academic import AcademicRepository, LearningFocusMemoryInput
from app.db.models import (
    AcademicDiscourseSession,
    AcademicLearningFocus,
    AcademicLearningFocusEvent,
    AcademicReflectionMemory,
    Base,
    StudyBlock,
)


@pytest.fixture
def engine(tmp_path: Path):
    created = create_engine(f"sqlite+pysqlite:///{tmp_path / 'learning-focus.db'}")
    Base.metadata.create_all(created)
    try:
        yield created
    finally:
        created.dispose()


def test_discourse_session_is_idempotent_resumable_completable_and_expirable(engine) -> None:
    started = datetime(2026, 9, 7, 21, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        first = AcademicRepository.create_discourse_session(
            session,
            external_event_id="discord-session-1",
            started_at=started,
            partial_state={"step": "extracting_focus"},
            expires_at=started + timedelta(minutes=30),
        )
        replayed = AcademicRepository.create_discourse_session(
            session,
            external_event_id="discord-session-1",
            started_at=started + timedelta(minutes=1),
            partial_state={"step": "different"},
        )
        resumed = AcademicRepository.resume_discourse_session(
            session,
            session_id=first.id,
            now=started + timedelta(minutes=5),
            partial_state={"course_code": "ECE 250"},
        )
        resumed_state = dict(resumed.partial_state)
        completed = AcademicRepository.complete_discourse_session(
            session,
            session_id=first.id,
            completed_at=started + timedelta(minutes=6),
            final_state={"topic": "recursion"},
        )

        assert replayed.id == first.id
        assert resumed_state == {
            "step": "extracting_focus",
            "course_code": "ECE 250",
        }
        assert completed.state == "completed"
        assert completed.partial_state["topic"] == "recursion"

    with Session(engine) as session, session.begin():
        AcademicRepository.create_discourse_session(
            session,
            external_event_id="discord-session-expired",
            started_at=started,
            expires_at=started + timedelta(minutes=1),
        )
        expired_count = AcademicRepository.expire_discourse_sessions(
            session,
            now=started + timedelta(minutes=2),
        )

    with Session(engine) as session:
        assert expired_count == 1
        assert session.scalar(select(func.count()).select_from(AcademicDiscourseSession)) == 2
        expired = session.scalar(
            select(AcademicDiscourseSession).where(
                AcademicDiscourseSession.external_event_id == "discord-session-expired"
            )
        )
        assert expired is not None
        assert expired.state == "expired"


def test_learning_focus_create_reinforce_due_listing_and_hard_delete(engine) -> None:
    now = datetime(2026, 9, 7, 22, tzinfo=UTC)
    review_at = now + timedelta(days=1)
    with Session(engine) as session, session.begin():
        discourse = AcademicRepository.create_discourse_session(
            session,
            external_event_id="discord-focus-session",
            started_at=now,
        )
        focus = AcademicRepository.create_learning_focus(
            session,
            topic="recursion",
            course_code="ECE 250",
            source_session_id=discourse.id,
            source_external_event_id="discord-message-create-focus",
            now=now,
            next_review_at=review_at,
            practice_due_on=date(2026, 9, 8),
            practice_minutes=45,
            memory=LearningFocusMemoryInput(
                raw_text="I really struggled with my ECE 250 quiz on recursion.",
                embedding=[0.1, 0.2, 0.3],
                embedding_model="test-embedding-v1",
                embedding_metadata={"source": "daily_reflection"},
                redacted_summary="Struggled with ECE 250 recursion.",
            ),
        )
        replayed = AcademicRepository.create_learning_focus(
            session,
            topic="ignored replay topic",
            source_external_event_id="discord-message-create-focus",
            now=now + timedelta(minutes=1),
        )

        assert replayed.id == focus.id
        assert replayed.topic == "recursion"
        focus_id = focus.id

    with Session(engine) as session:
        active = AcademicRepository.list_active_learning_focuses(session)
        due_before = AcademicRepository.list_due_learning_focus_reviews(
            session,
            now=review_at - timedelta(seconds=1),
        )
        due_at = AcademicRepository.list_due_learning_focus_reviews(session, now=review_at)
        memory = session.scalar(select(AcademicReflectionMemory))

        assert [row.topic for row in active] == ["recursion"]
        assert due_before == []
        assert [row.id for row in due_at] == [focus_id]
        assert memory is not None
        assert memory.raw_text == "I really struggled with my ECE 250 quiz on recursion."
        assert memory.embedding == [0.1, 0.2, 0.3]
        assert memory.embedding_dimensions == 3
        assert memory.embedding_metadata == {"source": "daily_reflection"}

    with Session(engine) as session, session.begin():
        reinforced = AcademicRepository.reinforce_learning_focus(
            session,
            focus_id=focus_id,
            now=now + timedelta(days=1),
            external_event_id="discord-message-reinforce-focus",
            next_review_at=now + timedelta(days=2),
            practice_due_on=date(2026, 9, 9),
            practice_minutes=30,
            memory=LearningFocusMemoryInput(
                raw_text="I still need recursion practice.",
                embedding=[0.4, 0.5, 0.6],
                embedding_model="test-embedding-v1",
            ),
        )
        replayed_reinforcement = AcademicRepository.reinforce_learning_focus(
            session,
            focus_id=focus_id,
            now=now + timedelta(days=1, minutes=1),
            external_event_id="discord-message-reinforce-focus",
            practice_minutes=999,
        )

        assert reinforced.reinforcement_count == 2
        assert replayed_reinforcement.reinforcement_count == 2
        assert reinforced.practice_due_on == date(2026, 9, 9)
        assert reinforced.practice_minutes == 30

    with Session(engine) as session, session.begin():
        plan = AcademicRepository.upsert_study_plan(
            session,
            plan_key="plan-with-learning-focus-practice",
            starts_on=date(2026, 9, 9),
            ends_on=date(2026, 9, 9),
            timezone="America/Toronto",
            status="published",
        )
        practice = AcademicRepository.upsert_study_block(
            session,
            plan_id=plan.id,
            block_key="focus-practice-recursion",
            title="Practice recursion",
            starts_at=now + timedelta(days=2, hours=9),
            ends_at=now + timedelta(days=2, hours=9, minutes=30),
            allocated_minutes=30,
            learning_focus_id=focus_id,
            block_kind="practice",
        )
        practice_id = practice.id
        deleted = AcademicRepository.hard_delete_learning_focus(session, focus_id=focus_id)

    with Session(engine) as session:
        practice = session.get(StudyBlock, practice_id)
        assert deleted is True
        assert session.get(AcademicLearningFocus, focus_id) is None
        assert practice is not None
        assert practice.learning_focus_id is None
        assert practice.block_kind == "practice"
        assert session.scalar(select(func.count()).select_from(AcademicLearningFocusEvent)) == 0
        assert session.scalar(select(func.count()).select_from(AcademicReflectionMemory)) == 0
        assert session.scalar(select(func.count()).select_from(AcademicDiscourseSession)) == 1


def test_learning_focus_missed_review_progression_snoozes_then_deletes(engine) -> None:
    now = datetime(2026, 9, 7, 22, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        discourse = AcademicRepository.create_discourse_session(
            session,
            external_event_id="discord-tree-session",
            started_at=now,
        )
        focus = AcademicRepository.create_learning_focus(
            session,
            topic="binary trees",
            course_code="ECE 250",
            source_session_id=discourse.id,
            source_external_event_id="discord-message-tree-focus",
            now=now,
            next_review_at=now + timedelta(days=1),
            practice_due_on=date(2026, 9, 8),
            practice_minutes=45,
            memory=LearningFocusMemoryInput(
                raw_text="I am struggling with binary tree recursion.",
                embedding=[1.0],
                embedding_model="test-embedding-v1",
            ),
        )
        focus_id = focus.id
        discourse_id = discourse.id

    with Session(engine) as session, session.begin():
        status_1, focus_1 = AcademicRepository.advance_learning_focus_reminder(
            session,
            focus_id=focus_id,
            now=now + timedelta(days=1),
            next_reminder_at=now + timedelta(days=2),
            external_event_id="discord-reminder-1",
        )
        reminder_1_status = focus_1.status if focus_1 is not None else None
        reminder_1_count = focus_1.reminder_count if focus_1 is not None else None
        status_2, focus_2 = AcademicRepository.advance_learning_focus_reminder(
            session,
            focus_id=focus_id,
            now=now + timedelta(days=2),
            next_reminder_at=now + timedelta(days=3),
            external_event_id="discord-reminder-2",
        )

        assert status_1 == "remind"
        assert reminder_1_status == "active"
        assert reminder_1_count == 1
        assert status_2 == "snoozed_and_remind"
        assert focus_2 is not None
        assert focus_2.status == "snoozed"
        assert focus_2.practice_due_on is None
        assert focus_2.practice_minutes is None

    with Session(engine) as session, session.begin():
        for day in range(3, 6):
            status, row = AcademicRepository.advance_learning_focus_reminder(
                session,
                focus_id=focus_id,
                now=now + timedelta(days=day),
                next_reminder_at=now + timedelta(days=day + 1),
                external_event_id=f"discord-reminder-{day}",
            )
            assert status == "snoozed_and_remind"
            assert row is not None
            assert row.reminder_count == day
        deleted_status, deleted_focus = AcademicRepository.advance_learning_focus_reminder(
            session,
            focus_id=focus_id,
            now=now + timedelta(days=6),
            external_event_id="discord-reminder-delete",
        )

        assert deleted_status == "delete"
        assert deleted_focus is None

    with Session(engine) as session:
        assert session.get(AcademicLearningFocus, focus_id) is None
        assert session.scalar(select(func.count()).select_from(AcademicLearningFocusEvent)) == 0
        assert session.scalar(select(func.count()).select_from(AcademicReflectionMemory)) == 0
        discourse = session.get(AcademicDiscourseSession, discourse_id)
        assert discourse is not None
        assert discourse.missed_review_count == 5
        assert discourse.reminder_count == 5
