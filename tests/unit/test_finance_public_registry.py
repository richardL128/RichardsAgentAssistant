from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
import pytest

from app.agents.finance.contracts import SourceApproval
from app.agents.finance.sources import (
    FINANCE_SOURCE_ALLOWLIST_VERSION_V2,
    PUBLIC_FINANCE_SOURCE_IDS,
    build_finance_adapter_registry,
)
from app.connectors.finance_sources.adapters import (
    build_source_queries,
    fetch_exactly_eight_sources,
)
from app.connectors.finance_sources.transport import EndpointRequestAudit
from app.core.config import Settings

FIXTURES = Path(__file__).parent.parent / "fixtures" / "finance" / "providers"
NOW = datetime(2026, 9, 7, 14, 30, tzinfo=UTC)

_SOURCE_ROWS = {
    "defense_gov_rss": (
        "defense-gov-rss-v1",
        "https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=9&Site=945&max=50",
        "primary",
        True,
        300,
    ),
    "breaking_defense_public": (
        "wp-rest-v2-public",
        "https://breakingdefense.com/wp-json/wp/v2/posts",
        "reported",
        True,
        200,
    ),
    "eia_public_data": (
        "eia-public-v1",
        "https://www.eia.gov/opendata/bulk/PET.zip",
        "primary",
        False,
        None,
    ),
    "federal_register_energy": (
        "federal-register-api-v1",
        "https://www.federalregister.gov/api/v1/documents.json",
        "primary",
        True,
        500,
    ),
    "sec_edgar": (
        "sec-edgar-public-v1",
        "https://data.sec.gov/submissions/",
        "primary",
        False,
        None,
    ),
    "company_ir_registry": (
        "company-ir-registry-2026.09-v1",
        "https://news.lockheedmartin.com/news-releases?category=788&pagetemplate=rss",
        "primary",
        False,
        None,
    ),
    "issuer_etf_holdings": (
        "issuer-etf-registry-2026.09-v1",
        "https://www.ishares.com/us/products/239726/ishares-core-s-p-500-etf/latest-holdings.csv",
        "primary",
        False,
        None,
    ),
    "technology_official_feeds": (
        "technology-official-registry-2026.09-v1",
        "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json",
        "primary",
        True,
        500,
    ),
}


def _approvals() -> tuple[SourceApproval, ...]:
    return tuple(
        SourceApproval.model_validate(
            {
                "source_id": source_id,
                "name": source_id,
                "base_url": base_url,
                "source_version": version,
                "allowlist_version": FINANCE_SOURCE_ALLOWLIST_VERSION_V2,
                "license_note": "Reviewed public endpoint metadata and permitted excerpts only.",
                "entitlement": "Public endpoint; no paid vendor credential required.",
                "classification": classification,
                "license_allows_excerpt": excerpt_allowed,
                "excerpt_max_chars": excerpt_max_chars,
                "enabled": True,
                "approved_at": NOW,
                "approval_audit_id": uuid.uuid4(),
            }
        )
        for source_id, (
            version,
            base_url,
            classification,
            excerpt_allowed,
            excerpt_max_chars,
        ) in _SOURCE_ROWS.items()
    )


def _eia_zip() -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "PET.txt",
            b'{"series_id":"PET.RWTC.D","name":"WTI spot price",'
            b'"units":"dollars per barrel","frequency":"daily",'
            b'"last_updated":"2026-09-07T13:00:00Z",'
            b'"data":[["2026-09-07",65.5]]}\n',
        )
    return output.getvalue()


