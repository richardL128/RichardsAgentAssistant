"""Focused checks for the Notion course calendar persistence migration."""

from __future__ import annotations

import importlib
import inspect
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text


def test_0009_migration_extends_current_head_with_required_tables() -> None:
    migration = importlib.import_module("app.db.migrations.versions.0009_notion_course_calendars")
    source = inspect.getsource(migration.upgrade)

    assert migration.revision == "0009_notion_course_calendars"
    assert migration.down_revision == "0008_phase6_finance_allowlist"
    assert "academic_course_calendars" in source
    assert "academic_clarifications" in source
    assert "academic_setup_reminders" in source
    assert 'op.add_column("assessments"' in source


def test_0013_migration_extends_0012_with_expanded_clarification_types() -> None:
    migration = importlib.import_module(
        "app.db.migrations.versions.0013_academic_todo_clarification_types"
    )
    source = inspect.getsource(migration.upgrade)

    assert migration.revision == "0013_academic_todo_types"
    assert len(migration.revision) <= 32
    assert migration.down_revision == "0012_academic_memory_review"
    assert "tutorial_preview_title" in source
    assert "lab_preview_title" in source
    assert "studying_block_preview_title" in source
    assert "studying_block" in source


def test_0016_migration_extends_0015_with_date_range_and_operation_journal() -> None:
    migration = importlib.import_module("app.db.migrations.versions.0016_academic_date_journal")
    source = inspect.getsource(migration.upgrade)

    assert migration.revision == "0016_academic_date_journal"
    assert len(migration.revision) <= 32
    assert migration.down_revision == "0015_academic_agent_clarify"
    assert "ends_at" in source
    assert "assessment_date_range_valid" in source
    assert "academic_proposal_operation_journal" in source
    assert "uq_academic_proposal_operation" in source
    assert "payload_hash" in source


