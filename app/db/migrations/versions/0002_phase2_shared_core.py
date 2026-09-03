"""Add the shared durable orchestration tables.

Revision ID: 0002_phase2_shared_core
Revises: 0001_phase0_procrastinate
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_phase2_shared_core"
down_revision = "0001_phase0_procrastinate"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=True)
_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("agent_name", sa.String(128), nullable=False),
        sa.Column("trigger", sa.String(128), nullable=False),
        sa.Column("schedule", sa.String(128)),
        sa.Column("model_version", sa.String(255)),
        sa.Column("config_version", sa.String(255)),
        sa.Column("input_version", sa.String(255)),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("summary", sa.String(4000)),
        sa.Column("artifact_key", sa.String(512)),
        sa.Column("error_code", sa.String(128)),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("length(agent_name) > 0", name="agent_name_nonempty"),
        sa.CheckConstraint("length(idempotency_key) > 0", name="idempotency_key_nonempty"),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','attention','failed','paused','cancelled')",
            name="status_valid",
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_agent_runs_idempotency_key"),
    )
    op.create_index("ix_agent_runs_status_created", "agent_runs", ["status", "created_at"])

    op.create_table(
        "run_steps",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "run_id", _UUID, sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("node_name", sa.String(128), nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="1"),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("started_at", _TS),
        sa.Column("ended_at", _TS),
        sa.Column("diagnostic", sa.String(2000)),
        sa.Column("model_call_ref", sa.String(255)),
        sa.Column("artifact_key", sa.String(512)),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("attempt >= 1", name="attempt_positive"),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','attention','failed','skipped')",
            name="status_valid",
        ),
        sa.UniqueConstraint("run_id", "node_name", "attempt", name="uq_run_steps_run_node_attempt"),
    )
    op.create_index("ix_run_steps_run_created", "run_steps", ["run_id", "created_at"])

    op.create_table(
        "deliveries",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("run_id", _UUID, sa.ForeignKey("agent_runs.id", ondelete="SET NULL")),
        sa.Column("channel", sa.String(64), nullable=False),
        sa.Column("target", sa.String(255), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_attempt_at", _TS),
        sa.Column("external_url", sa.String(2048)),
        sa.Column("receipt_artifact_key", sa.String(512)),
        sa.Column("error_code", sa.String(128)),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        sa.CheckConstraint(
            "status IN ('pending','sending','sent','failed','acknowledged')",
            name="status_valid",
        ),
        sa.UniqueConstraint("channel", "idempotency_key", name="uq_deliveries_channel_idempotency"),
    )
    op.create_index("ix_deliveries_run_status", "deliveries", ["run_id", "status"])

    op.create_table(
        "evidence_refs",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("run_id", _UUID, sa.ForeignKey("agent_runs.id", ondelete="SET NULL")),
        sa.Column("claim_id", sa.String(255), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("published_at", _TS),
        sa.Column("retrieved_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("source_version", sa.String(255)),
        sa.Column("classification", sa.String(32), nullable=False, server_default="reported"),
        sa.Column("access_classification", sa.String(128)),
        sa.Column("artifact_key", sa.String(512)),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("length(title) > 0", name="title_nonempty"),
        sa.CheckConstraint(
            "classification IN ('primary','reported','secondary')",
            name="classification_valid",
        ),
    )
    op.create_index("ix_evidence_refs_run_claim", "evidence_refs", ["run_id", "claim_id"])

    op.create_table(
        "health_checks",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("check_name", sa.String(128), nullable=False),
        sa.Column("rule", sa.String(255), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("last_success_at", _TS),
        sa.Column("next_due_at", _TS),
        sa.Column("checked_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("diagnostic", sa.String(2000)),
        sa.Column("artifact_key", sa.String(512)),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("state IN ('healthy','attention','failed')", name="state_valid"),
        sa.UniqueConstraint("check_name", name="uq_health_checks_check_name"),
    )
    op.create_index("ix_health_checks_state_due", "health_checks", ["state", "next_due_at"])

    op.create_table(
        "audit_events",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("target_type", sa.String(128), nullable=False),
        sa.Column("target_id", sa.String(255), nullable=False),
        sa.Column("result", sa.String(64), nullable=False),
        sa.Column("run_id", _UUID, sa.ForeignKey("agent_runs.id", ondelete="SET NULL")),
        sa.Column("artifact_key", sa.String(512)),
    )
    op.create_index(
        "ix_audit_events_target_created", "audit_events", ["target_type", "target_id", "created_at"]
    )
    op.create_index("ix_audit_events_run_created", "audit_events", ["run_id", "created_at"])

    # This is a database-level guard in addition to the repository's deliberate
    # omission of update/delete methods.  It protects the event history even if
    # another application code path obtains a raw SQLAlchemy connection.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION lifeagent_reject_audit_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_append_only
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION lifeagent_reject_audit_mutation();
        """
    )

    op.create_table(
        "ui_acknowledgements",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("run_id", _UUID, sa.ForeignKey("agent_runs.id", ondelete="CASCADE")),
        sa.Column("alert_key", sa.String(255), nullable=False),
        sa.Column(
            "acknowledged_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("user_id", "run_id", "alert_key", name="uq_ui_ack_user_run_alert"),
    )
    op.create_index("ix_ui_ack_run_user", "ui_acknowledgements", ["run_id", "user_id"])

    op.create_table(
        "approval_requests",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("run_id", _UUID, sa.ForeignKey("agent_runs.id", ondelete="SET NULL")),
        sa.Column("operation", sa.String(255), nullable=False),
        sa.Column("redacted_preview", sa.String(4000)),
        sa.Column("requester", sa.String(255), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("decision", sa.String(2000)),
        sa.Column("expires_at", _TS),
        sa.Column("audit_event_id", _UUID, sa.ForeignKey("audit_events.id", ondelete="SET NULL")),
        sa.Column("artifact_key", sa.String(512)),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("length(operation) > 0", name="operation_nonempty"),
        sa.CheckConstraint(
            "state IN ('pending','approved','rejected','expired','cancelled')",
            name="state_valid",
        ),
    )
    op.create_index(
        "ix_approval_requests_state_expires", "approval_requests", ["state", "expires_at"]
    )
    op.create_index("ix_approval_requests_run", "approval_requests", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_approval_requests_run", table_name="approval_requests")
    op.drop_index("ix_approval_requests_state_expires", table_name="approval_requests")
    op.drop_table("approval_requests")
    op.drop_index("ix_ui_ack_run_user", table_name="ui_acknowledgements")
    op.drop_table("ui_acknowledgements")
    op.execute("DROP TRIGGER IF EXISTS audit_events_append_only ON audit_events")
    op.drop_index("ix_audit_events_run_created", table_name="audit_events")
    op.drop_index("ix_audit_events_target_created", table_name="audit_events")
    op.drop_table("audit_events")
    op.execute("DROP FUNCTION IF EXISTS lifeagent_reject_audit_mutation()")
    op.drop_index("ix_health_checks_state_due", table_name="health_checks")
    op.drop_table("health_checks")
    op.drop_index("ix_evidence_refs_run_claim", table_name="evidence_refs")
    op.drop_table("evidence_refs")
    op.drop_index("ix_deliveries_run_status", table_name="deliveries")
    op.drop_table("deliveries")
    op.drop_index("ix_run_steps_run_created", table_name="run_steps")
    op.drop_table("run_steps")
    op.drop_index("ix_agent_runs_status_created", table_name="agent_runs")
    op.drop_table("agent_runs")
