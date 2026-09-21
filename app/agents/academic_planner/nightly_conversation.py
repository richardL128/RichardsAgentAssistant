"""Host-trusted state primitives for the academic nightly checklist."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TORONTO_TIMEZONE = "America/Toronto"
NIGHTLY_CHECKPOINT_VERSION = "academic-nightly-checkin-v2"
NIGHTLY_CHECKPOINT_ROOT_KEY = "nightly_checkin"

NightlyPhase = Literal[
    "awaiting_completion",
    "awaiting_move_confirmation",
    "completed",
    "cancelled",
]
NightlySemanticKind = Literal["movable_work_task", "fixed_commitment", "uncertain"]
NightlyOutcomeStatus = Literal[
    "completed",
    "moved",
    "left_in_place",
    "skipped",
    "failed",
]
NightlyProposalOperation = Literal["move_one_day", "mark_completed"]
NightlyReplySemanticAction = Literal[
    "completed",
    "incomplete",
    "confirm_move",
    "decline_move",
    "skip",
    "ambiguous",
]


class NightlyTaskDateRange(BaseModel):
    """Trusted local date shape for one checklist item."""

    model_config = ConfigDict(extra="forbid")

    timezone_name: str = TORONTO_TIMEZONE
    all_day: bool
    start_date: date
    start_time: time | None = None
    end_date: date | None = None
    end_time: time | None = None

    @field_validator("timezone_name")
    @classmethod
    def timezone_is_supported(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("timezone_name must not be empty")
        ZoneInfo(cleaned)
        return cleaned

    @model_validator(mode="after")
    def date_shape_is_consistent(self) -> NightlyTaskDateRange:
        if self.all_day:
            if self.start_time is not None or self.end_time is not None:
                raise ValueError("all-day ranges must not include times")
            return self
        if self.start_time is None:
            raise ValueError("timed ranges must include a start time")
        if (self.end_date is None) != (self.end_time is None):
            raise ValueError("timed range end date and end time must be supplied together")
        return self

    @classmethod
    def from_values(
        cls,
        *,
        start: date | datetime,
        ends_at: date | datetime | None = None,
        all_day: bool,
        timezone_name: str = TORONTO_TIMEZONE,
    ) -> NightlyTaskDateRange:
        zone = ZoneInfo(timezone_name)
        if all_day:
            if isinstance(start, datetime) or isinstance(ends_at, datetime):
                raise ValueError("all-day ranges must use date values")
            return cls(
                timezone_name=timezone_name,
                all_day=True,
                start_date=start,
                end_date=ends_at,
            )
        if not isinstance(start, datetime):
            raise ValueError("timed ranges must use datetime start values")
        local_start = _aware_local(start, zone)
        local_end = _aware_local(ends_at, zone) if isinstance(ends_at, datetime) else None
        return cls(
            timezone_name=timezone_name,
            all_day=False,
            start_date=local_start.date(),
            start_time=local_start.timetz().replace(tzinfo=None),
            end_date=local_end.date() if local_end is not None else None,
            end_time=local_end.timetz().replace(tzinfo=None) if local_end is not None else None,
        )

    def shifted_local_days(self, days: int = 1) -> NightlyTaskDateRange:
        if days < 1:
            raise ValueError("days must be positive")
        offset = timedelta(days=days)
        return self.model_copy(
            update={
                "start_date": self.start_date + offset,
                "end_date": self.end_date + offset if self.end_date is not None else None,
            }
        )

    def sort_key(self) -> tuple[str, str, str]:
        start_time = "00:00:00" if self.start_time is None else self.start_time.isoformat()
        end = "" if self.end_date is None else self.end_date.isoformat()
        return (self.start_date.isoformat(), start_time, end)


class NightlySemanticDecision(BaseModel):
    """Audit metadata for the model-reviewed movable-task decision."""

    model_config = ConfigDict(extra="forbid")

    kind: NightlySemanticKind
    accepted_by_critic: bool
    evidence_citations: tuple[str, ...] = Field(default=(), max_length=12)
    rationale: str | None = Field(default=None, max_length=1_000)
    model_identity: str = Field(min_length=1, max_length=255)
    prompt_version: str = Field(min_length=1, max_length=128)
    critic_version: str = Field(min_length=1, max_length=128)
    source_fingerprint: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def accepted_only_for_movable_work(self) -> NightlySemanticDecision:
        if self.accepted_by_critic and self.kind != "movable_work_task":
            raise ValueError("only movable work tasks may be critic-accepted")
        return self


class NightlyChecklistItem(BaseModel):
    """One host-trusted movable item in the nightly checklist."""

    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1, max_length=80)
    course_id: str = Field(min_length=1, max_length=255)
    course_code: str = Field(min_length=1, max_length=64)
    course_title: str | None = Field(default=None, max_length=255)
    course_display_order: int | None = None
    source_kind: str = Field(min_length=1, max_length=64)
    source_id: str = Field(min_length=1, max_length=255)
    expected_last_edited_at: datetime
    title: str = Field(min_length=1, max_length=500)
    date_range: NightlyTaskDateRange
    semantic_decision: NightlySemanticDecision
    source_fingerprint: str = Field(min_length=1, max_length=255)

    @field_validator("expected_last_edited_at")
    @classmethod
    def expected_last_edited_at_is_aware(cls, value: datetime) -> datetime:
        return _aware_datetime(value, "expected_last_edited_at")

    @model_validator(mode="after")
    def item_must_be_semantically_eligible(self) -> NightlyChecklistItem:
        if (
            self.semantic_decision.kind != "movable_work_task"
            or not self.semantic_decision.accepted_by_critic
        ):
            raise ValueError("nightly checklist items must be accepted movable work tasks")
        return self

    def ordering_key(self) -> tuple[int, str, tuple[str, str, str], str, str]:
        display_order = (
            self.course_display_order if self.course_display_order is not None else 10**9
        )
        return (
            display_order,
            self.course_code.casefold(),
            self.date_range.sort_key(),
            self.title.casefold(),
            self.item_id,
        )

    @property
    def course_label(self) -> str:
        return self.course_code.strip()


class NightlyReplySemanticAudit(BaseModel):
    """Host-private audit facts for the model-interpreted owner reply."""

    model_config = ConfigDict(extra="forbid")

    owner_event_id: str = Field(min_length=1, max_length=255)
    action: NightlyReplySemanticAction
    model_identity: str = Field(min_length=1, max_length=255)
    prompt_version: str = Field(min_length=1, max_length=128)
    occurred_at: datetime | None = None

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _aware_datetime(value, "occurred_at")


class NightlyMovePreviewProof(BaseModel):
    """Proof that the owner saw the exact pending move before natural consent."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(min_length=1, max_length=96)
    item_id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=500)
    old_date_range: NightlyTaskDateRange
    new_date_range: NightlyTaskDateRange
    preview_fingerprint: str = Field(min_length=64, max_length=64)
    rendered_at: datetime | None = None

    @field_validator("rendered_at")
    @classmethod
    def rendered_at_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _aware_datetime(value, "rendered_at")

    @model_validator(mode="after")
    def preview_fingerprint_matches_payload(self) -> NightlyMovePreviewProof:
        expected = nightly_move_preview_fingerprint(
            proposal_id=self.proposal_id,
            item_id=self.item_id,
            title=self.title,
            old_date_range=self.old_date_range,
            new_date_range=self.new_date_range,
        )
        if self.preview_fingerprint != expected:
            raise ValueError("move preview fingerprint does not match the preview payload")
        return self


