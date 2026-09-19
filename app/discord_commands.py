"""Dependency-light deterministic Discord command predicates."""

from __future__ import annotations


def is_discord_abort_command(content: str) -> bool:
    """Match only the exact ASCII abort code word after outer whitespace."""

    return content.strip() == "ABORT"


__all__ = ["is_discord_abort_command"]
