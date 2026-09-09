"""Coverage for the Discord academic see-memory review workflow."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session

from app.agents.academic_planner.contracts import (
    AcademicMemoryReviewDecision,
    AcademicMemorySummary,
    AcademicRequestRouteDecision,
    MemoryManagementOutcome,
)
from app.agents.academic_planner.discord_checkin import AcademicDiscordCheckinHandler
from app.agents.academic_planner.memory_workflow import AcademicMemoryService
from app.connectors.discord_gateway import DiscordAcademicMessageCreate
from app.db.academic import (
    AcademicRepository,
    LearningFocusMemoryInput,
    SQLAlchemyAcademicPlannerStore,
)
from app.db.models import (
    AcademicDiscourseSession,
    AcademicDiscourseTurn,
    AcademicLearningFocus,
    AcademicLearningFocusEvent,
    AcademicReflectionMemory,
    Base,
    StudyBlock,
)
from app.llm.embeddings import EmbeddingErrorCode, EmbeddingStatus

CHANNEL = "987654321012345678"
OWNER = "123456789012345678"
OTHER_OWNER = "222222222222222222"
NOW = datetime(2026, 9, 8, 14, tzinfo=UTC)


class Gateway:
    def __init__(
        self,
        *,
        summaries: Sequence[AcademicMemorySummary | None] = (),
        decisions: Sequence[AcademicMemoryReviewDecision | None] = (),
    ) -> None:
        self._summaries = iter(summaries)
        self._decisions = iter(decisions)
        self.prompts: list[str] = []
        self.response_models: list[type[Any]] = []

    async def invoke_structured(self, *, prompt: str, response_model: type[Any]) -> object:
        self.prompts.append(prompt)
        self.response_models.append(response_model)
        if response_model is AcademicMemorySummary:
            return SimpleNamespace(output=next(self._summaries))
        if response_model is AcademicMemoryReviewDecision:
            return SimpleNamespace(output=next(self._decisions))
        raise AssertionError(f"unexpected response model {response_model}")


class Embeddings:
    def __init__(
        self,
        *,
        status: EmbeddingStatus = EmbeddingStatus.VALID,
        vector: list[float] | None = None,
    ) -> None:
        self.status = status
        self.vector = vector or [0.9, 0.1, 0.2]
        self.calls: list[str] = []

    async def embed_reflection_text(self, text: str) -> object:
        self.calls.append(text)
        if self.status is EmbeddingStatus.VALID:
            return SimpleNamespace(
                status=EmbeddingStatus.VALID,
                embedding=SimpleNamespace(vector=self.vector),
                model_identity="test-embedding-v1",
                config_version="test-config-v1",
                error_code=None,
            )
        return SimpleNamespace(
            status=EmbeddingStatus.FAILED,
            embedding=None,
            model_identity="test-embedding-v1",
            config_version="test-config-v1",
            error_code=EmbeddingErrorCode.MODEL_ERROR,
        )


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    created = create_engine(f"sqlite+pysqlite:///{tmp_path / 'memory-review.db'}")
    Base.metadata.create_all(created)
    try:
        yield created
    finally:
        created.dispose()


def _service(
    engine: Engine,
    gateway: Gateway,
    embeddings: Embeddings | None = None,
    *,
    session_ttl_hours: int = 24,
) -> AcademicMemoryService:
    store = SQLAlchemyAcademicPlannerStore(
        engine,
        embedding_gateway=embeddings or Embeddings(),
        default_practice_minutes=30,
    )
    return AcademicMemoryService(
        store=store,
        model_gateway=gateway,
        embedding_gateway=embeddings or Embeddings(),
        timezone="America/Toronto",
        default_practice_minutes=30,
        session_ttl_hours=session_ttl_hours,
    )


def _create_focus(
    session: Session,
    *,
    topic: str,
    event_id: str,
    owner_user_id: str = OWNER,
    owner_channel_id: str = CHANNEL,
    course_code: str | None = "ECE 250",
    status: str = "active",
    practice_minutes: int = 30,
    next_review_at: datetime | None = None,
    raw_text: str | None = None,
    redacted_summary: str | None = None,
    embedding: list[float] | None = None,
) -> AcademicLearningFocus:
    focus = AcademicRepository.create_learning_focus(
        session,
        topic=topic,
        course_code=course_code,
        now=NOW,
        source_external_event_id=event_id,
        next_review_at=next_review_at or NOW + timedelta(days=1),
        practice_due_on=date(2026, 9, 9),
        practice_minutes=practice_minutes,
        owner_user_id=owner_user_id,
        owner_channel_id=owner_channel_id,
        memory=LearningFocusMemoryInput(
            raw_text=raw_text or f"I am struggling with {topic}.",
            embedding=embedding or [0.1, 0.2, 0.3],
            embedding_model="test-embedding-v1",
            redacted_summary=redacted_summary or f"Current focus is {topic}.",
        ),
    )
    if status != "active":
        focus.status = status
        focus.missed_review_count = 2
        focus.reminder_count = 2
    return focus


def test_finance_and_code_review_paths_do_not_access_academic_memory() -> None:
    root = Path(__file__).parents[2]
    targets = [
        *sorted((root / "app/agents/finance").glob("*.py")),
        *sorted((root / "app/agents/code_review").glob("*.py")),
        root / "app/db/finance.py",
        root / "app/db/code_review.py",
    ]
    forbidden = (
        "app.db.academic",
        "AcademicLearningFocus",
        "AcademicReflectionMemory",
        "AcademicDiscourseSession",
    )

    for target in targets:
        source = target.read_text(encoding="utf-8")
        assert not any(name in source for name in forbidden), target


async def _start_review(
    service: AcademicMemoryService,
    *,
    event_id: str = "discord-see-memory",
    raw_text: str = "see memory",
    received_at: datetime = NOW,
):
    return await service.handle_memory_review(
        external_event_id=event_id,
        channel_id=CHANNEL,
        user_id=OWNER,
        raw_text=raw_text,
        received_at=received_at,
    )


@pytest.mark.asyncio
async def test_unmentioned_see_memory_routes_before_reflection_and_notion() -> None:
    class SemanticRouter:
        async def invoke_structured(self, **_kwargs: Any) -> object:
            return SimpleNamespace(
                output=AcademicRequestRouteDecision(
                    memory_request="see memory",
                )
            )

    class Memory:
        review_calls = 0

        async def handle_memory_review(self, **_kwargs: Any) -> object:
            self.review_calls += 1
            return SimpleNamespace(status="summarized", response="Grounded academic summary.")

        async def handle_reflection(self, **_kwargs: Any) -> object:
            raise AssertionError("see memory must not reach reflection handling")

    class Delivery:
        def __init__(self) -> None:
            self.responses: list[str] = []

        async def send_response(self, content: str, *, idempotency_key: str) -> object:
            del idempotency_key
            self.responses.append(content)
            return object()

        async def send_confirmation(self, *_args: Any, **_kwargs: Any) -> object:
            raise AssertionError("see memory must not create a Notion proposal")

    class Store:
        confirmation_ttl_hours = 24

        def get_latest_daily_plan(self) -> None:
            raise AssertionError("see memory must not load the Notion proposal flow")

        def save_discord_checkin(self, *_args: Any, **_kwargs: Any) -> object:
            raise AssertionError("see memory must not persist a Notion proposal")

    class Runtime:
        async def ensure_ready(self) -> object:
            return object()

    memory = Memory()
    delivery = Delivery()
    handler = AcademicDiscordCheckinHandler(
        store=Store(),  # type: ignore[arg-type]
        delivery=delivery,  # type: ignore[arg-type]
        allowed_channel_ids={CHANNEL},
        authorized_user_ids={OWNER},
        writer_provider=lambda: None,
        ollama_runtime=Runtime(),  # type: ignore[arg-type]
        agent_gateway=object(),  # type: ignore[arg-type]
        semantic_router_gateway=SemanticRouter(),
        agent_catalog=object(),  # type: ignore[arg-type]
        assistant_user_id="777777777777777777",
        memory_service=memory,  # type: ignore[arg-type]
    )

    result = await handler(
        DiscordAcademicMessageCreate(
            message_id="333334444455555666",
            channel_id=CHANNEL,
            author_id=OWNER,
            timestamp=NOW,
            content=SecretStr("<@777777777777777777> see memory"),
            mentioned_user_ids=("777777777777777777",),
        )
    )

    assert result.status == "handled"
    assert memory.review_calls == 1
    # This fake delivery does not expose the editable progress-reporter API;
    # the durable memory response still remains authoritative.
    assert delivery.responses == ["Grounded academic summary."]


@pytest.mark.asyncio
async def test_see_memory_summary_is_qwen_grounded_and_creates_durable_session(
    engine: Engine,
) -> None:
    with Session(engine) as session, session.begin():
        active = _create_focus(
            session,
            topic="circuit design",
            event_id="focus-circuit",
            redacted_summary="Solving linear circuits is still shaky.",
        )
        snoozed = _create_focus(
            session,
            topic="recursion",
            event_id="focus-recursion",
            course_code="ECE 250",
            status="snoozed",
            practice_minutes=45,
            redacted_summary="Recursive stack tracing needs review.",
        )
        deleted = _create_focus(session, topic="deleted topic", event_id="focus-deleted")
        AcademicRepository.hard_delete_learning_focus(session, focus_id=deleted.id)
        _create_focus(
            session,
            topic="another user's circuits",
            event_id="focus-other-owner",
            owner_user_id=OTHER_OWNER,
        )
        active_id = str(active.id)
        snoozed_id = str(snoozed.id)
    gateway = Gateway(
        summaries=(
            AcademicMemorySummary(
                summary_text=(
                    "You are currently working on circuit design and have recursion snoozed."
                ),
                covered_focus_ids=(active_id, snoozed_id),
                memory_set_truncated=False,
            ),
        )
    )
    service = _service(engine, gateway)

    result = await _start_review(service)

    assert result.status == "summarized"
    assert (
        result.response == "You are currently working on circuit design and have recursion snoozed."
    )
    assert active_id not in result.response
    assert snoozed_id not in result.response
    assert "deleted topic" not in gateway.prompts[0]
    assert "another user's circuits" not in gateway.prompts[0]
    assert '"embedding":' not in gateway.prompts[0]
    assert '"raw_text":' not in gateway.prompts[0]
    assert '"status":"active"' in gateway.prompts[0]
    assert '"status":"snoozed"' in gateway.prompts[0]
    with Session(engine) as session:
        review = session.scalar(select(AcademicDiscourseSession))
        turns = list(session.scalars(select(AcademicDiscourseTurn)))
        assert review is not None
        assert review.session_kind == "memory_review"
        assert review.state == "open"
        assert review.discord_user_id == OWNER
        assert review.discord_channel_id == CHANNEL
        assert set(review.partial_state["candidate_revisions"]) == set()
        assert [item["focus_id"] for item in review.partial_state["focuses"]] == [
            active_id,
            snoozed_id,
        ]
        assert [turn.external_event_id for turn in turns] == ["discord-see-memory"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phrase",
    [
        "show me your memory",
        "what do you remember about my coursework?",
        "show my learning focuses",
        "please tell me what you remember about my coursework",
    ],
)
async def test_natural_memory_view_variants_route_to_summary(
    engine: Engine,
    phrase: str,
) -> None:
    with Session(engine) as session, session.begin():
        focus = _create_focus(session, topic="linear circuits", event_id=f"focus-{phrase[:5]}")
        focus_id = str(focus.id)
    gateway = Gateway(
        summaries=(
            AcademicMemorySummary(
                summary_text="You are practicing linear circuits.",
                covered_focus_ids=(focus_id,),
                memory_set_truncated=False,
            ),
        )
    )

    result = await _start_review(
        _service(engine, gateway),
        event_id=f"event-{phrase[:12]}",
        raw_text=phrase,
    )

    assert result.status == "summarized"
    assert result.response == "You are practicing linear circuits."


@pytest.mark.asyncio
async def test_empty_memory_returns_clear_response_and_closes_session(engine: Engine) -> None:
    gateway = Gateway()

    result = await _start_review(_service(engine, gateway))

    assert result.status == "summarized"
    assert result.response == (
        "I do not currently have any active or snoozed academic learning focuses stored for you."
    )
    assert gateway.prompts == []
    with Session(engine) as session:
        review = session.scalar(select(AcademicDiscourseSession))
        assert review is not None
        assert review.state == "completed"


@pytest.mark.asyncio
async def test_hallucinated_summary_id_uses_fallback_without_discord_uuid(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        focus = _create_focus(session, topic="nodal analysis", event_id="focus-nodal")
        focus_id = focus.id
    gateway = Gateway(
        summaries=(
            AcademicMemorySummary(
                summary_text="Nodal analysis is hard.",
                covered_focus_ids=("invented-focus-id",),
                memory_set_truncated=False,
            ),
        )
    )

    result = await _start_review(_service(engine, gateway))

    assert result.status == "summarized"
    assert "nodal analysis is active with 30-minute practice blocks" in str(result.response)
    assert str(focus_id) not in str(result.response)


@pytest.mark.asyncio
async def test_subjectless_deletion_clarifies_then_yes_deletes_and_no_does_not(
    engine: Engine,
) -> None:
    with Session(engine) as session, session.begin():
        focus = _create_focus(session, topic="circuit design", event_id="focus-delete-clarify")
        focus_id = focus.id
    gateway = Gateway(
        summaries=(
            AcademicMemorySummary(
                summary_text="You are working on circuit design.",
                covered_focus_ids=(str(focus_id),),
                memory_set_truncated=False,
            ),
        )
    )
    service = _service(engine, gateway)

    assert (await _start_review(service)).status == "summarized"
    ambiguous = await _start_review(
        service,
        event_id="event-ambiguous-delete",
        raw_text="I am no longer struggling",
    )
    assert ambiguous.status == "clarification"
    assert ambiguous.response == "Do you mean circuit design?"
    with Session(engine) as session:
        assert session.get(AcademicLearningFocus, focus_id) is not None
        review = session.scalar(select(AcademicDiscourseSession))
        assert review is not None
        assert review.partial_state["pending_action"]["operation"] == "delete_focus"

    no = await _start_review(service, event_id="event-no-delete", raw_text="no")
    assert no.status == "no_change"
    with Session(engine) as session:
        assert session.get(AcademicLearningFocus, focus_id) is not None
        review = session.scalar(select(AcademicDiscourseSession))
        assert review is not None
        assert review.state == "completed"

    gateway = Gateway(
        summaries=(
            AcademicMemorySummary(
                summary_text="You are working on circuit design.",
                covered_focus_ids=(str(focus_id),),
                memory_set_truncated=False,
            ),
        )
    )
    service = _service(engine, gateway)
    await _start_review(service, event_id="event-see-memory-again")
    await _start_review(
        service,
        event_id="event-ambiguous-delete-again",
        raw_text="I am no longer struggling",
    )
    yes = await _start_review(service, event_id="event-yes-delete", raw_text="yes")

    assert yes.status == "applied"
    assert "Deleted circuit design" in str(yes.response)
    assert str(focus_id) not in str(yes.response)
    with Session(engine) as session:
        assert session.get(AcademicLearningFocus, focus_id) is None


@pytest.mark.asyncio
async def test_cancel_closes_review_without_mutation_and_duplicate_is_harmless(
    engine: Engine,
) -> None:
    with Session(engine) as session, session.begin():
        focus = _create_focus(session, topic="circuit design", event_id="focus-cancel")
        focus_id = focus.id
    gateway = Gateway(
        summaries=(
            AcademicMemorySummary(
                summary_text="You are working on circuit design.",
                covered_focus_ids=(str(focus_id),),
                memory_set_truncated=False,
            ),
        )
    )
    service = _service(engine, gateway)

    await _start_review(service)
    cancelled = await _start_review(service, event_id="event-cancel", raw_text="never mind")
    duplicate = await _start_review(service, event_id="event-cancel", raw_text="never mind")

    assert cancelled.status == "cancelled"
    assert cancelled.response == "No problem—nothing was changed."
    assert duplicate.status == "duplicate"
    with Session(engine) as session:
        assert session.get(AcademicLearningFocus, focus_id) is not None
        review = session.scalar(select(AcademicDiscourseSession))
        assert review is not None
        assert review.state == "completed"
        assert review.partial_state["pending_action"] is None


@pytest.mark.asyncio
async def test_explicit_deletion_cleans_memory_events_and_preserves_practice_block(
    engine: Engine,
) -> None:
    with Session(engine) as session, session.begin():
        focus = _create_focus(session, topic="circuit design", event_id="focus-explicit-delete")
        focus_id = focus.id
        plan = AcademicRepository.upsert_study_plan(
            session,
            plan_key="plan-delete",
            starts_on=date(2026, 9, 9),
            ends_on=date(2026, 9, 9),
            timezone="America/Toronto",
            status="published",
        )
        block = AcademicRepository.upsert_study_block(
            session,
            plan_id=plan.id,
            block_key="practice-delete",
            title="Practice circuit design",
            starts_at=NOW + timedelta(hours=1),
            ends_at=NOW + timedelta(hours=1, minutes=30),
            allocated_minutes=30,
            learning_focus_id=focus_id,
            block_kind="practice",
        )
        block_id = block.id
    gateway = Gateway(
        summaries=(
            AcademicMemorySummary(
                summary_text="You are working on circuit design.",
                covered_focus_ids=(str(focus_id),),
                memory_set_truncated=False,
            ),
        ),
        decisions=(
            AcademicMemoryReviewDecision(
                outcome=MemoryManagementOutcome.DELETE_FOCUS,
                focus_id=str(focus_id),
                subject_text="circuit design",
                reason="The user said they are no longer struggling.",
            ),
        ),
    )
    service = _service(engine, gateway)

    await _start_review(service)
    deleted = await _start_review(
        service,
        event_id="event-explicit-delete",
        raw_text="I am no longer struggling with circuit design.",
    )
    duplicate = await _start_review(
        service,
        event_id="event-explicit-delete",
        raw_text="I am no longer struggling with circuit design.",
    )

    assert deleted.status == "applied"
    assert duplicate.status == "duplicate"
    assert "Deleted circuit design" in str(deleted.response)
    with Session(engine) as session:
        assert session.get(AcademicLearningFocus, focus_id) is None
        assert session.scalar(select(func.count()).select_from(AcademicReflectionMemory)) == 0
        assert session.scalar(select(func.count()).select_from(AcademicLearningFocusEvent)) == 0
        block = session.get(StudyBlock, block_id)
        assert block is not None
        assert block.learning_focus_id is None
        assert block.block_kind == "practice"


@pytest.mark.asyncio
async def test_rewrite_replaces_raw_text_vector_topic_and_resets_review_state(
    engine: Engine,
) -> None:
    with Session(engine) as session, session.begin():
        focus = _create_focus(
            session,
            topic="circuit design",
            event_id="focus-rewrite",
            raw_text="Old raw text about general circuit design.",
            embedding=[0.1, 0.2, 0.3],
        )
        focus.missed_review_count = 3
        focus.reminder_count = 3
        focus.status = "snoozed"
        focus_id = focus.id
        revision = focus.revision
    gateway = Gateway(
        summaries=(
            AcademicMemorySummary(
                summary_text="You are working on circuit design.",
                covered_focus_ids=(str(focus_id),),
                memory_set_truncated=False,
            ),
        ),
        decisions=(
            AcademicMemoryReviewDecision(
                outcome=MemoryManagementOutcome.REPLACE_FOCUS,
                focus_id=str(focus_id),
                subject_text="circuit design",
                replacement_topic="nodal analysis",
                replacement_course_code="ECE 250",
                target_minutes=40,
            ),
        ),
    )
    embeddings = Embeddings(vector=[0.7, 0.8, 0.9])
    service = _service(engine, gateway, embeddings)

    await _start_review(service)
    rewritten = await _start_review(
        service,
        event_id="event-rewrite",
        raw_text=(
            "It isn't general circuit design; I'm struggling specifically with nodal analysis."
        ),
    )

    assert rewritten.status == "applied"
    assert "Updated that learning focus to nodal analysis" in str(rewritten.response)
    with Session(engine) as session:
        focus = session.get(AcademicLearningFocus, focus_id)
        memories = list(session.scalars(select(AcademicReflectionMemory)))
        assert focus is not None
        assert focus.topic == "nodal analysis"
        assert focus.status == "active"
        assert focus.revision == revision + 1
        assert focus.missed_review_count == 0
        assert focus.reminder_count == 0
        assert focus.practice_due_on == date(2026, 9, 9)
        assert focus.practice_minutes == 40
        assert len(memories) == 1
        assert memories[0].raw_text == (
            "It isn't general circuit design; I'm struggling specifically with nodal analysis."
        )
        assert memories[0].embedding == [0.7, 0.8, 0.9]
        assert "Old raw text" not in memories[0].raw_text


@pytest.mark.asyncio
async def test_embedding_failure_during_rewrite_removes_stale_vector(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        focus = _create_focus(
            session,
            topic="circuit design",
            event_id="focus-rewrite-failed-embedding",
            raw_text="Old stale reflection text.",
            embedding=[0.1, 0.2, 0.3],
        )
        focus_id = focus.id
    gateway = Gateway(
        summaries=(
            AcademicMemorySummary(
                summary_text="You are working on circuit design.",
                covered_focus_ids=(str(focus_id),),
                memory_set_truncated=False,
            ),
        ),
        decisions=(
            AcademicMemoryReviewDecision(
                outcome=MemoryManagementOutcome.REPLACE_FOCUS,
                focus_id=str(focus_id),
                subject_text="circuit design",
                replacement_topic="nodal analysis",
            ),
        ),
    )
    service = _service(engine, gateway, Embeddings(status=EmbeddingStatus.FAILED))

    await _start_review(service)
    result = await _start_review(
        service,
        event_id="event-rewrite-failed-embedding",
        raw_text="Actually, the focus is nodal analysis.",
    )

    assert result.status == "applied"
    with Session(engine) as session:
        memory = session.scalar(select(AcademicReflectionMemory))
        assert memory is not None
        assert memory.raw_text == "Actually, the focus is nodal analysis."
        assert memory.embedding is None
        assert memory.embedding_model is None
        assert memory.embedding_metadata["status"] == "failed"
        assert memory.embedding_metadata["error_code"] == "embedding_model_error"


@pytest.mark.asyncio
async def test_expired_session_cannot_be_resumed(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        focus = _create_focus(session, topic="circuit design", event_id="focus-expire")
        focus_id = focus.id
    gateway = Gateway(
        summaries=(
            AcademicMemorySummary(
                summary_text="You are working on circuit design.",
                covered_focus_ids=(str(focus_id),),
                memory_set_truncated=False,
            ),
        )
    )
    service = _service(engine, gateway, session_ttl_hours=1)

    await _start_review(service, received_at=NOW)
    resumed = await _start_review(
        service,
        event_id="event-after-expiry",
        raw_text="I am no longer struggling with circuit design.",
        received_at=NOW + timedelta(hours=2),
    )

    assert resumed.status == "not_applicable"
    with Session(engine) as session:
        assert session.get(AcademicLearningFocus, focus_id) is not None
        review = session.scalar(select(AcademicDiscourseSession))
        assert review is not None
        assert review.state == "expired"


@pytest.mark.asyncio
async def test_stale_revision_fails_closed_without_deleting_focus(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        focus = _create_focus(session, topic="circuit design", event_id="focus-stale")
        focus_id = focus.id
    gateway = Gateway(
        summaries=(
            AcademicMemorySummary(
                summary_text="You are working on circuit design.",
                covered_focus_ids=(str(focus_id),),
                memory_set_truncated=False,
            ),
        ),
        decisions=(
            AcademicMemoryReviewDecision(
                outcome=MemoryManagementOutcome.DELETE_FOCUS,
                focus_id=str(focus_id),
                subject_text="circuit design",
            ),
        ),
    )
    service = _service(engine, gateway)

    await _start_review(service)
    with Session(engine) as session, session.begin():
        focus = session.get(AcademicLearningFocus, focus_id)
        assert focus is not None
        focus.revision += 1
    result = await _start_review(
        service,
        event_id="event-stale-delete",
        raw_text="I am no longer struggling with circuit design.",
    )

    assert result.status == "clarification"
    assert "changed after I showed it" in str(result.response)
    with Session(engine) as session:
        assert session.get(AcademicLearningFocus, focus_id) is not None


@pytest.mark.asyncio
async def test_invented_mutation_id_and_cross_owner_focus_do_not_mutate(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        own_focus = _create_focus(session, topic="circuits", event_id="focus-own")
        other_focus = _create_focus(
            session,
            topic="private recursion",
            event_id="focus-other",
            owner_user_id=OTHER_OWNER,
        )
        own_id = own_focus.id
        other_id = other_focus.id
    gateway = Gateway(
        summaries=(
            AcademicMemorySummary(
                summary_text="You are working on circuits.",
                covered_focus_ids=(str(own_id),),
                memory_set_truncated=False,
            ),
        ),
        decisions=(
            AcademicMemoryReviewDecision(
                outcome=MemoryManagementOutcome.DELETE_FOCUS,
                focus_id=str(other_id),
                subject_text="private recursion",
            ),
        ),
    )
    service = _service(engine, gateway)

    await _start_review(service)
    result = await _start_review(
        service,
        event_id="event-cross-owner-delete",
        raw_text="I am no longer struggling with private recursion.",
    )

    assert result.status == "no_change"
    assert "could not safely match" in str(result.response)
    with Session(engine) as session:
        assert session.get(AcademicLearningFocus, own_id) is not None
        assert session.get(AcademicLearningFocus, other_id) is not None
