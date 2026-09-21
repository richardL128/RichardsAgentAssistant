"""Focused coverage for the additive calendar semantic cache migration."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


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


def test_0026_replaces_study_block_schema_with_semantic_event_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = importlib.import_module("app.db.migrations.versions.0026_semantic_calendar_events")
    assert migration.revision == "0026_semantic_calendar_events"
    assert migration.down_revision == "0025_academic_embedding_hnsw"

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'semantic-events.db'}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE assessments ("
                    "id CHAR(32) PRIMARY KEY, "
                    "assessment_type VARCHAR(64), "
                    "calendar_semantic_status VARCHAR(32))"
                )
            )
            connection.execute(
                text(
                    "CREATE TABLE career_interview_events ("
                    "id CHAR(32) PRIMARY KEY, "
                    "calendar_semantic_status VARCHAR(32))"
                )
            )
            connection.execute(
                text(
                    "CREATE TABLE academic_clarifications ("
                    "id CHAR(32) PRIMARY KEY, "
                    "decision VARCHAR(32), "
                    "studying_block_preview_title VARCHAR(1024))"
                )
            )
            connection.execute(
                text(
                    "CREATE TABLE discord_wake_inbound ("
                    "id CHAR(32) PRIMARY KEY, "
                    "interaction_action VARCHAR(32))"
                )
            )
            connection.execute(
                text("CREATE TABLE academic_checkins (id CHAR(32) PRIMARY KEY, plan_id CHAR(32))")
            )
            connection.execute(
                text("CREATE TABLE study_plans (id CHAR(32) PRIMARY KEY, plan_key VARCHAR(255))")
            )
            connection.execute(
                text("CREATE TABLE study_blocks (id CHAR(32) PRIMARY KEY, plan_id CHAR(32))")
            )
            connection.execute(
                text(
                    "INSERT INTO assessments (id, assessment_type) "
                    "VALUES ('assessment-1', 'studying_block')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO academic_clarifications "
                    "(id, decision, studying_block_preview_title) "
                    "VALUES ('clarification-1', 'studying_block', 'Chapter 4 block')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO discord_wake_inbound (id, interaction_action) "
                    "VALUES ('wake-1', 'studying_block')"
                )
            )
            context = MigrationContext.configure(connection)
            monkeypatch.setattr(migration, "op", Operations(context))

            migration.upgrade()

            assessment_columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(assessments)")
            }
            clarification_columns = {
                row[1]
                for row in connection.exec_driver_sql("PRAGMA table_info(academic_clarifications)")
            }
            checkin_columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(academic_checkins)")
            }
            tables = {
                row[0]
                for row in connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            assert {
                "calendar_semantic_intent_value",
                "calendar_semantic_intent_status",
                "calendar_semantic_intent_rationale",
                "calendar_semantic_intent_evidence_ids",
            }.issubset(assessment_columns)
            assert "event_preview_title" in clarification_columns
            assert "studying_block_preview_title" not in clarification_columns
            assert "plan_id" not in checkin_columns
            assert "study_blocks" not in tables
            assert "study_plans" not in tables
            decision = connection.execute(
                text("SELECT decision FROM academic_clarifications")
            ).scalar_one()
            interaction_action = connection.execute(
                text("SELECT interaction_action FROM discord_wake_inbound")
            ).scalar_one()
            assessment_type = connection.execute(
                text("SELECT assessment_type FROM assessments")
            ).scalar_one()
            assert assessment_type == "event"
            assert decision == "event"
            assert interaction_action == "event"
    finally:
        engine.dispose()


def test_0033_clears_legacy_semantics_and_tightens_status_constraints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = importlib.import_module(
        "app.db.migrations.versions.0033_grounded_morning_task_summaries"
    )
    assert migration.revision == "0033_grounded_morning_summaries"
    assert migration.down_revision == "0032_model_query_contract_index"

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'grounded-morning.db'}")
    try:
        with engine.begin() as connection:
            semantic_columns = ", ".join(
                (
                    "calendar_semantic_overview VARCHAR(700)",
                    "calendar_semantic_description VARCHAR(1500)",
                    "calendar_semantic_status VARCHAR(32)",
                    "calendar_semantic_intent_value VARCHAR(128)",
                    "calendar_semantic_intent_status VARCHAR(32)",
                    "calendar_semantic_intent_rationale VARCHAR(500)",
                    "calendar_semantic_intent_evidence_ids JSON",
                    "calendar_semantic_evidence_ids JSON",
                    "calendar_semantic_description_evidence_ids JSON",
                    "calendar_semantic_source_fingerprint VARCHAR(128)",
                    "calendar_semantic_source_last_edited_at DATETIME",
                    "calendar_semantic_model_identity VARCHAR(128)",
                    "calendar_semantic_config_version VARCHAR(128)",
                    "calendar_semantic_prompt_version VARCHAR(128)",
                    "calendar_semantic_analyzed_at DATETIME",
                )
            )
            for table_name in ("assessments", "career_interview_events"):
                connection.execute(
                    text(
                        f"CREATE TABLE {table_name} ("
                        "id VARCHAR(32) PRIMARY KEY, title VARCHAR(255), "
                        f"{semantic_columns}, "
                        "CONSTRAINT calendar_semantic_status_valid CHECK ("
                        "calendar_semantic_status IS NULL OR calendar_semantic_status IN "
                        "('valid','not_substantive','unavailable','invalid')))"
                    )
                )
                connection.execute(
                    text(
                        f"INSERT INTO {table_name} ("  # noqa: S608
                        "id, title, calendar_semantic_overview, calendar_semantic_status, "
                        "calendar_semantic_intent_status, calendar_semantic_evidence_ids, "
                        "calendar_semantic_source_fingerprint, calendar_semantic_prompt_version) "
                        "VALUES ('legacy', 'Legacy task', 'Generic event metadata', "
                        "'not_substantive', 'valid', '[\"frag-1\"]', 'sha256:old', 'v3'), "
                        "('current', 'Current task', 'Grounded task meaning', "
                        "'valid', 'valid', '[\"frag-2\"]', 'sha256:new', 'v4')"
                    )
                )

            monkeypatch.setattr(
                migration,
                "op",
                Operations(MigrationContext.configure(connection)),
            )
            migration.upgrade()

            for table_name in ("assessments", "career_interview_events"):
                rows = connection.execute(
                    text(
                        f"SELECT id, title, calendar_semantic_status, "  # noqa: S608
                        f"calendar_semantic_overview, calendar_semantic_source_fingerprint "
                        f"FROM {table_name} ORDER BY id"
                    )
                ).all()
                assert rows == [
                    ("current", "Current task", "valid", "Grounded task meaning", "sha256:new"),
                    ("legacy", "Legacy task", None, None, None),
                ]
                constraint = next(
                    item
                    for item in inspect(connection).get_check_constraints(table_name)
                    if item["name"] == "calendar_semantic_status_valid"
                )
                assert "not_substantive" not in constraint["sqltext"]
                with pytest.raises(IntegrityError):
                    connection.execute(
                        text(
                            f"INSERT INTO {table_name} "  # noqa: S608
                            "(id, title, calendar_semantic_status) "
                            "VALUES ('rejected', 'Rejected', 'not_substantive')"
                        )
                    )
    finally:
        engine.dispose()


def test_0026_downgrade_does_not_restore_removed_scheduling_architecture() -> None:
    migration = importlib.import_module("app.db.migrations.versions.0026_semantic_calendar_events")

    with pytest.raises(RuntimeError, match="irreversible"):
        migration.downgrade()


def test_0033_downgrade_does_not_restore_non_substantive_runtime_status() -> None:
    migration = importlib.import_module(
        "app.db.migrations.versions.0033_grounded_morning_task_summaries"
    )

    with pytest.raises(RuntimeError, match="irreversible"):
        migration.downgrade()
