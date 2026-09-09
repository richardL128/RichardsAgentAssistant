"""Constrain open academic agent clarification sessions.

Revision ID: 0015_academic_agent_clarify
Revises: 0014_academic_materials
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_academic_agent_clarify"
down_revision = "0014_academic_materials"
branch_labels = None
depends_on = None

_OPEN_AGENT_CLARIFICATION_OWNER = (
    "state = 'open' AND session_kind = 'agent_clarification' "
    "AND discord_user_id IS NOT NULL AND discord_channel_id IS NOT NULL"
)


def upgrade() -> None:
    op.create_index(
        "uq_academic_discourse_open_agent_clarification_owner",
        "academic_discourse_sessions",
        ["discord_user_id", "discord_channel_id", "session_kind"],
        unique=True,
        postgresql_where=sa.text(_OPEN_AGENT_CLARIFICATION_OWNER),
        sqlite_where=sa.text(_OPEN_AGENT_CLARIFICATION_OWNER),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_academic_discourse_open_agent_clarification_owner",
        table_name="academic_discourse_sessions",
    )
