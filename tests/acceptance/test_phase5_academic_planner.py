from __future__ import annotations

import asyncio
import inspect
import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import fitz
import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agents.academic_planner.agent_clarification import AcademicAgentClarificationService
from app.agents.academic_planner.allocator import allocate_plan
from app.agents.academic_planner.contracts import (
    AcademicAgentDecision,
    AcademicAgentWireDecision,
    AcademicRequestRouteDecision,
    Assessment,
    AssessmentType,
    AvailabilityWindow,
    CreateStudySessionCall,
    FixedCommitment,
    MorningBriefing,
    PlanCritique,
    PlannerFacts,
    SearchCoursesCall,
    WorkBreakdown,
)
from app.agents.academic_planner.discord_checkin import AcademicDiscordCheckinHandler
from app.agents.academic_planner.documents import detect_ambiguous_deadlines, extract_document
from app.agents.academic_planner.material_ingestion import AssessmentMaterialIngestionService
from app.agents.academic_planner.material_reasoning import (
    AssessmentMaterialDecision,
    AssessmentMaterialInsightCritique,
    AssessmentMaterialMorningValidator,
    AssessmentMaterialReasonerService,
    AssessmentMaterialToolCall,
    MorningMaterialGroundingCritique,
)
from app.agents.academic_planner.morning_notification import (
    execute_scheduled_morning_notification,
    scheduled_delivery_key,
)
from app.agents.academic_planner.notion_mutations import DiscoveredAcademicNotionWriter
from app.agents.academic_planner.retrieval import retrieve_with_academic_repository
from app.agents.academic_planner.sync import AcademicNotionSyncResult
from app.agents.academic_planner.workflow import (
    confirm_checkin_proposal,
    create_checkin_proposal,
    run_morning_plan,
)
from app.artifacts.store import ArtifactStore
from app.connectors.discord import (
    DiscordAcademicPlannerAdapter,
    DiscordAcademicPlannerDelivery,
    DiscordAcademicResponseDelivery,
)
from app.connectors.discord_gateway import DiscordAcademicMessageCreate
from app.connectors.notion import (
    AcademicNotionWriter,
    NotionAssessmentMaterials,
    NotionConnector,
    NotionMaterialFile,
    NotionPageTarget,
)
from app.core.errors import ErrorCode
from app.db.academic import (
    AcademicRepository,
    DocumentChunkInput,
    SourceCitation,
    SQLAlchemyAcademicPlannerStore,
)
from app.db.models import (
    AcademicDocumentChunk,
    AcademicProposalOperationJournal,
    AcademicProposedChange,
    Base,
    StudyBlock,
    StudyPlan,
)
from app.db.models import (
    Assessment as StoredAssessment,
)
from app.db.models import (
    Delivery as StoredDelivery,
)
from app.db.models import (
    FixedCommitment as StoredCommitment,
)
from app.db.repositories import RunRepository
from app.llm.embeddings import EmbeddingStatus
from app.queue.periodic import PeriodicOccurrence, stable_period_key

TORONTO = ZoneInfo("America/Toronto")
ACCEPTANCE_CHANNEL_ID = "987654321012345678"
ACCEPTANCE_OWNER_ID = "123456789012345678"
ACCEPTANCE_ASSISTANT_ID = "777777777777777777"


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
        self.morning_briefing: MorningBriefing | None = None

    async def send_morning_plan(self, briefing, *, idempotency_key: str) -> object:
        self.morning_briefing = briefing
        self.calls.append(("morning", idempotency_key, briefing.message_text))
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


class BriefingModel:
    def __init__(self) -> None:
        self.morning_contexts = []

    async def breakdown(self, assessment) -> WorkBreakdown:
        return WorkBreakdown(
            assessment_id=assessment.id,
            steps=(f"Review {assessment.title}",),
            estimated_minutes=assessment.estimated_minutes,
            rationale="Use the scheduled deterministic block.",
        )

    async def critique(self, plan) -> PlanCritique:
        return PlanCritique(acceptable=True, concerns=())

    async def morning_briefing(self, context) -> MorningBriefing:
        self.morning_contexts.append(context)
        assessments = " ".join(
            (
                f"{item.title} for {item.course_code} is due "
                f"{item.exact_date_label} ({item.relative_date_label})."
            )
            for item in context.assessments
        )
        blocks = " ".join(
            f"Study {item.duration_minutes} minutes for {item.title}."
            for item in context.scheduled_blocks
        )
        return MorningBriefing(
            message_text=(
                "Good morning, Richard. "
                + " ".join(part for part in (assessments, blocks) if part)
                + " Have a good day!"
            ),
            referenced_assessment_ids=tuple(item.assessment_id for item in context.assessments),
            referenced_block_ids=tuple(item.block_id for item in context.scheduled_blocks),
        )

    async def extract_checkin(self, reply):
        return ()


