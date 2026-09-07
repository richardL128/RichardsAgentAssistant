"""Focused checks for the public-first finance source persistence migration."""

from __future__ import annotations

import importlib
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.db.finance import FinanceRepository


def test_0010_migration_seeds_disabled_v2_allowlist_and_endpoint_registry(
    tmp_path: Path,
) -> None:
    migration = importlib.import_module(
        "app.db.migrations.versions.0010_public_finance_sources"
    )
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'finance-migration.db'}")
    try:
        with engine.begin() as connection:
            _create_current_head_finance_tables(connection)
            _seed_v1_source(connection)
            context = MigrationContext.configure(connection)
            migration.op = Operations(context)

            migration.upgrade()

            assert migration.revision == "0010_public_finance_sources"
            assert migration.down_revision == "0009_notion_course_calendars"
            v1_count = connection.scalar(
                text(
                    "SELECT count(*) FROM finance_approved_sources "
                    "WHERE allowlist_version = 'finance-sources-2026.09'"
                )
            )
            v2_count = connection.scalar(
                text(
                    "SELECT count(*) FROM finance_approved_sources "
                    "WHERE allowlist_version = 'finance-sources-2026.09-v2'"
                )
            )
            disabled_count = connection.scalar(
                text(
                    "SELECT count(*) FROM finance_approved_sources "
                    "WHERE allowlist_version = 'finance-sources-2026.09-v2' "
                    "AND enabled = 0 AND approved_at IS NULL AND approval_audit_id IS NULL"
                )
            )
            endpoint_count = connection.scalar(
                text(
                    "SELECT count(*) FROM finance_source_endpoints "
                    "WHERE allowlist_version = 'finance-sources-2026.09-v2'"
                )
            )
            endpoint_rows = set(
                connection.execute(
                    text(
                        "SELECT source_id, endpoint_id FROM finance_source_endpoints "
                        "WHERE allowlist_version = 'finance-sources-2026.09-v2'"
                    )
                ).all()
            )
            enabled_endpoint_count = connection.scalar(
                text(
                    "SELECT count(*) FROM finance_source_endpoints "
                    "WHERE allowlist_version = 'finance-sources-2026.09-v2' AND enabled = 1"
                )
            )
            audit_row = connection.execute(
                text(
                    "SELECT id, action, result FROM audit_events "
                    "WHERE target_id = 'finance-sources-2026.09-v2'"
                )
            ).one()
            columns = {
                row[1]
                for row in connection.exec_driver_sql(
                    "PRAGMA table_info(finance_etf_exposures)"
                )
            }
            table_names = set(sa.inspect(connection).get_table_names())

            assert v1_count == 1
            assert v2_count == 8
            assert disabled_count == 8
            assert endpoint_count == 9
            assert enabled_endpoint_count == 9
            assert endpoint_rows == {
                ("defense_gov_rss", "defense-gov-releases-rss"),
                ("breaking_defense_public", "breaking-defense-wp-json"),
                ("eia_public_data", "eia-petroleum-bulk-zip"),
                ("eia_public_data", "eia-v2-api"),
                ("federal_register_energy", "federal-register-energy-json"),
                ("sec_edgar", "sec-lmt-submissions"),
                ("company_ir_registry", "lmt-official-news-rss"),
                ("issuer_etf_holdings", "ivv-ishares-latest-holdings-csv"),
                ("technology_official_feeds", "cisa-kev-json"),
            }
            assert audit_row.action == "record_finance_source_allowlist"
            assert audit_row.result == "recorded"
            assert str(audit_row.id).replace("-", "") == migration.ALLOWLIST_AUDIT_ID.hex
            assert {"source_url", "retrieved_at"}.issubset(columns)
            assert "finance_source_request_audits" in table_names

            with Session(bind=connection) as session:
                assert (
                    FinanceRepository.source_approval_gate(
                        session,
                        allowlist_version="finance-sources-2026.09-v2",
                    )
                    is False
                )

            duplicate = text(
                "INSERT INTO finance_source_endpoints "
                "(id, allowlist_version, source_id, endpoint_id, base_url, host, "
                "transport_kind, parser_kind, registry_version, enabled, "
                "expected_freshness_seconds, request_ceiling, license_note, retention_note, "
                "excerpt_allowed, issuer_scope, cik_scope, ticker_scope) "
                "SELECT :id, allowlist_version, source_id, endpoint_id, base_url, host, "
                "transport_kind, parser_kind, registry_version, enabled, "
                "expected_freshness_seconds, request_ceiling, license_note, retention_note, "
                "excerpt_allowed, issuer_scope, cik_scope, ticker_scope "
                "FROM finance_source_endpoints LIMIT 1"
            )
            try:
                connection.execute(duplicate, {"id": str(uuid.uuid4())})
            except sa.exc.IntegrityError:
                pass
            else:  # pragma: no cover - defensive assertion message is clearer than silent pass
                raise AssertionError("duplicate endpoint key must fail closed")

            with pytest.raises(RuntimeError, match="history-preserving"):
                migration.downgrade()

            retained_v2_count = connection.scalar(
                text(
                    "SELECT count(*) FROM finance_approved_sources "
                    "WHERE allowlist_version = 'finance-sources-2026.09-v2'"
                )
            )
            retained_endpoint_count = connection.scalar(
                text("SELECT count(*) FROM finance_source_endpoints")
            )
            assert retained_v2_count == 8
            assert retained_endpoint_count == 9
    finally:
        engine.dispose()


