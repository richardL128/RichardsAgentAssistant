"""FastAPI application entry point."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.github import router as github_router
from app.api.health import router as health_router
from app.core.config import Settings, get_settings
from app.db.session import Database
from app.llm.gateway import LLMGateway
from app.queue.app import procrastinate_app
from app.queue.tasks import code_review_task, defer_idempotent_async


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the API application, allowing tests to inject settings."""

    use_global_queue = settings is None
    app_settings = settings or get_settings()
    database = Database(app_settings)
    gateway = LLMGateway(app_settings)

    async def enqueue_code_review(run_id: str, idempotency_key: str) -> object:
        return await defer_idempotent_async(code_review_task, run_id, idempotency_key)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
        try:
            if use_global_queue:
                async with procrastinate_app.open_async():
                    yield
            else:
                yield
        finally:
            database.dispose()

    app = FastAPI(title=app_settings.app_name, version=__version__, lifespan=lifespan)
    app.state.settings = app_settings
    app.state.database = database
    app.state.model_identity = gateway.model_identity
    app.state.model_config_version = gateway.config_version
    app.state.enqueue_code_review = enqueue_code_review
    app.include_router(health_router)
    app.include_router(github_router)
    return app


app = create_app()

__all__ = ["app", "create_app"]
