"""Add durable Discord wake abort state and safe activity fields.

Revision ID: 0027_discord_wake_abort_state
Revises: 0026_semantic_calendar_events
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027_discord_wake_abort_state"
down_revision = "0026_semantic_calendar_events"
branch_labels = None
depends_on = None

_TABLE = "discord_wake_inbound"
_OLD_STATE_VALID = "state IN ('queued','running','completed','failed')"
_NEW_STATE_VALID = "state IN ('queued','running','abort_requested','aborted','completed','failed')"
_PRIOR_STATE_VALID = (
    "abort_requested_prior_state IS NULL OR abort_requested_prior_state IN "
    "('queued','running','abort_requested','aborted','completed','failed')"
)
_SIDE_EFFECT_VALID = (
    "activity_side_effect_class IS NULL OR activity_side_effect_class IN "
    "('read_only','proposal_only','durable_local_write','external_write','unknown')"
)
_TOOL_STATUS_VALID = (
    "activity_tool_status IS NULL OR activity_tool_status IN "
    "('not_started','running','succeeded','failed','unknown','completed_before_cancel',"
    "'cancellation_requested','cancelled')"
)
_NEW_COLUMN_NAMES = (
    "queue_job_id",
    "abort_requested_at",
    "abort_requested_by_event_id",
    "abort_requested_prior_state",
    "abort_terminal_at",
    "abort_reason_code",
    "activity_phase",
    "activity_model_turn",
    "activity_tool_name",
    "activity_tool_status",
    "activity_side_effect_class",
    "activity_updated_at",
)


def upgrade() -> None:
    if not _has_table("discord_abort_requests"):
        op.create_table(
            "discord_abort_requests",
            sa.Column("abort_event_id", sa.String(255), primary_key=True),
            sa.Column("handoff_nonce", sa.String(64), nullable=False),
            sa.Column("discord_channel_id", sa.String(32), nullable=False),
            sa.Column("discord_user_id", sa.String(32), nullable=False),
            sa.Column("ack_message_id", sa.String(32), nullable=False),
            sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default="processing"),
            sa.Column("target_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("running_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("queued_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("safe_activity_label", sa.String(80)),
            sa.Column("safe_tool_status", sa.String(32), nullable=False, server_default="none"),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.UniqueConstraint("handoff_nonce", name="uq_discord_abort_nonce"),
            sa.CheckConstraint(
                "length(abort_event_id) > 0",
                name="abort_event_id_nonempty",
            ),
            sa.CheckConstraint(
                "status IN ('processing','accepted','no_active','unconfirmed')",
                name="status_valid",
            ),
            sa.CheckConstraint(
                "safe_tool_status IN "
                "('none','cancelled','cancellation_requested','unknown',"
                "'completed_before_cancel')",
                name="safe_tool_status_valid",
            ),
            sa.CheckConstraint(
                "target_count >= 0 AND running_count >= 0 AND queued_count >= 0",
                name="abort_counts_nonnegative",
            ),
        )
        op.create_index(
            "ix_discord_abort_scope_received",
            "discord_abort_requests",
            ["discord_channel_id", "discord_user_id", "received_at"],
        )
    if not _has_table(_TABLE):
        return

    existing_columns = _columns(_TABLE)
    existing_checks = _check_constraints(_TABLE)
    with op.batch_alter_table(_TABLE) as batch:
        for column in _new_columns():
            if column.name not in existing_columns:
                batch.add_column(column)
        if "state_valid" in existing_checks:
            batch.drop_constraint("state_valid", type_="check")
        batch.create_check_constraint("state_valid", _NEW_STATE_VALID)
        if "abort_requested_prior_state_valid" not in existing_checks:
            batch.create_check_constraint(
                "abort_requested_prior_state_valid",
                _PRIOR_STATE_VALID,
            )
        if "activity_side_effect_class_valid" not in existing_checks:
            batch.create_check_constraint(
                "activity_side_effect_class_valid",
                _SIDE_EFFECT_VALID,
            )
        if "activity_tool_status_valid" not in existing_checks:
            batch.create_check_constraint("activity_tool_status_valid", _TOOL_STATUS_VALID)

    op.create_index(
        "ix_discord_wake_scope_active",
        _TABLE,
        ["discord_channel_id", "discord_user_id", "state"],
    )
    op.create_index("ix_discord_wake_queue_job", _TABLE, ["queue_job_id"])


def downgrade() -> None:
    if not _has_table(_TABLE):
        if _has_table("discord_abort_requests"):
            op.drop_index(
                "ix_discord_abort_scope_received",
                table_name="discord_abort_requests",
            )
            op.drop_table("discord_abort_requests")
        return

    op.drop_index("ix_discord_wake_queue_job", table_name=_TABLE)
    op.drop_index("ix_discord_wake_scope_active", table_name=_TABLE)
    op.execute(
        sa.text(
            "UPDATE discord_wake_inbound "
            "SET state = 'failed', "
            "    failed_at = COALESCE(failed_at, abort_terminal_at, abort_requested_at, CURRENT_TIMESTAMP), "
            "    last_error_code = COALESCE(last_error_code, abort_reason_code, 'user_abort') "
            "WHERE state IN ('abort_requested','aborted')"
        )
    )

    existing_columns = _columns(_TABLE)
    existing_checks = _check_constraints(_TABLE)
    with op.batch_alter_table(_TABLE) as batch:
        if "activity_tool_status_valid" in existing_checks:
            batch.drop_constraint("activity_tool_status_valid", type_="check")
        if "activity_side_effect_class_valid" in existing_checks:
            batch.drop_constraint("activity_side_effect_class_valid", type_="check")
        if "abort_requested_prior_state_valid" in existing_checks:
            batch.drop_constraint("abort_requested_prior_state_valid", type_="check")
        if "state_valid" in existing_checks:
            batch.drop_constraint("state_valid", type_="check")
        batch.create_check_constraint("state_valid", _OLD_STATE_VALID)
        for column_name in reversed(_NEW_COLUMN_NAMES):
            if column_name in existing_columns:
                batch.drop_column(column_name)
    if _has_table("discord_abort_requests"):
        op.drop_index(
            "ix_discord_abort_scope_received",
            table_name="discord_abort_requests",
        )
        op.drop_table("discord_abort_requests")


def _has_table(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _new_columns() -> tuple[sa.Column, ...]:
    return (
        sa.Column("queue_job_id", sa.BigInteger()),
        sa.Column("abort_requested_at", sa.DateTime(timezone=True)),
        sa.Column("abort_requested_by_event_id", sa.String(255)),
        sa.Column("abort_requested_prior_state", sa.String(32)),
        sa.Column("abort_terminal_at", sa.DateTime(timezone=True)),
        sa.Column("abort_reason_code", sa.String(128)),
        sa.Column("activity_phase", sa.String(64)),
        sa.Column("activity_model_turn", sa.Integer()),
        sa.Column("activity_tool_name", sa.String(128)),
        sa.Column("activity_tool_status", sa.String(64)),
        sa.Column("activity_side_effect_class", sa.String(64)),
        sa.Column("activity_updated_at", sa.DateTime(timezone=True)),
    )


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _check_constraints(table_name: str) -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(table_name)
        if constraint.get("name")
    }
