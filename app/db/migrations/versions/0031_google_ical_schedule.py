"""Add external calendar source metadata for the academic schedule.

Revision ID: 0031_google_ical_schedule
Revises: 0030_learn_persistence
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0031_google_ical_schedule"
down_revision = "0030_learn_persistence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = (
        sa.Column("source_kind", sa.String(32), nullable=False, server_default="notion"),
        sa.Column("external_source_id", sa.String(255)),
    )
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("academic_course_calendars", recreate="always") as batch:
            for column in columns:
                batch.add_column(column)
            batch.create_check_constraint(
                "source_kind_valid", "source_kind IN ('notion','google_ical')"
            )
            batch.create_check_constraint(
                "external_source_id_nonempty",
                "external_source_id IS NULL OR length(external_source_id) > 0",
            )
            batch.create_unique_constraint(
                "uq_academic_course_calendars_external_source", ["external_source_id"]
            )
    else:
        for column in columns:
            op.add_column("academic_course_calendars", column)
        op.create_check_constraint(
            "source_kind_valid",
            "academic_course_calendars",
            "source_kind IN ('notion','google_ical')",
        )
        op.create_check_constraint(
            "external_source_id_nonempty",
            "academic_course_calendars",
            "external_source_id IS NULL OR length(external_source_id) > 0",
        )
        op.create_unique_constraint(
            "uq_academic_course_calendars_external_source",
            "academic_course_calendars",
            ["external_source_id"],
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("academic_course_calendars", recreate="always") as batch:
            batch.drop_constraint("uq_academic_course_calendars_external_source", type_="unique")
            batch.drop_constraint("external_source_id_nonempty", type_="check")
            batch.drop_constraint("source_kind_valid", type_="check")
            batch.drop_column("external_source_id")
            batch.drop_column("source_kind")
    else:
        op.drop_constraint(
            "uq_academic_course_calendars_external_source",
            "academic_course_calendars",
            type_="unique",
        )
        op.drop_constraint(
            "external_source_id_nonempty",
            "academic_course_calendars",
            type_="check",
        )
        op.drop_constraint(
            "source_kind_valid",
            "academic_course_calendars",
            type_="check",
        )
        op.drop_column("academic_course_calendars", "external_source_id")
        op.drop_column("academic_course_calendars", "source_kind")
