"""Cache and watermark primitives for public finance source transports."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.connectors.finance_sources.definitions import RawSourcePayload


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("finance source cache timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _empty_records() -> dict[str, EndpointWatermark]:
    return {}


def _empty_endpoint_ids() -> set[str]:
    return set()


@dataclass(frozen=True, slots=True)
class EndpointWatermark:
    """Persistable state used for conditional requests and missed-poll recovery."""

    endpoint_id: str
    etag: str | None = None
    last_modified: str | None = None
    high_watermark: datetime | None = None
    seen_external_ids: frozenset[str] = frozenset()
    artifact_ref: str | None = None
    retrieved_at: datetime | None = None
    not_modified_at: datetime | None = None

    def conditional_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.etag:
            headers["If-None-Match"] = self.etag
        if self.last_modified:
            headers["If-Modified-Since"] = self.last_modified
        return headers


@dataclass(slots=True)
class InMemoryEndpointStateStore:
    """Small persistable-shape store suitable for tests and local workers.

    Production persistence can serialize the `EndpointWatermark` records without
    changing the transport interfaces.
    """

    _records: dict[str, EndpointWatermark] = field(default_factory=_empty_records)

    def get(self, endpoint_id: str) -> EndpointWatermark:
        return self._records.get(endpoint_id, EndpointWatermark(endpoint_id=endpoint_id))

    def seed(self, record: EndpointWatermark) -> None:
        self._records[record.endpoint_id] = record

    def conditional_headers(self, endpoint_id: str) -> dict[str, str]:
        return self.get(endpoint_id).conditional_headers()

    def remember_payload(
        self,
        payload: RawSourcePayload,
        *,
        high_watermark: datetime | None = None,
        seen_external_ids: Iterable[str] = (),
        artifact_ref: str | None = None,
    ) -> EndpointWatermark:
        previous = self.get(payload.endpoint_id)
        watermark = _max_datetime(previous.high_watermark, high_watermark)
        seen = frozenset((*previous.seen_external_ids, *seen_external_ids))
        record = EndpointWatermark(
            endpoint_id=payload.endpoint_id,
            etag=payload.etag or previous.etag,
            last_modified=payload.last_modified or previous.last_modified,
            high_watermark=watermark,
            seen_external_ids=seen,
            artifact_ref=artifact_ref or previous.artifact_ref,
            retrieved_at=_aware(payload.retrieved_at),
            not_modified_at=(
                _aware(payload.retrieved_at) if payload.not_modified else previous.not_modified_at
            ),
        )
        self._records[payload.endpoint_id] = record
        return record

    def remember_watermark(
        self,
        endpoint_id: str,
        *,
        high_watermark: datetime | None = None,
        seen_external_ids: Iterable[str] = (),
    ) -> EndpointWatermark:
        previous = self.get(endpoint_id)
        record = EndpointWatermark(
            endpoint_id=endpoint_id,
            etag=previous.etag,
            last_modified=previous.last_modified,
            high_watermark=_max_datetime(previous.high_watermark, high_watermark),
            seen_external_ids=frozenset((*previous.seen_external_ids, *seen_external_ids)),
            artifact_ref=previous.artifact_ref,
            retrieved_at=previous.retrieved_at,
            not_modified_at=previous.not_modified_at,
        )
        self._records[endpoint_id] = record
        return record

    def recovery_start(
        self,
        endpoint_id: str,
        *,
        cold_start_lookback: timedelta,
        missed_poll_lookback: timedelta,
        now: datetime,
    ) -> datetime:
        record = self.get(endpoint_id)
        now_utc = _aware(now)
        if record.high_watermark is None:
            return now_utc - cold_start_lookback
        return max(
            _aware(record.high_watermark) - missed_poll_lookback,
            now_utc - cold_start_lookback,
        )


class EndpointStateStore(Protocol):
    def get(self, endpoint_id: str) -> EndpointWatermark: ...

    def conditional_headers(self, endpoint_id: str) -> dict[str, str]: ...

    def remember_payload(
        self,
        payload: RawSourcePayload,
        *,
        high_watermark: datetime | None = None,
        seen_external_ids: Iterable[str] = (),
        artifact_ref: str | None = None,
    ) -> EndpointWatermark: ...

    def remember_watermark(
        self,
        endpoint_id: str,
        *,
        high_watermark: datetime | None = None,
        seen_external_ids: Iterable[str] = (),
    ) -> EndpointWatermark: ...


@dataclass(slots=True)
class PersistentEndpointStateStore:
    """Write through conditional validators and watermarks to durable storage."""

    loader: Callable[[str], EndpointWatermark | None]
    saver: Callable[[EndpointWatermark], None]
    _memory: InMemoryEndpointStateStore = field(default_factory=InMemoryEndpointStateStore)
    _loaded: set[str] = field(default_factory=_empty_endpoint_ids)

    def get(self, endpoint_id: str) -> EndpointWatermark:
        if endpoint_id not in self._loaded:
            record = self.loader(endpoint_id)
            if record is not None:
                self._memory.seed(record)
            self._loaded.add(endpoint_id)
        return self._memory.get(endpoint_id)

    def conditional_headers(self, endpoint_id: str) -> dict[str, str]:
        return self.get(endpoint_id).conditional_headers()

    def remember_payload(
        self,
        payload: RawSourcePayload,
        *,
        high_watermark: datetime | None = None,
        seen_external_ids: Iterable[str] = (),
        artifact_ref: str | None = None,
    ) -> EndpointWatermark:
        self.get(payload.endpoint_id)
        record = self._memory.remember_payload(
            payload,
            high_watermark=high_watermark,
            seen_external_ids=seen_external_ids,
            artifact_ref=artifact_ref,
        )
        self.saver(record)
        return record

    def remember_watermark(
        self,
        endpoint_id: str,
        *,
        high_watermark: datetime | None = None,
        seen_external_ids: Iterable[str] = (),
    ) -> EndpointWatermark:
        self.get(endpoint_id)
        record = self._memory.remember_watermark(
            endpoint_id,
            high_watermark=high_watermark,
            seen_external_ids=seen_external_ids,
        )
        self.saver(record)
        return record


def dedupe_new_items[ItemT](
    items: Sequence[ItemT],
    *,
    watermark: EndpointWatermark,
    external_id: Callable[[ItemT], str],
    published_at: Callable[[ItemT], datetime | None],
    lookback_start: datetime,
) -> tuple[ItemT, ...]:
    """Return unseen items at or after the bounded recovery start."""

    bounded_start = _aware(lookback_start)
    fresh: list[ItemT] = []
    for item in items:
        item_id = external_id(item)
        if not item_id or item_id in watermark.seen_external_ids:
            continue
        published = published_at(item)
        if published is not None and _aware(published) < bounded_start:
            continue
        fresh.append(item)
    return tuple(fresh)


def high_watermark_for[ItemT](
    items: Iterable[ItemT],
    published_at: Callable[[ItemT], datetime | None],
) -> datetime | None:
    watermark: datetime | None = None
    for item in items:
        watermark = _max_datetime(watermark, published_at(item))
    return watermark


def _max_datetime(left: datetime | None, right: datetime | None) -> datetime | None:
    if left is None:
        return _aware(right) if right is not None else None
    if right is None:
        return _aware(left)
    return max(_aware(left), _aware(right))


__all__ = [
    "EndpointStateStore",
    "EndpointWatermark",
    "InMemoryEndpointStateStore",
    "PersistentEndpointStateStore",
    "dedupe_new_items",
    "high_watermark_for",
]
