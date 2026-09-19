from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Literal, cast
from uuid import UUID

import pytest

from app.agents.academic_planner.contracts import AssessmentType, CheckinProposal, ProposedChange
from app.agents.academic_planner.notion_mutations import (
    AcademicInboundMaterialSnapshot,
    DiscoveredAcademicNotionWriter,
    NotionMutationReview,
    review_notion_mutation_batch,
)
from app.agents.academic_planner.proposal_review import confirm_checkin_proposal
from app.connectors.notion import NotionFileUploadReceipt, NotionUploadedPdf, NotionWriteReceipt
from app.core.errors import LifeAgentError
from app.db.academic import (
    AcademicAssessmentMutationTarget,
    AcademicCourseMutationTarget,
)

PROPOSAL_ID = UUID("86e6391c-04e7-4413-97e8-eb027f8cc3c6")
MATERIAL_ID = UUID("7e4b2d56-24d2-4a48-9d3d-9050a82a95f1")
EDITED_AT = datetime(2026, 9, 6, 18, tzinfo=UTC)
PDF_BYTES = b"%PDF-1.7\nrubric bytes"
_OperationStatus = Literal["ready", "already_applied", "in_progress", "uncertain", "failed"]


@dataclass(frozen=True, slots=True)
class _MaterialSnapshot(AcademicInboundMaterialSnapshot):
    id: UUID
    filename: str
    media_type: str | None
    observed_byte_size: int
    content_hash: str
    raw_artifact_key: str
    state: str
    proposal_id: UUID | None


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

    def resolve_course_mutation_target(self, course_id: str) -> AcademicCourseMutationTarget | None:
        return self.course if course_id == self.course.course_id else None

    def resolve_assessment_mutation_target(
        self, assessment_id: str
    ) -> AcademicAssessmentMutationTarget | None:
        return self.assessment if assessment_id == self.assessment.assessment_id else None

    def __init__(self) -> None:
        self.operations: dict[tuple[UUID, int], dict[str, Any]] = {}
        self.materials: dict[UUID, _MaterialSnapshot] = {}
        self.material_events: list[tuple[str, dict[str, Any]]] = []
        self.material_loads: list[tuple[UUID, UUID]] = []

    def begin_proposal_operation(
        self,
        *,
        proposal_id: UUID,
        ordinal: int,
        payload_hash: str,
        operation_id: str,
    ) -> tuple[_OperationStatus, Any]:
        key = (proposal_id, ordinal)
        existing = self.operations.get(key)
        if existing is not None:
            state: _OperationStatus = (
                "already_applied"
                if existing["state"] == "applied"
                else cast(_OperationStatus, existing["state"])
            )
            return state, existing
        row: dict[str, Any] = {
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
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
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
    ) -> dict[str, Any]:
        row = self.operations[(proposal_id, ordinal)]
        assert row["payload_hash"] == payload_hash
        row["state"] = "uncertain"
        row["error_code"] = error_code
        return row

    def add_material(
        self,
        *,
        material_id: UUID = MATERIAL_ID,
        content: bytes = PDF_BYTES,
        content_hash: str | None = None,
        state: str = "proposal_pending",
        filename: str = "rubric.pdf",
        media_type: str | None = "application/pdf",
        proposal_id: UUID | None = PROPOSAL_ID,
    ) -> _MaterialSnapshot:
        snapshot = _MaterialSnapshot(
            id=material_id,
            filename=filename,
            media_type=media_type,
            observed_byte_size=len(content),
            content_hash=content_hash or hashlib.sha256(content).hexdigest(),
            raw_artifact_key=hashlib.sha256(content).hexdigest(),
            state=state,
            proposal_id=proposal_id,
        )
        self.materials[material_id] = snapshot
        return snapshot

    def load_inbound_material_snapshot(
        self, material_id: UUID, *, proposal_id: UUID
    ) -> _MaterialSnapshot | None:
        self.material_loads.append((material_id, proposal_id))
        return self.materials.get(material_id)

    def mark_inbound_material_seeding(
        self,
        material_id: UUID,
        *,
        proposal_id: UUID,
        assessment_id: str | None = None,
        notion_upload_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "material_id": material_id,
            "proposal_id": proposal_id,
            "assessment_id": assessment_id,
            "notion_upload_id": notion_upload_id,
        }
        self.material_events.append(("seeding", payload))
        return payload

    def mark_inbound_material_seeded(
        self,
        material_id: UUID,
        *,
        proposal_id: UUID,
        assessment_id: str | None = None,
        notion_page_id: str,
        notion_block_id: str | None = None,
        notion_upload_id: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "material_id": material_id,
            "proposal_id": proposal_id,
            "assessment_id": assessment_id,
            "notion_page_id": notion_page_id,
            "notion_block_id": notion_block_id,
            "notion_upload_id": notion_upload_id,
        }
        self.material_events.append(("seeded", payload))
        return payload

    def mark_inbound_material_uncertain(
        self,
        material_id: UUID,
        *,
        proposal_id: UUID,
        assessment_id: str | None = None,
        error_code: str,
        notion_upload_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "material_id": material_id,
            "proposal_id": proposal_id,
            "assessment_id": assessment_id,
            "error_code": error_code,
            "notion_upload_id": notion_upload_id,
        }
        self.material_events.append(("uncertain", payload))
        return payload

    def mark_inbound_material_failed(
        self,
        material_id: UUID,
        *,
        proposal_id: UUID,
        error_code: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "material_id": material_id,
            "proposal_id": proposal_id,
            "error_code": error_code,
        }
        self.material_events.append(("failed", payload))
        return payload


