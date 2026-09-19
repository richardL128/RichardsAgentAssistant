"""Add durable native conversation sessions.

Revision ID: 0028_native_conversations
Revises: 0027_discord_wake_abort_state
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028_native_conversations"
down_revision = "0027_discord_wake_abort_state"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=True)
_TS = sa.DateTime(timezone=True)
_OPEN_OWNER_CHANNEL = "state IN ('processing','awaiting_user')"


def upgrade() -> None:
    if _has_table("academic_discourse_sessions"):
        op.execute(
            sa.text(
                "UPDATE academic_discourse_sessions "
                "SET state = 'expired', "
                "    updated_at = CURRENT_TIMESTAMP "
                "WHERE state = 'open' AND session_kind = 'agent_clarification'"
            )
        )

    op.create_table(
        "native_conversation_sessions",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("root_event_id", sa.String(255), nullable=False),
        sa.Column("channel", sa.String(64), nullable=False, server_default="discord"),
        sa.Column("discord_channel_id", sa.String(32), nullable=False),
        sa.Column("owner_discord_user_id", sa.String(32), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="processing"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("next_event_sequence", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("transcript_artifact_key", sa.String(64), nullable=False),
        sa.Column("tool_checkpoint_artifact_key", sa.String(64)),
        sa.Column("model_identity", sa.String(255)),
        sa.Column("prompt_config_version", sa.String(255)),
        sa.Column("started_at", _TS, nullable=False),
        sa.Column("last_turn_at", _TS, nullable=False),
        sa.Column("completed_at", _TS),
        sa.Column("expires_at", _TS),
        sa.Column("last_disposition", sa.String(32)),
        sa.Column("error_code", sa.String(128)),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("root_event_id", name="uq_native_conversations_root_event"),
        sa.CheckConstraint("length(root_event_id) > 0", name="root_event_id_nonempty"),
        sa.CheckConstraint("channel = 'discord'", name="channel_discord_only"),
        sa.CheckConstraint(
            "state IN ('processing','awaiting_user','completed','failed','expired','cancelled')",
            name="state_valid",
        ),
        sa.CheckConstraint("revision >= 1", name="revision_positive"),
        sa.CheckConstraint("next_event_sequence >= 1", name="next_event_sequence_positive"),
        sa.CheckConstraint(
            "length(transcript_artifact_key) = 64",
            name="transcript_artifact_key_valid",
        ),
        sa.CheckConstraint(
            "tool_checkpoint_artifact_key IS NULL OR length(tool_checkpoint_artifact_key) = 64",
            name="tool_checkpoint_artifact_key_valid",
        ),
        sa.CheckConstraint(
            "last_disposition IS NULL OR last_disposition IN "
            "('awaiting_user','completed','failed','expired','cancelled')",
            name="last_disposition_valid",
        ),
    )
    op.create_index(
        "uq_native_conversations_open_owner_channel",
        "native_conversation_sessions",
        ["owner_discord_user_id", "discord_channel_id"],
        unique=True,
        postgresql_where=sa.text(_OPEN_OWNER_CHANNEL),
        sqlite_where=sa.text(_OPEN_OWNER_CHANNEL),
    )
    op.create_index(
        "ix_native_conversations_state_expiry",
        "native_conversation_sessions",
        ["state", "expires_at"],
    )
    op.create_index(
        "ix_native_conversations_owner_state",
        "native_conversation_sessions",
        ["owner_discord_user_id", "discord_channel_id", "state"],
    )

    op.create_table(
        "native_conversation_inbound_events",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("conversation_id", _UUID, nullable=False),
        sa.Column("external_event_id", sa.String(255), nullable=False),
        sa.Column("event_sequence", sa.Integer(), nullable=False),
        sa.Column("received_at", _TS, nullable=False),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["native_conversation_sessions.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("external_event_id", name="uq_native_conversation_event"),
        sa.UniqueConstraint(
            "conversation_id",
            "event_sequence",
            name="uq_native_conversation_event_sequence",
        ),
        sa.CheckConstraint("length(external_event_id) > 0", name="external_event_id_nonempty"),
        sa.CheckConstraint("event_sequence >= 1", name="event_sequence_positive"),
    )
    op.create_index(
        "ix_native_conversation_events_session",
        "native_conversation_inbound_events",
        ["conversation_id", "event_sequence"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_native_conversation_events_session",
        table_name="native_conversation_inbound_events",
    )
    op.drop_table("native_conversation_inbound_events")
    op.drop_index("ix_native_conversations_owner_state", table_name="native_conversation_sessions")
    op.drop_index("ix_native_conversations_state_expiry", table_name="native_conversation_sessions")
    op.drop_index(
        "uq_native_conversations_open_owner_channel",
        table_name="native_conversation_sessions",
    )
    op.drop_table("native_conversation_sessions")


def _has_table(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)
