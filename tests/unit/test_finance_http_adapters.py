from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.agents.finance.contracts import (
    SourceApproval,
    SourceClassification,
    SourceFetchResult,
    SourceQuery,
)
from app.connectors.finance_sources.http import (
    AuthStyle,
    HttpAuthConfig,
    HttpFinanceSourceAdapter,
    HttpRequestConfig,
    SourceParser,
    parser_for_source,
)

NOW = datetime(2026, 9, 4, 13, tzinfo=UTC)
WINDOW_START = NOW - timedelta(hours=1)
WINDOW_END = NOW + timedelta(hours=1)


def _approval(
    source_id: str,
    *,
    source_version: str = "v1",
    base_url: str | None = None,
    classification: SourceClassification = SourceClassification.REPORTED,
    license_allows_excerpt: bool = True,
    excerpt_max_chars: int | None = None,
    excerpt_max_words: int | None = None,
) -> SourceApproval:
    return SourceApproval(
        source_id=source_id,
        name=source_id,
        base_url=base_url or f"https://{source_id}.example/feed",
        source_version=source_version,
        allowlist_version="finance-sources-2026.09",
        license_note="test license",
        entitlement="test entitlement",
        classification=classification,
        license_allows_excerpt=license_allows_excerpt,
        excerpt_max_chars=excerpt_max_chars,
        excerpt_max_words=excerpt_max_words,
        enabled=False,
    )


def _query(source_id: str, *, source_version: str = "v1") -> SourceQuery:
    return SourceQuery(
        source_id=source_id,
        source_version=source_version,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        tickers=("ACME",),
        themes=("defense",),
    )


def _article(
    *,
    title: str = "ACME reports defense contract",
    url: str = "https://news.example/acme",
    source_version: str = "v1",
    published_at: str = "2026-09-04T13:00:00Z",
) -> dict[str, Any]:
    return {
        "id": "doc-1",
        "title": title,
        "url": url,
        "source_version": source_version,
        "published_at": published_at,
        "summary": "A concise licensed summary about ACME.",
        "tickers": ["ACME"],
        "themes": ["defense"],
    }


