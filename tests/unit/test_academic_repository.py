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
    CalendarSemanticResultInput,
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
    AcademicDocument,
    AcademicDocumentChunk,
    AcademicInboundMaterial,
    AcademicProposalOperationJournal,
    AcademicProposedChange,
    AcademicSetupReminder,
    AcademicSyncCursor,
    Assessment,
    AuditEvent,
    Base,
    Course,
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


def _checkin_proposal(
    session: Session,
    *,
    event_id: str = "event-1",
    idempotency_key: str = "proposal-1",
    confirmation_event: str = "CONFIRM ACADEMIC exact-token",
    expires_at: datetime | None = None,
) -> AcademicProposedChange:
    checkin = AcademicRepository.create_checkin(
        session,
        idempotency_key=f"discord:{event_id}",
        external_event_id=event_id,
        channel="discord",
        received_at=datetime(2026, 9, 3, 21, tzinfo=UTC),
        redacted_summary="One completion update proposed.",
        status="proposal_pending",
    )
    return AcademicRepository.create_proposed_change(
        session,
        checkin_id=checkin.id,
        idempotency_key=idempotency_key,
        operation="notion_update",
        target_type="assessment",
        target_id="notion-assignment-1",
        payload={"completed": True},
        redacted_preview="Mark the assessment complete.",
        confirmation_token=confirmation_event,
        expires_at=expires_at,
    )


def _inbound_material_row(
    message_id: str,
    attachment_id: str,
    *,
    owner: str,
    channel: str,
    state: str,
    expires_at: datetime,
    proposal_id: UUID | None = None,
) -> AcademicInboundMaterial:
    suffix = int(attachment_id[-6:])
    return AcademicInboundMaterial(
        discord_message_id=message_id,
        discord_attachment_id=attachment_id,
        owner_discord_user_id=owner,
        discord_channel_id=channel,
        filename=f"material-{suffix}.pdf",
        media_type="application/pdf",
        declared_byte_size=10,
        observed_byte_size=10,
        content_hash=f"{suffix:064x}",
        raw_artifact_key=f"{suffix + 100:064x}",
        state=state,
        proposal_id=proposal_id,
        expires_at=expires_at,
    )


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


def test_assessment_end_range_is_nullable_and_validated(engine) -> None:
    course = _course(engine)
    starts = datetime(2026, 9, 10, 23, tzinfo=UTC)
    ends = starts + timedelta(minutes=45)

    with Session(engine) as session, session.begin():
        timed = AcademicRepository.upsert_assessment(
            session,
            notion_id="notion-study-1",
            course_id=course,
            title="Course Event - Race conditions",
            assessment_type="event",
            due_at=starts,
            ends_at=ends,
            grade_weight_percent=None,
            estimated_minutes=45,
            confidence=1,
            fact_state="confirmed",
            citation=SourceCitation(),
        )
        legacy = AcademicRepository.upsert_assessment(
            session,
            notion_id="notion-assignment-start-only",
            course_id=course,
            title="Start-only assignment",
            assessment_type="assignment",
            due_at=starts,
            grade_weight_percent=10,
            estimated_minutes=60,
            confidence=1,
            fact_state="confirmed",
            citation=SourceCitation(),
        )
        with pytest.raises(ValueError, match="assessment end must be after"):
            AcademicRepository.upsert_assessment(
                session,
                notion_id="notion-invalid-range",
                course_id=course,
                title="Invalid event",
                assessment_type="event",
                due_at=starts,
                ends_at=starts,
                grade_weight_percent=None,
                estimated_minutes=45,
                confidence=1,
                fact_state="confirmed",
                citation=SourceCitation(),
            )
        timed_id = timed.id
        legacy_id = legacy.id

    with Session(engine) as session:
        stored_timed = session.get(Assessment, timed_id)
        stored_legacy = session.get(Assessment, legacy_id)
        assert stored_timed is not None
        assert stored_legacy is not None
        stored_end = stored_timed.ends_at
        assert stored_end is not None
        if stored_end.tzinfo is None:
            stored_end = stored_end.replace(tzinfo=UTC)
        assert stored_end == ends
        assert stored_legacy.ends_at is None


def test_proposal_operation_journal_is_replay_safe_and_receipt_bounded(engine) -> None:
    proposal_id = uuid4()
    payload_hash = "a" * 64

    with Session(engine) as session, session.begin():
        status, row = AcademicRepository.begin_proposal_operation(
            session,
            proposal_id=proposal_id,
            ordinal=0,
            payload_hash=payload_hash,
            operation_id="proposal:0",
        )
        assert status == "ready"
        assert row.state == "in_progress"

        applied = AcademicRepository.mark_proposal_operation_applied(
            session,
            proposal_id=proposal_id,
            ordinal=0,
            payload_hash=payload_hash,
            receipt={
                "proposal_id": "proposal:0",
                "page_id": "page-1",
                "url": "https://example.test/" + ("x" * 2_000),
                "ignored": "secret",
            },
        )
        assert applied.state == "applied"
        assert set(applied.receipt or {}) == {"proposal_id", "page_id", "url"}
        assert len((applied.receipt or {})["url"]) == 1_000

        replay_status, replay_row = AcademicRepository.begin_proposal_operation(
            session,
            proposal_id=proposal_id,
            ordinal=0,
            payload_hash=payload_hash,
            operation_id="proposal:0",
        )
        assert replay_status == "already_applied"
        assert replay_row.id == row.id

    with Session(engine) as session:
        persisted = session.scalar(select(AcademicProposalOperationJournal))
        assert persisted is not None
        assert persisted.state == "applied"
        assert persisted.receipt is not None
        assert persisted.receipt["page_id"] == "page-1"


