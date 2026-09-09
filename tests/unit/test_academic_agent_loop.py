from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.agents.academic_planner.agent_loop import run_academic_agent_loop
from app.agents.academic_planner.contracts import (
    AcademicAgentContinuationInput,
    AcademicAgentDecision,
    AcademicAgentLookupKind,
    AcademicAgentLoopOutcome,
    AcademicAgentProgressEvent,
    AcademicAgentProgressPhase,
    AcademicAgentWireDecision,
    AcademicAssessmentOption,
    AcademicCourseOption,
    ArchiveAssessmentCall,
    AssessmentType,
    CreateAssessmentCall,
    CreateStudySessionCall,
    SearchAssessmentsCall,
    SearchCoursesCall,
    UpdateAssessmentCall,
    UserCreatableAssessmentType,
)

NOW = datetime(2026, 9, 7, 14, tzinfo=UTC)
DUE = datetime(2026, 9, 14, 21, tzinfo=UTC)
EDITED = datetime(2026, 9, 6, 22, tzinfo=UTC)


class Gateway:
    def __init__(self, decisions: Sequence[AcademicAgentDecision | BaseException | None]) -> None:
        self._decisions = iter(decisions)
        self.prompts: list[str] = []

    async def invoke_structured(
        self,
        *,
        prompt: str,
        response_model: type[AcademicAgentWireDecision],
    ) -> object:
        assert response_model is AcademicAgentWireDecision
        self.prompts.append(prompt)
        decision = next(self._decisions)
        if isinstance(decision, BaseException):
            raise decision
        return SimpleNamespace(output=decision)


class Catalog:
    def __init__(self) -> None:
        self.course_queries: list[str] = []
        self.assessment_queries: list[tuple[str, str | None]] = []

    def search_courses(self, query: str) -> Sequence[AcademicCourseOption]:
        self.course_queries.append(query)
        if query == "ECE 202":
            return (
                AcademicCourseOption(
                    course_id="course-ece202",
                    course_code="ECE 202",
                    title="Circuit Analysis",
                ),
            )
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
        if query == "old assignment":
            return (
                AcademicAssessmentOption(
                    assessment_id="assessment-ece250-old",
                    course_id="course-ece250",
                    course_code="ECE 250",
                    title="Old Assignment",
                    due_at=DUE,
                    assessment_type=AssessmentType.ASSIGNMENT,
                    expected_last_edited_at=EDITED,
                ),
            )
        return ()


def test_agent_decision_requires_one_semantic_action_mode() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        AcademicAgentDecision()

    with pytest.raises(ValidationError, match="exactly one"):
        AcademicAgentDecision(
            tool_calls=(SearchAssessmentsCall(tool="search_assessments", query="tomorrow"),),
            question="Which item?",
        )

    with pytest.raises(ValidationError, match="tools mode"):
        AcademicAgentWireDecision(mode="tools")

    with pytest.raises(ValidationError, match="answer mode"):
        AcademicAgentWireDecision(mode="answer")

    assert (
        AcademicAgentWireDecision(
            mode="tools",
            tool_calls=(SearchAssessmentsCall(tool="search_assessments", query="tomorrow"),),
        )
        .to_decision()
        .tool_calls
    )


@pytest.mark.asyncio
async def test_read_only_calendar_query_returns_model_answer_grounded_after_lookup() -> None:
    class QueryCatalog(Catalog):
        def search_assessments(
            self, query: str, course_id: str | None = None
        ) -> Sequence[AcademicAssessmentOption]:
            self.assessment_queries.append((query, course_id))
            return (
                AcademicAssessmentOption(
                    assessment_id="assessment-study",
                    course_id="course-ece250",
                    course_code="ECE 250",
                    title="Studying Block — race conditions",
                    due_at=DUE,
                    assessment_type=AssessmentType.STUDYING_BLOCK,
                    expected_last_edited_at=EDITED,
                ),
            )

    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(
                    SearchAssessmentsCall(tool="search_assessments", query="to dos tomorrow"),
                )
            ),
            AcademicAgentDecision(
                answer=(
                    "Tomorrow you have Studying Block — race conditions at 7:00 PM and "
                    "Studying Block — insertion sort at 7:45 PM."
                )
            ),
        )
    )
    catalog = QueryCatalog()

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=catalog,
        message="what are my to dos tmr",
        now=NOW,
    )

    assert result.outcome is AcademicAgentLoopOutcome.ANSWER_READY
    assert result.changes == ()
    assert result.question is None
    assert result.response is not None
    assert "race conditions" in result.response
    assert "assessment-study" not in result.response
    assert '"prior_read_only_tool_results_untrusted"' in gateway.prompts[1]


@pytest.mark.asyncio
async def test_multiple_instructions_become_one_ordered_change_tuple() -> None:
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(
                    SearchCoursesCall(tool="search_courses", query="ECE 202"),
                    SearchAssessmentsCall(tool="search_assessments", query="old assignment"),
                )
            ),
            AcademicAgentDecision(
                tool_calls=(
                    CreateAssessmentCall(
                        tool="create_assessment",
                        course_id="course-ece202",
                        title="Lab report",
                        due_at=DUE,
                        assessment_type=UserCreatableAssessmentType.LAB,
                    ),
                    ArchiveAssessmentCall(
                        tool="archive_assessment",
                        assessment_id="assessment-ece250-old",
                    ),
                )
            ),
        )
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message="create a lab report in ECE 202 and delete the old ECE 250 assignment",
        now=NOW,
    )

    assert [change.field for change in result.changes] == [
        "create_assessment",
        "archive_assessment",
    ]
    assert result.changes[0].course_id == "course-ece202"
    assert result.changes[0].title == "Lab — Lab report"
    assert result.changes[0].assessment_type is AssessmentType.LAB
    assert result.changes[1].assessment_id == "assessment-ece250-old"
    assert result.changes[1].expected_title == "Old Assignment"
    assert result.question is None


