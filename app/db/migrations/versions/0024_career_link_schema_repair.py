"""Repair career interview-link columns missing from older 0020 installs.

Revision ID: 0024_career_link_schema_repair
Revises: 0023_calendar_event_semantics

The columns are part of the current 0020 definition, but installations that
ran an earlier copy of that migration do not have them.  This migration is
therefore intentionally conditional and its downgrade is a no-op: at revision
0023 the current migration history already considers these columns present.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024_career_link_schema_repair"
down_revision = "0023_calendar_event_semantics"
branch_labels = None
depends_on = None

_TABLE = "career_interview_application_links"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    checks = {constraint["name"] for constraint in inspector.get_check_constraints(_TABLE)}

    missing_columns = {
        "interview_content_fingerprint",
        "application_content_fingerprint",
        "resolution_source",
    } - columns
    missing_resolution_check = "resolution_source_valid" not in checks
    if not missing_columns and not missing_resolution_check:
        return

    with op.batch_alter_table(_TABLE) as batch:
        if "interview_content_fingerprint" in missing_columns:
            batch.add_column(sa.Column("interview_content_fingerprint", sa.String(128)))
        if "application_content_fingerprint" in missing_columns:
            batch.add_column(sa.Column("application_content_fingerprint", sa.String(128)))
        if "resolution_source" in missing_columns:
            batch.add_column(
                sa.Column(
                    "resolution_source",
                    sa.String(16),
                    nullable=False,
                    server_default="model",
                )
            )
        if missing_resolution_check:
            batch.create_check_constraint(
                "resolution_source_valid",
                "resolution_source IN ('model','user')",
            )


def downgrade() -> None:
    # These fields belong to the current 0020 schema.  Removing them here would
    # make revision 0023 inconsistent for fresh installations.
    pass
