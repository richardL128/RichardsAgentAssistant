"""Application-level wiring checks for the academic HTTP boundary."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main as app_main
from app.core.config import Settings
from app.db.models import Base
from app.main import create_app


def test_main_mounts_academic_proposals_and_confirmation_fails_closed(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+pysqlite:///{tmp_path / 'academic-main.db'}",
        artifact_root=tmp_path / "artifacts",
        notion_token="",
        notion_courses_database_id="",
    )
    app = create_app(settings)
    Base.metadata.create_all(app.state.database.engine)

    with TestClient(app) as client:
        sync = client.post("/academic/sync")
        proposal = client.post(
            "/academic/checkin",
            json={"reply": "completed notion-assignment-1"},
        )
        confirmation = client.post(
            f"/academic/confirm/{proposal.json()['proposal_id']}",
            json={"confirmation_event": proposal.json()["confirmation_event"]},
        )
        rejection = client.post(
            f"/academic/reject/{proposal.json()['proposal_id']}",
            json={"rejection_event": f"reject {proposal.json()['proposal_id']}"},
        )

    assert proposal.status_code == 202
    assert sync.status_code == 200
    assert sync.json()["status"] == "setup_required"
    assert confirmation.status_code == 503
    assert confirmation.json() == {
        "status": "unavailable",
        "error_code": "notion_writer_unconfigured",
    }
    assert rejection.status_code == 200
    assert rejection.json()["status"] == "rejected"
    assert app.state.academic_model is None
    assert app.state.academic_delivery is None


def test_main_wires_backend_delivery_without_starting_gateway(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+pysqlite:///{tmp_path / 'academic-gateway.db'}",
        artifact_root=tmp_path / "artifacts",
        discord_bot_token="test-token",
        discord_application_id="111111111111111111",
        discord_academic_channel_id="123456789012345678",
        discord_academic_authorized_user_ids=[987654321012345678],
        discord_academic_message_content_enabled=True,
    )
    app = create_app(settings)

    assert app.state.academic_delivery is not None
    assert app.state.notion_writer is None
    assert app.state.discord_host_ingress_state == "external"
    app.state.database.dispose()


def test_application_startup_does_not_probe_or_load_qwen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def unexpected_readiness(*args: object, **kwargs: object) -> object:
        calls.append("readiness")
        raise AssertionError("startup must not probe or load Qwen")

    async def unexpected_invoke(*args: object, **kwargs: object) -> object:
        calls.append("invoke")
        raise AssertionError("startup must not invoke Qwen")

    monkeypatch.setattr(app_main.OllamaRuntime, "ensure_ready", unexpected_readiness)
    monkeypatch.setattr(app_main.LLMGateway, "invoke_structured", unexpected_invoke)
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+pysqlite:///{tmp_path / 'model-free-startup.db'}",
        artifact_root=tmp_path / "artifacts",
        academic_memory_enabled=False,
    )

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/health/live")

    assert response.status_code == 200
    assert calls == []


def test_invalid_courses_database_id_is_setup_condition_not_startup_crash(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+pysqlite:///{tmp_path / 'invalid-notion.db'}",
        artifact_root=tmp_path / "artifacts",
        notion_token="notion-secret",
        notion_courses_database_id="not a valid id",
    )
    app = create_app(settings)
    Base.metadata.create_all(app.state.database.engine)

    with TestClient(app) as client:
        response = client.post("/academic/sync")

    assert response.status_code == 200
    assert response.json()["status"] == "setup_required"
    assert response.json()["diagnostic_codes"] == ["notion_configuration_invalid"]


def test_valid_notion_configuration_wires_discovered_calendar_writer(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+pysqlite:///{tmp_path / 'academic-writer.db'}",
        artifact_root=tmp_path / "artifacts",
        notion_token="notion-secret",
        notion_courses_database_id="coursesDatabase123",
    )

    app = create_app(settings)

    assert app.state.notion_writer is not None
    app.state.database.dispose()
