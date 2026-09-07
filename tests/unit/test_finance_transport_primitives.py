from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.agents.finance import contracts as _finance_contracts  # noqa: F401
from app.connectors.finance_sources.cache import (
    EndpointWatermark,
    InMemoryEndpointStateStore,
    PersistentEndpointStateStore,
    dedupe_new_items,
    high_watermark_for,
)
from app.connectors.finance_sources.definitions import (
    ParserKind,
    RawSourcePayload,
    SourceEndpointDefinition,
    TransportKind,
)
from app.connectors.finance_sources.feed import FeedParseError, parse_rss_atom_payload
from app.connectors.finance_sources.transport import (
    ConditionalHttpTransport,
    InMemoryBulkArtifactStore,
    source_fetch_metadata,
)

NOW = datetime(2026, 9, 7, 14, 30, tzinfo=UTC)


def _endpoint(
    *,
    endpoint_id: str = "defense-rss",
    url: str = "https://www.defense.gov/rss",
    parser_kind: ParserKind = ParserKind.RSS,
    transport_kind: TransportKind = TransportKind.RSS_ATOM,
) -> SourceEndpointDefinition:
    host = httpx.URL(url).host
    assert host is not None
    return SourceEndpointDefinition(
        endpoint_id=endpoint_id,
        url=url,
        allowed_host=host,
        transport_kind=transport_kind,
        parser_kind=parser_kind,
        registry_version="finance-sources-2026.09-v2",
        expected_freshness_seconds=600,
        request_ceiling=1,
        license_note="short excerpts permitted for tests",
        excerpt_allowed=True,
        excerpt_max_chars=40,
    )


def _payload(body: bytes, endpoint: SourceEndpointDefinition) -> RawSourcePayload:
    return RawSourcePayload(
        endpoint_id=endpoint.endpoint_id,
        source_url=endpoint.url,
        retrieved_at=NOW,
        body=body,
        content_type="application/rss+xml",
    )


@pytest.mark.asyncio
async def test_conditional_transport_sends_etag_and_handles_304() -> None:
    endpoint = _endpoint()
    state = InMemoryEndpointStateStore()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                content=b"<rss><channel /></rss>",
                headers={"etag": '"v1"', "last-modified": "Mon, 07 Sep 2026 14:00:00 GMT"},
            )
        return httpx.Response(304)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = ConditionalHttpTransport(client=client, state_store=state, clock=lambda: NOW)
        first = await transport.fetch_endpoint(endpoint)
        second = await transport.fetch_endpoint(endpoint)

    assert first.payload is not None
    assert first.payload.etag == '"v1"'
    assert second.payload is not None
    assert second.payload.not_modified is True
    assert requests[1].headers["if-none-match"] == '"v1"'
    assert requests[1].headers["if-modified-since"] == "Mon, 07 Sep 2026 14:00:00 GMT"
    metadata = source_fetch_metadata(
        transport="rss_atom",
        endpoint_count=1,
        audits=(second.audit,),
    )
    assert metadata.request_count == 1
    assert metadata.not_modified_count == 1


@pytest.mark.asyncio
async def test_conditional_transport_rejects_non_allowlisted_host_before_request() -> None:
    endpoint = _endpoint(url="https://www.defense.gov/rss")
    object.__setattr__(endpoint, "allowed_host", "malicious.example")
    requests: list[httpx.Request] = []

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(200)
        )
    ) as client:
        transport = ConditionalHttpTransport(
            client=client,
            state_store=InMemoryEndpointStateStore(),
        )
        outcome = await transport.fetch_endpoint(endpoint)

    assert requests == []
    assert outcome.payload is None
    assert outcome.audit.error_code == "request_invalid"
    assert outcome.audit.request_counted is False


