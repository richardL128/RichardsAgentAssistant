"""Focused checks for the career interview persistence migration."""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text


def test_0020_migration_extends_0019_with_career_tables() -> None:
    migration = importlib.import_module("app.db.migrations.versions.0020_job_interviews")
    source = inspect.getsource(migration.upgrade)

    assert migration.revision == "0020_job_interviews"
    assert migration.down_revision == "0019_discord_enqueued_at"
    assert "career_jobs_workspaces" in source
    assert "career_application_rows" in source
    assert "career_interview_events" in source
    assert "career_preparation_plan_revisions" in source
    assert "career_write_proposals" in source
    assert "career_write_receipts" in source


def test_0020_sqlite_migration_creates_core_constraints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = importlib.import_module("app.db.migrations.versions.0020_job_interviews")
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'career-0020.db'}")
    try:
        with engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys=ON"))
            _create_parent_deliveries(connection)
            context = MigrationContext.configure(connection)
            monkeypatch.setattr(migration, "op", Operations(context))

            migration.upgrade()

            tables = {
                row[0]
                for row in connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            assert {
                "career_jobs_workspaces",
                "career_sync_cursors",
                "career_application_rows",
                "career_interview_events",
                "career_preparation_plans",
                "career_write_receipts",
            }.issubset(tables)

            workspace_id = "11111111-1111-1111-1111-111111111111"
            interview_id = "22222222-2222-2222-2222-222222222222"
            connection.execute(
                text(
                    """
                    INSERT INTO career_jobs_workspaces (
                        id, scope, jobs_page_id, discovery_status, created_at, updated_at
                    ) VALUES (
                        :id, 'default', 'jobs-page', 'valid', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                ),
                {"id": workspace_id},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO career_interview_events (
                        id, workspace_id, interview_page_id, title, local_date,
                        notion_last_edited_at, content_fingerprint, created_at, updated_at
                    ) VALUES (
                        :id, :workspace_id, 'interview-1', 'Technical round', NULL,
                        CURRENT_TIMESTAMP, 'hash', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                ),
                {"id": interview_id, "workspace_id": workspace_id},
            )
            assert (
                connection.execute(
                    text(
                        "SELECT local_date FROM career_interview_events "
                        "WHERE interview_page_id = 'interview-1'"
                    )
                ).scalar_one()
                is None
            )

            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    text(
                        """
                        INSERT INTO career_write_proposals (
                            id, idempotency_key, operation, target_page_id, payload,
                            redacted_preview, confirmation_token, state, created_at, updated_at
                        ) VALUES (
                            '33333333-3333-3333-3333-333333333333',
                            'proposal-1',
                            'unsafe_operation',
                            'interview-1',
                            '{}',
                            'Preview',
                            'CONFIRM',
                            'pending',
                            CURRENT_TIMESTAMP,
                            CURRENT_TIMESTAMP
                        )
                        """
                    )
                )
    finally:
        engine.dispose()


def _create_parent_deliveries(connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE deliveries (
                id CHAR(32) PRIMARY KEY,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
            )
            """
        )
    )
