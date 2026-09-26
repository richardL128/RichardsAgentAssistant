from __future__ import annotations

import importlib
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from app.db.models import Base


def test_0034_migration_adds_canonical_action_item_schema() -> None:
    migration = importlib.import_module("app.db.migrations.versions.0034_action_item_contracts")

    assert migration.revision == "0034_action_item_contracts"
    assert migration.down_revision == "0033_grounded_morning_summaries"
    assert "application_id" in {column.name for column in migration._ASSESSMENT_COLUMNS}
    assert "legacy_notion_course_assessment" in migration._SOURCE_KIND_VALID
    assert "done" in migration._STATUS_VALID
    assert "completed" not in migration._STATUS_VALID
    assert "blocked" not in migration._STATUS_VALID
    assert "domain IS NULL OR" in migration._DOMAIN_VALID


def test_fresh_sqlite_model_schema_includes_canonical_action_item_persistence() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        inspector = inspect(engine)

        assert "career_applications" in inspector.get_table_names()
        assessment_columns = {
            column["name"] for column in inspector.get_columns("assessments")
        }
        application_columns = {
            column["name"] for column in inspector.get_columns("career_applications")
        }
        link_columns = {
            column["name"]
            for column in inspector.get_columns("career_interview_application_links")
        }

        assert {
            "domain",
            "item_kind",
            "status",
            "start_date",
            "start_at",
            "date_precision",
            "source_kind",
            "context",
            "application_id",
            "interview_id",
        }.issubset(assessment_columns)
        assert {
            "application_page_id",
            "applications_data_source_id",
            "legacy_application_row_id",
            "deadline_date_precision",
            "deadline_start_date",
            "deadline_start_at",
            "deadline_timezone",
            "next_action_status",
            "next_action_date_precision",
            "next_action_start_date",
            "next_action_start_at",
            "next_action_timezone",
            "posting_url",
            "source",
            "location",
            "contact_name",
            "contact_email",
            "notes",
        }.issubset(application_columns)
        assert (
            next(
                column
                for column in inspector.get_columns("assessments")
                if column["name"] == "course_id"
            )["nullable"]
            is True
        )
        assert "application_id" in link_columns
    finally:
        engine.dispose()


