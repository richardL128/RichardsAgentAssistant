"""DST-aware periodic scheduling at the Toronto boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TORONTO = ZoneInfo("America/Toronto")


@dataclass(frozen=True, slots=True)
class PeriodicOccurrence:
    """A resolved occurrence with both local display and UTC enqueue times."""

    local_time: datetime
    scheduled_at: datetime

    @property
    def period_date(self) -> date:
        return self.local_time.date()


@dataclass(frozen=True, slots=True)
class TorontoPeriodicSchedule:
    """A weekly schedule expressed in local Toronto wall-clock time."""

    hour: int
    minute: int = 0
    weekdays: frozenset[int] | None = None
    timezone_name: str = "America/Toronto"

    def __post_init__(self) -> None:
        if not 0 <= self.hour <= 23 or not 0 <= self.minute <= 59:
            raise ValueError("schedule time must be a valid 24-hour wall-clock time")
        if self.weekdays is not None and not self.weekdays.issubset(set(range(7))):
            raise ValueError("weekdays must use ISO weekday values 0 (Monday) through 6")
        ZoneInfo(self.timezone_name)

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    @classmethod
    def from_time(
        cls,
        wall_time: time,
        *,
        weekdays: frozenset[int] | None = None,
        timezone_name: str = "America/Toronto",
    ) -> TorontoPeriodicSchedule:
        if wall_time.tzinfo is not None:
            raise ValueError("configured schedule must be a local wall-clock time")
        return cls(
            hour=wall_time.hour,
            minute=wall_time.minute,
            weekdays=weekdays,
            timezone_name=timezone_name,
        )

    def matches(self, local_time: datetime) -> bool:
        """Whether an aware instant represents this unique local period."""

        if local_time.tzinfo is None or local_time.utcoffset() is None:
            raise ValueError("local_time must be timezone-aware")
        localized = local_time.astimezone(self.zone)
        if localized.fold == 1:
            return False
        if self.weekdays is not None and localized.weekday() not in self.weekdays:
            return False
        return localized.hour == self.hour and localized.minute == self.minute

    def next_occurrence(self, after_utc: datetime) -> PeriodicOccurrence:
        """Find the next valid wall-clock occurrence after an aware UTC instant.

        Nonexistent spring-forward times are skipped. Ambiguous fall-back times
        resolve to the first occurrence (fold 0), so a period enqueues once.
        """

        if after_utc.tzinfo is None:
            raise ValueError("after_utc must be timezone-aware")
        utc_after = after_utc.astimezone(UTC)
        local_after = utc_after.astimezone(ZoneInfo(self.timezone_name))
        allowed = self.weekdays
        for offset in range(15):
            day = local_after.date() + timedelta(days=offset)
            if allowed is not None and day.weekday() not in allowed:
                continue
            candidate = _resolve_wall_time(
                day, time(self.hour, self.minute), ZoneInfo(self.timezone_name)
            )
            if candidate is None:
                continue
            if candidate.scheduled_at > utc_after:
                return candidate
        raise RuntimeError("schedule did not yield an occurrence in the next two weeks")


def stable_period_key(
    namespace: str,
    occurrence: PeriodicOccurrence,
    *,
    version: str = "v1",
) -> str:
    """Return a stable local-calendar key for one scheduled period."""

    from app.queue.idempotency import build_idempotency_key

    local = occurrence.local_time
    return build_idempotency_key(
        namespace,
        local.date().isoformat(),
        local.strftime("%H%M"),
        version=version,
    )


def _resolve_wall_time(
    day: date,
    wall_time: time,
    zone: ZoneInfo,
) -> PeriodicOccurrence | None:
    candidates: list[PeriodicOccurrence] = []
    for fold in (0, 1):
        local = datetime.combine(day, wall_time, tzinfo=zone).replace(fold=fold)
        utc = local.astimezone(UTC)
        if utc.astimezone(zone).replace(tzinfo=None) == local.replace(tzinfo=None):
            candidates.append(PeriodicOccurrence(local, utc))
    if not candidates:
        return None
    return min(candidates, key=lambda value: value.scheduled_at)
