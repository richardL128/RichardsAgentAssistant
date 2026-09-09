"""Add academic assessment ranges and proposal operation journal.

Revision ID: 0016_academic_date_journal
Revises: 0015_academic_agent_clarify
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_academic_date_journal"
down_revision = "0015_academic_agent_clarify"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=True)
_TS = sa.DateTime(timezone=True)
_DATE_RANGE_VALID = "ends_at IS NULL OR due_at IS NULL OR ends_at > due_at"
_OPERATION_STATE_VALID = "state IN ('in_progress','applied','uncertain','failed')"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("assessments", recreate="always") as batch_op:
            batch_op.add_column(sa.Column("ends_at", _TS))
            batch_op.create_check_constraint("assessment_date_range_valid", _DATE_RANGE_VALID)
    else:
        op.add_column("assessments", sa.Column("ends_at", _TS))
        op.create_check_constraint(
            "assessment_date_range_valid",
            "assessments",
            _DATE_RANGE_VALID,
        )

    op.create_table(
        "academic_proposal_operation_journal",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("proposal_id", _UUID, nullable=False),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("operation_id", sa.String(255), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="in_progress"),
        sa.Column("receipt", sa.JSON),
        sa.Column("error_code", sa.String(128)),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("proposal_id", "ordinal", name="uq_academic_proposal_operation"),
        sa.CheckConstraint("ordinal >= 0", name="academic_proposal_operation_ordinal_nonnegative"),
        sa.CheckConstraint(
            "length(payload_hash) = 64", name="academic_proposal_payload_hash_valid"
        ),
        sa.CheckConstraint(_OPERATION_STATE_VALID, name="academic_proposal_operation_state_valid"),
    )
    op.create_index(
        "ix_academic_proposal_operation_state",
        "academic_proposal_operation_journal",
        ["proposal_id", "state"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_academic_proposal_operation_state",
        table_name="academic_proposal_operation_journal",
    )
    op.drop_table("academic_proposal_operation_journal")

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("assessments", recreate="always") as batch_op:
            batch_op.drop_constraint("assessment_date_range_valid", type_="check")
            batch_op.drop_column("ends_at")
    else:
        op.drop_constraint("assessment_date_range_valid", "assessments", type_="check")
        op.drop_column("assessments", "ends_at")
