"""Complete Phase 2 lifecycle and idempotency constraints.

Revision ID: 0003_phase2_integration
Revises: 0002_phase2_shared_core
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_phase2_integration"
down_revision = "0002_phase2_shared_core"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.alter_column("agent_runs", "idempotency_key", type_=sa.String(512))
    op.add_column(
        "agent_runs",
        sa.Column("started_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.add_column("agent_runs", sa.Column("finished_at", _TS))

    op.alter_column("deliveries", "idempotency_key", type_=sa.String(512))
    op.drop_constraint("status_valid", "deliveries", type_="check")
    op.create_check_constraint(
        "status_valid",
        "deliveries",
        "status IN ('pending','sending','uncertain','sent','failed','acknowledged')",
    )

    op.add_column(
        "audit_events",
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )

    op.add_column(
        "approval_requests",
        sa.Column("idempotency_key", sa.String(512), nullable=True),
    )
    op.execute(
        "UPDATE approval_requests "
        "SET idempotency_key = 'legacy:' || id::text "
        "WHERE idempotency_key IS NULL"
    )
    op.alter_column("approval_requests", "idempotency_key", nullable=False)
    op.create_unique_constraint(
        "uq_approval_requests_idempotency_key",
        "approval_requests",
        ["idempotency_key"],
    )

    # PostgreSQL 16's NULLS NOT DISTINCT makes presentation acknowledgements
    # idempotent even for service alerts that are not associated with a run.
    op.drop_constraint("uq_ui_ack_user_run_alert", "ui_acknowledgements", type_="unique")
    op.execute(
        "ALTER TABLE ui_acknowledgements "
        "ADD CONSTRAINT uq_ui_ack_user_run_alert "
        "UNIQUE NULLS NOT DISTINCT (user_id, run_id, alert_key)"
    )


def downgrade() -> None:
    op.drop_constraint("uq_ui_ack_user_run_alert", "ui_acknowledgements", type_="unique")
    op.create_unique_constraint(
        "uq_ui_ack_user_run_alert",
        "ui_acknowledgements",
        ["user_id", "run_id", "alert_key"],
    )
    op.drop_constraint("uq_approval_requests_idempotency_key", "approval_requests", type_="unique")
    op.drop_column("approval_requests", "idempotency_key")
    op.drop_column("audit_events", "updated_at")
    op.drop_constraint("status_valid", "deliveries", type_="check")
    op.create_check_constraint(
        "status_valid",
        "deliveries",
        "status IN ('pending','sending','sent','failed','acknowledged')",
    )
    op.alter_column("deliveries", "idempotency_key", type_=sa.String(255))
    op.drop_column("agent_runs", "finished_at")
    op.drop_column("agent_runs", "started_at")
    op.alter_column("agent_runs", "idempotency_key", type_=sa.String(255))