class ReadyRuntime:
    async def ensure_ready(self) -> object:
        return object()


class QueuedAcademicGateway:
    def __init__(self, *decisions: AcademicAgentDecision) -> None:
        self._decisions = list(decisions)
        self.prompts: list[str] = []

    async def invoke_structured(self, *, prompt: str, response_model):
        assert response_model is AcademicAgentWireDecision
        self.prompts.append(prompt)
        if not self._decisions:
            raise AssertionError("unexpected academic agent turn")
        return SimpleNamespace(output=self._decisions.pop(0))


class CalendarSemanticRouter:
    async def invoke_structured(self, *, prompt: str, response_model):
        assert response_model is AcademicRequestRouteDecision
        payload = json.loads(prompt.partition("\n")[2])
        return SimpleNamespace(
            output=AcademicRequestRouteDecision(
                calendar_request=payload["message_untrusted"],
            )
        )


def _discord_message(
    message_id: str,
    content: str,
    *,
    timestamp: datetime,
) -> DiscordAcademicMessageCreate:
    mentions = (
        (ACCEPTANCE_ASSISTANT_ID,)
        if re.search(rf"<@!?{ACCEPTANCE_ASSISTANT_ID}>", content) is not None
        else ()
    )
    return DiscordAcademicMessageCreate(
        message_id=message_id,
        channel_id=ACCEPTANCE_CHANNEL_ID,
        author_id=ACCEPTANCE_OWNER_ID,
        timestamp=timestamp,
        content=SecretStr(content),
        mentioned_user_ids=mentions,
    )


def _sent_discord_contents(requests: list[httpx.Request]) -> list[str]:
    contents: list[str] = []
    for request in requests:
        if request.method != "POST" or not request.url.path.endswith("/messages"):
            continue
        body = json.loads(request.content)
        content = body.get("content")
        if isinstance(content, str):
            contents.append(content)
    return contents


class _StaticAcademicSync:
    def __init__(self, result: AcademicNotionSyncResult) -> None:
        self.result = result
        self.calls: list[datetime | None] = []

    async def sync(self, *, now: datetime | None = None) -> AcademicNotionSyncResult:
        self.calls.append(now)
        return self.result


