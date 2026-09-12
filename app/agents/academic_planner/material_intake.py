"""Helpers for captured Discord PDF material before Notion seeding."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy.orm import Session

from app.agents.academic_planner.documents import chunk_document, extract_document
from app.artifacts.store import ArtifactStore
from app.db.academic import (
    AcademicInboundMaterialRepository,
    AcademicInboundMaterialSnapshot,
    SQLAlchemyAcademicPlannerStore,
)
from app.db.models import AcademicProposedChange


class InboundMaterialArtifactError(ValueError):
    """Raised when a captured material artifact is missing or no longer trusted."""


@dataclass(frozen=True, slots=True)
class VerifiedInboundMaterial:
    """A hash-verified PDF payload loaded from the private artifact store."""

    material: AcademicInboundMaterialSnapshot
    content: bytes

    @property
    def content_hash(self) -> str:
        return self.material.content_hash

    @property
    def observed_byte_size(self) -> int:
        return self.material.observed_byte_size


@dataclass(frozen=True, slots=True)
class InboundPdfPreview:
    """Small cited preview from untrusted inbound document content."""

    page: int | None
    citation: str
    text: str

    def as_dict(self) -> dict[str, object]:
        return {"page": self.page, "citation": self.citation, "text": self.text}


@dataclass(frozen=True, slots=True)
class InboundPdfInspection:
    """Bounded inspection result for one owner-scoped inbound PDF."""

    inbound_material_id: UUID
    filename: str
    extraction_status: str
    page_count: int
    preview: tuple[InboundPdfPreview, ...] = ()
    headings: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "inbound_material_id": str(self.inbound_material_id),
            "filename": self.filename,
            "extraction_status": self.extraction_status,
            "page_count": self.page_count,
            "preview": [item.as_dict() for item in self.preview],
            "headings": list(self.headings),
            "diagnostics": list(self.diagnostics),
        }


@dataclass(frozen=True, slots=True)
class PendingAssessmentCreateOption:
    """Safe owner-scoped pending create proposal summary."""

    proposal_id: UUID
    course_id: str | None
    course_code: str | None
    title: str | None
    due_at: str | None
    assessment_type: str | None
    preview: str

    def as_dict(self) -> dict[str, object]:
        return {
            "proposal_id": str(self.proposal_id),
            "course_id": self.course_id,
            "course_code": self.course_code,
            "title": self.title,
            "due_at": self.due_at,
            "assessment_type": self.assessment_type,
            "preview": self.preview,
        }


@dataclass(frozen=True, slots=True)
class PendingInboundMaterialOption:
    """Safe summary of one recent unresolved owner-scoped PDF intake."""

    inbound_material_id: UUID
    filename: str
    state: str

    def as_dict(self) -> dict[str, object]:
        return {
            "inbound_material_id": str(self.inbound_material_id),
            "filename": self.filename,
            "state": self.state,
        }


class AcademicMaterialIntakeService:
    """Harness-friendly intake operations backed by durable rows and artifacts."""

    def __init__(
        self,
        *,
        store: SQLAlchemyAcademicPlannerStore,
        artifact_store: ArtifactStore,
        max_bytes: int,
        max_pages: int = 15,
        preview_chars: int = 1_200,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if max_pages < 1 or max_pages > 15:
            raise ValueError("max_pages must be between 1 and 15")
        if preview_chars < 200 or preview_chars > 4_000:
            raise ValueError("preview_chars must be between 200 and 4000")
        self._store = store
        self._artifact_store = artifact_store
        self._max_bytes = max_bytes
        self._max_pages = max_pages
        self._preview_chars = preview_chars

    def inspect_inbound_pdf(
        self,
        inbound_material_id: UUID,
        *,
        owner_discord_user_id: str,
        discord_channel_id: str,
    ) -> InboundPdfInspection:
        material = self._store.get_inbound_material(
            inbound_material_id,
            owner_discord_user_id=owner_discord_user_id,
            discord_channel_id=discord_channel_id,
        )
        if material is None:
            raise ValueError("inbound material was not found for owner/channel")
        try:
            verified = load_verified_inbound_pdf(self._artifact_store, material)
            extracted = extract_document(
                verified.content,
                document_id=str(material.id),
                title=material.filename,
                media_type=material.media_type or "application/pdf",
                document_version=material.content_hash,
                max_bytes=self._max_bytes,
                max_pages=self._max_pages,
            )
        except Exception as exc:
            return InboundPdfInspection(
                inbound_material_id=material.id,
                filename=material.filename,
                extraction_status="failed",
                page_count=0,
                diagnostics=(_safe_diagnostic(exc),),
            )

        headings = tuple(
            dict.fromkeys(
                chunk.heading.strip()
                for chunk in chunk_document(extracted)
                if chunk.heading is not None and chunk.heading.strip()
            )
        )[:20]
        return InboundPdfInspection(
            inbound_material_id=material.id,
            filename=material.filename,
            extraction_status=extracted.extraction_status,
            page_count=len(extracted.pages),
            preview=_page_previews(extracted.pages, self._preview_chars),
            headings=headings,
            diagnostics=tuple(extracted.diagnostics),
        )

    def validate_for_proposal(
        self,
        ids: tuple[UUID, ...],
        *,
        owner_discord_user_id: str,
        discord_channel_id: str,
        now: datetime | None = None,
    ) -> tuple[UUID, ...]:
        return self._store.validate_for_proposal(
            ids,
            owner_discord_user_id=owner_discord_user_id,
            discord_channel_id=discord_channel_id,
            now=now,
        )

    def get_inbound_material(
        self,
        inbound_material_id: UUID,
        *,
        owner_discord_user_id: str,
        discord_channel_id: str,
    ) -> AcademicInboundMaterialSnapshot | None:
        return self._store.get_inbound_material(
            inbound_material_id,
            owner_discord_user_id=owner_discord_user_id,
            discord_channel_id=discord_channel_id,
        )

    def mark_awaiting_target(
        self,
        ids: tuple[UUID, ...],
        *,
        owner_discord_user_id: str,
        discord_channel_id: str,
        now: datetime | None = None,
    ) -> tuple[UUID, ...]:
        if not ids or len(ids) > 5 or len(ids) != len(set(ids)):
            raise ValueError("one to five unique inbound material ids are required")
        with Session(self._store.engine) as session, session.begin():
            for material_id in ids:
                AcademicInboundMaterialRepository.advance_state(
                    session,
                    material_id,
                    owner_discord_user_id=owner_discord_user_id,
                    discord_channel_id=discord_channel_id,
                    state="awaiting_target",
                    now=now,
                )
        return ids

    def find_recent_unresolved(
        self,
        *,
        owner_discord_user_id: str,
        discord_channel_id: str,
        now: datetime | None = None,
        limit: int = 5,
    ) -> tuple[PendingInboundMaterialOption, ...]:
        bounded_limit = max(1, min(limit, 5))
        with Session(self._store.engine) as session, session.begin():
            rows = AcademicInboundMaterialRepository.find_recent_pending(
                session,
                owner_discord_user_id=owner_discord_user_id,
                discord_channel_id=discord_channel_id,
                now=now,
                limit=bounded_limit,
                states={"captured", "awaiting_target"},
            )
            return tuple(
                PendingInboundMaterialOption(
                    inbound_material_id=row.id,
                    filename=row.filename,
                    state=row.state,
                )
                for row in rows
            )

    def search_pending_assessment_creates(
        self,
        query: str,
        *,
        owner_discord_user_id: str,
        discord_channel_id: str,
        now: datetime | None = None,
    ) -> tuple[PendingAssessmentCreateOption, ...]:
        with Session(self._store.engine) as session:
            rows = AcademicInboundMaterialRepository.find_pending_create_proposals(
                session,
                owner_discord_user_id=owner_discord_user_id,
                discord_channel_id=discord_channel_id,
                now=now,
                limit=10,
            )
        options: list[PendingAssessmentCreateOption] = []
        for row in rows:
            public_id = _public_proposal_id(row)
            change = _creation_change(row)
            if public_id is None or change is None:
                continue
            option = PendingAssessmentCreateOption(
                proposal_id=public_id,
                course_id=_string_field(change, "course_id"),
                course_code=_string_field(change, "course_code"),
                title=_string_field(change, "title"),
                due_at=_iso_time_field(change, "due_at"),
                assessment_type=_string_field(change, "assessment_type"),
                preview=row.redacted_preview[:500],
            )
            if _matches_query(query, option):
                options.append(option)
        return tuple(options[:10])


def load_verified_inbound_pdf(
    artifact_store: ArtifactStore,
    material: AcademicInboundMaterialSnapshot,
) -> VerifiedInboundMaterial:
    """Load a captured PDF and verify the durable metadata before external upload."""

    content = artifact_store.get(material.raw_artifact_key)
    observed_size = len(content)
    if observed_size != material.observed_byte_size:
        raise InboundMaterialArtifactError("inbound material artifact size changed")
    if hashlib.sha256(content).hexdigest() != material.content_hash:
        raise InboundMaterialArtifactError("inbound material artifact hash mismatch")
    if not content.startswith(b"%PDF-"):
        raise InboundMaterialArtifactError("inbound material artifact is not a PDF")
    return VerifiedInboundMaterial(material=material, content=content)


def _page_previews(pages: object, limit: int) -> tuple[InboundPdfPreview, ...]:
    remaining = limit
    previews: list[InboundPdfPreview] = []
    for page in cast(Iterable[object], pages):
        text = " ".join(str(getattr(page, "text", "")).split())
        if not text:
            continue
        snippet = text[:remaining]
        citation = getattr(page, "citation", None)
        locator = getattr(citation, "locator", "")
        page_number = getattr(page, "page", None)
        previews.append(
            InboundPdfPreview(
                page=page_number if isinstance(page_number, int) else None,
                citation=str(locator),
                text=snippet,
            )
        )
        remaining -= len(snippet)
        if remaining <= 0 or len(previews) >= 3:
            break
    return tuple(previews)


def _safe_diagnostic(exc: Exception) -> str:
    if isinstance(exc, InboundMaterialArtifactError):
        return str(exc)[:128]
    return exc.__class__.__name__[:128]


def _public_proposal_id(row: AcademicProposedChange) -> UUID | None:
    prefix = "academic-proposal:"
    if not row.idempotency_key.startswith(prefix):
        return None
    try:
        return UUID(row.idempotency_key.removeprefix(prefix))
    except ValueError:
        return None


def _creation_change(row: AcademicProposedChange) -> dict[str, Any] | None:
    payload: Mapping[str, Any] = row.payload
    changes_raw = payload.get("changes")
    if not isinstance(changes_raw, list):
        return None
    changes = cast(list[object], changes_raw)
    if len(changes) != 1:
        return None
    change = changes[0]
    if not isinstance(change, Mapping):
        return None
    change_mapping = cast(Mapping[str, Any], change)
    if change_mapping.get("field") != "create_assessment":
        return None
    return dict(change_mapping)


def _string_field(value: dict[str, Any], key: str) -> str | None:
    field = value.get(key)
    if isinstance(field, str) and field.strip():
        return field.strip()[:255]
    return None


def _iso_time_field(value: dict[str, Any], key: str) -> str | None:
    field = value.get(key)
    if isinstance(field, datetime):
        return field.isoformat()
    if isinstance(field, str) and field.strip():
        return field.strip()[:64]
    return None


def _matches_query(query: str, option: PendingAssessmentCreateOption) -> bool:
    normalized = " ".join(query.lower().split())
    if not normalized:
        return True
    haystack = " ".join(
        value.lower()
        for value in (
            option.course_id,
            option.course_code,
            option.title,
            option.due_at,
            option.assessment_type,
            option.preview,
        )
        if value
    )
    return normalized in haystack
