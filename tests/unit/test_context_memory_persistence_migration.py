"""Regression coverage for context memory persistence migration."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect


def test_0029_adds_compaction_and_user_memory_tables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = importlib.import_module(
        "app.db.migrations.versions.0029_context_memory_persistence"
    )
    assert migration.revision == "0029_context_memory_persistence"
    assert migration.down_revision == "0028_native_conversations"

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'migration-0029.db'}")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE native_conversation_sessions (id CHAR(32) PRIMARY KEY)"
            )
            context = MigrationContext.configure(connection)
            monkeypatch.setattr(migration, "op", Operations(context))

            migration.upgrade()

            inspector = inspect(connection)
            assert "native_conversation_compactions" in inspector.get_table_names()
            assert "user_memory_facts" in inspector.get_table_names()
            assert "user_memory_events" in inspector.get_table_names()
            fact_columns = {column["name"] for column in inspector.get_columns("user_memory_facts")}
            assert {
                "content_artifact_key",
                "embedding",
                "embedding_model",
                "embedding_dimensions",
            }.issubset(fact_columns)
            fact_indexes = {index["name"] for index in inspector.get_indexes("user_memory_facts")}
            assert "ix_user_memory_owner_status_kind" in fact_indexes
            compaction_indexes = {
                index["name"] for index in inspector.get_indexes("native_conversation_compactions")
            }
            assert "ix_native_compactions_conversation_status_range" in compaction_indexes

            migration.downgrade()
            assert "user_memory_facts" not in inspect(connection).get_table_names()
            assert "native_conversation_compactions" not in inspect(connection).get_table_names()
    finally:
        engine.dispose()
