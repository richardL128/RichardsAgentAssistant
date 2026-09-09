from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from app.agents.finance.contracts import SourceClassification, SourceQuery
from app.connectors.finance_sources.providers import (
    ALLOWLIST_VERSION_V2,
    EiaMode,
    company_ir_definition,
    company_ir_missing_mapping_diagnostics,
    defense_gov_definition,
    eia_public_definition,
    etf_missing_mapping_diagnostics,
    issuer_etf_holdings_definition,
    parse_cisa_kev,
    parse_company_ir_feed,
    parse_defense_gov_rss,
    parse_eia_api_response,
    parse_eia_bulk_records,
    parse_eia_bulk_zip,
    parse_ishares_holdings_csv,
    parse_sec_submissions,
    public_provider_definitions,
    sec_edgar_definition,
    sec_missing_mapping_diagnostics,
    technology_official_definition,
)
from app.connectors.finance_sources.providers.common import ProviderParseError

FIXTURES = Path(__file__).parent.parent / "fixtures" / "finance" / "providers"
NOW = datetime(2026, 9, 4, 13, 30, tzinfo=UTC)
WINDOW_START = NOW - timedelta(days=1)
WINDOW_END = NOW + timedelta(hours=1)


def _query(
    source_id: str,
    source_version: str,
    *,
    tickers: tuple[str, ...] = ("LMT",),
) -> SourceQuery:
    return SourceQuery(
        source_id=source_id,
        source_version=source_version,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        tickers=tickers,
        themes=("defense",),
    )


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_public_provider_definitions_are_reviewed_and_versioned() -> None:
    definitions = public_provider_definitions()

    assert {definition.source_id for definition in definitions} == {
        "defense_gov_rss",
        "eia_public_data",
        "sec_edgar",
        "company_ir_registry",
        "issuer_etf_holdings",
        "technology_official_feeds",
    }
    assert {definition.allowlist_version for definition in definitions} == {ALLOWLIST_VERSION_V2}
    assert all(definition.endpoints for definition in definitions)
    assert all(
        endpoint.url.startswith(f"https://{endpoint.allowed_host}/")
        for definition in definitions
        for endpoint in definition.endpoints
    )


def test_eia_definition_selects_bulk_or_api_without_combining_modes() -> None:
    bulk = eia_public_definition(EiaMode.BULK)
    api = eia_public_definition(EiaMode.API)

    assert [endpoint.endpoint_id for endpoint in bulk.endpoints] == ["eia-petroleum-bulk-zip"]
    assert [endpoint.endpoint_id for endpoint in api.endpoints] == ["eia-v2-api"]
    assert bulk.endpoints[0].url == "https://www.eia.gov/opendata/bulk/PET.zip"
    assert api.endpoints[0].url == "https://api.eia.gov/v2/petroleum/pri/spt/data/"


def test_defense_rss_parser_preserves_canonical_url_time_and_short_excerpt() -> None:
    query = _query("defense_gov_rss", "defense-gov-rss-v1")

    documents = parse_defense_gov_rss(_fixture("defense_gov_rss.xml"), query, NOW)

    assert len(documents) == 1
    document = documents[0]
    assert document.source_id == "defense_gov_rss"
    assert document.classification is SourceClassification.PRIMARY
    assert document.external_id == "defense-release-4100000"
    assert str(document.url).startswith("https://www.war.gov/News/Releases/")
    assert document.published_at == datetime(2026, 9, 4, 13, 5, tzinfo=UTC)
    assert document.retrieved_at == NOW
    assert (
        document.excerpt == "Official summary of a Defense Department aircraft sustainment award."
    )


def test_feed_parser_rejects_malformed_xml_and_unreviewed_hosts() -> None:
    query = _query("defense_gov_rss", "defense-gov-rss-v1")
    with pytest.raises(ProviderParseError, match="malformed"):
        parse_defense_gov_rss(b"<rss><channel>", query, NOW)

    payload = _fixture("defense_gov_rss.xml").replace(
        b"https://www.war.gov/", b"https://example.com/"
    )
    with pytest.raises(ProviderParseError, match="host"):
        parse_defense_gov_rss(payload, query, NOW)


def test_eia_bulk_parser_preserves_units_frequency_period_and_release_time() -> None:
    query = _query("eia_public_data", "eia-public-v1")

    documents = parse_eia_bulk_records(_fixture("eia_bulk.ndjson"), query, NOW)

    assert len(documents) == 2
    first = documents[0]
    assert first.source_id == "eia_public_data"
    assert first.classification is SourceClassification.PRIMARY
    assert first.external_id == "ELEC.GEN.ALL-US-99.M:2026-07"
    assert first.published_at == datetime(2026, 9, 4, 12, 30, tzinfo=UTC)
    assert first.numbers[0].unit == "thousand megawatthours"
    assert first.numbers[0].as_of.isoformat() == "2026-07-01"
    assert "monthly" in first.numbers[0].label


def test_eia_bulk_zip_reads_only_reviewed_series_without_extracting_files() -> None:
    query = _query("eia_public_data", "eia-public-v1")
    archive_bytes = BytesIO()
    with ZipFile(archive_bytes, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "PET.txt",
            b'{"series_id":"PET.RWTC.D","name":"WTI","units":"dollars per barrel",'
            b'"frequency":"daily","last_updated":"2026-09-04T12:30:00Z",'
            b'"data":[["2026-09-04",65.5]]}\n'
            b'{"series_id":"PET.UNREVIEWED.D","name":"Other","units":"value",'
            b'"data":[["2026-09-04",1]]}\n',
        )

    documents = parse_eia_bulk_zip(archive_bytes.getvalue(), query, NOW)

    assert [document.external_id for document in documents] == ["PET.RWTC.D:2026-09-04"]
    assert documents[0].numbers[0].unit == "dollars per barrel"