@pytest.mark.asyncio
async def test_scheduled_morning_notification_uses_fresh_sync_sql_plan_and_discord_once(
    engine,
) -> None:
    occurrence = PeriodicOccurrence(
        local_time=datetime(2026, 9, 10, 8, 0, tzinfo=TORONTO),
        scheduled_at=datetime(2026, 9, 10, 8, 0, tzinfo=TORONTO).astimezone(UTC),
    )
    period_key = stable_period_key("academic-morning", occurrence)
    assert "model" not in inspect.signature(execute_scheduled_morning_notification).parameters
    with Session(engine) as session, session.begin():
        ece = AcademicRepository.upsert_course(
            session,
            notion_id="course-ece250-morning",
            course_code="ECE 250",
            title="Data Structures and Algorithms",
            term="2026F",
            priority=90,
        )
        math = AcademicRepository.upsert_course(
            session,
            notion_id="course-math239-morning",
            course_code="MATH 239",
            title="Combinatorics",
            term="2026F",
            priority=70,
        )
        AcademicRepository.upsert_assessment(
            session,
            notion_id="morning-ece-quiz-review",
            course_id=ece.id,
            title="ECE 250 Quiz 1 review",
            assessment_type="quiz",
            due_at=datetime(2026, 9, 11, 10, 0, tzinfo=TORONTO),
            grade_weight_percent=50,
            estimated_minutes=45,
            confidence=1.0,
            fact_state="confirmed",
            citation=SourceCitation(block="ece-quiz-row"),
        )
        AcademicRepository.upsert_assessment(
            session,
            notion_id="morning-math-assignment",
            course_id=math.id,
            title="MATH 239 Assignment 2",
            assessment_type="assignment",
            due_at=datetime(2026, 9, 12, 20, 0, tzinfo=TORONTO),
            grade_weight_percent=20,
            estimated_minutes=60,
            confidence=1.0,
            fact_state="confirmed",
            citation=SourceCitation(block="math-assignment-row"),
        )
        AcademicRepository.upsert_preferences(
            session,
            scope="default",
            timezone="America/Toronto",
            availability={
                "windows": [
                    {
                        "start_at": datetime(2026, 9, 10, 9, 0, tzinfo=TORONTO).isoformat(),
                        "end_at": datetime(2026, 9, 10, 12, 0, tzinfo=TORONTO).isoformat(),
                    }
                ]
            },
            daily_capacity_minutes=180,
            buffer_minutes=0,
        )
        run = RunRepository.create_or_get(
            session,
            idempotency_key=period_key,
            agent_name="academic_morning_notification",
            trigger="schedule",
            schedule="academic-morning",
            input_version="academic-morning:v1",
        )
        run_id = run.id

    syncer = _StaticAcademicSync(
        AcademicNotionSyncResult(
            status="succeeded",
            course_count=2,
            valid_course_count=2,
            assessment_count=2,
            synced_at=occurrence.scheduled_at,
        )
    )
    store = SQLAlchemyAcademicPlannerStore(engine)
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"id": "123456789012345678", "guild_id": "42"},
            request=request,
        )

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10",
        transport=httpx.MockTransport(respond),
    ) as client:
        delivery = DiscordAcademicPlannerDelivery(
            engine=engine,
            run_id=run_id,
            channel_id=ACCEPTANCE_CHANNEL_ID,
            adapter=DiscordAcademicPlannerAdapter(
                token=SecretStr("discord-token"),
                allowed_channel_ids={ACCEPTANCE_CHANNEL_ID},
                client=client,
            ),
        )
        result = await execute_scheduled_morning_notification(
            store=store,
            syncer=syncer,
            delivery=delivery,
            occurrence=occurrence,
            period_key=period_key,
            executed_at=occurrence.scheduled_at,
            timezone_name="America/Toronto",
        )
        replay = await execute_scheduled_morning_notification(
            store=store,
            syncer=syncer,
            delivery=delivery,
            occurrence=occurrence,
            period_key=period_key,
            executed_at=occurrence.scheduled_at,
            timezone_name="America/Toronto",
        )

    expected = (
        "Good morning, Richard. Today's plan for Thursday, September 10, 2026:\n"
        "- 09:00 — ECE 250 Quiz 1 review (45 minutes, assessment)\n"
        "- 09:45 — MATH 239 Assignment 2 (60 minutes, assessment)\n"
        "Have a good day!"
    )
    assert result["status"] == "succeeded"
    assert result["block_count"] == 2
    assert replay["status"] == "succeeded"
    assert syncer.calls == [occurrence.scheduled_at, occurrence.scheduled_at]
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["content"] == expected
    assert body["allowed_mentions"] == {"parse": []}
    assert body["enforce_nonce"] is True
    with Session(engine) as session:
        plan = session.scalar(select(StudyPlan))
        assert plan is not None
        stored_blocks = list(
            session.scalars(select(StudyBlock).order_by(StudyBlock.starts_at, StudyBlock.title))
        )
        deliveries = list(session.scalars(select(StoredDelivery)))
    assert [row.title for row in stored_blocks] == [
        "ECE 250 Quiz 1 review",
        "MATH 239 Assignment 2",
    ]
    assert [row.allocated_minutes for row in stored_blocks] == [45, 60]
    assert [row.block_kind for row in stored_blocks] == ["assessment", "assessment"]
    assert len(deliveries) == 1
    assert deliveries[0].idempotency_key == scheduled_delivery_key(period_key, occurrence)
    assert deliveries[0].status == "sent"
    assert deliveries[0].attempt_count == 1
    assert body["nonce"] == deliveries[0].id.hex[:25]


