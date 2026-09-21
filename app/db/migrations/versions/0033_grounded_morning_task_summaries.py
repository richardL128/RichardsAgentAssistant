"""Replace legacy non-substantive calendar semantics with grounded overviews.

Revision ID: 0033_grounded_morning_summaries
Revises: 0032_model_query_contract_index
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0033_grounded_morning_summaries"
down_revision = "0032_model_query_contract_index"
branch_labels = None
depends_on = None

_STATUS_CONSTRAINT = "calendar_semantic_status_valid"
_STATUS_SQL = (
    "calendar_semantic_status IS NULL OR "
    "calendar_semantic_status IN ('valid','unavailable','invalid')"
)
_SEMANTIC_COLUMNS = (
    "calendar_semantic_overview",
    "calendar_semantic_description",
    "calendar_semantic_status",
    "calendar_semantic_intent_value",
    "calendar_semantic_intent_status",
    "calendar_semantic_intent_rationale",
    "calendar_semantic_intent_evidence_ids",
    "calendar_semantic_evidence_ids",
    "calendar_semantic_description_evidence_ids",
    "calendar_semantic_source_fingerprint",
    "calendar_semantic_source_last_edited_at",
    "calendar_semantic_model_identity",
    "calendar_semantic_config_version",
    "calendar_semantic_prompt_version",
    "calendar_semantic_analyzed_at",
)


def upgrade() -> None:
    for table_name in ("assessments", "career_interview_events"):
        if not sa.inspect(op.get_bind()).has_table(table_name):
            continue
        columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}
        cleared = [column for column in _SEMANTIC_COLUMNS if column in columns]
        if "calendar_semantic_status" in columns and cleared:
            semantic_table = sa.table(
                table_name,
                *(sa.column(column) for column in cleared),
            )
            op.execute(
                semantic_table.update()
                .where(semantic_table.c.calendar_semantic_status == "not_substantive")
                .values(dict.fromkeys(cleared))
            )
        _replace_status_constraint(table_name)


def downgrade() -> None:
    raise RuntimeError(
        "Revision 0033 is irreversible because legacy semantic payloads were invalidated and "
        "must not be restored as executable runtime semantics."
    )


def _replace_status_constraint(table_name: str) -> None:
    constraint_names = {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(table_name)
        if constraint.get("name")
    }
    recreate = "always" if op.get_bind().dialect.name == "sqlite" else "auto"
    with op.batch_alter_table(table_name, recreate=recreate) as batch:
        if _STATUS_CONSTRAINT in constraint_names:
            batch.drop_constraint(_STATUS_CONSTRAINT, type_="check")
        batch.create_check_constraint(_STATUS_CONSTRAINT, _STATUS_SQL)