async def _fetch_with_payload(
    source_id: str,
    payload: object,
    *,
    approval: SourceApproval | None = None,
    auth: HttpAuthConfig | None = None,
    request_config: HttpRequestConfig | None = None,
    parser: SourceParser | None = None,
) -> tuple[SourceFetchResult, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = HttpFinanceSourceAdapter(
            source_id=source_id,
            approval=approval or _approval(source_id),
            client=client,
            parser=parser or parser_for_source(source_id),
            auth=auth or HttpAuthConfig(AuthStyle.NONE),
            request=request_config or HttpRequestConfig(),
            clock=lambda: NOW,
        )
        result = await adapter.fetch(_query(source_id))
    return result, requests


@pytest.mark.parametrize(
    ("source_id", "payload"),
    [
        (
            "dvids",
            {
                "results": [
                    {
                        **_article(title="DVIDS tracks ACME procurement"),
                        "date_published": "2026-09-04T13:00:00Z",
                        "short_description": "Official DoD public media summary.",
                    }
                ]
            },
        ),
        (
            "breaking_defense",
            {"items": [_article(title="Breaking Defense covers ACME award")]},
        ),
        (
            "eia_open_data",
            {
                "data": [
                    _article(
                        title="EIA reports power generation demand",
                        url="https://api.eia.gov/v2/",
                    )
                ]
            },
        ),
        (
            "federal_register_energy",
            {
                "results": [
                    {
                        **_article(title="DOE publishes an energy rule"),
                        "document_number": "2026-12345",
                        "html_url": "https://www.federalregister.gov/d/2026-12345",
                        "publication_date": "2026-09-04",
                        "abstract": "Department of Energy regulatory summary.",
                    }
                ]
            },
        ),
        (
            "alpha_vantage_news",
            {
                "feed": [
                    {
                        **_article(title="Alpha Vantage carries ACME market update"),
                        "time_published": "20260904T130000",
                    }
                ]
            },
        ),
        (
            "benzinga_news",
            {"news": [_article(title="Benzinga reports ACME executive comment")]},
        ),
        (
            "fmp_etf",
            {
                "holdings": [
                    {
                        **_article(
                            title="ACME appears in ETF holdings",
                            url="https://financialmodelingprep.com/api/v3/etf-holder/ETF",
                        ),
                        "weightPercentage": "4.25",
                        "metric": "ETF holding weight",
                    }
                ]
            },
        ),
        (
            "alpha_vantage_etf",
            {
                "holdings": [
                    {
                        **_article(title="ACME ETF holding"),
                        "symbol": "ACME",
                        "weight": "4.25",
                    }
                ]
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_each_supported_source_parser_maps_one_payload(
    source_id: str, payload: object
) -> None:
    result, requests = await _fetch_with_payload(source_id, payload)

    assert result.failure is None
    assert len(result.documents) == 1
    assert len(requests) == 1
    document = result.documents[0]
    assert document.source_id == source_id
    assert document.source_version == "v1"
    assert document.classification == SourceClassification.REPORTED
    assert document.license_allows_excerpt is True
    assert document.tickers == ("ACME",)


@pytest.mark.parametrize(
    (
        "source_id",
        "auth",
        "expected_query_name",
        "expected_query_value",
        "expected_header_name",
        "expected_header_value",
    ),
    [
        (
            "alpha_vantage_news",
            HttpAuthConfig(AuthStyle.QUERY_PARAM, "alpha-key", "apikey"),
            "apikey",
            "alpha-key",
            None,
            None,
        ),
        (
            "benzinga_news",
            HttpAuthConfig(AuthStyle.HEADER, "benzinga-token", header="token"),
            None,
            None,
            "token",
            "benzinga-token",
        ),
        (
            "dvids",
            HttpAuthConfig(AuthStyle.QUERY_PARAM, "dvids-key", "api_key"),
            "api_key",
            "dvids-key",
            None,
            None,
        ),
        (
            "federal_register_energy",
            HttpAuthConfig(AuthStyle.NONE),
            None,
            None,
            None,
            None,
        ),
        ("breaking_defense", HttpAuthConfig(AuthStyle.NONE), None, None, None, None),
    ],
)
@pytest.mark.asyncio
async def test_adapter_applies_auth_and_query_shape(
    source_id: str,
    auth: HttpAuthConfig,
    expected_query_name: str | None,
    expected_query_value: str | None,
    expected_header_name: str | None,
    expected_header_value: str | None,
) -> None:
    result, requests = await _fetch_with_payload(source_id, {"items": [_article()]}, auth=auth)

    assert result.failure is None
    assert len(requests) == 1
    request = requests[0]
    assert request.url.params["window_start"] == WINDOW_START.isoformat()
    assert request.url.params["window_end"] == WINDOW_END.isoformat()
    assert request.url.params["tickers"] == "ACME"
    assert request.url.params["themes"] == "defense"
    if expected_query_name is not None:
        assert request.url.params[expected_query_name] == expected_query_value
    if expected_header_name is not None:
        assert request.headers[expected_header_name] == expected_header_value
    if expected_header_name != "Authorization":
        assert "Authorization" not in request.headers


@pytest.mark.asyncio
async def test_adapter_respects_existing_endpoint_query_params() -> None:
    approval = _approval(
        "alpha_vantage_news",
        base_url="https://www.alphavantage.co/query?function=NEWS_SENTIMENT",
    )

    result, requests = await _fetch_with_payload(
        "alpha_vantage_news",
        {"feed": [_article()]},
        approval=approval,
        auth=HttpAuthConfig(AuthStyle.QUERY_PARAM, "alpha-key", "apikey"),
    )

    assert result.failure is None
    assert len(requests) == 1
    assert request_url_param(requests[0], "function") == "NEWS_SENTIMENT"
    assert request_url_param(requests[0], "apikey") == "alpha-key"


@pytest.mark.asyncio
async def test_adapter_does_not_emit_disallowed_excerpts_and_clamps_allowed_text() -> None:
    disallowed = await _fetch_with_payload(
        "alpha_vantage_etf",
        {"items": [{**_article(), "body": "This body must not be exposed."}]},
        approval=_approval("alpha_vantage_etf", license_allows_excerpt=False),
    )
    assert disallowed[0].failure is None
    assert disallowed[0].documents[0].excerpt is None

    char_limited = await _fetch_with_payload(
        "breaking_defense",
        {"items": [{**_article(), "summary": "abcdefghijklmnopqrstuvwxyz"}]},
        approval=_approval("breaking_defense", excerpt_max_chars=12),
    )
    assert char_limited[0].documents[0].excerpt == "abcdefghijkl"

    word_limited = await _fetch_with_payload(
        "breaking_defense",
        {"items": [{**_article(), "summary": "one two three four five"}]},
        approval=_approval("breaking_defense", excerpt_max_words=3),
    )
    assert word_limited[0].documents[0].excerpt == "one two three"


@pytest.mark.asyncio
async def test_adapter_filters_stale_and_wrong_version_documents() -> None:
    payload = {
        "items": [
            _article(title="stale", published_at="2026-08-01T13:00:00Z"),
            _article(title="wrong version", source_version="old-version"),
            _article(title="fresh matching item"),
        ]
    }

    result, requests = await _fetch_with_payload("breaking_defense", payload)

    assert result.failure is None
    assert len(result.documents) == 1
    assert result.documents[0].title == "fresh matching item"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_adapter_returns_timeout_failure_without_leaking_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadTimeout("slow", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = HttpFinanceSourceAdapter(
            source_id="benzinga_news",
            approval=_approval("benzinga_news"),
            client=client,
            parser=parser_for_source("benzinga_news"),
            auth=HttpAuthConfig(AuthStyle.HEADER, "super-secret-token", header="token"),
            clock=lambda: NOW,
        )
        result = await adapter.fetch(_query("benzinga_news"))

    assert len(requests) == 1
    assert result.failure is not None
    assert result.failure.error_code == "connector_timeout"
    assert "super-secret-token" not in result.failure.diagnostic


@pytest.mark.asyncio
async def test_adapter_returns_http_failure_without_body_or_fallback() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, json={"raw_body": "licensed text"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = HttpFinanceSourceAdapter(
            source_id="alpha_vantage_etf",
            approval=_approval("alpha_vantage_etf"),
            client=client,
            parser=parser_for_source("alpha_vantage_etf"),
            clock=lambda: NOW,
        )
        result = await adapter.fetch(_query("alpha_vantage_etf"))

    assert len(requests) == 1
    assert result.failure is not None
    assert result.failure.error_code == "connector_http_status"
    assert "503" in result.failure.diagnostic
    assert "licensed text" not in result.failure.diagnostic
    assert "search" not in result.failure.diagnostic


@pytest.mark.asyncio
async def test_adapter_returns_malformed_json_failure() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"{")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = HttpFinanceSourceAdapter(
            source_id="eia_open_data",
            approval=_approval("eia_open_data"),
            client=client,
            parser=parser_for_source("eia_open_data"),
            clock=lambda: NOW,
        )
        result = await adapter.fetch(_query("eia_open_data"))

    assert len(requests) == 1
    assert result.failure is not None
    assert result.failure.error_code == "malformed_json"


@pytest.mark.asyncio
async def test_adapter_returns_parser_failure() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"items": [_article()]})

    def parser(*_: object) -> tuple[()]:
        raise ValueError("do not leak this parser detail")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = HttpFinanceSourceAdapter(
            source_id="fmp_etf",
            approval=_approval("fmp_etf"),
            client=client,
            parser=parser,
            clock=lambda: NOW,
        )
        result = await adapter.fetch(_query("fmp_etf"))

    assert len(requests) == 1
    assert result.failure is not None
    assert result.failure.error_code == "parser_failed"
    assert "do not leak" not in result.failure.diagnostic


@pytest.mark.asyncio
async def test_adapter_reports_missing_auth_without_request() -> None:
    requests: list[httpx.Request] = []

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(200)
        )
    ) as client:
        adapter = HttpFinanceSourceAdapter(
            source_id="alpha_vantage_news",
            approval=_approval("alpha_vantage_news"),
            client=client,
            parser=parser_for_source("alpha_vantage_news"),
            auth=HttpAuthConfig(AuthStyle.QUERY_PARAM, None, "apikey"),
            clock=lambda: NOW,
        )
        result = await adapter.fetch(_query("alpha_vantage_news"))

    assert requests == []
    assert result.failure is not None
    assert result.failure.error_code == "connector_auth"


def request_url_param(request: httpx.Request, name: str) -> str:
    value = request.url.params[name]
    assert value is not None
    return value
