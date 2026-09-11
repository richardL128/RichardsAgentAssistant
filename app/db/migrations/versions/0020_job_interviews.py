"""Add career interview preparation persistence.

Revision ID: 0020_job_interviews
Revises: 0019_discord_enqueued_at
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020_job_interviews"
down_revision = "0019_discord_enqueued_at"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=True)
_TS = sa.DateTime(timezone=True)
_JSON_LIST = sa.text("'[]'")
_JSON_OBJECT = sa.text("'{}'")


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )


def upgrade() -> None:
    op.create_table(
        "career_jobs_workspaces",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("scope", sa.String(128), nullable=False, server_default="default"),
        sa.Column("jobs_page_id", sa.String(255)),
        sa.Column("jobs_page_title", sa.String(255)),
        sa.Column("discovery_status", sa.String(32), nullable=False, server_default="missing"),
        sa.Column("diagnostic_code", sa.String(128)),
        sa.Column("diagnostic_fingerprint", sa.String(128)),
        sa.Column("interviews_database_id", sa.String(255)),
        sa.Column("interviews_data_source_id", sa.String(255)),
        sa.Column("title_property_id", sa.String(255)),
        sa.Column("title_property_name", sa.String(255)),
        sa.Column("date_property_id", sa.String(255)),
        sa.Column("date_property_name", sa.String(255)),
        sa.Column("last_discovered_at", _TS),
        sa.Column("last_synced_at", _TS),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        *_timestamps(),
        sa.CheckConstraint("length(scope) > 0", name="scope_nonempty"),
        sa.CheckConstraint(
            "discovery_status IN ('valid','missing','duplicate','inaccessible','malformed')",
            name="discovery_status_valid",
        ),
        sa.UniqueConstraint("scope", name="uq_career_jobs_workspaces_scope"),
        sa.UniqueConstraint("jobs_page_id", name="uq_career_jobs_workspaces_page"),
        sa.UniqueConstraint(
            "interviews_data_source_id", name="uq_career_jobs_workspaces_interviews_source"
        ),
    )
    op.create_index(
        "ix_career_jobs_workspaces_status",
        "career_jobs_workspaces",
        ["discovery_status", "last_synced_at"],
    )

    op.create_table(
        "career_sync_cursors",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("scope", sa.String(255), nullable=False),
        sa.Column("source_version", sa.String(128), nullable=False, server_default="notion"),
        sa.Column("cursor", sa.String(512)),
        sa.Column("last_synced_at", _TS),
        sa.Column("status", sa.String(32), nullable=False, server_default="idle"),
        sa.Column("error_code", sa.String(128)),
        *_timestamps(),
        sa.CheckConstraint("length(scope) > 0", name="scope_nonempty"),
        sa.CheckConstraint(
            "status IN ('idle','running','succeeded','failed')",
            name="status_valid",
        ),
        sa.UniqueConstraint("scope", name="uq_career_sync_cursors_scope"),
    )

    op.create_table(
        "career_application_tables",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "workspace_id",
            _UUID,
            sa.ForeignKey("career_jobs_workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("table_block_id", sa.String(255), nullable=False),
        sa.Column("table_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("has_column_header", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("row_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("column_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("content_fingerprint", sa.String(128), nullable=False),
        sa.Column("last_seen_at", _TS, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        *_timestamps(),
        sa.CheckConstraint("length(table_block_id) > 0", name="table_block_id_nonempty"),
        sa.CheckConstraint("table_order >= 0", name="table_order_nonnegative"),
        sa.CheckConstraint("row_count >= 0", name="row_count_nonnegative"),
        sa.UniqueConstraint("table_block_id", name="uq_career_application_tables_block"),
    )
    op.create_index(
        "ix_career_application_tables_workspace_active",
        "career_application_tables",
        ["workspace_id", "active"],
    )

    op.create_table(
        "career_application_rows",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "table_id",
            _UUID,
            sa.ForeignKey("career_application_tables.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("row_block_id", sa.String(255), nullable=False),
        sa.Column("row_order", sa.Integer, nullable=False),
        sa.Column("is_header", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("cells", sa.JSON, nullable=False, server_default=_JSON_LIST),
        sa.Column("normalized_cells", sa.JSON, nullable=False, server_default=_JSON_LIST),
        sa.Column("content_fingerprint", sa.String(128), nullable=False),
        sa.Column("last_seen_at", _TS, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        *_timestamps(),
        sa.CheckConstraint("length(row_block_id) > 0", name="row_block_id_nonempty"),
        sa.CheckConstraint("row_order >= 0", name="row_order_nonnegative"),
        sa.UniqueConstraint("row_block_id", name="uq_career_application_rows_block"),
    )
    op.create_index(
        "ix_career_application_rows_table_active",
        "career_application_rows",
        ["table_id", "active", "row_order"],
    )
    op.create_index(
        "ix_career_application_rows_fingerprint",
        "career_application_rows",
        ["content_fingerprint"],
    )

    op.create_table(
        "career_application_interpretations",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "row_id",
            _UUID,
            sa.ForeignKey("career_application_rows.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("company_name", sa.String(255)),
        sa.Column("role_title", sa.String(500)),
        sa.Column("status", sa.String(255)),
        sa.Column("confidence", sa.Float, nullable=False, server_default="0"),
        sa.Column("evidence", sa.JSON, nullable=False, server_default=_JSON_LIST),
        sa.Column("model_version", sa.String(255)),
        sa.Column("interpreted_at", _TS, nullable=False),
        *_timestamps(),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_valid"),
        sa.UniqueConstraint("row_id", name="uq_career_application_interpretations_row"),
    )
    op.create_index(
        "ix_career_application_interpretations_company",
        "career_application_interpretations",
        ["company_name"],
    )

    op.create_table(
        "career_interview_events",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "workspace_id",
            _UUID,
            sa.ForeignKey("career_jobs_workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("interview_page_id", sa.String(255), nullable=False),
        sa.Column("interviews_database_id", sa.String(255)),
        sa.Column("interviews_data_source_id", sa.String(255)),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("date_start", _TS),
        sa.Column("local_date", sa.Date),
        sa.Column("is_all_day", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="America/Toronto"),
        sa.Column("notion_last_edited_at", _TS, nullable=False),
        sa.Column("source_url", sa.String(2048)),
        sa.Column("tags", sa.JSON, nullable=False, server_default=_JSON_LIST),
        sa.Column("property_snapshot", sa.JSON, nullable=False, server_default=_JSON_OBJECT),
        sa.Column("url_candidates", sa.JSON, nullable=False, server_default=_JSON_LIST),
        sa.Column("content_fingerprint", sa.String(128), nullable=False),
        sa.Column("content_artifact_key", sa.String(512)),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("archived", sa.Boolean, nullable=False, server_default=sa.false()),
        *_timestamps(),
        sa.CheckConstraint("length(interview_page_id) > 0", name="interview_page_id_nonempty"),
        sa.CheckConstraint("length(title) > 0", name="title_nonempty"),
        sa.UniqueConstraint("interview_page_id", name="uq_career_interview_events_page"),
    )
    op.create_index(
        "ix_career_interview_events_date_active",
        "career_interview_events",
        ["active", "local_date"],
    )
    op.create_index(
        "ix_career_interview_events_workspace",
        "career_interview_events",
        ["workspace_id", "active"],
    )

    op.create_table(
        "career_interview_application_links",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "interview_id",
            _UUID,
            sa.ForeignKey("career_interview_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "application_row_id",
            _UUID,
            sa.ForeignKey("career_application_rows.id", ondelete="SET NULL"),
        ),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("confidence", sa.Float, nullable=False, server_default="0"),
        sa.Column("rationale", sa.String(1000), nullable=False),
        sa.Column("evidence", sa.JSON, nullable=False, server_default=_JSON_LIST),
        sa.Column("clarification_id", _UUID),
        sa.Column("interview_content_fingerprint", sa.String(128)),
        sa.Column("application_content_fingerprint", sa.String(128)),
        sa.Column("resolution_source", sa.String(16), nullable=False, server_default="model"),
        sa.Column("resolved_at", _TS, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        *_timestamps(),
        sa.CheckConstraint(
            "state IN ('matched','ambiguous','needs_clarification','rejected')",
            name="state_valid",
        ),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_valid"),
        sa.CheckConstraint("resolution_source IN ('model','user')", name="resolution_source_valid"),
        sa.UniqueConstraint("interview_id", name="uq_career_interview_links_interview"),
    )
    op.create_index(
        "ix_career_interview_links_row",
        "career_interview_application_links",
        ["application_row_id", "state"],
    )

    op.create_table(
        "career_research_snapshots",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "interview_id",
            _UUID,
            sa.ForeignKey("career_interview_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_url", sa.String(2048), nullable=False),
        sa.Column("canonical_url", sa.String(2048)),
        sa.Column("company_name", sa.String(255)),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("content_fingerprint", sa.String(128)),
        sa.Column("excerpt_artifact_key", sa.String(512)),
        sa.Column("failure_code", sa.String(128)),
        sa.Column("retrieved_at", _TS, nullable=False),
        sa.Column("freshness_expires_at", _TS),
        sa.Column("source_metadata", sa.JSON, nullable=False, server_default=_JSON_OBJECT),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending','fetched','partial','failed','stale')",
            name="status_valid",
        ),
    )
    op.create_index(
        "ix_career_research_interview_time",
        "career_research_snapshots",
        ["interview_id", "retrieved_at"],
    )
    op.create_index(
        "ix_career_research_status_freshness",
        "career_research_snapshots",
        ["status", "freshness_expires_at"],
    )

    op.create_table(
        "career_preparation_plans",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "interview_id",
            _UUID,
            sa.ForeignKey("career_interview_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer, nullable=False, server_default="1"),
        sa.Column("status", sa.String(32), nullable=False, server_default="current"),
        sa.Column("generated_at", _TS, nullable=False),
        sa.Column("plan_hash", sa.String(128), nullable=False),
        sa.Column("summary", sa.String(1000), nullable=False),
        sa.Column("next_actions", sa.JSON, nullable=False, server_default=_JSON_LIST),
        sa.Column("evidence", sa.JSON, nullable=False, server_default=_JSON_LIST),
        sa.Column(
            "research_snapshot_id",
            _UUID,
            sa.ForeignKey("career_research_snapshots.id", ondelete="SET NULL"),
        ),
        sa.Column("artifact_key", sa.String(512)),
        sa.Column("material_change_reason", sa.String(1000)),
        sa.Column("plan_payload", sa.JSON, nullable=False, server_default=_JSON_OBJECT),
        *_timestamps(),
        sa.CheckConstraint("revision >= 1", name="revision_positive"),
        sa.CheckConstraint(
            "status IN ('draft','current','stale','failed')",
            name="status_valid",
        ),
        sa.UniqueConstraint("interview_id", name="uq_career_preparation_plans_interview"),
    )
    op.create_index(
        "ix_career_preparation_plans_status",
        "career_preparation_plans",
        ["status", "updated_at"],
    )

    op.create_table(
        "career_preparation_plan_revisions",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "plan_id",
            _UUID,
            sa.ForeignKey("career_preparation_plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "interview_id",
            _UUID,
            sa.ForeignKey("career_interview_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("generated_at", _TS, nullable=False),
        sa.Column("plan_hash", sa.String(128), nullable=False),
        sa.Column("summary", sa.String(1000), nullable=False),
        sa.Column("next_actions", sa.JSON, nullable=False, server_default=_JSON_LIST),
        sa.Column("evidence", sa.JSON, nullable=False, server_default=_JSON_LIST),
        sa.Column(
            "research_snapshot_id",
            _UUID,
            sa.ForeignKey("career_research_snapshots.id", ondelete="SET NULL"),
        ),
        sa.Column("artifact_key", sa.String(512)),
        sa.Column("material_change_reason", sa.String(1000)),
        sa.Column("plan_payload", sa.JSON, nullable=False, server_default=_JSON_OBJECT),
        *_timestamps(),
        sa.CheckConstraint("revision >= 1", name="revision_positive"),
        sa.UniqueConstraint("plan_id", "revision", name="uq_career_plan_revisions_plan_revision"),
    )
    op.create_index(
        "ix_career_plan_revisions_interview",
        "career_preparation_plan_revisions",
        ["interview_id", "revision"],
    )

    op.create_table(
        "career_clarifications",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("subject_type", sa.String(64), nullable=False),
        sa.Column("subject_id", sa.String(255), nullable=False),
        sa.Column("question", sa.String(1000), nullable=False),
        sa.Column("choices", sa.JSON, nullable=False, server_default=_JSON_LIST),
        sa.Column("idempotency_key", sa.String(512), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("partial_state", sa.JSON, nullable=False, server_default=_JSON_OBJECT),
        sa.Column("response_artifact_key", sa.String(512)),
        sa.Column("response_summary", sa.String(1000)),
        sa.Column("discord_channel_id", sa.String(32)),
        sa.Column("discord_user_id", sa.String(32)),
        sa.Column("delivery_id", sa.String(255)),
        sa.Column("delivered_at", _TS),
        sa.Column("answered_at", _TS),
        sa.Column("resolved_at", _TS),
        sa.Column("expires_at", _TS),
        sa.Column("error_code", sa.String(128)),
        *_timestamps(),
        sa.CheckConstraint(
            "kind IN ('jobs_configuration','application_match','interview_date','posting_url',"
            "'preparation_context')",
            name="kind_valid",
        ),
        sa.CheckConstraint(
            "state IN ('pending','delivered','answered','resolved','failed','expired','cancelled')",
            name="state_valid",
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_career_clarifications_idempotency"),
    )
    op.create_index(
        "ix_career_clarifications_state_expiry",
        "career_clarifications",
        ["state", "expires_at"],
    )
    op.create_index(
        "ix_career_clarifications_subject",
        "career_clarifications",
        ["subject_type", "subject_id", "state"],
    )

    op.create_table(
        "career_reminder_deliveries",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "interview_id",
            _UUID,
            sa.ForeignKey("career_interview_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reminder_date", sa.Date, nullable=False),
        sa.Column("reminder_kind", sa.String(32), nullable=False),
        sa.Column("days_until", sa.Integer, nullable=False),
        sa.Column("delivery_id", _UUID, sa.ForeignKey("deliveries.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("included_at", _TS),
        sa.Column("error_code", sa.String(128)),
        *_timestamps(),
        sa.CheckConstraint("days_until >= 0", name="days_until_nonnegative"),
        sa.CheckConstraint(
            "status IN ('pending','included','sent','failed','skipped')",
            name="status_valid",
        ),
        sa.UniqueConstraint(
            "interview_id",
            "reminder_date",
            "reminder_kind",
            name="uq_career_reminder_deliveries_event_day_kind",
        ),
    )
    op.create_index(
        "ix_career_reminder_deliveries_date_status",
        "career_reminder_deliveries",
        ["reminder_date", "status"],
    )

    op.create_table(
        "career_write_proposals",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "interview_id",
            _UUID,
            sa.ForeignKey("career_interview_events.id", ondelete="SET NULL"),
        ),
        sa.Column("idempotency_key", sa.String(512), nullable=False),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("target_page_id", sa.String(255), nullable=False),
        sa.Column("expected_last_edited_at", _TS),
        sa.Column("payload", sa.JSON, nullable=False, server_default=_JSON_OBJECT),
        sa.Column("redacted_preview", sa.String(4000), nullable=False),
        sa.Column("confirmation_token", sa.String(255), nullable=False),
        sa.Column("confirmation_event", sa.String(255)),
        sa.Column("state", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("requester", sa.String(255)),
        sa.Column("expires_at", _TS),
        sa.Column("applied_at", _TS),
        *_timestamps(),
        sa.CheckConstraint(
            "operation IN ('interview_date','preparation_plan')",
            name="operation_valid",
        ),
        sa.CheckConstraint(
            "state IN ('pending','confirmed','applying','rejected','applied','expired','failed')",
            name="state_valid",
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_career_write_proposals_idempotency"),
    )
    op.create_index(
        "ix_career_write_proposals_state",
        "career_write_proposals",
        ["state", "created_at"],
    )
    op.create_index(
        "ix_career_write_proposals_target",
        "career_write_proposals",
        ["target_page_id", "operation"],
    )

    op.create_table(
        "career_write_receipts",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "proposal_id",
            _UUID,
            sa.ForeignKey("career_write_proposals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("operation_id", sa.String(255), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="in_progress"),
        sa.Column("receipt", sa.JSON),
        sa.Column("error_code", sa.String(128)),
        *_timestamps(),
        sa.CheckConstraint("length(payload_hash) = 64", name="payload_hash_valid"),
        sa.CheckConstraint(
            "state IN ('in_progress','applied','uncertain','failed')",
            name="state_valid",
        ),
        sa.UniqueConstraint(
            "proposal_id", "operation_id", name="uq_career_write_receipts_operation"
        ),
    )
    op.create_index(
        "ix_career_write_receipts_state",
        "career_write_receipts",
        ["proposal_id", "state"],
    )


def downgrade() -> None:
    op.drop_index("ix_career_write_receipts_state", table_name="career_write_receipts")
    op.drop_table("career_write_receipts")
    op.drop_index("ix_career_write_proposals_target", table_name="career_write_proposals")
    op.drop_index("ix_career_write_proposals_state", table_name="career_write_proposals")
    op.drop_table("career_write_proposals")
    op.drop_index(
        "ix_career_reminder_deliveries_date_status",
        table_name="career_reminder_deliveries",
    )
    op.drop_table("career_reminder_deliveries")
    op.drop_index("ix_career_clarifications_subject", table_name="career_clarifications")
    op.drop_index("ix_career_clarifications_state_expiry", table_name="career_clarifications")
    op.drop_table("career_clarifications")
    op.drop_index(
        "ix_career_plan_revisions_interview",
        table_name="career_preparation_plan_revisions",
    )
    op.drop_table("career_preparation_plan_revisions")
    op.drop_index(
        "ix_career_preparation_plans_status",
        table_name="career_preparation_plans",
    )
    op.drop_table("career_preparation_plans")
    op.drop_index(
        "ix_career_research_status_freshness",
        table_name="career_research_snapshots",
    )
    op.drop_index(
        "ix_career_research_interview_time",
        table_name="career_research_snapshots",
    )
    op.drop_table("career_research_snapshots")
    op.drop_index("ix_career_interview_links_row", table_name="career_interview_application_links")
    op.drop_table("career_interview_application_links")
    op.drop_index("ix_career_interview_events_workspace", table_name="career_interview_events")
    op.drop_index("ix_career_interview_events_date_active", table_name="career_interview_events")
    op.drop_table("career_interview_events")
    op.drop_index(
        "ix_career_application_interpretations_company",
        table_name="career_application_interpretations",
    )
    op.drop_table("career_application_interpretations")
    op.drop_index(
        "ix_career_application_rows_fingerprint",
        table_name="career_application_rows",
    )
    op.drop_index(
        "ix_career_application_rows_table_active",
        table_name="career_application_rows",
    )
    op.drop_table("career_application_rows")
    op.drop_index(
        "ix_career_application_tables_workspace_active",
        table_name="career_application_tables",
    )
    op.drop_table("career_application_tables")
    op.drop_index("ix_career_jobs_workspaces_status", table_name="career_jobs_workspaces")
    op.drop_table("career_sync_cursors")
    op.drop_table("career_jobs_workspaces")
