"""Provision the queue table required by Procrastinate.

Revision ID: 0001_phase0_procrastinate
Revises:
"""

from __future__ import annotations

from importlib.resources import files

from alembic import op

revision = "0001_phase0_procrastinate"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Phase 0 intentionally contains no shared agent/run tables.  Procrastinate
    # owns its queue DDL (tables, functions, triggers, and indexes), so use the
    # schema shipped by the pinned dependency instead of maintaining an unsafe
    # partial copy here.
    try:
        schema = files("procrastinate").joinpath("sql", "schema.sql").read_text()
    except (ModuleNotFoundError, FileNotFoundError) as exc:
        raise RuntimeError(
            "procrastinate must be installed before applying the Phase 0 migration"
        ) from exc
    # Psycopg interprets percent signs in plain query strings as client-side
    # placeholders. The shipped PL/pgSQL contains ``RAISE ... '%'`` messages,
    # so escape them for the DBAPI; PostgreSQL still receives a single percent.
    op.get_bind().exec_driver_sql(schema.replace("%", "%%"))


def downgrade() -> None:
    # The queue schema is managed by Procrastinate and has functions/types that
    # must be dropped in dependency order.  Keep downgrade explicit and safe
    # for local environments; a fresh Phase 0 database can simply be discarded.
    op.execute("DROP TABLE IF EXISTS procrastinate_events CASCADE")
    op.execute("DROP TABLE IF EXISTS procrastinate_periodic_defers CASCADE")
    op.execute("DROP TABLE IF EXISTS procrastinate_jobs CASCADE")
    op.execute("DROP TABLE IF EXISTS procrastinate_workers CASCADE")
    op.execute("DROP TYPE IF EXISTS procrastinate_job_to_defer_v1 CASCADE")
    op.execute("DROP TYPE IF EXISTS procrastinate_job_event_type CASCADE")
    op.execute("DROP TYPE IF EXISTS procrastinate_job_status CASCADE")