def test_eia_api_parser_handles_v2_rows() -> None:
    query = _query("eia_public_data", "eia-public-v1")
    payload = {
        "response": {
            "frequency": "weekly",
            "data": [
                {
                    "series_id": "PET.WCRSTUS1.W",
                    "period": "2026-08-28",
                    "value": "420.5",
                    "units": "million barrels",
                    "release_date": "2026-09-04T12:00:00Z",
                }
            ],
        }
    }

    documents = parse_eia_api_response(payload, query, NOW)

    assert len(documents) == 1
    assert documents[0].numbers[0].value == 420.5
    assert documents[0].numbers[0].unit == "million barrels"
    assert documents[0].numbers[0].as_of.isoformat() == "2026-08-28"


def test_sec_parser_filters_to_relevant_forms_and_builds_canonical_filing_url() -> None:
    query = _query("sec_edgar", "sec-edgar-public-v1")

    documents = parse_sec_submissions(
        _fixture("sec_submissions_lmt.json"),
        query,
        NOW,
        ticker="LMT",
    )

    assert len(documents) == 1
    document = documents[0]
    assert document.external_id == "0000936468:0000936468-26-000120"
    assert "8-K" in document.title
    assert str(document.url) == (
        "https://www.sec.gov/Archives/edgar/data/936468/000093646826000120/lmt-20260904.htm"
    )
    assert document.tickers == ("LMT",)
    assert document.excerpt is None


def test_company_ir_feed_uses_registry_and_exposes_missing_mapping_diagnostic() -> None:
    query = _query("company_ir_registry", "company-ir-registry-2026.09-v1")

    documents = parse_company_ir_feed(_fixture("company_ir_lmt.xml"), query, NOW, ticker="LMT")
    diagnostics = company_ir_missing_mapping_diagnostics(("LMT", "MSFT"))

    assert len(documents) == 1
    assert documents[0].source_id == "company_ir_registry"
    assert documents[0].classification is SourceClassification.PRIMARY
    assert documents[0].tickers == ("LMT",)
    assert documents[0].excerpt is None
    assert documents[0].issuer == "Lockheed Martin Corporation"
    assert str(documents[0].feed_endpoint) == (
        "https://news.lockheedmartin.com/news-releases?category=788&pagetemplate=rss"
    )
    assert [diagnostic.ticker for diagnostic in diagnostics] == ["MSFT"]


def test_sec_and_etf_registry_diagnostics_are_visible_for_missing_mappings() -> None:
    sec_diagnostics = sec_missing_mapping_diagnostics(("LMT", "MSFT"))
    etf_diagnostics = etf_missing_mapping_diagnostics(("IVV", "QQQ"))

    assert [diagnostic.ticker for diagnostic in sec_diagnostics] == ["MSFT"]
    assert [diagnostic.ticker for diagnostic in etf_diagnostics] == ["QQQ"]


def test_ishares_holdings_parser_preserves_as_of_source_and_multi_holding_rows() -> None:
    result = parse_ishares_holdings_csv(
        _fixture("ishares_ivv.csv"),
        etf_symbol="IVV",
        retrieved_at=NOW,
    )

    assert result.etf_symbol == "IVV"
    assert result.as_of.isoformat() == "2026-09-04"
    assert result.source_url.endswith("/latest-holdings.csv")
    assert result.stale is False
    assert [(row.underlying_symbol, row.weight_percent) for row in result.exposures] == [
        ("MSFT", 6.75),
        ("LMT", 0.45),
    ]
    assert all(str(row.source_url).endswith("/latest-holdings.csv") for row in result.exposures)
    assert all(row.retrieved_at == NOW for row in result.exposures)


def test_ishares_holdings_parser_marks_older_as_of_date_as_stale() -> None:
    retrieved_at = datetime(2026, 9, 5, 13, 30, tzinfo=UTC)

    result = parse_ishares_holdings_csv(
        _fixture("ishares_ivv.csv"),
        etf_symbol="IVV",
        retrieved_at=retrieved_at,
    )

    assert result.stale is True
    assert result.as_of.isoformat() == "2026-09-04"


def test_cisa_kev_parser_preserves_cve_timestamp_and_short_description() -> None:
    query = _query("technology_official_feeds", "technology-official-registry-2026.09-v1")

    documents = parse_cisa_kev(_fixture("cisa_kev.json"), query, NOW)

    assert len(documents) == 1
    document = documents[0]
    assert document.external_id == "CVE-2026-12345"
    assert document.published_at == datetime(2026, 9, 4, tzinfo=UTC)
    assert document.classification is SourceClassification.PRIMARY
    assert "improper access control" in (document.excerpt or "")
    assert "technology-security" in document.themes


def test_definition_helpers_cover_each_provider_registry() -> None:
    definitions = (
        defense_gov_definition(),
        sec_edgar_definition(),
        company_ir_definition(),
        issuer_etf_holdings_definition(),
        technology_official_definition(),
    )

    assert [definition.source_id for definition in definitions] == [
        "defense_gov_rss",
        "sec_edgar",
        "company_ir_registry",
        "issuer_etf_holdings",
        "technology_official_feeds",
    ]
