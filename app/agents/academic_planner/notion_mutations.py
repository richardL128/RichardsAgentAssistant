"""Confirmed Notion mutations resolved from synchronized academic targets."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from app.agents.academic_planner.contracts import ProposedChange
from app.connectors.notion import NotionConnector
from app.core.errors import ErrorCode, permanent_error
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
            if change.field == "create_assessment":
                await self._create(change, operation_id)
            elif change.field == "update_assessment":
                await self._update(change, operation_id)
            elif change.field == "archive_assessment":
                await self._archive(change, operation_id)
            else:
                raise permanent_error(
                    ErrorCode.INPUT_INVALID,
                    "planner change is not an allowlisted discovered-calendar write",
                )

    async def _create(self, change: ProposedChange, operation_id: str) -> None:
        if change.course_id is None or change.title is None or change.due_at is None:
            raise permanent_error(ErrorCode.INPUT_INVALID, "assessment create is incomplete")
        target = self._target_store.resolve_course_mutation_target(change.course_id)
        if target is None:
            raise permanent_error(
                ErrorCode.INPUT_INVALID, "course calendar target is not allowlisted"
            )
        await self._connector.create_assessment_page(
            proposal_id=operation_id,
            data_source_id=target.data_source_id,
            title_property_id=target.title_property_id,
            date_property_id=target.date_property_id,
            title=change.title,
            due=change.due_at,
        )

    async def _update(self, change: ProposedChange, operation_id: str) -> None:
        target = self._assessment_target(change)
        if change.title is None and change.due_at is None:
            raise permanent_error(ErrorCode.INPUT_INVALID, "assessment update is incomplete")
        await self._connector.guarded_update_assessment_page(
            proposal_id=operation_id,
            page_id=target.page_id,
            title_property_id=target.title_property_id,
            date_property_id=target.date_property_id,
            expected_title=target.title,
            expected_last_edited_at=target.last_edited_at,
            title=change.title,
            due=change.due_at,
        )

    async def _archive(self, change: ProposedChange, operation_id: str) -> None:
        target = self._assessment_target(change)
        await self._connector.guarded_archive_assessment_page(
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


__all__ = ["AcademicMutationTargetStore", "DiscoveredAcademicNotionWriter"]
