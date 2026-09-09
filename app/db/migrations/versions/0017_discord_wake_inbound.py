"""Add durable Discord wake inbound handoff state.

Revision ID: 0017_discord_wake_inbound
Revises: 0016_academic_date_journal
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_discord_wake_inbound"
down_revision = "0016_academic_date_journal"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=True)
_TS = sa.DateTime(timezone=True)
_EVENT_KIND_VALID = "event_kind IN ('message','interaction')"
_ACTION_VALID = (
    "action IN "
    "('academic_checkin','agent_clarification','proposal_confirmation','proposal_rejection')"
)
_STATE_VALID = "state IN ('queued','running','completed','failed')"
_INTERACTION_ACTION_VALID = (
    "interaction_action IS NULL OR interaction_action IN "
    "('quiz','assignment','tutorial','lab','studying_block','ignore')"
)


def upgrade() -> None:
    op.create_table(
        "discord_wake_inbound",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("discord_event_id", sa.String(255), nullable=False),
        sa.Column("handoff_nonce", sa.String(64), nullable=False),
        sa.Column("event_kind", sa.String(32), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("action_id", _UUID),
        sa.Column("clarification_id", _UUID),
        sa.Column("interaction_action", sa.String(32)),
        sa.Column("discord_channel_id", sa.String(32)),
        sa.Column("discord_user_id", sa.String(32)),
        sa.Column("discord_message_id", sa.String(32)),
        sa.Column("discord_interaction_id", sa.String(32)),
        sa.Column("ack_message_id", sa.String(32)),
        sa.Column("content_artifact_key", sa.String(512), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.String(128)),
        sa.Column("received_at", _TS, nullable=False),
        sa.Column("started_at", _TS),
        sa.Column("completed_at", _TS),
        sa.Column("failed_at", _TS),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("discord_event_id", name="uq_discord_wake_event"),
        sa.UniqueConstraint("handoff_nonce", name="uq_discord_wake_nonce"),
        sa.CheckConstraint("length(discord_event_id) > 0", name="discord_event_id_nonempty"),
        sa.CheckConstraint(
            "length(handoff_nonce) > 0 AND length(handoff_nonce) <= 64",
            name="handoff_nonce_bounded",
        ),
        sa.CheckConstraint(_EVENT_KIND_VALID, name="event_kind_valid"),
        sa.CheckConstraint(_ACTION_VALID, name="action_valid"),
        sa.CheckConstraint(_STATE_VALID, name="state_valid"),
        sa.CheckConstraint(_INTERACTION_ACTION_VALID, name="interaction_action_valid"),
        sa.CheckConstraint("retry_count >= 0", name="retry_count_nonnegative"),
        sa.CheckConstraint(
            "length(content_artifact_key) > 0",
            name="content_artifact_key_nonempty",
        ),
    )
    op.create_index(
        "ix_discord_wake_state_created",
        "discord_wake_inbound",
        ["state", "created_at"],
    )
    op.create_index("ix_discord_wake_received", "discord_wake_inbound", ["received_at"])


def downgrade() -> None:
    op.drop_index("ix_discord_wake_received", table_name="discord_wake_inbound")
    op.drop_index("ix_discord_wake_state_created", table_name="discord_wake_inbound")
    op.drop_table("discord_wake_inbound")
