"""Exact human-review commands intercepted at the application boundary."""

from __future__ import annotations

import re
import uuid
from typing import Literal, cast

AcademicCommandAction = Literal["confirm", "reject"]

_COMMAND_PATTERN = re.compile(
    r"(?P<action>confirm|reject) "
    r"(?P<proposal_id>[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12})"
)


def parse_academic_command(content: str) -> tuple[AcademicCommandAction, uuid.UUID] | None:
    """Parse only a lowercase command with one canonical UUID and no extras."""

    match = _COMMAND_PATTERN.fullmatch(content)
    if match is None:
        return None
    proposal_id = uuid.UUID(match.group("proposal_id"))
    if str(proposal_id) != match.group("proposal_id"):
        return None
    return cast(AcademicCommandAction, match.group("action")), proposal_id


__all__ = ["AcademicCommandAction", "parse_academic_command"]
