"""Confirmed Notion mutations resolved from synchronized academic targets."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any, Literal, Protocol
from uuid import UUID

from app.agents.academic_planner.contracts import ProposedChange
from app.connectors.notion import NotionConnector, NotionWriteReceipt
from app.core.errors import ErrorCode, LifeAgentError, permanent_error
from app.db.academic import (
    AcademicAssessmentMutationTarget,
    AcademicCourseMutationTarget,
)


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


class DiscoveredAcademicNotionWriter:
    """Apply confirmed create/update/archive calls to discovered course calendars."""

    def __init__(
        self,
        *,
        connector: NotionConnector,
        target_store: AcademicMutationTargetStore,
    ) -> None:
        self._connector = connector
        self._target_store = target_store

    async def apply_confirmed_changes(
        self,
        changes: Sequence[ProposedChange],
        *,
        proposal_id: UUID,
        confirmation_event: str,
    ) -> None:
        if confirmation_event != f"confirm {proposal_id}":
            raise permanent_error(ErrorCode.INPUT_INVALID, "Notion confirmation is invalid")
        for index, change in enumerate(changes):
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
                if change.field == "create_assessment":
                    receipt = await self._create(change, operation_id)
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

    async def _create(self, change: ProposedChange, operation_id: str) -> NotionWriteReceipt:
        if change.course_id is None or change.title is None or change.due_at is None:
            raise permanent_error(ErrorCode.INPUT_INVALID, "assessment create is incomplete")
        target = self._target_store.resolve_course_mutation_target(change.course_id)
        if target is None:
            raise permanent_error(
                ErrorCode.INPUT_INVALID, "course calendar target is not allowlisted"
            )
        return await self._connector.create_assessment_page(
            proposal_id=operation_id,
            data_source_id=target.data_source_id,
            title_property_id=target.title_property_id,
            date_property_id=target.date_property_id,
            title=change.title,
            due=change.due_at,
            ends_at=getattr(change, "ends_at", None),
        )

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


def _payload_hash(change: ProposedChange) -> str:
    if hasattr(change, "model_dump"):
        payload = change.model_dump(mode="json", exclude_none=True)
    else:
        payload = {
            key: _jsonable(value)
            for key, value in vars(change).items()
            if not key.startswith("_") and value is not None
        }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, str):
        return enum_value
    return value


def _receipt_payload(receipt: NotionWriteReceipt) -> dict[str, Any]:
    return receipt.model_dump(mode="json", exclude_none=True)


def _error_code(exc: Exception) -> str:
    if isinstance(exc, LifeAgentError):
        return exc.record.code.value
    return ErrorCode.INTERNAL.value


__all__ = ["AcademicMutationTargetStore", "DiscoveredAcademicNotionWriter"]
