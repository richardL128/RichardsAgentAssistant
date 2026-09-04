from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import fitz
import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agents.academic_planner.allocator import allocate_plan
from app.agents.academic_planner.contracts import (
    Assessment,
    AssessmentType,
    AvailabilityWindow,
    FixedCommitment,
    PlannerFacts,
)
from app.agents.academic_planner.documents import detect_ambiguous_deadlines, extract_document
from app.agents.academic_planner.retrieval import retrieve_with_academic_repository
from app.agents.academic_planner.workflow import (
    confirm_checkin_proposal,
    create_checkin_proposal,
    run_morning_plan,
)
from app.connectors.notion import AcademicNotionWriter, NotionConnector, NotionPageTarget
from app.db.academic import (
    AcademicRepository,
    DocumentChunkInput,
    SourceCitation,
    SQLAlchemyAcademicPlannerStore,
)
from app.db.models import (
    AcademicProposedChange,
    Base,
    StudyBlock,
)
from app.db.models import (
    Assessment as StoredAssessment,
)
from app.db.models import (
    FixedCommitment as StoredCommitment,
)

TORONTO = ZoneInfo("America/Toronto")


@pytest.fixture
def engine(tmp_path: Path):
    created = create_engine(f"sqlite+pysqlite:///{tmp_path / 'phase5.db'}")
    Base.metadata.create_all(created)
    try:
        yield created
    finally:
        created.dispose()


class DeliveryRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    async def send_morning_plan(self, plan, *, idempotency_key: str) -> object:
        self.calls.append(("morning", idempotency_key, str(plan.plan_id)))
        return object()

    async def send_checkin(self, *, plan, idempotency_key: str) -> object:
        self.calls.append(("checkin", idempotency_key, str(plan.plan_id) if plan else None))
        return object()

    async def send_ambiguity_question(self, fact, *, idempotency_key: str) -> object:
        self.calls.append(("question", idempotency_key, fact.id))
        return object()

    async def send_confirmation(self, proposal, *, idempotency_key: str) -> object:
        self.calls.append(("confirmation", idempotency_key, str(proposal.proposal_id)))
        return object()


def _pdf(text: str) -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    body = document.tobytes()
    document.close()
    return body


def _properties() -> dict[str, dict[str, str]]:
    return {
        database: {name: f"{database}-{name}" for name in names}
        for database, names in {
            "courses": {"course", "term", "priority", "outline", "policy"},
            "assessments": {
                "course",
                "type",
                "due",
                "grade_weight",
                "instructions",
                "rubric",
                "scope",
                "status",
                "estimated_time",
            },
            "study_blocks": {
                "assessment",
                "planned_duration",
                "actual_duration",
                "completion_state",
                "notes",
            },
        }.items()
    }


def _overlaps(start: datetime, end: datetime, other_start: datetime, other_end: datetime) -> bool:
    return start < other_end and end > other_start


