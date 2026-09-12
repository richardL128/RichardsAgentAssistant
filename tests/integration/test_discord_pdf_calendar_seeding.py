from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agents.academic_planner.contracts import (
    AssessmentType,
    CheckinProposal,
    InboundMaterialProposalPreview,
    ProposedChange,
)
from app.agents.academic_planner.notion_mutations import DiscoveredAcademicNotionWriter
from app.agents.academic_planner.proposal_review import confirm_checkin_proposal
from app.artifacts.store import ArtifactStore
from app.connectors.notion import (
    NotionFileUploadReceipt,
    NotionUploadedPdf,
    NotionWriteReceipt,
)
from app.db.academic import (
    AcademicInboundMaterialInput,
    AcademicRepository,
    AssessmentSourceTrace,
    CourseCalendarInput,
    SourceCitation,
    SQLAlchemyAcademicPlannerStore,
)
from app.db.models import AcademicInboundMaterial, AcademicProposedChange, Base

NOW = datetime(2026, 9, 11, 15, tzinfo=UTC)
OWNER = "333333333333333333"
CHANNEL = "222222222222222222"
PDF = b"%PDF-1.7\nDiscord rubric acceptance bytes"


class _NotionBoundary:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def create_pdf_file_upload(self, *, filename: str) -> NotionFileUploadReceipt:
        self.calls.append("create_upload")
        return NotionFileUploadReceipt(file_upload_id="upload-1", filename=filename)

    async def send_pdf_file_upload(
        self, *, file_upload_id: str, filename: str, content: bytes
    ) -> NotionFileUploadReceipt:
        self.calls.append("send_upload")
        assert content == PDF
        return NotionFileUploadReceipt(
            file_upload_id=file_upload_id,
            filename=filename,
            content_length=len(content),
            status="uploaded",
        )

    async def create_assessment_page(self, **kwargs: Any) -> NotionWriteReceipt:
        self.calls.append("create_page")
        uploads = cast(tuple[NotionUploadedPdf, ...], kwargs["uploaded_pdfs"])
        return NotionWriteReceipt(
            proposal_id=str(kwargs["proposal_id"]),
            page_id="created-assessment-page",
            file_upload_ids=tuple(item.file_upload_id for item in uploads),
        )

    async def append_uploaded_pdf_blocks(self, **kwargs: Any) -> NotionWriteReceipt:
        self.calls.append("append_pdf")
        uploads = cast(tuple[NotionUploadedPdf, ...], kwargs["uploaded_pdfs"])
        return NotionWriteReceipt(
            proposal_id=str(kwargs["proposal_id"]),
            page_id=str(kwargs["page_id"]),
            file_upload_ids=tuple(item.file_upload_id for item in uploads),
            block_ids=("pdf-block-1",),
        )


class _SyncBoundary:
    def __init__(self) -> None:
        self.calls = 0

    async def sync(self, *, now: datetime | None = None) -> object:
        assert now is not None
        self.calls += 1
        return SimpleNamespace(status="succeeded", material_job_count=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["new", "existing"])