@pytest.mark.asyncio
async def test_scheduled_morning_source_failure_sends_actionable_discord_not_light_day(
    engine,
) -> None:
    occurrence = PeriodicOccurrence(
        local_time=datetime(2026, 9, 10, 8, 0, tzinfo=TORONTO),
        scheduled_at=datetime(2026, 9, 10, 8, 0, tzinfo=TORONTO).astimezone(UTC),
    )
    period_key = stable_period_key("academic-morning", occurrence)
    with Session(engine) as session, session.begin():
        run = RunRepository.create_or_get(
            session,
            idempotency_key=period_key,
            agent_name="academic_morning_notification",
            trigger="schedule",
            schedule="academic-morning",
            input_version="academic-morning:v1",
        )
        run_id = run.id

    syncer = _StaticAcademicSync(
        AcademicNotionSyncResult(
            status="setup_required",
            diagnostic_codes=("notion_configuration_missing",),
            error_code=ErrorCode.SOURCE_SETUP_REQUIRED.value,
            retryable=False,
        )
    )
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"id": "123456789012345679", "guild_id": "42"},
            request=request,
        )

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10",
        transport=httpx.MockTransport(respond),
    ) as client:
        result = await execute_scheduled_morning_notification(
            store=SQLAlchemyAcademicPlannerStore(engine),
            syncer=syncer,
            delivery=DiscordAcademicPlannerDelivery(
                engine=engine,
                run_id=run_id,
                channel_id=ACCEPTANCE_CHANNEL_ID,
                adapter=DiscordAcademicPlannerAdapter(
                    token=SecretStr("discord-token"),
                    allowed_channel_ids={ACCEPTANCE_CHANNEL_ID},
                    client=client,
                ),
            ),
            occurrence=occurrence,
            period_key=period_key,
            executed_at=occurrence.scheduled_at,
            timezone_name="America/Toronto",
        )

    assert result["status"] == "attention"
    assert result["error_code"] == ErrorCode.SOURCE_SETUP_REQUIRED.value
    assert result["delivery_count"] == 1
    assert len(requests) == 1
    content = json.loads(requests[0].content)["content"]
    assert "could not refresh the Notion academic source" in content
    assert "Please check the Notion token, Courses database, and sharing." in content
    assert "no scheduled study blocks" not in content
    with Session(engine) as session:
        assert session.scalar(select(StudyPlan)) is None
        delivery = session.scalar(select(StoredDelivery))
    assert delivery is not None
    assert delivery.idempotency_key.endswith(f"{ErrorCode.SOURCE_SETUP_REQUIRED.value}:v1")
    assert delivery.status == "sent"


