"""Focused checks for the Notion course calendar persistence migration."""

from __future__ import annotations

import importlib
import inspect


def test_0009_migration_extends_current_head_with_required_tables() -> None:
    migration = importlib.import_module("app.db.migrations.versions.0009_notion_course_calendars")
    source = inspect.getsource(migration.upgrade)

    assert migration.revision == "0009_notion_course_calendars"
    assert migration.down_revision == "0008_phase6_finance_allowlist"
    assert "academic_course_calendars" in source
    assert "academic_clarifications" in source
    assert "academic_setup_reminders" in source
    assert 'op.add_column("assessments"' in source
