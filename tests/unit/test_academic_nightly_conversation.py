from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.agents.academic_planner.nightly_conversation import (
    NightlyChecklistItem,
    NightlyItemOutcome,
    NightlyReplySemanticAudit,
    NightlySemanticDecision,
    NightlyTaskDateRange,
    advance_nightly_checkpoint,
    build_move_preview_proof,
    build_nightly_checkpoint,
    current_item,
    export_nightly_checkpoint,
    export_nightly_root_checkpoint,
    parse_nightly_checkpoint,
    reconstruct_nightly_lifecycle,
    render_completion_question,
    render_move_preview,
    render_summary,
    shift_toronto_local_calendar_day,
    stable_nightly_item_id,
    stable_nightly_proposal_id,
    with_pending_move_proposal,
)

PERIOD = "academic-end-of-day:2026-09-20:2100:v2"
TORONTO = ZoneInfo("America/Toronto")


def _decision(source: str = "fp-1") -> NightlySemanticDecision:
    return NightlySemanticDecision(
        kind="movable_work_task",
        accepted_by_critic=True,
        evidence_citations=("title", "course"),
        model_identity="qwen-test",
        prompt_version="nightly-eligibility-v1",
        critic_version="nightly-critic-v1",
        source_fingerprint=source,
    )


def _item(
    *,
    course: str,
    title: str,
    source_id: str,
    start: NightlyTaskDateRange,
    order: int | None = None,
) -> NightlyChecklistItem:
    item_id = stable_nightly_item_id(
        period_key=PERIOD,
        source_kind="notion_assessment",
        source_id=source_id,
        title=title,
        date_range=start,
    )
    return NightlyChecklistItem(
        item_id=item_id,
        course_id=f"course-{course}",
        course_code=course,
        course_display_order=order,
        source_kind="notion_assessment",
        source_id=source_id,
        expected_last_edited_at=datetime(2026, 9, 20, 12, tzinfo=UTC),
        title=title,
        date_range=start,
        semantic_decision=_decision(source=f"fp-{source_id}"),
        source_fingerprint=f"fp-{source_id}",
    )


def test_checkpoint_orders_validates_round_trips_and_renders_current_question() -> None:
    later = NightlyTaskDateRange(
        all_day=False,
        start_date=date(2026, 9, 20),
        start_time=time(18, 30),
    )
    earlier = NightlyTaskDateRange(
        all_day=False,
        start_date=date(2026, 9, 20),
        start_time=time(9, 0),
    )
    math = _item(course="MATH 239", title="Practice recurrence proofs", source_id="2", start=later)
    ece = _item(course="ECE 250", title="Review merge sort", source_id="1", start=earlier)

    checkpoint = build_nightly_checkpoint(
        period_key=PERIOD,
        local_date=date(2026, 9, 20),
        items=(math, ece),
        builder_model_identity="qwen-test",
        eligibility_prompt_version="nightly-eligibility-v1",
        critic_version="nightly-critic-v1",
        created_at=datetime(2026, 9, 21, 1, tzinfo=UTC),
    )

    assert [item.course_code for item in checkpoint.items] == ["ECE 250", "MATH 239"]
    assert current_item(checkpoint) == ece
    assert render_completion_question(checkpoint) == (
        'Evening check-in - ECE 250 (1/2): Did you complete "Review merge sort" today?'
    )
    exported = export_nightly_checkpoint(checkpoint)
    assert parse_nightly_checkpoint(exported) == checkpoint
    assert parse_nightly_checkpoint(export_nightly_root_checkpoint(checkpoint)) == checkpoint


def test_checkpoint_rejects_fixed_commitments_and_illegal_pending_state() -> None:
    fixed = _decision().model_copy(update={"kind": "fixed_commitment", "accepted_by_critic": False})
    with pytest.raises(ValidationError):
        NightlyChecklistItem(
            item_id="nightly_item_fixed",
            course_id="course-ece",
            course_code="ECE 250",
            source_kind="notion_assessment",
            source_id="midterm",
            expected_last_edited_at=datetime(2026, 9, 20, 12, tzinfo=UTC),
            title="ECE 250 midterm",
            date_range=NightlyTaskDateRange(
                all_day=True,
                start_date=date(2026, 9, 20),
            ),
            semantic_decision=fixed,
            source_fingerprint="fp-fixed",
        )

    item = _item(
        course="ECE 250",
        title="Study for the midterm",
        source_id="study",
        start=NightlyTaskDateRange(all_day=True, start_date=date(2026, 9, 20)),
    )
    checkpoint = build_nightly_checkpoint(
        period_key=PERIOD,
        local_date=date(2026, 9, 20),
        items=(item,),
    )

    with pytest.raises(ValidationError):
        parse_nightly_checkpoint(
            checkpoint.model_dump(mode="json")
            | {"phase": "awaiting_move_confirmation", "pending_proposal_id": None}
        )
    invalid_item = _item(
        course="ECE 250",
        title="Review merge sort",
        source_id="naive",
        start=NightlyTaskDateRange(all_day=True, start_date=date(2026, 9, 20)),
    ).model_dump(mode="python")
    invalid_item["expected_last_edited_at"] = "2026-09-20T12:00:00"
    with pytest.raises(ValidationError):
        NightlyChecklistItem.model_validate(invalid_item)