@pytest.mark.asyncio
async def test_conversation_triggered_study_session_flow_reaches_notion_once(
    engine, tmp_path: Path
) -> None:
    class MemoryNotApplicable:
        async def handle_memory_review(self, **_kwargs: Any) -> object:
            return SimpleNamespace(status="not_applicable", response=None)

        async def handle_reflection(self, **_kwargs: Any) -> object:
            return SimpleNamespace(status="not_applicable", response=None)

    store = SQLAlchemyAcademicPlannerStore(engine)
    store.upsert_course_calendar(
        {
            "notion_id": "course-ece250-page",
            "course_code": "ECE 250",
            "course_title": "Data Structures and Algorithms",
            "term": "2026F",
            "child_database_id": "ece250-assessments-db",
            "child_data_source_id": "ece250-assessments-source",
            "title_property_id": "title-prop",
            "date_property_id": "date-prop",
        },
        status="valid",
        schema_fingerprint="ece250-schema",
    )
    course = store.search_courses("ECE 250")[0]
    start = datetime(2026, 9, 10, 19, 0, tzinfo=TORONTO)
    gateway = QueuedAcademicGateway(
        AcademicAgentDecision(
            question=(
                "When should I schedule the ECE 250 race conditions and insertion sort "
                "study blocks, and how long should each be?"
            )
        ),
        AcademicAgentDecision(
            tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 250"),)
        ),
        AcademicAgentDecision(
            tool_calls=(
                CreateStudySessionCall(
                    tool="create_study_session",
                    course_id=course.course_id,
                    topic="Race conditions",
                    starts_at=start,
                    duration_minutes=45,
                ),
                CreateStudySessionCall(
                    tool="create_study_session",
                    course_id=course.course_id,
                    topic="Insertion sort",
                    starts_at=start + timedelta(minutes=45),
                    duration_minutes=45,
                ),
            )
        ),
        AcademicAgentDecision(
            tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 250"),)
        ),
        AcademicAgentDecision(
            tool_calls=(
                CreateStudySessionCall(
                    tool="create_study_session",
                    course_id=course.course_id,
                    topic="Graph traversals",
                    starts_at=datetime(2026, 9, 10, 21, 0, tzinfo=TORONTO),
                    duration_minutes=30,
                ),
            )
        ),
    )

    discord_requests: list[httpx.Request] = []
    notion_requests: list[httpx.Request] = []

    def discord_respond(request: httpx.Request) -> httpx.Response:
        discord_requests.append(request)
        external_id = str(200000000000000000 + len(discord_requests))
        return httpx.Response(
            200,
            json={"id": external_id, "guild_id": "42"},
            request=request,
        )

    def notion_respond(request: httpx.Request) -> httpx.Response:
        notion_requests.append(request)
        page_number = len(notion_requests)
        return httpx.Response(
            200,
            json={
                "id": f"notion-study-page-{page_number}",
                "url": f"https://notion.test/{page_number}",
            },
            request=request,
        )

    async with (
        httpx.AsyncClient(
            base_url="https://discord.com/api/v10",
            transport=httpx.MockTransport(discord_respond),
        ) as discord_client,
        httpx.AsyncClient(transport=httpx.MockTransport(notion_respond)) as notion_client,
    ):
        writer = DiscoveredAcademicNotionWriter(
            connector=NotionConnector(
                token=SecretStr("notion-token"),
                courses_database_id="courses-db",
                client=notion_client,
            ),
            target_store=store,
        )
        handler = AcademicDiscordCheckinHandler(
            store=store,
            delivery=DiscordAcademicResponseDelivery(
                engine=engine,
                channel_id=ACCEPTANCE_CHANNEL_ID,
                adapter=DiscordAcademicPlannerAdapter(
                    token=SecretStr("discord-token"),
                    allowed_channel_ids={ACCEPTANCE_CHANNEL_ID},
                    client=discord_client,
                ),
            ),
            allowed_channel_ids={ACCEPTANCE_CHANNEL_ID},
            authorized_user_ids={ACCEPTANCE_OWNER_ID},
            writer_provider=lambda: writer,
            ollama_runtime=ReadyRuntime(),
            agent_gateway=gateway,
            semantic_router_gateway=CalendarSemanticRouter(),
            agent_catalog=store,
            assistant_user_id=ACCEPTANCE_ASSISTANT_ID,
            memory_service=MemoryNotApplicable(),
            agent_clarification_service=AcademicAgentClarificationService(
                engine=engine,
                artifact_store=ArtifactStore(tmp_path / "agent-clarifications"),
            ),
        )

        now = datetime(2026, 9, 9, 18, 0, tzinfo=TORONTO)
        initial = _discord_message(
            "100000000000000001",
            (
                f"<@{ACCEPTANCE_ASSISTANT_ID}> I need to study for ECE 250, specifically "
                "race conditions and insertion sort."
            ),
            timestamp=now,
        )
        assert (await handler(initial)).status == "handled"
        assert notion_requests == []
        clarification = next(
            content
            for content in _sent_discord_contents(discord_requests)
            if "Reply with the requested details, or say cancel" in content
        )
        assert clarification.startswith(
            "When should I schedule the ECE 250 race conditions and insertion sort "
            "study blocks, and how long should each be?"
        )
        assert "No Notion change is ready to confirm" not in clarification
        assert f"<@{ACCEPTANCE_ASSISTANT_ID}>" not in clarification

        continuation = _discord_message(
            "100000000000000002",
            "Tomorrow at 7 PM, 45 minutes each.",
            timestamp=now + timedelta(minutes=1),
        )
        assert (await handler(continuation)).status == "handled"
        assert notion_requests == []
        preview = next(
            content
            for content in reversed(_sent_discord_contents(discord_requests))
            if content.startswith("Proposed academic updates")
        )
        assert (
            "- Create `Studying Block — Race conditions` in ECE 250, "
            "September 10, 2026, 7:00\N{EN DASH}7:45 PM America/Toronto (45 minutes)."
        ) in preview
        assert (
            "- Create `Studying Block — Insertion sort` in ECE 250, "
            "September 10, 2026, 7:45\N{EN DASH}8:30 PM America/Toronto (45 minutes)."
        ) in preview
        match = re.search(r"Confirm exactly: confirm ([0-9a-f-]{36})", preview)
        assert match is not None
        proposal_id = match.group(1)

        confirmation = _discord_message(
            "100000000000000003",
            f"confirm {proposal_id}",
            timestamp=now + timedelta(minutes=2),
        )
        assert (await handler(confirmation)).status == "handled"
        notion_page_requests = [
            request
            for request in notion_requests
            if request.method == "POST" and request.url.path.endswith("/pages")
        ]
        assert len(notion_page_requests) == 2
        notion_bodies = [json.loads(request.content) for request in notion_page_requests]
        assert [
            body["properties"]["title-prop"]["title"][0]["text"]["content"]
            for body in notion_bodies
        ] == [
            "Studying Block — Race conditions",
            "Studying Block — Insertion sort",
        ]
        assert [body["properties"]["date-prop"]["date"] for body in notion_bodies] == [
            {"start": "2026-09-10T23:00:00Z", "end": "2026-09-10T23:45:00Z"},
            {"start": "2026-09-10T23:45:00Z", "end": "2026-09-11T00:30:00Z"},
        ]

        with Session(engine) as session:
            journal_rows = session.scalars(
                select(AcademicProposalOperationJournal).order_by(
                    AcademicProposalOperationJournal.ordinal
                )
            ).all()
        assert [row.state for row in journal_rows] == ["applied", "applied"]

        discord_post_count = len(_sent_discord_contents(discord_requests))
        assert (await handler(initial)).status == "duplicate"
        assert (await handler(continuation)).status == "ignored"
        assert (await handler(confirmation)).status == "handled"
        assert (
            len(
                [
                    request
                    for request in notion_requests
                    if request.method == "POST" and request.url.path.endswith("/pages")
                ]
            )
            == 2
        )
        assert len(_sent_discord_contents(discord_requests)) == discord_post_count

        second_initial = _discord_message(
            "100000000000000004",
            (
                f"<@{ACCEPTANCE_ASSISTANT_ID}> Please schedule study time for ECE 250 "
                "graph traversals tomorrow at 9 PM for 30 minutes."
            ),
            timestamp=now + timedelta(minutes=3),
        )
        assert (await handler(second_initial)).status == "handled"
        second_preview = next(
            content
            for content in reversed(_sent_discord_contents(discord_requests))
            if content.startswith("Proposed academic updates") and "Graph traversals" in content
        )
        reject_match = re.search(r"Reject exactly: reject ([0-9a-f-]{36})", second_preview)
        assert reject_match is not None
        rejection = _discord_message(
            "100000000000000005",
            f"reject {reject_match.group(1)}",
            timestamp=now + timedelta(minutes=4),
        )
        assert (await handler(rejection)).status == "handled"
        assert (
            len(
                [
                    request
                    for request in notion_requests
                    if request.method == "POST" and request.url.path.endswith("/pages")
                ]
            )
            == 2
        )
        assert any(
            "is rejected. No Notion change was made." in content
            for content in _sent_discord_contents(discord_requests)
        )


