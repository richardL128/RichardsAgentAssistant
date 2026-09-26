"""Application-boundary checks for career sync, health, and write confirmation."""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.models import Base
from app.main import create_app


def _app(tmp_path: Path, **overrides: object):
    values: dict[str, object] = {
        "database_url": f"sqlite+pysqlite:///{tmp_path / 'career-main.db'}",
        "artifact_root": tmp_path / "artifacts",
        "notion_token": "",
        "notion_courses_database_id": "",
    }
    values.update(overrides)
    settings = Settings(
        _env_file=None,
        **values,
    )
    app = create_app(settings)
    Base.metadata.create_all(app.state.database.engine)
    return app


def test_career_http_boundary_surfaces_setup_and_safe_health_without_source_contents(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        sync = client.post("/job-interviews/sync")
        health = client.get("/job-interviews/health")

    assert sync.status_code == 200
    assert sync.json()["status"] == "setup_required"
    assert sync.json()["diagnostic_codes"] == ["notion_configuration_missing"]
    assert health.status_code == 200
    assert health.json() == {
        "jobs_discovery_status": "missing",
        "last_successful_jobs_sync": None,
        "active_application_row_count": 0,
        "upcoming_interview_count": 0,
        "unresolved_clarification_count": 0,
        "failed_or_stale_plan_count": 0,
        "pending_write_proposal_count": 0,
        "last_interview_reminder_at": None,
        "research_provider_configured": False,
    }
    assert "company" not in str(health.json()).casefold()


def test_career_http_boundary_never_invokes_writer_without_exact_confirmation(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    proposal_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    with TestClient(app) as client:
        wrong = client.post(
            f"/job-interviews/confirm/{proposal_id}",
            json={"confirmation_event": "yes, do it"},
        )
        exact_but_unconfigured = client.post(
            f"/job-interviews/confirm/{proposal_id}",
            json={"confirmation_event": f"confirm {proposal_id}"},
        )

    assert wrong.status_code == 200
    assert wrong.json() == {
        "status": "confirmation_required",
        "proposal_id": str(proposal_id),
    }
    assert exact_but_unconfigured.status_code == 503
    assert exact_but_unconfigured.json()["error_code"] == "career_notion_writer_unconfigured"


def test_valid_notion_configuration_wires_explicit_connector_but_not_legacy_career_writer(
    tmp_path: Path,
) -> None:
    app = _app(
        tmp_path,
        notion_token="notion-secret",
        notion_courses_database_id="coursesDatabase123",
        notion_action_items_database_id="actionsDatabase123",
        notion_applications_database_id="applicationsDatabase123",
        notion_interviews_database_id="interviewsDatabase123",
    )
    assert app.state.job_interview_syncer._connector is not None
    assert app.state.job_interview_notion_writer is None
    app.state.database.dispose()