def test_advance_move_and_summary_helpers_enforce_current_item_protocol() -> None:
    start = NightlyTaskDateRange(all_day=True, start_date=date(2026, 9, 20))
    first = _item(course="ECE 250", title="Review assignment feedback", source_id="1", start=start)
    second = _item(
        course="MATH 239",
        title="Practice recurrence proofs",
        source_id="2",
        start=start,
    )
    checkpoint = build_nightly_checkpoint(
        period_key=PERIOD,
        local_date=date(2026, 9, 20),
        items=(first, second),
    )
    proposal_id = stable_nightly_proposal_id(
        period_key=PERIOD,
        item_id=first.item_id,
        operation="move_one_day",
    )

    reply_audit = NightlyReplySemanticAudit(
        owner_event_id="discord-event-1",
        action="incomplete",
        model_identity="qwen-test",
        prompt_version="nightly-reply-v1",
        occurred_at=datetime(2026, 9, 21, 1, 1, tzinfo=UTC),
    )
    preview_proof = build_move_preview_proof(
        checkpoint,
        proposal_id=proposal_id,
        rendered_at=datetime(2026, 9, 21, 1, 1, 2, tzinfo=UTC),
    )
    awaiting_move = with_pending_move_proposal(
        checkpoint,
        proposal_id=proposal_id,
        preview_proof=preview_proof,
        reply_semantic_audit=reply_audit,
    )
    assert awaiting_move.pending_preview_proof == preview_proof
    assert awaiting_move.pending_reply_semantic_audit == reply_audit
    assert render_move_preview(awaiting_move) == (
        'Do you want me to move "ECE 250 - Review assignment feedback" '
        "from September 20 to September 21?"
    )
    moved = advance_nightly_checkpoint(
        awaiting_move,
        NightlyItemOutcome(
            item_id=first.item_id,
            status="moved",
            proposal_id=proposal_id,
            owner_event_id="discord-event-2",
            reply_model_identity="qwen-test",
            reply_prompt_version="nightly-reply-v1",
            reply_semantic_action="confirm_move",
        ),
    )
    assert current_item(moved) == second
    assert moved.pending_preview_proof is None
    completed = advance_nightly_checkpoint(
        moved,
        NightlyItemOutcome(item_id=second.item_id, status="completed"),
    )

    assert completed.phase == "completed"
    assert render_summary(completed) == "Evening check-in complete: 1 marked completed, 1 moved."


def test_reconstruct_lifecycle_renders_initial_question_from_checkpoint() -> None:
    item = _item(
        course="ECE 250",
        title="Review merge sort",
        source_id="merge-sort",
        start=NightlyTaskDateRange(all_day=True, start_date=date(2026, 9, 20)),
    )
    checkpoint = parse_nightly_checkpoint(
        export_nightly_root_checkpoint(
            build_nightly_checkpoint(
                period_key=PERIOD,
                local_date=date(2026, 9, 20),
                items=(item,),
            )
        )
    )

    lifecycle = reconstruct_nightly_lifecycle(checkpoint)

    assert lifecycle.disposition == "awaiting_user"
    assert lifecycle.content == (
        'Evening check-in - ECE 250 (1/1): Did you complete "Review merge sort" today?'
    )


def test_reconstruct_lifecycle_renders_pending_move_preview_from_proof() -> None:
    item = _item(
        course="ECE 250",
        title="Review assignment feedback",
        source_id="feedback",
        start=NightlyTaskDateRange(all_day=True, start_date=date(2026, 9, 20)),
    )
    checkpoint = build_nightly_checkpoint(
        period_key=PERIOD,
        local_date=date(2026, 9, 20),
        items=(item,),
    )
    proposal_id = stable_nightly_proposal_id(
        period_key=PERIOD,
        item_id=item.item_id,
        operation="move_one_day",
    )
    proof = build_move_preview_proof(
        checkpoint,
        proposal_id=proposal_id,
        shifted_range=NightlyTaskDateRange(all_day=True, start_date=date(2026, 9, 22)),
    )
    awaiting_move = with_pending_move_proposal(
        checkpoint,
        proposal_id=proposal_id,
        preview_proof=proof,
        reply_semantic_audit=NightlyReplySemanticAudit(
            owner_event_id="discord-event-1",
            action="incomplete",
            model_identity="qwen-test",
            prompt_version="nightly-reply-v1",
            occurred_at=datetime(2026, 9, 21, 1, 1, tzinfo=UTC),
        ),
    )

    lifecycle = reconstruct_nightly_lifecycle(awaiting_move)

    assert lifecycle.disposition == "awaiting_user"
    assert lifecycle.content == (
        'I understand. Do you want me to move "ECE 250 - Review assignment feedback" '
        "from September 20 to September 22?"
    )


