from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.agents.academic_planner.contracts import AssessmentType, ProposedChange
from app.agents.academic_planner.notion_mutations import DiscoveredAcademicNotionWriter
from app.connectors.notion import NotionWriteReceipt
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

    def __init__(self) -> None:
        self.operations: dict[tuple[UUID, int], dict[str, object]] = {}

    def begin_proposal_operation(
        self,
        *,
        proposal_id: UUID,
        ordinal: int,
        payload_hash: str,
        operation_id: str,
    ):
        key = (proposal_id, ordinal)
        existing = self.operations.get(key)
        if existing is not None:
            state = "already_applied" if existing["state"] == "applied" else existing["state"]
            return state, existing
        row = {
            "proposal_id": proposal_id,
            "ordinal": ordinal,
            "payload_hash": payload_hash,
            "operation_id": operation_id,
            "state": "in_progress",
        }
        self.operations[key] = row
        return "ready", row

    def mark_proposal_operation_applied(
        self,
        *,
        proposal_id: UUID,
        ordinal: int,
        payload_hash: str,
        receipt: dict[str, object],
    ):
        row = self.operations[(proposal_id, ordinal)]
        assert row["payload_hash"] == payload_hash
        row["state"] = "applied"
        row["receipt"] = receipt
        return row

    def mark_proposal_operation_uncertain(
        self,
        *,
        proposal_id: UUID,
        ordinal: int,
        payload_hash: str,
        error_code: str,
    ):
        row = self.operations[(proposal_id, ordinal)]
        assert row["payload_hash"] == payload_hash
        row["state"] = "uncertain"
        row["error_code"] = error_code
        return row


class _Connector:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def create_assessment_page(self, **kwargs):
        self.calls.append(("create", kwargs))
        return NotionWriteReceipt(
            proposal_id=str(kwargs["proposal_id"]),
            page_id="created-page",
            url="https://www.notion.so/created-page",
        )

    async def guarded_update_assessment_page(self, **kwargs):
        self.calls.append(("update", kwargs))
        return NotionWriteReceipt(proposal_id=str(kwargs["proposal_id"]), page_id="updated-page")

    async def guarded_archive_assessment_page(self, **kwargs):
        self.calls.append(("archive", kwargs))
        return NotionWriteReceipt(proposal_id=str(kwargs["proposal_id"]), page_id="archived-page")


@pytest.mark.asyncio
async def test_confirmed_batch_applies_ordered_create_update_and_archive() -> None:
    connector = _Connector()
    targets = _Targets()
    writer = DiscoveredAcademicNotionWriter(
        connector=connector,  # type: ignore[arg-type]
        target_store=targets,
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
    assert {row["state"] for row in targets.operations.values()} == {"applied"}


@pytest.mark.asyncio
async def test_study_create_passes_scheduled_end_and_records_receipt() -> None:
    connector = _Connector()
    targets = _Targets()
    writer = DiscoveredAcademicNotionWriter(
        connector=connector,  # type: ignore[arg-type]
        target_store=targets,
    )
    starts_at = datetime(2026, 9, 10, 23, tzinfo=UTC)
    ends_at = datetime(2026, 9, 10, 23, 45, tzinfo=UTC)
    change = SimpleNamespace(
        field="create_assessment",
        value="Studying Block - Race conditions",
        course_id="course-1",
        title="Studying Block - Race conditions",
        due_at=starts_at,
        ends_at=ends_at,
        assessment_type=AssessmentType.STUDYING_BLOCK,
    )

    await writer.apply_confirmed_changes(
        (change,),  # type: ignore[arg-type]
        proposal_id=PROPOSAL_ID,
        confirmation_event=f"confirm {PROPOSAL_ID}",
    )

    assert connector.calls[0][1]["due"] == starts_at
    assert connector.calls[0][1]["ends_at"] == ends_at
    journal = targets.operations[(PROPOSAL_ID, 0)]
    assert journal["state"] == "applied"
    assert journal["receipt"] == {
        "proposal_id": f"{PROPOSAL_ID}:0",
        "page_id": "created-page",
        "url": "https://www.notion.so/created-page",
    }


@pytest.mark.asyncio
async def test_applied_journal_entry_is_not_created_again() -> None:
    connector = _Connector()
    targets = _Targets()
    writer = DiscoveredAcademicNotionWriter(
        connector=connector,  # type: ignore[arg-type]
        target_store=targets,
    )
    due = datetime(2026, 9, 10, 21, tzinfo=UTC)
    change = ProposedChange(
        field="create_assessment",
        value="Create Lab 2 in ECE 202",
        course_id="course-1",
        title="Lab 2",
        due_at=due,
        assessment_type=AssessmentType.ASSIGNMENT,
    )

    await writer.apply_confirmed_changes(
        (change,),
        proposal_id=PROPOSAL_ID,
        confirmation_event=f"confirm {PROPOSAL_ID}",
    )
    await writer.apply_confirmed_changes(
        (change,),
        proposal_id=PROPOSAL_ID,
        confirmation_event=f"confirm {PROPOSAL_ID}",
    )

    assert [name for name, _ in connector.calls] == ["create"]


@pytest.mark.asyncio
async def test_failed_batch_records_completed_and_uncertain_operations() -> None:
    class FailingConnector(_Connector):
        async def create_assessment_page(self, **kwargs):
            receipt = await super().create_assessment_page(**kwargs)
            if len(self.calls) == 2:
                raise RuntimeError("timeout after create")
            return receipt

    connector = FailingConnector()
    targets = _Targets()
    writer = DiscoveredAcademicNotionWriter(
        connector=connector,  # type: ignore[arg-type]
        target_store=targets,
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
            field="create_assessment",
            value="Create Lab 3 in ECE 202",
            course_id="course-1",
            title="Lab 3",
            due_at=due,
            assessment_type=AssessmentType.ASSIGNMENT,
        ),
    )

    with pytest.raises(RuntimeError):
        await writer.apply_confirmed_changes(
            changes,
            proposal_id=PROPOSAL_ID,
            confirmation_event=f"confirm {PROPOSAL_ID}",
        )

    assert targets.operations[(PROPOSAL_ID, 0)]["state"] == "applied"
    assert targets.operations[(PROPOSAL_ID, 1)]["state"] == "uncertain"

    with pytest.raises(LifeAgentError):
        await writer.apply_confirmed_changes(
            changes,
            proposal_id=PROPOSAL_ID,
            confirmation_event=f"confirm {PROPOSAL_ID}",
        )
    assert len(connector.calls) == 2


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
