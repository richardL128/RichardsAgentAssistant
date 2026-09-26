"""Add canonical action-item contracts and typed career applications.

Revision ID: 0034_action_item_contracts
Revises: 0033_grounded_morning_summaries
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
from alembic import op

revision = "0034_action_item_contracts"
down_revision = "0033_grounded_morning_summaries"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=True)
_TS = sa.DateTime(timezone=True)
_JSON_OBJECT = sa.text("'{}'")

_DOMAIN_VALID = (
    "domain IS NULL OR domain IN ('academic','career','personal','administrative','project')"
)
_ITEM_KIND_VALID = (
    "item_kind IN "
    "('task','assignment','quiz','exam','lab','tutorial','meeting','event',"
    "'application_follow_up','interview_prep','deadline','needs_review')"
)
_STATUS_VALID = "status IN ('inbox','needs_review','to_do','in_progress','waiting','done','canceled')"
_SOURCE_KIND_VALID = (
    "source_kind IN "
    "('notion_action_items','notion_applications','notion_interviews','learn',"
    "'google_calendar','manual','legacy_notion_course_assessment')"
)
_DATE_PRECISION_VALID = "date_precision IS NULL OR date_precision IN ('date','datetime')"
_TEMPORAL_SHAPE_VALID = (
    "("
    "date_precision IS NULL AND start_date IS NULL AND end_date_exclusive IS NULL "
    "AND start_at IS NULL AND end_at IS NULL"
    ") OR ("
    "date_precision = 'date' AND start_date IS NOT NULL AND start_at IS NULL "
    "AND end_at IS NULL AND (end_date_exclusive IS NULL OR end_date_exclusive > start_date)"
    ") OR ("
    "date_precision = 'datetime' AND start_date IS NULL AND end_date_exclusive IS NULL "
    "AND start_at IS NOT NULL AND timezone IS NOT NULL "
    "AND (end_at IS NULL OR end_at > start_at)"
    ")"
)
_APPLICATION_DEADLINE_TEMPORAL_SHAPE_VALID = (
    "("
    "deadline_date_precision IS NULL AND deadline_start_date IS NULL "
    "AND deadline_start_at IS NULL AND deadline_timezone IS NULL"
    ") OR ("
    "deadline_date_precision = 'date' AND deadline_start_date IS NOT NULL "
    "AND deadline_start_at IS NULL"
    ") OR ("
    "deadline_date_precision = 'datetime' AND deadline_start_date IS NULL "
    "AND deadline_start_at IS NOT NULL AND deadline_timezone IS NOT NULL"
    ")"
)
_APPLICATION_NEXT_ACTION_TEMPORAL_SHAPE_VALID = (
    "("
    "next_action_date_precision IS NULL AND next_action_start_date IS NULL "
    "AND next_action_start_at IS NULL AND next_action_timezone IS NULL"
    ") OR ("
    "next_action_date_precision = 'date' AND next_action_start_date IS NOT NULL "
    "AND next_action_start_at IS NULL"
    ") OR ("
    "next_action_date_precision = 'datetime' AND next_action_start_date IS NULL "
    "AND next_action_start_at IS NOT NULL AND next_action_timezone IS NOT NULL"
    ")"
)

def _assessment_columns() -> tuple[sa.Column[Any], ...]:
    return (
        sa.Column("domain", sa.String(32)),
        sa.Column(
            "item_kind",
            sa.String(64),
            nullable=False,
            server_default="needs_review",
        ),
        sa.Column("status", sa.String(32), nullable=False, server_default="to_do"),
        sa.Column("start_date", sa.Date()),
        sa.Column("end_date_exclusive", sa.Date()),
        sa.Column("start_at", _TS),
        sa.Column("end_at", _TS),
        sa.Column("date_precision", sa.String(32)),
        sa.Column("timezone", sa.String(64)),
        sa.Column(
            "source_kind",
            sa.String(64),
            nullable=False,
            server_default="legacy_notion_course_assessment",
        ),
        sa.Column("source_label", sa.String(255)),
        sa.Column("context", sa.JSON(), nullable=False, server_default=_JSON_OBJECT),
        sa.Column("notion_database_id", sa.String(255)),
        sa.Column("notion_data_source_id", sa.String(255)),
        sa.Column("notion_page_id", sa.String(255)),
        sa.Column("notion_title_property_id", sa.String(255)),
        sa.Column("notion_date_property_id", sa.String(255)),
        sa.Column("notion_domain_property_id", sa.String(255)),
        sa.Column("notion_status_property_id", sa.String(255)),
        sa.Column("notion_kind_property_id", sa.String(255)),
        sa.Column("application_id", _UUID),
        sa.Column("interview_id", _UUID),
    )


_ASSESSMENT_COLUMNS = _assessment_columns()

_ASSESSMENT_CHECKS = {
    "action_domain_valid": _DOMAIN_VALID,
    "item_kind_valid": _ITEM_KIND_VALID,
    "action_status_valid": _STATUS_VALID,
    "action_source_kind_valid": _SOURCE_KIND_VALID,
    "action_date_precision_valid": _DATE_PRECISION_VALID,
    "action_temporal_shape_valid": _TEMPORAL_SHAPE_VALID,
}


def upgrade() -> None:
    _create_career_applications()
    _upgrade_assessments()
    _upgrade_interview_application_links()
    _backfill_assessments(_owner_timezone())


def downgrade() -> None:
    _drop_interview_application_link_columns()
    _drop_assessment_columns()
    op.drop_table("career_applications")


def _create_career_applications() -> None:
    op.create_table(
        "career_applications",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("workspace_id", _UUID, sa.ForeignKey("career_jobs_workspaces.id", ondelete="SET NULL")),
        sa.Column(
            "legacy_application_row_id",
            _UUID,
            sa.ForeignKey("career_application_rows.id", ondelete="SET NULL"),
        ),
        sa.Column("application_page_id", sa.String(255)),
        sa.Column("applications_database_id", sa.String(255)),
        sa.Column("applications_data_source_id", sa.String(255)),
        sa.Column(
            "source_kind",
            sa.String(64),
            nullable=False,
            server_default="notion_applications",
        ),
        sa.Column("company_name", sa.String(255)),
        sa.Column("role_title", sa.String(500)),
        sa.Column("status", sa.String(128)),
        sa.Column("deadline_date_precision", sa.String(32)),
        sa.Column("deadline_start_date", sa.Date()),
        sa.Column("deadline_start_at", _TS),
        sa.Column("deadline_timezone", sa.String(64)),
        sa.Column("next_action", sa.String(1_000)),
        sa.Column("next_action_status", sa.String(32)),
        sa.Column("next_action_date_precision", sa.String(32)),
        sa.Column("next_action_start_date", sa.Date()),
        sa.Column("next_action_start_at", _TS),
        sa.Column("next_action_timezone", sa.String(64)),
        sa.Column("posting_url", sa.String(2_048)),
        sa.Column("applied_on", sa.Date()),
        sa.Column("next_action_date", sa.Date()),
        sa.Column("last_activity_at", _TS),
        sa.Column("notion_last_edited_at", _TS),
        sa.Column("source_url", sa.String(2_048)),
        sa.Column("source", sa.String(255)),
        sa.Column("location", sa.String(255)),
        sa.Column("contact_name", sa.String(255)),
        sa.Column("contact_email", sa.String(255)),
        sa.Column("notes", sa.Text()),
        sa.Column("property_snapshot", sa.JSON(), nullable=False, server_default=_JSON_OBJECT),
        sa.Column("content_fingerprint", sa.String(128)),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("application_page_id", name="uq_career_applications_page"),
        sa.CheckConstraint(
            "source_kind IN ('notion_applications','manual')",
            name="source_kind_valid",
        ),
        sa.CheckConstraint("status IS NULL OR length(status) > 0", name="status_nonempty"),
        sa.CheckConstraint(
            "next_action_status IS NULL OR "
            "next_action_status IN ('inbox','needs_review','to_do','in_progress','waiting',"
            "'done','canceled')",
            name="next_action_status_valid",
        ),
        sa.CheckConstraint(
            _APPLICATION_DEADLINE_TEMPORAL_SHAPE_VALID,
            name="deadline_temporal_shape_valid",
        ),
        sa.CheckConstraint(
            _APPLICATION_NEXT_ACTION_TEMPORAL_SHAPE_VALID,
            name="next_action_temporal_shape_valid",
        ),
    )
    op.create_index(
        "ix_career_applications_company_active",
        "career_applications",
        ["company_name", "active"],
    )
    op.create_index(
        "ix_career_applications_status_active",
        "career_applications",
        ["status", "active"],
    )
    op.create_index(
        "ix_career_applications_legacy_row",
        "career_applications",
        ["legacy_application_row_id"],
    )


def _upgrade_assessments() -> None:
    existing_columns = _columns("assessments")
    existing_checks = _checks("assessments")
    recreate = "always" if op.get_bind().dialect.name == "sqlite" else "auto"
    with op.batch_alter_table("assessments", recreate=recreate) as batch:
        if "course_id" in existing_columns:
            batch.alter_column("course_id", existing_type=_UUID, nullable=True)
        for column in _assessment_columns():
            if column.name not in existing_columns:
                batch.add_column(column)
        if "application_id" not in existing_columns:
            batch.create_foreign_key(
                "fk_assessments_application_id_career_applications",
                "career_applications",
                ["application_id"],
                ["id"],
                ondelete="SET NULL",
            )
        if "interview_id" not in existing_columns:
            batch.create_foreign_key(
                "fk_assessments_interview_id_career_interview_events",
                "career_interview_events",
                ["interview_id"],
                ["id"],
                ondelete="SET NULL",
            )
        for name, sql in _ASSESSMENT_CHECKS.items():
            if name not in existing_checks:
                batch.create_check_constraint(name, sql)
    _create_index(
        "ix_assessments_domain_status_temporal",
        "assessments",
        ["domain", "status", "start_date", "start_at"],
    )
    _create_index("ix_assessments_application", "assessments", ["application_id", "status"])
    _create_index("ix_assessments_interview", "assessments", ["interview_id", "status"])


def _upgrade_interview_application_links() -> None:
    existing_columns = _columns("career_interview_application_links")
    if "application_id" not in existing_columns:
        with op.batch_alter_table("career_interview_application_links") as batch:
            batch.add_column(sa.Column("application_id", _UUID))
            batch.create_foreign_key(
                "fk_career_interview_links_application_id_applications",
                "career_applications",
                ["application_id"],
                ["id"],
                ondelete="SET NULL",
            )
    _create_index(
        "ix_career_interview_links_application",
        "career_interview_application_links",
        ["application_id", "state"],
    )


def _backfill_assessments(owner_timezone: ZoneInfo) -> None:
    assessments = sa.table(
        "assessments",
        sa.column("id"),
        sa.column("assessment_type"),
        sa.column("due_at"),
        sa.column("ends_at"),
        sa.column("completed"),
        sa.column("is_all_day"),
        sa.column("fact_state"),
        sa.column("source_id"),
        sa.column("source_scope"),
        sa.column("notion_id"),
        sa.column("title_property_id"),
        sa.column("domain"),
        sa.column("item_kind"),
        sa.column("status"),
        sa.column("start_date"),
        sa.column("end_date_exclusive"),
        sa.column("start_at"),
        sa.column("end_at"),
        sa.column("date_precision"),
        sa.column("timezone"),
        sa.column("source_kind"),
        sa.column("source_label"),
        sa.column("context", sa.JSON()),
        sa.column("notion_data_source_id"),
        sa.column("notion_page_id"),
        sa.column("notion_title_property_id"),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(
            assessments.c.id,
            assessments.c.assessment_type,
            assessments.c.due_at,
            assessments.c.ends_at,
            assessments.c.completed,
            assessments.c.is_all_day,
            assessments.c.fact_state,
            assessments.c.source_id,
            assessments.c.source_scope,
            assessments.c.notion_id,
            assessments.c.title_property_id,
        )
    )
    for row in rows.mappings():
        due_at = _legacy_utc(row["due_at"])
        ends_at = _legacy_utc(row["ends_at"])
        item_kind = _item_kind(row["assessment_type"])
        needs_domain_review = item_kind in {"task", "needs_review"}
        domain = None if needs_domain_review else "academic"
        status = _status(row["completed"], row["fact_state"], needs_domain_review)
        values: dict[str, Any] = {
            "domain": domain,
            "item_kind": item_kind,
            "status": status,
            "source_kind": _source_kind(row["source_scope"], item_kind),
            "source_label": (
                "Legacy misc calendar; domain review required"
                if needs_domain_review
                else "Legacy academic assessment calendar"
            ),
            "context": {
                "legacy_assessment_type": str(row["assessment_type"] or ""),
                "legacy_fact_state": str(row["fact_state"] or ""),
                "domain_review_required": needs_domain_review,
            },
            "notion_data_source_id": row["source_id"],
            "notion_page_id": row["notion_id"],
            "notion_title_property_id": row["title_property_id"],
        }
        if due_at is None:
            values.update(
                {
                    "date_precision": None,
                    "start_date": None,
                    "end_date_exclusive": None,
                    "start_at": None,
                    "end_at": None,
                    "timezone": None,
                }
            )
        elif bool(row["is_all_day"]):
            start_date = due_at.astimezone(owner_timezone).date()
            end_date = ends_at.astimezone(owner_timezone).date() if ends_at is not None else None
            values.update(
                {
                    "date_precision": "date",
                    "start_date": start_date,
                    "end_date_exclusive": end_date if end_date is not None and end_date > start_date else None,
                    "start_at": None,
                    "end_at": None,
                    "timezone": None,
                }
            )
        else:
            values.update(
                {
                    "date_precision": "datetime",
                    "start_date": None,
                    "end_date_exclusive": None,
                    "start_at": due_at,
                    "end_at": ends_at if ends_at is not None and ends_at > due_at else None,
                    "timezone": owner_timezone.key,
                }
            )
        connection.execute(
            assessments.update().where(assessments.c.id == row["id"]).values(**values)
        )


def _drop_interview_application_link_columns() -> None:
    with op.batch_alter_table("career_interview_application_links") as batch:
        batch.drop_index("ix_career_interview_links_application")
        batch.drop_constraint(
            "fk_career_interview_links_application_id_applications",
            type_="foreignkey",
        )
        batch.drop_column("application_id")


def _drop_assessment_columns() -> None:
    with op.batch_alter_table("assessments", recreate="always") as batch:
        batch.drop_index("ix_assessments_interview")
        batch.drop_index("ix_assessments_application")
        batch.drop_index("ix_assessments_domain_status_temporal")
        batch.drop_constraint(
            "fk_assessments_interview_id_career_interview_events",
            type_="foreignkey",
        )
        batch.drop_constraint(
            "fk_assessments_application_id_career_applications",
            type_="foreignkey",
        )
        for name in _ASSESSMENT_CHECKS:
            batch.drop_constraint(name, type_="check")
        for column in reversed(_ASSESSMENT_COLUMNS):
            batch.drop_column(column.name)
        batch.alter_column("course_id", existing_type=_UUID, nullable=False)


def _owner_timezone() -> ZoneInfo:
    raw = os.environ.get("APP_TIMEZONE", "America/Toronto")
    try:
        return ZoneInfo(raw)
    except ZoneInfoNotFoundError:
        return ZoneInfo("America/Toronto")


def _legacy_utc(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _item_kind(value: object) -> str:
    raw = str(value or "").casefold()
    if raw in {
        "task",
        "assignment",
        "quiz",
        "exam",
        "tutorial",
        "lab",
        "meeting",
        "event",
        "application_follow_up",
        "interview_prep",
        "deadline",
        "needs_review",
    }:
        return raw
    return "needs_review"


def _status(completed: object, fact_state: object, needs_domain_review: bool) -> str:
    if bool(completed):
        return "done"
    if str(fact_state or "").casefold() == "rejected":
        return "canceled"
    if needs_domain_review or str(fact_state or "").casefold() == "ambiguous":
        return "needs_review"
    return "to_do"


def _source_kind(source_scope: object, item_kind: str) -> str:
    raw_scope = str(source_scope or "").casefold()
    if raw_scope.startswith("google_ical:"):
        return "google_calendar"
    if item_kind in {"task", "needs_review"}:
        return "notion_action_items"
    return "legacy_notion_course_assessment"


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _checks(table_name: str) -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(table_name)
        if constraint.get("name")
    }


def _indexes(table_name: str) -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
        if index.get("name")
    }


def _create_index(name: str, table_name: str, columns: list[str]) -> None:
    if name not in _indexes(table_name):
        op.create_index(name, table_name, columns)