class _Connector:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def create_assessment_page(self, **kwargs: Any) -> NotionWriteReceipt:
        self.calls.append(("create", kwargs))
        uploaded_pdfs = cast(tuple[NotionUploadedPdf, ...], kwargs.get("uploaded_pdfs", ()))
        return NotionWriteReceipt(
            proposal_id=str(kwargs["proposal_id"]),
            page_id="created-page",
            url="https://www.notion.so/created-page",
            file_upload_ids=tuple(item.file_upload_id for item in uploaded_pdfs),
        )

    async def guarded_update_assessment_page(self, **kwargs: Any) -> NotionWriteReceipt:
        self.calls.append(("update", kwargs))
        return NotionWriteReceipt(proposal_id=str(kwargs["proposal_id"]), page_id="updated-page")

    async def guarded_archive_assessment_page(self, **kwargs: Any) -> NotionWriteReceipt:
        self.calls.append(("archive", kwargs))
        return NotionWriteReceipt(proposal_id=str(kwargs["proposal_id"]), page_id="archived-page")

    async def create_pdf_file_upload(self, **kwargs: Any) -> NotionFileUploadReceipt:
        self.calls.append(("create_upload", kwargs))
        upload_id = f"upload-{len([call for call in self.calls if call[0] == 'create_upload'])}"
        return NotionFileUploadReceipt(
            file_upload_id=upload_id,
            filename=kwargs["filename"],
            content_type="application/pdf",
        )

    async def send_pdf_file_upload(self, **kwargs: Any) -> NotionFileUploadReceipt:
        self.calls.append(("send_upload", kwargs))
        return NotionFileUploadReceipt(
            file_upload_id=kwargs["file_upload_id"],
            filename=kwargs["filename"],
            content_type="application/pdf",
            content_length=len(kwargs["content"]),
            status="uploaded",
        )

    async def append_uploaded_pdf_blocks(self, **kwargs: Any) -> NotionWriteReceipt:
        self.calls.append(("append_pdf", kwargs))
        uploaded_pdfs = cast(tuple[NotionUploadedPdf, ...], kwargs.get("uploaded_pdfs", ()))
        uploaded = tuple(item.file_upload_id for item in uploaded_pdfs)
        return NotionWriteReceipt(
            proposal_id=str(kwargs["proposal_id"]),
            page_id=kwargs["page_id"],
            file_upload_ids=uploaded,
            block_ids=tuple(f"block-{index}" for index, _ in enumerate(uploaded, start=1)),
        )


class _Artifacts:
    def __init__(self, bodies: dict[str, bytes]) -> None:
        self.bodies = bodies
        self.loads: list[str] = []

    def get(self, key: str) -> bytes:
        self.loads.append(key)
        return self.bodies[key]


