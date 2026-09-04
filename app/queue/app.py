"""Procrastinate application and queue names.

Creating this module does not open a database connection.  ``App.open_async``
does that only when an API/worker explicitly enters its lifecycle.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import procrastinate

from app.core.config import Settings, get_settings
from app.queue.retry import RetryPolicy, TransientRetryStrategy

QUEUE_NAMES = ("code_review", "academic_planner", "finance")

# A queue carries more than one kind of work: the code-review queue runs push
# reviews, the nightly consolidation, and repository ingestion.  Handlers are
# registered per job kind, and every kind names the queue it runs on so a job
# can never be dispatched to a worker that does not serve it.
JOB_KINDS: dict[str, str] = {
    "code_review": "code_review",
    "code_review_daily": "code_review",
    "code_review_ingest": "code_review",
    "academic_planner": "academic_planner",
    "finance": "finance",
}


def postgres_conninfo(database_url: str) -> str:
    """Convert SQLAlchemy's psycopg URL to a psycopg-compatible conninfo URL."""

    parsed = urlsplit(database_url)
    scheme = parsed.scheme.removesuffix("+psycopg")
    return urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment))


def create_procrastinate_app(settings: Settings | None = None) -> procrastinate.App:
    """Build an app with one-worker defaults and no connection at import time."""

    app_settings = settings or get_settings()
    connector = procrastinate.PsycopgConnector(
        conninfo=postgres_conninfo(app_settings.database_url),
        min_size=1,
        max_size=1,
    )
    return procrastinate.App(
        connector=connector,
        import_paths=["app.queue.tasks"],
        worker_defaults={"concurrency": app_settings.worker_concurrency},
    )


def create_retry_strategy(settings: Settings | None = None) -> TransientRetryStrategy:
    app_settings = settings or get_settings()
    return TransientRetryStrategy(
        RetryPolicy(
            max_attempts=app_settings.retry_max_attempts,
            base_delay_seconds=app_settings.retry_base_delay_seconds,
            max_delay_seconds=app_settings.retry_max_delay_seconds,
            jitter_ratio=app_settings.retry_jitter_ratio,
        )
    )


_settings = get_settings()
procrastinate_app = create_procrastinate_app(_settings)
default_retry_strategy = create_retry_strategy(_settings)

__all__ = [
    "JOB_KINDS",
    "QUEUE_NAMES",
    "create_procrastinate_app",
    "create_retry_strategy",
    "default_retry_strategy",
    "postgres_conninfo",
    "procrastinate_app",
]
