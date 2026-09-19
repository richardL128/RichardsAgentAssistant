"""Regression coverage for historical career-link schema drift."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text


def test_0024_repairs_legacy_link_table_without_losing_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = importlib.import_module("app.db.migrations.versions.0024_career_link_schema_repair")
    assert migration.revision == "0024_career_link_schema_repair"
    assert migration.down_revision == "0023_calendar_event_semantics"

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'career-link-repair.db'}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE career_interview_application_links ("
                    "id CHAR(32) PRIMARY KEY, state VARCHAR(32) NOT NULL)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO career_interview_application_links (id, state) "
                    "VALUES ('link-1', 'matched')"
                )
            )
            context = MigrationContext.configure(connection)
            monkeypatch.setattr(migration, "op", Operations(context))

            migration.upgrade()

            columns = {
                column["name"]
                for column in inspect(connection).get_columns("career_interview_application_links")
            }
            assert {
                "interview_content_fingerprint",
                "application_content_fingerprint",
                "resolution_source",
            }.issubset(columns)
            repaired = connection.execute(
                text(
                    "SELECT state, resolution_source "
                    "FROM career_interview_application_links WHERE id = 'link-1'"
                )
            ).one()
            assert repaired == ("matched", "model")

            # Re-running the conditional repair must be harmless.
            migration.upgrade()
    finally:
        engine.dispose()
