"""Idempotent command for provisioning LangGraph checkpoint tables."""

from __future__ import annotations

from app.core.config import get_settings
from app.workflows.checkpoint import setup_checkpoint_schema


def main() -> None:
    settings = get_settings()
    setup_checkpoint_schema(settings.database_url)


if __name__ == "__main__":
    main()
