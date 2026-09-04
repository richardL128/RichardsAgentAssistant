"""Add account-scale code-review operation tables and columns.

Revision ID: 0005_phase4_operations
Revises: 0004_phase3_code_review
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_phase4_operations"
down_revision = "0004_phase3_code_review"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=True)
_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    # Discovery and profile progress live on the repository row so that the
    # resume query is a single indexed lookup for the next unprofiled entry.
    op.add_column("repositories", sa.Column("discovery_version", sa.String(128)))
    op.add_column("repositories", sa.Column("discovered_at", _TS))
    op.add_column(
        "repositories",
        sa.Column("profile_state", sa.String(32), nullable=False, server_default="unprofiled"),
    )
    op.add_column("repositories", sa.Column("profiled_at", _TS))
    op.add_column("repositories", sa.Column("profile_error_code", sa.String(128)))
    op.add_column("repositories", sa.Column("last_reviewed_at", _TS))
    op.create_check_constraint(
        "ck_repositories_profile_state_valid",
        "repositories",
        "profile_state IN ('unprofiled','profiling','profiled','failed')",
    )
    op.create_index("ix_repositories_profile_state", "repositories", ["profile_state", "full_name"])

    op.add_column(
        "reviewed_commits",
        sa.Column("trigger", sa.String(32), nullable=False, server_default="push"),
    )
    op.create_check_constraint(
        "ck_reviewed_commits_trigger_valid",
        "reviewed_commits",
        "trigger IN ('push','quick_scan','daily','catchup','manual')",
    )
    op.create_index(
        "ix_reviewed_commits_repo_created", "reviewed_commits", ["repository_id", "created_at"]
    )

    op.create_table(
        "repository_discovery_state",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("scope", sa.String(128), nullable=False),
        sa.Column("discovery_version", sa.String(128), nullable=False),
        sa.Column("page", sa.Integer, nullable=False, server_default="1"),
        sa.Column("cursor", sa.String(512)),
        sa.Column("discovered_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("discovery_complete", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("last_full_name", sa.String(201)),
        sa.Column("last_run_id", _UUID, sa.ForeignKey("agent_runs.id", ondelete="SET NULL")),
        sa.Column("last_error_code", sa.String(128)),
        sa.Column("completed_at", _TS),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("scope", name="uq_repository_discovery_state_scope"),
        sa.CheckConstraint("page >= 1", name="page_positive"),
        sa.CheckConstraint("discovered_count >= 0", name="discovered_count_nonnegative"),
    )

    op.create_table(
        "review_finding_dismissals",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "repository_id",
            _UUID,
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(2000)),
        sa.Column("dismissed_by", sa.String(255), nullable=False),
        sa.Column("dismissed_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("run_id", _UUID, sa.ForeignKey("agent_runs.id", ondelete="SET NULL")),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint(
            "repository_id", "fingerprint", name="uq_review_finding_dismissals_repo_fingerprint"
        ),
        sa.CheckConstraint("length(reason_code) > 0", name="reason_code_nonempty"),
    )
    op.create_index(
        "ix_review_finding_dismissals_repo",
        "review_finding_dismissals",
        ["repository_id", "created_at"],
    )

    op.create_table(
        "daily_review_reports",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("report_date", sa.Date, nullable=False),
        sa.Column(
            "run_id",
            _UUID,
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("schedule_name", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="running"),
        sa.Column("commit_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("repository_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("finding_counts", sa.JSON, nullable=False),
        sa.Column("artifact_key", sa.String(64)),
        sa.Column("delivery_id", _UUID, sa.ForeignKey("deliveries.id", ondelete="SET NULL")),
        sa.Column("error_code", sa.String(128)),
        sa.Column("generated_at", _TS),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("report_date", name="uq_daily_review_reports_report_date"),
        sa.UniqueConstraint("run_id", name="uq_daily_review_reports_run_id"),
        sa.CheckConstraint(
            "status IN ('running','succeeded','attention','failed')", name="status_valid"
        ),
        sa.CheckConstraint("commit_count >= 0", name="commit_count_nonnegative"),
        sa.CheckConstraint("repository_count >= 0", name="repository_count_nonnegative"),
    )
    op.create_index("ix_daily_review_reports_date", "daily_review_reports", ["report_date"])


def downgrade() -> None:
    op.drop_index("ix_daily_review_reports_date", table_name="daily_review_reports")
    op.drop_table("daily_review_reports")
    op.drop_index("ix_review_finding_dismissals_repo", table_name="review_finding_dismissals")
    op.drop_table("review_finding_dismissals")
    op.drop_table("repository_discovery_state")
    op.drop_index("ix_reviewed_commits_repo_created", table_name="reviewed_commits")
    op.drop_constraint("ck_reviewed_commits_trigger_valid", "reviewed_commits", type_="check")
    op.drop_column("reviewed_commits", "trigger")
    op.drop_index("ix_repositories_profile_state", table_name="repositories")
    op.drop_constraint("ck_repositories_profile_state_valid", "repositories", type_="check")
    op.drop_column("repositories", "last_reviewed_at")
    op.drop_column("repositories", "profile_error_code")
    op.drop_column("repositories", "profiled_at")
    op.drop_column("repositories", "profile_state")
    op.drop_column("repositories", "discovered_at")
    op.drop_column("repositories", "discovery_version")
