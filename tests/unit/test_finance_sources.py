from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.agents.finance.contracts import SourceApproval, SourceDocument, SourceFetchResult
from app.connectors.finance_sources import SourceGateError, build_source_queries
from app.connectors.finance_sources.adapters import (
    EXACT_SOURCE_COUNT,
    check_source_gate,
    fetch_exactly_eight_sources,
)

NOW = datetime(2026, 9, 4, 13, tzinfo=UTC)
ALLOWLIST = "finance-sources-v1"


def approvals(
    *, count: int = EXACT_SOURCE_COUNT, approved: bool = True
) -> tuple[SourceApproval, ...]:
    audit_id = uuid4() if approved else None
    approved_at = NOW if approved else None
    return tuple(
        SourceApproval(
            source_id=f"source{i}",
            name=f"Source {i}",
            base_url=f"https://source{i}.example",
            source_version="v1",
            allowlist_version=ALLOWLIST,
            license_note="Links and short excerpts permitted by test licence.",
            entitlement="test subscription",
            enabled=True,
            approved_at=approved_at,
            approval_audit_id=audit_id,
        )
        for i in range(1, count + 1)
    )


class Adapter:
    def __init__(self, source_id: str, *, fail: bool = False) -> None:
        self.source_id = source_id
        self.fail = fail
        self.calls = 0

    async def fetch(self, query):
        self.calls += 1
        if self.fail:
            raise TimeoutError
        return SourceFetchResult(
            source_id=query.source_id,
            documents=(
                SourceDocument(
                    source_id=query.source_id,
                    source_version=query.source_version,
                    external_id="doc-1",
                    title="ACME reports quarterly revenue growth",
                    url=f"https://{query.source_id}.example/report",
                    published_at=NOW,
                    retrieved_at=NOW,
                    tickers=("ACME",),
                    themes=("earnings",),
                ),
            ),
        )


def test_source_gate_requires_exactly_eight_approved_sources() -> None:
    gated = check_source_gate(approvals(count=7), allowlist_version=ALLOWLIST)
    assert gated.enabled is False
    assert "exactly eight" in str(gated.diagnostic)

    unapproved = check_source_gate(
        approvals(count=EXACT_SOURCE_COUNT, approved=False), allowlist_version=ALLOWLIST
    )
    assert unapproved.enabled is False
    assert set(unapproved.statuses.values()) == {"missing_approval"}

    ready = check_source_gate(approvals(), allowlist_version=ALLOWLIST)
    assert ready.enabled is True


def test_query_builder_rejects_version_mismatch() -> None:
    source_records = approvals()
    source_records = (
        source_records[0].model_copy(update={"allowlist_version": "old"}),
        *source_records[1:],
    )
    with pytest.raises(SourceGateError, match="version mismatch"):
        build_source_queries(
            source_records,
            allowlist_version=ALLOWLIST,
            window_start=NOW,
            window_end=NOW.replace(hour=14),
            tickers=("ACME",),
            themes=("earnings",),
        )


@pytest.mark.asyncio
async def test_run_issues_exactly_eight_source_calls_and_reports_failure_without_fallback() -> None:
    source_records = approvals()
    adapters = {
        source.source_id: Adapter(source.source_id, fail=source.source_id == "source8")
        for source in source_records
    }
    results = await fetch_exactly_eight_sources(
        adapters=adapters,
        approved_sources=source_records,
        allowlist_version=ALLOWLIST,
        window_start=NOW,
        window_end=NOW.replace(hour=14),
        tickers=("ACME",),
        themes=("earnings",),
    )

    assert len(results) == EXACT_SOURCE_COUNT
    assert sum(adapter.calls for adapter in adapters.values()) == EXACT_SOURCE_COUNT
    assert [result.failure.source_id for result in results if result.failure] == ["source8"]
    assert all(
        "search" not in (result.failure.diagnostic if result.failure else "") for result in results
    )
