"""Focused Phase 5 persistence tests using SQLite as a fast transaction seam."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db.academic import (
    AcademicRepository,
    AssessmentSourceTrace,
    ClarificationInput,
    CourseCalendarInput,
    DocumentChunkInput,
    SourceCitation,
    SQLAlchemyAcademicPlannerStore,
)
from app.db.models import (
    AcademicCheckIn,
    AcademicClarification,
    AcademicCourseCalendar,
    AcademicDocumentChunk,
    AcademicProposedChange,
    AcademicSetupReminder,
    AcademicSyncCursor,
    Assessment,
    AuditEvent,
    Base,
    Course,
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


def test_course_calendar_and_source_scoped_assessment_reconciliation(engine) -> None:
    course = _course(engine)
    edited = datetime(2026, 9, 5, 14, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        calendar = AcademicRepository.upsert_course_calendar(
            session,
            calendar=CourseCalendarInput(
                course_id=course,
                course_page_id="course-page-1",
                child_database_id="child-db-1",
                child_data_source_id="source-1",
                title_property_id="title-prop",
                title_property_name="Name",
                date_property_id="date-prop",
                date_property_name="Date",
                last_discovered_at=edited,
                last_synced_at=edited,
            ),
        )
        first = AcademicRepository.upsert_assessment(
            session,
            notion_id="event-1",
            course_id=course,
            title="Chapter 4",
            assessment_type="quiz",
            due_at=edited + timedelta(days=1),
            grade_weight_percent=None,
            confidence=1,
            fact_state="confirmed",
            citation=SourceCitation(url="https://notion.test/page/event-1"),
            trace=AssessmentSourceTrace(
                source_id="source-1",
                source_scope="notion:source-1",
                notion_last_edited_at=edited,
                title_property_id="title-prop",
                label_source="explicit:quiz",
                source_url="https://notion.test/page/event-1",
            ),
        )
        second = AcademicRepository.upsert_assessment(
            session,
            notion_id="event-2",
            course_id=course,
            title="Essay",
            assessment_type="assignment",
            due_at=edited + timedelta(days=2),
            grade_weight_percent=None,
            confidence=1,
            fact_state="confirmed",
            citation=SourceCitation(),
            trace=AssessmentSourceTrace(
                source_id="source-1",
                source_scope="notion:source-1",
                notion_last_edited_at=edited,
                title_property_id="title-prop",
            ),
        )
        archived = AcademicRepository.reconcile_assessment_source(
            session,
            source_id="source-1",
            seen_notion_ids=["event-1"],
            synced_at=edited + timedelta(minutes=1),
        )

        assert calendar.discovery_status == "valid"
        assert first.title_property_id == "title-prop"
        assert archived == 1
        assert second.archived is True
        assert second.active is False

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AcademicCourseCalendar)) == 1
        active = session.scalar(select(Assessment).where(Assessment.notion_id == "event-1"))
        assert active is not None
        assert active.active is True


def test_store_accepts_connector_aliases_for_calendar_and_assessment(engine) -> None:
    store = SQLAlchemyAcademicPlannerStore(engine)
    course = {
        "course_id": "course-page-alias",
        "course_code": "HIST-202",
        "course_title": "Modern History",
        "term": "2026-fall",
        "assessments_database_id": "child-db-alias",
        "assessments_source_id": "source-alias",
        "title_property_id": "title-prop",
        "date_property_id": "date-prop",
    }
    calendar_id = store.upsert_course_calendar(
        course,
        status="valid",
        schema_fingerprint="schema-alias",
    )
    assessment_id = store.upsert_synced_assessment(
        course,
        {
            "notion_id": "event-alias",
            "title": "Chapter 5",
            "due_at": "2026-09-07T15:00:00+00:00",
            "assessments_source_id": "source-alias",
            "title_property_id": "title-prop",
            "notion_last_edited_at": "2026-09-06T12:00:00+00:00",
        },
        kind="quiz",
        label_source="explicit:quiz",
    )

    assert UUID(calendar_id)
    assert UUID(assessment_id)
    with Session(engine) as session:
        calendar = session.scalar(select(AcademicCourseCalendar))
        assessment = session.scalar(select(Assessment).where(Assessment.notion_id == "event-alias"))
        assert calendar is not None
        assert calendar.child_database_id == "child-db-alias"
        assert calendar.child_data_source_id == "source-alias"
        course_row = session.scalar(select(Course).where(Course.notion_id == "course-page-alias"))
        assert assessment is not None
        assert course_row is not None
        assert course_row.title == "Modern History"
        assert assessment.source_id == "source-alias"
        assert assessment.label_source == "explicit:quiz"


def test_clarification_claims_ignore_and_write_states_are_replay_safe(engine) -> None:
    course = _course(engine)
    expected = datetime(2026, 9, 5, 15, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        request = ClarificationInput(
            course_id=course,
            event_notion_id="event-ambiguous",
            original_title="Chapter 4",
            raw_label="assigment?",
            quiz_preview_title="Quiz - Chapter 4",
            assignment_preview_title="Assignment - Chapter 4",
            expected_edited_at=expected,
            expires_at=expected + timedelta(hours=24),
            idempotency_key="clarification:event-ambiguous:v1",
            title_property_id="title-prop",
        )
        row = AcademicRepository.create_or_get_clarification(session, request=request)
        replay = AcademicRepository.create_or_get_clarification(
            session,
            request=replace(request, expires_at=request.expires_at + timedelta(days=1)),
        )
        assert replay.id == row.id
        assert replay.expires_at == request.expires_at
        AcademicRepository.mark_clarification_delivered(
            session,
            clarification_id=row.id,
            delivery_id="discord-message-1",
            delivered_at=expected + timedelta(minutes=1),
        )
        status, claimed = AcademicRepository.claim_clarification(
            session,
            clarification_id=row.id,
            action="assignment",
            actor_id=123456789,
            now=expected + timedelta(minutes=2),
        )
        assert status == "ready"
        assert claimed.write_status == "pending"
        assert claimed.decision_user_id == 123456789
        replay_status, _ = AcademicRepository.claim_clarification(
            session,
            clarification_id=row.id,
            action="assignment",
            actor_id=123456789,
            now=expected + timedelta(minutes=3),
        )
        assert replay_status == "claimed"
        AcademicRepository.mark_clarification_conflict(session, clarification_id=row.id)
        assert (
            AcademicRepository.claim_clarification(
                session,
                clarification_id=row.id,
                action="assignment",
                actor_id=123456789,
                now=expected + timedelta(minutes=4),
            )[0]
            == "conflict"
        )

        ignore = AcademicRepository.create_or_get_clarification(
            session,
            request=ClarificationInput(
                course_id=course,
                event_notion_id="event-ignore",
                original_title="Reading",
                quiz_preview_title="Quiz - Reading",
                assignment_preview_title="Assignment - Reading",
                expected_edited_at=expected,
                expires_at=expected + timedelta(hours=24),
                idempotency_key="clarification:event-ignore:v1",
            ),
        )
        ignore_status, ignored = AcademicRepository.claim_clarification(
            session,
            clarification_id=ignore.id,
            action="ignore",
            actor_id=123456789,
            now=expected + timedelta(minutes=2),
        )
        assert ignore_status == "ignored"
        assert ignored.write_status == "skipped"

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AcademicClarification)) == 2
        assert set(session.scalars(select(AuditEvent.result))) == {"conflict", "skipped"}


def test_store_clarification_claim_returns_ready_once_then_claimed(engine) -> None:
    store = SQLAlchemyAcademicPlannerStore(engine)
    expected = datetime(2026, 9, 5, 15, tzinfo=UTC)
    clarification_id = store.create_or_get_clarification(
        event_notion_id="event-ready",
        original_title="Chapter 7",
        raw_label="unknown",
        quiz_preview_title="Quiz - Chapter 7",
        assignment_preview_title="Assignment - Chapter 7",
        expected_edited_at=expected,
        expires_at=expected + timedelta(hours=24),
        idempotency_key="clarification:event-ready:v1",
        title_property_id="title-prop",
    )

    first, first_row = store.claim_clarification(
        clarification_id,
        "quiz",
        123456789,
        now=expected + timedelta(minutes=1),
    )
    second, second_row = store.claim_clarification(
        clarification_id,
        "quiz",
        123456789,
        now=expected + timedelta(minutes=2),
    )

    assert first == "ready"
    assert first_row["write_status"] == "pending"
    assert second == "claimed"
    assert second_row["decision"] == "quiz"


def test_store_reads_and_expires_clarifications_without_secret_payloads(engine) -> None:
    store = SQLAlchemyAcademicPlannerStore(engine)
    expected = datetime(2026, 9, 5, 15, tzinfo=UTC)
    clarification_id = store.create_or_get_clarification(
        event_notion_id="event-store",
        original_title="Chapter 6",
        raw_label="unknown",
        quiz_preview_title="Quiz - Chapter 6",
        assignment_preview_title="Assignment - Chapter 6",
        expected_edited_at=expected - timedelta(days=2),
        expires_at=expected - timedelta(days=1),
        idempotency_key="clarification:event-store:v1",
        title_property_id="title-prop",
    )

    store.mark_clarification_delivered(
        clarification_id,
        delivery_id="discord-message-store",
        delivered_at=expected - timedelta(days=1, minutes=1),
    )
    delivered = store.get_clarification(clarification_id)
    assert delivered is not None
    assert delivered["delivery_id"] == "discord-message-store"
    assert delivered["delivered_at"] is not None
    assert "unknown" not in str(delivered)

    assert store.expire_clarifications(now=expected) == 1
    expired = store.get_clarification(clarification_id)
    assert expired is not None
    assert expired["state"] == "expired"


def test_clarification_expiry_reminders_and_health_snapshot(engine) -> None:
    store = SQLAlchemyAcademicPlannerStore(engine)
    course_id = _course(engine)
    expected = datetime(2026, 9, 5, 15, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        AcademicRepository.upsert_course_calendar(
            session,
            calendar=CourseCalendarInput(
                course_id=course_id,
                course_page_id="course-page-1",
                child_database_id=None,
                child_data_source_id=None,
                discovery_status="missing",
                diagnostic_code="missing_assessments_calendar",
                diagnostic_fingerprint="schema-v1",
            ),
        )
        expired = AcademicRepository.create_or_get_clarification(
            session,
            request=ClarificationInput(
                course_id=course_id,
                event_notion_id="event-expired",
                original_title="Reading",
                quiz_preview_title="Quiz - Reading",
                assignment_preview_title="Assignment - Reading",
                expected_edited_at=expected - timedelta(days=2),
                expires_at=expected - timedelta(days=1),
                idempotency_key="clarification:event-expired:v1",
            ),
        )
        assert expired.state == "pending"
        assert AcademicRepository.expire_clarifications(session, now=expected) == 1
        reminder_day = date(2026, 9, 5)
        assert AcademicRepository.setup_reminder_due(
            session,
            condition_code="missing_assessments_calendar",
            fingerprint="schema-v1",
            reminder_day=reminder_day,
        )
        AcademicRepository.record_setup_reminder(
            session,
            condition_code="missing_assessments_calendar",
            fingerprint="schema-v1",
            reminder_day=reminder_day,
            affected_course_codes=["HIST-201"],
            delivered_at=expected,
            delivery_id="discord-reminder-1",
        )
        assert not AcademicRepository.setup_reminder_due(
            session,
            condition_code="missing_assessments_calendar",
            fingerprint="schema-v1",
            reminder_day=reminder_day,
        )
        AcademicRepository.record_setup_reminder(
            session,
            condition_code="inaccessible_courses_database",
            fingerprint="schema-v2",
            reminder_day=reminder_day,
            error_code="discord_unavailable",
        )
        assert not AcademicRepository.setup_reminder_due(
            session,
            condition_code="inaccessible_courses_database",
            fingerprint="schema-v2",
            reminder_day=reminder_day,
        )

    snapshot = store.academic_notion_health()
    assert snapshot["invalid_calendar_count"] == 1
    assert snapshot["setup_reminder_count"] == 2
    assert snapshot["pending_clarification_count"] == 0

    assert store.clear_setup_reminders("missing_assessments_calendar", "schema-v1") == 1
    with Session(engine) as session:
        active_reminders = session.scalar(
            select(func.count())
            .select_from(AcademicSetupReminder)
            .where(AcademicSetupReminder.state != "cleared")
        )
        assert active_reminders == 1


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
