"""Focused coverage for the additive calendar semantic cache migration."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text


def test_0023_adds_nullable_semantic_fields_without_losing_calendar_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = importlib.import_module("app.db.migrations.versions.0023_calendar_event_semantics")
    assert migration.revision == "0023_calendar_event_semantics"
    assert migration.down_revision == "0022_academic_material_profiles"

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'calendar-semantics.db'}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE assessments ("
                    "id CHAR(32) PRIMARY KEY, title VARCHAR(255), due_at DATETIME)"
                )
            )
            connection.execute(
                text(
                    "CREATE TABLE career_interview_events ("
                    "id CHAR(32) PRIMARY KEY, title VARCHAR(500), local_date DATE)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO assessments (id, title, due_at) "
                    "VALUES ('assessment-1', 'Graph quiz', '2026-09-11 14:00:00')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO career_interview_events (id, title, local_date) "
                    "VALUES ('interview-1', 'Technical round', '2026-09-13')"
                )
            )
            context = MigrationContext.configure(connection)
            monkeypatch.setattr(migration, "op", Operations(context))

            migration.upgrade()

            assessment_columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(assessments)")
            }
            interview_columns = {
                row[1]
                for row in connection.exec_driver_sql("PRAGMA table_info(career_interview_events)")
            }
            semantic_columns = {
                "calendar_semantic_overview",
                "calendar_semantic_description",
                "calendar_semantic_status",
                "calendar_semantic_source_fingerprint",
                "calendar_semantic_model_identity",
                "calendar_semantic_prompt_version",
                "calendar_semantic_analyzed_at",
            }
            assert semantic_columns.issubset(assessment_columns)
            assert semantic_columns.issubset(interview_columns)
            assert "is_all_day" in assessment_columns
            assert connection.execute(text("SELECT title FROM assessments")).scalar_one() == (
                "Graph quiz"
            )
            assert (
                connection.execute(
                    text("SELECT calendar_semantic_status FROM assessments")
                ).scalar_one()
                is None
            )
    finally:
        engine.dispose()
