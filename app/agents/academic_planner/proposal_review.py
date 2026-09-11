"""Exact human-review boundary for persisted academic proposals."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from app.agents.academic_planner.contracts import CheckinProposal, ProposedChange
from app.agents.academic_planner.notion_mutations import (
    DiscoveredAcademicNotionWriter,
    review_notion_mutation_batch,
)


class ProposalReviewStore(Protocol):
    """Persistence operations required by exact proposal review."""

    def prepare_checkin_application(
        self,
        proposal_id: uuid.UUID,
        confirmation_event: str,
        *,
        now: datetime | None = None,
    ) -> tuple[str, CheckinProposal | None]: ...

    def mark_checkin_applied(
        self, proposal_id: uuid.UUID, confirmation_event: str | None = None
    ) -> None: ...

    def reject_checkin_proposal(
        self,
        proposal_id: uuid.UUID,
        *,
        actor: str = "academic_planner",
        now: datetime | None = None,
    ) -> tuple[str, CheckinProposal | None]: ...


class NotionAcademicWriter(Protocol):
    """Narrow writer seam reached only after exact confirmation."""

    async def apply_confirmed_changes(
        self,
        changes: Sequence[ProposedChange],
        *,
        proposal_id: uuid.UUID,
        confirmation_event: str,
    ) -> None: ...


async def confirm_checkin_proposal(
    *,
    store: ProposalReviewStore,
    writer: NotionAcademicWriter,
    proposal_id: uuid.UUID,
    confirmation_event: str,
    now: datetime | None = None,
) -> dict[str, object]:
    """Apply the persisted batch only after byte-for-byte human confirmation."""

    if confirmation_event != f"confirm {proposal_id}":
        return {"status": "confirmation_required", "proposal_id": str(proposal_id)}

    if now is None:
        status, proposal = store.prepare_checkin_application(proposal_id, confirmation_event)
    else:
        status, proposal = store.prepare_checkin_application(
            proposal_id, confirmation_event, now=now
        )
    if status == "not_found" or proposal is None:
        return {"status": "not_found", "proposal_id": str(proposal_id)}
    if status in {"confirmation_required", "expired"}:
        return {"status": "confirmation_required", "proposal_id": str(proposal_id)}
    if status == "already_applied":
        return {
            "status": "applied",
            "proposal_id": str(proposal_id),
            "change_count": len(proposal.changes),
        }
    if status == "in_progress":
        return {"status": "confirmation_required", "proposal_id": str(proposal_id)}
    if status != "ready":
        raise RuntimeError("academic proposal entered an unknown confirmation state")

    if isinstance(writer, DiscoveredAcademicNotionWriter):
        review = review_notion_mutation_batch(
            proposal.changes,
            proposal_id=proposal_id,
            confirmation_event=confirmation_event,
        )
        await writer.apply_confirmed_changes(
            proposal.changes,
            proposal_id=proposal_id,
            confirmation_event=confirmation_event,
            review=review,
        )
    else:
        await writer.apply_confirmed_changes(
            proposal.changes,
            proposal_id=proposal_id,
            confirmation_event=confirmation_event,
        )
    store.mark_checkin_applied(proposal_id, confirmation_event)
    return {
        "status": "applied",
        "proposal_id": str(proposal_id),
        "change_count": len(proposal.changes),
    }


def reject_checkin_proposal(
    *,
    store: ProposalReviewStore,
    proposal_id: uuid.UUID,
    rejection_event: str,
    now: datetime | None = None,
) -> dict[str, object]:
    """Terminally reject exactly one pending proposal without an external write."""

    if rejection_event != f"reject {proposal_id}":
        return {"status": "rejection_required", "proposal_id": str(proposal_id)}
    if now is None:
        status, proposal = store.reject_checkin_proposal(
            proposal_id,
            actor="discord_authorized_user",
        )
    else:
        status, proposal = store.reject_checkin_proposal(
            proposal_id,
            actor="discord_authorized_user",
            now=now,
        )
    if proposal is None:
        status = "not_found"
    elif status not in {"rejected", "already_rejected"}:
        status = "rejection_required"
    result: dict[str, object] = {
        "status": status,
        "proposal_id": str(proposal_id),
    }
    if proposal is not None:
        result["change_count"] = len(proposal.changes)
    return result


__all__ = [
    "NotionAcademicWriter",
    "ProposalReviewStore",
    "confirm_checkin_proposal",
    "reject_checkin_proposal",
]
