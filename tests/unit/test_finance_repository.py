from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents.finance.contracts import (
    BriefingPayload,
    ETFExposure,
    EventCard,
    ExposureMapping,
    ImpactLabel,
)
from app.db.finance import (
    FinanceRepository,
    FinanceSourceEndpoint,
    FinanceSourceRequestAudit,
    SQLAlchemyFinanceStore,
)
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
            source_url="https://issuer.example/holdings.csv",
            retrieved_at=NOW,
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
    assert str(snapshot.etf_exposures[0].source_url) == "https://issuer.example/holdings.csv"
    assert snapshot.etf_exposures[0].retrieved_at == NOW
    filters = store.list_run_filter_metadata()
    assert filters[0].run_id == run_id
    assert filters[0].tickers == ("ACME",)
    assert filters[0].themes == ("earnings",)


def test_bulk_etf_exposure_upsert_preserves_source_metadata(engine) -> None:
    store = SQLAlchemyFinanceStore(engine, allowlist_version=ALLOWLIST)
    store.upsert_etf_exposures(
        (
            ETFExposure(
                etf_symbol="IVV",
                underlying_symbol="LMT",
                weight_percent=1.25,
                source_id="issuer_etf_holdings",
                as_of=date(2026, 9, 7),
                source_url="https://www.ishares.com/holdings.csv",
                retrieved_at=NOW,
            ),
        )
    )

    snapshot = store.load_portfolio_snapshot()
    assert len(snapshot.etf_exposures) == 1
    exposure = snapshot.etf_exposures[0]
    assert exposure.etf_symbol == "IVV"
    assert exposure.underlying_symbol == "LMT"
    assert str(exposure.source_url) == "https://www.ishares.com/holdings.csv"
    assert exposure.retrieved_at == NOW


def test_source_endpoints_and_cache_state_round_trip(engine) -> None:
    with Session(engine) as session, session.begin():
        FinanceRepository.upsert_approved_source(
            session,
            source_id="sec_edgar",
            name="SEC EDGAR",
            base_url="https://data.sec.gov/submissions/",
            source_version="sec-edgar-public-v1",
            allowlist_version=ALLOWLIST,
            license_note="Public SEC filing metadata.",
            entitlement="Public SEC access with descriptive user agent.",
            classification="primary",
        )
        FinanceRepository.upsert_source_endpoint(
            session,
            allowlist_version=ALLOWLIST,
            source_id="sec_edgar",
            endpoint_id="sec_submissions",
            base_url="https://data.sec.gov/submissions/",
            host="data.sec.gov",
            transport_kind="json_http",
            parser_kind="json",
            registry_version="sec-edgar-public-v1",
            expected_freshness_seconds=600,
            request_ceiling=10,
            license_note="Public SEC submissions endpoint.",
            retention_note="Retain normalized filing metadata and cache validators.",
            cik_scope=("0000936468",),
            ticker_scope=("LMT",),
        )
        FinanceRepository.save_source_cache_state(
            session,
            allowlist_version=ALLOWLIST,
            source_id="sec_edgar",
            endpoint_id="sec_submissions",
            etag='"abc"',
            last_modified="Mon, 07 Sep 2026 12:00:00 GMT",
            watermark_external_id="0000936468-26-000001",
            watermark_published_at=NOW,
            cached_artifact_key="finance/sec/submissions.json",
            payload_sha256="a" * 64,
            last_retrieved_at=NOW,
        )

    store = SQLAlchemyFinanceStore(engine, allowlist_version=ALLOWLIST)
    endpoints = store.list_source_endpoints(source_id="sec_edgar")
    assert len(endpoints) == 1
    assert endpoints[0].host == "data.sec.gov"
    assert endpoints[0].request_ceiling == 10
    assert endpoints[0].cik_scope == ("0000936468",)
    assert endpoints[0].ticker_scope == ("LMT",)

    state = store.load_source_cache_state(source_id="sec_edgar", endpoint_id="sec_submissions")
    assert state is not None
    assert state.etag == '"abc"'
    assert state.watermark_external_id == "0000936468-26-000001"
    assert state.watermark_published_at == NOW
    assert state.last_retrieved_at == NOW

    store.save_source_cache_state(
        source_id="sec_edgar",
        endpoint_id="sec_submissions",
        etag='"def"',
        last_not_modified_at=NOW,
    )
    updated = store.load_source_cache_state(
        source_id="sec_edgar", endpoint_id="sec_submissions"
    )
    assert updated is not None
    assert updated.etag == '"def"'
    assert updated.last_not_modified_at == NOW

    store.record_source_request_audit(
        source_id="sec_edgar",
        endpoint_id="sec_submissions",
        requested_at=NOW,
        status_code=304,
        error_code=None,
        not_modified=True,
    )
    with Session(engine) as session:
        audit = session.scalar(select(FinanceSourceRequestAudit))
        assert audit is not None
        assert audit.outcome == "not_modified"
        assert audit.status_code == 304


def test_source_endpoint_uniqueness(engine) -> None:
    values = {
        "allowlist_version": ALLOWLIST,
        "source_id": "defense_gov_rss",
        "endpoint_id": "defense_feed",
        "base_url": "https://www.defense.gov/feed",
        "host": "www.defense.gov",
        "transport_kind": "rss_atom",
        "parser_kind": "rss",
        "registry_version": "defense-gov-rss-v1",
        "expected_freshness_seconds": 600,
        "request_ceiling": 1,
        "license_note": "Official feed.",
        "retention_note": "Metadata and short excerpts only.",
    }
    with Session(engine) as session, session.begin():
        FinanceRepository.upsert_approved_source(
            session,
            source_id="defense_gov_rss",
            name="Defense.gov RSS",
            base_url="https://www.defense.gov/feed",
            source_version="defense-gov-rss-v1",
            allowlist_version=ALLOWLIST,
            license_note="Official feed.",
            entitlement="Public.",
            classification="primary",
        )
        FinanceRepository.upsert_source_endpoint(session, **values)

    duplicate = dict(values, id=uuid4())
    with pytest.raises(IntegrityError):
        _insert_source_endpoint_duplicate(engine, duplicate)


def _insert_source_endpoint_duplicate(engine, values: dict[str, object]) -> None:
    with Session(engine) as session, session.begin():
        session.execute(FinanceSourceEndpoint.__table__.insert().values(**values))