@pytest.mark.asyncio
async def test_lookup_result_feeds_second_model_turn() -> None:
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 202"),)
            ),
            AcademicAgentDecision(
                tool_calls=(
                    CreateAssessmentCall(
                        tool="create_assessment",
                        course_id="course-ece202",
                        title="Problem set 1",
                        due_at=DUE,
                        assessment_type=UserCreatableAssessmentType.ASSIGNMENT,
                    ),
                )
            ),
        )
    )
    catalog = Catalog()

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=catalog,
        message="put assignment problem set 1 in ECE 202",
        now=NOW,
    )

    assert catalog.course_queries == ["ECE 202"]
    assert len(gateway.prompts) == 2
    assert "course-ece202" in gateway.prompts[1]
    assert result.changes[0].course_id == "course-ece202"


@pytest.mark.asyncio
async def test_progress_events_are_ordered_and_redacted_for_multiturn_loop() -> None:
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(
                    SearchCoursesCall(tool="search_courses", query="ECE 202"),
                    SearchAssessmentsCall(tool="search_assessments", query="old assignment"),
                )
            ),
            AcademicAgentDecision(
                tool_calls=(
                    CreateAssessmentCall(
                        tool="create_assessment",
                        course_id="course-ece202",
                        title="Lab report",
                        due_at=DUE,
                        assessment_type=UserCreatableAssessmentType.LAB,
                    ),
                    ArchiveAssessmentCall(
                        tool="archive_assessment",
                        assessment_id="assessment-ece250-old",
                    ),
                )
            ),
        )
    )
    events: list[AcademicAgentProgressEvent] = []

    async def collect_progress(event: AcademicAgentProgressEvent) -> None:
        events.append(event)

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message="create a lab report in ECE 202 and delete the old ECE 250 assignment",
        now=NOW,
        progress_sink=collect_progress,
        attempt_number=2,
        attempt_limit=3,
    )

    assert result.outcome is AcademicAgentLoopOutcome.PROPOSAL_READY
    assert [event.phase for event in events] == [
        AcademicAgentProgressPhase.MODEL_TURN,
        AcademicAgentProgressPhase.COURSE_LOOKUP,
        AcademicAgentProgressPhase.ASSESSMENT_LOOKUP,
        AcademicAgentProgressPhase.COURSE_LOOKUP,
        AcademicAgentProgressPhase.ASSESSMENT_LOOKUP,
        AcademicAgentProgressPhase.MODEL_TURN,
        AcademicAgentProgressPhase.PROPOSAL_VALIDATION,
        AcademicAgentProgressPhase.PROPOSAL_READY,
    ]
    assert [(event.attempt_number, event.attempt_limit) for event in events] == [(2, 3)] * 8
    assert events[0].model_turn == 1
    assert events[5].model_turn == 2
    assert events[3].lookup_kind is AcademicAgentLookupKind.COURSE
    assert events[3].result_count == 1
    assert events[4].lookup_kind is AcademicAgentLookupKind.ASSESSMENT
    assert events[4].result_count == 1
    assert events[-1].terminal is True
    serialized_events = " ".join(event.model_dump_json() for event in events)
    assert "ECE 202" not in serialized_events
    assert "course-ece202" not in serialized_events
    assert "old assignment" not in serialized_events
    assert "assessment-ece250-old" not in serialized_events


@pytest.mark.asyncio
async def test_progress_sink_failure_is_best_effort() -> None:
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 202"),)
            ),
            AcademicAgentDecision(
                tool_calls=(
                    CreateAssessmentCall(
                        tool="create_assessment",
                        course_id="course-ece202",
                        title="Problem set 1",
                        due_at=DUE,
                        assessment_type=UserCreatableAssessmentType.ASSIGNMENT,
                    ),
                )
            ),
        )
    )

    async def failing_sink(event: AcademicAgentProgressEvent) -> None:
        raise RuntimeError("progress transport unavailable")

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message="put assignment problem set 1 in ECE 202",
        now=NOW,
        progress_sink=failing_sink,
    )

    assert result.outcome is AcademicAgentLoopOutcome.PROPOSAL_READY
    assert len(result.changes) == 1


