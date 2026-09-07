"""Official EIA public data parsers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from io import BytesIO
from typing import cast
from zipfile import BadZipFile, ZipFile

from pydantic import ValidationError

from app.agents.finance.contracts import (
    QuantValue,
    SourceClassification,
    SourceDocument,
    SourceQuery,
)

from .common import (
    JsonObject,
    ProviderParseError,
    in_query_window,
    parse_datetime,
    parse_json_lines,
    parse_json_object,
    parse_period_date,
    stable_id,
    text,
    unique_lower,
    unique_upper,
)
from .registries import EIA_PUBLIC_DATA, EIA_SOURCE_VERSION

EIA_BULK_URL = "https://www.eia.gov/opendata/bulk/PET.zip"
EIA_API_URL = "https://api.eia.gov/v2/petroleum/pri/spt/data/"
EIA_REVIEWED_BULK_SERIES: tuple[str, ...] = (
    "PET.RBRTE.D",
    "PET.RWTC.D",
    "PET.WCESTUS1.W",
    "PET.WCRSTUS1.W",
)
_MAX_UNCOMPRESSED_BULK_BYTES = 256 * 1024 * 1024


def parse_eia_bulk_records(
    payload: bytes | str | Sequence[JsonObject],
    query: SourceQuery,
    retrieved_at: datetime,
    *,
    source_url: str = EIA_BULK_URL,
) -> tuple[SourceDocument, ...]:
    records = parse_json_lines(payload) if isinstance(payload, bytes | str) else tuple(payload)
    documents: list[SourceDocument] = []
    for record in records:
        documents.extend(_documents_from_record(record, query, retrieved_at, source_url))
    return tuple(documents)


def parse_eia_bulk_zip(
    payload: bytes,
    query: SourceQuery,
    retrieved_at: datetime,
    *,
    source_url: str = EIA_BULK_URL,
    allowed_series_ids: Sequence[str] = EIA_REVIEWED_BULK_SERIES,
) -> tuple[SourceDocument, ...]:
    """Parse only audited series from a reviewed EIA ZIP without extracting files."""

    reviewed = frozenset(allowed_series_ids)
    try:
        with ZipFile(BytesIO(payload)) as archive:
            members = tuple(
                member
                for member in archive.infolist()
                if not member.is_dir() and member.filename.casefold().endswith(".txt")
            )
            if len(members) != 1:
                raise ProviderParseError("EIA bulk ZIP must contain one reviewed text payload")
            member = members[0]
            if member.file_size > _MAX_UNCOMPRESSED_BULK_BYTES:
                raise ProviderParseError("EIA bulk ZIP exceeds the uncompressed size ceiling")
            selected: list[JsonObject] = []
            with archive.open(member) as handle:
                for raw_line in handle:
                    if len(raw_line) > 2_000_000:
                        raise ProviderParseError("EIA bulk record exceeds the line size ceiling")
                    try:
                        record = parse_json_object(raw_line)
                    except ProviderParseError:
                        continue
                    if _series_id(record) in reviewed:
                        selected.append(record)
    except (BadZipFile, OSError) as exc:
        raise ProviderParseError("EIA bulk payload is not a valid ZIP archive") from exc
    return parse_eia_bulk_records(selected, query, retrieved_at, source_url=source_url)


def parse_eia_api_response(
    payload: bytes | str | JsonObject,
    query: SourceQuery,
    retrieved_at: datetime,
    *,
    source_url: str = EIA_API_URL,
) -> tuple[SourceDocument, ...]:
    root = parse_json_object(payload)
    response = root.get("response")
    if not isinstance(response, Mapping):
        raise ProviderParseError("EIA API response is missing response object")
    response_object = cast(JsonObject, response)
    data = response_object.get("data")
    if not isinstance(data, Sequence) or isinstance(data, str | bytes | bytearray):
        raise ProviderParseError("EIA API response is missing data rows")
    data_rows = cast(Sequence[object], data)
    rows = tuple(cast(JsonObject, row) for row in data_rows if isinstance(row, Mapping))
    frequency = text(response_object.get("frequency")) or text(root.get("frequency"))
    documents: list[SourceDocument] = []
    for row in rows:
        record = dict(row)
        if frequency and "frequency" not in record:
            record["frequency"] = frequency
        documents.extend(_documents_from_record(record, query, retrieved_at, source_url))
    return tuple(documents)


def _documents_from_record(
    record: JsonObject,
    query: SourceQuery,
    retrieved_at: datetime,
    source_url: str,
) -> tuple[SourceDocument, ...]:
    series_id = _series_id(record)
    title = _series_title(record, series_id)
    unit = _unit(record)
    frequency = text(record.get("frequency") or record.get("f"))
    release_time = parse_datetime(
        record.get("release_date") or record.get("last_updated") or record.get("updated")
    )
    rows = _value_rows(record)
    documents: list[SourceDocument] = []
    for period, value in rows:
        period_date = parse_period_date(period)
        if period_date is None or value is None:
            continue
        published_at = release_time or datetime(
            period_date.year,
            period_date.month,
            period_date.day,
            tzinfo=retrieved_at.tzinfo,
        )
        if not in_query_window(published_at, query, retrieved_at):
            continue
        try:
            documents.append(
                SourceDocument.model_validate(
                    {
                        "source_id": EIA_PUBLIC_DATA,
                        "source_version": EIA_SOURCE_VERSION,
                        "external_id": f"{series_id}:{period}",
                        "title": title,
                        "url": source_url,
                        "published_at": published_at,
                        "retrieved_at": retrieved_at,
                        "classification": SourceClassification.PRIMARY,
                        "license_allows_excerpt": False,
                        "tickers": unique_upper(query.tickers),
                        "themes": unique_lower((*query.themes, "energy")),
                        "numbers": (
                            QuantValue(
                                label=_number_label(title, frequency, period),
                                value=value,
                                unit=unit,
                                as_of=period_date,
                                source_ids=(EIA_PUBLIC_DATA,),
                            ),
                        ),
                    }
                )
            )
        except ValidationError:
            continue
    return tuple(documents)


def _value_rows(record: JsonObject) -> tuple[tuple[str, float | None], ...]:
    data = record.get("data")
    if isinstance(data, Sequence) and not isinstance(data, str | bytes | bytearray):
        rows: list[tuple[str, float | None]] = []
        for row in cast(Sequence[object], data):
            if isinstance(row, Mapping):
                rows.extend(_value_rows(cast(JsonObject, row)))
            elif isinstance(row, Sequence) and not isinstance(row, str | bytes | bytearray):
                cells = tuple(cast(Sequence[object], row))
                if len(cells) >= 2:
                    rows.append((text(cells[0]), _float_value(cells[1])))
        return tuple(rows)
    period = text(record.get("period") or record.get("date"))
    value = _float_value(record.get("value"))
    return ((period, value),) if period else ()


def _series_id(record: JsonObject) -> str:
    return (
        text(record.get("series_id"))
        or text(record.get("seriesId"))
        or text(record.get("series"))
        or stable_id(record.get("name"), record.get("description"), max_length=24)
    )[:255]


def _series_title(record: JsonObject, series_id: str) -> str:
    return (
        text(record.get("name"))
        or text(record.get("seriesDescription"))
        or text(record.get("description"))
        or f"EIA series {series_id}"
    )[:500]


def _unit(record: JsonObject) -> str:
    return (
        text(record.get("units"))
        or text(record.get("unit"))
        or text(record.get("value-units"))
        or "value"
    )[:40]


def _number_label(title: str, frequency: str, period: str) -> str:
    pieces = [title]
    if frequency:
        pieces.append(frequency)
    pieces.append(period)
    return " ".join(pieces)[:120]


def _float_value(value: object) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    raw = text(value).replace(",", "")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
