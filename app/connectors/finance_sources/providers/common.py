"""Shared public finance provider parsing helpers."""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import cast
from urllib.parse import urlsplit

from app.agents.finance.contracts import SourceQuery

JsonObject = Mapping[str, object]


class ProviderParseError(ValueError):
    """Raised when an operator-reviewed provider payload is malformed."""


@dataclass(frozen=True, slots=True)
class ProviderDiagnostic:
    code: str
    diagnostic: str
    ticker: str | None = None
    endpoint_id: str | None = None


def parse_json_object(payload: bytes | str | JsonObject) -> JsonObject:
    if isinstance(payload, Mapping):
        return payload
    try:
        raw = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderParseError("provider JSON payload is malformed") from exc
    if not isinstance(value, Mapping):
        raise ProviderParseError("provider JSON payload must be an object")
    return cast(JsonObject, value)


def parse_json_lines(payload: bytes | str) -> tuple[JsonObject, ...]:
    raw = payload.decode("utf-8") if isinstance(payload, bytes) else payload
    records: list[JsonObject] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ProviderParseError("provider JSON lines payload is malformed") from exc
        if isinstance(value, Mapping):
            records.append(cast(JsonObject, value))
    return tuple(records)


def text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, int | float):
        return str(value)
    return ""


def clean_text(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


def stable_id(*parts: object, max_length: int = 255) -> str:
    key = ":".join(text(part) for part in parts if text(part))
    if not key:
        key = "provider-document"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:max_length]


def clamp_excerpt(value: object, max_chars: int) -> str | None:
    cleaned = text(value)
    if not cleaned:
        return None
    return cleaned[: min(max_chars, 500)].rstrip()


def parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return aware(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        parsed = _parse_partial_datetime(raw)
    if parsed is None:
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
    return aware(parsed)


def parse_period_date(value: object) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    raw = text(value)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        pass
    if re.fullmatch(r"\d{4}-\d{2}", raw):
        year, month = raw.split("-")
        return date(int(year), int(month), 1)
    if re.fullmatch(r"\d{4}", raw):
        return date(int(raw), 1, 1)
    parsed = parse_datetime(raw)
    return parsed.date() if parsed is not None else None


def in_query_window(value: datetime | None, query: SourceQuery, fallback: datetime) -> bool:
    timestamp = (value or fallback).astimezone(UTC)
    return query.window_start <= timestamp <= query.window_end


def unique_upper(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.strip().upper()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return tuple(result)


def unique_lower(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.strip().casefold()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return tuple(result)


def string_values(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(part.strip() for part in re.split(r"[,;]", value) if part.strip())
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        items = cast(Sequence[object], value)
        return tuple(text(item) for item in items if text(item))
    return ()


def ensure_reviewed_host(url: str, allowed_host: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != allowed_host:
        raise ProviderParseError("provider payload referenced an unreviewed URL host")


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_partial_datetime(raw: str) -> datetime | None:
    date_value = parse_period_date(raw) if re.fullmatch(r"\d{4}(?:-\d{2})?", raw) else None
    if date_value is not None:
        return datetime(date_value.year, date_value.month, date_value.day, tzinfo=UTC)
    try:
        date_value = date.fromisoformat(raw)
    except ValueError:
        return None
    return datetime(date_value.year, date_value.month, date_value.day, tzinfo=UTC)
