"""Seed the Phase 6 finance source allowlist.

Revision ID: 0008_phase6_finance_allowlist
Revises: 0007_phase6_finance
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0008_phase6_finance_allowlist"
down_revision = "0007_phase6_finance"
branch_labels = None
depends_on = None

ALLOWLIST_VERSION = "finance-sources-2026.09"
ALLOWLIST_AUDIT_ID = uuid.uuid5(
    uuid.NAMESPACE_URL,
    f"lifeagent:{ALLOWLIST_VERSION}:recorded",
)
APPROVED_AT = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)

_UUID = sa.Uuid(as_uuid=True)
_TS = sa.DateTime(timezone=True)

_SOURCES: tuple[dict[str, Any], ...] = (
    {
        "source_id": "dvids",
        "name": "Defense Visual Information Distribution Service (DVIDS) Search API",
        "base_url": "https://api.dvidshub.net/search",
        "source_version": "dvids-search-v1",
        "classification": "primary",
        "entitlement": ("Free DVIDS developer API key assigned to the registered API client"),
        "license_note": (
            "DVIDS API Terms permit commercial use of API data and require proper DVIDS "
            "attribution; cached results must be updated when asset metadata changes. "
            "Do not imply DoD endorsement."
        ),
        "license_allows_excerpt": True,
        "excerpt_max_chars": 300,
        "excerpt_max_words": None,
    },
    {
        "source_id": "breaking_defense",
        "name": "Breaking Defense",
        "base_url": "https://breakingdefense.com/wp-json/wp/v2/posts",
        "source_version": "wp-rest-v2",
        "classification": "reported",
        "entitlement": "Public feed (Breaking Media); no key",
        "license_note": (
            "All rights reserved. Headline + short snippet + attribution + canonical link "
            "only (fair use); no full-text republication."
        ),
        "license_allows_excerpt": True,
        "excerpt_max_chars": 200,
        "excerpt_max_words": None,
    },
    {
        "source_id": "eia_open_data",
        "name": "U.S. EIA Open Data API v2",
        "base_url": "https://api.eia.gov/v2/",
        "source_version": "eia-api-v2",
        "classification": "primary",
        "entitlement": "Free registered API key",
        "license_note": (
            "U.S. Government work, public domain, no copyright. Attribution requested: "
            '"Source: U.S. Energy Information Administration".'
        ),
        "license_allows_excerpt": True,
        "excerpt_max_chars": None,
        "excerpt_max_words": None,
    },
    {
        "source_id": "federal_register_energy",
        "name": "FederalRegister.gov Department of Energy Documents API",
        "base_url": "https://www.federalregister.gov/api/v1/documents.json",
        "source_version": "federal-register-api-v1",
        "classification": "primary",
        "entitlement": "Public FederalRegister.gov API; no API key required",
        "license_note": (
            "Public U.S. federal regulatory metadata and documents. Attribute "
            "FederalRegister.gov / Office of the Federal Register, link to the canonical "
            "document and official PDF, and do not present the XML rendition as the official "
            "legal edition."
        ),
        "license_allows_excerpt": True,
        "excerpt_max_chars": 500,
        "excerpt_max_words": None,
    },
    {
        "source_id": "alpha_vantage_news",
        "name": "Alpha Vantage News & Sentiment API",
        "base_url": "https://www.alphavantage.co/query?function=NEWS_SENTIMENT",
        "source_version": "news-sentiment-v1",
        "classification": "reported",
        "entitlement": "Alpha Vantage premium API plan",
        "license_note": (
            "API ToS. Third-party publisher headlines/summaries for internal app display "
            "to entitled users; no bulk redistribution or resale. Attribute to originating "
            "publisher."
        ),
        "license_allows_excerpt": True,
        "excerpt_max_chars": 500,
        "excerpt_max_words": None,
    },
    {
        "source_id": "benzinga_news",
        "name": "Benzinga News API",
        "base_url": "https://api.benzinga.com/api/v2/news",
        "source_version": "benzinga-news-v2",
        "classification": "reported",
        "entitlement": "Benzinga licensed newswire feed; contracted display seats",
        "license_note": (
            "Commercial redistribution licence. Display headlines/body to entitled end "
            "users per contract; no public archive or resale; Benzinga attribution required."
        ),
        "license_allows_excerpt": True,
        "excerpt_max_chars": 500,
        "excerpt_max_words": None,
    },
    {
        "source_id": "fmp_etf",
        "name": "Financial Modeling Prep ETF Holdings API",
        "base_url": "https://financialmodelingprep.com/api/v3/etf-holder/",
        "source_version": "fmp-api-v3",
        "classification": "secondary",
        "entitlement": "FMP paid plan (Starter+ for ETF endpoints); commercial licence, internal use",
        "license_note": (
            "Commercial data licence. Derived/aggregated figures may be shown to entitled "
            "users; raw dataset redistribution or resale prohibited."
        ),
        "license_allows_excerpt": False,
        "excerpt_max_chars": None,
        "excerpt_max_words": None,
    },
    {
        "source_id": "alpha_vantage_etf",
        "name": "Alpha Vantage ETF Profile & Holdings API",
        "base_url": "https://www.alphavantage.co/query?function=ETF_PROFILE",
        "source_version": "etf-profile-v1",
        "classification": "secondary",
        "entitlement": "Alpha Vantage premium API plan; private individual use",
        "license_note": (
            "API ToS. Use for Richard's private individual investment analysis and monitoring "
            "only; no third-party access or redistribution. Numeric ETF profile and holdings "
            "data only; cite the endpoint and retrieval date."
        ),
        "license_allows_excerpt": False,
        "excerpt_max_chars": None,
        "excerpt_max_words": None,
    },
)


def upgrade() -> None:
    op.add_column(
        "finance_approved_sources",
        sa.Column(
            "license_allows_excerpt", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column("finance_approved_sources", sa.Column("excerpt_max_chars", sa.Integer()))
    op.add_column("finance_approved_sources", sa.Column("excerpt_max_words", sa.Integer()))
    op.create_check_constraint(
        "classification_valid",
        "finance_approved_sources",
        "classification IN ('primary','reported','secondary')",
    )
    op.create_check_constraint(
        "excerpt_limits_require_permission",
        "finance_approved_sources",
        "license_allows_excerpt OR (excerpt_max_chars IS NULL AND excerpt_max_words IS NULL)",
    )

    bind = op.get_bind()
    audit_events = _audit_events_table()
    approved_sources = _approved_sources_table()
    if (
        bind.execute(
            sa.select(audit_events.c.id).where(audit_events.c.id == ALLOWLIST_AUDIT_ID)
        ).first()
        is None
    ):
        bind.execute(
            audit_events.insert().values(
                id=ALLOWLIST_AUDIT_ID,
                created_at=APPROVED_AT,
                updated_at=APPROVED_AT,
                actor="richard",
                action="record_finance_source_allowlist",
                target_type="finance_source_allowlist",
                target_id=ALLOWLIST_VERSION,
                result="approved",
                run_id=None,
                artifact_key=None,
            )
        )
    for source in _SOURCES:
        exists = bind.execute(
            sa.select(approved_sources.c.id).where(
                approved_sources.c.source_id == source["source_id"],
                approved_sources.c.allowlist_version == ALLOWLIST_VERSION,
            )
        ).first()
        if exists is not None:
            continue
        bind.execute(
            approved_sources.insert().values(
                id=uuid.uuid5(
                    uuid.NAMESPACE_URL, f"lifeagent:{ALLOWLIST_VERSION}:{source['source_id']}"
                ),
                source_id=source["source_id"],
                name=source["name"],
                base_url=source["base_url"],
                source_version=source["source_version"],
                allowlist_version=ALLOWLIST_VERSION,
                license_note=source["license_note"],
                entitlement=source["entitlement"],
                classification=source["classification"],
                license_allows_excerpt=source["license_allows_excerpt"],
                excerpt_max_chars=source["excerpt_max_chars"],
                excerpt_max_words=source["excerpt_max_words"],
                enabled=False,
                approved_at=None,
                approval_audit_id=None,
                created_at=APPROVED_AT,
                updated_at=APPROVED_AT,
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    approved_sources = _approved_sources_table()
    audit_events = _audit_events_table()
    bind.execute(
        approved_sources.delete().where(approved_sources.c.allowlist_version == ALLOWLIST_VERSION)
    )

    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE audit_events DISABLE TRIGGER audit_events_append_only")
    try:
        bind.execute(audit_events.delete().where(audit_events.c.id == ALLOWLIST_AUDIT_ID))
    finally:
        if bind.dialect.name == "postgresql":
            op.execute("ALTER TABLE audit_events ENABLE TRIGGER audit_events_append_only")

    op.drop_constraint(
        "excerpt_limits_require_permission",
        "finance_approved_sources",
        type_="check",
    )
    op.drop_constraint("classification_valid", "finance_approved_sources", type_="check")
    op.drop_column("finance_approved_sources", "excerpt_max_words")
    op.drop_column("finance_approved_sources", "excerpt_max_chars")
    op.drop_column("finance_approved_sources", "license_allows_excerpt")


def _audit_events_table() -> sa.Table:
    return sa.table(
        "audit_events",
        sa.column("id", _UUID),
        sa.column("created_at", _TS),
        sa.column("updated_at", _TS),
        sa.column("actor", sa.String(255)),
        sa.column("action", sa.String(128)),
        sa.column("target_type", sa.String(128)),
        sa.column("target_id", sa.String(255)),
        sa.column("result", sa.String(64)),
        sa.column("run_id", _UUID),
        sa.column("artifact_key", sa.String(512)),
    )


def _approved_sources_table() -> sa.Table:
    return sa.table(
        "finance_approved_sources",
        sa.column("id", _UUID),
        sa.column("source_id", sa.String(64)),
        sa.column("name", sa.String(200)),
        sa.column("base_url", sa.String(2048)),
        sa.column("source_version", sa.String(128)),
        sa.column("allowlist_version", sa.String(128)),
        sa.column("license_note", sa.String(1000)),
        sa.column("entitlement", sa.String(255)),
        sa.column("classification", sa.String(64)),
        sa.column("license_allows_excerpt", sa.Boolean()),
        sa.column("excerpt_max_chars", sa.Integer()),
        sa.column("excerpt_max_words", sa.Integer()),
        sa.column("enabled", sa.Boolean()),
        sa.column("approved_at", _TS),
        sa.column("approval_audit_id", _UUID),
        sa.column("created_at", _TS),
        sa.column("updated_at", _TS),
    )