@pytest.mark.asyncio
async def test_v2_registry_fetches_exactly_eight_keyless_public_envelopes() -> None:
    requests: list[httpx.Request] = []
    audits: list[tuple[str, EndpointRequestAudit]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        host = request.url.host
        if host == "www.war.gov":
            return httpx.Response(200, content=(FIXTURES / "defense_gov_rss.xml").read_bytes())
        if host == "breakingdefense.com":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 42,
                        "date_gmt": "2026-09-07T13:05:00Z",
                        "title": {"rendered": "Breaking Defense reports an award"},
                        "link": "https://breakingdefense.com/2026/09/reviewed-award/",
                        "excerpt": {"rendered": "Independent short discovery excerpt."},
                    }
                ],
            )
        if host == "www.eia.gov":
            return httpx.Response(200, content=_eia_zip(), headers={"etag": '"pet-v1"'})
        if host == "www.federalregister.gov":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "document_number": "2026-12345",
                            "title": "DOE publishes an energy rule",
                            "html_url": "https://www.federalregister.gov/d/2026-12345",
                            "publication_date": "2026-09-07",
                            "abstract": "Official regulatory summary.",
                        }
                    ]
                },
            )
        if host == "data.sec.gov":
            assert "LifeAgent/0.1" in request.headers["user-agent"]
            assert "@" in request.headers["user-agent"]
            return httpx.Response(200, content=(FIXTURES / "sec_submissions_lmt.json").read_bytes())
        if host == "news.lockheedmartin.com":
            return httpx.Response(200, content=(FIXTURES / "company_ir_lmt.xml").read_bytes())
        if host == "www.ishares.com":
            return httpx.Response(200, content=(FIXTURES / "ishares_ivv.csv").read_bytes())
        if host == "www.cisa.gov":
            return httpx.Response(200, content=(FIXTURES / "cisa_kev.json").read_bytes())
        raise AssertionError(f"unexpected public request host: {host}")

    settings = Settings.model_validate(
        {
            "finance_source_allowlist_version": FINANCE_SOURCE_ALLOWLIST_VERSION_V2,
            "finance_eia_mode": "bulk",
            "eia_api_key": None,
        }
    )
    approvals = _approvals()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapters = build_finance_adapter_registry(
            settings,
            approvals,
            client=client,
            request_audit=lambda source_id, audit: audits.append((source_id, audit)),
        )
        results = await fetch_exactly_eight_sources(
            adapters=adapters,
            approved_sources=approvals,
            allowlist_version=FINANCE_SOURCE_ALLOWLIST_VERSION_V2,
            window_start=NOW - timedelta(days=7),
            window_end=NOW,
            tickers=("LMT",),
            themes=("defense", "energy", "technology-security"),
            issuer_tickers=("LMT",),
            etf_tickers=("IVV",),
        )

    assert set(adapters) == set(PUBLIC_FINANCE_SOURCE_IDS)
    assert len(results) == 8
    assert {result.source_id for result in results} == set(PUBLIC_FINANCE_SOURCE_IDS)
    assert len(requests) == 8
    assert len(audits) == 8
    assert all(result.metadata is not None for result in results)
    assert (
        sum(result.metadata.request_count for result in results if result.metadata is not None) == 8
    )
    assert all(result.failure is None for result in results)
    assert all("api_key" not in request.url.params for request in requests)
    etf_result = next(result for result in results if result.source_id == "issuer_etf_holdings")
    assert {(row.etf_symbol, row.underlying_symbol) for row in etf_result.etf_exposures} == {
        ("IVV", "LMT"),
        ("IVV", "MSFT"),
    }
    serialized = json.dumps([result.model_dump(mode="json") for result in results])
    assert "Independent short discovery excerpt" in serialized
    assert "secret" not in serialized.casefold()


@pytest.mark.asyncio
async def test_registry_missing_mapping_is_visible_without_substitute_request() -> None:
    requests: list[httpx.Request] = []
    settings = Settings.model_validate(
        {
            "finance_source_allowlist_version": FINANCE_SOURCE_ALLOWLIST_VERSION_V2,
        }
    )
    approvals = _approvals()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=(FIXTURES / "company_ir_lmt.xml").read_bytes())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = build_finance_adapter_registry(settings, approvals, client=client)[
            "company_ir_registry"
        ]
        result = await adapter.fetch(
            next(
                query
                for query in build_source_queries(
                    approvals,
                    allowlist_version=FINANCE_SOURCE_ALLOWLIST_VERSION_V2,
                    window_start=NOW - timedelta(days=1),
                    window_end=NOW,
                    tickers=("LMT", "MSFT"),
                    issuer_tickers=("LMT", "MSFT"),
                    themes=("defense",),
                )
                if query.source_id == "company_ir_registry"
            )
        )

    assert len(requests) == 1
    assert result.failure is None
    assert result.metadata is not None
    assert [failure.error_code for failure in result.metadata.endpoint_failures] == [
        "company_ir_mapping_missing"
    ]
