"""Replace study blocks with semantic calendar event persistence.

Revision ID: 0026_semantic_calendar_events
Revises: 0025_academic_embedding_hnsw
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026_semantic_calendar_events"
down_revision = "0025_academic_embedding_hnsw"
branch_labels = None
depends_on = None

_SEMANTIC_STATUS_SQL = "('valid','not_substantive','unavailable','invalid')"
_SEMANTIC_INTENT_STATUS_SQL = "('valid','unavailable','invalid')"
_SEMANTIC_INTENT_VALUE_SQL = "('study','regular')"
_EVENT_ACTION_SQL = "('quiz','assignment','tutorial','lab','event','ignore')"


def upgrade() -> None:
    bind = op.get_bind()
    sqlite = bind.dialect.name == "sqlite"
    if sqlite:
        op.execute("PRAGMA ignore_check_constraints = ON")

    try:
        for table_name in ("assessments", "career_interview_events"):
            _add_semantic_intent_columns(table_name)
            _create_semantic_constraints(table_name)

        if _has_table("assessments") and _has_column("assessments", "assessment_type"):
            op.execute(
                "UPDATE assessments SET assessment_type = 'event' "
                "WHERE assessment_type = 'studying_block'"
            )

        if _has_table("academic_clarifications"):
            _drop_check("academic_clarifications", "decision_valid")
            if _has_column("academic_clarifications", "studying_block_preview_title"):
                _rename_column(
                    "academic_clarifications",
                    "studying_block_preview_title",
                    "event_preview_title",
                    sa.String(length=1_024),
                )
            op.execute(
                "UPDATE academic_clarifications "
                "SET decision = 'event' "
                "WHERE decision = 'studying_block'"
            )
            _create_check(
                "academic_clarifications",
                "decision_valid",
                f"decision IS NULL OR decision IN {_EVENT_ACTION_SQL}",
            )

        if _has_table("discord_wake_inbound"):
            _drop_check("discord_wake_inbound", "interaction_action_valid")
            op.execute(
                "UPDATE discord_wake_inbound "
                "SET interaction_action = 'event' "
                "WHERE interaction_action = 'studying_block'"
            )
            _create_check(
                "discord_wake_inbound",
                "interaction_action_valid",
                f"interaction_action IS NULL OR interaction_action IN {_EVENT_ACTION_SQL}",
            )

        if _has_table("academic_checkins") and _has_column("academic_checkins", "plan_id"):
            with op.batch_alter_table("academic_checkins") as batch:
                batch.drop_column("plan_id")

        if _has_table("study_blocks"):
            op.drop_table("study_blocks")
        if _has_table("study_plans"):
            op.drop_table("study_plans")
    finally:
        if sqlite:
            op.execute("PRAGMA ignore_check_constraints = OFF")


def downgrade() -> None:
    raise RuntimeError(
        "Revision 0026 is irreversible because it permanently removes derived local "
        "scheduling tables; restore from a pre-upgrade backup instead."
    )


def _semantic_intent_columns() -> tuple[sa.Column, ...]:
    return (
        sa.Column("calendar_semantic_intent_value", sa.String(length=128)),
        sa.Column("calendar_semantic_intent_status", sa.String(length=32)),
        sa.Column("calendar_semantic_intent_rationale", sa.String(length=500)),
        sa.Column("calendar_semantic_intent_evidence_ids", sa.JSON()),
    )


def _add_semantic_intent_columns(table_name: str) -> None:
    if not _has_table(table_name):
        return
    existing = _columns(table_name)
    with op.batch_alter_table(table_name) as batch:
        for column in _semantic_intent_columns():
            if column.name not in existing:
                batch.add_column(column)


def _create_semantic_constraints(table_name: str) -> None:
    if not _has_table(table_name):
        return
    _create_check(
        table_name,
        "calendar_semantic_status_valid",
        f"calendar_semantic_status IS NULL OR calendar_semantic_status IN {_SEMANTIC_STATUS_SQL}",
    )
    _create_check(
        table_name,
        "calendar_semantic_intent_status_valid",
        "calendar_semantic_intent_status IS NULL "
        f"OR calendar_semantic_intent_status IN {_SEMANTIC_INTENT_STATUS_SQL}",
    )
    _create_check(
        table_name,
        "calendar_semantic_intent_value_valid",
        "calendar_semantic_intent_value IS NULL "
        f"OR calendar_semantic_intent_value IN {_SEMANTIC_INTENT_VALUE_SQL}",
    )
    _create_check(
        table_name,
        "calendar_semantic_intent_rationale_nonempty",
        "calendar_semantic_intent_rationale IS NULL "
        "OR length(calendar_semantic_intent_rationale) > 0",
    )


def _rename_column(
    table_name: str, old_name: str, new_name: str, existing_type: sa.types.TypeEngine
) -> None:
    with op.batch_alter_table(table_name) as batch:
        batch.alter_column(old_name, new_column_name=new_name, existing_type=existing_type)


def _create_check(table_name: str, name: str, condition: str) -> None:
    if name in _check_constraints(table_name):
        return
    with op.batch_alter_table(table_name) as batch:
        batch.create_check_constraint(name, condition)


def _drop_check(table_name: str, name: str) -> None:
    if name not in _check_constraints(table_name):
        return
    with op.batch_alter_table(table_name) as batch:
        batch.drop_constraint(name, type_="check")


def _has_table(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _has_column(table_name: str, column_name: str) -> bool:
    return column_name in _columns(table_name)


def _check_constraints(table_name: str) -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(table_name)
        if constraint.get("name")
    }
