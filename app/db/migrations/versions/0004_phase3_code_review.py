"""Add one-repository code-review persistence.

Revision ID: 0004_phase3_code_review
Revises: 0003_phase2_integration
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_phase3_code_review"
down_revision = "0003_phase2_integration"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=True)
_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "repositories",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("full_name", sa.String(201), nullable=False),
        sa.Column("clone_url", sa.String(1000), nullable=False),
        sa.Column("default_branch", sa.String(255), nullable=False),
        sa.Column("installation_id", sa.BigInteger, nullable=False),
        sa.Column("allowlist_version", sa.String(128), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("length(full_name) > 2", name="full_name_nonempty"),
        sa.UniqueConstraint("full_name", name="uq_repositories_full_name"),
    )
    op.create_index("ix_repositories_enabled_name", "repositories", ["enabled", "full_name"])

    op.create_table(
        "reviewed_commits",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "repository_id",
            _UUID,
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            _UUID,
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("delivery_id", sa.String(128), nullable=False),
        sa.Column("ref", sa.String(500), nullable=False),
        sa.Column("base_sha", sa.String(64), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("risk", sa.String(16), nullable=False, server_default="medium"),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("report_artifact_key", sa.String(64)),
        sa.Column("error_code", sa.String(128)),
        sa.Column("started_at", _TS),
        sa.Column("finished_at", _TS),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','attention','failed','cancelled')",
            name="status_valid",
        ),
        sa.CheckConstraint("risk IN ('high','medium','low')", name="risk_valid"),
        sa.UniqueConstraint("run_id", name="uq_reviewed_commits_run_id"),
        sa.UniqueConstraint("delivery_id", name="uq_reviewed_commits_delivery_id"),
        sa.UniqueConstraint("repository_id", "head_sha", name="uq_reviewed_commits_repo_head"),
    )
    op.create_index(
        "ix_reviewed_commits_status_created", "reviewed_commits", ["status", "created_at"]
    )

    op.create_table(
        "review_findings",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "reviewed_commit_id",
            _UUID,
            sa.ForeignKey("reviewed_commits.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("path", sa.String(500), nullable=False),
        sa.Column("line", sa.Integer, nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("explanation", sa.String(2000), nullable=False),
        sa.Column("reproduction_or_missing_test", sa.String(2000), nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("assumptions", sa.JSON, nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("evidence_refs", sa.JSON, nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("published_inline", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("severity IN ('block','important','suggestion')", name="severity_valid"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_valid"),
        sa.CheckConstraint("line >= 1", name="line_positive"),
        sa.CheckConstraint("published_inline = false", name="phase3_inline_disabled"),
        sa.UniqueConstraint(
            "reviewed_commit_id",
            "fingerprint",
            name="uq_review_findings_commit_fingerprint",
        ),
    )
    op.create_index(
        "ix_review_findings_commit_severity",
        "review_findings",
        ["reviewed_commit_id", "severity"],
    )

    op.create_table(
        "project_profiles",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "repository_id",
            _UUID,
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("commit_sha", sa.String(64), nullable=False),
        sa.Column("profile_version", sa.String(128), nullable=False),
        sa.Column("summary", sa.String(2000), nullable=False),
        sa.Column("artifact_key", sa.String(64), nullable=False),
        sa.Column(
            "instruction_provenance", sa.JSON, nullable=False, server_default=sa.text("'[]'::json")
        ),
        sa.Column("reviewed", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint(
            "repository_id",
            "commit_sha",
            "profile_version",
            name="uq_project_profiles_repo_commit_version",
        ),
    )
    op.create_index(
        "ix_project_profiles_repo_reviewed", "project_profiles", ["repository_id", "reviewed"]
    )


def downgrade() -> None:
    op.drop_index("ix_project_profiles_repo_reviewed", table_name="project_profiles")
    op.drop_table("project_profiles")
    op.drop_index("ix_review_findings_commit_severity", table_name="review_findings")
    op.drop_table("review_findings")
    op.drop_index("ix_reviewed_commits_status_created", table_name="reviewed_commits")
    op.drop_table("reviewed_commits")
    op.drop_index("ix_repositories_enabled_name", table_name="repositories")
    op.drop_table("repositories")
