from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.agents.academic_planner.contracts import AssessmentType, ProposedChange
from app.agents.academic_planner.notion_mutations import DiscoveredAcademicNotionWriter
from app.core.errors import LifeAgentError
from app.db.academic import (
    AcademicAssessmentMutationTarget,
    AcademicCourseMutationTarget,
)

PROPOSAL_ID = UUID("86e6391c-04e7-4413-97e8-eb027f8cc3c6")
EDITED_AT = datetime(2026, 9, 6, 18, tzinfo=UTC)


class _Targets:
    course = AcademicCourseMutationTarget(
        course_id="course-1",
        course_code="ECE 202",
        data_source_id="source123",
        title_property_id="titleProp",
        date_property_id="dateProp",
    )
    assessment = AcademicAssessmentMutationTarget(
        assessment_id="assessment-1",
        course_id="course-1",
        page_id="page123",
        title="Old assignment",
        last_edited_at=EDITED_AT,
        title_property_id="titleProp",
        date_property_id="dateProp",
    )

    def resolve_course_mutation_target(self, course_id: str):
        return self.course if course_id == self.course.course_id else None

    def resolve_assessment_mutation_target(self, assessment_id: str):
        return self.assessment if assessment_id == self.assessment.assessment_id else None


class _Connector:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def create_assessment_page(self, **kwargs):
        self.calls.append(("create", kwargs))

    async def guarded_update_assessment_page(self, **kwargs):
        self.calls.append(("update", kwargs))

    async def guarded_archive_assessment_page(self, **kwargs):
        self.calls.append(("archive", kwargs))


@pytest.mark.asyncio
async def test_confirmed_batch_applies_ordered_create_update_and_archive() -> None:
    connector = _Connector()
    writer = DiscoveredAcademicNotionWriter(
        connector=connector,  # type: ignore[arg-type]
        target_store=_Targets(),
    )
    due = datetime(2026, 9, 10, 21, tzinfo=UTC)
    changes = (
        ProposedChange(
            field="create_assessment",
            value="Create Lab 2 in ECE 202",
            course_id="course-1",
            title="Lab 2",
            due_at=due,
            assessment_type=AssessmentType.ASSIGNMENT,
        ),
        ProposedChange(
            field="update_assessment",
            value="Rename Old assignment",
            assessment_id="assessment-1",
            title="Updated assignment",
            expected_title="Old assignment",
            expected_last_edited_at=EDITED_AT,
        ),
        ProposedChange(
            field="archive_assessment",
            value="Archive Old assignment",
            assessment_id="assessment-1",
            expected_title="Old assignment",
            expected_last_edited_at=EDITED_AT,
        ),
    )

    await writer.apply_confirmed_changes(
        changes,
        proposal_id=PROPOSAL_ID,
        confirmation_event=f"confirm {PROPOSAL_ID}",
    )

    assert [name for name, _ in connector.calls] == ["create", "update", "archive"]
    assert connector.calls[0][1]["data_source_id"] == "source123"
    assert connector.calls[1][1]["expected_title"] == "Old assignment"
    assert connector.calls[2][1]["page_id"] == "page123"


@pytest.mark.asyncio
async def test_writer_rejects_tampered_precondition_before_connector_call() -> None:
    connector = _Connector()
    writer = DiscoveredAcademicNotionWriter(
        connector=connector,  # type: ignore[arg-type]
        target_store=_Targets(),
    )
    change = ProposedChange(
        field="archive_assessment",
        value="Archive Old assignment",
        assessment_id="assessment-1",
        expected_title="Different title",
        expected_last_edited_at=EDITED_AT,
    )

    with pytest.raises(LifeAgentError):
        await writer.apply_confirmed_changes(
            (change,),
            proposal_id=PROPOSAL_ID,
            confirmation_event=f"confirm {PROPOSAL_ID}",
        )

    assert connector.calls == []


@pytest.mark.asyncio
async def test_writer_requires_exact_confirmation() -> None:
    connector = _Connector()
    writer = DiscoveredAcademicNotionWriter(
        connector=connector,  # type: ignore[arg-type]
        target_store=_Targets(),
    )

    with pytest.raises(LifeAgentError):
        await writer.apply_confirmed_changes(
            (),
            proposal_id=PROPOSAL_ID,
            confirmation_event="confirm another-proposal",
        )
