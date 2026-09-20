"""Reserved semantic roles for Notion rows in the Courses database."""

from __future__ import annotations

import re
from enum import StrEnum


class AcademicCalendarRole(StrEnum):
    """Host-derived role for one discovered Courses row."""

    COURSE = "course"
    MISC = "misc"
    LEARN = "learn"


_TASK_PREFIX = re.compile(r"^\s*task\b\s*(?:[—:-]\s*)?", re.IGNORECASE)
LEARN_CALENDAR_TITLE = "Classes + Tutorials + Labs"


def academic_calendar_role(title: str) -> AcademicCalendarRole:
    """Reserve only an exact, normalized ``misc`` title for general to-dos."""

    normalized = " ".join(title.casefold().split())
    if normalized == " ".join(LEARN_CALENDAR_TITLE.casefold().split()):
        return AcademicCalendarRole.LEARN
    if normalized == AcademicCalendarRole.MISC.value:
        return AcademicCalendarRole.MISC
    return AcademicCalendarRole.COURSE


def canonical_misc_task_title(title: str) -> str:
    """Build the stable title written into the reserved misc calendar."""

    body = _TASK_PREFIX.sub("", title.strip(), count=1).strip() or "Untitled task"
    return f"Task — {body}"[:500]


__all__ = [
    "LEARN_CALENDAR_TITLE",
    "AcademicCalendarRole",
    "academic_calendar_role",
    "canonical_misc_task_title",
]
