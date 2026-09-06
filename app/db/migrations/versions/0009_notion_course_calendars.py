"""Add Notion course calendar discovery and clarification state.

Revision ID: 0009_notion_course_calendars
Revises: 0008_phase6_finance_allowlist
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_notion_course_calendars"
down_revision = "0008_phase6_finance_allowlist"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=True)
_TS = sa.DateTime(timezone=True)
_JSON_LIST = sa.text("'[]'")


def upgrade() -> None:
    op.add_column("assessments", sa.Column("source_id", sa.String(255)))
    op.add_column("assessments", sa.Column("source_scope", sa.String(255)))
    op.add_column("assessments", sa.Column("notion_last_edited_at", _TS))
    op.add_column("assessments", sa.Column("title_property_id", sa.String(255)))
    op.add_column("assessments", sa.Column("label_source", sa.String(255)))
    op.add_column(
        "assessments",
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "assessments",
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_assessments_source_active", "assessments", ["source_id", "active"])

    op.create_table(
        "academic_course_calendars",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "course_id",
            _UUID,
            sa.ForeignKey("courses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("course_page_id", sa.String(255), nullable=False),
        sa.Column("child_database_id", sa.String(255)),
        sa.Column("child_data_source_id", sa.String(255)),
        sa.Column("title_property_id", sa.String(255)),
        sa.Column("title_property_name", sa.String(255)),
        sa.Column("date_property_id", sa.String(255)),
        sa.Column("date_property_name", sa.String(255)),
        sa.Column("discovery_status", sa.String(32), nullable=False, server_default="valid"),
        sa.Column("diagnostic_code", sa.String(128)),
        sa.Column("diagnostic_fingerprint", sa.String(128)),
        sa.Column("last_discovered_at", _TS),
        sa.Column("last_synced_at", _TS),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("length(course_page_id) > 0", name="course_page_id_nonempty"),
        sa.CheckConstraint(
            "child_database_id IS NULL OR length(child_database_id) > 0",
            name="child_database_id_nonempty",
        ),
        sa.CheckConstraint(
            "child_data_source_id IS NULL OR length(child_data_source_id) > 0",
            name="child_data_source_id_nonempty",
        ),
        sa.CheckConstraint(
            "discovery_status IN ('valid','missing','inaccessible','malformed','duplicate')",
            name="discovery_status_valid",
        ),
        sa.UniqueConstraint("course_id", name="uq_academic_course_calendars_course"),
        sa.UniqueConstraint(
            "child_data_source_id",
            name="uq_academic_course_calendars_source",
        ),
    )
    op.create_index(
        "ix_academic_course_calendars_status",
        "academic_course_calendars",
        ["discovery_status", "last_synced_at"],
    )

    op.create_table(
        "academic_clarifications",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("course_id", _UUID, sa.ForeignKey("courses.id", ondelete="SET NULL")),
        sa.Column("assessment_id", _UUID, sa.ForeignKey("assessments.id", ondelete="SET NULL")),
        sa.Column("event_notion_id", sa.String(255), nullable=False),
        sa.Column("original_title", sa.String(1_024), nullable=False),
        sa.Column("raw_label", sa.String(1_024)),
        sa.Column("quiz_preview_title", sa.String(1_024), nullable=False),
        sa.Column("assignment_preview_title", sa.String(1_024), nullable=False),
        sa.Column("expected_edited_at", _TS, nullable=False),
        sa.Column("title_property_id", sa.String(255)),
        sa.Column("idempotency_key", sa.String(512), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("delivery_id", sa.String(255)),
        sa.Column("delivered_at", _TS),
        sa.Column("decision", sa.String(32)),
        sa.Column("decision_user_id", sa.BigInteger),
        sa.Column("decision_at", _TS),
        sa.Column("write_status", sa.String(32), nullable=False, server_default="none"),
        sa.Column("write_error_code", sa.String(128)),
        sa.Column("expires_at", _TS, nullable=False),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("length(event_notion_id) > 0", name="event_notion_id_nonempty"),
        sa.CheckConstraint(
            "state IN "
            "('pending','delivered','claimed','ignored','applied','conflict','failed','expired')",
            name="state_valid",
        ),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN ('quiz','assignment','ignore')",
            name="decision_valid",
        ),
        sa.CheckConstraint(
            "write_status IN ('none','skipped','pending','applied','conflict','failed')",
            name="write_status_valid",
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_academic_clarifications_idempotency"),
    )
    op.create_index(
        "ix_academic_clarifications_state_expiry",
        "academic_clarifications",
        ["state", "expires_at"],
    )
    op.create_index(
        "ix_academic_clarifications_event",
        "academic_clarifications",
        ["event_notion_id", "expected_edited_at"],
    )

    op.create_table(
        "academic_setup_reminders",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("condition", sa.String(128), nullable=False),
        sa.Column("schema_fingerprint", sa.String(128), nullable=False),
        sa.Column("reminder_day", sa.Date, nullable=False),
        sa.Column("affected_course_codes", sa.JSON, nullable=False, server_default=_JSON_LIST),
        sa.Column("state", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("delivered_at", _TS),
        sa.Column("delivery_id", sa.String(255)),
        sa.Column("error_code", sa.String(128)),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("length(condition) > 0", name="condition_nonempty"),
        sa.CheckConstraint("length(schema_fingerprint) > 0", name="schema_fingerprint_nonempty"),
        sa.CheckConstraint(
            "state IN ('pending','delivered','failed','cleared')",
            name="state_valid",
        ),
        sa.UniqueConstraint(
            "condition",
            "schema_fingerprint",
            "reminder_day",
            name="uq_academic_setup_reminders_condition_day",
        ),
    )
    op.create_index(
        "ix_academic_setup_reminders_condition",
        "academic_setup_reminders",
        ["condition", "state"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_academic_setup_reminders_condition",
        table_name="academic_setup_reminders",
    )
    op.drop_table("academic_setup_reminders")
    op.drop_index("ix_academic_clarifications_event", table_name="academic_clarifications")
    op.drop_index(
        "ix_academic_clarifications_state_expiry",
        table_name="academic_clarifications",
    )
    op.drop_table("academic_clarifications")
    op.drop_index(
        "ix_academic_course_calendars_status",
        table_name="academic_course_calendars",
    )
    op.drop_table("academic_course_calendars")
    op.drop_index("ix_assessments_source_active", table_name="assessments")
    op.drop_column("assessments", "archived")
    op.drop_column("assessments", "active")
    op.drop_column("assessments", "label_source")
    op.drop_column("assessments", "title_property_id")
    op.drop_column("assessments", "notion_last_edited_at")
    op.drop_column("assessments", "source_scope")
    op.drop_column("assessments", "source_id")
