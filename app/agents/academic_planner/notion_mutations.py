"""Confirmed Notion mutations resolved from synchronized academic targets."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, cast
from uuid import UUID

from app.agents.academic_planner.contracts import ProposedChange
from app.connectors.notion import (
    MAX_NOTION_DIRECT_UPLOAD_BYTES,
    NotionConnector,
    NotionUploadedPdf,
    NotionWriteReceipt,
)
from app.core.errors import ErrorCode, LifeAgentError, permanent_error, transient_error
from app.db.academic import (
    AcademicAssessmentMutationTarget,
    AcademicCourseMutationTarget,
)

_MATERIAL_AVAILABLE_STATES = frozenset(("captured", "awaiting_target", "proposal_pending"))


class AcademicMutationTargetStore(Protocol):
    def resolve_course_mutation_target(
        self, course_id: str
    ) -> AcademicCourseMutationTarget | None: ...

    def resolve_assessment_mutation_target(
        self, assessment_id: str
    ) -> AcademicAssessmentMutationTarget | None: ...

    def begin_proposal_operation(
        self,
        *,
        proposal_id: UUID,
        ordinal: int,
        payload_hash: str,
        operation_id: str,
    ) -> tuple[Literal["ready", "already_applied", "in_progress", "uncertain", "failed"], Any]: ...

    def mark_proposal_operation_applied(
        self,
        *,
        proposal_id: UUID,
        ordinal: int,
        payload_hash: str,
        receipt: dict[str, Any],
    ) -> Any: ...

    def mark_proposal_operation_uncertain(
        self,
        *,
        proposal_id: UUID,
        ordinal: int,
        payload_hash: str,
        error_code: str,
    ) -> Any: ...

    def load_inbound_material_snapshot(
        self,
        *,
        material_id: UUID,
        proposal_id: UUID,
    ) -> AcademicInboundMaterialSnapshot | None: ...

    def mark_inbound_material_seeding(
        self,
        material_id: UUID,
        *,
        proposal_id: UUID,
        assessment_id: str | None = None,
        notion_upload_id: str | None = None,
    ) -> Any: ...

    def mark_inbound_material_seeded(
        self,
        material_id: UUID,
        *,
        proposal_id: UUID,
        assessment_id: str | None = None,
        notion_page_id: str,
        notion_block_id: str | None = None,
        notion_upload_id: str,
    ) -> Any: ...

    def mark_inbound_material_uncertain(
        self,
        material_id: UUID,
        *,
        proposal_id: UUID,
        assessment_id: str | None = None,
        error_code: str,
    ) -> Any: ...

    def mark_inbound_material_failed(
        self,
        material_id: UUID,
        *,
        proposal_id: UUID,
        error_code: str,
    ) -> Any: ...


class AcademicInboundMaterialSnapshot(Protocol):
    @property
    def id(self) -> UUID: ...

    @property
    def filename(self) -> str: ...

    @property
    def media_type(self) -> str | None: ...

    @property
    def observed_byte_size(self) -> int: ...

    @property
    def content_hash(self) -> str: ...

    @property
    def raw_artifact_key(self) -> str: ...

    @property
    def state(self) -> str: ...

    @property
    def proposal_id(self) -> UUID | None: ...


class AcademicMaterialArtifactLoader(Protocol):
    def get(self, key: str) -> bytes: ...


class AcademicPostSeedSyncer(Protocol):
    async def sync(self, *, now: datetime | None = None) -> Any: ...


@dataclass(frozen=True, slots=True)
class _VerifiedInboundMaterial:
    snapshot: AcademicInboundMaterialSnapshot
    content: bytes

    @property
    def id(self) -> UUID:
        return self.snapshot.id

    @property
    def filename(self) -> str:
        return self.snapshot.filename


@dataclass(frozen=True, slots=True)
class NotionMutationReview:
    """Deterministic proof that one ordered proposal batch was reviewed."""

    proposal_id: UUID
    confirmation_event: str
    batch_hash: str


def review_notion_mutation_batch(
    changes: Sequence[ProposedChange],
    *,
    proposal_id: UUID,
    confirmation_event: str,
) -> NotionMutationReview:
    """Create the HITL review token for the exact ordered Notion mutation batch."""

    _validate_confirmation(proposal_id, confirmation_event)
    return NotionMutationReview(
        proposal_id=proposal_id,
        confirmation_event=confirmation_event,
        batch_hash=_batch_hash(proposal_id, confirmation_event, changes),
    )


class DiscoveredAcademicNotionWriter:
    """Apply confirmed create/update/archive calls to discovered course calendars."""

    def __init__(
        self,
        *,
        connector: NotionConnector,
        target_store: AcademicMutationTargetStore,
        artifact_loader: AcademicMaterialArtifactLoader | None = None,
        post_seed_syncer: AcademicPostSeedSyncer | None = None,
    ) -> None:
        self._connector = connector
        self._target_store = target_store
        self._artifact_loader = artifact_loader
        self._post_seed_syncer = post_seed_syncer
        self.last_material_indexing_status: Literal["queued", "delayed"] | None = None

    async def apply_confirmed_changes(
        self,
        changes: Sequence[ProposedChange],
        *,
        proposal_id: UUID,
        confirmation_event: str,
        review: NotionMutationReview | None = None,
    ) -> None:
        _validate_confirmation(proposal_id, confirmation_event)
        self.last_material_indexing_status = None
        if _requires_hitl_review(changes):
            _validate_review(
                review,
                changes,
                proposal_id=proposal_id,
                confirmation_event=confirmation_event,
            )
        for index, change in enumerate(changes):
            materials: tuple[_VerifiedInboundMaterial, ...] = ()
            operation_id = f"{proposal_id}:{index}"
            payload_hash = _payload_hash(change)
            status, _ = self._target_store.begin_proposal_operation(
                proposal_id=proposal_id,
                ordinal=index,
                payload_hash=payload_hash,
                operation_id=operation_id,
            )
            if status == "already_applied":
                continue
            if status != "ready":
                raise permanent_error(
                    ErrorCode.INPUT_INVALID,
                    "proposal operation is already in progress or uncertain",
                )
            try:
                materials = self._load_materials_for_change(change, proposal_id=proposal_id)
                if change.field == "create_assessment":
                    receipt = await self._create(change, operation_id, materials=materials)
                elif change.field == "attach_assessment_material":
                    receipt = await self._attach(change, operation_id, materials=materials)
                elif change.field == "update_assessment":
                    receipt = await self._update(change, operation_id)
                elif change.field == "archive_assessment":
                    receipt = await self._archive(change, operation_id)
                else:
                    raise permanent_error(
                        ErrorCode.INPUT_INVALID,
                        "planner change is not an allowlisted discovered-calendar write",
                    )
            except Exception as exc:
                self._mark_materials_uncertain(change, materials, proposal_id, _error_code(exc))
                self._target_store.mark_proposal_operation_uncertain(
                    proposal_id=proposal_id,
                    ordinal=index,
                    payload_hash=payload_hash,
                    error_code=_error_code(exc),
                )
                raise
            self._target_store.mark_proposal_operation_applied(
                proposal_id=proposal_id,
                ordinal=index,
                payload_hash=payload_hash,
                receipt=_receipt_payload(receipt),
            )
            if receipt.file_upload_ids:
                await self._post_seed_sync()

    async def _post_seed_sync(self) -> None:
        if self._post_seed_syncer is None:
            self.last_material_indexing_status = "delayed"
            return
        try:
            result = await self._post_seed_syncer.sync(now=datetime.now(UTC))
            self.last_material_indexing_status = (
                "queued"
                if str(getattr(result, "status", "")) in {"succeeded", "partial"}
                and int(getattr(result, "material_job_count", 0)) > 0
                else "delayed"
            )
        except Exception:
            # The durable Notion mutation and operation receipt already succeeded.
            self.last_material_indexing_status = "delayed"

    async def _create(
        self,
        change: ProposedChange,
        operation_id: str,
        *,
        materials: Sequence[_VerifiedInboundMaterial] = (),
    ) -> NotionWriteReceipt:
        if change.course_id is None or change.title is None or change.due_at is None:
            raise permanent_error(ErrorCode.INPUT_INVALID, "assessment create is incomplete")
        target = self._target_store.resolve_course_mutation_target(change.course_id)
        if target is None:
            raise permanent_error(
                ErrorCode.INPUT_INVALID, "course calendar target is not allowlisted"
            )
        uploaded_pdfs = await self._send_material_uploads(
            materials,
            proposal_id=operation_id,
            assessment_id=None,
        )
        receipt = await self._connector.create_assessment_page(
            proposal_id=operation_id,
            data_source_id=target.data_source_id,
            title_property_id=target.title_property_id,
            date_property_id=target.date_property_id,
            title=change.title,
            due=change.due_at,
            ends_at=getattr(change, "ends_at", None),
            uploaded_pdfs=uploaded_pdfs,
        )
        self._mark_materials_seeded(
            materials,
            proposal_id=operation_id,
            assessment_id=None,
            receipt=receipt,
        )
        return receipt

    async def _attach(
        self,
        change: ProposedChange,
        operation_id: str,
        *,
        materials: Sequence[_VerifiedInboundMaterial],
    ) -> NotionWriteReceipt:
        if not materials:
            raise permanent_error(ErrorCode.INPUT_INVALID, "assessment material is required")
        target = self._assessment_target(change)
        uploaded_pdfs = await self._send_material_uploads(
            materials,
            proposal_id=operation_id,
            assessment_id=target.assessment_id,
        )
        receipt = await self._connector.append_uploaded_pdf_blocks(
            proposal_id=operation_id,
            page_id=target.page_id,
            title_property_id=target.title_property_id,
            expected_title=target.title,
            expected_last_edited_at=target.last_edited_at,
            uploaded_pdfs=uploaded_pdfs,
        )
        if len(receipt.block_ids) != len(materials):
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT,
                "Notion returned incomplete PDF block receipt",
            )
        self._mark_materials_seeded(
            materials,
            proposal_id=operation_id,
            assessment_id=target.assessment_id,
            receipt=receipt,
        )
        return receipt

    async def _send_material_uploads(
        self,
        materials: Sequence[_VerifiedInboundMaterial],
        *,
        proposal_id: str,
        assessment_id: str | None,
    ) -> tuple[NotionUploadedPdf, ...]:
        uploaded: list[NotionUploadedPdf] = []
        proposal_uuid = _proposal_uuid_from_operation(proposal_id)
        for material in materials:
            self._target_store.mark_inbound_material_seeding(
                material.id,
                proposal_id=proposal_uuid,
                assessment_id=assessment_id,
            )
            created = await self._connector.create_pdf_file_upload(filename=material.filename)
            self._target_store.mark_inbound_material_seeding(
                material.id,
                proposal_id=proposal_uuid,
                assessment_id=assessment_id,
                notion_upload_id=created.file_upload_id,
            )
            sent = await self._connector.send_pdf_file_upload(
                file_upload_id=created.file_upload_id,
                filename=material.filename,
                content=material.content,
            )
            self._target_store.mark_inbound_material_seeding(
                material.id,
                proposal_id=proposal_uuid,
                assessment_id=assessment_id,
                notion_upload_id=sent.file_upload_id,
            )
            uploaded.append(
                NotionUploadedPdf(file_upload_id=sent.file_upload_id, filename=material.filename)
            )
        return tuple(uploaded)

    async def _update(self, change: ProposedChange, operation_id: str) -> NotionWriteReceipt:
        target = self._assessment_target(change)
        if change.title is None and change.due_at is None:
            raise permanent_error(ErrorCode.INPUT_INVALID, "assessment update is incomplete")
        return await self._connector.guarded_update_assessment_page(
            proposal_id=operation_id,
            page_id=target.page_id,
            title_property_id=target.title_property_id,
            date_property_id=target.date_property_id,
            expected_title=target.title,
            expected_last_edited_at=target.last_edited_at,
            title=change.title,
            due=change.due_at,
        )

    async def _archive(self, change: ProposedChange, operation_id: str) -> NotionWriteReceipt:
        target = self._assessment_target(change)
        return await self._connector.guarded_archive_assessment_page(
            proposal_id=operation_id,
            page_id=target.page_id,
            title_property_id=target.title_property_id,
            expected_title=target.title,
            expected_last_edited_at=target.last_edited_at,
        )

    def _load_materials_for_change(
        self,
        change: ProposedChange,
        *,
        proposal_id: UUID,
    ) -> tuple[_VerifiedInboundMaterial, ...]:
        material_ids = cast(tuple[UUID, ...], getattr(change, "inbound_material_ids", ()))
        if not material_ids:
            return ()
        if change.field not in {"create_assessment", "attach_assessment_material"}:
            raise permanent_error(
                ErrorCode.INPUT_INVALID, "assessment material is not valid for this mutation"
            )
        if len(material_ids) != len(set(material_ids)):
            raise permanent_error(
                ErrorCode.INPUT_INVALID, "duplicate assessment material requested"
            )
        if self._artifact_loader is None:
            raise permanent_error(ErrorCode.SOURCE_SETUP_REQUIRED, "artifact loader is required")

        materials: list[_VerifiedInboundMaterial] = []
        for material_id in material_ids:
            snapshot = self._target_store.load_inbound_material_snapshot(
                material_id=material_id,
                proposal_id=proposal_id,
            )
            if snapshot is None:
                raise permanent_error(
                    ErrorCode.INPUT_INVALID,
                    "assessment material is not authorized or available",
                )
            try:
                content = self._artifact_loader.get(snapshot.raw_artifact_key)
            except Exception:
                self._mark_material_failed(snapshot.id, proposal_id)
                raise permanent_error(
                    ErrorCode.INPUT_INVALID,
                    "assessment material artifact is unavailable",
                ) from None
            self._validate_material_snapshot(snapshot, content, proposal_id=proposal_id)
            materials.append(_VerifiedInboundMaterial(snapshot=snapshot, content=content))
        return tuple(materials)

    def _validate_material_snapshot(
        self,
        snapshot: AcademicInboundMaterialSnapshot,
        content: bytes,
        *,
        proposal_id: UUID,
    ) -> None:
        def fail() -> None:
            self._mark_material_failed(snapshot.id, proposal_id)
            raise permanent_error(ErrorCode.INPUT_INVALID, "assessment material is invalid")

        if snapshot.state not in _MATERIAL_AVAILABLE_STATES:
            fail()
        if snapshot.media_type not in (None, "application/pdf"):
            fail()
        if not snapshot.filename.casefold().endswith(".pdf"):
            fail()
        if (
            snapshot.observed_byte_size <= 0
            or snapshot.observed_byte_size > MAX_NOTION_DIRECT_UPLOAD_BYTES
            or len(content) != snapshot.observed_byte_size
            or len(content) > MAX_NOTION_DIRECT_UPLOAD_BYTES
            or not content.startswith(b"%PDF-")
            or hashlib.sha256(content).hexdigest() != snapshot.content_hash
        ):
            fail()

    def _mark_materials_seeded(
        self,
        materials: Sequence[_VerifiedInboundMaterial],
        *,
        proposal_id: str,
        assessment_id: str | None,
        receipt: NotionWriteReceipt,
    ) -> None:
        proposal_uuid = _proposal_uuid_from_operation(proposal_id)
        for index, material in enumerate(materials):
            block_id = receipt.block_ids[index] if index < len(receipt.block_ids) else None
            upload_id = (
                receipt.file_upload_ids[index] if index < len(receipt.file_upload_ids) else None
            )
            if upload_id is None:
                raise transient_error(
                    ErrorCode.CONNECTOR_TRANSIENT,
                    "Notion returned incomplete file upload receipt",
                )
            self._target_store.mark_inbound_material_seeded(
                material.id,
                proposal_id=proposal_uuid,
                assessment_id=assessment_id,
                notion_page_id=receipt.page_id,
                notion_block_id=block_id,
                notion_upload_id=upload_id,
            )

    def _mark_materials_uncertain(
        self,
        change: ProposedChange,
        materials: Sequence[_VerifiedInboundMaterial],
        proposal_id: UUID,
        error_code: str,
    ) -> None:
        if not materials:
            return
        assessment_id = (
            change.assessment_id if change.field == "attach_assessment_material" else None
        )
        for material in materials:
            self._target_store.mark_inbound_material_uncertain(
                material.id,
                proposal_id=proposal_id,
                assessment_id=assessment_id,
                error_code=error_code,
            )

    def _mark_material_failed(self, material_id: UUID, proposal_id: UUID) -> None:
        self._target_store.mark_inbound_material_failed(
            material_id,
            proposal_id=proposal_id,
            error_code=ErrorCode.INPUT_INVALID.value,
        )

    def _assessment_target(self, change: ProposedChange) -> AcademicAssessmentMutationTarget:
        if change.assessment_id is None:
            raise permanent_error(ErrorCode.INPUT_INVALID, "assessment target is required")
        target = self._target_store.resolve_assessment_mutation_target(change.assessment_id)
        if target is None:
            raise permanent_error(ErrorCode.INPUT_INVALID, "assessment target is not allowlisted")
        if (
            change.expected_title != target.title
            or change.expected_last_edited_at != target.last_edited_at
        ):
            raise permanent_error(
                ErrorCode.INPUT_INVALID, "assessment changed after the proposal was prepared"
            )
        return target


def _validate_confirmation(proposal_id: UUID, confirmation_event: str) -> None:
    if confirmation_event != f"confirm {proposal_id}":
        raise permanent_error(ErrorCode.INPUT_INVALID, "Notion confirmation is invalid")


def _proposal_uuid_from_operation(operation_id: str) -> UUID:
    return UUID(operation_id.split(":", 1)[0])


def _requires_hitl_review(changes: Sequence[ProposedChange]) -> bool:
    return any(
        getattr(change, "field", None)
        in {"create_assessment", "attach_assessment_material", "archive_assessment"}
        for change in changes
    )


def _validate_review(
    review: NotionMutationReview | None,
    changes: Sequence[ProposedChange],
    *,
    proposal_id: UUID,
    confirmation_event: str,
) -> None:
    if review is None:
        raise permanent_error(ErrorCode.INPUT_INVALID, "Notion HITL review is required")
    if (
        review.proposal_id != proposal_id
        or review.confirmation_event != confirmation_event
        or review.batch_hash != _batch_hash(proposal_id, confirmation_event, changes)
    ):
        raise permanent_error(ErrorCode.INPUT_INVALID, "Notion HITL review does not match proposal")


def _batch_hash(
    proposal_id: UUID,
    confirmation_event: str,
    changes: Sequence[ProposedChange],
) -> str:
    payload = {
        "proposal_id": str(proposal_id),
        "confirmation_event": confirmation_event,
        "changes": [_change_payload(change) for change in changes],
    }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _payload_hash(change: ProposedChange) -> str:
    encoded = json.dumps(
        _change_payload(change), ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _change_payload(change: ProposedChange) -> dict[str, Any]:
    if hasattr(change, "model_dump"):
        return change.model_dump(mode="json", exclude_none=True)
    return {
        key: _jsonable(value)
        for key, value in vars(change).items()
        if not key.startswith("_") and value is not None
    }


def _jsonable(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, str):
        return enum_value
    return value


def _receipt_payload(receipt: NotionWriteReceipt) -> dict[str, Any]:
    payload = receipt.model_dump(mode="json", exclude_none=True)
    if not payload.get("file_upload_ids"):
        payload.pop("file_upload_ids", None)
    if not payload.get("block_ids"):
        payload.pop("block_ids", None)
    return payload


def _error_code(exc: Exception) -> str:
    if isinstance(exc, LifeAgentError):
        return exc.record.code.value
    return ErrorCode.INTERNAL.value


__all__ = [
    "AcademicInboundMaterialSnapshot",
    "AcademicMaterialArtifactLoader",
    "AcademicMutationTargetStore",
    "AcademicPostSeedSyncer",
    "DiscoveredAcademicNotionWriter",
    "NotionMutationReview",
    "review_notion_mutation_batch",
]
