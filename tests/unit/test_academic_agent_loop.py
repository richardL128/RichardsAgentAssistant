from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.agents.academic_planner.agent_loop import run_academic_agent_loop
from app.agents.academic_planner.contracts import (
    AcademicAgentDecision,
    AcademicAssessmentOption,
    AcademicCourseOption,
    ArchiveAssessmentCall,
    AssessmentType,
    CreateAssessmentCall,
    SearchAssessmentsCall,
    SearchCoursesCall,
    UpdateAssessmentCall,
)

NOW = datetime(2026, 9, 7, 14, tzinfo=UTC)
DUE = datetime(2026, 9, 14, 21, tzinfo=UTC)
EDITED = datetime(2026, 9, 6, 22, tzinfo=UTC)


class Gateway:
    def __init__(self, decisions: Sequence[AcademicAgentDecision | None]) -> None:
        self._decisions = iter(decisions)
        self.prompts: list[str] = []

    async def invoke_structured(self, *, prompt, response_model):
        assert response_model is AcademicAgentDecision
        self.prompts.append(prompt)
        return SimpleNamespace(output=next(self._decisions))


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
                        assessment_type=AssessmentType.ASSIGNMENT,
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
    assert result.changes[0].title == "Assignment — Lab report"
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
                        assessment_type=AssessmentType.ASSIGNMENT,
                    ),
                )
            ),
        )
    )
    catalog = Catalog()

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=catalog,
        message="put problem set 1 in ECE 202",
        now=NOW,
    )

    assert catalog.course_queries == ["ECE 202"]
    assert len(gateway.prompts) == 2
    assert "course-ece202" in gateway.prompts[1]
    assert result.changes[0].course_id == "course-ece202"


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
                        assessment_type=AssessmentType.ASSIGNMENT,
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
                        assessment_type=AssessmentType.ASSIGNMENT,
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
    assert result.changes[0].title == "Assignment — Lab report"
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


@pytest.mark.asyncio
async def test_loop_stops_after_max_turns_without_proposal() -> None:
    gateway = Gateway(
        (
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 202"),)
            ),
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 202"),)
            ),
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 202"),)
            ),
            AcademicAgentDecision(
                tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 202"),)
            ),
        )
    )

    result = await run_academic_agent_loop(
        gateway=gateway,
        catalog=Catalog(),
        message="keep looking forever",
        now=NOW,
    )

    assert len(gateway.prompts) == 4
    assert result.turns == 4
    assert result.changes == ()
    assert result.question == (
        "I could not resolve that into a safe proposal within four model turns."
    )