@pytest.mark.asyncio
async def test_continuation_prompt_labels_fields_and_grounding_ignores_bot_questions() -> None:
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 202"),)
            ),
            AcademicAgentDecision(
                tool_calls=(
                    CreateAssessmentCall(
                        tool="create_assessment",
                        course_id="course-ece202",
                        title="Op amps",
                        due_at=DUE,
                        assessment_type=UserCreatableAssessmentType.ASSIGNMENT,
                    ),
                )
            ),
        )
    )
    continuation = AcademicAgentContinuationInput(
        original_user_request="create op amps in ECE 202",
        prior_clarification_questions=("Is this a lab?",),
        clarification_answers=("assignment",),
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message="assignment",
        now=NOW,
        continuation=continuation,
    )

    assert result.outcome is AcademicAgentLoopOutcome.PROPOSAL_READY
    assert result.changes[0].title == "Assignment — Op amps"
    assert "original_user_request_untrusted" in gateway.prompts[0]
    assert "prior_clarification_questions_untrusted" in gateway.prompts[0]
    assert "clarification_answers_untrusted" in gateway.prompts[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "message", "title", "expected_title"),
    [
        (
            UserCreatableAssessmentType.QUIZ,
            "create quiz chapter 4 in ECE 202",
            "Chapter 4",
            "Quiz — Chapter 4",
        ),
        (
            UserCreatableAssessmentType.ASSIGNMENT,
            "create assignment problem set 1 in ECE 202",
            "Problem set 1",
            "Assignment — Problem set 1",
        ),
        (
            UserCreatableAssessmentType.TUTORIAL,
            "create tutorial week 3 in ECE 202",
            "Week 3",
            "Tutorial — Week 3",
        ),
        (
            UserCreatableAssessmentType.LAB,
            "create lab op amps in ECE 202",
            "Op amps",
            "Lab — Op amps",
        ),
        (
            UserCreatableAssessmentType.STUDYING_BLOCK,
            "create studying block Fourier review in ECE 202",
            "Fourier review",
            "Studying Block — Fourier review",
        ),
    ],
)
async def test_supported_create_types_produce_grounded_canonical_titles(
    kind: UserCreatableAssessmentType,
    message: str,
    title: str,
    expected_title: str,
) -> None:
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 202"),)
            ),
            AcademicAgentDecision(
                tool_calls=(
                    CreateAssessmentCall(
                        tool="create_assessment",
                        course_id="course-ece202",
                        title=title,
                        due_at=DUE,
                        assessment_type=kind,
                    ),
                )
            ),
        )
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message=message,
        now=NOW,
    )

    assert result.question is None
    assert len(result.changes) == 1
    assert result.changes[0].title == expected_title
    assert result.changes[0].assessment_type == AssessmentType(kind.value)
    assert result.changes[0].course_id == "course-ece202"
    assert result.changes[0].course_code == "ECE 202"
    assert result.changes[0].due_at == DUE


@pytest.mark.asyncio
async def test_natural_study_request_creates_ordered_sequential_studying_blocks() -> None:
    starts_at = datetime(2026, 9, 8, 23, tzinfo=UTC)
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 250"),)
            ),
            AcademicAgentDecision(
                tool_calls=(
                    CreateStudySessionCall(
                        tool="create_study_session",
                        course_id="course-ece250",
                        topic="race conditions",
                        starts_at=starts_at,
                        duration_minutes=45,
                    ),
                    CreateStudySessionCall(
                        tool="create_study_session",
                        course_id="course-ece250",
                        topic="insertion sort",
                        starts_at=starts_at + timedelta(minutes=45),
                        duration_minutes=45,
                    ),
                )
            ),
        )
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message=(
            "I need to study for ECE 250, specifically race conditions and insertion sort. "
            "Tomorrow at 7 PM, 45 minutes each."
        ),
        now=NOW,
    )

    assert result.outcome is AcademicAgentLoopOutcome.PROPOSAL_READY
    assert result.question is None
    assert [change.title for change in result.changes] == [
        "Studying Block — race conditions",
        "Studying Block — insertion sort",
    ]
    assert [change.course_code for change in result.changes] == ["ECE 250", "ECE 250"]
    assert [change.assessment_type for change in result.changes] == [
        AssessmentType.STUDYING_BLOCK,
        AssessmentType.STUDYING_BLOCK,
    ]
    assert result.changes[0].due_at == starts_at
    assert result.changes[0].ends_at == starts_at + timedelta(minutes=45)
    assert result.changes[1].due_at == starts_at + timedelta(minutes=45)
    assert result.changes[1].ends_at == starts_at + timedelta(minutes=90)
    assert "create_study_session" in gateway.prompts[1]


@pytest.mark.asyncio
async def test_combined_study_request_allows_one_multi_topic_block() -> None:
    starts_at = datetime(2026, 9, 8, 22, tzinfo=UTC)
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 250"),)
            ),
            AcademicAgentDecision(
                tool_calls=(
                    CreateStudySessionCall(
                        tool="create_study_session",
                        course_id="course-ece250",
                        topic="recursion and AVL trees",
                        starts_at=starts_at,
                        duration_minutes=60,
                    ),
                )
            ),
        )
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message=(
            "Create one combined study session for recursion and AVL trees in ECE 250 "
            "tomorrow at 6 PM for one hour."
        ),
        now=NOW,
    )

    assert result.outcome is AcademicAgentLoopOutcome.PROPOSAL_READY
    assert len(result.changes) == 1
    assert result.changes[0].title == "Studying Block — recursion and AVL trees"
    assert result.changes[0].due_at == starts_at
    assert result.changes[0].ends_at == starts_at + timedelta(minutes=60)


@pytest.mark.parametrize("duration", [4, 241])
def test_create_study_session_call_enforces_existing_duration_bounds(duration: int) -> None:
    with pytest.raises(ValidationError):
        CreateStudySessionCall(
            tool="create_study_session",
            course_id="course-ece250",
            topic="recursion",
            starts_at=DUE,
            duration_minutes=duration,
        )


@pytest.mark.asyncio
async def test_study_session_accepts_model_semantic_decision_without_keyword_intent() -> None:
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 250"),)
            ),
            AcademicAgentDecision(
                tool_calls=(
                    CreateStudySessionCall(
                        tool="create_study_session",
                        course_id="course-ece250",
                        topic="recursion",
                        starts_at=DUE,
                        duration_minutes=30,
                    ),
                )
            ),
        )
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message="I am still confused by recursion in ECE 250.",
        now=NOW,
    )

    assert result.outcome is AcademicAgentLoopOutcome.PROPOSAL_READY
    assert result.question is None
    assert len(result.changes) == 1
    assert result.changes[0].title == "Studying Block — recursion"


