"""Private, replay-safe ingestion of Notion assessment material."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import fitz
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.agents.academic_planner.documents import (
    DocumentChunk,
    ExtractedDocument,
    chunk_document,
    extract_document,
    extract_notion_body_document,
)
from app.artifacts.store import ArtifactStore
from app.connectors.notion import (
    NotionAssessmentMaterials,
    NotionConnector,
    NotionMaterialFile,
)
from app.db.academic import AcademicRepository, DocumentChunkInput, SourceCitation
from app.db.models import AcademicDocument, Assessment

PRIVATE_MATERIAL_CLASS = "academic_material_private"
PRIVATE_EXTRACT_CLASS = "academic_material_extracted_private"


class MaterialEmbeddingGateway(Protocol):
    model_identity: str

    async def embed_academic_text(self, text: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class MaterialIngestionResult:
    assessment_page_id: str
    status: str
    discovered_count: int
    activated_count: int
    unchanged_count: int
    failed_count: int
    inactive_count: int
    diagnostic_codes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "assessment_page_id": self.assessment_page_id,
            "status": self.status,
            "discovered_count": self.discovered_count,
            "activated_count": self.activated_count,
            "unchanged_count": self.unchanged_count,
            "failed_count": self.failed_count,
            "inactive_count": self.inactive_count,
            "diagnostic_codes": list(self.diagnostic_codes),
        }


class AssessmentMaterialIngestionService:
    """Acquire, persist, extract, embed, and activate one assessment snapshot."""

    def __init__(
        self,
        *,
        engine: Engine,
        connector: NotionConnector,
        artifact_store: ArtifactStore,
        embedding_gateway: MaterialEmbeddingGateway,
        max_bytes: int,
        max_depth: int = 8,
        max_blocks: int = 1_000,
        max_requests: int = 20,
        pdf_max_pages: int = 15,
        ocr_timeout_seconds: float = 30.0,
        ocr_min_page_chars: int = 24,
    ) -> None:
        self._engine = engine
        self._connector = connector
        self._artifacts = artifact_store
        self._embeddings = embedding_gateway
        self._max_bytes = max_bytes
        self._max_depth = max_depth
        self._max_blocks = max_blocks
        self._max_requests = max_requests
        self._pdf_max_pages = pdf_max_pages
        self._ocr_timeout_seconds = ocr_timeout_seconds
        self._ocr_min_page_chars = ocr_min_page_chars

    async def ingest_assessment(
        self,
        assessment_page_id: str,
        *,
        source_fingerprint: str | None = None,
    ) -> MaterialIngestionResult:
        """Refresh the page and atomically activate only usable material versions."""

        del source_fingerprint  # queue deduplication input; never source content.
        materials = await self._connector.retrieve_assessment_materials(
            assessment_page_id,
            max_depth=self._max_depth,
            max_blocks=self._max_blocks,
            max_requests=self._max_requests,
        )
        assessment = self._assessment(materials.assessment_page_id)
        seen: set[str] = set()
        activated = 0
        unchanged = 0
        failed = 0
        diagnostics = [item.code for item in materials.diagnostics]

        if materials.text_blocks:
            body_key = _body_source_key(materials.assessment_page_id)
            seen.add(body_key)
            try:
                body = extract_notion_body_document(
                    document_id=body_key,
                    title="Notion assessment page body",
                    blocks=materials.text_blocks,
                )
                raw = json.dumps(
                    [
                        {
                            "block_id": block.source_block_id,
                            "order": block.order,
                            "text": block.text,
                        }
                        for block in materials.text_blocks
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                was_unchanged = await self._persist_usable(
                    assessment=assessment,
                    materials=materials,
                    source_key=body_key,
                    source_kind="notion_page_body",
                    source_block_id=None,
                    source_property_id=None,
                    filename=None,
                    raw_content=raw,
                    raw_media_type="application/json",
                    extracted=body,
                )
                unchanged += int(was_unchanged)
                activated += int(not was_unchanged)
            except Exception as exc:
                failed += 1
                diagnostics.append("notion_body_ingestion_failed")
                self._persist_failure(
                    assessment=assessment,
                    materials=materials,
                    source_key=body_key,
                    source_kind="notion_page_body",
                    error_code="notion_body_ingestion_failed",
                    error_detail=_safe_error(exc),
                )

        for material in materials.files:
            seen.add(material.source_key)
            try:
                refreshed = await self._connector.refresh_assessment_material_file(
                    assessment_page_id=assessment_page_id,
                    source_key=material.source_key,
                )
                content = await self._connector.download_attachment(
                    refreshed.as_attachment(), max_bytes=self._max_bytes
                )
                media_type = _supported_media_type(refreshed, content)
                ocr_deadline = time.monotonic() + self._ocr_timeout_seconds

                def bounded_ocr(
                    pdf: bytes,
                    page: int,
                    *,
                    deadline: float = ocr_deadline,
                ) -> str | None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    return tesseract_pdf_page(pdf, page, timeout_seconds=remaining)

                extracted = extract_document(
                    content,
                    document_id=refreshed.source_key,
                    title=refreshed.name,
                    media_type=media_type,
                    ocr_extractor=bounded_ocr,
                    max_bytes=self._max_bytes,
                    max_pages=self._pdf_max_pages,
                    min_page_chars=self._ocr_min_page_chars,
                    ocr_timeout_seconds=self._ocr_timeout_seconds,
                )
                if extracted.extraction_status not in {"extracted", "partial"}:
                    raise ValueError(f"material extraction was {extracted.extraction_status}")
                was_unchanged = await self._persist_usable(
                    assessment=assessment,
                    materials=materials,
                    source_key=refreshed.source_key,
                    source_kind=refreshed.source_kind,
                    source_block_id=refreshed.source_block_id,
                    source_property_id=refreshed.source_property_id,
                    filename=refreshed.name,
                    raw_content=content,
                    raw_media_type=media_type,
                    extracted=extracted,
                )
                unchanged += int(was_unchanged)
                activated += int(not was_unchanged)
            except Exception as exc:
                failed += 1
                code = _failure_code(exc)
                diagnostics.append(code)
                self._persist_failure(
                    assessment=assessment,
                    materials=materials,
                    source_key=material.source_key,
                    source_kind=material.source_kind,
                    source_block_id=material.source_block_id,
                    source_property_id=material.source_property_id,
                    filename=material.name,
                    error_code=code,
                    error_detail=_safe_error(exc),
                )

        inactive = self._reconcile(assessment.id, seen)
        failed += self._persist_connector_diagnostics(assessment, materials)
        discovered = int(bool(materials.text_blocks)) + len(materials.files)
        has_last_good = self._has_active_material(assessment.id)
        status = (
            "succeeded"
            if failed == 0
            else ("partial" if activated or unchanged or has_last_good else "failed")
        )
        return MaterialIngestionResult(
            assessment_page_id=assessment_page_id,
            status=status,
            discovered_count=discovered,
            activated_count=activated,
            unchanged_count=unchanged,
            failed_count=failed,
            inactive_count=inactive,
            diagnostic_codes=tuple(sorted(set(diagnostics)))[:100],
        )

    def _assessment(self, page_id: str) -> Assessment:
        with Session(self._engine) as session:
            row = session.scalar(select(Assessment).where(Assessment.notion_id == page_id))
            if row is None:
                raise ValueError("assessment page has not been synchronized")
            session.expunge(row)
            return row

    async def _persist_usable(
        self,
        *,
        assessment: Assessment,
        materials: NotionAssessmentMaterials,
        source_key: str,
        source_kind: str,
        source_block_id: str | None,
        source_property_id: str | None,
        filename: str | None,
        raw_content: bytes | str,
        raw_media_type: str,
        extracted: ExtractedDocument,
    ) -> bool:
        raw_artifact = self._artifacts.put(
            raw_content,
            media_type=raw_media_type,
            data_class=PRIVATE_MATERIAL_CLASS,
            already_redacted=True,
        )
        self._artifacts.put(
            extracted.text,
            media_type="text/plain",
            data_class=PRIVATE_EXTRACT_CLASS,
            already_redacted=True,
        )
        content_hash = hashlib.sha256(
            raw_content.encode("utf-8") if isinstance(raw_content, str) else raw_content
        ).hexdigest()
        chunks = chunk_document(extracted)
        if not chunks:
            raise ValueError("assessment material did not produce usable text chunks")
        embedded_chunks = await self._embed_chunks(chunks)
        with Session(self._engine) as session, session.begin():
            existing = session.scalar(
                select(AcademicDocument).where(
                    AcademicDocument.source_key == source_key,
                    AcademicDocument.document_version == content_hash,
                    AcademicDocument.active.is_(True),
                    AcademicDocument.extraction_status.in_(("extracted", "partial")),
                )
            )
            row = AcademicRepository.upsert_document(
                session,
                notion_id=materials.assessment_page_id,
                document_version=content_hash,
                title=extracted.title,
                document_type=extracted.media_type,
                retrieved_at=datetime.now(UTC),
                artifact_key=raw_artifact.key,
                content_hash=content_hash,
                course_id=assessment.course_id,
                assessment_id=assessment.id,
                source_kind=source_kind,
                source_page_id=materials.assessment_page_id,
                source_block_id=source_block_id,
                source_property_id=source_property_id,
                source_key=source_key,
                original_filename=filename,
                media_type=extracted.media_type,
                source_last_edited_at=materials.last_edited_at,
                access_classification="private",
                extraction_status=extracted.extraction_status,
                active=existing is not None,
            )
            AcademicRepository.replace_document_chunks(
                session,
                document_id=row.id,
                chunks=[
                    DocumentChunkInput(
                        ordinal=chunk.chunk_index,
                        content=chunk.content,
                        heading=chunk.heading,
                        token_count=len(chunk.content.split()),
                        content_hash=hashlib.sha256(chunk.content.encode("utf-8")).hexdigest(),
                        citation=SourceCitation(
                            page=chunk.page,
                            block=chunk.citation.block,
                        ),
                        embedding=chunk.embedding,
                        embedding_model=chunk.embedding_model,
                    )
                    for chunk in embedded_chunks
                ],
            )
            AcademicRepository.activate_document_version(session, document_id=row.id)
        return existing is not None

    async def _embed_chunks(self, chunks: Sequence[DocumentChunk]) -> tuple[DocumentChunk, ...]:
        result: list[DocumentChunk] = []
        for chunk in chunks:
            embedding = await self._embeddings.embed_academic_text(chunk.content)
            vector = getattr(getattr(embedding, "embedding", None), "vector", None)
            status = str(getattr(embedding, "status", ""))
            if vector is None or status not in {"valid", "EmbeddingStatus.VALID"}:
                raise RuntimeError("assessment material embedding failed")
            result.append(
                DocumentChunk(
                    document_id=chunk.document_id,
                    chunk_index=chunk.chunk_index,
                    content=chunk.content,
                    page=chunk.page,
                    heading=chunk.heading,
                    citation=chunk.citation,
                    course=chunk.course,
                    term=chunk.term,
                    access_classification=chunk.access_classification,
                    document_version=chunk.document_version,
                    embedding=tuple(float(item) for item in vector),
                    embedding_model=str(
                        getattr(embedding, "model_identity", self._embeddings.model_identity)
                    ),
                    embedding_dimensions=len(vector),
                )
            )
        return tuple(result)

    def _persist_failure(
        self,
        *,
        assessment: Assessment,
        materials: NotionAssessmentMaterials,
        source_key: str,
        source_kind: str,
        error_code: str,
        error_detail: str,
        source_block_id: str | None = None,
        source_property_id: str | None = None,
        filename: str | None = None,
    ) -> None:
        diagnostic = json.dumps(
            {"error_code": error_code, "detail": error_detail},
            sort_keys=True,
            separators=(",", ":"),
        )
        artifact = self._artifacts.put(
            diagnostic,
            media_type="application/json",
            data_class=PRIVATE_EXTRACT_CLASS,
            already_redacted=True,
        )
        digest = hashlib.sha256(
            f"{source_key}\0{materials.last_edited_at.isoformat()}\0{error_code}".encode()
        ).hexdigest()
        with Session(self._engine) as session, session.begin():
            AcademicRepository.upsert_document(
                session,
                notion_id=materials.assessment_page_id,
                document_version=f"failed-{digest[:32]}",
                title="Assessment material diagnostic",
                document_type="diagnostic",
                retrieved_at=datetime.now(UTC),
                artifact_key=artifact.key,
                content_hash=hashlib.sha256(diagnostic.encode()).hexdigest(),
                course_id=assessment.course_id,
                assessment_id=assessment.id,
                source_kind=source_kind,
                source_page_id=materials.assessment_page_id,
                source_block_id=source_block_id,
                source_property_id=source_property_id,
                source_key=source_key,
                original_filename=filename,
                source_last_edited_at=materials.last_edited_at,
                extraction_status="failed",
                active=False,
                extraction_error_code=error_code,
                extraction_error_detail=error_detail,
            )

    def _persist_connector_diagnostics(
        self, assessment: Assessment, materials: NotionAssessmentMaterials
    ) -> int:
        count = 0
        for diagnostic in materials.diagnostics:
            if diagnostic.source_key is None:
                continue
            self._persist_failure(
                assessment=assessment,
                materials=materials,
                source_key=diagnostic.source_key,
                source_kind=(
                    "notion_block_file"
                    if diagnostic.source_block_id is not None
                    else "notion_property_file"
                ),
                source_block_id=diagnostic.source_block_id,
                source_property_id=diagnostic.source_property_id,
                error_code=diagnostic.code,
                error_detail=diagnostic.message,
            )
            count += 1
        return count

    def _reconcile(self, assessment_id: Any, seen: set[str]) -> int:
        with Session(self._engine) as session, session.begin():
            current = AcademicRepository.list_assessment_materials(
                session, assessment_id=assessment_id, active_only=True, limit=100
            )
            removed = {
                row.source_key
                for row in current
                if row.source_key is not None and row.source_key not in seen
            }
            return sum(
                AcademicRepository.mark_document_source_inactive(
                    session,
                    source_key=source_key,
                    error_code="material_source_removed",
                    error_detail="Material is no longer present on the Notion assessment page.",
                )
                for source_key in removed
            )

    def _has_active_material(self, assessment_id: Any) -> bool:
        with Session(self._engine) as session:
            return bool(
                AcademicRepository.list_assessment_materials(
                    session, assessment_id=assessment_id, active_only=True, limit=1
                )
            )


def tesseract_pdf_page(content: bytes, page_number: int, *, timeout_seconds: float) -> str | None:
    """OCR one already-bounded PDF page with the local Tesseract executable."""

    document: Any = fitz.open(stream=content, filetype="pdf")
    try:
        executable = shutil.which("tesseract")
        if executable is None:
            return None
        page: Any = document[page_number - 1]
        pixmap: Any = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        with tempfile.TemporaryDirectory(prefix="lifeagent-ocr-") as directory:
            image_path = Path(directory) / "page.png"
            pixmap.save(str(image_path))
            completed = subprocess.run(  # noqa: S603
                [executable, str(image_path), "stdout", "--psm", "6"],
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
            if completed.returncode != 0:
                return None
            return completed.stdout.decode("utf-8", errors="replace").strip() or None
    except (FileNotFoundError, subprocess.TimeoutExpired, IndexError):
        return None
    finally:
        document.close()


def assessment_material_fingerprint(page_id: str, last_edited_at: datetime) -> str:
    """Build an identifier-only queue fingerprint from stable page metadata."""

    return hashlib.sha256(f"{page_id}\0{last_edited_at.isoformat()}".encode()).hexdigest()


async def run_assessment_material_ingestion(
    assessment_page_id: str,
    source_fingerprint: str,
) -> dict[str, object]:
    """Worker entry point built entirely from identifier-only job arguments."""

    if len(source_fingerprint) != 64 or any(
        character not in "0123456789abcdef" for character in source_fingerprint
    ):
        raise ValueError("assessment material fingerprint must be SHA-256 hex")
    from app.connectors.notion import NotionConnector
    from app.core.config import get_settings
    from app.db.session import Database
    from app.llm.embeddings import AcademicEmbeddingGateway

    settings = get_settings()
    if settings.notion_token is None or settings.notion_courses_database_id is None:
        raise RuntimeError("Notion assessment material ingestion is not configured")
    database = Database(settings)
    connector = NotionConnector(
        token=settings.notion_token,
        courses_database_id=settings.notion_courses_database_id,
        timeout_seconds=settings.connector_timeout_seconds,
    )
    service = AssessmentMaterialIngestionService(
        engine=database.engine,
        connector=connector,
        artifact_store=ArtifactStore(
            settings.artifact_root,
            default_retention_days=settings.artifact_retention_days,
        ),
        embedding_gateway=AcademicEmbeddingGateway(settings),
        max_bytes=settings.notion_attachment_max_bytes,
        max_depth=settings.notion_material_max_block_depth,
        max_blocks=settings.notion_material_max_blocks,
        max_requests=settings.notion_material_max_cursor_pages,
        pdf_max_pages=settings.academic_material_pdf_max_pages,
        ocr_timeout_seconds=settings.academic_material_ocr_timeout_seconds,
        ocr_min_page_chars=settings.academic_material_ocr_min_page_chars,
    )
    try:
        result = await service.ingest_assessment(
            assessment_page_id,
            source_fingerprint=source_fingerprint,
        )
        return result.as_dict()
    finally:
        database.dispose()


def _body_source_key(page_id: str) -> str:
    return f"notion-page-body:{page_id}"


def _supported_media_type(material: NotionMaterialFile, content: bytes) -> str:
    if content.startswith(b"%PDF-"):
        return "application/pdf"
    media_type = (material.mime_type or "").casefold().split(";", 1)[0].strip()
    if media_type.startswith("text/"):
        return media_type
    raise ValueError("unsupported assessment material media type")


def _failure_code(exc: Exception) -> str:
    message = str(exc).casefold()
    if "too many pages" in message:
        return "pdf_page_limit_exceeded"
    if "unsupported" in message:
        return "unsupported_material"
    if "embedding" in message:
        return "material_embedding_failed"
    if "ocr" in message:
        return "material_ocr_failed"
    return "material_ingestion_failed"


def _safe_error(exc: Exception) -> str:
    message = str(exc).strip()
    if not message:
        return "Assessment material processing failed."
    # Vendor URLs and document text are never needed for an operational diagnostic.
    if "http://" in message or "https://" in message:
        return "Assessment material processing failed at the connector boundary."
    return message[:500]


__all__ = [
    "AssessmentMaterialIngestionService",
    "MaterialIngestionResult",
    "assessment_material_fingerprint",
    "run_assessment_material_ingestion",
    "tesseract_pdf_page",
]
