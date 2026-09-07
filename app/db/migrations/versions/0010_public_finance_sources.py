"""Add public-first Phase 6 finance source persistence.

Revision ID: 0010_public_finance_sources
Revises: 0009_notion_course_calendars

Schema downgrade is refused so Alembic cannot stamp 0009 while retaining the
v2 source seed and additive tables. Normal rollback is done by switching
FINANCE_SOURCE_ALLOWLIST_VERSION back to the v1 allowlist.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0010_public_finance_sources"
down_revision = "0009_notion_course_calendars"
branch_labels = None
depends_on = None

ALLOWLIST_VERSION = "finance-sources-2026.09-v2"
ALLOWLIST_AUDIT_ID = uuid.uuid5(
    uuid.NAMESPACE_URL,
    f"lifeagent:{ALLOWLIST_VERSION}:recorded",
)
RECORDED_AT = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)

_UUID = sa.Uuid(as_uuid=True)
_TS = sa.DateTime(timezone=True)
_JSON_LIST = sa.text("'[]'")

_SOURCES: tuple[dict[str, Any], ...] = (
    {
        "source_id": "defense_gov_rss",
        "name": "Defense.gov Official RSS",
        "base_url": "https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=9&Site=945&max=50",
        "source_version": "defense-gov-rss-v1",
        "classification": "primary",
        "entitlement": "Public Defense.gov RSS feed; no API key required",
        "license_note": (
            "Official U.S. Department of Defense public website feed. Retain canonical URL, "
            "publication and retrieval timestamps, attribution, and only short permitted excerpts."
        ),
        "license_allows_excerpt": True,
        "excerpt_max_chars": 300,
        "excerpt_max_words": None,
    },
    {
        "source_id": "breaking_defense_public",
        "name": "Breaking Defense Public WordPress JSON",
        "base_url": "https://breakingdefense.com/wp-json/wp/v2/posts",
        "source_version": "wp-rest-v2-public",
        "classification": "reported",
        "entitlement": "Public WordPress JSON endpoint; no API key required",
        "license_note": (
            "All rights reserved. Use headline, attribution, canonical link, and current approved "
            "short excerpt only; no full-text republication."
        ),
        "license_allows_excerpt": True,
        "excerpt_max_chars": 200,
        "excerpt_max_words": None,
    },
    {
        "source_id": "eia_public_data",
        "name": "U.S. EIA Public Data",
        "base_url": "https://www.eia.gov/opendata/bulk/PET.zip",
        "source_version": "eia-public-v1",
        "classification": "primary",
        "entitlement": "Public EIA bulk files by default; optional EIA_API_KEY only in api mode",
        "license_note": (
            "U.S. Government work, public domain. Preserve units, frequency, period/as-of date, "
            "release date, retrieval time, and EIA attribution."
        ),
        "license_allows_excerpt": False,
        "excerpt_max_chars": None,
        "excerpt_max_words": None,
    },
    {
        "source_id": "federal_register_energy",
        "name": "FederalRegister.gov Energy Documents API",
        "base_url": "https://www.federalregister.gov/api/v1/documents.json",
        "source_version": "federal-register-api-v1",
        "classification": "primary",
        "entitlement": "Public FederalRegister.gov API; no API key required",
        "license_note": (
            "Public U.S. federal regulatory metadata and documents. Attribute FederalRegister.gov "
            "and link to canonical material; do not present non-official renditions as legal editions."
        ),
        "license_allows_excerpt": True,
        "excerpt_max_chars": 500,
        "excerpt_max_words": None,
    },
    {
        "source_id": "sec_edgar",
        "name": "SEC EDGAR Public Data",
        "base_url": "https://data.sec.gov/submissions/",
        "source_version": "sec-edgar-public-v1",
        "classification": "primary",
        "entitlement": "Public SEC data.sec.gov access; descriptive SEC_USER_AGENT required",
        "license_note": (
            "Public SEC filing metadata and company facts. Use configured CIK scopes only, respect "
            "SEC fair-access limits and conditional caching, and cite canonical SEC filing URLs."
        ),
        "license_allows_excerpt": False,
        "excerpt_max_chars": None,
        "excerpt_max_words": None,
    },
    {
        "source_id": "company_ir_registry",
        "name": "Versioned Company IR Registry",
        "base_url": "https://news.lockheedmartin.com/news-releases?category=788&pagetemplate=rss",
        "source_version": "company-ir-registry-2026.09-v1",
        "classification": "primary",
        "entitlement": "Reviewed issuer RSS/Atom/documented JSON endpoints; no generic crawling",
        "license_note": (
            "Official issuer investor-relations endpoints only. Preserve issuer, feed endpoint, "
            "publication timestamp, canonical URL, and attribution; no arbitrary URLs."
        ),
        "license_allows_excerpt": False,
        "excerpt_max_chars": None,
        "excerpt_max_words": None,
    },
    {
        "source_id": "issuer_etf_holdings",
        "name": "Reviewed ETF Issuer Holdings Files",
        "base_url": "https://www.ishares.com/us/products/239726/ishares-core-s-p-500-etf/latest-holdings.csv",
        "source_version": "issuer-etf-registry-2026.09-v1",
        "classification": "primary",
        "entitlement": "Reviewed direct issuer holdings downloads; no API key required",
        "license_note": (
            "Issuer-published ETF composition files. Preserve ticker, weight, as-of date, source URL, "
            "and retrieval time; never represent older as-of files as live holdings."
        ),
        "license_allows_excerpt": False,
        "excerpt_max_chars": None,
        "excerpt_max_words": None,
    },
    {
        "source_id": "technology_official_feeds",
        "name": "Technology Official Security Feeds",
        "base_url": "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json",
        "source_version": "technology-official-registry-2026.09-v1",
        "classification": "primary",
        "entitlement": "Reviewed government and vendor security advisory feeds; no generic search",
        "license_note": (
            "Official government/vendor security facts only. Begin with CISA KEV JSON and keep "
            "financial/product announcements in the company IR registry."
        ),
        "license_allows_excerpt": True,
        "excerpt_max_chars": 500,
        "excerpt_max_words": None,
    },
)

_ENDPOINTS: tuple[dict[str, Any], ...] = (
    {
        "source_id": "defense_gov_rss",
        "endpoint_id": "defense-gov-releases-rss",
        "base_url": "https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=9&Site=945&max=50",
        "host": "www.war.gov",
        "transport_kind": "rss_atom",
        "parser_kind": "rss",
        "registry_version": "defense-gov-rss-v1",
        "enabled": True,
        "expected_freshness_seconds": 600,
        "request_ceiling": 1,
        "license_note": "Official Defense.gov public RSS endpoint.",
        "retention_note": "Retain normalized metadata, canonical URL, timestamps, and short excerpt only.",
        "excerpt_allowed": True,
        "excerpt_max_chars": 300,
        "issuer_scope": (),
        "cik_scope": (),
        "ticker_scope": (),
    },
    {
        "source_id": "breaking_defense_public",
        "endpoint_id": "breaking-defense-wp-json",
        "base_url": "https://breakingdefense.com/wp-json/wp/v2/posts",
        "host": "breakingdefense.com",
        "transport_kind": "json_http",
        "parser_kind": "json",
        "registry_version": "wp-rest-v2-public",
        "enabled": True,
        "expected_freshness_seconds": 600,
        "request_ceiling": 1,
        "license_note": "Breaking Defense public WordPress posts endpoint.",
        "retention_note": "Retain headline, attribution, canonical URL, and approved short excerpt only.",
        "excerpt_allowed": True,
        "excerpt_max_chars": 200,
        "issuer_scope": (),
        "cik_scope": (),
        "ticker_scope": (),
    },
    {
        "source_id": "eia_public_data",
        "endpoint_id": "eia-petroleum-bulk-zip",
        "base_url": "https://www.eia.gov/opendata/bulk/PET.zip",
        "host": "www.eia.gov",
        "transport_kind": "bulk_file",
        "parser_kind": "json",
        "registry_version": "eia-public-v1",
        "enabled": True,
        "expected_freshness_seconds": 43200,
        "request_ceiling": 1,
        "license_note": "Official EIA public petroleum bulk ZIP.",
        "retention_note": "Cache unchanged artifacts conditionally and persist normalized series metadata.",
        "excerpt_allowed": False,
        "excerpt_max_chars": None,
        "issuer_scope": (),
        "cik_scope": (),
        "ticker_scope": (),
    },
    {
        "source_id": "eia_public_data",
        "endpoint_id": "eia-v2-api",
        "base_url": "https://api.eia.gov/v2/petroleum/pri/spt/data/",
        "host": "api.eia.gov",
        "transport_kind": "json_http",
        "parser_kind": "json",
        "registry_version": "eia-public-v1",
        "enabled": True,
        "expected_freshness_seconds": 3600,
        "request_ceiling": 5,
        "license_note": "Official EIA API v2 endpoint, used only when FINANCE_EIA_MODE=api and EIA_API_KEY is configured.",
        "retention_note": "Persist normalized series metadata and retrieval timestamps; never log API keys.",
        "excerpt_allowed": False,
        "excerpt_max_chars": None,
        "issuer_scope": (),
        "cik_scope": (),
        "ticker_scope": (),
    },
    {
        "source_id": "federal_register_energy",
        "endpoint_id": "federal-register-energy-json",
        "base_url": "https://www.federalregister.gov/api/v1/documents.json",
        "host": "www.federalregister.gov",
        "transport_kind": "json_http",
        "parser_kind": "json",
        "registry_version": "federal-register-api-v1",
        "enabled": True,
        "expected_freshness_seconds": 21600,
        "request_ceiling": 1,
        "license_note": "Public FederalRegister.gov documents API.",
        "retention_note": "Retain normalized regulatory metadata, canonical URLs, and short abstracts.",
        "excerpt_allowed": True,
        "excerpt_max_chars": 500,
        "issuer_scope": (),
        "cik_scope": (),
        "ticker_scope": (),
    },
    {
        "source_id": "sec_edgar",
        "endpoint_id": "sec-lmt-submissions",
        "base_url": "https://data.sec.gov/submissions/CIK0000936468.json",
        "host": "data.sec.gov",
        "transport_kind": "json_http",
        "parser_kind": "json",
        "registry_version": "sec-edgar-public-v1",
        "enabled": True,
        "expected_freshness_seconds": 600,
        "request_ceiling": 1,
        "license_note": "Public SEC submissions endpoint.",
        "retention_note": "Retain filing metadata, canonical filing URLs, timestamps, and CIK-scoped cache state.",
        "excerpt_allowed": False,
        "excerpt_max_chars": None,
        "issuer_scope": ("Lockheed Martin",),
        "cik_scope": ("0000936468",),
        "ticker_scope": ("LMT",),
    },
    {
        "source_id": "company_ir_registry",
        "endpoint_id": "lmt-official-news-rss",
        "base_url": "https://news.lockheedmartin.com/news-releases?category=788&pagetemplate=rss",
        "host": "news.lockheedmartin.com",
        "transport_kind": "rss_atom",
        "parser_kind": "rss",
        "registry_version": "company-ir-registry-2026.09-v1",
        "enabled": True,
        "expected_freshness_seconds": 600,
        "request_ceiling": 1,
        "license_note": "Official Lockheed Martin news releases RSS endpoint.",
        "retention_note": "Retain issuer, feed endpoint, canonical URL, timestamp, and short excerpt only.",
        "excerpt_allowed": False,
        "excerpt_max_chars": None,
        "issuer_scope": ("Lockheed Martin",),
        "cik_scope": ("0000936468",),
        "ticker_scope": ("LMT",),
    },
    {
        "source_id": "issuer_etf_holdings",
        "endpoint_id": "ivv-ishares-latest-holdings-csv",
        "base_url": "https://www.ishares.com/us/products/239726/ishares-core-s-p-500-etf/latest-holdings.csv",
        "host": "www.ishares.com",
        "transport_kind": "bulk_file",
        "parser_kind": "csv",
        "registry_version": "issuer-etf-registry-2026.09-v1",
        "enabled": True,
        "expected_freshness_seconds": 86400,
        "request_ceiling": 1,
        "license_note": "Reviewed iShares IVV latest holdings CSV download.",
        "retention_note": "Persist normalized ticker, weight, source URL, retrieval time, and issuer as-of date.",
        "excerpt_allowed": False,
        "excerpt_max_chars": None,
        "issuer_scope": ("iShares",),
        "cik_scope": (),
        "ticker_scope": ("IVV",),
    },
    {
        "source_id": "technology_official_feeds",
        "endpoint_id": "cisa-kev-json",
        "base_url": "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json",
        "host": "www.cisa.gov",
        "transport_kind": "json_http",
        "parser_kind": "json",
        "registry_version": "technology-official-registry-2026.09-v1",
        "enabled": True,
        "expected_freshness_seconds": 600,
        "request_ceiling": 1,
        "license_note": "Official CISA Known Exploited Vulnerabilities JSON feed.",
        "retention_note": "Retain normalized vulnerability facts, source URL, timestamps, and vendor/product metadata.",
        "excerpt_allowed": True,
        "excerpt_max_chars": 500,
        "issuer_scope": (),
        "cik_scope": (),
        "ticker_scope": (),
    },
)


def upgrade() -> None:
    op.add_column("finance_etf_exposures", sa.Column("source_url", sa.String(2048)))
    op.add_column("finance_etf_exposures", sa.Column("retrieved_at", _TS))

    op.create_table(
        "finance_source_endpoints",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("allowlist_version", sa.String(128), nullable=False),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("endpoint_id", sa.String(128), nullable=False),
        sa.Column("base_url", sa.String(2048), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("transport_kind", sa.String(32), nullable=False),
        sa.Column("parser_kind", sa.String(32), nullable=False),
        sa.Column("registry_version", sa.String(128), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("expected_freshness_seconds", sa.Integer, nullable=False),
        sa.Column("request_ceiling", sa.Integer, nullable=False),
        sa.Column("license_note", sa.String(1000), nullable=False),
        sa.Column("retention_note", sa.String(1000), nullable=False),
        sa.Column("excerpt_allowed", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("excerpt_max_chars", sa.Integer),
        sa.Column("issuer_scope", sa.JSON, nullable=False, server_default=_JSON_LIST),
        sa.Column("cik_scope", sa.JSON, nullable=False, server_default=_JSON_LIST),
        sa.Column("ticker_scope", sa.JSON, nullable=False, server_default=_JSON_LIST),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(
            ["source_id", "allowlist_version"],
            [
                "finance_approved_sources.source_id",
                "finance_approved_sources.allowlist_version",
            ],
            ondelete="CASCADE",
            name="fk_finance_source_endpoints_source_allowlist",
        ),
        sa.UniqueConstraint(
            "allowlist_version",
            "source_id",
            "endpoint_id",
            name="uq_finance_source_endpoints_allowlist_source_endpoint",
        ),
        sa.CheckConstraint("length(allowlist_version) > 0", name="allowlist_version_nonempty"),
        sa.CheckConstraint("length(source_id) > 0", name="source_id_nonempty"),
        sa.CheckConstraint("length(endpoint_id) > 0", name="endpoint_id_nonempty"),
        sa.CheckConstraint("length(base_url) > 0", name="base_url_nonempty"),
        sa.CheckConstraint("length(host) > 0", name="host_nonempty"),
        sa.CheckConstraint(
            "transport_kind IN ('json_http','rss_atom','bulk_file')",
            name="transport_kind_valid",
        ),
        sa.CheckConstraint(
            "parser_kind IN ('json','rss','atom','csv','xls','xlsx')",
            name="parser_kind_valid",
        ),
        sa.CheckConstraint("length(registry_version) > 0", name="registry_version_nonempty"),
        sa.CheckConstraint("expected_freshness_seconds > 0", name="expected_freshness_positive"),
        sa.CheckConstraint("request_ceiling > 0", name="request_ceiling_positive"),
        sa.CheckConstraint("length(license_note) > 0", name="license_note_nonempty"),
        sa.CheckConstraint("length(retention_note) > 0", name="retention_note_nonempty"),
        sa.CheckConstraint(
            "excerpt_allowed OR excerpt_max_chars IS NULL",
            name="endpoint_excerpt_limit_requires_permission",
        ),
        sa.CheckConstraint(
            "excerpt_max_chars IS NULL OR (excerpt_max_chars >= 1 AND excerpt_max_chars <= 500)",
            name="endpoint_excerpt_max_chars_valid",
        ),
    )
    op.create_index(
        "ix_finance_source_endpoints_allowlist_source",
        "finance_source_endpoints",
        ["allowlist_version", "source_id", "enabled"],
    )

    op.create_table(
        "finance_source_cache_state",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("allowlist_version", sa.String(128), nullable=False),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("endpoint_id", sa.String(128), nullable=False),
        sa.Column("etag", sa.String(512)),
        sa.Column("last_modified", sa.String(512)),
        sa.Column("watermark_external_id", sa.String(512)),
        sa.Column("watermark_published_at", _TS),
        sa.Column("cached_artifact_key", sa.String(512)),
        sa.Column("payload_sha256", sa.String(64)),
        sa.Column("last_retrieved_at", _TS),
        sa.Column("last_not_modified_at", _TS),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(
            ["allowlist_version", "source_id", "endpoint_id"],
            [
                "finance_source_endpoints.allowlist_version",
                "finance_source_endpoints.source_id",
                "finance_source_endpoints.endpoint_id",
            ],
            ondelete="CASCADE",
            name="fk_finance_source_cache_state_endpoint",
        ),
        sa.UniqueConstraint(
            "allowlist_version",
            "source_id",
            "endpoint_id",
            name="uq_finance_source_cache_state_endpoint",
        ),
        sa.CheckConstraint("length(allowlist_version) > 0", name="allowlist_version_nonempty"),
        sa.CheckConstraint("length(source_id) > 0", name="source_id_nonempty"),
        sa.CheckConstraint("length(endpoint_id) > 0", name="endpoint_id_nonempty"),
    )
    op.create_index(
        "ix_finance_source_cache_state_watermark",
        "finance_source_cache_state",
        ["allowlist_version", "source_id", "watermark_published_at"],
    )

    op.create_table(
        "finance_source_request_audits",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("allowlist_version", sa.String(128), nullable=False),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("endpoint_id", sa.String(128), nullable=False),
        sa.Column("requested_at", _TS, nullable=False),
        sa.Column("status_code", sa.Integer),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("error_code", sa.String(128)),
        sa.Column("not_modified", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(
            ["allowlist_version", "source_id", "endpoint_id"],
            [
                "finance_source_endpoints.allowlist_version",
                "finance_source_endpoints.source_id",
                "finance_source_endpoints.endpoint_id",
            ],
            ondelete="RESTRICT",
            name="fk_finance_source_request_audits_endpoint",
        ),
        sa.CheckConstraint(
            "outcome IN ('succeeded','not_modified','failed')",
            name="outcome_valid",
        ),
    )
    op.create_index(
        "ix_finance_source_request_audits_endpoint_time",
        "finance_source_request_audits",
        ["allowlist_version", "source_id", "endpoint_id", "requested_at"],
    )

    bind = op.get_bind()
    audit_events = _audit_events_table()
    approved_sources = _approved_sources_table()
    endpoints = _endpoints_table()

    if (
        bind.execute(
            sa.select(audit_events.c.id).where(audit_events.c.id == ALLOWLIST_AUDIT_ID)
        ).first()
        is None
    ):
        bind.execute(
            audit_events.insert().values(
                id=ALLOWLIST_AUDIT_ID,
                created_at=RECORDED_AT,
                updated_at=RECORDED_AT,
                actor="richard",
                action="record_finance_source_allowlist",
                target_type="finance_source_allowlist",
                target_id=ALLOWLIST_VERSION,
                result="recorded",
                run_id=None,
                artifact_key=None,
            )
        )

    for source in _SOURCES:
        source_exists = bind.execute(
            sa.select(approved_sources.c.id).where(
                approved_sources.c.source_id == source["source_id"],
                approved_sources.c.allowlist_version == ALLOWLIST_VERSION,
            )
        ).first()
        if source_exists is None:
            bind.execute(
                approved_sources.insert().values(
                    id=uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"lifeagent:{ALLOWLIST_VERSION}:{source['source_id']}",
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
                    created_at=RECORDED_AT,
                    updated_at=RECORDED_AT,
                )
            )

    for endpoint in _ENDPOINTS:
        endpoint_exists = bind.execute(
            sa.select(endpoints.c.id).where(
                endpoints.c.allowlist_version == ALLOWLIST_VERSION,
                endpoints.c.source_id == endpoint["source_id"],
                endpoints.c.endpoint_id == endpoint["endpoint_id"],
            )
        ).first()
        if endpoint_exists is not None:
            continue
        bind.execute(
            endpoints.insert().values(
                id=uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    (
                        f"lifeagent:{ALLOWLIST_VERSION}:"
                        f"{endpoint['source_id']}:{endpoint['endpoint_id']}"
                    ),
                ),
                allowlist_version=ALLOWLIST_VERSION,
                source_id=endpoint["source_id"],
                endpoint_id=endpoint["endpoint_id"],
                base_url=endpoint["base_url"],
                host=endpoint["host"],
                transport_kind=endpoint["transport_kind"],
                parser_kind=endpoint["parser_kind"],
                registry_version=endpoint["registry_version"],
                enabled=endpoint["enabled"],
                expected_freshness_seconds=endpoint["expected_freshness_seconds"],
                request_ceiling=endpoint["request_ceiling"],
                license_note=endpoint["license_note"],
                retention_note=endpoint["retention_note"],
                excerpt_allowed=endpoint["excerpt_allowed"],
                excerpt_max_chars=endpoint["excerpt_max_chars"],
                issuer_scope=list(endpoint["issuer_scope"]),
                cik_scope=list(endpoint["cik_scope"]),
                ticker_scope=list(endpoint["ticker_scope"]),
                created_at=RECORDED_AT,
                updated_at=RECORDED_AT,
            )
        )


def downgrade() -> None:
    """Refuse destructive rollback; switch the configured allowlist to v1."""

    raise RuntimeError(
        "0010 is history-preserving; rollback with FINANCE_SOURCE_ALLOWLIST_VERSION"
    )


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


def _endpoints_table() -> sa.Table:
    return sa.table(
        "finance_source_endpoints",
        sa.column("id", _UUID),
        sa.column("allowlist_version", sa.String(128)),
        sa.column("source_id", sa.String(64)),
        sa.column("endpoint_id", sa.String(128)),
        sa.column("base_url", sa.String(2048)),
        sa.column("host", sa.String(255)),
        sa.column("transport_kind", sa.String(32)),
        sa.column("parser_kind", sa.String(32)),
        sa.column("registry_version", sa.String(128)),
        sa.column("enabled", sa.Boolean()),
        sa.column("expected_freshness_seconds", sa.Integer()),
        sa.column("request_ceiling", sa.Integer()),
        sa.column("license_note", sa.String(1000)),
        sa.column("retention_note", sa.String(1000)),
        sa.column("excerpt_allowed", sa.Boolean()),
        sa.column("excerpt_max_chars", sa.Integer()),
        sa.column("issuer_scope", sa.JSON()),
        sa.column("cik_scope", sa.JSON()),
        sa.column("ticker_scope", sa.JSON()),
        sa.column("created_at", _TS),
        sa.column("updated_at", _TS),
    )
