"""Allow owner-scoped Discord clarification continuations.

Revision ID: 0018_discord_continuation
Revises: 0017_discord_wake_inbound
"""

from __future__ import annotations

from alembic import op

revision = "0018_discord_continuation"
down_revision = "0017_discord_wake_inbound"
branch_labels = None
depends_on = None

_NEW_ACTIONS = (
    "action IN ('academic_checkin','academic_continuation','agent_clarification',"
    "'proposal_confirmation','proposal_rejection')"
)
_OLD_ACTIONS = (
    "action IN ('academic_checkin','agent_clarification','proposal_confirmation',"
    "'proposal_rejection')"
)


def upgrade() -> None:
    op.drop_constraint("action_valid", "discord_wake_inbound", type_="check")
    op.create_check_constraint("action_valid", "discord_wake_inbound", _NEW_ACTIONS)


def downgrade() -> None:
    op.drop_constraint("action_valid", "discord_wake_inbound", type_="check")
    op.create_check_constraint("action_valid", "discord_wake_inbound", _OLD_ACTIONS)