@pytest.mark.asyncio
async def test_study_session_requires_prior_course_lookup() -> None:
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(
                    CreateStudySessionCall(
                        tool="create_study_session",
                        course_id="course-ece250",
                        topic="recursion",
                        starts_at=DUE,
                        duration_minutes=30,
                    ),
                )
            ),
        )
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message="Schedule 30 minutes to review recursion for ECE 250 tomorrow.",
        now=NOW,
    )

    assert result.changes == ()
    assert result.question == "I could not verify the course for a requested study session."
    assert result.outcome is AcademicAgentLoopOutcome.HOST_VALIDATION_FAILED


@pytest.mark.asyncio
async def test_study_session_requires_future_start() -> None:
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 250"),)
            ),
            AcademicAgentDecision(
                tool_calls=(
                    CreateStudySessionCall(
                        tool="create_study_session",
                        course_id="course-ece250",
                        topic="recursion",
                        starts_at=NOW,
                        duration_minutes=30,
                    ),
                )
            ),
        )
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message="Schedule 30 minutes to review recursion for ECE 250 right now.",
        now=NOW,
    )

    assert result.changes == ()
    assert result.question == "Study sessions must start in the future."
    assert result.outcome is AcademicAgentLoopOutcome.HOST_VALIDATION_FAILED


@pytest.mark.asyncio
async def test_study_session_accepts_model_resolved_bare_hour() -> None:
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 250"),)
            ),
            AcademicAgentDecision(
                tool_calls=(
                    CreateStudySessionCall(
                        tool="create_study_session",
                        course_id="course-ece250",
                        topic="recursion",
                        starts_at=DUE,
                        duration_minutes=30,
                    ),
                )
            ),
        )
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message="Schedule 30 minutes to review recursion for ECE 250 tomorrow at 7.",
        now=NOW,
    )

    assert result.outcome is AcademicAgentLoopOutcome.PROPOSAL_READY
    assert result.question is None
    assert len(result.changes) == 1
    assert result.changes[0].due_at == DUE


@pytest.mark.asyncio
async def test_study_session_accepts_model_resolved_missing_start_time() -> None:
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 250"),)
            ),
            AcademicAgentDecision(
                tool_calls=(
                    CreateStudySessionCall(
                        tool="create_study_session",
                        course_id="course-ece250",
                        topic="recursion",
                        starts_at=DUE,
                        duration_minutes=30,
                    ),
                )
            ),
        )
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message="Schedule 30 minutes to review recursion for ECE 250 tomorrow.",
        now=NOW,
    )

    assert result.outcome is AcademicAgentLoopOutcome.PROPOSAL_READY
    assert result.question is None
    assert len(result.changes) == 1
    assert result.changes[0].due_at == DUE


@pytest.mark.asyncio
async def test_study_session_accepts_model_resolved_missing_date() -> None:
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 250"),)
            ),
            AcademicAgentDecision(
                tool_calls=(
                    CreateStudySessionCall(
                        tool="create_study_session",
                        course_id="course-ece250",
                        topic="recursion",
                        starts_at=datetime(2026, 9, 8, 23, tzinfo=UTC),
                        duration_minutes=30,
                    ),
                )
            ),
        )
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message="Schedule 30 minutes to review recursion for ECE 250 at 7 PM.",
        now=NOW,
    )

    assert result.outcome is AcademicAgentLoopOutcome.PROPOSAL_READY
    assert result.question is None
    assert len(result.changes) == 1
    assert result.changes[0].due_at == datetime(2026, 9, 8, 23, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "Schedule 30 minutes to review recursion for ECE 250 tomorrow after 6 PM.",
        "Schedule 30 minutes to review recursion for ECE 250 tomorrow before 8 PM.",
        "Schedule 30 minutes to review recursion for ECE 250 tomorrow between 6 and 8 PM.",
        "Schedule 30 minutes to review recursion for ECE 250 tomorrow any time after 6 PM.",
        "Schedule 30 minutes to review recursion for ECE 250 tomorrow around 6 PM.",
        "Schedule 30 minutes to review recursion for ECE 250 tomorrow approximately 6 PM.",
    ],
)
async def test_study_session_accepts_model_resolved_non_exact_clock_ranges(
    message: str,
) -> None:
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 250"),)
            ),
            AcademicAgentDecision(
                tool_calls=(
                    CreateStudySessionCall(
                        tool="create_study_session",
                        course_id="course-ece250",
                        topic="recursion",
                        starts_at=datetime(2026, 9, 8, 22, tzinfo=UTC),
                        duration_minutes=30,
                    ),
                )
            ),
        )
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message=message,
        now=NOW,
    )

    assert result.outcome is AcademicAgentLoopOutcome.PROPOSAL_READY
    assert result.question is None
    assert len(result.changes) == 1
    assert result.changes[0].due_at == datetime(2026, 9, 8, 22, tzinfo=UTC)


