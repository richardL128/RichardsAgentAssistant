"""Add academic memory review ownership metadata.

Revision ID: 0012_academic_memory_review
Revises: 0011_academic_learning_focuses
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_academic_memory_review"
down_revision = "0011_academic_learning_focuses"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "academic_learning_focuses",
        sa.Column("owner_user_id", sa.String(24), nullable=True),
    )
    op.add_column(
        "academic_learning_focuses",
        sa.Column("owner_channel_id", sa.String(24), nullable=True),
    )
    op.add_column(
        "academic_learning_focuses",
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_check_constraint(
        "revision_positive",
        "academic_learning_focuses",
        "revision >= 1",
    )
    op.execute(
        sa.text(
            """
            UPDATE academic_learning_focuses
            SET
                owner_user_id = (
                    SELECT academic_discourse_sessions.discord_user_id
                    FROM academic_discourse_sessions
                    WHERE academic_discourse_sessions.id =
                        academic_learning_focuses.source_session_id
                        AND academic_discourse_sessions.discord_user_id IS NOT NULL
                        AND academic_discourse_sessions.discord_channel_id IS NOT NULL
                    LIMIT 1
                ),
                owner_channel_id = (
                    SELECT academic_discourse_sessions.discord_channel_id
                    FROM academic_discourse_sessions
                    WHERE academic_discourse_sessions.id =
                        academic_learning_focuses.source_session_id
                        AND academic_discourse_sessions.discord_user_id IS NOT NULL
                        AND academic_discourse_sessions.discord_channel_id IS NOT NULL
                    LIMIT 1
                )
            WHERE academic_learning_focuses.source_session_id IS NOT NULL
                AND EXISTS (
                    SELECT 1
                    FROM academic_discourse_sessions
                    WHERE academic_discourse_sessions.id =
                        academic_learning_focuses.source_session_id
                        AND academic_discourse_sessions.discord_user_id IS NOT NULL
                        AND academic_discourse_sessions.discord_channel_id IS NOT NULL
                )
            """
        )
    )
    op.create_index(
        "ix_academic_focus_owner_status_review",
        "academic_learning_focuses",
        ["owner_user_id", "owner_channel_id", "status", "next_review_at"],
    )
    op.create_index(
        "ix_academic_focus_owner_course_status",
        "academic_learning_focuses",
        ["owner_user_id", "owner_channel_id", "course_id", "status"],
    )
    op.create_index(
        "ix_academic_discourse_owner_kind_state_expiry",
        "academic_discourse_sessions",
        ["discord_user_id", "discord_channel_id", "session_kind", "state", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_academic_discourse_owner_kind_state_expiry",
        table_name="academic_discourse_sessions",
    )
    op.drop_index(
        "ix_academic_focus_owner_course_status",
        table_name="academic_learning_focuses",
    )
    op.drop_index(
        "ix_academic_focus_owner_status_review",
        table_name="academic_learning_focuses",
    )
    op.drop_constraint(
        "revision_positive",
        "academic_learning_focuses",
        type_="check",
    )
    op.drop_column("academic_learning_focuses", "revision")
    op.drop_column("academic_learning_focuses", "owner_channel_id")
    op.drop_column("academic_learning_focuses", "owner_user_id")