def _stored_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@pytest.mark.asyncio
async def test_phase5_academic_planner_acceptance_contract(engine) -> None:
    now = datetime(2026, 3, 7, 8, 0, tzinfo=TORONTO)
    class_start = datetime(2026, 3, 7, 9, 0, tzinfo=TORONTO).astimezone(UTC)
    class_end = datetime(2026, 3, 7, 10, 0, tzinfo=TORONTO).astimezone(UTC)
    test_start = datetime(2026, 3, 7, 11, 0, tzinfo=TORONTO).astimezone(UTC)
    test_end = datetime(2026, 3, 7, 12, 0, tzinfo=TORONTO).astimezone(UTC)
    assignment_due = datetime(2026, 3, 12, 20, 0, tzinfo=TORONTO).astimezone(UTC)
    quiz_due = datetime(2026, 3, 10, 10, 0, tzinfo=TORONTO).astimezone(UTC)
    ambiguous_document = extract_document(
        _pdf("Project brief: due 2026-03-12. Schedule table deadline 2026-03-19."),
        document_id="ambiguous-outline",
        title="Ambiguous outline",
        media_type="application/pdf",
    )
    ambiguity = detect_ambiguous_deadlines(ambiguous_document)

    with Session(engine) as session, session.begin():
        history = AcademicRepository.upsert_course(
            session,
            notion_id="course-history",
            course_code="HIST-201",
            title="Research Methods",
            term="2026-spring",
            priority=85,
        )
        biology = AcademicRepository.upsert_course(
            session,
            notion_id="course-biology",
            course_code="BIO-101",
            title="Biology",
            term="2026-spring",
            priority=40,
        )
        assignment = AcademicRepository.upsert_assessment(
            session,
            notion_id="notion-assignment-1",
            course_id=history.id,
            title="Archive analysis",
            assessment_type="assignment",
            due_at=assignment_due,
            grade_weight_percent=25,
            estimated_minutes=90,
            confidence_gap=0.1,
            scope_size=12,
            confidence=0.95,
            fact_state="confirmed",
            citation=SourceCitation(page=2, block="assessment-table"),
        )
        quiz = AcademicRepository.upsert_assessment(
            session,
            notion_id="notion-quiz-1",
            course_id=history.id,
            title="Primary-source quiz",
            assessment_type="quiz",
            due_at=quiz_due,
            grade_weight_percent=5,
            estimated_minutes=60,
            confidence_gap=0.2,
            scope_size=4,
            confidence=0.9,
            fact_state="confirmed",
            citation=SourceCitation(page=3, block="quiz-table"),
        )
        AcademicRepository.upsert_assessment(
            session,
            notion_id="notion-ambiguous-pdf",
            course_id=history.id,
            title="Ambiguous PDF project",
            assessment_type="assignment",
            due_at=None,
            grade_weight_percent=15,
            estimated_minutes=120,
            confidence=0.2,
            fact_state="ambiguous",
            ambiguity_reason=ambiguity.reason,
            citation=SourceCitation(page=ambiguity.citations[0].page),
        )
        AcademicRepository.upsert_fixed_commitment(
            session,
            notion_id="notion-class-1",
            title="HIST seminar",
            commitment_type="class",
            starts_at=class_start,
            ends_at=class_end,
            timezone="America/Toronto",
            course_id=history.id,
            confidence=1.0,
            fact_state="confirmed",
            citation=SourceCitation(block="class-calendar"),
        )
        AcademicRepository.upsert_fixed_commitment(
            session,
            notion_id="notion-test-1",
            title="BIO lab test",
            commitment_type="test",
            starts_at=test_start,
            ends_at=test_end,
            timezone="America/Toronto",
            confidence=1.0,
            fact_state="confirmed",
            citation=SourceCitation(block="test-calendar"),
        )
        AcademicRepository.upsert_preferences(
            session,
            scope="default",
            timezone="America/Toronto",
            availability={
                "windows": [
                    {
                        "start_at": now.isoformat(),
                        "end_at": datetime(2026, 3, 7, 13, 0, tzinfo=TORONTO).isoformat(),
                    }
                ]
            },
            daily_capacity_minutes=240,
            buffer_minutes=20,
        )
        old_plan = AcademicRepository.upsert_study_plan(
            session,
            plan_key="old-phase5-plan",
            starts_on=date(2026, 3, 6),
            ends_on=date(2026, 3, 6),
            timezone="America/Toronto",
        )
        AcademicRepository.upsert_study_block(
            session,
            plan_id=old_plan.id,
            block_key="old-assignment-block",
            title="Finish archive outline",
            starts_at=datetime(2026, 3, 6, 15, 0, tzinfo=UTC),
            ends_at=datetime(2026, 3, 6, 15, 30, tzinfo=UTC),
            allocated_minutes=30,
            status="incomplete",
            assessment_id=assignment.id,
        )
        history_doc = AcademicRepository.upsert_document(
            session,
            notion_id="history-outline",
            document_version="etag-history",
            title="HIST outline",
            document_type="outline",
            retrieved_at=now,
            artifact_key="a" * 64,
            content_hash="b" * 64,
            course_id=history.id,
        )
        biology_doc = AcademicRepository.upsert_document(
            session,
            notion_id="biology-outline",
            document_version="etag-biology",
            title="BIO outline",
            document_type="outline",
            retrieved_at=now,
            artifact_key="c" * 64,
            content_hash="d" * 64,
            course_id=biology.id,
        )
        AcademicRepository.replace_document_chunks(
            session,
            document_id=history_doc.id,
            chunks=[
                DocumentChunkInput(
                    ordinal=0,
                    heading="Makeup tests",
                    content="Makeup tests require dean approval and a documented reason.",
                    citation=SourceCitation(page=5),
                )
            ],
        )
        AcademicRepository.replace_document_chunks(
            session,
            document_id=biology_doc.id,
            chunks=[
                DocumentChunkInput(
                    ordinal=0,
                    heading="Makeup tests",
                    content="Makeup tests require instructor approval in biology.",
                    citation=SourceCitation(page=9),
                )
            ],
        )
        history_id = history.id
        original_assignment_due = assignment.due_at
        original_quiz_due = quiz.due_at

    with Session(engine) as session:
        retrieved = retrieve_with_academic_repository(
            session,
            question="makeup tests",
            course_id=history_id,
            term="2026-spring",
            limit=4,
        )
    assert len(retrieved) == 1
    assert retrieved[0].chunk.heading == "Makeup tests"
    assert retrieved[0].citation.locator == "page 5"
    facts = SQLAlchemyAcademicPlannerStore(engine).load_planner_facts(
        now=now,
        horizon_days=7,
    )
    assert {fact.id for fact in facts.ambiguous_facts} == {"notion-ambiguous-pdf"}
    assert {assessment.id for assessment in facts.assessments} == {
        "notion-assignment-1",
        "notion-quiz-1",
    }

    store = SQLAlchemyAcademicPlannerStore(engine)
    delivery = DeliveryRecorder()
    result = await run_morning_plan(
        store=store,
        delivery=delivery,
        now=now,
        horizon_days=7,
    )
    assert result["status"] == "succeeded"
    assert result["ambiguous_count"] == 1
    assert ("question", "academic-ambiguity:notion-ambiguous-pdf:v1", "notion-ambiguous-pdf") in (
        delivery.calls
    )

    plan = store.get_latest_daily_plan()
    assert plan is not None
    assert plan.ambiguous_questions == ()
    assert {block.assessment_id for block in plan.blocks} <= {
        "notion-assignment-1",
        "notion-quiz-1",
    }
    assert any(
        block.assessment_id == "notion-assignment-1" and block.carried_over for block in plan.blocks
    )
    protected = (
        (class_start - timedelta(minutes=20), class_end + timedelta(minutes=20)),
        (test_start - timedelta(minutes=20), test_end + timedelta(minutes=20)),
    )
    assert all(
        not _overlaps(block.start_at, block.end_at, start, end)
        for block in plan.blocks
        for start, end in protected
    )

    with Session(engine) as session:
        stored_assignment = session.scalar(
            select(StoredAssessment).where(StoredAssessment.notion_id == "notion-assignment-1")
        )
        stored_quiz = session.scalar(
            select(StoredAssessment).where(StoredAssessment.notion_id == "notion-quiz-1")
        )
        stored_commitments = list(
            session.scalars(
                select(StoredCommitment).where(
                    StoredCommitment.notion_id.in_(["notion-class-1", "notion-test-1"])
                )
            )
        )
        carried_rows = list(
            session.scalars(select(StudyBlock).where(StudyBlock.status == "carried_forward"))
        )
    assert stored_assignment is not None
    assert stored_assignment.due_at is not None
    assert _stored_utc(stored_assignment.due_at) == original_assignment_due
    assert stored_quiz is not None
    assert stored_quiz.due_at is not None
    assert _stored_utc(stored_quiz.due_at) == original_quiz_due
    commitment_times = {
        (_stored_utc(row.starts_at), _stored_utc(row.ends_at)) for row in stored_commitments
    }
    assert commitment_times == {
        (class_start, class_end),
        (test_start, test_end),
    }
    assert len(carried_rows) == 1

    requests: list[httpx.Request] = []

    def notion_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"id": "assignment-page-1", "url": "https://www.notion.so/assignment-page-1"},
        )

    properties = _properties()
    async with httpx.AsyncClient(transport=httpx.MockTransport(notion_handler)) as client:
        writer = AcademicNotionWriter(
            connector=NotionConnector(
                token="secret",
                database_ids={
                    "courses": "courses-id",
                    "assessments": "assessments-id",
                    "study_blocks": "study-blocks-id",
                },
                property_ids=properties,
                client=client,
            ),
            targets={
                "notion-assignment-1": NotionPageTarget(
                    target_id="notion-assignment-1",
                    page_id="assignment-page-1",
                    database="assessments",
                )
            },
            property_ids=properties,
        )
        proposal = await create_checkin_proposal(
            store=store,
            reply="completed notion-assignment-1",
            plan_id=plan.plan_id,
        )
        denied = await confirm_checkin_proposal(
            store=store,
            writer=writer,
            proposal_id=proposal.proposal_id,
            confirmation_event=proposal.confirmation_event + " ",
        )
        applied = await confirm_checkin_proposal(
            store=store,
            writer=writer,
            proposal_id=proposal.proposal_id,
            confirmation_event=proposal.confirmation_event,
        )
        replay = await confirm_checkin_proposal(
            store=store,
            writer=writer,
            proposal_id=proposal.proposal_id,
            confirmation_event=proposal.confirmation_event,
        )

    assert denied["status"] == "confirmation_required"
    assert applied["status"] == "applied"
    assert replay["status"] == "applied"
    assert len(requests) == 1
    assert requests[0].method == "PATCH"
    assert requests[0].url.path == "/v1/pages/assignment-page-1"
    request_body = requests[0].content.decode()
    assert "assessments-status" in request_body
    assert "Completed" in request_body
    with Session(engine) as session:
        assert session.scalar(select(AcademicProposedChange)).state == "applied"