@pytest.mark.asyncio
async def test_morning_briefing_reaches_discord_user_boundary_within_timeout(engine) -> None:
    now = datetime(2026, 9, 9, 8, 0, tzinfo=TORONTO)
    facts = PlannerFacts(
        assessments=(
            Assessment(
                id="quiz-circuits",
                course="ECE 222",
                title="Linear Circuits quiz",
                assessment_type=AssessmentType.QUIZ,
                due_at=datetime(2026, 9, 10, 10, 0, tzinfo=TORONTO),
                estimated_minutes=60,
                weight_percent=10,
            ),
        ),
        availability=(
            AvailabilityWindow(
                start_at=now,
                end_at=now + timedelta(hours=2),
            ),
        ),
    )

    class FactStore:
        def __init__(self) -> None:
            self.plan = None

        def load_planner_facts(self, *, now, horizon_days):
            return facts

        def save_daily_plan(self, plan) -> None:
            self.plan = plan

    with Session(engine) as session, session.begin():
        run = RunRepository.create_or_get(
            session,
            idempotency_key="academic-user-boundary:2026-09-09",
            agent_name="academic_planner",
            trigger="schedule",
        )
        run_id = run.id

    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"id": "123456789012345678", "guild_id": "42"},
            request=request,
        )

    channel_id = "987654321012345678"
    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10",
        transport=httpx.MockTransport(respond),
    ) as client:
        delivery = DiscordAcademicPlannerDelivery(
            engine=engine,
            run_id=run_id,
            channel_id=channel_id,
            adapter=DiscordAcademicPlannerAdapter(
                token=SecretStr("test-token"),
                allowed_channel_ids={channel_id},
                client=client,
            ),
        )
        result = await asyncio.wait_for(
            run_morning_plan(
                store=FactStore(),
                delivery=delivery,
                model=BriefingModel(),
                now=now,
            ),
            timeout=1,
        )

    assert result["status"] == "succeeded"
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["content"].startswith("Good morning, Richard.")
    assert "September 10, 2026 (tomorrow)" in body["content"]
    assert "60 minutes" in body["content"]
    assert body["allowed_mentions"] == {"parse": []}
    assert body["enforce_nonce"] is True


