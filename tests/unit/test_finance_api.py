from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.finance import router
from app.db.finance import FinanceRunFilterMetadata, FinanceSourceRecord

NOW = datetime(2026, 9, 4, 13, tzinfo=UTC)


class Store:
    def source_approval_gate(self) -> bool:
        return False

    def list_source_records(self) -> tuple[FinanceSourceRecord, ...]:
        return (
            FinanceSourceRecord(
                source_id="source1",
                name="Source 1",
                base_url="https://source1.example",
                classification="primary",
                entitlement="test subscription",
                license_note="Links permitted.",
                source_version="v1",
                allowlist_version="finance-sources-v1",
                enabled=False,
                approved_at=None,
                health="attention",
                health_checked_at=NOW,
            ),
        )

    def list_run_filter_metadata(self, *, limit: int = 100) -> tuple[FinanceRunFilterMetadata, ...]:
        return (
            FinanceRunFilterMetadata(
                run_id=uuid4(),
                generated_at=NOW,
                status="attention",
                source_allowlist_version="finance-sources-v1",
                tickers=("ACME",),
                themes=("earnings",),
            ),
        )


def _app() -> FastAPI:
    app = FastAPI()
    app.state.finance_store = Store()
    app.include_router(router)
    return app


def test_finance_sources_are_read_only_gate_metadata() -> None:
    with TestClient(_app()) as client:
        response = client.get("/finance/sources")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["sources"][0]["source_id"] == "source1"
    assert body["sources"][0]["base_url"] == "https://source1.example"
    assert body["sources"][0]["entitlement"] == "test subscription"
    assert "raw" not in body["sources"][0]


def test_finance_run_filters_expose_ticker_theme_metadata() -> None:
    with TestClient(_app()) as client:
        response = client.get("/finance/runs/filters")
    assert response.status_code == 200
    body = response.json()
    assert body[0]["source_allowlist_version"] == "finance-sources-v1"
    assert body[0]["tickers"] == ["ACME"]
    assert body[0]["themes"] == ["earnings"]