def test_phase5_allocator_handles_toronto_dst_boundaries_with_zoneinfo() -> None:
    spring_start = datetime(2026, 3, 8, 1, 30, tzinfo=TORONTO)
    spring_blocks = allocate_plan(
        PlannerFacts(
            assessments=(
                Assessment(
                    id="spring-assignment",
                    course="HIST-201",
                    title="Spring forward review",
                    assessment_type=AssessmentType.ASSIGNMENT,
                    due_at=datetime(2026, 3, 8, 5, 30, tzinfo=TORONTO),
                    estimated_minutes=60,
                    weight_percent=10,
                ),
            ),
            availability=(
                AvailabilityWindow(
                    start_at=spring_start,
                    end_at=datetime(2026, 3, 8, 4, 30, tzinfo=TORONTO),
                ),
            ),
        ),
        now=spring_start,
    )
    assert spring_blocks
    assert spring_blocks[0].start_at == datetime(2026, 3, 8, 6, 30, tzinfo=UTC)

    first_fold_start = datetime(2026, 11, 1, 1, 0, tzinfo=TORONTO, fold=0)
    first_fold_end = datetime(2026, 11, 1, 1, 30, tzinfo=TORONTO, fold=0)
    second_fold_start = datetime(2026, 11, 1, 1, 0, tzinfo=TORONTO, fold=1)
    second_fold_end = datetime(2026, 11, 1, 1, 30, tzinfo=TORONTO, fold=1)
    fall_start = datetime(2026, 11, 1, 0, 30, tzinfo=TORONTO)
    fall_blocks = allocate_plan(
        PlannerFacts(
            assessments=(
                Assessment(
                    id="fall-assignment",
                    course="HIST-201",
                    title="Fall back review",
                    assessment_type=AssessmentType.QUIZ,
                    due_at=datetime(2026, 11, 1, 4, 0, tzinfo=TORONTO),
                    estimated_minutes=30,
                    weight_percent=10,
                ),
            ),
            availability=(
                AvailabilityWindow(
                    start_at=fall_start,
                    end_at=datetime(2026, 11, 1, 2, 30, tzinfo=TORONTO),
                ),
            ),
            commitments=(
                FixedCommitment(
                    id="first-fold-class",
                    title="Repeated hour class",
                    start_at=first_fold_start,
                    end_at=first_fold_end,
                    kind="class",
                ),
                FixedCommitment(
                    id="second-fold-test",
                    title="Repeated hour test",
                    start_at=second_fold_start,
                    end_at=second_fold_end,
                    kind="test",
                ),
            ),
        ),
        now=fall_start,
    )
    assert first_fold_start.astimezone(UTC) != second_fold_start.astimezone(UTC)
    assert fall_blocks
    assert all(
        not _overlaps(
            block.start_at,
            block.end_at,
            first_fold_start.astimezone(UTC),
            first_fold_end.astimezone(UTC),
        )
        for block in fall_blocks
    )
    assert all(
        not _overlaps(
            block.start_at,
            block.end_at,
            second_fold_start.astimezone(UTC),
            second_fold_end.astimezone(UTC),
        )
        for block in fall_blocks
    )
