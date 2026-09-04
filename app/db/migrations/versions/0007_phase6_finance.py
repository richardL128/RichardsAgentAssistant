"""Add Phase 6 finance briefing records.

Revision ID: 0007_phase6_finance
Revises: 0006_phase5_academic_planner
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_phase6_finance"
down_revision = "0006_phase5_academic_planner"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=True)
_TS = sa.DateTime(timezone=True)
_JSON_LIST = sa.text("'[]'")
_JSON_OBJECT = sa.text("'{}'")


def upgrade() -> None:
    op.create_table(
        "finance_approved_sources",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("base_url", sa.String(2048), nullable=False),
        sa.Column("source_version", sa.String(128), nullable=False),
        sa.Column("allowlist_version", sa.String(128), nullable=False),
        sa.Column("license_note", sa.String(1000), nullable=False),
        sa.Column("entitlement", sa.String(255), nullable=False),
        sa.Column("classification", sa.String(64), nullable=False, server_default="reported"),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("approved_at", _TS),
        sa.Column(
            "approval_audit_id", _UUID, sa.ForeignKey("audit_events.id", ondelete="SET NULL")
        ),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("length(source_id) > 0", name="source_id_nonempty"),
        sa.CheckConstraint("length(name) > 0", name="name_nonempty"),
        sa.CheckConstraint("length(base_url) > 0", name="base_url_nonempty"),
        sa.CheckConstraint("length(license_note) > 0", name="license_note_nonempty"),
        sa.CheckConstraint("length(entitlement) > 0", name="entitlement_nonempty"),
        sa.UniqueConstraint(
            "source_id",
            "allowlist_version",
            name="uq_finance_sources_source_allowlist",
        ),
    )
    op.create_index(
        "ix_finance_sources_allowlist_enabled",
        "finance_approved_sources",
        ["allowlist_version", "enabled"],
    )

    op.create_table(
        "finance_source_health",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("source_version", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("checked_at", _TS, nullable=False),
        sa.Column("diagnostic", sa.String(1000)),
        sa.CheckConstraint("status IN ('healthy','attention','failed')", name="status_valid"),
        sa.UniqueConstraint("source_id", "source_version", name="uq_finance_source_health_version"),
    )
    op.create_index(
        "ix_finance_source_health_status",
        "finance_source_health",
        ["status", "checked_at"],
    )

    op.create_table(
        "finance_holdings",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("quantity", sa.Float, nullable=False, server_default="0"),
        sa.Column("market_value", sa.Float, nullable=False, server_default="0"),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("tags", sa.JSON, nullable=False, server_default=_JSON_LIST),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.CheckConstraint("quantity >= 0", name="quantity_nonnegative"),
        sa.CheckConstraint("market_value >= 0", name="market_value_nonnegative"),
        sa.UniqueConstraint("symbol", name="uq_finance_holdings_symbol"),
    )
    op.create_index(
        "ix_finance_holdings_enabled_symbol",
        "finance_holdings",
        ["enabled", "symbol"],
    )

    op.create_table(
        "finance_investment_theses",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("version", sa.String(128), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("thesis_summary", sa.Text, nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("status IN ('active','paused','closed')", name="status_valid"),
        sa.UniqueConstraint("symbol", "version", name="uq_finance_theses_symbol_version"),
    )
    op.create_index(
        "ix_finance_theses_status_symbol",
        "finance_investment_theses",
        ["status", "symbol"],
    )

    op.create_table(
        "finance_watchlist",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column(
            "thesis_id",
            _UUID,
            sa.ForeignKey("finance_investment_theses.id", ondelete="SET NULL"),
        ),
        sa.Column("themes", sa.JSON, nullable=False, server_default=_JSON_LIST),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.UniqueConstraint("symbol", name="uq_finance_watchlist_symbol"),
    )
    op.create_index(
        "ix_finance_watchlist_enabled_symbol",
        "finance_watchlist",
        ["enabled", "symbol"],
    )

    op.create_table(
        "finance_etf_exposures",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("etf_symbol", sa.String(32), nullable=False),
        sa.Column("underlying_symbol", sa.String(32), nullable=False),
        sa.Column("weight_percent", sa.Float, nullable=False),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("as_of", sa.Date, nullable=False),
        sa.CheckConstraint("weight_percent >= 0 AND weight_percent <= 100", name="weight_valid"),
        sa.UniqueConstraint(
            "etf_symbol",
            "underlying_symbol",
            "as_of",
            name="uq_finance_etf_exposure_as_of",
        ),
    )
    op.create_index(
        "ix_finance_etf_exposure_underlying",
        "finance_etf_exposures",
        ["underlying_symbol", "as_of"],
    )

    op.create_table(
        "finance_thesis_events",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "thesis_id",
            _UUID,
            sa.ForeignKey("finance_investment_theses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_id", sa.String(64), nullable=False),
        sa.Column("impact_label", sa.String(32), nullable=False),
        sa.Column("rationale", sa.String(1000), nullable=False),
        sa.Column("counter_case", sa.String(1000), nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.CheckConstraint(
            "impact_label IN ('monitor','revisit thesis','no action')",
            name="impact_label_valid",
        ),
        sa.UniqueConstraint("thesis_id", "event_id", name="uq_finance_thesis_events_once"),
    )
    op.create_index(
        "ix_finance_thesis_events_thesis_created",
        "finance_thesis_events",
        ["thesis_id", "created_at"],
    )

    op.create_table(
        "finance_briefings",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "run_id",
            _UUID,
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_allowlist_version", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("generated_at", _TS, nullable=False),
        sa.Column("tickers", sa.JSON, nullable=False, server_default=_JSON_LIST),
        sa.Column("themes", sa.JSON, nullable=False, server_default=_JSON_LIST),
        sa.Column("card_count", sa.Integer, nullable=False),
        sa.Column("source_failure_count", sa.Integer, nullable=False),
        sa.Column("redacted_payload", sa.JSON, nullable=False, server_default=_JSON_OBJECT),
        sa.CheckConstraint("status IN ('succeeded','attention')", name="status_valid"),
        sa.CheckConstraint("card_count >= 0", name="card_count_nonnegative"),
        sa.UniqueConstraint("run_id", name="uq_finance_briefings_run"),
    )
    op.create_index(
        "ix_finance_briefings_generated",
        "finance_briefings",
        ["generated_at", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_finance_briefings_generated", table_name="finance_briefings")
    op.drop_table("finance_briefings")
    op.drop_index("ix_finance_thesis_events_thesis_created", table_name="finance_thesis_events")
    op.drop_table("finance_thesis_events")
    op.drop_index("ix_finance_etf_exposure_underlying", table_name="finance_etf_exposures")
    op.drop_table("finance_etf_exposures")
    op.drop_index("ix_finance_watchlist_enabled_symbol", table_name="finance_watchlist")
    op.drop_table("finance_watchlist")
    op.drop_index("ix_finance_theses_status_symbol", table_name="finance_investment_theses")
    op.drop_table("finance_investment_theses")
    op.drop_index("ix_finance_holdings_enabled_symbol", table_name="finance_holdings")
    op.drop_table("finance_holdings")
    op.drop_index("ix_finance_source_health_status", table_name="finance_source_health")
    op.drop_table("finance_source_health")
    op.drop_index("ix_finance_sources_allowlist_enabled", table_name="finance_approved_sources")
    op.drop_table("finance_approved_sources")
