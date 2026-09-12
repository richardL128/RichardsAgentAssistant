"""Deterministic multipart Discord manifest builder for morning briefings."""

from __future__ import annotations

import hashlib
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

DISCORD_CONTENT_LIMIT = 2_000


class CalendarBriefingManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CalendarBriefingManifestPart(CalendarBriefingManifestModel):
    ordinal: int = Field(ge=1)
    delivery_key: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=DISCORD_CONTENT_LIMIT)


class CalendarBriefingDeliveryManifest(CalendarBriefingManifestModel):
    """Fully rendered, retry-safe ordered message parts."""

    delivery_key_prefix: str = Field(min_length=1, max_length=220)
    logical_content_fingerprint: str = Field(min_length=8, max_length=128)
    logical_content_chars: int = Field(ge=0)
    parts: tuple[CalendarBriefingManifestPart, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def parts_are_ordered_and_bounded(self) -> CalendarBriefingDeliveryManifest:
        for expected, part in enumerate(self.parts, start=1):
            if part.ordinal != expected:
                raise ValueError("calendar briefing manifest part ordinals must be contiguous")
            expected_key = f"{self.delivery_key_prefix}:{expected:03d}"
            if part.delivery_key != expected_key:
                raise ValueError("calendar briefing manifest delivery keys must be deterministic")
            if len(part.content) > DISCORD_CONTENT_LIMIT:
                raise ValueError("calendar briefing manifest part exceeds Discord content limit")
        return self


def build_calendar_briefing_manifest(
    logical_content: str,
    *,
    delivery_key_prefix: str,
    max_chars: int = DISCORD_CONTENT_LIMIT,
) -> CalendarBriefingDeliveryManifest:
    """Split a logical briefing at paragraph, line, then hard character boundaries."""

    if not logical_content:
        raise ValueError("calendar briefing content must not be empty")
    if max_chars > DISCORD_CONTENT_LIMIT:
        raise ValueError("calendar briefing max_chars cannot exceed Discord's content limit")
    if max_chars < 80:
        raise ValueError("calendar briefing max_chars must leave room for content and labels")
    if len(logical_content) <= max_chars:
        parts = (
            CalendarBriefingManifestPart(
                ordinal=1,
                delivery_key=f"{delivery_key_prefix}:001",
                content=logical_content,
            ),
        )
        return CalendarBriefingDeliveryManifest(
            delivery_key_prefix=delivery_key_prefix,
            logical_content_fingerprint=_fingerprint(logical_content),
            logical_content_chars=len(logical_content),
            parts=parts,
        )

    reserve = len(_part_label(999, 999))
    while True:
        body_limit = max_chars - reserve
        if body_limit < 1:
            raise ValueError("calendar briefing max_chars leaves no room after continuation label")
        bodies = _pack_segments(_segments_for_limit(logical_content, body_limit), body_limit)
        needed = max(len(_part_label(index, len(bodies))) for index in range(1, len(bodies) + 1))
        if needed <= reserve:
            break
        reserve = needed

    parts = tuple(
        CalendarBriefingManifestPart(
            ordinal=index,
            delivery_key=f"{delivery_key_prefix}:{index:03d}",
            content=f"{_part_label(index, len(bodies))}{body}",
        )
        for index, body in enumerate(bodies, start=1)
    )
    return CalendarBriefingDeliveryManifest(
        delivery_key_prefix=delivery_key_prefix,
        logical_content_fingerprint=_fingerprint(logical_content),
        logical_content_chars=len(logical_content),
        parts=parts,
    )


def _segments_for_limit(content: str, limit: int) -> list[str]:
    segments: list[str] = []
    for paragraph in _paragraph_segments(content):
        if len(paragraph) <= limit:
            segments.append(paragraph)
            continue
        for line in paragraph.splitlines(keepends=True):
            if len(line) <= limit:
                segments.append(line)
                continue
            segments.extend(line[start : start + limit] for start in range(0, len(line), limit))
    return segments


def _paragraph_segments(content: str) -> list[str]:
    tokens = re.split(r"(\n{2,})", content)
    segments: list[str] = []
    index = 0
    while index < len(tokens):
        segment = tokens[index]
        if index + 1 < len(tokens):
            segment += tokens[index + 1]
        if segment:
            segments.append(segment)
        index += 2
    return segments


def _pack_segments(segments: list[str], limit: int) -> list[str]:
    bodies: list[str] = []
    current = ""
    for segment in segments:
        if len(segment) > limit:
            raise ValueError("calendar briefing segment exceeds part body limit")
        if current and len(current) + len(segment) > limit:
            bodies.append(current)
            current = segment
        else:
            current += segment
    if current:
        bodies.append(current)
    return bodies


def _part_label(index: int, total: int) -> str:
    return f"(Part {index}/{total})\n"


def _fingerprint(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


__all__ = [
    "DISCORD_CONTENT_LIMIT",
    "CalendarBriefingDeliveryManifest",
    "CalendarBriefingManifestPart",
    "build_calendar_briefing_manifest",
]
