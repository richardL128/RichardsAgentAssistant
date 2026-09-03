"""PostgreSQL checkpoint configuration for durable LangGraph workflows."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from sqlalchemy.engine import make_url


def checkpoint_dsn(database_url: str) -> str:
    """Convert SQLAlchemy's driver URL into the DSN expected by Psycopg."""

    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError("LangGraph durable checkpoints require PostgreSQL")
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@contextmanager
def postgres_checkpointer(
    database_url: str, *, setup: bool = False
) -> Generator[PostgresSaver, None, None]:
    """Open a strict, non-pickle Postgres saver without logging its DSN."""

    serializer = JsonPlusSerializer(
        pickle_fallback=False,
        allowed_json_modules=None,
        allowed_msgpack_modules=None,
    )
    with PostgresSaver.from_conn_string(checkpoint_dsn(database_url)) as saver:
        # The saver accepts SerializerProtocol but the public factory does not
        # expose it, so install the explicitly strict serializer before use.
        saver.serde = serializer
        if setup:
            saver.setup()
        yield saver


def setup_checkpoint_schema(database_url: str) -> None:
    """Apply the upstream checkpointer's idempotent schema migrations."""

    with postgres_checkpointer(database_url, setup=True):
        pass


def thread_config(run_id: str) -> RunnableConfig:
    """Use the durable LifeAgent run ID as the sole LangGraph thread ID."""

    return RunnableConfig(configurable={"thread_id": run_id, "checkpoint_ns": ""})


__all__ = ["checkpoint_dsn", "postgres_checkpointer", "setup_checkpoint_schema", "thread_config"]