@pytest.mark.asyncio
async def test_conditional_transport_enforces_payload_byte_ceiling() -> None:
    endpoint = _endpoint()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"abcdef")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = ConditionalHttpTransport(
            client=client,
            state_store=InMemoryEndpointStateStore(),
            max_payload_bytes=5,
            clock=lambda: NOW,
        )
        outcome = await transport.fetch_endpoint(endpoint)

    assert len(requests) == 1
    assert outcome.payload is None
    assert outcome.audit.error_code == "payload_too_large"


@pytest.mark.parametrize(
    ("response_status", "expected_error"),
    [
        (302, "connector_redirect_disallowed"),
        (429, "connector_rate_limited"),
        (503, "connector_http_status"),
    ],
)
@pytest.mark.asyncio
async def test_conditional_transport_reports_http_failures_without_body_or_fallback(
    response_status: int, expected_error: str
) -> None:
    endpoint = _endpoint()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(response_status, content=b"licensed full text")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = ConditionalHttpTransport(
            client=client,
            state_store=InMemoryEndpointStateStore(),
        )
        outcome = await transport.fetch_endpoint(endpoint)

    assert len(requests) == 1
    assert outcome.payload is None
    assert outcome.audit.error_code == expected_error
    assert str(response_status) in str(outcome.audit.diagnostic)
    assert "licensed full text" not in str(outcome.audit.diagnostic)
    assert "search" not in str(outcome.audit.diagnostic)