def test_proposal_operation_journal_blocks_uncertain_replay(engine) -> None:
    proposal_id = uuid4()
    payload_hash = "b" * 64

    with Session(engine) as session, session.begin():
        status, _ = AcademicRepository.begin_proposal_operation(
            session,
            proposal_id=proposal_id,
            ordinal=1,
            payload_hash=payload_hash,
            operation_id="proposal:1",
        )
        assert status == "ready"
        uncertain = AcademicRepository.mark_proposal_operation_uncertain(
            session,
            proposal_id=proposal_id,
            ordinal=1,
            payload_hash=payload_hash,
            error_code="connector_transient",
        )
        assert uncertain.state == "uncertain"

        replay_status, replay = AcademicRepository.begin_proposal_operation(
            session,
            proposal_id=proposal_id,
            ordinal=1,
            payload_hash=payload_hash,
            operation_id="proposal:1",
        )
        assert replay_status == "uncertain"
        assert replay.id == uncertain.id

        with pytest.raises(ValueError, match="payload hash changed"):
            AcademicRepository.begin_proposal_operation(
                session,
                proposal_id=proposal_id,
                ordinal=1,
                payload_hash="c" * 64,
                operation_id="proposal:1",
            )


def test_store_preserves_expanded_todo_types_for_generic_scheduling(engine) -> None:
    from app.agents.academic_planner.contracts import AssessmentType

    course = _course(engine)
    due = datetime(2026, 10, 3, 15, tzinfo=UTC)
    expected_types = {
        "notion-tutorial-1": AssessmentType.TUTORIAL,
        "notion-lab-1": AssessmentType.LAB,
        "notion-event-1": AssessmentType.EVENT,
    }
    with Session(engine) as session, session.begin():
        for index, (notion_id, assessment_type) in enumerate(expected_types.items()):
            AcademicRepository.upsert_assessment(
                session,
                notion_id=notion_id,
                course_id=course,
                title=f"{assessment_type.value} work",
                assessment_type=assessment_type.value,
                due_at=due + timedelta(hours=index),
                grade_weight_percent=None,
                estimated_minutes=45,
                confidence=1,
                fact_state="confirmed",
                citation=SourceCitation(),
            )

    with Session(engine) as session:
        stored = session.scalars(select(Assessment).where(Assessment.notion_id.in_(expected_types)))
        assert {
            assessment.notion_id: AssessmentType(assessment.assessment_type)
            for assessment in stored
        } == expected_types


