"""End-to-end unit coverage for durable academic semantic memory."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session

from app.agents.academic_planner.contracts import (
    AcademicDiscourseDecision,
    CreateLearningFocusAction,
    DiscourseClarification,
    DiscourseIntent,
    DiscoursePartialFacts,
    ResolveLearningFocusAction,
    SearchLearningFocusesCall,
)
from app.agents.academic_planner.memory_workflow import AcademicMemoryService
from app.agents.academic_planner.workflow import build_daily_plan
from app.db.academic import (
    AcademicRepository,
    LearningFocusMemoryInput,
    SQLAlchemyAcademicPlannerStore,
)
from app.db.models import (
    AcademicDiscourseSession,
    AcademicDiscourseTurn,
    AcademicLearningFocus,
    AcademicReflectionMemory,
    Base,
    PlanningPreference,
)
from app.llm.embeddings import EmbeddingStatus


class Gateway:
    def __init__(self, decisions: Sequence[AcademicDiscourseDecision]) -> None:
        self._decisions = iter(decisions)

    async def invoke_structured(
        self,
        *,
        prompt: str,
        response_model: type[AcademicDiscourseDecision],
    ) -> object:
        del prompt, response_model
        return SimpleNamespace(output=next(self._decisions))


class Embeddings:
    async def embed_reflection_text(self, text: str) -> object:
        del text
        return SimpleNamespace(
            status=EmbeddingStatus.VALID,
            embedding=SimpleNamespace(vector=[0.1, 0.2, 0.3]),
            model_identity="test-embedding-v1",
            config_version="test-config-v1",
            error_code=None,
        )


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    created = create_engine(f"sqlite+pysqlite:///{tmp_path / 'memory-workflow.db'}")
    Base.metadata.create_all(created)
    try:
        yield created
    finally:
        created.dispose()


def _service(
    engine: Engine,
    decisions: Sequence[AcademicDiscourseDecision],
) -> AcademicMemoryService:
    store = SQLAlchemyAcademicPlannerStore(engine, default_practice_minutes=30)
    return AcademicMemoryService(
        store=store,
        model_gateway=Gateway(decisions),
        embedding_gateway=Embeddings(),
        timezone="America/Toronto",
        default_practice_minutes=30,
    )


@pytest.mark.asyncio
async def test_explicit_struggle_persists_raw_text_vector_and_next_day_practice(
    engine: Engine,
) -> None:
    raw_text = "I really struggled with my ECE 250 quiz on recursion."
    service = _service(
        engine,
        (
            AcademicDiscourseDecision(
                actions=(
                    CreateLearningFocusAction(
                        action="create_focus",
                        topic="recursion",
                        evidence_text=raw_text,
                    ),
                )
            ),
        ),
    )
    received_at = datetime(2026, 9, 8, 2, tzinfo=UTC)

    result = await service.handle_reflection(
        external_event_id="discord-memory-create",
        channel_id="123456",
        user_id="654321",
        raw_text=raw_text,
        received_at=received_at,
    )
    duplicate = await service.handle_reflection(
        external_event_id="discord-memory-create",
        channel_id="123456",
        user_id="654321",
        raw_text=raw_text,
        received_at=received_at,
    )

    assert result.status == "applied"
    assert "separate 30-minute practice block" in str(result.response)
    assert duplicate.status == "duplicate"
    with Session(engine) as session:
        focus = session.scalar(select(AcademicLearningFocus))
        memory = session.scalar(select(AcademicReflectionMemory))
        discourse = session.scalar(select(AcademicDiscourseSession))
        assert focus is not None
        assert focus.topic == "recursion"
        assert focus.practice_due_on is not None
        assert focus.next_review_at is not None
        assert focus.practice_due_on.isoformat() == "2026-09-08"
        assert focus.next_review_at.replace(tzinfo=UTC) == datetime(2026, 9, 9, 1, tzinfo=UTC)
        assert memory is not None
        assert memory.raw_text == raw_text
        assert memory.embedding == [0.1, 0.2, 0.3]
        assert memory.embedding_model == "test-embedding-v1"
        assert discourse is not None
        assert discourse.state == "completed"
        assert discourse.discord_channel_id == "123456"
        assert discourse.discord_user_id == "654321"


@pytest.mark.asyncio
async def test_clarification_resumes_same_session_and_embeds_combined_reflection(
    engine: Engine,
) -> None:
    service = _service(
        engine,
        (
            AcademicDiscourseDecision(
                clarification=DiscourseClarification(
                    question="Which topic was difficult?",
                    partial_facts=DiscoursePartialFacts(
                        intent=DiscourseIntent.CREATE_FOCUS,
                        evidence_text="I struggled on the quiz.",
                    ),
                )
            ),
            AcademicDiscourseDecision(
                actions=(
                    CreateLearningFocusAction(
                        action="create_focus",
                        topic="recursion",
                        evidence_text="ECE 250 recursion",
                    ),
                )
            ),
        ),
    )
    received_at = datetime(2026, 9, 8, 1, tzinfo=UTC)

    first = await service.handle_reflection(
        external_event_id="discord-memory-question",
        channel_id="123456",
        user_id="654321",
        raw_text="I struggled on the quiz.",
        received_at=received_at,
    )
    second = await service.handle_reflection(
        external_event_id="discord-memory-answer",
        channel_id="123456",
        user_id="654321",
        raw_text="ECE 250 recursion",
        received_at=received_at,
    )

    assert first.status == "clarification"
    assert first.response == "Which topic was difficult?"
    assert second.status == "applied"
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AcademicDiscourseSession)) == 1
        assert session.scalar(select(func.count()).select_from(AcademicDiscourseTurn)) == 2
        memory = session.scalar(select(AcademicReflectionMemory))
        assert memory is not None
        assert memory.raw_text == "I struggled on the quiz.\nECE 250 recursion"


@pytest.mark.asyncio
async def test_resolved_focus_hard_deletes_raw_text_and_embedding(engine: Engine) -> None:
    create_service = _service(
        engine,
        (
            AcademicDiscourseDecision(
                actions=(
                    CreateLearningFocusAction(
                        action="create_focus",
                        topic="recursion",
                        evidence_text="Recursion is still difficult.",
                    ),
                )
            ),
        ),
    )
    received_at = datetime(2026, 9, 8, 1, tzinfo=UTC)
    await create_service.handle_reflection(
        external_event_id="discord-memory-before-delete",
        channel_id="123456",
        user_id="654321",
        raw_text="Recursion is still difficult.",
        received_at=received_at,
    )
    with Session(engine) as session:
        focus_id = session.scalar(select(AcademicLearningFocus.id))
        assert focus_id is not None

    resolve_service = _service(
        engine,
        (
            AcademicDiscourseDecision(
                tool_calls=(
                    SearchLearningFocusesCall(
                        tool="search_learning_focuses",
                        query="recursion",
                    ),
                )
            ),
            AcademicDiscourseDecision(
                actions=(
                    ResolveLearningFocusAction(
                        action="resolve_focus",
                        focus_id=str(focus_id),
                        reason="The user said no more practice is needed.",
                    ),
                )
            ),
        ),
    )
    result = await resolve_service.handle_reflection(
        external_event_id="discord-memory-delete",
        channel_id="123456",
        user_id="654321",
        raw_text="No, I do not need recursion practice anymore.",
        received_at=received_at,
    )

    assert result.status == "applied"
    assert "including its stored reflection text and embedding" in str(result.response)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AcademicLearningFocus)) == 0
        assert session.scalar(select(func.count()).select_from(AcademicReflectionMemory)) == 0


@pytest.mark.asyncio
async def test_semantic_search_returns_the_nearest_active_raw_reflection(engine: Engine) -> None:
    now = datetime(2026, 9, 8, 1, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        AcademicRepository.create_learning_focus(
            session,
            topic="recursion",
            now=now,
            source_external_event_id="semantic-recursion",
            memory=LearningFocusMemoryInput(
                raw_text="Recursive stack-frame tracing is difficult.",
                embedding=[0.1, 0.2, 0.3],
                embedding_model="test-embedding-v1",
            ),
        )
        AcademicRepository.create_learning_focus(
            session,
            topic="integration",
            now=now,
            source_external_event_id="semantic-integration",
            memory=LearningFocusMemoryInput(
                raw_text="Integration by parts is difficult.",
                embedding=[0.9, 0.0, 0.0],
                embedding_model="test-embedding-v1",
            ),
        )
    store = SQLAlchemyAcademicPlannerStore(engine, embedding_gateway=Embeddings())

    candidates = await store.search_semantic_focuses("recursive calls", limit=1)

    assert len(candidates) == 1
    assert candidates[0].focus is not None
    assert candidates[0].focus.topic == "recursion"
    assert candidates[0].text == "Recursive stack-frame tracing is difficult."


def test_active_focus_enters_first_planner_snapshot_as_separate_practice(engine: Engine) -> None:
    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        focus = AcademicRepository.create_learning_focus(
            session,
            topic="recursion",
            course_code="ECE 250",
            now=now - timedelta(hours=10),
            next_review_at=datetime(2026, 9, 9, 1, tzinfo=UTC),
            practice_due_on=date(2026, 9, 8),
            practice_minutes=30,
        )
        session.add(
            PlanningPreference(
                scope="academic",
                timezone="America/Toronto",
                availability={
                    "windows": [
                        {
                            "start_at": "2026-09-08T13:00:00+00:00",
                            "end_at": "2026-09-08T15:00:00+00:00",
                        }
                    ]
                },
                daily_capacity_minutes=120,
                buffer_minutes=15,
                version="test",
            )
        )
        focus_id = focus.id
    store = SQLAlchemyAcademicPlannerStore(engine)

    facts = store.load_planner_facts(now=now, horizon_days=7)
    plan = build_daily_plan(facts, now=now)
    store.save_daily_plan(plan)
    reloaded = store.get_latest_daily_plan()

    assert len(plan.blocks) == 1
    assert plan.blocks[0].block_kind == "practice"
    assert plan.blocks[0].learning_focus_id == str(focus_id)
    assert reloaded is not None
    assert reloaded.blocks[0].block_kind == "practice"
    assert reloaded.blocks[0].learning_focus_id == str(focus_id)
