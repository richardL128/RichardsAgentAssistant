"""Small synchronous SQLAlchemy database wrapper used by health checks."""

from __future__ import annotations

import re
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings


class Database:
    """Lazily-created SQLAlchemy engine and Phase 0 health probes."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._engine: Engine | None = None

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            connect_args: dict[str, Any] = {}
            if self.settings.database_url.startswith(("postgresql", "mysql")):
                connect_args["connect_timeout"] = int(
                    self.settings.database_connect_timeout_seconds
                )
            self._engine = create_engine(
                self.settings.database_url,
                pool_pre_ping=True,
                connect_args=connect_args,
            )
        return self._engine

    @contextmanager
    def connection(self) -> Generator[Any, None, None]:
        with self.engine.connect() as connection:
            yield connection

    def check_connection(self) -> tuple[bool, str]:
        try:
            with self.connection() as connection:
                connection.execute(text("SELECT 1"))
            return True, "database connection is healthy"
        except (SQLAlchemyError, OSError) as exc:
            return False, self._error_detail(exc)

    def check_procrastinate_schema(self) -> tuple[bool, str]:
        """Check for Procrastinate's canonical jobs table.

        The SQL is portable for SQLite test databases and PostgreSQL local
        deployments.  Procrastinate uses ``procrastinate_jobs`` in the public
        schema by default; its schema is provisioned by the queue migration.
        """

        try:
            with self.connection() as connection:
                dialect = connection.dialect.name
                if dialect == "sqlite":
                    result = connection.execute(
                        text(
                            "SELECT 1 FROM sqlite_master "
                            "WHERE type = 'table' AND name = 'procrastinate_jobs'"
                        )
                    ).first()
                else:
                    result = connection.execute(
                        text("SELECT to_regclass('public.procrastinate_jobs')")
                    ).scalar_one_or_none()
            if result:
                return True, "Procrastinate schema is present"
            return False, "Procrastinate schema is missing (run database migrations)"
        except (SQLAlchemyError, OSError) as exc:
            return False, self._error_detail(exc)

    def dispose(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    @staticmethod
    def _error_detail(exc: BaseException) -> str:
        # Exception strings can contain connection URLs.  Keep only a stable,
        # non-secret class/message summary and redact common URL credentials.
        message = str(exc).splitlines()[0][:240]
        # Driver errors are useful during local diagnosis, but may echo a DSN
        # or keyword connection options.  Remove credentials before exposing
        # the first line to a health endpoint.
        message = re.sub(r"([a-z][a-z0-9+.-]*://)[^@\s]+@", r"\1", message)
        message = re.sub(
            r"(?i)(password|passwd|pwd|secret)\s*[=:]\s*[^\s,;]+",
            r"\1=[redacted]",
            message,
        )
        return f"{exc.__class__.__name__}: {message}"