def test_calendar_availability_includes_fixed_and_timed_calendar_events(engine) -> None:
    course = _course(engine)
    now = datetime(2026, 10, 1, 12, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        AcademicRepository.upsert_fixed_commitment(
            session,
            notion_id="class-ece250",
            title="ECE 250 lecture",
            commitment_type="class",
            starts_at=now + timedelta(hours=1),
            ends_at=now + timedelta(hours=2),
            timezone="America/Toronto",
            citation=SourceCitation(),
            confidence=1,
            fact_state="confirmed",
        )
        AcademicRepository.upsert_assessment(
            session,
            notion_id="event-review",
            course_id=course,
            title="Review graph traversal",
            assessment_type="event",
            due_at=now + timedelta(hours=3),
            ends_at=now + timedelta(hours=4),
            grade_weight_percent=None,
            confidence=1,
            fact_state="confirmed",
            citation=SourceCitation(),
        )
        AcademicRepository.upsert_preferences(
            session,
            scope="owner",
            timezone="America/Toronto",
            availability={
                "windows": [
                    {
                        "start_at": (now + timedelta(minutes=30)).isoformat(),
                        "end_at": (now + timedelta(hours=5)).isoformat(),
                    }
                ]
            },
            daily_capacity_minutes=240,
            buffer_minutes=10,
        )

    facts = SQLAlchemyAcademicPlannerStore(engine).load_calendar_availability(
        now=now,
        horizon_days=7,
    )

    assert [item.id for item in facts.commitments] == ["class-ece250", "event-review"]
    assert len(facts.availability) == 1
    assert facts.buffer_minutes == 10


def test_upcoming_calendar_items_use_local_window_and_semantic_cache(engine) -> None:
    course = _course(engine)
    edited = datetime(2026, 9, 9, 12, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        included_all_day = AcademicRepository.upsert_assessment(
            session,
            notion_id="notion-all-day-boundary",
            course_id=course,
            title="Boundary all-day quiz",
            assessment_type="quiz",
            due_at=datetime(2026, 9, 21, 3, 59, tzinfo=UTC),
            grade_weight_percent=5,
            estimated_minutes=30,
            confidence=1,
            fact_state="confirmed",
            citation=SourceCitation(),
            is_all_day=True,
            trace=AssessmentSourceTrace(
                source_id="assessment-source",
                source_scope="notion:assessment-source",
                notion_last_edited_at=edited,
                title_property_id="title-prop",
            ),
        )
        AcademicRepository.upsert_assessment(
            session,
            notion_id="notion-timed-outside",
            course_id=course,
            title="Late timed lab",
            assessment_type="lab",
            due_at=datetime(2026, 9, 20, 16, 1, tzinfo=UTC),
            grade_weight_percent=None,
            estimated_minutes=60,
            confidence=1,
            fact_state="confirmed",
            citation=SourceCitation(),
        )
        AcademicRepository.upsert_assessment(
            session,
            notion_id="notion-completed-ambiguous",
            course_id=course,
            title="Completed ambiguous assignment",
            assessment_type="assignment",
            due_at=datetime(2026, 9, 11, 15, tzinfo=UTC),
            grade_weight_percent=10,
            estimated_minutes=90,
            confidence=0.4,
            fact_state="ambiguous",
            citation=SourceCitation(),
            completed=True,
        )
        saved = AcademicRepository.save_assessment_calendar_semantics(
            session,
            notion_id=included_all_day.notion_id,
            semantics=CalendarSemanticResultInput(
                status="not_substantive",
                overview="A quiz scheduled as an all-day course date.",
                source_fingerprint="academic-source-v1",
                source_last_edited_at=edited,
                model_identity="qwen-test",
                config_version="calendar-test",
                prompt_version="prompt-v1",
                analyzed_at=datetime(2026, 9, 9, 13, tzinfo=UTC),
                evidence_ids=("frag-1",),
                intent_value="study",
                intent_status="valid",
                intent_rationale="Quiz prep is study-related.",
                intent_evidence_ids=("frag-2",),
            ),
        )
        stale = AcademicRepository.save_assessment_calendar_semantics(
            session,
            notion_id=included_all_day.notion_id,
            semantics=CalendarSemanticResultInput(
                status="valid",
                overview="Stale overview",
                description="Stale description",
                source_fingerprint="academic-source-v0",
                source_last_edited_at=edited - timedelta(minutes=1),
                model_identity="qwen-test",
                config_version="calendar-test",
                prompt_version="prompt-v1",
                analyzed_at=datetime(2026, 9, 9, 14, tzinfo=UTC),
                evidence_ids=("frag-old",),
                description_evidence_ids=("frag-old",),
            ),
        )
        assert saved is True
        assert stale is False

    items = SQLAlchemyAcademicPlannerStore(engine).load_upcoming_calendar_items(
        occurrence=date(2026, 9, 10)
    )

    assert [item["event_id"] for item in items] == [
        "notion-completed-ambiguous",
        "notion-all-day-boundary",
    ]
    assert items[0]["completed"] is True
    assert items[1]["is_all_day"] is True
    assert items[1]["semantic_status"] == "not_substantive"
    assert items[1]["semantic_overview"] == "A quiz scheduled as an all-day course date."
    assert items[1]["semantic_intent_value"] == "study"
    assert items[1]["semantic_intent_status"] == "valid"
    assert items[1]["semantic_cache"]["intent_value"] == "study"
    assert items[1]["semantic_cache"]["intent_evidence_ids"] == ("frag-2",)
    assert items[1]["semantic_cache"]["source_fingerprint"] == "academic-source-v1"


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


def test_assessment_material_versions_activate_without_source_collisions(engine) -> None:
    course = _course(engine)
    retrieved = datetime(2026, 9, 3, 12, tzinfo=UTC)
    edited = datetime(2026, 9, 3, 11, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        assessment = AcademicRepository.upsert_assessment(
            session,
            notion_id="assessment-page-1",
            course_id=course,
            title="ECE 222 Assignment 1",
            assessment_type="assignment",
            due_at=retrieved + timedelta(days=10),
            grade_weight_percent=None,
            estimated_minutes=120,
            confidence=1,
            fact_state="confirmed",
            citation=SourceCitation(url="https://notion.test/assessment-page-1"),
        )
        body = AcademicRepository.upsert_document(
            session,
            notion_id="assessment-page-1",
            document_version="v1",
            title="Assessment body",
            document_type="notion_body",
            retrieved_at=retrieved,
            artifact_key="a" * 64,
            content_hash="b" * 64,
            source_url="https://prod-files-secure.notion-static.com/signed?X-Amz-Signature=abc",
            course_id=course,
            assessment_id=assessment.id,
            source_kind="notion_page_body",
            source_page_id="assessment-page-1",
            source_key="notion:assessment-page-1:body",
            source_last_edited_at=edited,
            extraction_status="extracted",
        )
        attachment = AcademicRepository.upsert_document(
            session,
            notion_id="assessment-page-1",
            document_version="v1",
            title="Rubric.pdf",
            document_type="pdf",
            retrieved_at=retrieved,
            artifact_key="c" * 64,
            content_hash="d" * 64,
            course_id=course,
            assessment_id=assessment.id,
            source_kind="notion_property_file",
            source_page_id="assessment-page-1",
            source_property_id="files-prop",
            source_key="notion:assessment-page-1:property:files-prop:rubric",
            original_filename="Rubric.pdf",
            media_type="application/pdf",
            source_last_edited_at=edited,
            extraction_status="extracted",
        )
        replay = AcademicRepository.upsert_document(
            session,
            notion_id="assessment-page-1",
            document_version="v1",
            title="Rubric renamed.pdf",
            document_type="pdf",
            retrieved_at=retrieved,
            artifact_key="c" * 64,
            content_hash="d" * 64,
            course_id=course,
            assessment_id=assessment.id,
            source_kind="notion_property_file",
            source_page_id="assessment-page-1",
            source_property_id="files-prop",
            source_key="notion:assessment-page-1:property:files-prop:rubric",
            extraction_status="extracted",
        )
        changed = AcademicRepository.upsert_document(
            session,
            notion_id="assessment-page-1",
            document_version="v2",
            title="Rubric changed.pdf",
            document_type="pdf",
            retrieved_at=retrieved + timedelta(hours=1),
            artifact_key="e" * 64,
            content_hash="f" * 64,
            course_id=course,
            assessment_id=assessment.id,
            source_kind="notion_property_file",
            source_page_id="assessment-page-1",
            source_property_id="files-prop",
            source_key="notion:assessment-page-1:property:files-prop:rubric",
            extraction_status="partial",
            active=False,
        )
        AcademicRepository.activate_document_version(session, document_id=changed.id)
        failed = AcademicRepository.upsert_document(
            session,
            notion_id="assessment-page-1",
            document_version="v3",
            title="Rubric failed.pdf",
            document_type="pdf",
            retrieved_at=retrieved + timedelta(hours=2),
            artifact_key="1" * 64,
            content_hash="2" * 64,
            course_id=course,
            assessment_id=assessment.id,
            source_kind="notion_property_file",
            source_page_id="assessment-page-1",
            source_property_id="files-prop",
            source_key="notion:assessment-page-1:property:files-prop:rubric",
            extraction_status="failed",
            active=False,
            extraction_error_code="malformed_pdf",
            extraction_error_detail="PDF could not be opened",
        )

        assert body.id != attachment.id
        assert replay.id == attachment.id
        assert body.source_url is None
        assert failed.extraction_error_code == "malformed_pdf"
        changed_id = changed.id
        attachment_id = attachment.id
        failed_id = failed.id

    with Session(engine) as session:
        rows = list(session.scalars(select(AcademicDocument).order_by(AcademicDocument.title)))
        latest = session.get(AcademicDocument, changed_id)
        previous = session.get(AcademicDocument, attachment_id)
        assert len(rows) == 4
        assert latest is not None
        assert latest.active is True
        assert previous is not None
        assert previous.active is False
        failed_row = session.get(AcademicDocument, failed_id)
        assert failed_row is not None
        assert failed_row.active is False


def test_removed_assessment_material_becomes_inactive_without_erasing_history(engine) -> None:
    course = _course(engine)
    retrieved = datetime(2026, 9, 3, 12, tzinfo=UTC)
    source_key = "notion:assessment-page-2:body"
    with Session(engine) as session, session.begin():
        assessment = AcademicRepository.upsert_assessment(
            session,
            notion_id="assessment-page-2",
            course_id=course,
            title="Quiz 1",
            assessment_type="quiz",
            due_at=retrieved + timedelta(days=3),
            grade_weight_percent=None,
            estimated_minutes=30,
            confidence=1,
            fact_state="confirmed",
            citation=SourceCitation(),
        )
        document = AcademicRepository.upsert_document(
            session,
            notion_id="assessment-page-2",
            document_version="v1",
            title="Quiz body",
            document_type="notion_body",
            retrieved_at=retrieved,
            artifact_key="a" * 64,
            content_hash="b" * 64,
            course_id=course,
            assessment_id=assessment.id,
            source_kind="notion_page_body",
            source_page_id="assessment-page-2",
            source_key=source_key,
            extraction_status="extracted",
        )
        changed = AcademicRepository.mark_document_source_inactive(
            session,
            source_key=source_key,
            error_code="source_removed",
            error_detail="Source no longer appears on the Notion page.",
        )
        assert changed == 1
        assert document.active is False
        assert document.extraction_status == "inactive"

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AcademicDocument)) == 1
        inactive = session.scalar(select(AcademicDocument))
        assert inactive.extraction_error_code == "source_removed"


def test_assessment_scoped_lexical_semantic_and_chunk_reads_are_isolated(engine) -> None:
    history_course = _course(engine)
    retrieved = datetime(2026, 9, 3, 12, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        engineering_course = AcademicRepository.upsert_course(
            session,
            notion_id="notion-course-ece",
            course_code="ECE-222",
            title="Circuits",
            term="2026-fall",
            priority=90,
        ).id
        first = AcademicRepository.upsert_assessment(
            session,
            notion_id="ece-assessment-1",
            course_id=engineering_course,
            title="Assignment 1",
            assessment_type="assignment",
            due_at=retrieved + timedelta(days=4),
            grade_weight_percent=None,
            estimated_minutes=120,
            confidence=1,
            fact_state="confirmed",
            citation=SourceCitation(),
        )
        second = AcademicRepository.upsert_assessment(
            session,
            notion_id="hist-assessment-1",
            course_id=history_course,
            title="Essay",
            assessment_type="assignment",
            due_at=retrieved + timedelta(days=4),
            grade_weight_percent=None,
            estimated_minutes=120,
            confidence=1,
            fact_state="confirmed",
            citation=SourceCitation(),
        )
        first_doc = AcademicRepository.upsert_document(
            session,
            notion_id="ece-assessment-1",
            document_version="v1",
            title="ECE material",
            document_type="pdf",
            retrieved_at=retrieved,
            artifact_key="a" * 64,
            content_hash="b" * 64,
            course_id=engineering_course,
            assessment_id=first.id,
            source_kind="notion_property_file",
            source_page_id="ece-assessment-1",
            source_property_id="files",
            source_key="notion:ece-assessment-1:files:rubric",
            extraction_status="extracted",
        )
        second_doc = AcademicRepository.upsert_document(
            session,
            notion_id="hist-assessment-1",
            document_version="v1",
            title="HIST material",
            document_type="pdf",
            retrieved_at=retrieved,
            artifact_key="c" * 64,
            content_hash="d" * 64,
            course_id=history_course,
            assessment_id=second.id,
            source_kind="notion_property_file",
            source_page_id="hist-assessment-1",
            source_property_id="files",
            source_key="notion:hist-assessment-1:files:rubric",
            extraction_status="extracted",
        )
        first_chunk = AcademicRepository.replace_document_chunks(
            session,
            document_id=first_doc.id,
            chunks=[
                DocumentChunkInput(
                    ordinal=0,
                    heading="Circuit work",
                    content="Solve linear circuits and compare AC with DC behavior.",
                    citation=SourceCitation(page=2, block="rubric-row"),
                    embedding=[1.0, 0.0],
                    embedding_model="test-embedding-v1",
                )
            ],
        )[0]
        second_chunk = AcademicRepository.replace_document_chunks(
            session,
            document_id=second_doc.id,
            chunks=[
                DocumentChunkInput(
                    ordinal=0,
                    heading="Essay work",
                    content="Use primary evidence to support the argument.",
                    citation=SourceCitation(page=1),
                    embedding=[0.0, 1.0],
                    embedding_model="test-embedding-v1",
                )
            ],
        )[0]
        first_id = first.id
        first_chunk_id = first_chunk.id
        second_chunk_id = second_chunk.id

    with Session(engine) as session:
        lexical = AcademicRepository.search_document_chunks(
            session,
            query="evidence",
            assessment_id=first_id,
            active_only=True,
        )
        semantic = AcademicRepository.search_semantic_document_chunks(
            session,
            assessment_id=first_id,
            query_embedding=[0.9, 0.1],
            embedding_model="test-embedding-v1",
            limit=2,
        )
        guarded = AcademicRepository.read_assessment_material_chunks(
            session,
            assessment_id=first_id,
            chunk_ids=[second_chunk_id, first_chunk_id],
        )

        assert lexical == []
        assert [chunk.id for chunk, _score in semantic] == [first_chunk_id]
        assert semantic[0][1] > 0.99
        assert [chunk.id for chunk in guarded] == [first_chunk_id]


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


def test_clarification_persists_all_todo_previews_and_write_decisions(engine) -> None:
    expected = datetime(2026, 9, 5, 15, tzinfo=UTC)
    actions = ("quiz", "assignment", "tutorial", "lab", "event", "ignore")
    with Session(engine) as session, session.begin():
        for action in actions:
            row = AcademicRepository.create_or_get_clarification(
                session,
                request=ClarificationInput(
                    event_notion_id=f"event-{action}",
                    original_title="Chapter 4",
                    raw_label="chapter 4",
                    quiz_preview_title="Quiz - Chapter 4",
                    assignment_preview_title="Assignment - Chapter 4",
                    tutorial_preview_title="Tutorial - Chapter 4",
                    lab_preview_title="Lab - Chapter 4",
                    event_preview_title="Course Event - Chapter 4",
                    expected_edited_at=expected,
                    expires_at=expected + timedelta(hours=24),
                    idempotency_key=f"clarification:{action}:v1",
                    title_property_id="title-prop",
                ),
            )
            status, claimed = AcademicRepository.claim_clarification(
                session,
                clarification_id=row.id,
                action=action,
                actor_id=123456789,
                now=expected + timedelta(minutes=1),
            )
            if action == "ignore":
                assert status == "ignored"
                assert claimed.write_status == "skipped"
            else:
                assert status == "ready"
                assert claimed.write_status == "pending"
                applied = AcademicRepository.mark_clarification_applied(
                    session,
                    clarification_id=row.id,
                    applied_at=expected + timedelta(minutes=2),
                )
                assert applied.state == "applied"

        invalid = AcademicRepository.create_or_get_clarification(
            session,
            request=ClarificationInput(
                event_notion_id="event-invalid",
                original_title="Chapter 5",
                quiz_preview_title="Quiz - Chapter 5",
                assignment_preview_title="Assignment - Chapter 5",
                expected_edited_at=expected,
                expires_at=expected + timedelta(hours=24),
                idempotency_key="clarification:invalid:v1",
            ),
        )
        with pytest.raises(ValueError, match="academic clarification action is invalid"):
            AcademicRepository.claim_clarification(
                session,
                clarification_id=invalid.id,
                action="homework",
                actor_id=123456789,
                now=expected + timedelta(minutes=1),
            )

    with Session(engine) as session:
        stored = session.scalar(
            select(AcademicClarification).where(
                AcademicClarification.event_notion_id == "event-event"
            )
        )
        assert stored is not None
        assert stored.decision == "event"
        assert stored.event_preview_title == "Course Event - Chapter 4"
        assert session.scalar(select(func.count()).select_from(AcademicClarification)) == 7


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


def test_abort_owner_channel_continuations_closes_only_resumable_state(engine) -> None:
    now = datetime(2026, 9, 15, 12, tzinfo=UTC)
    channel_id = "222222222222222222"
    user_id = "333333333333333333"
    with Session(engine) as session, session.begin():
        learning = AcademicRepository.create_discourse_session(
            session,
            external_event_id="learning-session",
            discord_channel_id=channel_id,
            discord_user_id=user_id,
            session_kind="learning_focus",
            partial_state={"private": "removed on abort"},
            started_at=now - timedelta(minutes=5),
        )
        review = AcademicRepository.create_discourse_session(
            session,
            external_event_id="memory-review-session",
            discord_channel_id=channel_id,
            discord_user_id=user_id,
            session_kind="memory_review",
            partial_state={"step": "reviewing"},
            started_at=now - timedelta(minutes=4),
        )
        unrelated = AcademicRepository.create_discourse_session(
            session,
            external_event_id="other-channel-session",
            discord_channel_id="444444444444444444",
            discord_user_id=user_id,
            session_kind="memory_review",
            partial_state={"step": "reviewing"},
            started_at=now - timedelta(minutes=4),
        )
        later_session = AcademicRepository.create_discourse_session(
            session,
            external_event_id="later-session",
            discord_channel_id=channel_id,
            discord_user_id=user_id,
            session_kind="memory_review",
            partial_state={"step": "later"},
            started_at=now + timedelta(seconds=1),
        )
        proposal = _checkin_proposal(
            session,
            event_id="proposal-kept",
            idempotency_key="proposal-kept",
            expires_at=now + timedelta(hours=1),
        )
        captured = _inbound_material_row(
            "111111111111111111",
            "111111111111111112",
            owner=user_id,
            channel=channel_id,
            state="captured",
            expires_at=now + timedelta(hours=1),
        )
        awaiting = _inbound_material_row(
            "111111111111111113",
            "111111111111111114",
            owner=user_id,
            channel=channel_id,
            state="awaiting_target",
            expires_at=now + timedelta(hours=1),
        )
        proposal_linked = _inbound_material_row(
            "111111111111111115",
            "111111111111111116",
            owner=user_id,
            channel=channel_id,
            state="awaiting_target",
            proposal_id=proposal.id,
            expires_at=now + timedelta(hours=1),
        )
        later_material = _inbound_material_row(
            "111111111111111117",
            "111111111111111118",
            owner=user_id,
            channel=channel_id,
            state="captured",
            expires_at=now + timedelta(hours=1),
        )
        session.add_all((captured, awaiting, proposal_linked, later_material))
        session.flush()
        captured.created_at = now - timedelta(minutes=2)
        awaiting.created_at = now - timedelta(minutes=2)
        proposal_linked.created_at = now - timedelta(minutes=2)
        later_material.created_at = now + timedelta(seconds=1)

        result = AcademicRepository.abort_owner_channel_continuations(
            session,
            discord_channel_id=channel_id,
            discord_user_id=user_id,
            abort_event_id="999999999999999999",
            aborted_at=now,
        )

        assert result == {
            "discourse_sessions_closed": 2,
            "inbound_materials_expired": 2,
        }
        assert learning.state == "completed"
        assert review.state == "completed"
        assert learning.partial_state == {
            "outcome": "aborted",
            "abort_discord_event_id": "999999999999999999",
            "aborted_at": now.isoformat(),
        }
        assert unrelated.state == "open"
        assert later_session.state == "open"
        assert captured.state == "expired"
        assert captured.error_code == "user_abort"
        assert awaiting.state == "expired"
        assert proposal_linked.state == "awaiting_target"
        assert later_material.state == "captured"
        assert proposal.state == "pending"


def test_store_serializes_expanded_clarification_previews(engine) -> None:
    store = SQLAlchemyAcademicPlannerStore(engine)
    expected = datetime(2026, 9, 5, 15, tzinfo=UTC)
    clarification_id = store.create_or_get_clarification(
        event_notion_id="event-expanded-store",
        original_title="Chapter 8",
        raw_label="chapter 8",
        quiz_preview_title="Quiz - Chapter 8",
        assignment_preview_title="Assignment - Chapter 8",
        tutorial_preview_title="Tutorial - Chapter 8",
        lab_preview_title="Lab - Chapter 8",
        event_preview_title="Course Event - Chapter 8",
        expected_edited_at=expected,
        expires_at=expected + timedelta(hours=24),
        idempotency_key="clarification:event-expanded-store:v1",
        title_property_id="title-prop",
    )

    first, row = store.claim_clarification(
        clarification_id,
        "event",
        123456789,
        now=expected + timedelta(minutes=1),
    )

    assert first == "ready"
    assert row["decision"] == "event"
    assert row["event_preview_title"] == "Course Event - Chapter 8"
    assert row["preview_titles"] == {
        "quiz": "Quiz - Chapter 8",
        "assignment": "Assignment - Chapter 8",
        "tutorial": "Tutorial - Chapter 8",
        "lab": "Lab - Chapter 8",
        "event": "Course Event - Chapter 8",
    }


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


def test_checkin_proposal_requires_exact_confirmation_and_is_replay_safe(engine) -> None:
    confirmation_event = "CONFIRM ACADEMIC exact-token"
    with Session(engine) as session, session.begin():
        proposal = _checkin_proposal(session, confirmation_event=confirmation_event)
        replay = AcademicRepository.create_proposed_change(
            session,
            checkin_id=proposal.checkin_id,
            idempotency_key="proposal-1",
            operation="notion_update",
            target_type="assessment",
            target_id="notion-assignment-1",
            payload={"completed": True},
            redacted_preview="Mark the assessment complete.",
            confirmation_token=confirmation_event,
        )
        assert replay.id == proposal.id
        denied, _ = AcademicRepository.begin_confirmed_change(
            session,
            proposal_id=proposal.id,
            confirmation_event="CONFIRM ACADEMIC exact",
        )
        assert denied == "confirmation_required"
        ready, claimed = AcademicRepository.begin_confirmed_change(
            session, proposal_id=proposal.id, confirmation_event=confirmation_event
        )
        assert ready == "ready"
        assert claimed.state == "applying"
        in_progress, _ = AcademicRepository.begin_confirmed_change(
            session, proposal_id=proposal.id, confirmation_event=confirmation_event
        )
        assert in_progress == "in_progress"
        applied = AcademicRepository.mark_proposed_change_applied(
            session, proposal_id=proposal.id, confirmation_event=confirmation_event
        )
        assert applied.state == "applied"
        assert (
            AcademicRepository.mark_proposed_change_applied(
                session, proposal_id=proposal.id, confirmation_event=confirmation_event
            ).id
            == proposal.id
        )
        already_applied, _ = AcademicRepository.begin_confirmed_change(
            session, proposal_id=proposal.id, confirmation_event=confirmation_event
        )
        assert already_applied == "already_applied"

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AcademicCheckIn)) == 1
        assert session.scalar(select(func.count()).select_from(AcademicProposedChange)) == 1


