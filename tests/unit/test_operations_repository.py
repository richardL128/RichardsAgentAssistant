from __future__ import annotations

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