def test_0016_sqlite_migration_preserves_rows_and_enforces_new_constraints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = importlib.import_module("app.db.migrations.versions.0016_academic_date_journal")
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'academic-0016.db'}")
    try:
        with engine.begin() as connection:
            _create_old_assessments(connection)
            connection.execute(
                text(
                    """
                    INSERT INTO assessments (
                        id,
                        course_id,
                        notion_id,
                        title,
                        assessment_type,
                        due_at,
                        grade_weight_percent,
                        estimated_minutes,
                        confidence_gap,
                        scope_size,
                        fact_state,
                        confidence,
                        completed,
                        active,
                        archived,
                        created_at,
                        updated_at
                    ) VALUES (
                        :id,
                        :course_id,
                        'notion-assignment-1',
                        'Research brief',
                        'assignment',
                        '2026-09-10 23:00:00',
                        15,
                        60,
                        0.5,
                        0,
                        'confirmed',
                        1,
                        0,
                        1,
                        0,
                        CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP
                    )
                    """
                ),
                {"id": str(uuid.uuid4()), "course_id": str(uuid.uuid4())},
            )
            context = MigrationContext.configure(connection)
            monkeypatch.setattr(migration, "op", Operations(context))

            migration.upgrade()

            assessment_columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(assessments)")
            }
            journal_columns = {
                row[1]
                for row in connection.exec_driver_sql(
                    "PRAGMA table_info(academic_proposal_operation_journal)"
                )
            }
            existing = connection.execute(
                text("SELECT title, due_at, ends_at FROM assessments WHERE notion_id = :notion_id"),
                {"notion_id": "notion-assignment-1"},
            ).one()

            assert "ends_at" in assessment_columns
            assert {"proposal_id", "ordinal", "payload_hash", "state", "receipt"}.issubset(
                journal_columns
            )
            assert existing[0] == "Research brief"
            assert existing[2] is None

            connection.execute(
                text(
                    """
                    UPDATE assessments
                    SET ends_at = '2026-09-11 00:00:00'
                    WHERE notion_id = 'notion-assignment-1'
                    """
                )
            )
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    text(
                        """
                        INSERT INTO assessments (
                            id,
                            course_id,
                            notion_id,
                            title,
                            assessment_type,
                            due_at,
                            ends_at,
                            estimated_minutes,
                            confidence_gap,
                            scope_size,
                            fact_state,
                            confidence,
                            completed,
                            active,
                            archived,
                            created_at,
                            updated_at
                        ) VALUES (
                            :id,
                            :course_id,
                            'notion-invalid-range',
                            'Invalid range',
                            'studying_block',
                            '2026-09-10 23:00:00',
                            '2026-09-10 23:00:00',
                            45,
                            0.5,
                            0,
                            'confirmed',
                            1,
                            0,
                            1,
                            0,
                            CURRENT_TIMESTAMP,
                            CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {"id": str(uuid.uuid4()), "course_id": str(uuid.uuid4())},
                )

            connection.execute(
                text(
                    """
                    INSERT INTO academic_proposal_operation_journal (
                        id,
                        proposal_id,
                        ordinal,
                        operation_id,
                        payload_hash,
                        state,
                        created_at,
                        updated_at
                    ) VALUES (
                        :id,
                        :proposal_id,
                        0,
                        'proposal:0',
                        :payload_hash,
                        'in_progress',
                        CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP
                    )
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "proposal_id": str(uuid.uuid4()),
                    "payload_hash": "a" * 64,
                },
            )
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    text(
                        """
                        INSERT INTO academic_proposal_operation_journal (
                            id,
                            proposal_id,
                            ordinal,
                            operation_id,
                            payload_hash,
                            state,
                            created_at,
                            updated_at
                        ) VALUES (
                            :id,
                            :proposal_id,
                            1,
                            'proposal:1',
                            'short',
                            'in_progress',
                            CURRENT_TIMESTAMP,
                            CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {"id": str(uuid.uuid4()), "proposal_id": str(uuid.uuid4())},
                )
    finally:
        engine.dispose()


def test_0013_production_migration_branch_adds_columns_and_replaces_constraint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = importlib.import_module(
        "app.db.migrations.versions.0013_academic_todo_clarification_types"
    )
    recorder = _ProductionOpRecorder()
    monkeypatch.setattr(migration, "op", recorder)

    migration.upgrade()

    assert [call[0] for call in recorder.calls] == [
        "add_column",
        "add_column",
        "add_column",
        "drop_constraint",
        "create_check_constraint",
    ]
    assert [call[2].name for call in recorder.calls[:3]] == [
        "tutorial_preview_title",
        "lab_preview_title",
        "studying_block_preview_title",
    ]
    assert recorder.calls[3] == (
        "drop_constraint",
        "decision_valid",
        "academic_clarifications",
        "check",
    )
    assert recorder.calls[4] == (
        "create_check_constraint",
        "decision_valid",
        "academic_clarifications",
        "decision IS NULL OR decision IN "
        "('quiz','assignment','tutorial','lab','studying_block','ignore')",
    )


def test_0013_sqlite_migration_preserves_rows_and_accepts_new_decisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = importlib.import_module(
        "app.db.migrations.versions.0013_academic_todo_clarification_types"
    )
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'academic-0013.db'}")
    try:
        with engine.begin() as connection:
            _create_old_academic_clarifications(connection)
            connection.execute(
                text(
                    """
                    INSERT INTO academic_clarifications (
                        id,
                        event_notion_id,
                        original_title,
                        raw_label,
                        quiz_preview_title,
                        assignment_preview_title,
                        expected_edited_at,
                        idempotency_key,
                        state,
                        decision,
                        write_status,
                        expires_at,
                        created_at,
                        updated_at
                    ) VALUES (
                        :id,
                        'event-existing',
                        'Chapter 4',
                        'chapter 4',
                        'Quiz - Chapter 4',
                        'Assignment - Chapter 4',
                        '2026-09-05 15:00:00',
                        'clarification:existing',
                        'pending',
                        NULL,
                        'none',
                        '2026-09-06 15:00:00',
                        CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP
                    )
                    """
                ),
                {"id": str(uuid.uuid4())},
            )
            context = MigrationContext.configure(connection)
            monkeypatch.setattr(migration, "op", Operations(context))

            migration.upgrade()

            columns = {
                row[1]
                for row in connection.exec_driver_sql("PRAGMA table_info(academic_clarifications)")
            }
            existing = connection.execute(
                text(
                    """
                    SELECT state, decision, tutorial_preview_title, lab_preview_title,
                        studying_block_preview_title
                    FROM academic_clarifications
                    WHERE idempotency_key = 'clarification:existing'
                    """
                )
            ).one()

            assert {
                "tutorial_preview_title",
                "lab_preview_title",
                "studying_block_preview_title",
            }.issubset(columns)
            assert existing == ("pending", None, None, None, None)

            for action in ("tutorial", "lab", "studying_block", "ignore"):
                connection.execute(
                    text(
                        """
                        INSERT INTO academic_clarifications (
                            id,
                            event_notion_id,
                            original_title,
                            quiz_preview_title,
                            assignment_preview_title,
                            tutorial_preview_title,
                            lab_preview_title,
                            studying_block_preview_title,
                            expected_edited_at,
                            idempotency_key,
                            state,
                            decision,
                            write_status,
                            expires_at,
                            created_at,
                            updated_at
                        ) VALUES (
                            :id,
                            :event_id,
                            'Chapter 5',
                            'Quiz - Chapter 5',
                            'Assignment - Chapter 5',
                            'Tutorial - Chapter 5',
                            'Lab - Chapter 5',
                            'Studying Block - Chapter 5',
                            '2026-09-05 15:00:00',
                            :idempotency_key,
                            'claimed',
                            :decision,
                            'pending',
                            '2026-09-06 15:00:00',
                            CURRENT_TIMESTAMP,
                            CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "event_id": f"event-{action}",
                        "idempotency_key": f"clarification:{action}",
                        "decision": action,
                    },
                )

            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    text(
                        """
                        INSERT INTO academic_clarifications (
                            id,
                            event_notion_id,
                            original_title,
                            quiz_preview_title,
                            assignment_preview_title,
                            expected_edited_at,
                            idempotency_key,
                            state,
                            decision,
                            write_status,
                            expires_at,
                            created_at,
                            updated_at
                        ) VALUES (
                            :id,
                            'event-homework',
                            'Chapter 6',
                            'Quiz - Chapter 6',
                            'Assignment - Chapter 6',
                            '2026-09-05 15:00:00',
                            'clarification:homework',
                            'claimed',
                            'homework',
                            'pending',
                            '2026-09-06 15:00:00',
                            CURRENT_TIMESTAMP,
                            CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {"id": str(uuid.uuid4())},
                )
    finally:
        engine.dispose()


def _create_old_academic_clarifications(connection: sa.Connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE academic_clarifications (
                id VARCHAR NOT NULL,
                course_id VARCHAR,
                assessment_id VARCHAR,
                event_notion_id VARCHAR(255) NOT NULL,
                original_title VARCHAR(1024) NOT NULL,
                raw_label VARCHAR(1024),
                quiz_preview_title VARCHAR(1024) NOT NULL,
                assignment_preview_title VARCHAR(1024) NOT NULL,
                expected_edited_at DATETIME NOT NULL,
                title_property_id VARCHAR(255),
                idempotency_key VARCHAR(512) NOT NULL,
                state VARCHAR(32) DEFAULT 'pending' NOT NULL,
                delivery_id VARCHAR(255),
                delivered_at DATETIME,
                decision VARCHAR(32),
                decision_user_id BIGINT,
                decision_at DATETIME,
                write_status VARCHAR(32) DEFAULT 'none' NOT NULL,
                write_error_code VARCHAR(128),
                expires_at DATETIME NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                PRIMARY KEY (id),
                CONSTRAINT event_notion_id_nonempty CHECK (length(event_notion_id) > 0),
                CONSTRAINT state_valid CHECK (
                    state IN (
                        'pending',
                        'delivered',
                        'claimed',
                        'ignored',
                        'applied',
                        'conflict',
                        'failed',
                        'expired'
                    )
                ),
                CONSTRAINT decision_valid CHECK (
                    decision IS NULL OR decision IN ('quiz','assignment','ignore')
                ),
                CONSTRAINT write_status_valid CHECK (
                    write_status IN ('none','skipped','pending','applied','conflict','failed')
                ),
                CONSTRAINT uq_academic_clarifications_idempotency UNIQUE (idempotency_key)
            )
            """
        )
    )