@pytest.mark.asyncio
async def test_assessment_material_reaches_grounded_discord_morning_guidance(
    engine, tmp_path: Path
) -> None:
    """Exercise acquisition through private storage, retrieval, validation, and delivery."""

    now = datetime(2026, 9, 9, 8, 0, tzinfo=TORONTO)
    assessment_page_id = "assessment-material-page"
    with Session(engine) as session, session.begin():
        course = AcademicRepository.upsert_course(
            session,
            notion_id="course-material-page",
            course_code="ECE 222",
            title="Linear Circuits",
            term="2026F",
        )
        AcademicRepository.upsert_assessment(
            session,
            notion_id=assessment_page_id,
            course_id=course.id,
            title="Assignment 1",
            assessment_type="assignment",
            due_at=datetime(2026, 9, 10, 20, 0, tzinfo=TORONTO),
            grade_weight_percent=40,
            estimated_minutes=60,
            confidence=1.0,
            fact_state="confirmed",
            citation=SourceCitation(block="assessment-row"),
        )
        AcademicRepository.upsert_preferences(
            session,
            scope="default",
            timezone="America/Toronto",
            availability={
                "windows": [
                    {
                        "start_at": now.isoformat(),
                        "end_at": (now + timedelta(hours=2)).isoformat(),
                    }
                ]
            },
            daily_capacity_minutes=120,
            buffer_minutes=0,
        )

    snapshot = NotionAssessmentMaterials(
        assessment_page_id=assessment_page_id,
        last_edited_at=now.astimezone(UTC),
        files=(
            NotionMaterialFile(
                source_kind="notion_property_file",
                source_page_id=assessment_page_id,
                source_property_id="materials-property",
                source_key=f"{assessment_page_id}:property:materials-property:file:0",
                order=0,
                name="assignment-one.pdf",
                url=("https://prod-files-secure.s3.us-west-2.amazonaws.com/assignment-one.pdf"),
                mime_type="application/pdf",
            ),
        ),
    )

    class Connector:
        async def retrieve_assessment_materials(self, page_id: str, **kwargs):
            assert page_id == assessment_page_id
            assert kwargs == {"max_depth": 8, "max_blocks": 1000, "max_requests": 20}
            return snapshot

        async def refresh_assessment_material_file(
            self, *, assessment_page_id: str, source_key: str
        ):
            assert assessment_page_id == snapshot.assessment_page_id
            assert source_key == snapshot.files[0].source_key
            return snapshot.files[0]

        async def download_attachment(self, attachment, *, max_bytes: int) -> bytes:
            assert attachment.name == "assignment-one.pdf"
            assert max_bytes == 1024 * 1024
            return _pdf(
                "Assignment 1 is worth 40%. Begin with linear circuits, then compare AC and DC "
                "behavior."
            )

    class Embeddings:
        model_identity = "acceptance-embedding:v1"

        async def embed_academic_text(self, text: str):
            vector = [1.0, float(len(text) % 7 + 1)]
            return SimpleNamespace(
                status=EmbeddingStatus.VALID,
                model_identity=self.model_identity,
                embedding=SimpleNamespace(vector=vector),
            )

    embeddings = Embeddings()
    artifacts = ArtifactStore(tmp_path / "material-artifacts")
    ingestion = AssessmentMaterialIngestionService(
        engine=engine,
        connector=Connector(),  # type: ignore[arg-type]
        artifact_store=artifacts,
        embedding_gateway=embeddings,
        max_bytes=1024 * 1024,
    )
    ingestion_result = await ingestion.ingest_assessment(assessment_page_id)
    assert ingestion_result.status == "succeeded"

    with Session(engine) as session:
        chunk = session.scalar(select(AcademicDocumentChunk))
        assert chunk is not None
        chunk_id = str(chunk.id)

    class GroundedModel:
        def __init__(self) -> None:
            self.decision_count = 0

        async def breakdown(self, assessment) -> WorkBreakdown:
            return WorkBreakdown(
                assessment_id=assessment.id,
                steps=("Review linear circuits", "Compare AC and DC behavior"),
                estimated_minutes=60,
                rationale="Follow the verified assignment requirements.",
            )

        async def critique(self, plan) -> PlanCritique:
            return PlanCritique(acceptable=True, concerns=())

        async def morning_briefing(self, context) -> MorningBriefing:
            assessment = context.assessments[0]
            block = context.scheduled_blocks[0]
            insight = context.material_insights[0]
            return MorningBriefing(
                message_text=(
                    f"Good morning, Richard. {assessment.title} for {assessment.course_code} "
                    f"is due {assessment.exact_date_label} ({assessment.relative_date_label}). "
                    f"Study {block.duration_minutes} minutes; it is worth 40%, so begin with "
                    "linear circuits and then compare AC and DC behavior. Have a good day!"
                ),
                referenced_assessment_ids=(assessment.assessment_id,),
                referenced_block_ids=(block.block_id,),
                referenced_insight_ids=(insight.insight_id,),
            )

        async def extract_checkin(self, reply):
            return ()

        async def invoke_structured(self, *, prompt: str, response_model: type[Any]) -> object:
            if response_model is AssessmentMaterialDecision:
                self.decision_count += 1
                if self.decision_count == 1:
                    output = AssessmentMaterialDecision(
                        tool_calls=(
                            AssessmentMaterialToolCall(
                                tool="semantic_search_assessment_materials",
                                assessment_id=assessment_page_id,
                                query="What should today's Assignment 1 block focus on?",
                                limit=4,
                            ),
                        )
                    )
                else:
                    from app.agents.academic_planner.contracts import GroundedAssessmentInsight

                    output = AssessmentMaterialDecision(
                        insights=(
                            GroundedAssessmentInsight(
                                insight_id="insight-assignment-material",
                                assessment_id=assessment_page_id,
                                text=(
                                    "Assignment 1 is worth 40%; begin with linear circuits, "
                                    "then compare AC and DC behavior."
                                ),
                                evidence_chunk_ids=(chunk_id,),
                            ),
                        )
                    )
            elif response_model is AssessmentMaterialInsightCritique:
                output = AssessmentMaterialInsightCritique(
                    insight_id="insight-assignment-material",
                    accepted=True,
                    entailed=True,
                    relevant_to_today=True,
                    safe=True,
                )
            elif response_model is MorningMaterialGroundingCritique:
                output = MorningMaterialGroundingCritique(
                    accepted=True,
                    no_new_material_claims=True,
                    no_cross_assessment_leakage=True,
                    recommendations_match_scheduled_blocks=True,
                )
            else:  # pragma: no cover - guards future protocol expansion
                raise AssertionError(response_model)
            return SimpleNamespace(output=output)

    model = GroundedModel()
    store = SQLAlchemyAcademicPlannerStore(engine, embedding_gateway=embeddings)
    with Session(engine) as session, session.begin():
        run = RunRepository.create_or_get(
            session,
            idempotency_key="material-guidance:2026-09-09",
            agent_name="academic_planner",
            trigger="schedule",
        )
        run_id = run.id

    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "123456789012345679"}, request=request)

    channel_id = "987654321012345678"
    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10",
        transport=httpx.MockTransport(respond),
    ) as client:
        delivery = DiscordAcademicPlannerDelivery(
            engine=engine,
            run_id=run_id,
            channel_id=channel_id,
            adapter=DiscordAcademicPlannerAdapter(
                token=SecretStr("test-token"),
                allowed_channel_ids={channel_id},
                client=client,
            ),
        )
        result = await run_morning_plan(
            store=store,
            delivery=delivery,
            model=model,
            material_reasoner=AssessmentMaterialReasonerService(model=model, store=store),
            semantic_validator=AssessmentMaterialMorningValidator(model),
            now=now,
        )

    assert result["material_insight_count"] == 1
    assert result["delivery_count"] == 1
    assert len(requests) == 1
    delivered = json.loads(requests[0].content)["content"]
    assert "worth 40%" in delivered
    assert "linear circuits" in delivered
    assert "AC and DC" in delivered


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
    model = BriefingModel()
    result = await run_morning_plan(
        store=store,
        delivery=delivery,
        model=model,
        now=now,
        horizon_days=7,
    )
    assert result["status"] == "succeeded"
    assert result["ambiguous_count"] == 1
    assert model.morning_contexts
    assert delivery.morning_briefing is not None
    assert any(
        call[0] == "morning"
        and call[1].endswith(":v2")
        and call[2] is not None
        and call[2].startswith("Good morning, Richard.")
        and "Today's academic plan:" not in call[2]
        for call in delivery.calls
    )
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