def _review(changes: Sequence[ProposedChange]) -> NotionMutationReview:
    return review_notion_mutation_batch(
        changes,
        proposal_id=PROPOSAL_ID,
        confirmation_event=f"confirm {PROPOSAL_ID}",
    )


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
        review=_review(changes),
    )

    assert [name for name, _ in connector.calls] == ["create", "update", "archive"]
    assert connector.calls[0][1]["data_source_id"] == "source123"
    assert connector.calls[1][1]["expected_title"] == "Old assignment"
    assert connector.calls[2][1]["page_id"] == "page123"
    assert {row["state"] for row in targets.operations.values()} == {"applied"}


@pytest.mark.asyncio
async def test_confirmed_misc_task_writes_only_resolved_misc_name_and_date_properties() -> None:
    connector = _Connector()
    targets = _Targets()
    targets.course = AcademicCourseMutationTarget(
        course_id="misc-course",
        course_code="misc",
        data_source_id="misc-source",
        title_property_id="misc-title",
        date_property_id="misc-date",
    )
    writer = DiscoveredAcademicNotionWriter(
        connector=connector,  # type: ignore[arg-type]
        target_store=targets,
    )
    due = datetime(2026, 9, 14, 22, tzinfo=UTC)
    changes = (
        ProposedChange(
            field="create_assessment",
            value="Task — scrub the toilets",
            course_id="misc-course",
            course_code="misc",
            title="Task — scrub the toilets",
            due_at=due,
            assessment_type=AssessmentType.TASK,
        ),
    )

    await writer.apply_confirmed_changes(
        changes,
        proposal_id=PROPOSAL_ID,
        confirmation_event=f"confirm {PROPOSAL_ID}",
        review=_review(changes),
    )

    assert [name for name, _ in connector.calls] == ["create"]
    payload = connector.calls[0][1]
    assert payload["data_source_id"] == "misc-source"
    assert payload["title_property_id"] == "misc-title"
    assert payload["date_property_id"] == "misc-date"
    assert payload["title"] == "Task — scrub the toilets"
    assert payload["due"] == due


