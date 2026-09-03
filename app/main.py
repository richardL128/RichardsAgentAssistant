"""FastAPI application entry point."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.health import router as health_router
from app.core.config import Settings, get_settings
from app.db.session import Database


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the API application, allowing tests to inject settings."""

    app_settings = settings or get_settings()
    database = Database(app_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
        yield
        database.dispose()

    app = FastAPI(title=app_settings.app_name, version=__version__, lifespan=lifespan)
    app.state.settings = app_settings
    app.state.database = database
    app.include_router(health_router)
    return app


app = create_app()

__all__ = ["app", "create_app"]
