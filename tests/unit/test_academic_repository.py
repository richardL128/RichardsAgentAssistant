"""Focused Phase 5 persistence tests using SQLite as a fast transaction seam."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db.academic import (
    AcademicRepository,
    DocumentChunkInput,
    SourceCitation,
    SQLAlchemyAcademicPlannerStore,
)
from app.db.models import (
    AcademicCheckIn,
    AcademicDocumentChunk,
    AcademicProposedChange,
    AcademicSyncCursor,
    Assessment,
    Base,
    StudyBlock,
    StudyPlan,
)


@pytest.fixture
def engine(tmp_path: Path):
    created = create_engine(f"sqlite+pysqlite:///{tmp_path / 'academic.db'}")
    Base.metadata.create_all(created)
    try:
        yield created
    finally:
        created.dispose()


def _course(engine) -> UUID:
    with Session(engine) as session, session.begin():
        return AcademicRepository.upsert_course(
            session,
            notion_id="notion-course-1",
            course_code="HIST-201",
            title="Research Methods",
            term="2026-fall",
            priority=80,
        ).id


def test_delta_upserts_preserve_typed_citations_and_ambiguity(engine) -> None:
    course = _course(engine)
    due = datetime(2026, 10, 14, 15, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        first = AcademicRepository.upsert_assessment(
            session,
            notion_id="notion-assignment-1",
            course_id=course,
            title="Research brief",
            assessment_type="assignment",
            due_at=due,
            grade_weight_percent=15,
            estimated_minutes=180,
            confidence_gap=0.2,
            scope_size=3,
            scope="Two primary sources and one course reading.",
            confidence=0.9,
            fact_state="confirmed",
            citation=SourceCitation(page=3, block="assessment-table", url="https://example.test"),
        )
        second = AcademicRepository.upsert_assessment(
            session,
            notion_id="notion-assignment-1",
            course_id=course,
            title="Research brief revised",
            assessment_type="assignment",
            due_at=due,
            grade_weight_percent=15,
            estimated_minutes=240,
            confidence_gap=0.1,
            scope_size=4,
            scope="Updated scope.",
            confidence=0.95,
            fact_state="confirmed",
            citation=SourceCitation(page=4, block="revised-table"),
        )
        ambiguous = AcademicRepository.upsert_assessment(
            session,
            notion_id="notion-quiz-1",
            course_id=course,
            title="Quiz",
            assessment_type="quiz",
            due_at=None,
            grade_weight_percent=5,
            estimated_minutes=30,
            confidence=0.2,
            fact_state="ambiguous",
            ambiguity_reason="Outline lists two contradictory dates.",
            citation=SourceCitation(page=3, block="schedule"),
        )
        assert first.id == second.id
        assert second.estimated_minutes == 240
        assert ambiguous.fact_state == "ambiguous"
        revised_id = second.id

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Assessment)) == 2
        stored = session.get(Assessment, revised_id)
        assert stored is not None
        assert stored.source_page == 4
        facts = SQLAlchemyAcademicPlannerStore(engine).load_planner_facts(
            now=datetime(2026, 10, 1, tzinfo=UTC), horizon_days=14
        )
    assert [assessment.id for assessment in facts.assessments] == ["notion-assignment-1"]
    assert facts.assessments[0].estimated_minutes == 240
    assert facts.assessments[0].course_priority == 80
    assert [item.id for item in facts.ambiguous_facts] == ["notion-quiz-1"]
    assert facts.ambiguous_facts[0].source_citation == "page 3, block schedule"


def test_document_versions_chunks_search_and_cursor_are_idempotent(engine) -> None:
    course = _course(engine)
    retrieved = datetime(2026, 9, 3, 12, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        document = AcademicRepository.upsert_document(
            session,
            notion_id="notion-outline-1",
            document_version="etag-1",
            title="Course outline",
            document_type="pdf",
            retrieved_at=retrieved,
            artifact_key="a" * 64,
            content_hash="b" * 64,
            course_id=course,
        )
        chunks = AcademicRepository.replace_document_chunks(
            session,
            document_id=document.id,
            chunks=[
                DocumentChunkInput(
                    ordinal=0,
                    heading="Late submission policy",
                    content="Late submissions lose ten percent per day.",
                    citation=SourceCitation(page=7),
                ),
                DocumentChunkInput(
                    ordinal=1,
                    heading="Exams",
                    content="The final exam is held in December.",
                    citation=SourceCitation(page=10, block="exam-table"),
                ),
            ],
        )
        cursor = AcademicRepository.upsert_sync_cursor(
            session,
            scope="notion:academic-databases",
            source_version="notion-v1",
            cursor="next-cursor",
            last_synced_at=retrieved,
        )
        assert len(chunks) == 2
        assert cursor.cursor == "next-cursor"
        assert (
            AcademicRepository.upsert_document(
                session,
                notion_id="notion-outline-1",
                document_version="etag-1",
                title="Course outline updated",
                document_type="pdf",
                retrieved_at=retrieved,
                artifact_key="a" * 64,
                content_hash="c" * 64,
                course_id=course,
            ).id
            == document.id
        )

    with Session(engine) as session:
        found = AcademicRepository.search_document_chunks(
            session, query="late submissions", course_id=course, term="2026-fall"
        )
        assert len(found) == 1
        assert found[0].source_page == 7
        assert session.scalar(select(func.count()).select_from(AcademicDocumentChunk)) == 2
        assert session.scalar(select(func.count()).select_from(AcademicSyncCursor)) == 1
        row = session.get(Assessment, uuid4())
        assert row is None


def test_plan_replay_and_incomplete_carry_forward_are_visible(engine) -> None:
    starts = datetime(2026, 9, 4, 14, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        source = AcademicRepository.upsert_study_plan(
            session,
            plan_key="plan-2026-09-04",
            starts_on=date(2026, 9, 4),
            ends_on=date(2026, 9, 10),
            timezone="America/Toronto",
        )
        target = AcademicRepository.upsert_study_plan(
            session,
            plan_key="plan-2026-09-11",
            starts_on=date(2026, 9, 11),
            ends_on=date(2026, 9, 17),
            timezone="America/Toronto",
        )
        block = AcademicRepository.upsert_study_block(
            session,
            plan_id=source.id,
            block_key="assessment-1-block-1",
            title="Carry this work",
            starts_at=starts,
            ends_at=starts + timedelta(minutes=60),
            allocated_minutes=60,
            status="incomplete",
            notes="20 minutes remain.",
        )
        carried = AcademicRepository.carry_forward_incomplete_blocks(
            session,
            source_plan_id=source.id,
            target_plan_id=target.id,
            starts_at=datetime(2026, 9, 11, 14, tzinfo=UTC),
        )
        replay = AcademicRepository.carry_forward_incomplete_blocks(
            session,
            source_plan_id=source.id,
            target_plan_id=target.id,
            starts_at=datetime(2026, 9, 11, 14, tzinfo=UTC),
        )
        assert len(carried) == len(replay) == 1
        assert carried[0].id == replay[0].id
        assert carried[0].carry_forward_from_id == block.id
        assert carried[0].status == "carried_forward"
        assert carried[0].ends_at > carried[0].starts_at

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(StudyPlan)) == 2
        assert session.scalar(select(func.count()).select_from(StudyBlock)) == 2


def test_checkin_proposal_requires_exact_confirmation_and_is_replay_safe(engine) -> None:
    token = "CONFIRM ACADEMIC exact-token"
    with Session(engine) as session, session.begin():
        checkin = AcademicRepository.create_checkin(
            session,
            idempotency_key="discord:event-1",
            external_event_id="event-1",
            channel="discord",
            received_at=datetime(2026, 9, 3, 21, tzinfo=UTC),
            redacted_summary="One completion update proposed.",
            status="proposal_pending",
        )
        proposal = AcademicRepository.create_proposed_change(
            session,
            checkin_id=checkin.id,
            idempotency_key="proposal-1",
            operation="notion_update",
            target_type="assessment",
            target_id="notion-assignment-1",
            payload={"completed": True},
            redacted_preview="Mark the assessment complete.",
            confirmation_token=token,
        )
        replay = AcademicRepository.create_proposed_change(
            session,
            checkin_id=checkin.id,
            idempotency_key="proposal-1",
            operation="notion_update",
            target_type="assessment",
            target_id="notion-assignment-1",
            payload={"completed": True},
            redacted_preview="Mark the assessment complete.",
            confirmation_token=token,
        )
        assert replay.id == proposal.id
        denied, _ = AcademicRepository.begin_confirmed_change(
            session,
            proposal_id=proposal.id,
            confirmation_event="CONFIRM ACADEMIC exact",
        )
        assert denied == "confirmation_required"
        ready, claimed = AcademicRepository.begin_confirmed_change(
            session, proposal_id=proposal.id, confirmation_event=token
        )
        assert ready == "ready"
        assert claimed.state == "applying"
        in_progress, _ = AcademicRepository.begin_confirmed_change(
            session, proposal_id=proposal.id, confirmation_event=token
        )
        assert in_progress == "in_progress"
        applied = AcademicRepository.mark_proposed_change_applied(
            session, proposal_id=proposal.id, confirmation_event=token
        )
        assert applied.state == "applied"
        assert (
            AcademicRepository.mark_proposed_change_applied(
                session, proposal_id=proposal.id, confirmation_event=token
            ).id
            == proposal.id
        )
        already_applied, _ = AcademicRepository.begin_confirmed_change(
            session, proposal_id=proposal.id, confirmation_event=token
        )
        assert already_applied == "already_applied"

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AcademicCheckIn)) == 1
        assert session.scalar(select(func.count()).select_from(AcademicProposedChange)) == 1