@pytest.mark.asyncio
async def test_course_event_create_passes_scheduled_end_and_records_receipt() -> None:
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
        value="Review race conditions",
        course_id="course-1",
        title="Review race conditions",
        due_at=starts_at,
        ends_at=ends_at,
        assessment_type=AssessmentType.EVENT,
    )

    await writer.apply_confirmed_changes(
        (change,),  # type: ignore[arg-type]
        proposal_id=PROPOSAL_ID,
        confirmation_event=f"confirm {PROPOSAL_ID}",
        review=_review((change,)),  # type: ignore[arg-type]
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
async def test_material_create_does_not_load_or_upload_before_review() -> None:
    connector = _Connector()
    targets = _Targets()
    snapshot = targets.add_material()
    artifacts = _Artifacts({snapshot.raw_artifact_key: PDF_BYTES})
    writer = DiscoveredAcademicNotionWriter(
        connector=connector,  # type: ignore[arg-type]
        target_store=targets,
        artifact_loader=artifacts,
    )
    change = ProposedChange(
        field="create_assessment",
        value="Create Lab 2 in ECE 202 with rubric",
        course_id="course-1",
        title="Lab 2",
        due_at=datetime(2026, 9, 10, 21, tzinfo=UTC),
        assessment_type=AssessmentType.ASSIGNMENT,
        inbound_material_ids=(MATERIAL_ID,),
    )

    with pytest.raises(LifeAgentError):
        await writer.apply_confirmed_changes(
            (change,),
            proposal_id=PROPOSAL_ID,
            confirmation_event=f"confirm {PROPOSAL_ID}",
        )

    assert targets.material_loads == []
    assert targets.material_events == []
    assert artifacts.loads == []
    assert connector.calls == []
    assert targets.operations == {}


@pytest.mark.asyncio
async def test_create_with_material_uploads_before_atomic_page_children() -> None:
    connector = _Connector()
    targets = _Targets()
    snapshot = targets.add_material()
    artifacts = _Artifacts({snapshot.raw_artifact_key: PDF_BYTES})
    writer = DiscoveredAcademicNotionWriter(
        connector=connector,  # type: ignore[arg-type]
        target_store=targets,
        artifact_loader=artifacts,
    )
    change = ProposedChange(
        field="create_assessment",
        value="Create Assignment 2 in ECE 202 with rubric",
        course_id="course-1",
        title="Assignment 2",
        due_at=datetime(2026, 10, 8, 21, tzinfo=UTC),
        assessment_type=AssessmentType.ASSIGNMENT,
        inbound_material_ids=(MATERIAL_ID,),
    )

    await writer.apply_confirmed_changes(
        (change,),
        proposal_id=PROPOSAL_ID,
        confirmation_event=f"confirm {PROPOSAL_ID}",
        review=_review((change,)),
    )

    assert [name for name, _ in connector.calls] == ["create_upload", "send_upload", "create"]
    assert connector.calls[1][1]["content"] == PDF_BYTES
    uploaded = connector.calls[2][1]["uploaded_pdfs"]
    assert uploaded == (NotionUploadedPdf(file_upload_id="upload-1", filename="rubric.pdf"),)
    assert targets.operations[(PROPOSAL_ID, 0)]["receipt"] == {
        "proposal_id": f"{PROPOSAL_ID}:0",
        "page_id": "created-page",
        "url": "https://www.notion.so/created-page",
        "file_upload_ids": ["upload-1"],
    }
    assert targets.material_events[-1] == (
        "seeded",
        {
            "material_id": MATERIAL_ID,
            "proposal_id": PROPOSAL_ID,
            "assessment_id": None,
            "notion_page_id": "created-page",
            "notion_block_id": None,
            "notion_upload_id": "upload-1",
        },
    )


@pytest.mark.asyncio
async def test_attach_material_uploads_and_guarded_append_blocks() -> None:
    connector = _Connector()
    targets = _Targets()
    snapshot = targets.add_material()
    artifacts = _Artifacts({snapshot.raw_artifact_key: PDF_BYTES})
    writer = DiscoveredAcademicNotionWriter(
        connector=connector,  # type: ignore[arg-type]
        target_store=targets,
        artifact_loader=artifacts,
    )
    change = ProposedChange(
        field="attach_assessment_material",
        value="Attach rubric to Old assignment",
        assessment_id="assessment-1",
        expected_title="Old assignment",
        expected_last_edited_at=EDITED_AT,
        inbound_material_ids=(MATERIAL_ID,),
    )

    await writer.apply_confirmed_changes(
        (change,),
        proposal_id=PROPOSAL_ID,
        confirmation_event=f"confirm {PROPOSAL_ID}",
        review=_review((change,)),
    )

    assert [name for name, _ in connector.calls] == ["create_upload", "send_upload", "append_pdf"]
    append = connector.calls[2][1]
    assert append["page_id"] == "page123"
    assert append["expected_title"] == "Old assignment"
    assert append["expected_last_edited_at"] == EDITED_AT
    assert append["uploaded_pdfs"] == (
        NotionUploadedPdf(file_upload_id="upload-1", filename="rubric.pdf"),
    )
    assert targets.operations[(PROPOSAL_ID, 0)]["receipt"] == {
        "proposal_id": f"{PROPOSAL_ID}:0",
        "page_id": "page123",
        "file_upload_ids": ["upload-1"],
        "block_ids": ["block-1"],
    }
    assert targets.material_events[-1] == (
        "seeded",
        {
            "material_id": MATERIAL_ID,
            "proposal_id": PROPOSAL_ID,
            "assessment_id": "assessment-1",
            "notion_page_id": "page123",
            "notion_block_id": "block-1",
            "notion_upload_id": "upload-1",
        },
    )


@pytest.mark.asyncio
async def test_material_hash_mismatch_fails_before_notion_calls() -> None:
    connector = _Connector()
    targets = _Targets()
    snapshot = targets.add_material(content_hash="0" * 64)
    artifacts = _Artifacts({snapshot.raw_artifact_key: PDF_BYTES})
    writer = DiscoveredAcademicNotionWriter(
        connector=connector,  # type: ignore[arg-type]
        target_store=targets,
        artifact_loader=artifacts,
    )
    change = ProposedChange(
        field="attach_assessment_material",
        value="Attach rubric to Old assignment",
        assessment_id="assessment-1",
        expected_title="Old assignment",
        expected_last_edited_at=EDITED_AT,
        inbound_material_ids=(MATERIAL_ID,),
    )

    with pytest.raises(LifeAgentError):
        await writer.apply_confirmed_changes(
            (change,),
            proposal_id=PROPOSAL_ID,
            confirmation_event=f"confirm {PROPOSAL_ID}",
            review=_review((change,)),
        )

    assert connector.calls == []
    assert targets.material_events == [
        (
            "failed",
            {
                "material_id": MATERIAL_ID,
                "proposal_id": PROPOSAL_ID,
                "error_code": "input_invalid",
            },
        )
    ]
    assert targets.operations[(PROPOSAL_ID, 0)]["state"] == "uncertain"


@pytest.mark.asyncio
async def test_lost_upload_response_marks_uncertain_and_does_not_replay() -> None:
    class LostUploadConnector(_Connector):
        async def create_pdf_file_upload(self, **kwargs: Any) -> NotionFileUploadReceipt:
            await super().create_pdf_file_upload(**kwargs)
            raise RuntimeError("lost response after upload creation")

    connector = LostUploadConnector()
    targets = _Targets()
    snapshot = targets.add_material()
    artifacts = _Artifacts({snapshot.raw_artifact_key: PDF_BYTES})
    writer = DiscoveredAcademicNotionWriter(
        connector=connector,  # type: ignore[arg-type]
        target_store=targets,
        artifact_loader=artifacts,
    )
    change = ProposedChange(
        field="create_assessment",
        value="Create Assignment 2 in ECE 202 with rubric",
        course_id="course-1",
        title="Assignment 2",
        due_at=datetime(2026, 10, 8, 21, tzinfo=UTC),
        assessment_type=AssessmentType.ASSIGNMENT,
        inbound_material_ids=(MATERIAL_ID,),
    )

    with pytest.raises(RuntimeError):
        await writer.apply_confirmed_changes(
            (change,),
            proposal_id=PROPOSAL_ID,
            confirmation_event=f"confirm {PROPOSAL_ID}",
            review=_review((change,)),
        )
    with pytest.raises(LifeAgentError):
        await writer.apply_confirmed_changes(
            (change,),
            proposal_id=PROPOSAL_ID,
            confirmation_event=f"confirm {PROPOSAL_ID}",
            review=_review((change,)),
        )

    assert [name for name, _ in connector.calls] == ["create_upload"]
    assert targets.operations[(PROPOSAL_ID, 0)]["state"] == "uncertain"
    assert targets.material_events[-1] == (
        "uncertain",
        {
            "material_id": MATERIAL_ID,
            "proposal_id": PROPOSAL_ID,
            "assessment_id": None,
            "error_code": "internal",
            "notion_upload_id": None,
        },
    )


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
        review=_review((change,)),
    )
    await writer.apply_confirmed_changes(
        (change,),
        proposal_id=PROPOSAL_ID,
        confirmation_event=f"confirm {PROPOSAL_ID}",
        review=_review((change,)),
    )

    assert [name for name, _ in connector.calls] == ["create"]