def test_checkin_proposal_rejection_is_atomic_audited_and_replay_safe(engine) -> None:
    now = datetime(2026, 9, 3, 22, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        proposal = _checkin_proposal(session, expires_at=now + timedelta(hours=2))
        proposal_row_id = proposal.id
        status, rejected = AcademicRepository.reject_proposed_change(
            session,
            proposal_id=proposal_row_id,
            actor="discord:123456789",
            now=now,
        )
        assert status == "rejected"
        assert rejected.state == "rejected"
        proposal_public_id = rejected.target_id
        checkin = session.get(AcademicCheckIn, rejected.checkin_id)
        assert checkin is not None
        assert checkin.status == "completed"

        replay_status, replay = AcademicRepository.reject_proposed_change(
            session,
            proposal_id=proposal_row_id,
            actor="discord:123456789",
            now=now + timedelta(minutes=1),
        )
        assert replay_status == "already_rejected"
        assert replay.id == proposal_row_id

        denied, _ = AcademicRepository.begin_confirmed_change(
            session,
            proposal_id=proposal_row_id,
            confirmation_event="CONFIRM ACADEMIC exact-token",
            now=now + timedelta(minutes=2),
        )
        assert denied == "confirmation_required"

    with Session(engine) as session:
        audit_events = list(session.scalars(select(AuditEvent)))
        assert len(audit_events) == 1
        assert audit_events[0].actor == "discord:123456789"
        assert audit_events[0].action == "academic_proposal.rejected"
        assert audit_events[0].target_id == proposal_public_id
        assert audit_events[0].result == "rejected"
        assert session.scalar(select(func.count()).select_from(Assessment)) == 0


def test_rejecting_expired_or_non_pending_proposal_never_audits(engine) -> None:
    now = datetime(2026, 9, 3, 22, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        expired = _checkin_proposal(
            session,
            event_id="event-expired-proposal",
            idempotency_key="proposal-expired",
            expires_at=now - timedelta(minutes=1),
        )
        expired_status, expired_row = AcademicRepository.reject_proposed_change(
            session,
            proposal_id=expired.id,
            now=now,
        )
        assert expired_status == "expired"
        assert expired_row.state == "expired"

        applying = _checkin_proposal(
            session,
            event_id="event-applying-proposal",
            idempotency_key="proposal-applying",
            expires_at=now + timedelta(hours=1),
        )
        ready, applying_row = AcademicRepository.begin_confirmed_change(
            session,
            proposal_id=applying.id,
            confirmation_event="CONFIRM ACADEMIC exact-token",
            now=now,
        )
        assert ready == "ready"
        assert applying_row.state == "applying"
        reject_status, same_row = AcademicRepository.reject_proposed_change(
            session,
            proposal_id=applying.id,
            now=now + timedelta(minutes=1),
        )
        assert reject_status == "in_progress"
        assert same_row.state == "applying"

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_confirm_reject_race_resolves_to_one_terminal_winner(engine) -> None:
    now = datetime(2026, 9, 3, 22, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        reject_first = _checkin_proposal(
            session,
            event_id="event-reject-first",
            idempotency_key="proposal-reject-first",
            expires_at=now + timedelta(hours=1),
        )
        rejected, rejected_row = AcademicRepository.reject_proposed_change(
            session,
            proposal_id=reject_first.id,
            now=now,
        )
        confirmed_after_reject, same_rejected_row = AcademicRepository.begin_confirmed_change(
            session,
            proposal_id=reject_first.id,
            confirmation_event="CONFIRM ACADEMIC exact-token",
            now=now + timedelta(seconds=1),
        )
        assert rejected == "rejected"
        assert rejected_row.state == "rejected"
        assert confirmed_after_reject == "confirmation_required"
        assert same_rejected_row.state == "rejected"

        confirm_first = _checkin_proposal(
            session,
            event_id="event-confirm-first",
            idempotency_key="proposal-confirm-first",
            expires_at=now + timedelta(hours=1),
        )
        confirmed, confirmed_row = AcademicRepository.begin_confirmed_change(
            session,
            proposal_id=confirm_first.id,
            confirmation_event="CONFIRM ACADEMIC exact-token",
            now=now,
        )
        rejected_after_confirm, same_confirmed_row = AcademicRepository.reject_proposed_change(
            session,
            proposal_id=confirm_first.id,
            now=now + timedelta(seconds=1),
        )
        assert confirmed == "ready"
        assert confirmed_row.state == "applying"
        assert rejected_after_confirm == "in_progress"
        assert same_confirmed_row.state == "applying"

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1


def test_store_rejects_checkin_proposal_by_public_id(engine) -> None:
    from app.agents.academic_planner.contracts import CheckinProposal, ProposedChange

    store = SQLAlchemyAcademicPlannerStore(engine)
    proposal_id = uuid4()
    store.save_checkin_proposal(
        CheckinProposal(
            proposal_id=proposal_id,
            confirmation_event=f"confirm {proposal_id}",
            changes=(
                ProposedChange(
                    field="completed",
                    value="true",
                    assessment_id="notion-assignment-1",
                ),
            ),
        )
    )

    status, proposal = store.reject_checkin_proposal(proposal_id, actor="discord:123456789")
    replay_status, replay = store.reject_checkin_proposal(proposal_id, actor="discord:123456789")

    assert status == "rejected"
    assert proposal is not None
    assert proposal.proposal_id == proposal_id
    assert replay_status == "already_rejected"
    assert replay is not None
    with Session(engine) as session:
        row = session.scalar(
            select(AcademicProposedChange).where(
                AcademicProposedChange.idempotency_key == f"academic-proposal:{proposal_id}"
            )
        )
        assert row is not None
        assert row.state == "rejected"
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1


def test_store_persists_discord_checkin_with_external_event_dedupe(engine) -> None:
    from app.agents.academic_planner.contracts import CheckinProposal, ProposedChange

    store = SQLAlchemyAcademicPlannerStore(engine)
    proposal_id = uuid4()
    received_at = datetime(2026, 9, 3, 22, tzinfo=UTC)
    proposal = CheckinProposal(
        proposal_id=proposal_id,
        confirmation_event=f"confirm {proposal_id}",
        changes=(
            ProposedChange(
                field="completed",
                value="true",
                assessment_id="notion-assignment-1",
            ),
        ),
    )

    created = store.save_discord_checkin(
        proposal,
        external_event_id="discord-message-1",
        channel="discord",
        received_at=received_at,
    )
    replayed = store.save_discord_checkin(
        CheckinProposal(
            proposal_id=uuid4(),
            confirmation_event="confirm different-proposal",
            changes=(
                ProposedChange(
                    field="completed",
                    value="true",
                    assessment_id="notion-assignment-2",
                ),
            ),
        ),
        external_event_id="discord-message-1",
        channel="discord",
        received_at=received_at + timedelta(minutes=1),
    )

    assert created.status == "created"
    assert created.checkin_status == "proposal_pending"
    assert created.proposal_row_id is not None
    assert replayed.status == "replayed"
    assert replayed.checkin_id == created.checkin_id
    assert replayed.proposal_row_id == created.proposal_row_id
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AcademicCheckIn)) == 1
        assert session.scalar(select(func.count()).select_from(AcademicProposedChange)) == 1
        checkin = session.get(AcademicCheckIn, created.checkin_id)
        proposed = session.get(AcademicProposedChange, created.proposal_row_id)
        assert checkin is not None
        assert proposed is not None
        assert checkin.external_event_id == "discord-message-1"
        assert checkin.content_artifact_key is None
        assert checkin.redacted_summary == "Academic check-in proposal pending confirmation."
        assert proposed.payload == {
            "changes": [
                {
                    "field": "completed",
                    "value": "true",
                    "assessment_id": "notion-assignment-1",
                }
            ]
        }
        assert "raw private text" not in str(checkin)
        assert "raw private text" not in str(proposed.payload)


def test_store_persists_zero_change_discord_checkin_as_questioned_only(engine) -> None:
    from app.agents.academic_planner.contracts import CheckinProposal

    store = SQLAlchemyAcademicPlannerStore(engine)
    proposal_id = uuid4()
    proposal = CheckinProposal(
        proposal_id=proposal_id,
        confirmation_event=f"confirm {proposal_id}",
        changes=(),
    )
    received_at = datetime(2026, 9, 3, 22, tzinfo=UTC)

    created = store.save_discord_checkin(
        proposal,
        external_event_id="discord-message-question",
        channel="discord",
        received_at=received_at,
    )
    replayed = store.save_discord_checkin(
        proposal,
        external_event_id="discord-message-question",
        channel="discord",
        received_at=received_at + timedelta(minutes=1),
    )

    assert created.status == "created"
    assert created.checkin_status == "questioned"
    assert created.proposal_row_id is None
    assert replayed.status == "replayed"
    assert replayed.checkin_id == created.checkin_id
    assert replayed.proposal_row_id is None
    with Session(engine) as session:
        checkin = session.get(AcademicCheckIn, created.checkin_id)
        assert checkin is not None
        assert checkin.status == "questioned"
        assert checkin.redacted_summary == "Academic check-in needs clarification."
        assert session.scalar(select(func.count()).select_from(AcademicProposedChange)) == 0