@pytest.mark.asyncio
async def test_study_session_accepts_explicit_weekday_date() -> None:
    starts_at = datetime(2026, 9, 8, 23, tzinfo=UTC)
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 250"),)
            ),
            AcademicAgentDecision(
                tool_calls=(
                    CreateStudySessionCall(
                        tool="create_study_session",
                        course_id="course-ece250",
                        topic="recursion",
                        starts_at=starts_at,
                        duration_minutes=30,
                    ),
                )
            ),
        )
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message="Schedule 30 minutes to review recursion for ECE 250 Tuesday at 7 PM.",
        now=NOW,
    )

    assert result.outcome is AcademicAgentLoopOutcome.PROPOSAL_READY
    assert result.question is None
    assert len(result.changes) == 1
    assert result.changes[0].due_at == starts_at


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "now", "starts_at"),
    [
        (
            "Schedule 30 minutes to review recursion for ECE 250 tomorrow at 7pm.",
            NOW,
            datetime(2026, 9, 8, 23, tzinfo=UTC),
        ),
        (
            "Schedule 30 minutes to review recursion for ECE 250 tomorrow at 7 pm.",
            NOW,
            datetime(2026, 9, 8, 23, tzinfo=UTC),
        ),
        (
            "Schedule 30 minutes to review recursion for ECE 250 tomorrow at 19:00.",
            NOW,
            datetime(2026, 9, 8, 23, tzinfo=UTC),
        ),
        (
            "Schedule 30 minutes to review recursion for ECE 250 tomorrow at noon.",
            NOW,
            datetime(2026, 9, 8, 16, tzinfo=UTC),
        ),
        (
            "Schedule 30 minutes to review recursion for ECE 250 tomorrow at midnight.",
            NOW,
            datetime(2026, 9, 8, 4, tzinfo=UTC),
        ),
        (
            "Schedule 30 minutes to review recursion for ECE 250 tomorrow at 7pm.",
            datetime(2026, 1, 14, 15, tzinfo=UTC),
            datetime(2026, 1, 16, 0, tzinfo=UTC),
        ),
    ],
)
async def test_study_session_accepts_exact_requested_local_clocks(
    message: str,
    now: datetime,
    starts_at: datetime,
) -> None:
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 250"),)
            ),
            AcademicAgentDecision(
                tool_calls=(
                    CreateStudySessionCall(
                        tool="create_study_session",
                        course_id="course-ece250",
                        topic="recursion",
                        starts_at=starts_at,
                        duration_minutes=30,
                    ),
                )
            ),
        )
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message=message,
        now=now,
    )

    assert result.outcome is AcademicAgentLoopOutcome.PROPOSAL_READY
    assert result.question is None
    assert len(result.changes) == 1
    assert result.changes[0].due_at == starts_at


@pytest.mark.asyncio
async def test_verbose_model_study_question_is_passed_through_without_mutations() -> None:
    gateway = Gateway(
        (
            AcademicAgentDecision(
                question=(
                    "I can help schedule that study session, but I need to know what time "
                    "you want to begin, how long it should last, and whether race "
                    "conditions and insertion sort should be one session or separate ones."
                ),
            ),
        )
    )
    catalog = Catalog()

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=catalog,
        message=(
            "I need to study for ECE 250 tomorrow, specifically race conditions and insertion sort."
        ),
        now=NOW,
    )

    assert result.changes == ()
    assert result.question == (
        "I can help schedule that study session, but I need to know what time "
        "you want to begin, how long it should last, and whether race "
        "conditions and insertion sort should be one session or separate ones."
    )
    assert result.outcome is AcademicAgentLoopOutcome.CLARIFICATION_REQUIRED
    assert catalog.course_queries == []
    assert catalog.assessment_queries == []


@pytest.mark.asyncio
async def test_study_session_accepts_model_resolved_relative_date() -> None:
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 250"),)
            ),
            AcademicAgentDecision(
                tool_calls=(
                    CreateStudySessionCall(
                        tool="create_study_session",
                        course_id="course-ece250",
                        topic="recursion",
                        starts_at=datetime(2026, 9, 9, 23, tzinfo=UTC),
                        duration_minutes=30,
                    ),
                )
            ),
        )
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message="Schedule 30 minutes to review recursion for ECE 250 tomorrow at 7 PM.",
        now=NOW,
    )

    assert result.outcome is AcademicAgentLoopOutcome.PROPOSAL_READY
    assert result.question is None
    assert len(result.changes) == 1
    assert result.changes[0].due_at == datetime(2026, 9, 9, 23, tzinfo=UTC)


@pytest.mark.asyncio
async def test_study_session_accepts_model_resolved_local_clock() -> None:
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 250"),)
            ),
            AcademicAgentDecision(
                tool_calls=(
                    CreateStudySessionCall(
                        tool="create_study_session",
                        course_id="course-ece250",
                        topic="recursion",
                        starts_at=datetime(2026, 9, 8, 23, tzinfo=UTC),
                        duration_minutes=30,
                    ),
                )
            ),
        )
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message="Schedule 30 minutes to review recursion for ECE 250 tomorrow at 7pm.",
        now=NOW,
    )

    assert result.outcome is AcademicAgentLoopOutcome.PROPOSAL_READY
    assert result.question is None
    assert len(result.changes) == 1
    assert result.changes[0].due_at == datetime(2026, 9, 8, 23, tzinfo=UTC)


@pytest.mark.asyncio
async def test_study_session_accepts_ordered_topics_that_share_a_word() -> None:
    start = datetime(2026, 9, 8, 23, tzinfo=UTC)
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 250"),)
            ),
            AcademicAgentDecision(
                tool_calls=(
                    CreateStudySessionCall(
                        tool="create_study_session",
                        course_id="course-ece250",
                        topic="graph traversal",
                        starts_at=start,
                        duration_minutes=30,
                    ),
                    CreateStudySessionCall(
                        tool="create_study_session",
                        course_id="course-ece250",
                        topic="graph coloring",
                        starts_at=start + timedelta(minutes=30),
                        duration_minutes=30,
                    ),
                )
            ),
        )
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message=(
            "Study graph traversal and graph coloring for ECE 250 tomorrow at 7pm, 30 minutes each."
        ),
        now=NOW,
    )

    assert result.outcome is AcademicAgentLoopOutcome.PROPOSAL_READY
    assert result.question is None
    assert [change.title for change in result.changes] == [
        "Studying Block — graph traversal",
        "Studying Block — graph coloring",
    ]


