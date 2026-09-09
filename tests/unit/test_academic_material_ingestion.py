from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.agents.academic_planner.material_ingestion import (
    AssessmentMaterialIngestionService,
    assessment_material_fingerprint,
)
from app.artifacts.store import ArtifactStore
from app.connectors.notion import (
    NotionAssessmentMaterials,
    NotionMaterialFile,
    NotionMaterialTextBlock,
)
from app.db.academic import AcademicRepository, SourceCitation, SQLAlchemyAcademicPlannerStore
from app.db.models import AcademicDocument, AcademicDocumentChunk, Base

NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)


def _pdf(text: str) -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    content = document.tobytes()
    document.close()
    return content


class _EmbeddingGateway:
    model_identity = "fake-embedding:v1"

    async def embed_academic_text(self, text: str):
        seed = float((sum(text.encode()) % 7) + 1)
        return SimpleNamespace(
            status="valid",
            model_identity=self.model_identity,
            embedding=SimpleNamespace(vector=[seed, 1.0]),
        )


class _Connector:
    def __init__(self, snapshot: NotionAssessmentMaterials, content: bytes | None = None) -> None:
        self.snapshot = snapshot
        self.content = content
        self.download_error: Exception | None = None

    async def retrieve_assessment_materials(self, page_id: str, **kwargs):
        assert page_id == self.snapshot.assessment_page_id
        assert kwargs["max_depth"] == 8
        return self.snapshot

    async def refresh_assessment_material_file(self, *, assessment_page_id: str, source_key: str):
        assert assessment_page_id == self.snapshot.assessment_page_id
        return next(item for item in self.snapshot.files if item.source_key == source_key)

    async def download_attachment(self, attachment, *, max_bytes: int) -> bytes:
        assert max_bytes > 0
        if self.download_error is not None:
            raise self.download_error
        assert self.content is not None
        return self.content


@pytest.fixture
def material_runtime(tmp_path: Path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'materials.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        course = AcademicRepository.upsert_course(
            session,
            notion_id="course-page",
            course_code="ECE 222",
            title="Linear Circuits",
            term="2026F",
        )
        AcademicRepository.upsert_assessment(
            session,
            notion_id="assessment-page",
            course_id=course.id,
            title="Assignment 1",
            assessment_type="assignment",
            due_at=datetime(2026, 9, 15, tzinfo=UTC),
            grade_weight_percent=None,
            estimated_minutes=120,
            confidence=1.0,
            fact_state="confirmed",
            citation=SourceCitation(),
        )
    try:
        yield engine, ArtifactStore(tmp_path / "artifacts")
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_body_ingestion_is_private_cited_embedded_and_replay_safe(material_runtime) -> None:
    engine, artifacts = material_runtime
    snapshot = NotionAssessmentMaterials(
        assessment_page_id="assessment-page",
        last_edited_at=NOW,
        text_blocks=(
            NotionMaterialTextBlock(
                source_page_id="assessment-page",
                source_block_id="bullet-1",
                source_key="assessment-page:body:bullet-1",
                order=0,
                block_type="bulleted_list_item",
                text="10% quiz; focus on linear circuits and the distinction between AC and DC.",
            ),
        ),
    )
    service = AssessmentMaterialIngestionService(
        engine=engine,
        connector=_Connector(snapshot),  # type: ignore[arg-type]
        artifact_store=artifacts,
        embedding_gateway=_EmbeddingGateway(),
        max_bytes=1024 * 1024,
    )

    first = await service.ingest_assessment("assessment-page")
    second = await service.ingest_assessment("assessment-page")

    assert first.status == "succeeded"
    assert first.activated_count == 1
    assert second.unchanged_count == 1
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AcademicDocument)) == 1
        document = session.scalar(select(AcademicDocument))
        assert document is not None
        assert document.active is True
        assert document.source_kind == "notion_page_body"
        assert "http" not in (document.source_url or "")
        assert (
            artifacts.get_metadata(document.artifact_key).data_class == "academic_material_private"
        )
        chunk = session.scalar(select(AcademicDocumentChunk))
        assert chunk is not None
        assert chunk.source_block == "bullet-1"
        assert chunk.embedding_model == "fake-embedding:v1"
        assert chunk.embedding_dimensions == 2


@pytest.mark.asyncio
async def test_changed_pdf_failure_preserves_last_good_and_removed_source_deactivates(
    material_runtime,
) -> None:
    engine, artifacts = material_runtime
    file = NotionMaterialFile(
        source_kind="notion_property_file",
        source_page_id="assessment-page",
        source_property_id="files-property",
        source_key="assessment-page:property:files-property:file:0",
        order=0,
        name="rubric.pdf",
        url="https://prod-files-secure.s3.us-west-2.amazonaws.com/file.pdf?X-Amz-Signature=x",
        mime_type="application/pdf",
    )
    snapshot = NotionAssessmentMaterials(
        assessment_page_id="assessment-page",
        last_edited_at=NOW,
        files=(file,),
    )
    connector = _Connector(snapshot, _pdf("Testing and performance evidence are assessed."))
    service = AssessmentMaterialIngestionService(
        engine=engine,
        connector=connector,  # type: ignore[arg-type]
        artifact_store=artifacts,
        embedding_gateway=_EmbeddingGateway(),
        max_bytes=1024 * 1024,
    )

    assert (await service.ingest_assessment("assessment-page")).status == "succeeded"
    connector.snapshot = snapshot.model_copy(update={"last_edited_at": NOW.replace(hour=13)})
    connector.download_error = RuntimeError("expired attachment")
    failed = await service.ingest_assessment("assessment-page")
    assert failed.status == "partial"

    with Session(engine) as session:
        good = list(
            session.scalars(
                select(AcademicDocument).where(
                    AcademicDocument.source_key == file.source_key,
                    AcademicDocument.extraction_status == "extracted",
                )
            )
        )
        diagnostics = list(
            session.scalars(
                select(AcademicDocument).where(AcademicDocument.extraction_status == "failed")
            )
        )
        assert len(good) == 1
        assert good[0].active is True
        assert len(diagnostics) == 1
        assert diagnostics[0].active is False

    health = SQLAlchemyAcademicPlannerStore(engine).academic_notion_health()
    assert health["material_failed_count"] == 1
    assert "rubric.pdf" not in str(health)

    connector.snapshot = NotionAssessmentMaterials(
        assessment_page_id="assessment-page",
        last_edited_at=NOW.replace(hour=14),
    )
    removed = await service.ingest_assessment("assessment-page")
    assert removed.inactive_count == 1
    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count()).select_from(AcademicDocument).where(AcademicDocument.active)
            )
            == 0
        )


def test_queue_fingerprint_uses_only_stable_metadata() -> None:
    first = assessment_material_fingerprint("assessment-page", NOW)
    assert first == assessment_material_fingerprint("assessment-page", NOW)
    assert first != assessment_material_fingerprint("assessment-page", NOW.replace(hour=13))
    assert len(first) == 64
