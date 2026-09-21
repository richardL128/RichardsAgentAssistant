from __future__ import annotations

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect


def test_0031_adds_external_calendar_source_metadata(tmp_path, monkeypatch) -> None:
    migration = importlib.import_module("app.db.migrations.versions.0031_google_ical_schedule")
    assert migration.revision == "0031_google_ical_schedule"
    assert migration.down_revision == "0030_learn_persistence"
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'migration-0031.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE academic_course_calendars (id CHAR(32) PRIMARY KEY)"
        )
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )
        migration.upgrade()
        columns = {
            column["name"]: column
            for column in inspect(connection).get_columns("academic_course_calendars")
        }
        assert columns["source_kind"]["nullable"] is False
        assert "external_source_id" in columns
        constraints = {
            item["name"]
            for item in inspect(connection).get_unique_constraints("academic_course_calendars")
        }
        assert "uq_academic_course_calendars_external_source" in constraints

        migration.downgrade()
        downgraded = {
            column["name"]
            for column in inspect(connection).get_columns("academic_course_calendars")
        }
        assert "source_kind" not in downgraded
        assert "external_source_id" not in downgraded