@pytest.mark.asyncio
async def test_study_session_accepts_model_ordered_topics_without_raw_order_reparse() -> None:
    start = datetime(2026, 9, 8, 23, tzinfo=UTC)
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 250"),)
            ),
            AcademicAgentDecision(
                tool_calls=(
                    CreateStudySessionCall(
                        tool="create_study_session",
                        course_id="course-ece250",
                        topic="graph coloring",
                        starts_at=start,
                        duration_minutes=30,
                    ),
                    CreateStudySessionCall(
                        tool="create_study_session",
                        course_id="course-ece250",
                        topic="graph traversal",
                        starts_at=start + timedelta(minutes=30),
                        duration_minutes=30,
                    ),
                )
            ),
        )
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message=(
            "Study graph traversal and graph coloring for ECE 250 tomorrow at 7pm, 30 minutes each."
        ),
        now=NOW,
    )

    assert result.outcome is AcademicAgentLoopOutcome.PROPOSAL_READY
    assert result.question is None
    assert [change.title for change in result.changes] == [
        "Studying Block — graph coloring",
        "Studying Block — graph traversal",
    ]


@pytest.mark.asyncio
async def test_study_session_accepts_model_resolved_topic_without_raw_topic_reparse() -> None:
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 250"),)
            ),
            AcademicAgentDecision(
                tool_calls=(
                    CreateStudySessionCall(
                        tool="create_study_session",
                        course_id="course-ece250",
                        topic="red black trees",
                        starts_at=DUE,
                        duration_minutes=30,
                    ),
                )
            ),
        )
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message="Schedule 30 minutes to review recursion for ECE 250 tomorrow.",
        now=NOW,
    )

    assert result.outcome is AcademicAgentLoopOutcome.PROPOSAL_READY
    assert result.question is None
    assert len(result.changes) == 1
    assert result.changes[0].title == "Studying Block — red black trees"


@pytest.mark.asyncio
async def test_study_session_rejects_duplicate_topic_start_pairs() -> None:
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 250"),)
            ),
            AcademicAgentDecision(
                tool_calls=(
                    CreateStudySessionCall(
                        tool="create_study_session",
                        course_id="course-ece250",
                        topic="recursion",
                        starts_at=DUE,
                        duration_minutes=30,
                    ),
                    CreateStudySessionCall(
                        tool="create_study_session",
                        course_id="course-ece250",
                        topic="recursion",
                        starts_at=DUE,
                        duration_minutes=30,
                    ),
                )
            ),
        )
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message="Schedule separate 30 minute study sessions for recursion in ECE 250 tomorrow.",
        now=NOW,
    )

    assert result.changes == ()
    assert result.question == "Duplicate study-session blocks were not accepted."
    assert result.outcome is AcademicAgentLoopOutcome.HOST_VALIDATION_FAILED


@pytest.mark.asyncio
async def test_multiple_study_topics_without_split_preference_uses_model_decision() -> None:
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 250"),)
            ),
            AcademicAgentDecision(
                tool_calls=(
                    CreateStudySessionCall(
                        tool="create_study_session",
                        course_id="course-ece250",
                        topic="recursion",
                        starts_at=DUE,
                        duration_minutes=30,
                    ),
                    CreateStudySessionCall(
                        tool="create_study_session",
                        course_id="course-ece250",
                        topic="AVL trees",
                        starts_at=DUE + timedelta(minutes=30),
                        duration_minutes=30,
                    ),
                )
            ),
        )
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message="I need to study recursion and AVL trees for ECE 250 tomorrow at 7 PM.",
        now=NOW,
    )

    assert result.outcome is AcademicAgentLoopOutcome.PROPOSAL_READY
    assert result.question is None
    assert [change.title for change in result.changes] == [
        "Studying Block — recursion",
        "Studying Block — AVL trees",
    ]


@pytest.mark.asyncio
async def test_single_study_call_uses_model_topic_scope_without_raw_completeness_reparse() -> None:
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 250"),)
            ),
            AcademicAgentDecision(
                tool_calls=(
                    CreateStudySessionCall(
                        tool="create_study_session",
                        course_id="course-ece250",
                        topic="recursion",
                        starts_at=DUE,
                        duration_minutes=30,
                    ),
                )
            ),
        )
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message=(
            "Create one combined study session for recursion and AVL trees in ECE 250 "
            "tomorrow at 7 PM for one hour."
        ),
        now=NOW,
    )

    assert result.outcome is AcademicAgentLoopOutcome.PROPOSAL_READY
    assert result.question is None
    assert len(result.changes) == 1
    assert result.changes[0].title == "Studying Block — recursion"


@pytest.mark.asyncio
async def test_each_study_blocks_may_have_gap_when_model_emits_non_overlapping_times() -> None:
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 250"),)
            ),
            AcademicAgentDecision(
                tool_calls=(
                    CreateStudySessionCall(
                        tool="create_study_session",
                        course_id="course-ece250",
                        topic="recursion",
                        starts_at=DUE,
                        duration_minutes=30,
                    ),
                    CreateStudySessionCall(
                        tool="create_study_session",
                        course_id="course-ece250",
                        topic="AVL trees",
                        starts_at=DUE + timedelta(minutes=45),
                        duration_minutes=30,
                    ),
                )
            ),
        )
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message=(
            "Create separate study sessions for recursion and AVL trees in ECE 250 tomorrow "
            "at 7 PM, 30 minutes each."
        ),
        now=NOW,
    )

    assert result.outcome is AcademicAgentLoopOutcome.PROPOSAL_READY
    assert result.question is None
    assert [change.due_at for change in result.changes] == [
        DUE,
        DUE + timedelta(minutes=45),
    ]


