from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.agents.finance.contracts import BriefingPayload, EventCard, ExposureMapping, ImpactLabel
from app.db.finance import FinanceRepository, SQLAlchemyFinanceStore
from app.db.models import AuditEvent, Base

ALLOWLIST = "finance-sources-v1"
NOW = datetime(2026, 9, 4, 13, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path):
    import app.db.finance  # noqa: F401

    created = create_engine(f"sqlite+pysqlite:///{tmp_path / 'finance.db'}")
    Base.metadata.create_all(created)
    try:
        yield created
    finally:
        created.dispose()


def _audit_id(session: Session):
    audit = AuditEvent(
        actor="test",
        action="approve_finance_source",
        target_type="finance_source",
        target_id="source",
        result="approved",
    )
    session.add(audit)
    session.flush()
    return audit.id


def test_source_records_gate_and_read_only_metadata(engine) -> None:
    with Session(engine) as session, session.begin():
        audit_id = _audit_id(session)
        for index in range(1, 9):
            FinanceRepository.upsert_approved_source(
                session,
                source_id=f"source{index}",
                name=f"Source {index}",
                base_url=f"https://source{index}.example",
                source_version="v1",
                allowlist_version=ALLOWLIST,
                license_note="Links permitted.",
                entitlement="test subscription",
                classification="primary" if index == 1 else "reported",
                license_allows_excerpt=index != 1,
                excerpt_max_chars=200 if index == 2 else None,
                enabled=True,
                approved_at=NOW,
                approval_audit_id=audit_id,
            )
        FinanceRepository.record_source_health(
            session,
            source_id="source1",
            source_version="v1",
            status="healthy",
            checked_at=NOW,
            diagnostic="ok",
        )

    store = SQLAlchemyFinanceStore(engine, allowlist_version=ALLOWLIST)
    assert store.source_approval_gate() is True
    records = store.list_source_records()
    assert len(records) == 8
    assert records[0].base_url == "https://source1.example"
    assert records[0].classification == "primary"
    assert records[0].entitlement == "test subscription"
    assert records[0].license_allows_excerpt is False
    assert records[1].license_allows_excerpt is True
    assert records[1].excerpt_max_chars == 200
    assert records[0].health == "healthy"

    approvals = store.load_approved_sources(allowlist_version=ALLOWLIST)
    assert approvals[0].classification.value == "primary"
    assert approvals[1].license_allows_excerpt is True
    assert approvals[1].excerpt_max_chars == 200


def test_source_gate_remains_disabled_without_audit_approval(engine) -> None:
    with Session(engine) as session, session.begin():
        for index in range(1, 9):
            FinanceRepository.upsert_approved_source(
                session,
                source_id=f"source{index}",
                name=f"Source {index}",
                base_url=f"https://source{index}.example",
                source_version="v1",
                allowlist_version=ALLOWLIST,
                license_note="Links permitted.",
                entitlement="test subscription",
                enabled=True,
                approved_at=NOW,
                approval_audit_id=None,
            )

    store = SQLAlchemyFinanceStore(engine, allowlist_version=ALLOWLIST)
    assert store.source_approval_gate() is False


def test_portfolio_snapshot_briefing_payload_and_run_filter_metadata(engine) -> None:
    run_id = uuid4()
    with Session(engine) as session, session.begin():
        thesis = FinanceRepository.upsert_thesis(
            session,
            symbol="ACME",
            version="v1",
            title="ACME margin expansion",
            thesis_summary="Track whether margins expand with revenue.",
        )
        thesis_id = thesis.id
        FinanceRepository.upsert_holding(
            session,
            symbol="acme",
            name="ACME Corp",
            quantity=2,
            market_value=200,
            tags=("industrial",),
        )
        FinanceRepository.upsert_watchlist_entry(
            session,
            symbol="acme",
            name="ACME Corp",
            thesis_id=thesis.id,
            themes=("earnings",),
        )
        FinanceRepository.upsert_etf_exposure(
            session,
            etf_symbol="ETF",
            underlying_symbol="ACME",
            weight_percent=4.5,
            source_id="source1",
            as_of=date(2026, 9, 4),
        )
        payload = BriefingPayload(
            run_id=run_id,
            source_allowlist_version=ALLOWLIST,
            generated_at=NOW,
            status="succeeded",
            tickers=("ACME",),
            themes=("earnings",),
            cards=(
                EventCard(
                    event_id="event-1",
                    title="ACME reports quarterly revenue growth",
                    verified_facts=("ACME reported revenue growth.",),
                    uncertainty="Future demand is uncertain.",
                    counter_case="Growth could normalize.",
                    impact_label=ImpactLabel.MONITOR,
                    exposure=ExposureMapping(event_id="event-1", holding_symbols=("ACME",)),
                    citations=("source1",),
                ),
            ),
        )
        FinanceRepository.save_briefing_payload(session, payload)

    store = SQLAlchemyFinanceStore(engine, allowlist_version=ALLOWLIST)
    snapshot = store.load_portfolio_snapshot()
    assert snapshot.holdings[0].symbol == "ACME"
    assert snapshot.watchlist[0].thesis_id == thesis_id
    assert snapshot.etf_exposures[0].weight_percent == 4.5
    filters = store.list_run_filter_metadata()
    assert filters[0].run_id == run_id
    assert filters[0].tickers == ("ACME",)
    assert filters[0].themes == ("earnings",)
