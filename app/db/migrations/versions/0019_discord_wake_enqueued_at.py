"""Record successful queue acceptance for Discord wake handoffs.

Revision ID: 0019_discord_enqueued_at
Revises: 0018_discord_continuation
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_discord_enqueued_at"
down_revision = "0018_discord_continuation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "discord_wake_inbound",
        sa.Column("enqueued_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_column("discord_wake_inbound", "enqueued_at")