@pytest.mark.asyncio
async def test_one_combined_call_uses_model_split_decision_without_raw_reparse() -> None:
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 250"),)
            ),
            AcademicAgentDecision(
                tool_calls=(
                    CreateStudySessionCall(
                        tool="create_study_session",
                        course_id="course-ece250",
                        topic="recursion and AVL trees",
                        starts_at=DUE,
                        duration_minutes=60,
                    ),
                )
            ),
        )
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message=(
            "Create separate study sessions for recursion and AVL trees in ECE 250 tomorrow "
            "at 7 PM, 30 minutes each."
        ),
        now=NOW,
    )

    assert result.outcome is AcademicAgentLoopOutcome.PROPOSAL_READY
    assert result.question is None
    assert len(result.changes) == 1
    assert result.changes[0].title == "Studying Block — recursion and AVL trees"


def test_create_assessment_call_rejects_non_user_creatable_types() -> None:
    with pytest.raises(ValidationError):
        CreateAssessmentCall(
            tool="create_assessment",
            course_id="course-ece202",
            title="Midterm",
            due_at=DUE,
            assessment_type=AssessmentType.MIDTERM,  # type: ignore[reportArgumentType]
        )


@pytest.mark.asyncio
async def test_unsupported_create_type_word_uses_model_type_decision() -> None:
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 202"),)
            ),
            AcademicAgentDecision(
                tool_calls=(
                    CreateAssessmentCall(
                        tool="create_assessment",
                        course_id="course-ece202",
                        title="Thing",
                        due_at=DUE,
                        assessment_type=UserCreatableAssessmentType.ASSIGNMENT,
                    ),
                )
            ),
        )
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message="schedule an ECE 202 thing",
        now=NOW,
    )

    assert result.outcome is AcademicAgentLoopOutcome.PROPOSAL_READY
    assert result.question is None
    assert len(result.changes) == 1
    assert result.changes[0].assessment_type is AssessmentType.ASSIGNMENT


@pytest.mark.asyncio
async def test_ungrounded_create_type_word_uses_model_type_decision() -> None:
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 202"),)
            ),
            AcademicAgentDecision(
                tool_calls=(
                    CreateAssessmentCall(
                        tool="create_assessment",
                        course_id="course-ece202",
                        title="Op amps",
                        due_at=DUE,
                        assessment_type=UserCreatableAssessmentType.ASSIGNMENT,
                    ),
                )
            ),
        )
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message="create lab op amps in ECE 202",
        now=NOW,
    )

    assert result.outcome is AcademicAgentLoopOutcome.PROPOSAL_READY
    assert result.question is None
    assert len(result.changes) == 1
    assert result.changes[0].assessment_type is AssessmentType.ASSIGNMENT


@pytest.mark.asyncio
async def test_conflicting_create_type_words_use_model_type_decision() -> None:
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 202"),)
            ),
            AcademicAgentDecision(
                tool_calls=(
                    CreateAssessmentCall(
                        tool="create_assessment",
                        course_id="course-ece202",
                        title="Quiz assignment packet",
                        due_at=DUE,
                        assessment_type=UserCreatableAssessmentType.ASSIGNMENT,
                    ),
                )
            ),
        )
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message="create quiz assignment packet in ECE 202",
        now=NOW,
    )

    assert result.outcome is AcademicAgentLoopOutcome.PROPOSAL_READY
    assert result.question is None
    assert len(result.changes) == 1
    assert result.changes[0].title == "Assignment — Quiz assignment packet"


@pytest.mark.asyncio
async def test_mixed_read_and_mutation_turn_executes_only_read_then_replans() -> None:
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 202"),)
            ),
            AcademicAgentDecision(
                tool_calls=(
                    SearchAssessmentsCall(tool="search_assessments", query="old assignment"),
                    CreateAssessmentCall(
                        tool="create_assessment",
                        course_id="course-ece202",
                        title="Lab report",
                        due_at=DUE,
                        assessment_type=UserCreatableAssessmentType.LAB,
                    ),
                )
            ),
            AcademicAgentDecision(
                tool_calls=(
                    CreateAssessmentCall(
                        tool="create_assessment",
                        course_id="course-ece202",
                        title="Lab report",
                        due_at=DUE,
                        assessment_type=UserCreatableAssessmentType.LAB,
                    ),
                    ArchiveAssessmentCall(
                        tool="archive_assessment",
                        assessment_id="assessment-ece250-old",
                    ),
                )
            ),
        )
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message="create the lab and delete the old assignment",
        now=NOW,
    )

    assert [change.field for change in result.changes] == [
        "create_assessment",
        "archive_assessment",
    ]
    assert result.changes[0].title == "Lab — Lab report"
    assert "Premature mutation calls" in gateway.prompts[2]


@pytest.mark.asyncio
async def test_unknown_mutation_ids_fail_closed_to_question() -> None:
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(
                    UpdateAssessmentCall(
                        tool="update_assessment",
                        assessment_id="invented-assessment",
                        due_at=DUE,
                    ),
                )
            ),
        )
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message="move the made-up assignment",
        now=NOW,
    )

    assert result.changes == ()
    assert result.question == "I could not verify the assessment id for a requested change."
    assert result.outcome is AcademicAgentLoopOutcome.HOST_VALIDATION_FAILED