def _create_old_assessments(connection: sa.Connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE assessments (
                id VARCHAR NOT NULL,
                course_id VARCHAR NOT NULL,
                notion_id VARCHAR(255) NOT NULL,
                title VARCHAR(255) NOT NULL,
                assessment_type VARCHAR(64) NOT NULL,
                due_at DATETIME,
                grade_weight_percent FLOAT,
                estimated_minutes INTEGER DEFAULT 60 NOT NULL,
                confidence_gap FLOAT DEFAULT 0.5 NOT NULL,
                scope_size FLOAT DEFAULT 0 NOT NULL,
                scope VARCHAR(4000),
                fact_state VARCHAR(32) DEFAULT 'unconfirmed' NOT NULL,
                confidence FLOAT DEFAULT 0 NOT NULL,
                ambiguity_reason VARCHAR(2000),
                source_page INTEGER,
                source_block VARCHAR(255),
                source_url VARCHAR(1000),
                completed BOOLEAN DEFAULT 0 NOT NULL,
                source_id VARCHAR(255),
                source_scope VARCHAR(255),
                notion_last_edited_at DATETIME,
                title_property_id VARCHAR(255),
                label_source VARCHAR(255),
                active BOOLEAN DEFAULT 1 NOT NULL,
                archived BOOLEAN DEFAULT 0 NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                PRIMARY KEY (id),
                CONSTRAINT uq_assessments_notion_id UNIQUE (notion_id),
                CONSTRAINT confidence_valid CHECK (confidence >= 0 AND confidence <= 1),
                CONSTRAINT fact_state_valid CHECK (
                    fact_state IN ('unconfirmed','confirmed','ambiguous','rejected')
                ),
                CONSTRAINT grade_weight_valid CHECK (
                    grade_weight_percent IS NULL OR
                    (grade_weight_percent >= 0 AND grade_weight_percent <= 100)
                ),
                CONSTRAINT source_page_positive CHECK (source_page IS NULL OR source_page >= 1),
                CONSTRAINT estimated_minutes_positive CHECK (estimated_minutes > 0),
                CONSTRAINT confidence_gap_valid CHECK (confidence_gap >= 0 AND confidence_gap <= 1),
                CONSTRAINT scope_size_valid CHECK (scope_size >= 0 AND scope_size <= 100)
            )
            """
        )
    )


class _ProductionOpRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def get_bind(self) -> object:
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def add_column(self, table_name: str, column: sa.Column[object]) -> None:
        self.calls.append(("add_column", table_name, column))

    def drop_constraint(
        self,
        constraint_name: str,
        table_name: str,
        *,
        type_: str,
    ) -> None:
        self.calls.append(("drop_constraint", constraint_name, table_name, type_))

    def create_check_constraint(
        self,
        constraint_name: str,
        table_name: str,
        condition: str,
    ) -> None:
        self.calls.append(("create_check_constraint", constraint_name, table_name, condition))
