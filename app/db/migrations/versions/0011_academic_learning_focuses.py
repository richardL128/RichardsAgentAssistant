"""Add academic learning focus discourse memory.

Revision ID: 0011_academic_learning_focuses
Revises: 0010_public_finance_sources
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0011_academic_learning_focuses"
down_revision = "0010_public_finance_sources"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=True)
_TS = sa.DateTime(timezone=True)
_JSON_EMPTY = sa.text("'{}'")


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "academic_discourse_sessions",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("external_event_id", sa.String(255), nullable=False),
        sa.Column("channel", sa.String(64), nullable=False, server_default="discord"),
        sa.Column("discord_channel_id", sa.String(24)),
        sa.Column("discord_user_id", sa.String(24)),
        sa.Column("session_kind", sa.String(64), nullable=False, server_default="learning_focus"),
        sa.Column("state", sa.String(32), nullable=False, server_default="open"),
        sa.Column("partial_state", sa.JSON(), nullable=False, server_default=_JSON_EMPTY),
        sa.Column("missed_review_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("reminder_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("started_at", _TS, nullable=False),
        sa.Column("last_turn_at", _TS, nullable=False),
        sa.Column("completed_at", _TS),
        sa.Column("expires_at", _TS),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("length(external_event_id) > 0", name="external_event_id_nonempty"),
        sa.CheckConstraint("channel = 'discord'", name="channel_discord_only"),
        sa.CheckConstraint("state IN ('open','completed','expired')", name="state_valid"),
        sa.CheckConstraint("missed_review_count >= 0", name="missed_review_count_nonnegative"),
        sa.CheckConstraint("reminder_count >= 0", name="reminder_count_nonnegative"),
        sa.UniqueConstraint("external_event_id", name="uq_academic_discourse_external_event"),
    )
    op.create_index(
        "ix_academic_discourse_state_expiry",
        "academic_discourse_sessions",
        ["state", "expires_at"],
    )
    op.create_index(
        "ix_academic_discourse_last_turn",
        "academic_discourse_sessions",
        ["state", "last_turn_at"],
    )
    op.create_index(
        "ix_academic_discourse_discord_owner",
        "academic_discourse_sessions",
        ["discord_channel_id", "discord_user_id", "state"],
    )
    op.create_table(
        "academic_discourse_turns",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "session_id",
            _UUID,
            sa.ForeignKey("academic_discourse_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_event_id", sa.String(255), nullable=False),
        sa.Column("received_at", _TS, nullable=False),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("external_event_id", name="uq_academic_discourse_turn_event"),
    )
    op.create_index(
        "ix_academic_discourse_turn_session",
        "academic_discourse_turns",
        ["session_id", "received_at"],
    )

    op.create_table(
        "academic_learning_focuses",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("course_id", _UUID, sa.ForeignKey("courses.id", ondelete="SET NULL")),
        sa.Column(
            "assessment_id",
            _UUID,
            sa.ForeignKey("assessments.id", ondelete="SET NULL"),
        ),
        sa.Column("course_code", sa.String(64)),
        sa.Column("topic", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column(
            "source_session_id",
            _UUID,
            sa.ForeignKey("academic_discourse_sessions.id", ondelete="SET NULL"),
        ),
        sa.Column("source_external_event_id", sa.String(255)),
        sa.Column("reinforcement_count", sa.Integer, nullable=False, server_default="1"),
        sa.Column("next_review_at", _TS),
        sa.Column("practice_due_on", sa.Date),
        sa.Column("practice_minutes", sa.Integer),
        sa.Column("last_reviewed_at", _TS),
        sa.Column("last_review_prompted_at", _TS),
        sa.Column("last_reinforced_at", _TS, nullable=False),
        sa.Column("missed_review_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("reminder_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_reminded_at", _TS),
        sa.Column("snoozed_at", _TS),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("length(topic) > 0", name="topic_nonempty"),
        sa.CheckConstraint("status IN ('active','snoozed')", name="status_valid"),
        sa.CheckConstraint("reinforcement_count >= 1", name="reinforcement_count_positive"),
        sa.CheckConstraint("missed_review_count >= 0", name="missed_review_count_nonnegative"),
        sa.CheckConstraint("reminder_count >= 0", name="reminder_count_nonnegative"),
        sa.CheckConstraint("practice_minutes IS NULL OR practice_minutes > 0", name="practice_minutes_positive"),
        sa.UniqueConstraint("source_external_event_id", name="uq_academic_focus_source_event"),
    )
    op.create_index(
        "ix_academic_focus_status_review",
        "academic_learning_focuses",
        ["status", "next_review_at"],
    )
    op.create_index(
        "ix_academic_focus_course_status",
        "academic_learning_focuses",
        ["course_id", "status"],
    )
    op.add_column(
        "study_blocks",
        sa.Column(
            "learning_focus_id",
            _UUID,
            sa.ForeignKey("academic_learning_focuses.id", ondelete="SET NULL"),
        ),
    )
    op.add_column(
        "study_blocks",
        sa.Column(
            "block_kind",
            sa.String(32),
            nullable=False,
            server_default="assessment",
        ),
    )
    op.create_check_constraint(
        "block_kind_valid",
        "study_blocks",
        "block_kind IN ('assessment','practice')",
    )
    op.create_index(
        "ix_study_blocks_learning_focus",
        "study_blocks",
        ["learning_focus_id", "status"],
    )

    op.create_table(
        "academic_learning_focus_events",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "focus_id",
            _UUID,
            sa.ForeignKey("academic_learning_focuses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            _UUID,
            sa.ForeignKey("academic_discourse_sessions.id", ondelete="SET NULL"),
        ),
        sa.Column("external_event_id", sa.String(255)),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(255)),
        sa.Column("occurred_at", _TS, nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=_JSON_EMPTY),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("length(event_type) > 0", name="event_type_nonempty"),
        sa.CheckConstraint("actor IS NULL OR length(actor) > 0", name="actor_nonempty"),
        sa.UniqueConstraint("external_event_id", name="uq_academic_focus_events_external_event"),
    )
    op.create_index(
        "ix_academic_focus_events_focus_time",
        "academic_learning_focus_events",
        ["focus_id", "occurred_at"],
    )
    op.create_index(
        "ix_academic_focus_events_session",
        "academic_learning_focus_events",
        ["session_id", "occurred_at"],
    )

    op.create_table(
        "academic_reflection_memories",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "focus_id",
            _UUID,
            sa.ForeignKey("academic_learning_focuses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            _UUID,
            sa.ForeignKey("academic_discourse_sessions.id", ondelete="SET NULL"),
        ),
        sa.Column("external_event_id", sa.String(255)),
        sa.Column("raw_text", sa.Text, nullable=False),
        sa.Column("redacted_summary", sa.String(2000)),
        sa.Column(
            "embedding",
            Vector() if op.get_bind().dialect.name == "postgresql" else sa.JSON(),
        ),
        sa.Column("embedding_model", sa.String(255)),
        sa.Column("embedding_dimensions", sa.Integer),
        sa.Column("embedding_metadata", sa.JSON(), nullable=False, server_default=_JSON_EMPTY),
        sa.Column("recorded_at", _TS, nullable=False),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("length(raw_text) > 0", name="raw_text_nonempty"),
        sa.CheckConstraint(
            "embedding_dimensions IS NULL OR embedding_dimensions > 0",
            name="embedding_dimensions_positive",
        ),
        sa.UniqueConstraint("external_event_id", name="uq_academic_reflection_external_event"),
    )
    op.create_index(
        "ix_academic_reflections_focus_time",
        "academic_reflection_memories",
        ["focus_id", "recorded_at"],
    )
    op.create_index(
        "ix_academic_reflections_session",
        "academic_reflection_memories",
        ["session_id", "recorded_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_academic_reflections_session", table_name="academic_reflection_memories")
    op.drop_index("ix_academic_reflections_focus_time", table_name="academic_reflection_memories")
    op.drop_table("academic_reflection_memories")
    op.drop_index("ix_academic_focus_events_session", table_name="academic_learning_focus_events")
    op.drop_index(
        "ix_academic_focus_events_focus_time",
        table_name="academic_learning_focus_events",
    )
    op.drop_table("academic_learning_focus_events")
    op.drop_index("ix_study_blocks_learning_focus", table_name="study_blocks")
    op.drop_constraint("block_kind_valid", "study_blocks", type_="check")
    op.drop_column("study_blocks", "block_kind")
    op.drop_column("study_blocks", "learning_focus_id")
    op.drop_index("ix_academic_focus_course_status", table_name="academic_learning_focuses")
    op.drop_index("ix_academic_focus_status_review", table_name="academic_learning_focuses")
    op.drop_table("academic_learning_focuses")
    op.drop_index(
        "ix_academic_discourse_turn_session",
        table_name="academic_discourse_turns",
    )
    op.drop_table("academic_discourse_turns")
    op.drop_index(
        "ix_academic_discourse_discord_owner",
        table_name="academic_discourse_sessions",
    )
    op.drop_index("ix_academic_discourse_last_turn", table_name="academic_discourse_sessions")
    op.drop_index("ix_academic_discourse_state_expiry", table_name="academic_discourse_sessions")
    op.drop_table("academic_discourse_sessions")