@pytest.mark.asyncio
async def test_conditional_transport_reports_timeout_without_retry_or_secret_leak() -> None:
    endpoint = _endpoint()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadTimeout("secret-token", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = ConditionalHttpTransport(
            client=client,
            state_store=InMemoryEndpointStateStore(),
        )
        outcome = await transport.fetch_endpoint(
            endpoint,
            headers={"Authorization": "Bearer secret"},
        )

    assert len(requests) == 1
    assert outcome.audit.error_code == "connector_timeout"
    assert "secret" not in str(outcome.audit.diagnostic)


def test_rss_parser_maps_entries_and_clamps_permitted_excerpt() -> None:
    endpoint = _endpoint()
    payload = _payload(
        b"""
        <rss><channel><item>
          <guid>dod-1</guid>
          <title>Defense.gov posts ACME award</title>
          <link>https://www.defense.gov/News/Releases/release-1</link>
          <pubDate>Mon, 07 Sep 2026 14:15:00 GMT</pubDate>
          <description>Official summary with extra words that should be limited.</description>
          <category>defense</category>
        </item></channel></rss>
        """,
        endpoint,
    )

    entries = parse_rss_atom_payload(payload, endpoint)

    assert len(entries) == 1
    assert entries[0].external_id == "dod-1"
    assert entries[0].published_at == datetime(2026, 9, 7, 14, 15, tzinfo=UTC)
    assert entries[0].excerpt == "Official summary with extra words that s"
    assert entries[0].categories == ("defense",)


def test_atom_parser_maps_entries_with_stable_id() -> None:
    endpoint = _endpoint(
        endpoint_id="vendor-advisories",
        url="https://vendor.example/security.atom",
        parser_kind=ParserKind.ATOM,
    )
    payload = _payload(
        b"""
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <id>tag:vendor.example,2026:advisory-1</id>
            <title>Vendor posts advisory</title>
            <link rel="alternate" href="https://vendor.example/security/advisory-1" />
            <published>2026-09-07T14:10:00Z</published>
            <summary>Vendor advisory summary.</summary>
            <category term="security" />
          </entry>
        </feed>
        """,
        endpoint,
    )

    entries = parse_rss_atom_payload(payload, endpoint)

    assert len(entries) == 1
    assert entries[0].external_id == "tag:vendor.example,2026:advisory-1"
    assert entries[0].url == "https://vendor.example/security/advisory-1"
    assert entries[0].categories == ("security",)


def test_feed_parser_reports_malformed_xml_without_raw_body() -> None:
    endpoint = _endpoint()

    with pytest.raises(FeedParseError) as exc:
        parse_rss_atom_payload(_payload(b"<rss>", endpoint), endpoint)

    assert exc.value.error_code == "malformed_xml"
    assert "<rss>" not in exc.value.diagnostic


def test_watermark_recovery_filters_old_or_duplicate_items() -> None:
    state = InMemoryEndpointStateStore()
    endpoint_id = "company-ir"
    state.remember_watermark(
        endpoint_id,
        high_watermark=NOW - timedelta(minutes=6),
        seen_external_ids=("old-seen",),
    )
    recovery_start = state.recovery_start(
        endpoint_id,
        cold_start_lookback=timedelta(hours=2),
        missed_poll_lookback=timedelta(minutes=10),
        now=NOW,
    )
    items = (
        {"id": "old-seen", "published": NOW - timedelta(minutes=5)},
        {"id": "too-old", "published": NOW - timedelta(hours=1)},
        {"id": "fresh", "published": NOW - timedelta(minutes=3)},
    )

    fresh = dedupe_new_items(
        items,
        watermark=state.get(endpoint_id),
        external_id=lambda item: str(item["id"]),
        published_at=lambda item: item["published"],
        lookback_start=recovery_start,
    )

    assert recovery_start == NOW - timedelta(minutes=16)
    assert fresh == ({"id": "fresh", "published": NOW - timedelta(minutes=3)},)
    assert high_watermark_for(fresh, lambda item: item["published"]) == NOW - timedelta(minutes=3)


def test_cold_start_uses_bounded_backfill_window() -> None:
    state = InMemoryEndpointStateStore()

    assert state.recovery_start(
        "new-feed",
        cold_start_lookback=timedelta(hours=6),
        missed_poll_lookback=timedelta(minutes=10),
        now=NOW,
    ) == NOW - timedelta(hours=6)


def test_persistent_state_store_recovers_validators_and_watermarks() -> None:
    durable: dict[str, EndpointWatermark] = {
        "company-ir": EndpointWatermark(
            endpoint_id="company-ir",
            etag='"feed-v1"',
            high_watermark=NOW - timedelta(minutes=10),
            seen_external_ids=frozenset({"old-item"}),
        )
    }
    store = PersistentEndpointStateStore(
        loader=lambda endpoint_id: durable.get(endpoint_id),
        saver=lambda record: durable.__setitem__(record.endpoint_id, record),
    )

    assert store.conditional_headers("company-ir") == {"If-None-Match": '"feed-v1"'}
    store.remember_watermark(
        "company-ir",
        high_watermark=NOW,
        seen_external_ids=("new-item",),
    )
    recovered = PersistentEndpointStateStore(
        loader=lambda endpoint_id: durable.get(endpoint_id),
        saver=lambda record: durable.__setitem__(record.endpoint_id, record),
    ).get("company-ir")

    assert recovered.high_watermark == NOW
    assert recovered.seen_external_ids == frozenset({"old-item", "new-item"})


def test_bulk_artifact_store_keeps_body_private_and_returns_reference() -> None:
    endpoint = replace(
        _endpoint(url="https://www.eia.gov/bulk/file.zip"),
        parser_kind=ParserKind.CSV,
        transport_kind=TransportKind.BULK_FILE,
    )
    store = InMemoryBulkArtifactStore()
    payload = RawSourcePayload(
        endpoint_id=endpoint.endpoint_id,
        source_url=endpoint.url,
        retrieved_at=NOW,
        body=b"series,value\nELEC,42\n",
        content_type="text/csv",
        etag='"bulk-v1"',
    )

    reference = store.put(payload)

    assert reference.endpoint_id == endpoint.endpoint_id
    assert reference.size_bytes == len(payload.body)
    assert reference == store.latest(endpoint.endpoint_id)
    assert store.get_body(reference) == payload.body
    assert "ELEC,42" not in repr(reference)