async def test_confirmed_pdf_seed_crosses_real_persistence_and_notion_boundaries(
    tmp_path, target: str
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    artifact = artifacts.put(
        PDF,
        media_type="application/pdf",
        data_class="discord_academic_pdf_private",
        already_redacted=True,
    )
    with Session(engine) as session, session.begin():
        course = AcademicRepository.upsert_course(
            session,
            notion_id="course-page",
            course_code="ECE 222",
            title="Digital Computers",
            term="Fall 2026",
        )
        AcademicRepository.upsert_course_calendar(
            session,
            calendar=CourseCalendarInput(
                course_id=course.id,
                course_page_id="course-page",
                child_database_id="assessment-database",
                child_data_source_id="assessment-source",
                title_property_id="title-property",
                title_property_name="Name",
                date_property_id="date-property",
                date_property_name="Date",
                discovery_status="valid",
                last_discovered_at=NOW,
                last_synced_at=NOW,
            ),
        )
        assessment = AcademicRepository.upsert_assessment(
            session,
            notion_id="existing-assessment-page",
            course_id=course.id,
            title="Assignment 2",
            assessment_type="assignment",
            due_at=NOW + timedelta(days=7),
            grade_weight_percent=None,
            confidence=1,
            fact_state="confirmed",
            citation=SourceCitation(),
            trace=AssessmentSourceTrace(
                source_id="assessment-source",
                source_scope="notion:assessment-source",
                notion_last_edited_at=NOW,
                title_property_id="title-property",
            ),
        )
        course_id = str(course.id)
        assessment_id = str(assessment.id)

    store = SQLAlchemyAcademicPlannerStore(engine)
    _, material = store.create_or_replay_inbound_material(
        AcademicInboundMaterialInput(
            discord_message_id="111111111111111111",
            discord_attachment_id="555555555555555555",
            owner_discord_user_id=OWNER,
            discord_channel_id=CHANNEL,
            filename="ece222-a2-rubric.pdf",
            media_type="application/pdf",
            declared_byte_size=len(PDF),
            observed_byte_size=len(PDF),
            content_hash=hashlib.sha256(PDF).hexdigest(),
            raw_artifact_key=artifact.key,
            captured_at=NOW,
            expires_at=NOW + timedelta(days=2),
        )
    )
    preview = InboundMaterialProposalPreview(
        inbound_material_id=material.id,
        filename=material.filename,
        byte_size=material.observed_byte_size,
    )
    if target == "new":
        change = ProposedChange(
            field="create_assessment",
            value="Create ECE 222 Assignment 3 with its rubric.",
            course_id=course_id,
            course_code="ECE 222",
            title="Assignment 3",
            due_at=NOW + timedelta(days=14),
            assessment_type=AssessmentType.ASSIGNMENT,
            inbound_material_ids=(material.id,),
            inbound_material_previews=(preview,),
        )
        expected_calls = ["create_upload", "send_upload", "create_page"]
        expected_page = "created-assessment-page"
    else:
        change = ProposedChange(
            field="attach_assessment_material",
            value="Attach the rubric to ECE 222 Assignment 2.",
            assessment_id=assessment_id,
            course_id=course_id,
            course_code="ECE 222",
            title="Assignment 2",
            due_at=NOW + timedelta(days=7),
            expected_title="Assignment 2",
            expected_last_edited_at=NOW,
            inbound_material_ids=(material.id,),
            inbound_material_previews=(preview,),
        )
        expected_calls = ["create_upload", "send_upload", "append_pdf"]
        expected_page = "existing-assessment-page"

    proposal_id = uuid4()
    proposal = CheckinProposal(
        proposal_id=proposal_id,
        confirmation_event=f"confirm {proposal_id}",
        changes=(change,),
        expires_at=NOW + timedelta(hours=24),
    )
    store.save_discord_checkin(
        proposal,
        external_event_id=f"seed-{target}-event",
        channel=CHANNEL,
        received_at=NOW,
        owner_discord_user_id=OWNER,
    )
    notion = _NotionBoundary()
    syncer = _SyncBoundary()
    writer = DiscoveredAcademicNotionWriter(
        connector=cast(Any, notion),
        target_store=store,
        artifact_loader=artifacts,
        post_seed_syncer=syncer,
    )

    refused = await confirm_checkin_proposal(
        store=store,
        writer=writer,
        proposal_id=proposal_id,
        confirmation_event=f"confirm {uuid4()}",
        now=NOW,
    )
    assert refused["status"] == "confirmation_required"
    assert notion.calls == []

    applied = await confirm_checkin_proposal(
        store=store,
        writer=writer,
        proposal_id=proposal_id,
        confirmation_event=f"confirm {proposal_id}",
        now=NOW,
    )
    assert applied["status"] == "applied"
    assert notion.calls == expected_calls
    assert syncer.calls == 1
    assert writer.last_material_indexing_status == "queued"

    replay = await confirm_checkin_proposal(
        store=store,
        writer=writer,
        proposal_id=proposal_id,
        confirmation_event=f"confirm {proposal_id}",
        now=NOW,
    )
    assert replay["status"] == "applied"
    assert notion.calls == expected_calls
    assert syncer.calls == 1

    with Session(engine) as session:
        stored_material = session.get(AcademicInboundMaterial, material.id)
        stored_proposal = session.scalar(
            select(AcademicProposedChange).where(
                AcademicProposedChange.idempotency_key == f"academic-proposal:{proposal_id}"
            )
        )
        assert stored_material is not None
        assert stored_material.state == "seeded"
        assert stored_material.notion_page_id == expected_page
        assert stored_material.notion_upload_id == "upload-1"
        assert stored_material.notion_block_id == (None if target == "new" else "pdf-block-1")
        assert stored_proposal is not None
        assert stored_proposal.state == "applied"

    engine.dispose()
