"""Index the host-enforced incomplete assessment temporal query.

Revision ID: 0032_model_query_contract_index
Revises: 0031_google_ical_schedule
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0032_model_query_contract_index"
down_revision = "0031_google_ical_schedule"
branch_labels = None
depends_on = None

_INDEX_NAME = "ix_assessments_active_incomplete_temporal"
_POSTGRES_PREDICATE = sa.text(
    "active IS TRUE AND archived IS FALSE AND completed IS FALSE "
    "AND notion_last_edited_at IS NOT NULL"
)
_SQLITE_PREDICATE = sa.text(
    "active = 1 AND archived = 0 AND completed = 0 AND notion_last_edited_at IS NOT NULL"
)


def upgrade() -> None:
    op.create_index(
        _INDEX_NAME,
        "assessments",
        ["is_all_day", "due_at", "title", "id"],
        unique=False,
        postgresql_where=_POSTGRES_PREDICATE,
        sqlite_where=_SQLITE_PREDICATE,
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="assessments")
