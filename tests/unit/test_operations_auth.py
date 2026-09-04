from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.auth import require_ops_console_user
from app.core.config import Settings


def _app(settings: Settings) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings

    @app.get("/private")
    async def private(user_id: Annotated[str, Depends(require_ops_console_user)]) -> dict[str, str]:
        return {"user_id": user_id}

    return app


def test_operations_auth_fails_closed_when_unconfigured() -> None:
    with TestClient(_app(Settings())) as client:
        response = client.get("/private")

    assert response.status_code == 503


def test_operations_auth_rejects_invalid_credentials_without_echoing_secrets() -> None:
    settings = Settings(ops_console_username="richard", ops_console_password="private-password")
    with TestClient(_app(settings)) as client:
        response = client.get("/private", auth=("richard", "wrong-password"))

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic")
    assert "private-password" not in response.text
    assert "richard" not in response.text


def test_operations_auth_returns_fixed_single_user_identity() -> None:
    settings = Settings(ops_console_username="richard", ops_console_password="private-password")
    with TestClient(_app(settings)) as client:
        response = client.get("/private", auth=("richard", "private-password"))

    assert response.status_code == 200
    assert response.json() == {"user_id": "ops-console"}
