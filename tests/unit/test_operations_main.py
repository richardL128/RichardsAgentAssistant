"""Application wiring checks for the operations-console boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.models import Base
from app.main import create_app, format_toronto_time


def test_main_mounts_authenticated_operations_api_and_templates(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path / 'operations-main.db'}",
        artifact_root=tmp_path / "artifacts",
        ops_console_username="operator",
        ops_console_password="private-password",
    )
    app = create_app(settings)
    Base.metadata.create_all(app.state.database.engine)

    with TestClient(app) as client:
        unauthenticated = client.get("/api/operations")
        authenticated = client.get(
            "/api/operations",
            auth=("operator", "private-password"),
        )

    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 200
    assert [card["component"] for card in authenticated.json()] == [
        "finance",
        "code_review",
        "academic_planner",
        "shared_services",
    ]
    assert authenticated.headers["content-security-policy"] == (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )
    assert authenticated.headers["x-content-type-options"] == "nosniff"
    assert app.state.templates is not None
    assert any(getattr(route, "name", None) == "static" for route in app.routes)


def test_toronto_time_filter_is_exact_and_dst_aware() -> None:
    assert format_toronto_time(None) == "Never"
    assert format_toronto_time(datetime(2026, 1, 15, 15, 30, tzinfo=UTC)) == (
        "2026-01-15 10:30:00 EST"
    )
    assert format_toronto_time(datetime(2026, 7, 15, 15, 30, tzinfo=UTC)) == (
        "2026-07-15 11:30:00 EDT"
    )