def test_reconstruct_lifecycle_renders_result_detail_plus_next_question_or_summary() -> None:
    start = NightlyTaskDateRange(all_day=True, start_date=date(2026, 9, 20))
    first = _item(course="ECE 250", title="Review merge sort", source_id="1", start=start)
    second = _item(
        course="MATH 239",
        title="Practice recurrence proofs",
        source_id="2",
        start=start,
    )
    checkpoint = build_nightly_checkpoint(
        period_key=PERIOD,
        local_date=date(2026, 9, 20),
        items=(first, second),
    )

    after_first = advance_nightly_checkpoint(
        checkpoint,
        NightlyItemOutcome(
            item_id=first.item_id,
            status="completed",
            detail='Marked it as "Completed - Review merge sort".',
        ),
    )
    first_lifecycle = reconstruct_nightly_lifecycle(after_first)

    assert first_lifecycle.disposition == "awaiting_user"
    assert first_lifecycle.content == (
        'Marked it as "Completed - Review merge sort". Next, '
        'Evening check-in - MATH 239 (2/2): Did you complete "Practice recurrence proofs" today?'
    )

    completed = advance_nightly_checkpoint(
        after_first,
        NightlyItemOutcome(
            item_id=second.item_id,
            status="failed",
            detail="I couldn't safely mark that task completed. It was left unchanged.",
        ),
    )
    terminal_lifecycle = reconstruct_nightly_lifecycle(completed)

    assert terminal_lifecycle.disposition == "completed"
    assert terminal_lifecycle.content == (
        "I couldn't safely mark that task completed. It was left unchanged. "
        "Evening check-in complete: 1 marked completed, 1 not changed."
    )


def test_reconstruct_lifecycle_renders_cancelled_skip_deterministically() -> None:
    item = _item(
        course="ECE 250",
        title="Review merge sort",
        source_id="merge-sort",
        start=NightlyTaskDateRange(all_day=True, start_date=date(2026, 9, 20)),
    )
    checkpoint = build_nightly_checkpoint(
        period_key=PERIOD,
        local_date=date(2026, 9, 20),
        items=(item,),
    ).model_copy(update={"phase": "cancelled"})

    lifecycle = reconstruct_nightly_lifecycle(checkpoint)

    assert lifecycle.disposition == "completed"
    assert lifecycle.content == "Skipped tonight's check-in. No additional tasks were changed."


def test_reconstruct_lifecycle_keeps_legacy_v2_checkpoint_without_result_detail_resumable() -> None:
    start = NightlyTaskDateRange(all_day=True, start_date=date(2026, 9, 20))
    first = _item(course="ECE 250", title="Review merge sort", source_id="1", start=start)
    second = _item(
        course="MATH 239",
        title="Practice recurrence proofs",
        source_id="2",
        start=start,
    )
    checkpoint = build_nightly_checkpoint(
        period_key=PERIOD,
        local_date=date(2026, 9, 20),
        items=(first, second),
    )
    legacy_after_first = advance_nightly_checkpoint(
        checkpoint,
        NightlyItemOutcome(item_id=first.item_id, status="completed"),
    )

    lifecycle = reconstruct_nightly_lifecycle(
        parse_nightly_checkpoint(legacy_after_first.model_dump(mode="json"))
    )

    assert lifecycle.disposition == "awaiting_user"
    assert lifecycle.content == (
        'Evening check-in - MATH 239 (2/2): Did you complete "Practice recurrence proofs" today?'
    )


def test_shift_toronto_local_calendar_day_preserves_all_day_timed_range_and_dst_wall_time() -> None:
    all_day = NightlyTaskDateRange(all_day=True, start_date=date(2026, 9, 20))
    assert shift_toronto_local_calendar_day(all_day).start_date == date(2026, 9, 21)

    timed = NightlyTaskDateRange.from_values(
        start=datetime(2026, 10, 31, 23, 30, tzinfo=TORONTO),
        ends_at=datetime(2026, 11, 1, 1, 30, tzinfo=TORONTO),
        all_day=False,
    )
    shifted = shift_toronto_local_calendar_day(timed)

    assert shifted.start_date == date(2026, 11, 1)
    assert shifted.start_time == time(23, 30)
    assert shifted.end_date == date(2026, 11, 2)
    assert shifted.end_time == time(1, 30)

    before = datetime.combine(timed.start_date, timed.start_time, tzinfo=TORONTO)
    after = datetime.combine(shifted.start_date, shifted.start_time, tzinfo=TORONTO)
    assert before.utcoffset() != after.utcoffset()
    assert after.astimezone(UTC) - before.astimezone(UTC) != timedelta(hours=24)
