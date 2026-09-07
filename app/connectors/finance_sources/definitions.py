"""Immutable definitions shared by public-first finance source adapters.

Definitions contain only operator-reviewed endpoints.  Runtime queries never
carry URLs, so adapters cannot be redirected by model, user, or source data.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit


class TransportKind(StrEnum):
    JSON_HTTP = "json_http"
    RSS_ATOM = "rss_atom"
    BULK_FILE = "bulk_file"


class ParserKind(StrEnum):
    JSON = "json"
    RSS = "rss"
    ATOM = "atom"
    CSV = "csv"
    XLS = "xls"
    XLSX = "xlsx"


@dataclass(frozen=True, slots=True)
class SourceEndpointDefinition:
    endpoint_id: str
    url: str
    allowed_host: str
    transport_kind: TransportKind
    parser_kind: ParserKind
    registry_version: str
    expected_freshness_seconds: int
    request_ceiling: int
    license_note: str
    excerpt_allowed: bool = False
    excerpt_max_chars: int | None = None
    issuer_scope: tuple[str, ...] = ()
    cik_scope: tuple[str, ...] = ()
    ticker_scope: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        if parsed.scheme != "https" or parsed.hostname != self.allowed_host:
            raise ValueError("finance endpoint URL must exactly match its reviewed HTTPS host")
        if not self.endpoint_id or not self.registry_version or not self.license_note:
            raise ValueError("finance endpoint identity and review metadata are required")
        if self.expected_freshness_seconds <= 0 or self.request_ceiling <= 0:
            raise ValueError("finance endpoint freshness and request ceiling must be positive")
        if self.excerpt_max_chars is not None and (
            not self.excerpt_allowed or not 1 <= self.excerpt_max_chars <= 500
        ):
            raise ValueError(
                "finance endpoint excerpt limit requires permission and is capped at 500"
            )


@dataclass(frozen=True, slots=True)
class SourceDefinition:
    source_id: str
    source_version: str
    allowlist_version: str
    endpoints: tuple[SourceEndpointDefinition, ...]
    max_fanout: int

    def __post_init__(self) -> None:
        endpoint_ids = tuple(endpoint.endpoint_id for endpoint in self.endpoints)
        if not self.source_id or not self.source_version or not self.allowlist_version:
            raise ValueError("finance source identity and versions are required")
        if not self.endpoints or len(self.endpoints) > self.max_fanout:
            raise ValueError("finance source endpoints must fit within configured fan-out")
        if len(endpoint_ids) != len(set(endpoint_ids)):
            raise ValueError("finance source endpoint IDs must be unique")


@dataclass(frozen=True, slots=True)
class RawSourcePayload:
    endpoint_id: str
    source_url: str
    retrieved_at: datetime
    body: bytes = field(repr=False)
    content_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False


class CredentialProvider(Protocol):
    """Return a configured credential without exposing it in adapter metadata."""

    def get(self, credential_id: str) -> str | None: ...


def _empty_credentials() -> Mapping[str, str]:
    return {}


@dataclass(frozen=True, slots=True)
class StaticCredentialProvider:
    values: Mapping[str, str] = field(default_factory=_empty_credentials, repr=False)

    def get(self, credential_id: str) -> str | None:
        return self.values.get(credential_id) or None


__all__ = [
    "CredentialProvider",
    "ParserKind",
    "RawSourcePayload",
    "SourceDefinition",
    "SourceEndpointDefinition",
    "StaticCredentialProvider",
    "TransportKind",
]