@pytest.mark.asyncio
async def test_failed_batch_records_completed_and_uncertain_operations() -> None:
    class FailingConnector(_Connector):
        async def create_assessment_page(self, **kwargs: Any) -> NotionWriteReceipt:
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
            review=_review(changes),
        )

    assert targets.operations[(PROPOSAL_ID, 0)]["state"] == "applied"
    assert targets.operations[(PROPOSAL_ID, 1)]["state"] == "uncertain"

    with pytest.raises(LifeAgentError):
        await writer.apply_confirmed_changes(
            changes,
            proposal_id=PROPOSAL_ID,
            confirmation_event=f"confirm {PROPOSAL_ID}",
            review=_review(changes),
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
            review=_review((change,)),
        )

    assert connector.calls == []


@pytest.mark.parametrize("field", ["create_assessment", "archive_assessment"])
@pytest.mark.asyncio
async def test_create_and_archive_require_hitl_review_before_connector_call(field: str) -> None:
    connector = _Connector()
    targets = _Targets()
    writer = DiscoveredAcademicNotionWriter(
        connector=connector,  # type: ignore[arg-type]
        target_store=targets,
    )
    change = (
        ProposedChange(
            field="create_assessment",
            value="Create Lab 2 in ECE 202",
            course_id="course-1",
            title="Lab 2",
            due_at=datetime(2026, 9, 10, 21, tzinfo=UTC),
            assessment_type=AssessmentType.ASSIGNMENT,
        )
        if field == "create_assessment"
        else ProposedChange(
            field="archive_assessment",
            value="Archive Old assignment",
            assessment_id="assessment-1",
            expected_title="Old assignment",
            expected_last_edited_at=EDITED_AT,
        )
    )

    with pytest.raises(LifeAgentError):
        await writer.apply_confirmed_changes(
            (change,),
            proposal_id=PROPOSAL_ID,
            confirmation_event=f"confirm {PROPOSAL_ID}",
        )

    assert connector.calls == []
    assert targets.operations == {}


