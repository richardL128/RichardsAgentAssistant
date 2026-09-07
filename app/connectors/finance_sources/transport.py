"""Generic public finance source transport primitives."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from app.agents.finance.contracts import (
    SourceEndpointFailure,
    SourceFetchMetadata,
)
from app.connectors.finance_sources.cache import EndpointStateStore
from app.connectors.finance_sources.definitions import (
    RawSourcePayload,
    SourceEndpointDefinition,
    TransportKind,
)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class EndpointRequestAudit:
    endpoint_id: str
    url: str
    host: str
    requested_at: datetime
    status_code: int | None = None
    not_modified: bool = False
    error_code: str | None = None
    diagnostic: str | None = None
    request_counted: bool = True

    @property
    def failure(self) -> SourceEndpointFailure | None:
        if self.error_code is None or self.diagnostic is None:
            return None
        return SourceEndpointFailure(
            endpoint_id=self.endpoint_id,
            error_code=self.error_code,
            diagnostic=self.diagnostic,
        )


@dataclass(frozen=True, slots=True)
class EndpointFetchOutcome:
    payload: RawSourcePayload | None
    audit: EndpointRequestAudit


@dataclass(slots=True)
class ConditionalHttpTransport:
    """Fetch reviewed endpoint URLs with conditional headers and byte ceilings."""

    client: httpx.AsyncClient
    state_store: EndpointStateStore
    max_payload_bytes: int = 2_000_000
    timeout_seconds: float = 10.0
    clock: Callable[[], datetime] = field(default=_now, repr=False)
    bulk_artifact_sink: Callable[[RawSourcePayload], str] | None = field(
        default=None, repr=False
    )

    async def fetch_endpoint(
        self,
        endpoint: SourceEndpointDefinition,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> EndpointFetchOutcome:
        requested_at = _aware(self.clock())
        try:
            url, host = _reviewed_url(endpoint)
        except ValueError:
            return EndpointFetchOutcome(
                payload=None,
                audit=EndpointRequestAudit(
                    endpoint_id=endpoint.endpoint_id,
                    url=endpoint.url,
                    host=endpoint.allowed_host,
                    requested_at=requested_at,
                    error_code="request_invalid",
                    diagnostic="finance endpoint URL failed allowlist validation",
                    request_counted=False,
                ),
            )

        request_headers = self.state_store.conditional_headers(endpoint.endpoint_id)
        request_headers.update(headers or {})
        try:
            async with self.client.stream(
                "GET",
                url,
                headers=request_headers,
                params=params,
                timeout=self.timeout_seconds,
            ) as response:
                if response.status_code == 304:
                    payload = RawSourcePayload(
                        endpoint_id=endpoint.endpoint_id,
                        source_url=url,
                        retrieved_at=requested_at,
                        body=b"",
                        content_type=response.headers.get("content-type"),
                        etag=response.headers.get("etag"),
                        last_modified=response.headers.get("last-modified"),
                        not_modified=True,
                    )
                    self.state_store.remember_payload(payload)
                    return EndpointFetchOutcome(
                        payload=payload,
                        audit=EndpointRequestAudit(
                            endpoint_id=endpoint.endpoint_id,
                            url=url,
                            host=host,
                            requested_at=requested_at,
                            status_code=304,
                            not_modified=True,
                        ),
                    )
                if response.status_code == 429:
                    return _http_failure(
                        endpoint,
                        url,
                        host,
                        requested_at,
                        response,
                        "connector_rate_limited",
                    )
                if 300 <= response.status_code < 400:
                    return _http_failure(
                        endpoint,
                        url,
                        host,
                        requested_at,
                        response,
                        "connector_redirect_disallowed",
                    )
                if response.status_code >= 400:
                    return _http_failure(
                        endpoint,
                        url,
                        host,
                        requested_at,
                        response,
                        "connector_http_status",
                    )
                body = await _bounded_body(response, self.max_payload_bytes)
        except httpx.TimeoutException:
            return _transport_failure(
                endpoint,
                url,
                host,
                requested_at,
                "connector_timeout",
                "finance source request timed out",
            )
        except PayloadTooLargeError:
            return _transport_failure(
                endpoint,
                url,
                host,
                requested_at,
                "payload_too_large",
                "finance source payload exceeded the byte ceiling",
            )
        except httpx.HTTPError:
            return _transport_failure(
                endpoint,
                url,
                host,
                requested_at,
                "connector_transient",
                "finance source request failed",
            )

        payload = RawSourcePayload(
            endpoint_id=endpoint.endpoint_id,
            source_url=url,
            retrieved_at=requested_at,
            body=body,
            content_type=response.headers.get("content-type"),
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
        )
        artifact_ref = None
        if (
            endpoint.transport_kind is TransportKind.BULK_FILE
            and self.bulk_artifact_sink is not None
        ):
            try:
                artifact_ref = self.bulk_artifact_sink(payload)
            except (OSError, ValueError):
                return _transport_failure(
                    endpoint,
                    url,
                    host,
                    requested_at,
                    "cache_persistence_failed",
                    "finance bulk artifact could not be persisted",
                )
        self.state_store.remember_payload(payload, artifact_ref=artifact_ref)
        return EndpointFetchOutcome(
            payload=payload,
            audit=EndpointRequestAudit(
                endpoint_id=endpoint.endpoint_id,
                url=url,
                host=host,
                requested_at=requested_at,
                status_code=response.status_code,
            ),
        )


class PayloadTooLargeError(ValueError):
    """Raised internally when transport content exceeds the configured ceiling."""


def _empty_bodies() -> dict[str, bytes]:
    return {}


def _empty_bulk_references() -> dict[str, BulkArtifactReference]:
    return {}


@dataclass(frozen=True, slots=True)
class BulkArtifactReference:
    endpoint_id: str
    source_url: str
    artifact_ref: str
    sha256: str
    size_bytes: int
    retrieved_at: datetime
    content_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None


@dataclass(slots=True)
class InMemoryBulkArtifactStore:
    """Private body store that exposes only stable artifact references."""

    _bodies: dict[str, bytes] = field(default_factory=_empty_bodies, repr=False)
    _latest_by_endpoint: dict[str, BulkArtifactReference] = field(
        default_factory=_empty_bulk_references
    )

    def put(self, payload: RawSourcePayload) -> BulkArtifactReference:
        digest = hashlib.sha256(payload.body).hexdigest()
        artifact_ref = f"{payload.endpoint_id}:{digest}"
        reference = BulkArtifactReference(
            endpoint_id=payload.endpoint_id,
            source_url=payload.source_url,
            artifact_ref=artifact_ref,
            sha256=digest,
            size_bytes=len(payload.body),
            retrieved_at=_aware(payload.retrieved_at),
            content_type=payload.content_type,
            etag=payload.etag,
            last_modified=payload.last_modified,
        )
        self._bodies[artifact_ref] = payload.body
        self._latest_by_endpoint[payload.endpoint_id] = reference
        return reference

    def latest(self, endpoint_id: str) -> BulkArtifactReference | None:
        return self._latest_by_endpoint.get(endpoint_id)

    def get_body(self, reference: BulkArtifactReference) -> bytes | None:
        return self._bodies.get(reference.artifact_ref)


def source_fetch_metadata(
    *,
    transport: str,
    endpoint_count: int,
    audits: tuple[EndpointRequestAudit, ...],
    stale: bool = False,
    ingestion_latency_seconds: float | None = None,
) -> SourceFetchMetadata:
    return SourceFetchMetadata(
        transport=transport,
        endpoint_count=endpoint_count,
        request_count=sum(1 for audit in audits if audit.request_counted),
        not_modified_count=sum(1 for audit in audits if audit.not_modified),
        ingestion_latency_seconds=ingestion_latency_seconds,
        stale=stale,
        endpoint_failures=tuple(
            failure for audit in audits if (failure := audit.failure) is not None
        ),
    )


async def _bounded_body(response: httpx.Response, max_payload_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_payload_bytes:
            raise PayloadTooLargeError
        chunks.append(chunk)
    return b"".join(chunks)


def _reviewed_url(endpoint: SourceEndpointDefinition) -> tuple[str, str]:
    parsed = urlsplit(endpoint.url)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.hostname != endpoint.allowed_host
    ):
        raise ValueError("finance endpoint URL is not allowlisted")
    return endpoint.url, parsed.hostname


def _http_failure(
    endpoint: SourceEndpointDefinition,
    url: str,
    host: str,
    requested_at: datetime,
    response: httpx.Response,
    error_code: str,
) -> EndpointFetchOutcome:
    return _transport_failure(
        endpoint,
        url,
        host,
        requested_at,
        error_code,
        f"finance source returned HTTP {response.status_code}",
        status_code=response.status_code,
    )


def _transport_failure(
    endpoint: SourceEndpointDefinition,
    url: str,
    host: str,
    requested_at: datetime,
    error_code: str,
    diagnostic: str,
    *,
    status_code: int | None = None,
) -> EndpointFetchOutcome:
    return EndpointFetchOutcome(
        payload=None,
        audit=EndpointRequestAudit(
            endpoint_id=endpoint.endpoint_id,
            url=url,
            host=host,
            requested_at=requested_at,
            status_code=status_code,
            error_code=error_code,
            diagnostic=diagnostic,
        ),
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("finance source transport timestamps must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "BulkArtifactReference",
    "ConditionalHttpTransport",
    "EndpointFetchOutcome",
    "EndpointRequestAudit",
    "InMemoryBulkArtifactStore",
    "PayloadTooLargeError",
    "source_fetch_metadata",
]
