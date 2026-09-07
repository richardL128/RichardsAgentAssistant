"""Private Discord message routing for academic proposals and decisions."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any, Literal, Protocol, cast

from app.agents.academic_planner.contracts import CheckinProposal
from app.agents.academic_planner.workflow import (
    NotionAcademicWriter,
    confirm_checkin_proposal,
    extract_checkin_changes,
    reject_checkin_proposal,
)
from app.connectors.discord_gateway import (
    DiscordAcademicMessageCreate,
    DiscordMessageCallbackResult,
)

_PROPOSAL_NAMESPACE = uuid.UUID("6f7240e2-48d0-44bd-b9bf-a8bd8d9adccc")
_COMMAND_PATTERN = re.compile(
    r"(?P<action>confirm|reject) "
    r"(?P<proposal_id>[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12})"
)

AcademicCommandAction = Literal["confirm", "reject"]


class DiscordCheckinStore(Protocol):
    confirmation_ttl_hours: int

    def get_latest_daily_plan(self) -> Any | None: ...

    def save_discord_checkin(
        self,
        proposal: CheckinProposal,
        *,
        external_event_id: str,
        channel: str,
        received_at: Any,
    ) -> Any: ...


class DiscordCheckinDelivery(Protocol):
    async def send_confirmation(
        self, proposal: CheckinProposal, *, idempotency_key: str
    ) -> object: ...

    async def send_response(self, content: str, *, idempotency_key: str) -> object: ...


WriterProvider = Callable[[], NotionAcademicWriter | None]


def parse_academic_command(content: str) -> tuple[AcademicCommandAction, uuid.UUID] | None:
    """Parse only a lowercase command with one canonical UUID and no extras."""

    match = _COMMAND_PATTERN.fullmatch(content)
    if match is None:
        return None
    proposal_id = uuid.UUID(match.group("proposal_id"))
    if str(proposal_id) != match.group("proposal_id"):
        return None
    return cast(AcademicCommandAction, match.group("action")), proposal_id


class AcademicDiscordCheckinHandler:
    """Route one already-normalized private message outside the Gateway loop."""

    def __init__(
        self,
        *,
        store: DiscordCheckinStore,
        delivery: DiscordCheckinDelivery,
        allowed_channel_ids: set[str],
        authorized_user_ids: set[str],
        writer_provider: WriterProvider,
    ) -> None:
        self._store = store
        self._delivery = delivery
        self._allowed_channel_ids = frozenset(allowed_channel_ids)
        self._authorized_user_ids = frozenset(authorized_user_ids)
        self._writer_provider = writer_provider

    async def __call__(self, message: DiscordAcademicMessageCreate) -> DiscordMessageCallbackResult:
        if (
            message.channel_id not in self._allowed_channel_ids
            or message.author_id not in self._authorized_user_ids
        ):
            return DiscordMessageCallbackResult(status="unauthorized")
        content = message.content.get_secret_value()
        command = parse_academic_command(content)
        if command is not None:
            return await self._handle_command(message, *command)
        if content.startswith(("confirm", "reject")):
            await self._delivery.send_response(
                "Command not accepted. Use exactly `confirm <proposal-uuid>` or "
                "`reject <proposal-uuid>` from the authorized private channel.",
                idempotency_key=f"academic-command-invalid:{message.message_id}:v1",
            )
            return DiscordMessageCallbackResult(status="invalid")
        return await self._handle_checkin(message, content)

    async def _handle_checkin(
        self, message: DiscordAcademicMessageCreate, content: str
    ) -> DiscordMessageCallbackResult:
        proposal_id = uuid.uuid5(_PROPOSAL_NAMESPACE, message.message_id)
        latest_plan = self._store.get_latest_daily_plan()
        plan_id = getattr(latest_plan, "plan_id", None)
        changes = extract_checkin_changes(content)
        proposal = CheckinProposal(
            proposal_id=proposal_id,
            confirmation_event=f"confirm {proposal_id}",
            changes=changes,
            source_plan_id=plan_id,
            expires_at=message.timestamp + timedelta(hours=self._store.confirmation_ttl_hours),
            question=(
                None
                if changes
                else "No supported change was extracted. Reply with an explicit supported form."
            ),
        )
        persisted = self._store.save_discord_checkin(
            proposal,
            external_event_id=message.message_id,
            channel=message.channel_id,
            received_at=message.timestamp,
        )
        await self._delivery.send_confirmation(
            proposal,
            idempotency_key=f"academic-discord-message:{message.message_id}:proposal:v1",
        )
        return DiscordMessageCallbackResult(
            status=("duplicate" if getattr(persisted, "status", None) == "replayed" else "handled")
        )

    async def _handle_command(
        self,
        message: DiscordAcademicMessageCreate,
        action: AcademicCommandAction,
        proposal_id: uuid.UUID,
    ) -> DiscordMessageCallbackResult:
        event = f"{action} {proposal_id}"
        if action == "reject":
            result = reject_checkin_proposal(
                store=cast(Any, self._store),
                proposal_id=proposal_id,
                rejection_event=event,
            )
            status = str(result["status"])
            if status in {"rejected", "already_rejected"}:
                response = f"Proposal {proposal_id} is rejected. No Notion change was made."
            else:
                response = f"Proposal {proposal_id} could not be rejected (state: {status})."
        else:
            writer = self._writer_provider()
            if writer is None:
                response = (
                    f"Proposal {proposal_id} was not applied: the scoped Notion writer is "
                    "not configured. Review the Notion mapping setup, then confirm again."
                )
            else:
                try:
                    result = await confirm_checkin_proposal(
                        store=cast(Any, self._store),
                        writer=writer,
                        proposal_id=proposal_id,
                        confirmation_event=event,
                    )
                    status = str(result["status"])
                    response = (
                        f"Proposal {proposal_id} applied."
                        if status == "applied"
                        else f"Proposal {proposal_id} was not applied (state: {status})."
                    )
                except Exception:
                    response = (
                        f"Proposal {proposal_id} could not be safely verified as applied. "
                        "No automatic retry was issued; inspect academic connector health."
                    )
        await self._delivery.send_response(
            response,
            idempotency_key=f"academic-discord-message:{message.message_id}:{action}:v1",
        )
        return DiscordMessageCallbackResult(status="handled")


__all__ = [
    "AcademicCommandAction",
    "AcademicDiscordCheckinHandler",
    "DiscordCheckinDelivery",
    "DiscordCheckinStore",
    "WriterProvider",
    "parse_academic_command",
]
