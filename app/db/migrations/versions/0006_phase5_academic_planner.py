"""Add durable academic planner records and cited document chunks.

Revision ID: 0006_phase5_academic_planner
Revises: 0005_phase4_operations
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_phase5_academic_planner"
down_revision = "0005_phase4_operations"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=True)
_TS = sa.DateTime(timezone=True)
_JSON_EMPTY = sa.text("'{}'")


def upgrade() -> None:
    op.create_table(
        "courses",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("notion_id", sa.String(255), nullable=False),
        sa.Column("course_code", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("term", sa.String(128), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("priority", sa.Integer, nullable=False, server_default="50"),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("length(notion_id) > 0", name="notion_id_nonempty"),
        sa.CheckConstraint("priority >= 0 AND priority <= 100", name="priority_valid"),
        sa.UniqueConstraint("notion_id", name="uq_courses_notion_id"),
    )
    op.create_index("ix_courses_term_code", "courses", ["term", "course_code"])

    op.create_table(
        "assessments",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "course_id", _UUID, sa.ForeignKey("courses.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("notion_id", sa.String(255), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("assessment_type", sa.String(64), nullable=False),
        sa.Column("due_at", _TS),
        sa.Column("grade_weight_percent", sa.Float),
        sa.Column("estimated_minutes", sa.Integer, nullable=False, server_default="60"),
        sa.Column("confidence_gap", sa.Float, nullable=False, server_default="0.5"),
        sa.Column("scope_size", sa.Float, nullable=False, server_default="0"),
        sa.Column("scope", sa.String(4000)),
        sa.Column("fact_state", sa.String(32), nullable=False, server_default="unconfirmed"),
        sa.Column("confidence", sa.Float, nullable=False, server_default="0"),
        sa.Column("ambiguity_reason", sa.String(2000)),
        sa.Column("source_page", sa.Integer),
        sa.Column("source_block", sa.String(255)),
        sa.Column("source_url", sa.String(1000)),
        sa.Column("completed", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_valid"),
        sa.CheckConstraint(
            "fact_state IN ('unconfirmed','confirmed','ambiguous','rejected')",
            name="fact_state_valid",
        ),
        sa.CheckConstraint(
            "grade_weight_percent IS NULL OR "
            "(grade_weight_percent >= 0 AND grade_weight_percent <= 100)",
            name="grade_weight_valid",
        ),
        sa.CheckConstraint("source_page IS NULL OR source_page >= 1", name="source_page_positive"),
        sa.CheckConstraint("estimated_minutes > 0", name="estimated_minutes_positive"),
        sa.CheckConstraint(
            "confidence_gap >= 0 AND confidence_gap <= 1", name="confidence_gap_valid"
        ),
        sa.CheckConstraint("scope_size >= 0 AND scope_size <= 100", name="scope_size_valid"),
        sa.UniqueConstraint("notion_id", name="uq_assessments_notion_id"),
    )
    op.create_index("ix_assessments_course_due", "assessments", ["course_id", "due_at"])
    op.create_index("ix_assessments_fact_state", "assessments", ["fact_state", "due_at"])

    op.create_table(
        "fixed_commitments",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("course_id", _UUID, sa.ForeignKey("courses.id", ondelete="SET NULL")),
        sa.Column("notion_id", sa.String(255), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("commitment_type", sa.String(64), nullable=False),
        sa.Column("starts_at", _TS, nullable=False),
        sa.Column("ends_at", _TS, nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("fact_state", sa.String(32), nullable=False, server_default="unconfirmed"),
        sa.Column("confidence", sa.Float, nullable=False, server_default="0"),
        sa.Column("ambiguity_reason", sa.String(2000)),
        sa.Column("source_page", sa.Integer),
        sa.Column("source_block", sa.String(255)),
        sa.Column("source_url", sa.String(1000)),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            "fact_state IN ('unconfirmed','confirmed','ambiguous','rejected')",
            name="fact_state_valid",
        ),
        sa.CheckConstraint("source_page IS NULL OR source_page >= 1", name="source_page_positive"),
        sa.CheckConstraint("ends_at > starts_at", name="commitment_times_valid"),
        sa.UniqueConstraint("notion_id", name="uq_fixed_commitments_notion_id"),
    )
    op.create_index("ix_fixed_commitments_time", "fixed_commitments", ["starts_at", "ends_at"])

    op.create_table(
        "planning_preferences",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("scope", sa.String(128), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("availability", sa.JSON, nullable=False, server_default=_JSON_EMPTY),
        sa.Column("daily_capacity_minutes", sa.Integer, nullable=False, server_default="240"),
        sa.Column("buffer_minutes", sa.Integer, nullable=False, server_default="15"),
        sa.Column("sleep_schedule", sa.JSON, nullable=False, server_default=_JSON_EMPTY),
        sa.Column("version", sa.String(128), nullable=False, server_default="v1"),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("daily_capacity_minutes > 0", name="daily_capacity_positive"),
        sa.CheckConstraint("buffer_minutes >= 0", name="buffer_nonnegative"),
        sa.UniqueConstraint("scope", name="uq_planning_preferences_scope"),
    )

    op.create_table(
        "study_plans",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("plan_key", sa.String(255), nullable=False),
        sa.Column("starts_on", sa.Date, nullable=False),
        sa.Column("ends_on", sa.Date, nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("preference_version", sa.String(128)),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("ends_on >= starts_on", name="study_plan_dates_valid"),
        sa.CheckConstraint("status IN ('draft','published','superseded')", name="status_valid"),
        sa.UniqueConstraint("plan_key", name="uq_study_plans_plan_key"),
    )
    op.create_index("ix_study_plans_window", "study_plans", ["starts_on", "ends_on"])

    op.create_table(
        "study_blocks",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "plan_id", _UUID, sa.ForeignKey("study_plans.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("block_key", sa.String(255), nullable=False),
        sa.Column("assessment_id", _UUID, sa.ForeignKey("assessments.id", ondelete="SET NULL")),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("starts_at", _TS, nullable=False),
        sa.Column("ends_at", _TS, nullable=False),
        sa.Column("allocated_minutes", sa.Integer, nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="planned"),
        sa.Column(
            "carry_forward_from_id",
            _UUID,
            sa.ForeignKey("study_blocks.id", ondelete="SET NULL"),
        ),
        sa.Column("notes", sa.String(2000)),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("allocated_minutes > 0", name="allocated_minutes_positive"),
        sa.CheckConstraint(
            "status IN ('planned','in_progress','completed','incomplete','carried_forward')",
            name="status_valid",
        ),
        sa.CheckConstraint("ends_at > starts_at", name="study_block_times_valid"),
        sa.UniqueConstraint("plan_id", "block_key", name="uq_study_blocks_plan_key"),
    )
    op.create_index("ix_study_blocks_plan_start", "study_blocks", ["plan_id", "starts_at"])
    op.create_index("ix_study_blocks_assessment", "study_blocks", ["assessment_id", "status"])

    op.create_table(
        "academic_documents",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("course_id", _UUID, sa.ForeignKey("courses.id", ondelete="SET NULL")),
        sa.Column("notion_id", sa.String(255), nullable=False),
        sa.Column("document_version", sa.String(128), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("document_type", sa.String(64), nullable=False),
        sa.Column("source_url", sa.String(1000)),
        sa.Column("retrieved_at", _TS, nullable=False),
        sa.Column("artifact_key", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("access_classification", sa.String(64), nullable=False, server_default="private"),
        sa.Column("extraction_status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("length(content_hash) = 64", name="content_hash_length"),
        sa.UniqueConstraint("notion_id", "document_version", name="uq_academic_documents_version"),
    )
    op.create_index(
        "ix_academic_documents_course_version",
        "academic_documents",
        ["course_id", "document_version"],
    )

    op.create_table(
        "academic_document_chunks",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "document_id",
            _UUID,
            sa.ForeignKey("academic_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("heading", sa.String(500)),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("source_page", sa.Integer),
        sa.Column("source_block", sa.String(255)),
        sa.Column("source_url", sa.String(1000)),
        sa.Column("token_count", sa.Integer),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("ordinal >= 0", name="ordinal_nonnegative"),
        sa.CheckConstraint("source_page IS NULL OR source_page >= 1", name="source_page_positive"),
        sa.UniqueConstraint("document_id", "ordinal", name="uq_academic_chunks_document_ordinal"),
    )
    # PostgreSQL gets a persisted tsvector and GIN index. SQLite unit tests use
    # the repository's deterministic ILIKE fallback instead.
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE academic_document_chunks ADD COLUMN search_vector tsvector "
            "GENERATED ALWAYS AS (to_tsvector('simple', "
            "coalesce(heading, '') || ' ' || content)) STORED"
        )
        op.create_index(
            "ix_academic_chunks_search_vector",
            "academic_document_chunks",
            ["search_vector"],
            postgresql_using="gin",
        )
    else:
        op.add_column("academic_document_chunks", sa.Column("search_vector", sa.Text))
    op.create_index(
        "ix_academic_chunks_document_page",
        "academic_document_chunks",
        ["document_id", "source_page", "ordinal"],
    )

    op.create_table(
        "academic_sync_cursors",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("scope", sa.String(255), nullable=False),
        sa.Column("source", sa.String(64), nullable=False, server_default="notion"),
        sa.Column("cursor", sa.String(512)),
        sa.Column("source_version", sa.String(128), nullable=False),
        sa.Column("last_synced_at", _TS),
        sa.Column("status", sa.String(32), nullable=False, server_default="idle"),
        sa.Column("error_code", sa.String(128)),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("scope", name="uq_academic_sync_cursors_scope"),
    )

    op.create_table(
        "academic_checkins",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("idempotency_key", sa.String(512), nullable=False),
        sa.Column("external_event_id", sa.String(255), nullable=False),
        sa.Column("channel", sa.String(64), nullable=False),
        sa.Column("received_at", _TS, nullable=False),
        sa.Column("content_artifact_key", sa.String(64)),
        sa.Column("redacted_summary", sa.String(2000)),
        sa.Column("status", sa.String(32), nullable=False, server_default="received"),
        sa.Column("plan_id", _UUID, sa.ForeignKey("study_plans.id", ondelete="SET NULL")),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            "status IN ('received','questioned','planned','proposal_pending','completed','failed')",
            name="status_valid",
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_academic_checkins_idempotency"),
        sa.UniqueConstraint("external_event_id", name="uq_academic_checkins_external_event"),
    )

    op.create_table(
        "academic_proposed_changes",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "checkin_id",
            _UUID,
            sa.ForeignKey("academic_checkins.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(512), nullable=False),
        sa.Column("operation", sa.String(128), nullable=False),
        sa.Column("target_type", sa.String(128), nullable=False),
        sa.Column("target_id", sa.String(255), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False, server_default=_JSON_EMPTY),
        sa.Column("redacted_preview", sa.String(4000), nullable=False),
        sa.Column("confirmation_token", sa.String(255), nullable=False),
        sa.Column("confirmation_event", sa.String(255)),
        sa.Column("state", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("expires_at", _TS),
        sa.Column("applied_at", _TS),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            "state IN ('pending','confirmed','applying','rejected','applied','expired')",
            name="state_valid",
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_academic_proposals_idempotency"),
    )
    op.create_index(
        "ix_academic_proposals_state", "academic_proposed_changes", ["state", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_academic_proposals_state", table_name="academic_proposed_changes")
    op.drop_table("academic_proposed_changes")
    op.drop_table("academic_checkins")
    op.drop_table("academic_sync_cursors")
    if op.get_bind().dialect.name == "postgresql":
        op.drop_index("ix_academic_chunks_search_vector", table_name="academic_document_chunks")
    else:
        op.drop_column("academic_document_chunks", "search_vector")
    op.drop_index("ix_academic_chunks_document_page", table_name="academic_document_chunks")
    op.drop_table("academic_document_chunks")
    op.drop_index("ix_academic_documents_course_version", table_name="academic_documents")
    op.drop_table("academic_documents")
    op.drop_index("ix_study_blocks_assessment", table_name="study_blocks")
    op.drop_index("ix_study_blocks_plan_start", table_name="study_blocks")
    op.drop_table("study_blocks")
    op.drop_index("ix_study_plans_window", table_name="study_plans")
    op.drop_table("study_plans")
    op.drop_table("planning_preferences")
    op.drop_index("ix_fixed_commitments_time", table_name="fixed_commitments")
    op.drop_table("fixed_commitments")
    op.drop_index("ix_assessments_fact_state", table_name="assessments")
    op.drop_index("ix_assessments_course_due", table_name="assessments")
    op.drop_table("assessments")
    op.drop_index("ix_courses_term_code", table_name="courses")
    op.drop_table("courses")
