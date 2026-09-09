"""Expand academic clarification todo decisions.

Revision ID: 0013_academic_todo_types
Revises: 0012_academic_memory_review
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_academic_todo_types"
down_revision = "0012_academic_memory_review"
branch_labels = None
depends_on = None

_NEW_DECISION_VALID = (
    "decision IS NULL OR decision IN "
    "('quiz','assignment','tutorial','lab','studying_block','ignore')"
)
_OLD_DECISION_VALID = "decision IS NULL OR decision IN ('quiz','assignment','ignore')"


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("academic_clarifications", recreate="always") as batch_op:
            batch_op.add_column(sa.Column("tutorial_preview_title", sa.String(1_024)))
            batch_op.add_column(sa.Column("lab_preview_title", sa.String(1_024)))
            batch_op.add_column(sa.Column("studying_block_preview_title", sa.String(1_024)))
            batch_op.drop_constraint("decision_valid", type_="check")
            batch_op.create_check_constraint("decision_valid", _NEW_DECISION_VALID)
        return

    op.add_column(
        "academic_clarifications",
        sa.Column("tutorial_preview_title", sa.String(1_024)),
    )
    op.add_column(
        "academic_clarifications",
        sa.Column("lab_preview_title", sa.String(1_024)),
    )
    op.add_column(
        "academic_clarifications",
        sa.Column("studying_block_preview_title", sa.String(1_024)),
    )
    op.drop_constraint("decision_valid", "academic_clarifications", type_="check")
    op.create_check_constraint(
        "decision_valid",
        "academic_clarifications",
        _NEW_DECISION_VALID,
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("academic_clarifications", recreate="always") as batch_op:
            batch_op.drop_constraint("decision_valid", type_="check")
            batch_op.create_check_constraint("decision_valid", _OLD_DECISION_VALID)
            batch_op.drop_column("studying_block_preview_title")
            batch_op.drop_column("lab_preview_title")
            batch_op.drop_column("tutorial_preview_title")
        return

    op.drop_constraint("decision_valid", "academic_clarifications", type_="check")
    op.create_check_constraint(
        "decision_valid",
        "academic_clarifications",
        _OLD_DECISION_VALID,
    )
    op.drop_column("academic_clarifications", "studying_block_preview_title")
    op.drop_column("academic_clarifications", "lab_preview_title")
    op.drop_column("academic_clarifications", "tutorial_preview_title")
