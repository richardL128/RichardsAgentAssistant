from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.models import Base
from app.main import create_app


def test_main_mounts_read_only_finance_source_api(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path / 'finance-main.db'}",
        artifact_root=tmp_path / "artifacts",
    )
    app = create_app(settings)
    Base.metadata.create_all(app.state.database.engine)

    with TestClient(app) as client:
        response = client.get("/finance/sources")

    assert response.status_code == 200
    assert response.json() == {"enabled": False, "sources": []}
    assert app.state.finance_store is not None
