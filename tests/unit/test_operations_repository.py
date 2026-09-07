from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.finance import FinanceRepository
from app.db.models import Base, HealthCheck, HealthState
from app.operations.repository import OperationsRepository


def test_source_settings_projects_the_disabled_allowlist_without_enabling_schedule(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'operations.db'}")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session, session.begin():
            for index in range(1, 9):
                FinanceRepository.upsert_approved_source(
                    session,
                    source_id=f"source{index}",
                    name=f"Source {index}",
                    base_url=f"https://source{index}.example/api",
                    source_version="v1",
                    allowlist_version="finance-sources-test",
                    license_note="Links only.",
                    entitlement="Test entitlement",
                    enabled=False,
                )
            FinanceRepository.upsert_source_endpoint(
                session,
                allowlist_version="finance-sources-test",
                source_id="source1",
                endpoint_id="source1-reviewed-feed",
                base_url="https://source1.example/feed?api_key=must-not-render",
                host="source1.example",
                transport_kind="rss_atom",
                parser_kind="rss",
                registry_version="test-registry-v1",
                enabled=True,
                expected_freshness_seconds=600,
                request_ceiling=1,
                license_note="Reviewed fixture feed.",
                retention_note="Retain metadata and short excerpts only.",
                excerpt_allowed=True,
                excerpt_max_chars=200,
                ticker_scope=("LMT",),
            )
            FinanceRepository.save_source_cache_state(
                session,
                allowlist_version="finance-sources-test",
                source_id="source1",
                endpoint_id="source1-reviewed-feed",
                watermark_external_id="fixture-1",
                watermark_published_at=datetime.now(UTC) - timedelta(minutes=5),
                last_retrieved_at=datetime.now(UTC) - timedelta(minutes=5),
            )
            FinanceRepository.record_source_health(
                session,
                source_id="source1",
                source_version="v1",
                status="healthy",
                checked_at=datetime.now(UTC),
            )
            FinanceRepository.record_source_request_audit(
                session,
                allowlist_version="finance-sources-test",
                source_id="source1",
                endpoint_id="source1-reviewed-feed",
                requested_at=datetime.now(UTC),
                status_code=429,
                error_code="connector_rate_limited",
                not_modified=False,
            )

        with Session(engine) as session:
            settings = OperationsRepository.source_settings(
                session,
                allowlist_version="finance-sources-test",
            )
    finally:
        engine.dispose()

    assert settings.allowlist_version == "finance-sources-test"
    assert settings.approval_complete is False
    assert settings.schedule_enabled is False
    assert len(settings.sources) == 8
    assert settings.sources[0].hostname == "source1.example"
    assert settings.sources[0].source_id == "source1"
    assert settings.sources[0].approved is False
    assert settings.sources[0].health == "healthy"
    assert settings.sources[0].endpoint_count == 1
    endpoint = settings.sources[0].endpoints[0]
    assert endpoint.endpoint_id == "source1-reviewed-feed"
    assert endpoint.host == "source1.example"
    assert endpoint.transport == "rss_atom"
    assert endpoint.expected_freshness_label == "10 minutes"
    assert endpoint.scope_label == "tickers: LMT"
    assert endpoint.health == "failed"
    assert "connector_rate_limited" in endpoint.diagnostic
    assert "api_key" not in endpoint.model_dump_json()
    assert "0 of 8" in settings.diagnostic


def test_health_cards_use_only_the_canonical_shared_services_check(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'health-cards.db'}")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session, session.begin():
            session.add_all(
                (
                    HealthCheck(
                        check_name="unrelated",
                        rule="fixture",
                        state=HealthState.FAILED,
                        diagnostic="must not become shared services",
                    ),
                    HealthCheck(
                        check_name="shared_services",
                        rule="fixture",
                        state=HealthState.HEALTHY,
                        diagnostic="shared services are healthy",
                    ),
                )
            )
        with Session(engine) as session:
            cards = OperationsRepository.health_cards(session)
    finally:
        engine.dispose()

    assert len(cards) == 4
    shared = next(card for card in cards if card.component == "shared_services")
    assert shared.state == "healthy"
    assert shared.diagnostic == "shared services are healthy"
