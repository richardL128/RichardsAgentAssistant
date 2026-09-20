"""Add LEARN persistence tables.

Revision ID: 0030_learn_persistence
Revises: 0029_context_memory_persistence
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0030_learn_persistence"
down_revision = "0029_context_memory_persistence"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=True)
_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column(
        "academic_course_calendars",
        sa.Column("learn_context_property_id", sa.String(255)),
    )
    op.add_column(
        "academic_course_calendars",
        sa.Column("learn_context_property_name", sa.String(255)),
    )

    op.create_table(
        "learn_courses",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("org_unit_id", sa.String(128), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("term", sa.String(128)),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("url", sa.String(2048)),
        sa.Column("first_seen_at", _TS, nullable=False),
        sa.Column("last_seen_at", _TS, nullable=False),
        sa.Column("disappeared_at", _TS),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("org_unit_id", name="uq_learn_courses_org_unit"),
        sa.CheckConstraint("length(org_unit_id) > 0", name="org_unit_id_nonempty"),
        sa.CheckConstraint("length(code) > 0", name="code_nonempty"),
        sa.CheckConstraint("length(name) > 0", name="name_nonempty"),
        sa.CheckConstraint("url IS NULL OR length(url) > 0", name="url_nonempty"),
    )
    op.create_index("ix_learn_courses_active_code", "learn_courses", ["active", "code"])
    op.create_index("ix_learn_courses_term_code", "learn_courses", ["term", "code"])

    op.create_table(
        "learn_scheduled_items",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("source_id", sa.String(255), nullable=False),
        sa.Column("course_id", _UUID, nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("start_at", _TS),
        sa.Column("due_at", _TS),
        sa.Column("end_at", _TS),
        sa.Column("date_precision", sa.String(32), nullable=False),
        sa.Column("completion_state", sa.String(32), nullable=False),
        sa.Column("url", sa.String(2048)),
        sa.Column("fingerprint", sa.String(128), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("first_seen_at", _TS, nullable=False),
        sa.Column("last_seen_at", _TS, nullable=False),
        sa.Column("disappeared_at", _TS),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["course_id"], ["learn_courses.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("source_id", name="uq_learn_scheduled_items_source"),
        sa.UniqueConstraint(
            "source_id",
            "fingerprint",
            name="uq_learn_scheduled_items_fingerprint",
        ),
        sa.CheckConstraint("length(source_id) > 0", name="source_id_nonempty"),
        sa.CheckConstraint("length(title) > 0", name="title_nonempty"),
        sa.CheckConstraint("length(fingerprint) > 0", name="fingerprint_nonempty"),
        sa.CheckConstraint("date_precision IN ('date','datetime')", name="date_precision_valid"),
        sa.CheckConstraint(
            "completion_state IN ('unknown','incomplete','complete','cancelled')",
            name="completion_state_valid",
        ),
        sa.CheckConstraint(
            "end_at IS NULL OR start_at IS NULL OR end_at >= start_at",
            name="range_valid",
        ),
    )
    op.create_index(
        "ix_learn_scheduled_items_course_start",
        "learn_scheduled_items",
        ["course_id", "start_date", "start_at"],
    )
    op.create_index(
        "ix_learn_scheduled_items_active",
        "learn_scheduled_items",
        ["active", "start_date"],
    )

    op.create_table(
        "learn_announcement_sources",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("source_id", sa.String(255), nullable=False),
        sa.Column("course_id", _UUID, nullable=False),
        sa.Column("published_at", _TS),
        sa.Column("content_updated_at", _TS),
        sa.Column("effective_at", _TS, nullable=False),
        sa.Column("url", sa.String(2048)),
        sa.Column("fingerprint", sa.String(128), nullable=False),
        sa.Column("visible", sa.Boolean(), nullable=False),
        sa.Column("has_attachments", sa.Boolean(), nullable=False),
        sa.Column("first_seen_at", _TS, nullable=False),
        sa.Column("last_seen_at", _TS, nullable=False),
        sa.Column("disappeared_at", _TS),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["course_id"], ["learn_courses.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("source_id", name="uq_learn_announcements_source"),
        sa.UniqueConstraint(
            "source_id",
            "fingerprint",
            name="uq_learn_announcements_fingerprint",
        ),
        sa.CheckConstraint("length(source_id) > 0", name="source_id_nonempty"),
        sa.CheckConstraint("length(fingerprint) > 0", name="fingerprint_nonempty"),
        sa.CheckConstraint("url IS NULL OR length(url) > 0", name="url_nonempty"),
    )
    op.create_index(
        "ix_learn_announcements_course_effective",
        "learn_announcement_sources",
        ["course_id", "effective_at"],
    )
    op.create_index(
        "ix_learn_announcements_visible_effective",
        "learn_announcement_sources",
        ["visible", "effective_at"],
    )

    op.create_table(
        "learn_announcement_semantic_results",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("announcement_id", _UUID, nullable=False),
        sa.Column("course_id", _UUID, nullable=False),
        sa.Column("source_fingerprint", sa.String(128), nullable=False),
        sa.Column("summary", sa.String(1600), nullable=False),
        sa.Column("why_it_matters", sa.String(1600), nullable=False),
        sa.Column("action_items", sa.JSON(), nullable=False),
        sa.Column("evidence_fragments", sa.JSON(), nullable=False),
        sa.Column("source_url", sa.String(2048)),
        sa.Column("model_identity", sa.String(255), nullable=False),
        sa.Column("prompt_version", sa.String(128), nullable=False),
        sa.Column("critic_model_identity", sa.String(255)),
        sa.Column("critic_prompt_version", sa.String(128)),
        sa.Column("repair_attempted", sa.Boolean(), nullable=False),
        sa.Column("anti_copy_passed", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error_code", sa.String(128)),
        sa.Column("interpreted_at", _TS, nullable=False),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(
            ["announcement_id"],
            ["learn_announcement_sources.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["course_id"], ["learn_courses.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "announcement_id",
            "source_fingerprint",
            "prompt_version",
            name="uq_learn_semantic_announcement_fingerprint_prompt",
        ),
        sa.CheckConstraint(
            "length(source_fingerprint) > 0",
            name="source_fingerprint_nonempty",
        ),
        sa.CheckConstraint("length(summary) > 0", name="summary_nonempty"),
        sa.CheckConstraint("length(why_it_matters) > 0", name="why_it_matters_nonempty"),
        sa.CheckConstraint("length(model_identity) > 0", name="model_identity_nonempty"),
        sa.CheckConstraint("length(prompt_version) > 0", name="prompt_version_nonempty"),
        sa.CheckConstraint(
            "status IN ('valid','summary_unavailable','invalid','superseded','source_removed')",
            name="status_valid",
        ),
    )
    op.create_index(
        "ix_learn_semantic_status",
        "learn_announcement_semantic_results",
        ["status", "interpreted_at"],
    )
    op.create_index(
        "ix_learn_semantic_announcement_status",
        "learn_announcement_semantic_results",
        ["announcement_id", "status"],
    )

    op.create_table(
        "learn_dated_implications",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("semantic_result_id", _UUID, nullable=False),
        sa.Column("announcement_id", _UUID, nullable=False),
        sa.Column("course_id", _UUID, nullable=False),
        sa.Column("source_fingerprint", sa.String(128), nullable=False),
        sa.Column("implication_key", sa.String(128), nullable=False),
        sa.Column("activity_type", sa.String(128), nullable=False),
        sa.Column("academic_date", sa.Date(), nullable=False),
        sa.Column("start_at", _TS),
        sa.Column("end_at", _TS),
        sa.Column("date_precision", sa.String(32), nullable=False),
        sa.Column("reminder_date", sa.Date(), nullable=False),
        sa.Column("evidence_fragments", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(
            ["semantic_result_id"],
            ["learn_announcement_semantic_results.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["announcement_id"],
            ["learn_announcement_sources.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["course_id"], ["learn_courses.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "semantic_result_id",
            "implication_key",
            name="uq_learn_dated_implications_result_key",
        ),
        sa.CheckConstraint("length(implication_key) > 0", name="implication_key_nonempty"),
        sa.CheckConstraint("length(activity_type) > 0", name="activity_type_nonempty"),
        sa.CheckConstraint("date_precision IN ('date','datetime')", name="date_precision_valid"),
        sa.CheckConstraint(
            "end_at IS NULL OR start_at IS NULL OR end_at >= start_at",
            name="range_valid",
        ),
        sa.CheckConstraint(
            "status IN ('active','superseded','source_removed')", name="status_valid"
        ),
    )
    op.create_index(
        "ix_learn_dated_implications_reminder",
        "learn_dated_implications",
        ["status", "reminder_date"],
    )
    op.create_index(
        "ix_learn_dated_implications_course_date",
        "learn_dated_implications",
        ["course_id", "academic_date"],
    )

    op.create_table(
        "learn_notification_deliveries",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("message_key", sa.String(512), nullable=False),
        sa.Column("delivery_kind", sa.String(64), nullable=False),
        sa.Column("occurrence_date", sa.Date(), nullable=False),
        sa.Column("scheduled_for", _TS),
        sa.Column("channel", sa.String(64), nullable=False),
        sa.Column("target", sa.String(255)),
        sa.Column("announcement_id", _UUID),
        sa.Column("dated_implication_id", _UUID),
        sa.Column("source_fingerprint", sa.String(128)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("sent_at", _TS),
        sa.Column("external_message_id", sa.String(255)),
        sa.Column("error_code", sa.String(128)),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(
            ["announcement_id"],
            ["learn_announcement_sources.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["dated_implication_id"],
            ["learn_dated_implications.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("message_key", name="uq_learn_notification_deliveries_message"),
        sa.CheckConstraint("length(message_key) > 0", name="message_key_nonempty"),
        sa.CheckConstraint(
            "delivery_kind IN ('announcement_window','day_before','reconnect_alert')",
            name="delivery_kind_valid",
        ),
        sa.CheckConstraint(
            "status IN ('pending','sent','failed','superseded')", name="status_valid"
        ),
    )
    op.create_index(
        "ix_learn_notification_delivery_occurrence",
        "learn_notification_deliveries",
        ["delivery_kind", "occurrence_date", "status"],
    )
    op.create_index(
        "ix_learn_notification_delivery_source",
        "learn_notification_deliveries",
        ["announcement_id", "source_fingerprint"],
    )

    op.create_table(
        "learn_notion_proposal_links",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("source_kind", sa.String(64), nullable=False),
        sa.Column("source_id", sa.String(255), nullable=False),
        sa.Column("source_fingerprint", sa.String(128), nullable=False),
        sa.Column("scheduled_item_id", _UUID),
        sa.Column("dated_implication_id", _UUID),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(512), nullable=False),
        sa.Column("proposal_id", _UUID),
        sa.Column("reserved_calendar_notion_id", sa.String(255), nullable=False),
        sa.Column("target_notion_page_id", sa.String(255)),
        sa.Column("target_expected_title", sa.String(500)),
        sa.Column("target_expected_date", sa.Date()),
        sa.Column("target_expected_start_at", _TS),
        sa.Column("target_expected_last_edited_at", _TS),
        sa.Column("learn_context_property_id", sa.String(255), nullable=False),
        sa.Column("learn_context_property_name", sa.String(255), nullable=False),
        sa.Column("explicit_request_key", sa.String(255)),
        sa.Column("conflict_code", sa.String(128)),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(
            ["scheduled_item_id"],
            ["learn_scheduled_items.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["dated_implication_id"],
            ["learn_dated_implications.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["academic_proposed_changes.id"],
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_learn_proposal_links_idempotency"),
        sa.CheckConstraint("length(source_id) > 0", name="source_id_nonempty"),
        sa.CheckConstraint(
            "length(source_fingerprint) > 0",
            name="source_fingerprint_nonempty",
        ),
        sa.CheckConstraint(
            "source_kind IN ('scheduled_item','announcement_implication')",
            name="source_kind_valid",
        ),
        sa.CheckConstraint(
            "operation IN ('create_learn_calendar_event','enrich_learn_calendar_event')",
            name="operation_valid",
        ),
        sa.CheckConstraint(
            "state IN ('pending','confirmed','rejected','expired','applied','superseded','skipped')",
            name="state_valid",
        ),
    )
    op.create_index(
        "ix_learn_proposal_links_source",
        "learn_notion_proposal_links",
        ["source_kind", "source_id", "source_fingerprint", "operation", "state"],
    )
    op.create_index(
        "ix_learn_proposal_links_proposal",
        "learn_notion_proposal_links",
        ["proposal_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_learn_proposal_links_proposal", table_name="learn_notion_proposal_links")
    op.drop_index("ix_learn_proposal_links_source", table_name="learn_notion_proposal_links")
    op.drop_table("learn_notion_proposal_links")
    op.drop_index(
        "ix_learn_notification_delivery_source",
        table_name="learn_notification_deliveries",
    )
    op.drop_index(
        "ix_learn_notification_delivery_occurrence",
        table_name="learn_notification_deliveries",
    )
    op.drop_table("learn_notification_deliveries")
    op.drop_index(
        "ix_learn_dated_implications_course_date",
        table_name="learn_dated_implications",
    )
    op.drop_index(
        "ix_learn_dated_implications_reminder",
        table_name="learn_dated_implications",
    )
    op.drop_table("learn_dated_implications")
    op.drop_index(
        "ix_learn_semantic_announcement_status",
        table_name="learn_announcement_semantic_results",
    )
    op.drop_index(
        "ix_learn_semantic_status",
        table_name="learn_announcement_semantic_results",
    )
    op.drop_table("learn_announcement_semantic_results")
    op.drop_index(
        "ix_learn_announcements_visible_effective",
        table_name="learn_announcement_sources",
    )
    op.drop_index(
        "ix_learn_announcements_course_effective",
        table_name="learn_announcement_sources",
    )
    op.drop_table("learn_announcement_sources")
    op.drop_index("ix_learn_scheduled_items_active", table_name="learn_scheduled_items")
    op.drop_index("ix_learn_scheduled_items_course_start", table_name="learn_scheduled_items")
    op.drop_table("learn_scheduled_items")
    op.drop_index("ix_learn_courses_term_code", table_name="learn_courses")
    op.drop_index("ix_learn_courses_active_code", table_name="learn_courses")
    op.drop_table("learn_courses")
    op.drop_column("academic_course_calendars", "learn_context_property_name")
    op.drop_column("academic_course_calendars", "learn_context_property_id")
