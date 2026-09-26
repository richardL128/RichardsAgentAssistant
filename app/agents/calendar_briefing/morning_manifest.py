"""Host-controlled rendering and retry-safe manifest for four morning embeds."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agents.calendar_briefing.contracts import (
    ActiveMorningCourse,
    CalendarEventSemanticStatus,
    ScheduledMorningCalendarItem,
)
from app.agents.calendar_briefing.morning_composer import (
    MorningCategory,
    ScheduleComposition,
    SpokenTaskComposition,
)

DISCORD_EMBED_TITLE_LIMIT = 256
DISCORD_EMBED_DESCRIPTION_LIMIT = 4_096
MORNING_MANIFEST_VERSION = "morning-four-embed-v2"
MORNING_CATEGORY_ORDER = (
    MorningCategory.COURSES,
    MorningCategory.JOBS,
    MorningCategory.MISC,
    MorningCategory.SCHEDULE,
)


class MorningManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MorningEmbedPayload(MorningManifestModel):
    title: str = Field(min_length=1, max_length=DISCORD_EMBED_TITLE_LIMIT)
    description: str = Field(min_length=1, max_length=DISCORD_EMBED_DESCRIPTION_LIMIT)


class MorningManifestEntry(MorningManifestModel):
    ordinal: int = Field(ge=1, le=4)
    category: MorningCategory
    delivery_key: str = Field(min_length=1, max_length=255)
    embed: MorningEmbedPayload


class MorningBriefingDeliveryManifest(MorningManifestModel):
    """Complete ordered four-embed payload persisted before the first POST."""

    version: str = Field(default=MORNING_MANIFEST_VERSION)
    local_date: date
    source_fingerprint: str = Field(min_length=8, max_length=128)
    entries: tuple[MorningManifestEntry, ...] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def entries_are_the_four_categories(self) -> MorningBriefingDeliveryManifest:
        if self.version != MORNING_MANIFEST_VERSION:
            raise ValueError("unsupported morning manifest version")
        for ordinal, (entry, category) in enumerate(
            zip(self.entries, MORNING_CATEGORY_ORDER, strict=True), start=1
        ):
            if entry.ordinal != ordinal or entry.category is not category:
                raise ValueError("morning manifest categories must be ordered and contiguous")
        if len({entry.delivery_key for entry in self.entries}) != 4:
            raise ValueError("morning manifest delivery keys must be unique")
        return self


def build_morning_briefing_manifest(
    *,
    local_date: date,
    delivery_key_prefix: str,
    timezone_name: str = "America/Toronto",
    active_courses: Sequence[ActiveMorningCourse] | None,
    course_items: Sequence[ScheduledMorningCalendarItem],
    job_items: Sequence[ScheduledMorningCalendarItem],
    misc_items: Sequence[ScheduledMorningCalendarItem],
    schedule_items: Sequence[ScheduledMorningCalendarItem],
    course_composition: SpokenTaskComposition | None = None,
    job_composition: SpokenTaskComposition | None,
    misc_composition: SpokenTaskComposition | None,
    schedule_composition: ScheduleComposition | None,
    unavailable: Mapping[MorningCategory, str] | None = None,
) -> MorningBriefingDeliveryManifest:
    """Render stable category layouts while keeping model output inside prose slots."""

    unavailable = unavailable or {}
    date_label = local_date.strftime("%A, %B %d, %Y").replace(" 0", " ")
    descriptions = {
        MorningCategory.COURSES: _courses_description(
            active_courses,
            course_items,
            course_composition,
            unavailable.get(MorningCategory.COURSES),
            local_date,
            timezone_name,
        ),
        MorningCategory.JOBS: _events_description(
            MorningCategory.JOBS,
            job_items,
            job_composition,
            unavailable.get(MorningCategory.JOBS),
            local_date,
            timezone_name,
        ),
        MorningCategory.MISC: _events_description(
            MorningCategory.MISC,
            misc_items,
            misc_composition,
            unavailable.get(MorningCategory.MISC),
            local_date,
            timezone_name,
        ),
        MorningCategory.SCHEDULE: _schedule_description(
            schedule_items,
            schedule_composition,
            unavailable.get(MorningCategory.SCHEDULE),
            timezone_name,
        ),
    }
    titles = {
        MorningCategory.COURSES: f"Courses — {date_label}",
        MorningCategory.JOBS: f"Jobs — {date_label}",
        MorningCategory.MISC: f"Misc — {date_label}",
        MorningCategory.SCHEDULE: f"Classes + Tutorials + Labs — {date_label}",
    }
    entries = tuple(
        MorningManifestEntry(
            ordinal=ordinal,
            category=category,
            delivery_key=f"{delivery_key_prefix}:{category.value}:v2",
            embed=MorningEmbedPayload(
                title=titles[category],
                description=(
                    _ensure_description_limit(descriptions[category], category)
                    if category is MorningCategory.SCHEDULE
                    else _fit_task_description(descriptions[category], category)
                ),
            ),
        )
        for ordinal, category in enumerate(MORNING_CATEGORY_ORDER, start=1)
    )
    fingerprint = hashlib.sha256(
        "\n".join(
            f"{entry.category.value}:{entry.embed.title}:{entry.embed.description}"
            for entry in entries
        ).encode("utf-8")
    ).hexdigest()
    return MorningBriefingDeliveryManifest(
        local_date=local_date,
        source_fingerprint=f"sha256:{fingerprint}",
        entries=entries,
    )


def _courses_description(
    courses: Sequence[ActiveMorningCourse] | None,
    items: Sequence[ScheduledMorningCalendarItem],
    composition: SpokenTaskComposition | None,
    unavailable: str | None,
    local_date: date,
    timezone_name: str,
) -> str:
    if unavailable is not None or courses is None:
        return _unavailable_text(unavailable or "Fresh course data was unavailable.")
    if not courses:
        return "No active courses were found in the fresh Notion course inventory."
    by_course: dict[str, list[ScheduledMorningCalendarItem]] = {}
    for item in items:
        by_course.setdefault(item.source_id, []).append(item)
    lines: list[str] = []
    for course in courses:
        lines.append(f"**{_escape_markdown(course.course_code)}**")
        events = by_course.get(course.course_id, [])
        if not events:
            code = _escape_markdown(course.course_code)
            lines.append(f"Nothing is pressing for {code} today or over the next seven days.")
        else:
            lines.append(
                _spoken_events_description(
                    MorningCategory.COURSES,
                    events,
                    composition,
                    local_date=local_date,
                    timezone_name=timezone_name,
                )
            )
        lines.append("")
    return "\n".join(lines).rstrip()


def _events_description(
    category: MorningCategory,
    items: Sequence[ScheduledMorningCalendarItem],
    composition: SpokenTaskComposition | None,
    unavailable: str | None,
    local_date: date,
    timezone_name: str,
) -> str:
    if unavailable is not None:
        return _unavailable_text(unavailable)
    if not items:
        label = "Jobs" if category is MorningCategory.JOBS else "Misc"
        return f"You have no incomplete {label} tasks today."
    description = _spoken_events_description(
        category,
        items,
        composition,
        local_date=local_date,
        timezone_name=timezone_name,
    )
    notice = _multi_source_notice(items)
    return f"{notice}\n\n{description}" if notice else description


def _spoken_events_description(
    category: MorningCategory,
    items: Sequence[ScheduledMorningCalendarItem],
    composition: SpokenTaskComposition | None,
    *,
    local_date: date,
    timezone_name: str,
) -> str:
    phrases = (
        {clause.event_id: clause.action_phrase for clause in composition.clauses}
        if composition is not None
        else {}
    )
    sentences: list[str] = []
    pending_when: str | None = None
    pending_phrases: list[str] = []

    def flush_pending() -> None:
        nonlocal pending_when
        if pending_when is None:
            return
        sentences.append(_spoken_sentence(pending_phrases, pending_when))
        pending_when = None
        pending_phrases.clear()

    for item in items:
        phrase = phrases.get(item.event_id)
        when = _natural_temporal_phrase(item, local_date, timezone_name)
        if phrase is None:
            flush_pending()
            sentences.append(
                _grounded_event_fallback(
                    category,
                    item,
                    natural_when=when,
                )
            )
            continue
        linked_phrase = _linked_text(_clean_action_phrase(phrase), item.source_url)
        if pending_when != when:
            flush_pending()
            pending_when = when
        pending_phrases.append(linked_phrase)
    flush_pending()
    return " ".join(sentences)


def _grounded_event_fallback(
    category: MorningCategory,
    item: ScheduledMorningCalendarItem,
    *,
    natural_when: str,
) -> str:
    """Render host facts when optional spoken composition is unavailable for one item."""

    if category is MorningCategory.COURSES:
        return _course_event_line(item)

    title = _linked_text(item.title, item.source_url)
    if item.semantic_status is CalendarEventSemanticStatus.VALID:
        detail = item.semantic_description or item.semantic_overview
    else:
        detail = None
    grounded_detail = _sentence(detail or "Additional details were unavailable.")
    return (
        f"You have “{title}” scheduled for {natural_when}. "
        f"{_trusted_item_context(item)} {grounded_detail}"
    )


def _course_event_line(item: ScheduledMorningCalendarItem) -> str:
    due = _due_label(item)
    if item.semantic_status is CalendarEventSemanticStatus.VALID and item.semantic_overview:
        overview = _sentence(item.semantic_overview)
        if item.semantic_description:
            overview = f"{overview} {_sentence(item.semantic_description)}"
        return f"• {overview} Due {due}. {_trusted_item_context(item)}{_notion_link_suffix(item)}"
    return (
        f"• {_linked_title(item)} is due {due}. {_trusted_item_context(item)} "
        "Additional interpretation was unavailable."
    )


def _due_label(item: ScheduledMorningCalendarItem) -> str:
    start = (
        item.local_start_label
        if item.relative_date_label == item.local_start_label
        else f"{item.relative_date_label} ({item.local_start_label})"
    )
    if item.local_end_label is None:
        return start
    return f"{start} through {item.local_end_label}"


def _trusted_item_context(item: ScheduledMorningCalendarItem) -> str:
    status = "complete" if item.completed else "incomplete"
    context = f"{item.source_label} · {item.display_kind}"
    return (
        f"[domain: {item.source_area.value}; status: {status}; "
        f"context: {_escape_markdown(context)}]"
    )


def _multi_source_notice(items: Sequence[ScheduledMorningCalendarItem]) -> str | None:
    domains = tuple(dict.fromkeys(item.source_area.value for item in items))
    if len(domains) <= 1:
        return None
    return "Host note: this section includes trusted work from " + ", ".join(domains) + "."


def _sentence(value: str) -> str:
    text = " ".join(value.split()).rstrip()
    if text[-1] in ".!?":
        return text
    return f"{text}."


def _spoken_sentence(phrases: Sequence[str], when: str) -> str:
    if not phrases:
        raise ValueError("spoken sentence requires at least one action phrase")
    if len(phrases) == 1:
        return f"You need to {phrases[0]} {when}."
    if len(phrases) == 2:
        return f"You need to {phrases[0]} {when}, and also {phrases[1]}."
    return f"You need to {', '.join(phrases[:-1])}, and {phrases[-1]} {when}."


def _clean_action_phrase(value: str) -> str:
    normalized = " ".join(value.split()).strip()
    return normalized.rstrip(".!?")


def _natural_temporal_phrase(
    item: ScheduledMorningCalendarItem,
    local_date: date,
    timezone_name: str,
) -> str:
    zone = ZoneInfo(timezone_name)
    start = item.starts_at.astimezone(zone)
    if item.ends_at is None:
        return _natural_temporal_point(start, local_date, include_time=not item.is_all_day)
    end = item.ends_at.astimezone(zone)
    return (
        f"from {_natural_temporal_point(start, local_date, include_time=not item.is_all_day)} "
        f"through {_natural_temporal_point(end, local_date, include_time=not item.is_all_day)}"
    )


def _natural_temporal_point(value: datetime, local_date: date, *, include_time: bool) -> str:
    day_delta = (value.date() - local_date).days
    if day_delta == 0:
        day = "today"
    elif day_delta == 1:
        day = "tomorrow"
    else:
        day = "on " + _date_label(value, include_year=value.year != local_date.year)
    if not include_time:
        return day
    return f"{day} at {_clock_label(value)}"


def _date_label(value: datetime, *, include_year: bool) -> str:
    label = value.strftime("%A, %B %d").replace(" 0", " ")
    return f"{label}, {value.year}" if include_year else label


def _clock_label(value: datetime) -> str:
    return value.strftime("%I:%M %p").lstrip("0")


def _schedule_description(
    items: Sequence[ScheduledMorningCalendarItem],
    composition: ScheduleComposition | None,
    unavailable: str | None,
    timezone_name: str,
) -> str:
    if unavailable is not None:
        return _unavailable_text(unavailable)
    if not items:
        return "No classes, tutorials, or labs overlap today."
    inferred = {item.event_id: item for item in composition.events} if composition else {}
    lines: list[str] = []
    if composition is None:
        lines.append("⚠ **Facts only** — schedule inference was unavailable.")
    lines.extend(
        (
            "```text",
            "TIME          COURSE       TYPE      LOCATION          ",
            "------------  -----------  --------  ------------------",
        )
    )
    notes: list[str] = []
    for item in items:
        value = inferred.get(item.event_id)
        course = value.course if value is not None and value.course_supported else "Unclear"
        session_type = (
            value.session_type.value
            if value is not None and value.session_type_supported
            else "Unclear"
        )
        location = value.location if value is not None and value.location_supported else "-"
        lines.append(
            f"{_cell(_time_label(item, timezone_name), 12)}  {_cell(course, 11)}  "
            f"{_cell(session_type, 8)}  {_cell(location, 18)}"
        )
        if value is not None and value.note is not None and value.note_supported:
            notes.append(f"• {_escape_markdown(course)}: {value.note}")
    lines.append("```")
    if composition is None:
        lines.append("Exact Notion names: " + "; ".join(item.title for item in items))
    if notes:
        lines.extend(("", *notes))
    return "\n".join(lines)


def _time_label(item: ScheduledMorningCalendarItem, timezone_name: str) -> str:
    if item.is_all_day:
        return "All day"
    zone = ZoneInfo(timezone_name)
    start = item.starts_at.astimezone(zone).strftime("%H:%M")
    if item.ends_at is None:
        return start
    return f"{start}-{item.ends_at.astimezone(zone).strftime('%H:%M')}"


def _linked_title(item: ScheduledMorningCalendarItem) -> str:
    return _linked_text(item.title, item.source_url)


def _notion_link_suffix(item: ScheduledMorningCalendarItem) -> str:
    if not _safe_notion_url(item.source_url):
        return ""
    return f" ([Notion]({item.source_url}))"


def _linked_text(value: str, source_url: str | None) -> str:
    text = _escape_markdown(value)
    if not _safe_notion_url(source_url):
        return text
    return f"[{text}]({source_url})"


def _safe_notion_url(value: str | None) -> bool:
    if value is None or len(value) > 1_000:
        return False
    parsed = urlsplit(value)
    host = parsed.hostname.casefold() if parsed.hostname else ""
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and (host in {"notion.so", "notion.site"} or host.endswith((".notion.so", ".notion.site")))
    )


def _escape_markdown(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    for marker in ("`", "*", "_", "~", "|", ">", "#", "[", "]"):
        escaped = escaped.replace(marker, f"\\{marker}")
    return escaped


def _cell(value: str, width: int) -> str:
    normalized = " ".join(value.replace("`", "'").split())
    if len(normalized) > width:
        normalized = normalized[: width - 1] + "…"
    return normalized.ljust(width)


def _unavailable_text(reason: str) -> str:
    return f"⚠ **Unavailable** — {reason} No stale calendar data was used."


def _ensure_description_limit(value: str, category: MorningCategory) -> str:
    if len(value) <= DISCORD_EMBED_DESCRIPTION_LIMIT:
        return value
    return _unavailable_text(f"Fresh {category.value} facts exceeded the safe Discord embed size.")


def _fit_task_description(value: str, category: MorningCategory) -> str:
    if len(value) <= DISCORD_EMBED_DESCRIPTION_LIMIT:
        return value
    task_label = {
        MorningCategory.COURSES: "course",
        MorningCategory.JOBS: "job",
        MorningCategory.MISC: "miscellaneous",
    }.get(category, category.value)
    notice = (
        f"\n\n⚠ Additional {task_label} tasks could not fit in this Discord embed; "
        "review the source calendar for the remaining trusted items."
    )
    budget = DISCORD_EMBED_DESCRIPTION_LIMIT - len(notice)
    prefix = value[:budget]
    boundaries = [prefix.rfind(". "), prefix.rfind(".\n"), prefix.rfind("\n\n")]
    boundary = max(boundaries)
    if boundary >= 0:
        prefix = prefix[: boundary + (1 if prefix[boundary : boundary + 2].startswith(".") else 0)]
    return (prefix.rstrip() + notice)[:DISCORD_EMBED_DESCRIPTION_LIMIT]


__all__ = [
    "DISCORD_EMBED_DESCRIPTION_LIMIT",
    "DISCORD_EMBED_TITLE_LIMIT",
    "MORNING_CATEGORY_ORDER",
    "MORNING_MANIFEST_VERSION",
    "MorningBriefingDeliveryManifest",
    "MorningEmbedPayload",
    "MorningManifestEntry",
    "build_morning_briefing_manifest",
]
