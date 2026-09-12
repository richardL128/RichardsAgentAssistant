"""Add calendar event semantic cache fields.

Revision ID: 0023_calendar_event_semantics
Revises: 0022_academic_material_profiles
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_calendar_event_semantics"
down_revision = "0022_academic_material_profiles"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)


def _semantic_columns() -> tuple[sa.Column, ...]:
    return (
        sa.Column("calendar_semantic_overview", sa.String(700)),
        sa.Column("calendar_semantic_description", sa.String(1500)),
        sa.Column("calendar_semantic_status", sa.String(32)),
        sa.Column("calendar_semantic_evidence_ids", sa.JSON),
        sa.Column("calendar_semantic_description_evidence_ids", sa.JSON),
        sa.Column("calendar_semantic_source_fingerprint", sa.String(128)),
        sa.Column("calendar_semantic_source_last_edited_at", _TS),
        sa.Column("calendar_semantic_model_identity", sa.String(128)),
        sa.Column("calendar_semantic_config_version", sa.String(128)),
        sa.Column("calendar_semantic_prompt_version", sa.String(128)),
        sa.Column("calendar_semantic_analyzed_at", _TS),
    )


def upgrade() -> None:
    op.add_column(
        "assessments",
        sa.Column("is_all_day", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    for column in _semantic_columns():
        op.add_column("assessments", column)
    for column in _semantic_columns():
        op.add_column("career_interview_events", column)
    op.create_index(
        "ix_assessments_calendar_semantic_status",
        "assessments",
        ["calendar_semantic_status", "due_at"],
    )
    op.create_index(
        "ix_career_interview_events_calendar_semantic_status",
        "career_interview_events",
        ["calendar_semantic_status", "local_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_career_interview_events_calendar_semantic_status",
        table_name="career_interview_events",
    )
    op.drop_index("ix_assessments_calendar_semantic_status", table_name="assessments")
    for column in reversed(_semantic_columns()):
        op.drop_column("career_interview_events", column.name)
    for column in reversed(_semantic_columns()):
        op.drop_column("assessments", column.name)
    op.drop_column("assessments", "is_all_day")
