from __future__ import annotations

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect


def test_0032_adds_partial_temporal_assessment_index(tmp_path, monkeypatch) -> None:
    migration = importlib.import_module(
        "app.db.migrations.versions.0032_model_query_contract_index"
    )
    assert migration.revision == "0032_model_query_contract_index"
    assert migration.down_revision == "0031_google_ical_schedule"
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'migration-0032.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE assessments ("
            "id CHAR(32) PRIMARY KEY, is_all_day BOOLEAN NOT NULL, "
            "due_at DATETIME, title VARCHAR(255) NOT NULL, "
            "active BOOLEAN NOT NULL, archived BOOLEAN NOT NULL, "
            "completed BOOLEAN NOT NULL, notion_last_edited_at DATETIME)"
        )
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )

        migration.upgrade()
        indexes = {item["name"]: item for item in inspect(connection).get_indexes("assessments")}
        assert indexes["ix_assessments_active_incomplete_temporal"]["column_names"] == [
            "is_all_day",
            "due_at",
            "title",
            "id",
        ]

        migration.downgrade()
        assert "ix_assessments_active_incomplete_temporal" not in {
            item["name"] for item in inspect(connection).get_indexes("assessments")
        }
