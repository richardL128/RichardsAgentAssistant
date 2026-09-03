"""Alembic environment for the application and Procrastinate schema."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def run_migrations_offline() -> None:
    """Run migrations without opening a database connection."""

    settings = get_settings()
    database_url = config.attributes.get("database_url", settings.database_url)
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against the configured database."""

    settings = get_settings()
    database_url = config.attributes.get("database_url", settings.database_url)
    section = config.get_section(config.config_ini_section, {}) or {}
    # Alembic's ConfigParser interpolates percent signs.  Passwords remain in
    # process memory only and are never printed by this module.
    section["sqlalchemy.url"] = str(database_url).replace("%", "%%")
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
