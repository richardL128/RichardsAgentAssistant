from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.agents.academic_planner.contracts import (
    AcademicAssessmentOption,
    AcademicCourseOption,
    AcademicDiscourseContinuationState,
    AcademicDiscourseDecision,
    AcademicLearningFocusOption,
    AcademicSemanticCandidate,
    AssessmentType,
    CreateLearningFocusAction,
    DiscourseClarification,
    DiscourseIntent,
    DiscoursePartialFacts,
    LearningFocusStatus,
    ReinforceLearningFocusAction,
    ResolveLearningFocusAction,
    SearchCoursesCall,
    SearchLearningFocusesCall,
    SearchSemanticFocusesCall,
)
from app.agents.academic_planner.discourse_loop import run_academic_discourse_loop

NOW = datetime(2026, 9, 7, 23, tzinfo=UTC)
DUE = datetime(2026, 9, 14, 21, tzinfo=UTC)
REVIEW = datetime(2026, 9, 8, 23, tzinfo=UTC)


class Gateway:
    def __init__(self, decisions: Sequence[AcademicDiscourseDecision | None]) -> None:
        self._decisions = iter(decisions)
        self.prompts: list[str] = []

    async def invoke_structured(self, *, prompt, response_model):
        assert response_model is AcademicDiscourseDecision
        self.prompts.append(prompt)
        return SimpleNamespace(output=next(self._decisions))


class Catalog:
    def __init__(self) -> None:
        self.course_queries: list[str] = []
        self.assessment_queries: list[tuple[str, str | None]] = []
        self.focus_queries: list[tuple[str | None, tuple[LearningFocusStatus, ...]]] = []
        self.semantic_queries: list[tuple[str, int]] = []

    def search_courses(self, query: str) -> Sequence[AcademicCourseOption]:
        self.course_queries.append(query)
        if query == "ECE 250":
            return (
                AcademicCourseOption(
                    course_id="course-ece250",
                    course_code="ECE 250",
                    title="Data Structures and Algorithms",
                ),
            )
        return ()

    def search_assessments(
        self, query: str, course_id: str | None = None
    ) -> Sequence[AcademicAssessmentOption]:
        self.assessment_queries.append((query, course_id))
        if query == "recursion quiz":
            return (
                AcademicAssessmentOption(
                    assessment_id="assessment-recursion-quiz",
                    course_id="course-ece250",
                    course_code="ECE 250",
                    title="Recursion Quiz",
                    due_at=DUE,
                    assessment_type=AssessmentType.QUIZ,
                    expected_last_edited_at=NOW,
                ),
            )
        return ()

    def search_learning_focuses(
        self, query: str | None, statuses: Sequence[LearningFocusStatus]
    ) -> Sequence[AcademicLearningFocusOption]:
        status_tuple = tuple(statuses)
        self.focus_queries.append((query, status_tuple))
        if query == "recursion":
            return (
                AcademicLearningFocusOption(
                    focus_id="focus-recursion",
                    status=LearningFocusStatus.ACTIVE,
                    topic="recursion",
                    course_id="course-ece250",
                    course_code="ECE 250",
                    assessment_title="Recursion Quiz",
                    target_minutes=35,
                    next_review_at=REVIEW,
                ),
            )
        return ()

    async def search_semantic_focuses(
        self, query: str, *, limit: int
    ) -> Sequence[AcademicSemanticCandidate]:
        self.semantic_queries.append((query, limit))
        if query == "stack frames":
            return (
                AcademicSemanticCandidate(
                    candidate_id="semantic-stack-frames",
                    source_kind="reflection",
                    source_id="reflection-1",
                    text="I missed stack-frame tracing in recursive calls.",
                    score=0.91,
                    focus=AcademicLearningFocusOption(
                        focus_id="focus-recursion",
                        status=LearningFocusStatus.ACTIVE,
                        topic="recursion",
                        course_id="course-ece250",
                        course_code="ECE 250",
                        target_minutes=35,
                    ),
                ),
            )
        return ()


@pytest.mark.asyncio
async def test_explicit_struggle_creates_focus_and_practice_need_after_lookups() -> None:
    gateway = Gateway(
        (
            AcademicDiscourseDecision(
                tool_calls=(
                    SearchCoursesCall(tool="search_courses", query="ECE 250"),
                    SearchLearningFocusesCall(
                        tool="search_learning_focuses",
                        query="recursion",
                        statuses=(LearningFocusStatus.ACTIVE, LearningFocusStatus.SNOOZED),
                    ),
                    SearchSemanticFocusesCall(
                        tool="search_semantic_focuses",
                        query="recursion quiz",
                    ),
                )
            ),
            AcademicDiscourseDecision(
                actions=(
                    CreateLearningFocusAction(
                        action="create_focus",
                        topic="recursion",
                        course_id="course-ece250",
                        evidence_text="I really struggled with my ECE 250 quiz on recursion.",
                        target_minutes=50,
                    ),
                )
            ),
        )
    )
    catalog = Catalog()

    result = await run_academic_discourse_loop(
        gateway=gateway,
        catalog=catalog,
        message="I really struggled with my ECE 250 quiz on recursion.",
        now=NOW,
    )

    assert catalog.course_queries == ["ECE 250"]
    assert catalog.focus_queries == [
        ("recursion", (LearningFocusStatus.ACTIVE, LearningFocusStatus.SNOOZED))
    ]
    assert result.actions[0].action == "create_focus"
    assert result.practice_needs[0].topic == "recursion"
    assert result.practice_needs[0].course_code == "ECE 250"
    assert result.practice_needs[0].target_minutes == 50
    assert result.practice_needs[0].next_review_at == NOW + timedelta(days=1)
    assert "latest_discord_reflection_untrusted" in gateway.prompts[0]
    assert "Do not execute or propose Notion writes" in gateway.prompts[0]