def test_0034_sqlite_upgrade_backfills_temporal_contract_and_preserves_links(
    tmp_path,
    monkeypatch,
) -> None:
    migration = importlib.import_module("app.db.migrations.versions.0034_action_item_contracts")
    monkeypatch.setenv("APP_TIMEZONE", "America/Toronto")
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'action-items-0034.db'}")
    try:
        with engine.begin() as connection:
            _create_previous_schema(connection)
            course_id = str(uuid.uuid4())
            all_day_id = str(uuid.uuid4())
            task_id = str(uuid.uuid4())
            misc_id = str(uuid.uuid4())
            timed_id = str(uuid.uuid4())
            link_id = str(uuid.uuid4())
            row_id = str(uuid.uuid4())
            interview_id = str(uuid.uuid4())
            connection.execute(
                text("INSERT INTO courses (id, notion_id) VALUES (:id, 'course-page')"),
                {"id": course_id},
            )
            connection.execute(
                text(
                    "INSERT INTO career_application_rows (id, row_block_id) "
                    "VALUES (:id, 'legacy-row')"
                ),
                {"id": row_id},
            )
            connection.execute(
                text(
                    "INSERT INTO career_interview_events (id, interview_page_id) "
                    "VALUES (:id, 'interview-page')"
                ),
                {"id": interview_id},
            )
            connection.execute(
                text(
                    "INSERT INTO career_interview_application_links "
                    "(id, interview_id, application_row_id, state) "
                    "VALUES (:id, :interview_id, :row_id, 'matched')"
                ),
                {"id": link_id, "interview_id": interview_id, "row_id": row_id},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO assessments (
                        id, course_id, notion_id, title, assessment_type, due_at, ends_at,
                        completed, is_all_day, fact_state, source_id, source_scope,
                        title_property_id
                    ) VALUES
                    (
                        :all_day_id, :course_id, 'notion-all-day', 'Assignment 1',
                        'assignment', '2026-09-23 03:59:00', NULL, 0, 1, 'confirmed',
                        'source-academic', 'notion:source-academic', 'title-prop'
                    ),
                    (
                        :task_id, :course_id, 'notion-task', 'Task - classify me',
                        'task', NULL, NULL, 0, 0, 'ambiguous',
                        'source-misc', 'notion:source-misc', 'title-prop'
                    ),
                    (
                        :misc_id, :course_id, 'notion-misc', 'Misc item',
                        'misc', NULL, NULL, 0, 0, 'confirmed',
                        'source-misc', 'notion:source-misc', 'title-prop'
                    ),
                    (
                        :timed_id, :course_id, 'notion-timed', 'Timed interview prep',
                        'deadline', '2026-09-24T14:00:00+00:00',
                        '2026-09-24T15:00:00+00:00', 1, 0, 'confirmed',
                        'source-academic', 'google_ical:schedule', 'title-prop'
                    )
                    """
                ),
                {
                    "all_day_id": all_day_id,
                    "task_id": task_id,
                    "misc_id": misc_id,
                    "timed_id": timed_id,
                    "course_id": course_id,
                },
            )
            monkeypatch.setattr(
                migration,
                "op",
                Operations(MigrationContext.configure(connection)),
            )

            migration.upgrade()

            inspector = inspect(connection)
            assessment_columns = {
                column["name"]: column for column in inspector.get_columns("assessments")
            }
            application_columns = {
                column["name"] for column in inspector.get_columns("career_applications")
            }
            link_columns = {
                column["name"]
                for column in inspector.get_columns("career_interview_application_links")
            }
            all_day = connection.execute(
                text(
                    "SELECT domain, item_kind, status, date_precision, start_date, start_at, "
                    "timezone, source_kind, notion_data_source_id, notion_page_id "
                    "FROM assessments WHERE id = :id"
                ),
                {"id": all_day_id},
            ).one()
            task = connection.execute(
                text(
                    "SELECT domain, status, source_kind, context "
                    "FROM assessments WHERE id = :id"
                ),
                {"id": task_id},
            ).one()
            timed = connection.execute(
                text(
                    "SELECT status, date_precision, start_date, start_at, end_at, timezone, "
                    "source_kind FROM assessments WHERE id = :id"
                ),
                {"id": timed_id},
            ).one()
            misc = connection.execute(
                text(
                    "SELECT domain, item_kind, status, source_kind "
                    "FROM assessments WHERE id = :id"
                ),
                {"id": misc_id},
            ).one()
            link = connection.execute(
                text(
                    "SELECT application_row_id, application_id "
                    "FROM career_interview_application_links WHERE id = :id"
                ),
                {"id": link_id},
            ).one()

            assert assessment_columns["course_id"]["nullable"] is True
            assert {
                "domain",
                "item_kind",
                "status",
                "start_date",
                "start_at",
                "date_precision",
                "source_kind",
                "context",
                "application_id",
                "interview_id",
            }.issubset(assessment_columns)
            assert {
                "application_page_id",
                "applications_data_source_id",
                "legacy_application_row_id",
                "company_name",
                "deadline_date_precision",
                "deadline_start_date",
                "deadline_start_at",
                "deadline_timezone",
                "next_action_status",
                "next_action_date_precision",
                "next_action_start_date",
                "next_action_start_at",
                "next_action_timezone",
                "posting_url",
                "source",
                "location",
                "contact_name",
                "contact_email",
                "notes",
            }.issubset(application_columns)
            assert {"application_row_id", "application_id"}.issubset(link_columns)
            assert all_day == (
                "academic",
                "assignment",
                "to_do",
                "date",
                "2026-09-22",
                None,
                None,
                "legacy_notion_course_assessment",
                "source-academic",
                "notion-all-day",
            )
            assert task[0:3] == (None, "needs_review", "notion_action_items")
            assert '"domain_review_required": true' in task[3]
            assert misc == (None, "needs_review", "needs_review", "notion_action_items")
            assert timed[0] == "done"
            assert timed[1] == "datetime"
            assert timed[2] is None
            assert _as_utc(timed[3]) == datetime(2026, 9, 24, 14, 0, tzinfo=UTC)
            assert _as_utc(timed[4]) == datetime(2026, 9, 24, 15, 0, tzinfo=UTC)
            assert timed[5] == "America/Toronto"
            assert timed[6] == "google_calendar"
            assert link[0] == row_id
            assert link[1] is None
    finally:
        engine.dispose()


def _create_previous_schema(connection: sa.Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE courses (
            id CHAR(32) PRIMARY KEY,
            notion_id VARCHAR(255) NOT NULL
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE career_jobs_workspaces (
            id CHAR(32) PRIMARY KEY
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE career_application_rows (
            id CHAR(32) PRIMARY KEY,
            row_block_id VARCHAR(255) NOT NULL
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE career_interview_events (
            id CHAR(32) PRIMARY KEY,
            interview_page_id VARCHAR(255) NOT NULL
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE career_interview_application_links (
            id CHAR(32) PRIMARY KEY,
            interview_id CHAR(32) NOT NULL,
            application_row_id CHAR(32),
            state VARCHAR(32) NOT NULL
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE assessments (
            id CHAR(32) PRIMARY KEY,
            course_id CHAR(32) NOT NULL,
            notion_id VARCHAR(255) NOT NULL,
            title VARCHAR(255) NOT NULL,
            assessment_type VARCHAR(64) NOT NULL,
            due_at DATETIME,
            ends_at DATETIME,
            completed BOOLEAN NOT NULL DEFAULT 0,
            is_all_day BOOLEAN NOT NULL DEFAULT 0,
            fact_state VARCHAR(32) NOT NULL DEFAULT 'confirmed',
            source_id VARCHAR(255),
            source_scope VARCHAR(255),
            title_property_id VARCHAR(255),
            FOREIGN KEY(course_id) REFERENCES courses(id) ON DELETE CASCADE
        )
        """
    )


def _as_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
