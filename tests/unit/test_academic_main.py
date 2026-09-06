"""Application-level wiring checks for the academic HTTP boundary."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.models import Base
from app.main import create_app


def test_main_mounts_academic_proposals_and_confirmation_fails_closed(tmp_path: Path) -> None:
    settings = Settings(
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

    assert proposal.status_code == 202
    assert sync.status_code == 200
    assert sync.json()["status"] == "setup_required"
    assert confirmation.status_code == 503
    assert confirmation.json() == {
        "status": "unavailable",
        "error_code": "notion_writer_unconfigured",
    }


def test_invalid_courses_database_id_is_setup_condition_not_startup_crash(
    tmp_path: Path,
) -> None:
    settings = Settings(
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