@pytest.mark.asyncio
async def test_loop_stops_after_max_turns_without_proposal() -> None:
    gateway = Gateway(
        tuple(
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 202"),)
            )
            for _ in range(10)
        )
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message="keep looking forever",
        now=NOW,
    )

    assert len(gateway.prompts) == 10
    assert result.turns == 10
    assert result.changes == ()
    assert result.question == (
        "I could not resolve that into a safe proposal within 10 model turns."
    )
    assert result.outcome is AcademicAgentLoopOutcome.AGENT_TURN_LIMIT_EXHAUSTED


@pytest.mark.asyncio
async def test_invalid_model_output_is_distinct_failure_outcome() -> None:
    events: list[AcademicAgentProgressEvent] = []

    async def collect_progress(event: AcademicAgentProgressEvent) -> None:
        events.append(event)

    result = await run_academic_agent_loop(
        gateway=Gateway((None,)),
        catalog=Catalog(),
        message="create assignment problem set 1 in ECE 202",
        now=NOW,
        progress_sink=collect_progress,
    )

    assert result.changes == ()
    assert result.outcome is AcademicAgentLoopOutcome.MODEL_INVALID_OUTPUT
    assert [event.phase for event in events] == [
        AcademicAgentProgressPhase.MODEL_TURN,
        AcademicAgentProgressPhase.FAILED,
    ]
    assert events[-1].terminal is True


@pytest.mark.asyncio
async def test_model_failures_and_timeouts_are_distinct_outcomes() -> None:
    timeout_result = await run_academic_agent_loop(
        gateway=Gateway((TimeoutError(),)),
        catalog=Catalog(),
        message="create assignment problem set 1 in ECE 202",
        now=NOW,
    )
    failed_result = await run_academic_agent_loop(
        gateway=Gateway((RuntimeError("llm unavailable"),)),
        catalog=Catalog(),
        message="create assignment problem set 1 in ECE 202",
        now=NOW,
    )

    assert timeout_result.outcome is AcademicAgentLoopOutcome.MODEL_TIMEOUT
    assert failed_result.outcome is AcademicAgentLoopOutcome.MODEL_FAILED


@pytest.mark.asyncio
async def test_free_form_delete_is_semantically_planned_as_archives() -> None:
    class DeleteCatalog(Catalog):
        def search_assessments(self, query: str, course_id: str | None = None):
            self.assessment_queries.append((query, course_id))
            return (
                AcademicAssessmentOption(
                    assessment_id="study-race",
                    course_id="course-ece250",
                    course_code="ECE 250",
                    title="Studying Block — race conditions",
                    due_at=datetime(2026, 9, 10, 23, tzinfo=UTC),
                    assessment_type=AssessmentType.STUDYING_BLOCK,
                    expected_last_edited_at=NOW,
                ),
                AcademicAssessmentOption(
                    assessment_id="study-sort",
                    course_id="course-ece250",
                    course_code="ECE 250",
                    title="Studying Block — insertion sort",
                    due_at=datetime(2026, 9, 10, 23, 45, tzinfo=UTC),
                    assessment_type=AssessmentType.STUDYING_BLOCK,
                    expected_last_edited_at=NOW,
                ),
            )

    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(
                    SearchAssessmentsCall(
                        tool="search_assessments",
                        query="both study sessions tomorrow",
                    ),
                ),
            ),
            AcademicAgentDecision(
                tool_calls=(
                    ArchiveAssessmentCall(tool="archive_assessment", assessment_id="study-race"),
                    ArchiveAssessmentCall(tool="archive_assessment", assessment_id="study-sort"),
                ),
            ),
        )
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=DeleteCatalog(),
        message=(
            "Uhh can you delete both my study things for tmrw pls—and commit to your memory "
            "that I'm confident on recursion + insertion sort now?"
        ),
        now=NOW,
    )

    assert result.outcome is AcademicAgentLoopOutcome.PROPOSAL_READY
    assert [change.field for change in result.changes] == [
        "archive_assessment",
        "archive_assessment",
    ]
    assert "free-form, unsanitized" in gateway.prompts[0]
    assert "Never turn an existing-item deletion into create_study_session" in gateway.prompts[0]
    assert "do not ask the user to restate or identify" in gateway.prompts[0]


@pytest.mark.asyncio
async def test_model_owned_clarification_is_passed_through_without_host_rewriting() -> None:
    question = "Would you like dawn-ish to mean 6:00 AM, or another exact time?"
    result = await run_academic_agent_loop(
        gateway=Gateway((AcademicAgentDecision(question=question),)),
        catalog=Catalog(),
        message="Chuck in a review thing sometime around dawn-ish tomorrow",
        now=NOW,
    )

    assert result.outcome is AcademicAgentLoopOutcome.CLARIFICATION_REQUIRED
    assert result.question == question


def test_semantic_not_applicable_decision_cannot_smuggle_event_tools() -> None:
    with pytest.raises(ValidationError):
        AcademicAgentDecision(
            not_applicable=True,
            tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 250"),),
        )


@pytest.mark.asyncio
async def test_prompt_assigns_free_form_operation_reasoning_to_qwen() -> None:
    gateway = Gateway((AcademicAgentDecision(question="What type is it?"),))

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message="schedule something for ECE 202",
        now=NOW,
    )

    prompt = gateway.prompts[0]
    assert result.changes == ()
    assert "quiz, assignment, tutorial, lab, studying_block" in prompt
    assert "free-form, unsanitized" in prompt
    assert "Distinguish create, update, archive/delete" in prompt
    assert "misspellings, paraphrases, pronouns" in prompt
    assert "separate semantic memory agent" in prompt
    assert "create_study_session" in prompt
    assert "combined versus separate" in prompt
    assert "The quantifier each, per topic, or apiece" in prompt