@pytest.mark.asyncio
async def test_hitl_review_must_match_exact_ordered_batch_before_connector_call() -> None:
    connector = _Connector()
    targets = _Targets()
    writer = DiscoveredAcademicNotionWriter(
        connector=connector,  # type: ignore[arg-type]
        target_store=targets,
    )
    create = ProposedChange(
        field="create_assessment",
        value="Create Lab 2 in ECE 202",
        course_id="course-1",
        title="Lab 2",
        due_at=datetime(2026, 9, 10, 21, tzinfo=UTC),
        assessment_type=AssessmentType.ASSIGNMENT,
    )
    archive = ProposedChange(
        field="archive_assessment",
        value="Archive Old assignment",
        assessment_id="assessment-1",
        expected_title="Old assignment",
        expected_last_edited_at=EDITED_AT,
    )
    review = _review((archive, create))

    with pytest.raises(LifeAgentError):
        await writer.apply_confirmed_changes(
            (create, archive),
            proposal_id=PROPOSAL_ID,
            confirmation_event=f"confirm {PROPOSAL_ID}",
            review=review,
        )

    assert connector.calls == []
    assert targets.operations == {}


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


@pytest.mark.parametrize("field", ["create_assessment", "archive_assessment"])
@pytest.mark.asyncio
async def test_confirmation_workflow_supplies_bound_review_to_configured_writer(
    field: str,
) -> None:
    change = (
        ProposedChange(
            field="create_assessment",
            value="Create Lab 2 in ECE 202",
            course_id="course-1",
            title="Lab 2",
            due_at=datetime(2026, 9, 10, 21, tzinfo=UTC),
            assessment_type=AssessmentType.ASSIGNMENT,
        )
        if field == "create_assessment"
        else ProposedChange(
            field="archive_assessment",
            value="Archive Old assignment",
            assessment_id="assessment-1",
            expected_title="Old assignment",
            expected_last_edited_at=EDITED_AT,
        )
    )
    proposal = CheckinProposal(
        proposal_id=PROPOSAL_ID,
        confirmation_event=f"confirm {PROPOSAL_ID}",
        changes=(change,),
    )

    class _ProposalStore:
        applied = False

        def prepare_checkin_application(
            self,
            proposal_id: UUID,
            confirmation_event: str,
            **_kwargs: Any,
        ) -> tuple[str, CheckinProposal]:
            assert proposal_id == PROPOSAL_ID
            assert confirmation_event == proposal.confirmation_event
            return "ready", proposal

        def mark_checkin_applied(
            self,
            proposal_id: UUID,
            confirmation_event: str | None = None,
        ) -> None:
            assert proposal_id == PROPOSAL_ID
            assert confirmation_event == proposal.confirmation_event
            self.applied = True

    connector = _Connector()
    targets = _Targets()
    writer = DiscoveredAcademicNotionWriter(
        connector=connector,  # type: ignore[arg-type]
        target_store=targets,
    )
    store = _ProposalStore()

    result = await confirm_checkin_proposal(
        store=store,  # type: ignore[arg-type]
        writer=writer,
        proposal_id=PROPOSAL_ID,
        confirmation_event=proposal.confirmation_event,
    )

    assert result["status"] == "applied"
    assert store.applied is True
    assert [name for name, _ in connector.calls] == [
        "create" if field == "create_assessment" else "archive"
    ]
