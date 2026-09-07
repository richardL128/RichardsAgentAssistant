"""Reviewed v2 public finance source registries."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.connectors.finance_sources.definitions import (
    ParserKind,
    SourceDefinition,
    SourceEndpointDefinition,
    TransportKind,
)

ALLOWLIST_VERSION_V2 = "finance-sources-2026.09-v2"

DEFENSE_GOV_RSS = "defense_gov_rss"
EIA_PUBLIC_DATA = "eia_public_data"
SEC_EDGAR = "sec_edgar"
COMPANY_IR_REGISTRY = "company_ir_registry"
ISSUER_ETF_HOLDINGS = "issuer_etf_holdings"
TECHNOLOGY_OFFICIAL_FEEDS = "technology_official_feeds"

DEFENSE_SOURCE_VERSION = "defense-gov-rss-v1"
EIA_SOURCE_VERSION = "eia-public-v1"
SEC_SOURCE_VERSION = "sec-edgar-public-v1"
COMPANY_IR_SOURCE_VERSION = "company-ir-registry-2026.09-v1"
ETF_SOURCE_VERSION = "issuer-etf-registry-2026.09-v1"
TECH_SOURCE_VERSION = "technology-official-registry-2026.09-v1"


class EiaMode(StrEnum):
    BULK = "bulk"
    API = "api"


@dataclass(frozen=True, slots=True)
class SecCompany:
    ticker: str
    cik: str
    issuer: str
    forms: tuple[str, ...] = ("8-K", "10-Q", "10-K")

    @property
    def endpoint_id(self) -> str:
        return f"sec-{self.ticker.casefold()}-submissions"

    @property
    def submissions_url(self) -> str:
        return f"https://data.sec.gov/submissions/CIK{self.cik}.json"


@dataclass(frozen=True, slots=True)
class CompanyIrFeed:
    ticker: str
    issuer: str
    endpoint_id: str
    feed_url: str
    allowed_host: str


@dataclass(frozen=True, slots=True)
class EtfHoldingsEndpoint:
    ticker: str
    issuer: str
    endpoint_id: str
    holdings_url: str
    allowed_host: str
    parser: str


SEC_TICKER_REGISTRY: dict[str, SecCompany] = {
    "LMT": SecCompany(ticker="LMT", cik="0000936468", issuer="Lockheed Martin Corporation"),
}

COMPANY_IR_REGISTRY_FEEDS: dict[str, CompanyIrFeed] = {
    "LMT": CompanyIrFeed(
        ticker="LMT",
        issuer="Lockheed Martin Corporation",
        endpoint_id="lmt-official-news-rss",
        feed_url="https://news.lockheedmartin.com/news-releases?category=788&pagetemplate=rss",
        allowed_host="news.lockheedmartin.com",
    ),
}

ETF_HOLDINGS_REGISTRY: dict[str, EtfHoldingsEndpoint] = {
    "IVV": EtfHoldingsEndpoint(
        ticker="IVV",
        issuer="BlackRock iShares",
        endpoint_id="ivv-ishares-latest-holdings-csv",
        holdings_url=(
            "https://www.ishares.com/us/products/239726/ishares-core-s-p-500-etf/"
            "latest-holdings.csv"
        ),
        allowed_host="www.ishares.com",
        parser="ishares_csv",
    ),
}


def defense_gov_definition() -> SourceDefinition:
    return SourceDefinition(
        source_id=DEFENSE_GOV_RSS,
        source_version=DEFENSE_SOURCE_VERSION,
        allowlist_version=ALLOWLIST_VERSION_V2,
        max_fanout=1,
        endpoints=(
            SourceEndpointDefinition(
                endpoint_id="defense-gov-releases-rss",
                url="https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=9&Site=945&max=50",
                allowed_host="www.war.gov",
                transport_kind=TransportKind.RSS_ATOM,
                parser_kind=ParserKind.RSS,
                registry_version="defense-gov-rss-v1",
                expected_freshness_seconds=600,
                request_ceiling=1,
                license_note="Official Defense.gov public RSS metadata and short excerpt.",
                excerpt_allowed=True,
                excerpt_max_chars=300,
            ),
        ),
    )


def eia_public_definition(mode: EiaMode = EiaMode.BULK) -> SourceDefinition:
    endpoint = (
        SourceEndpointDefinition(
            endpoint_id="eia-petroleum-bulk-zip",
            url="https://www.eia.gov/opendata/bulk/PET.zip",
            allowed_host="www.eia.gov",
            transport_kind=TransportKind.BULK_FILE,
            parser_kind=ParserKind.JSON,
            registry_version="eia-public-v1",
            expected_freshness_seconds=43_200,
            request_ceiling=1,
            license_note="Official EIA open data bulk files; public domain US government data.",
            excerpt_allowed=False,
        )
        if mode is EiaMode.BULK
        else SourceEndpointDefinition(
            endpoint_id="eia-v2-api",
            url="https://api.eia.gov/v2/petroleum/pri/spt/data/",
            allowed_host="api.eia.gov",
            transport_kind=TransportKind.JSON_HTTP,
            parser_kind=ParserKind.JSON,
            registry_version="eia-public-v1",
            expected_freshness_seconds=3_600,
            request_ceiling=1,
            license_note=(
                "Official EIA v2 API; optional key accelerator, no fallback from selected mode."
            ),
            excerpt_allowed=False,
        )
    )
    return SourceDefinition(
        source_id=EIA_PUBLIC_DATA,
        source_version=EIA_SOURCE_VERSION,
        allowlist_version=ALLOWLIST_VERSION_V2,
        max_fanout=1,
        endpoints=(endpoint,),
    )


def sec_edgar_definition() -> SourceDefinition:
    endpoints = tuple(
        SourceEndpointDefinition(
            endpoint_id=company.endpoint_id,
            url=company.submissions_url,
            allowed_host="data.sec.gov",
            transport_kind=TransportKind.JSON_HTTP,
            parser_kind=ParserKind.JSON,
            registry_version="sec-edgar-public-v1",
            expected_freshness_seconds=600,
            request_ceiling=1,
            license_note="Public SEC EDGAR submissions metadata; fair-access User-Agent required.",
            excerpt_allowed=False,
            cik_scope=(company.cik,),
            ticker_scope=(company.ticker,),
        )
        for company in SEC_TICKER_REGISTRY.values()
    )
    return SourceDefinition(
        source_id=SEC_EDGAR,
        source_version=SEC_SOURCE_VERSION,
        allowlist_version=ALLOWLIST_VERSION_V2,
        max_fanout=10,
        endpoints=endpoints,
    )


def company_ir_definition() -> SourceDefinition:
    endpoints = tuple(
        SourceEndpointDefinition(
            endpoint_id=feed.endpoint_id,
            url=feed.feed_url,
            allowed_host=feed.allowed_host,
            transport_kind=TransportKind.RSS_ATOM,
            parser_kind=ParserKind.RSS,
            registry_version="company-ir-registry-2026.09-v1",
            expected_freshness_seconds=600,
            request_ceiling=1,
            license_note="Official issuer IR feed metadata; no generic crawling or arbitrary URLs.",
            excerpt_allowed=False,
            issuer_scope=(feed.issuer,),
            ticker_scope=(feed.ticker,),
        )
        for feed in COMPANY_IR_REGISTRY_FEEDS.values()
    )
    return SourceDefinition(
        source_id=COMPANY_IR_REGISTRY,
        source_version=COMPANY_IR_SOURCE_VERSION,
        allowlist_version=ALLOWLIST_VERSION_V2,
        max_fanout=10,
        endpoints=endpoints,
    )


def issuer_etf_holdings_definition() -> SourceDefinition:
    endpoints = tuple(
        SourceEndpointDefinition(
            endpoint_id=endpoint.endpoint_id,
            url=endpoint.holdings_url,
            allowed_host=endpoint.allowed_host,
            transport_kind=TransportKind.BULK_FILE,
            parser_kind=ParserKind.CSV,
            registry_version="issuer-etf-registry-2026.09-v1",
            expected_freshness_seconds=86_400,
            request_ceiling=1,
            license_note=(
                "Reviewed issuer holdings CSV; store numeric holdings with source and as-of date."
            ),
            excerpt_allowed=False,
            issuer_scope=(endpoint.issuer,),
            ticker_scope=(endpoint.ticker,),
        )
        for endpoint in ETF_HOLDINGS_REGISTRY.values()
    )
    return SourceDefinition(
        source_id=ISSUER_ETF_HOLDINGS,
        source_version=ETF_SOURCE_VERSION,
        allowlist_version=ALLOWLIST_VERSION_V2,
        max_fanout=10,
        endpoints=endpoints,
    )


def technology_official_definition() -> SourceDefinition:
    return SourceDefinition(
        source_id=TECHNOLOGY_OFFICIAL_FEEDS,
        source_version=TECH_SOURCE_VERSION,
        allowlist_version=ALLOWLIST_VERSION_V2,
        max_fanout=10,
        endpoints=(
            SourceEndpointDefinition(
                endpoint_id="cisa-kev-json",
                url="https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json",
                allowed_host="www.cisa.gov",
                transport_kind=TransportKind.JSON_HTTP,
                parser_kind=ParserKind.JSON,
                registry_version="technology-official-registry-2026.09-v1",
                expected_freshness_seconds=600,
                request_ceiling=1,
                license_note=(
                    "Official CISA KEV catalog JSON; government facts and short descriptions."
                ),
                excerpt_allowed=True,
                excerpt_max_chars=500,
            ),
        ),
    )


def public_provider_definitions(mode: EiaMode = EiaMode.BULK) -> tuple[SourceDefinition, ...]:
    return (
        defense_gov_definition(),
        eia_public_definition(mode),
        sec_edgar_definition(),
        company_ir_definition(),
        issuer_etf_holdings_definition(),
        technology_official_definition(),
    )