class NightlyItemOutcome(BaseModel):
    """Terminal result for one checklist item."""

    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1, max_length=80)
    status: NightlyOutcomeStatus
    proposal_id: str | None = Field(default=None, max_length=96)
    owner_event_id: str | None = Field(default=None, max_length=255)
    reply_model_identity: str | None = Field(default=None, max_length=255)
    reply_prompt_version: str | None = Field(default=None, max_length=128)
    reply_semantic_action: NightlyReplySemanticAction | None = None
    occurred_at: datetime | None = None
    detail: str | None = Field(default=None, max_length=1_000)

    @field_validator("occurred_at")
    @classmethod
    def outcome_occurred_at_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _aware_datetime(value, "occurred_at")


class NightlyChecklistCheckpoint(BaseModel):
    """Versioned durable checkpoint stored inside native conversation artifacts."""

    model_config = ConfigDict(extra="forbid")

    version: Literal["academic-nightly-checkin-v2"] = NIGHTLY_CHECKPOINT_VERSION
    period_key: str = Field(min_length=1, max_length=255)
    local_date: date
    timezone_name: str = TORONTO_TIMEZONE
    items: tuple[NightlyChecklistItem, ...] = Field(min_length=1, max_length=100)
    current_index: int = Field(default=0, ge=0)
    phase: NightlyPhase = "awaiting_completion"
    pending_proposal_id: str | None = Field(default=None, max_length=96)
    pending_preview_proof: NightlyMovePreviewProof | None = None
    pending_reply_semantic_audit: NightlyReplySemanticAudit | None = None
    outcomes: dict[str, NightlyItemOutcome] = Field(default_factory=dict)
    builder_model_identity: str | None = Field(default=None, max_length=255)
    eligibility_prompt_version: str | None = Field(default=None, max_length=128)
    critic_version: str | None = Field(default=None, max_length=128)
    source_fingerprints: tuple[str, ...] = Field(default=(), max_length=200)
    created_at: datetime | None = None

    @field_validator("timezone_name")
    @classmethod
    def checkpoint_timezone_is_supported(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("timezone_name must not be empty")
        ZoneInfo(cleaned)
        return cleaned

    @model_validator(mode="after")
    def checkpoint_state_is_legal(self) -> NightlyChecklistCheckpoint:
        item_ids = [item.item_id for item in self.items]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("nightly checklist item ids must be unique")
        if set(self.outcomes) - set(item_ids):
            raise ValueError("nightly outcomes contain an unknown item id")
        for key, outcome in self.outcomes.items():
            if outcome.item_id != key:
                raise ValueError("nightly outcome key must match item_id")
        if self.current_index > len(self.items):
            raise ValueError("current_index is outside the checklist")
        terminal = self.phase in {"completed", "cancelled"}
        if terminal:
            if self.pending_proposal_id is not None:
                raise ValueError("terminal nightly checkpoints cannot have a pending proposal")
            if self.pending_preview_proof is not None:
                raise ValueError("terminal nightly checkpoints cannot have a pending preview")
            if self.pending_reply_semantic_audit is not None:
                raise ValueError("terminal nightly checkpoints cannot have pending reply audit")
            return self
        if self.current_index >= len(self.items):
            raise ValueError("non-terminal nightly checkpoints need a current item")
        current = self.items[self.current_index]
        if self.phase == "awaiting_move_confirmation":
            if self.pending_proposal_id is None:
                raise ValueError("move confirmation phase requires a pending proposal id")
            if self.pending_preview_proof is None:
                raise ValueError("move confirmation phase requires pending preview proof")
            if self.pending_preview_proof.proposal_id != self.pending_proposal_id:
                raise ValueError("pending preview proof must match the pending proposal")
            if self.pending_preview_proof.item_id != current.item_id:
                raise ValueError("pending preview proof must match the current item")
            if self.pending_preview_proof.title != current.title:
                raise ValueError("pending preview proof must match the current item title")
            if self.pending_preview_proof.old_date_range != current.date_range:
                raise ValueError("pending preview proof must match the current item dates")
        if self.phase == "awaiting_completion":
            if self.pending_proposal_id is not None:
                raise ValueError("completion phase cannot have a pending proposal id")
            if self.pending_preview_proof is not None:
                raise ValueError("completion phase cannot have pending preview proof")
            if self.pending_reply_semantic_audit is not None:
                raise ValueError("completion phase cannot have pending reply audit")
        return self


def stable_nightly_item_id(
    *,
    period_key: str,
    source_kind: str,
    source_id: str,
    title: str,
    date_range: NightlyTaskDateRange,
) -> str:
    return _stable_id(
        "nightly_item",
        period_key,
        source_kind,
        source_id,
        title,
        date_range.model_dump(mode="json"),
    )


def stable_nightly_proposal_id(
    *,
    period_key: str,
    item_id: str,
    operation: NightlyProposalOperation,
) -> str:
    return _stable_id("nightly_proposal", period_key, item_id, operation)


def nightly_move_preview_fingerprint(
    *,
    proposal_id: str,
    item_id: str,
    title: str,
    old_date_range: NightlyTaskDateRange,
    new_date_range: NightlyTaskDateRange,
) -> str:
    payload = json.dumps(
        {
            "proposal_id": proposal_id,
            "item_id": item_id,
            "title": title,
            "old_date_range": old_date_range.model_dump(mode="json"),
            "new_date_range": new_date_range.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_move_preview_proof(
    checkpoint: NightlyChecklistCheckpoint,
    *,
    proposal_id: str,
    shifted_range: NightlyTaskDateRange | None = None,
    rendered_at: datetime | None = None,
) -> NightlyMovePreviewProof:
    item = current_item(checkpoint)
    if item is None:
        raise ValueError("nightly checkpoint has no current item")
    target = shifted_range or shift_toronto_local_calendar_day(item.date_range)
    return NightlyMovePreviewProof(
        proposal_id=proposal_id,
        item_id=item.item_id,
        title=item.title,
        old_date_range=item.date_range,
        new_date_range=target,
        preview_fingerprint=nightly_move_preview_fingerprint(
            proposal_id=proposal_id,
            item_id=item.item_id,
            title=item.title,
            old_date_range=item.date_range,
            new_date_range=target,
        ),
        rendered_at=rendered_at,
    )


def ordered_nightly_items(
    items: Sequence[NightlyChecklistItem],
) -> tuple[NightlyChecklistItem, ...]:
    return tuple(sorted(items, key=lambda item: item.ordering_key()))


def build_nightly_checkpoint(
    *,
    period_key: str,
    local_date: date,
    items: Sequence[NightlyChecklistItem],
    timezone_name: str = TORONTO_TIMEZONE,
    builder_model_identity: str | None = None,
    eligibility_prompt_version: str | None = None,
    critic_version: str | None = None,
    created_at: datetime | None = None,
) -> NightlyChecklistCheckpoint:
    ordered = ordered_nightly_items(items)
    return NightlyChecklistCheckpoint(
        period_key=period_key,
        local_date=local_date,
        timezone_name=timezone_name,
        items=ordered,
        builder_model_identity=builder_model_identity,
        eligibility_prompt_version=eligibility_prompt_version,
        critic_version=critic_version,
        source_fingerprints=tuple(item.source_fingerprint for item in ordered),
        created_at=created_at,
    )


def parse_nightly_checkpoint(value: Mapping[str, Any] | None) -> NightlyChecklistCheckpoint:
    if not value:
        raise ValueError("nightly checkpoint is missing")
    candidate: Mapping[str, Any]
    if value.get("version") == NIGHTLY_CHECKPOINT_VERSION:
        candidate = value
    else:
        nested = value.get(NIGHTLY_CHECKPOINT_ROOT_KEY)
        if not isinstance(nested, Mapping):
            raise ValueError("nightly checkpoint is missing")
        candidate = cast(Mapping[str, Any], nested)
    return NightlyChecklistCheckpoint.model_validate(candidate)


def export_nightly_checkpoint(checkpoint: NightlyChecklistCheckpoint) -> dict[str, Any]:
    return checkpoint.model_dump(mode="json", exclude_none=True)


def export_nightly_root_checkpoint(checkpoint: NightlyChecklistCheckpoint) -> dict[str, Any]:
    return {NIGHTLY_CHECKPOINT_ROOT_KEY: export_nightly_checkpoint(checkpoint)}


def current_item(checkpoint: NightlyChecklistCheckpoint) -> NightlyChecklistItem | None:
    if checkpoint.phase in {"completed", "cancelled"} or checkpoint.current_index >= len(
        checkpoint.items
    ):
        return None
    return checkpoint.items[checkpoint.current_index]


def with_pending_move_proposal(
    checkpoint: NightlyChecklistCheckpoint,
    *,
    proposal_id: str,
    preview_proof: NightlyMovePreviewProof | None = None,
    reply_semantic_audit: NightlyReplySemanticAudit | None = None,
) -> NightlyChecklistCheckpoint:
    if checkpoint.phase != "awaiting_completion":
        raise ValueError("move proposals can only be prepared from completion phase")
    if current_item(checkpoint) is None:
        raise ValueError("nightly checkpoint has no current item")
    proof = preview_proof or build_move_preview_proof(checkpoint, proposal_id=proposal_id)
    return _replace_checkpoint(
        checkpoint,
        phase="awaiting_move_confirmation",
        pending_proposal_id=proposal_id,
        pending_preview_proof=proof,
        pending_reply_semantic_audit=reply_semantic_audit,
    )


def advance_nightly_checkpoint(
    checkpoint: NightlyChecklistCheckpoint,
    outcome: NightlyItemOutcome,
) -> NightlyChecklistCheckpoint:
    item = current_item(checkpoint)
    if item is None:
        raise ValueError("nightly checkpoint is already terminal")
    if outcome.item_id != item.item_id:
        raise ValueError("outcome must belong to the current nightly item")
    if checkpoint.phase == "awaiting_completion" and outcome.status == "moved":
        raise ValueError("move outcomes require move confirmation phase")
    if checkpoint.phase == "awaiting_move_confirmation":
        if outcome.proposal_id != checkpoint.pending_proposal_id:
            raise ValueError("move outcome must match the pending proposal")
        if outcome.status == "completed":
            raise ValueError("completion outcomes require completion phase")
    next_index = checkpoint.current_index + 1
    next_phase: NightlyPhase = (
        "completed" if next_index >= len(checkpoint.items) else "awaiting_completion"
    )
    outcomes = dict(checkpoint.outcomes)
    outcomes[item.item_id] = outcome
    return _replace_checkpoint(
        checkpoint,
        current_index=next_index,
        phase=next_phase,
        pending_proposal_id=None,
        pending_preview_proof=None,
        pending_reply_semantic_audit=None,
        outcomes=outcomes,
    )


def summarize_nightly_outcomes(
    checkpoint: NightlyChecklistCheckpoint,
) -> dict[NightlyOutcomeStatus, int]:
    counts: Counter[NightlyOutcomeStatus] = Counter()
    for outcome in checkpoint.outcomes.values():
        counts[outcome.status] += 1
    return {
        "completed": counts["completed"],
        "moved": counts["moved"],
        "left_in_place": counts["left_in_place"],
        "skipped": counts["skipped"],
        "failed": counts["failed"],
    }


def shift_toronto_local_calendar_day(
    date_range: NightlyTaskDateRange,
) -> NightlyTaskDateRange:
    if date_range.timezone_name != TORONTO_TIMEZONE:
        date_range = date_range.model_copy(update={"timezone_name": TORONTO_TIMEZONE})
    return date_range.shifted_local_days(1)


def render_completion_question(checkpoint: NightlyChecklistCheckpoint) -> str:
    item = current_item(checkpoint)
    if item is None:
        raise ValueError("nightly checkpoint has no current item")
    position = checkpoint.current_index + 1
    total = len(checkpoint.items)
    return (
        f"Evening check-in - {item.course_label} ({position}/{total}): "
        f'Did you complete "{item.title}" today?'
    )


def render_move_preview(
    checkpoint: NightlyChecklistCheckpoint,
    *,
    shifted_range: NightlyTaskDateRange | None = None,
) -> str:
    item = current_item(checkpoint)
    if item is None:
        raise ValueError("nightly checkpoint has no current item")
    target = shifted_range or shift_toronto_local_calendar_day(item.date_range)
    return (
        f'Do you want me to move "{item.course_label} - {item.title}" '
        f"from {_format_date_label(item.date_range.start_date)} "
        f"to {_format_date_label(target.start_date)}?"
    )


def render_summary(checkpoint: NightlyChecklistCheckpoint) -> str:
    counts = summarize_nightly_outcomes(checkpoint)
    pieces: list[str] = []
    if counts["completed"]:
        pieces.append(f"{counts['completed']} marked completed")
    if counts["moved"]:
        pieces.append(f"{counts['moved']} moved")
    if counts["left_in_place"]:
        pieces.append(f"{counts['left_in_place']} left in place")
    if counts["skipped"]:
        pieces.append(f"{counts['skipped']} skipped")
    if counts["failed"]:
        pieces.append(f"{counts['failed']} not changed")
    if not pieces:
        pieces.append("no tasks changed")
    return f"Evening check-in complete: {', '.join(pieces)}."


def _stable_id(prefix: str, *parts: object) -> str:
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}_{digest}"


def _aware_local(value: datetime, zone: ZoneInfo) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timed values must be timezone-aware")
    return value.astimezone(zone)


def _aware_datetime(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value


def _format_date_label(value: date) -> str:
    return f"{value.strftime('%B')} {value.day}"


def _replace_checkpoint(
    checkpoint: NightlyChecklistCheckpoint,
    **updates: object,
) -> NightlyChecklistCheckpoint:
    payload = checkpoint.model_dump(mode="python")
    payload.update(updates)
    return NightlyChecklistCheckpoint.model_validate(payload)


__all__ = [
    "NIGHTLY_CHECKPOINT_ROOT_KEY",
    "NIGHTLY_CHECKPOINT_VERSION",
    "TORONTO_TIMEZONE",
    "NightlyChecklistCheckpoint",
    "NightlyChecklistItem",
    "NightlyItemOutcome",
    "NightlyMovePreviewProof",
    "NightlyOutcomeStatus",
    "NightlyPhase",
    "NightlyProposalOperation",
    "NightlyReplySemanticAction",
    "NightlyReplySemanticAudit",
    "NightlySemanticDecision",
    "NightlySemanticKind",
    "NightlyTaskDateRange",
    "advance_nightly_checkpoint",
    "build_move_preview_proof",
    "build_nightly_checkpoint",
    "current_item",
    "export_nightly_checkpoint",
    "export_nightly_root_checkpoint",
    "nightly_move_preview_fingerprint",
    "ordered_nightly_items",
    "parse_nightly_checkpoint",
    "render_completion_question",
    "render_move_preview",
    "render_summary",
    "shift_toronto_local_calendar_day",
    "stable_nightly_item_id",
    "stable_nightly_proposal_id",
    "summarize_nightly_outcomes",
    "with_pending_move_proposal",
]