def _create_current_head_finance_tables(connection: sa.Connection) -> None:
    metadata = sa.MetaData()
    uuid_type = sa.Uuid(as_uuid=True)
    timestamp = sa.DateTime(timezone=True)
    sa.Table(
        "audit_events",
        metadata,
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("updated_at", timestamp, nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("target_type", sa.String(128), nullable=False),
        sa.Column("target_id", sa.String(255), nullable=False),
        sa.Column("result", sa.String(64), nullable=False),
        sa.Column("run_id", uuid_type),
        sa.Column("artifact_key", sa.String(512)),
    )
    sa.Table(
        "finance_approved_sources",
        metadata,
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("base_url", sa.String(2048), nullable=False),
        sa.Column("source_version", sa.String(128), nullable=False),
        sa.Column("allowlist_version", sa.String(128), nullable=False),
        sa.Column("license_note", sa.String(1000), nullable=False),
        sa.Column("entitlement", sa.String(255), nullable=False),
        sa.Column("classification", sa.String(64), nullable=False),
        sa.Column("license_allows_excerpt", sa.Boolean, nullable=False),
        sa.Column("excerpt_max_chars", sa.Integer),
        sa.Column("excerpt_max_words", sa.Integer),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("approved_at", timestamp),
        sa.Column("approval_audit_id", uuid_type),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("updated_at", timestamp, nullable=False),
        sa.UniqueConstraint(
            "source_id",
            "allowlist_version",
            name="uq_finance_sources_source_allowlist",
        ),
    )
    sa.Table(
        "finance_etf_exposures",
        metadata,
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("etf_symbol", sa.String(32), nullable=False),
        sa.Column("underlying_symbol", sa.String(32), nullable=False),
        sa.Column("weight_percent", sa.Float, nullable=False),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("as_of", sa.Date, nullable=False),
    )
    metadata.create_all(connection)


def _seed_v1_source(connection: sa.Connection) -> None:
    now = migration_time = "2026-09-04 12:00:00"
    connection.execute(
        text(
            "INSERT INTO finance_approved_sources "
            "(id, source_id, name, base_url, source_version, allowlist_version, "
            "license_note, entitlement, classification, license_allows_excerpt, "
            "enabled, created_at, updated_at) "
            "VALUES (:id, 'dvids', 'DVIDS', 'https://api.dvidshub.net/search', "
            "'dvids-search-v1', 'finance-sources-2026.09', 'Existing v1 source.', "
            "'Legacy v1 key.', 'primary', 1, 0, :created_at, :updated_at)"
        ),
        {
            "id": str(uuid.uuid4()),
            "created_at": now,
            "updated_at": migration_time,
        },
    )