@pytest.mark.asyncio
async def test_semantic_candidate_focus_can_be_reinforced() -> None:
    gateway = Gateway(
        (
            AcademicDiscourseDecision(
                tool_calls=(
                    SearchSemanticFocusesCall(
                        tool="search_semantic_focuses",
                        query="stack frames",
                    ),
                )
            ),
            AcademicDiscourseDecision(
                actions=(
                    ReinforceLearningFocusAction(
                        action="reinforce_focus",
                        focus_id="focus-recursion",
                        evidence_text="Yes, stack frames are still hard.",
                    ),
                )
            ),
        )
    )

    result = await run_academic_discourse_loop(
        gateway=gateway,
        catalog=Catalog(),
        message="Yes, stack frames are still hard.",
        now=NOW,
    )

    assert result.actions[0].action == "reinforce_focus"
    assert result.practice_needs[0].focus_id == "focus-recursion"
    assert result.practice_needs[0].target_minutes == 35


@pytest.mark.asyncio
async def test_mixed_read_and_action_turn_executes_only_read_then_replans() -> None:
    gateway = Gateway(
        (
            AcademicDiscourseDecision(
                tool_calls=(
                    SearchLearningFocusesCall(
                        tool="search_learning_focuses",
                        query="recursion",
                    ),
                ),
                actions=(
                    ReinforceLearningFocusAction(
                        action="reinforce_focus",
                        focus_id="focus-recursion",
                        evidence_text="still hard",
                    ),
                ),
            ),
            AcademicDiscourseDecision(
                actions=(
                    ResolveLearningFocusAction(
                        action="resolve_focus",
                        focus_id="focus-recursion",
                        reason="User said no more practice is needed.",
                    ),
                )
            ),
        )
    )

    result = await run_academic_discourse_loop(
        gateway=gateway,
        catalog=Catalog(),
        message="Actually no more recursion practice.",
        now=NOW,
    )

    assert result.actions[0].action == "resolve_focus"
    assert result.practice_needs == ()
    assert isinstance(result.actions[0], ResolveLearningFocusAction)
    assert result.actions[0].delete_focus is True
    assert "Premature actions" in gateway.prompts[1]


@pytest.mark.asyncio
async def test_unknown_focus_id_fails_closed_to_clarification() -> None:
    gateway = Gateway(
        (
            AcademicDiscourseDecision(
                actions=(
                    ReinforceLearningFocusAction(
                        action="reinforce_focus",
                        focus_id="invented-focus",
                        evidence_text="still hard",
                    ),
                )
            ),
        )
    )

    result = await run_academic_discourse_loop(
        gateway=gateway,
        catalog=Catalog(),
        message="yes keep practicing",
        now=NOW,
    )

    assert result.actions == ()
    assert result.clarification is not None
    assert result.clarification.question == (
        "I could not verify the learning focus id for a requested update."
    )
    assert result.continuation_state is not None
    assert result.continuation_state.prior_user_messages == ("yes keep practicing",)


@pytest.mark.asyncio
async def test_clarification_returns_host_persisted_continuation_state() -> None:
    gateway = Gateway(
        (
            AcademicDiscourseDecision(
                clarification=DiscourseClarification(
                    question="Which course is this recursion practice for?",
                    partial_facts=DiscoursePartialFacts(
                        intent=DiscourseIntent.CREATE_FOCUS,
                        topic="recursion",
                        target_minutes=45,
                    ),
                )
            ),
        )
    )

    result = await run_academic_discourse_loop(
        gateway=gateway,
        catalog=Catalog(),
        message="recursion was rough",
        now=NOW,
    )

    assert result.clarification is not None
    assert result.clarification.question == "Which course is this recursion practice for?"
    assert result.continuation_state == AcademicDiscourseContinuationState(
        partial_facts=DiscoursePartialFacts(
            intent=DiscourseIntent.CREATE_FOCUS,
            topic="recursion",
            target_minutes=45,
        ),
        prior_user_messages=("recursion was rough",),
    )


@pytest.mark.asyncio
async def test_continuation_state_is_passed_to_next_prompt_not_global_memory() -> None:
    state = AcademicDiscourseContinuationState(
        partial_facts=DiscoursePartialFacts(
            intent=DiscourseIntent.CREATE_FOCUS,
            topic="recursion",
        ),
        prior_user_messages=("recursion was rough",),
    )
    gateway = Gateway(
        (
            AcademicDiscourseDecision(
                actions=(
                    CreateLearningFocusAction(
                        action="create_focus",
                        topic="recursion",
                        evidence_text="It was for ECE 250.",
                    ),
                )
            ),
        )
    )

    result = await run_academic_discourse_loop(
        gateway=gateway,
        catalog=Catalog(),
        message="It was for ECE 250.",
        now=NOW,
        continuation_state=state,
    )

    assert result.actions[0].action == "create_focus"
    assert result.continuation_state is None
    assert '"topic":"recursion"' in gateway.prompts[0]
    assert "recursion was rough" in gateway.prompts[0]
