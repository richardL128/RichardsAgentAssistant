"""Add academic material planning profiles.

Revision ID: 0022_academic_material_profiles
Revises: 0021_academic_inbound_materials
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022_academic_material_profiles"
down_revision = "0021_academic_inbound_materials"
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
        "academic_assessment_material_profiles",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "assessment_id",
            _UUID,
            sa.ForeignKey("assessments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("profile_id", sa.String(128), nullable=False),
        sa.Column("profile_version", sa.String(128), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="validated"),
        sa.Column("deliverables_summary", sa.String(1000), nullable=False),
        sa.Column("success_criteria_summary", sa.String(1000), nullable=False),
        sa.Column("study_topics_summary", sa.String(1000), nullable=False),
        sa.Column("explicit_grade_weight_percent", sa.Float),
        sa.Column("effort_lower_minutes", sa.Integer, nullable=False),
        sa.Column("effort_upper_minutes", sa.Integer, nullable=False),
        sa.Column("scope_score", sa.Float, nullable=False),
        sa.Column("dependency_risk_score", sa.Float, nullable=False),
        sa.Column("evidence_chunk_ids", sa.JSON, nullable=False, server_default=_JSON_LIST),
        sa.Column("document_versions", sa.JSON, nullable=False, server_default=_JSON_LIST),
        sa.Column("model_identity", sa.JSON, nullable=False, server_default=_JSON_OBJECT),
        sa.Column("critique", sa.JSON, nullable=False, server_default=_JSON_OBJECT),
        sa.Column("rejection_reason", sa.String(500)),
        sa.Column("generated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("activated_at", _TS),
        *_timestamps(),
        sa.CheckConstraint(
            "state IN ('validated','active','rejected','inactive')",
            name="state_valid",
        ),
        sa.CheckConstraint("effort_lower_minutes > 0", name="effort_lower_positive"),
        sa.CheckConstraint(
            "effort_upper_minutes >= effort_lower_minutes",
            name="effort_range_valid",
        ),
        sa.CheckConstraint("scope_score >= 0 AND scope_score <= 1", name="scope_score_valid"),
        sa.CheckConstraint(
            "dependency_risk_score >= 0 AND dependency_risk_score <= 1",
            name="dependency_risk_score_valid",
        ),
        sa.CheckConstraint(
            "explicit_grade_weight_percent IS NULL OR "
            "(explicit_grade_weight_percent >= 0 AND explicit_grade_weight_percent <= 100)",
            name="explicit_grade_weight_valid",
        ),
        sa.UniqueConstraint("profile_version", name="uq_academic_material_profiles_version"),
    )
    op.create_index(
        "uq_academic_material_profiles_active_assessment",
        "academic_assessment_material_profiles",
        ["assessment_id"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
        sqlite_where=sa.text("state = 'active'"),
    )
    op.create_index(
        "ix_academic_material_profiles_assessment_state",
        "academic_assessment_material_profiles",
        ["assessment_id", "state"],
    )
    op.create_index(
        "ix_academic_material_profiles_created",
        "academic_assessment_material_profiles",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_academic_material_profiles_created",
        table_name="academic_assessment_material_profiles",
    )
    op.drop_index(
        "ix_academic_material_profiles_assessment_state",
        table_name="academic_assessment_material_profiles",
    )
    op.drop_index(
        "uq_academic_material_profiles_active_assessment",
        table_name="academic_assessment_material_profiles",
    )
    op.drop_table("academic_assessment_material_profiles")
