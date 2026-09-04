from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.agents.code_review.operations import (
    QuickScanEvent,
    ReviewCommitRecord,
    actionable_findings,
    bounded_catchup_bounds,
    consolidate_daily_reviews,
    render_harness_report,
    route_high_risk_quick_scan,
    select_catchup_commits,
    select_todays_commits,
    toronto_day_window,
)
from app.artifacts.store import ArtifactStore

SHA = "a" * 40
BASE = "b" * 40


def test_toronto_day_window_uses_dst_correct_midnight() -> None:
    spring = toronto_day_window(datetime(2025, 3, 9, 6, 30, tzinfo=UTC))
    fall = toronto_day_window(datetime(2025, 11, 2, 6, 30, tzinfo=UTC))

    assert spring.start_utc == datetime(2025, 3, 9, 5, tzinfo=UTC)
    assert spring.end_utc == datetime(2025, 3, 9, 6, 30, tzinfo=UTC)
    assert fall.start_utc == datetime(2025, 11, 2, 4, tzinfo=UTC)


def test_select_todays_commits_excludes_prior_toronto_date() -> None:
    now = datetime(2026, 9, 3, 16, tzinfo=UTC)
    records = [
        ReviewCommitRecord("acme/app", SHA, BASE, occurred_at=datetime(2026, 9, 3, 15, tzinfo=UTC)),
        ReviewCommitRecord(
            "acme/app", "c" * 40, BASE, occurred_at=datetime(2026, 9, 3, 3, 59, tzinfo=UTC)
        ),
    ]

    selected = select_todays_commits(records, now=now)
    assert selected == (records[0],)


def test_catchup_policy_is_disabled_or_calendar_bounded() -> None:
    now = datetime(2026, 9, 3, 16, tzinfo=UTC)
    assert select_catchup_commits([], now=now, enabled=False) == ()
    start, end = bounded_catchup_bounds(now, max_days=2)
    assert start == datetime(2026, 9, 1, 4, tzinfo=UTC)
    assert end == datetime(2026, 9, 3, 4, tzinfo=UTC)


def test_harness_report_contains_actionable_records_only() -> None:
    commit_id = uuid4()
    commit = ReviewCommitRecord(
        "acme/app", SHA, BASE, reviewed_commit_id=commit_id, occurred_at=datetime.now(UTC)
    )
    finding = {
        "reviewed_commit_id": commit_id,
        "fingerprint": "d" * 64,
        "severity": "important",
        "path": "app/auth.py",
        "line": 42,
        "title": "Missing authorization",
        "explanation": "Require authorization before deleting the account.",
        "reproduction_or_missing_test": "Add a request without the owner role.",
        "confidence": 0.94,
        "dismissed": False,
    }
    dismissed = dict(finding, fingerprint="e" * 64, dismissed=True, title="Do not show")

    actionable = actionable_findings([finding, dismissed], commits=[commit])
    report = render_harness_report(actionable, report_date=date(2026, 9, 3))
    assert "Missing authorization" in report
    assert "Do not show" not in report
    assert "app/auth.py:42" in report


def test_harness_report_redacts_credential_shaped_finding_text() -> None:
    finding = actionable_findings(
        [
            {
                "reviewed_commit_id": "commit-1",
                "fingerprint": "d" * 64,
                "severity": "block",
                "path": "app/config.py",
                "line": 7,
                "title": "Hard-coded access_token = super-secret-token",
                "explanation": "The configured password: hunter2 reaches the daily report.",
                "reproduction_or_missing_test": "Add a test asserting api_key = abc123 is omitted.",
                "confidence": 0.98,
            }
        ],
        commits=[
            {
                "repository": "acme/app",
                "base_sha": BASE,
                "head_sha": SHA,
                "reviewed_commit_id": "commit-1",
            }
        ],
    )

    report = render_harness_report(finding, report_date=date(2026, 9, 3))

    assert "super-secret-token" not in report
    assert "hunter2" not in report
    assert "abc123" not in report
    assert "access_token = [REDACTED]" in report
    assert "password: [REDACTED]" in report
    assert "api_key = [REDACTED]" in report


class _Store:
    def __init__(self, commit: ReviewCommitRecord, finding: dict[str, object]) -> None:
        self.commit = commit
        self.finding = finding
        self.report: dict[str, object] | None = None

    def list_commits_between(self, start: datetime, end: datetime, *, limit: int):
        return [self.commit]

    def list_findings_for_commits(self, ids):
        return [self.finding]

    def is_finding_dismissed(self, repository_id, fingerprint):
        return False

    def get_daily_report(self, report_date):
        return self.report

    def create_daily_report(self, *, report_date, run_id, schedule_name):
        self.report = {"id": uuid4(), "status": "running"}
        return self.report

    def finish_daily_report(self, report_id, **values):
        assert self.report is not None
        self.report.update(values)
        self.report["status"] = values["status"]


class _Delivery:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def deliver_report(self, **values):
        self.calls.append(values)
        return UUID("11111111-1111-1111-1111-111111111111")


@pytest.mark.asyncio
async def test_daily_consolidation_persists_artifact_and_delivery_receipt(tmp_path: Path) -> None:
    commit_id = uuid4()
    now = datetime(2026, 9, 3, 16, tzinfo=UTC)
    commit = ReviewCommitRecord(
        "acme/app", SHA, BASE, reviewed_commit_id=commit_id, occurred_at=now
    )
    finding = {
        "reviewed_commit_id": commit_id,
        "fingerprint": "d" * 64,
        "severity": "block",
        "path": "app/auth.py",
        "line": 42,
        "title": "Missing authorization",
        "explanation": "Require authorization.",
        "reproduction_or_missing_test": "Add an authorization test.",
        "confidence": 0.94,
    }
    store = _Store(commit, finding)
    delivery = _Delivery()
    result = await consolidate_daily_reviews(
        store=store,
        artifact_store=ArtifactStore(tmp_path),
        now=now,
        delivery=delivery,
    )

    assert result.status == "succeeded"
    assert result.report_artifact_key is not None
    assert result.delivery_id == UUID("11111111-1111-1111-1111-111111111111")
    assert len(delivery.calls) == 1
    assert store.report is not None
    assert store.report["delivery_id"] == result.delivery_id


class _Queue:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def enqueue_review(self, **values):
        self.calls.append(values)
        return "job-1"


@pytest.mark.asyncio
async def test_high_risk_quick_scan_uses_exact_sha_idempotency_key() -> None:
    queue = _Queue()
    result = await route_high_risk_quick_scan(
        QuickScanEvent("acme/app", BASE, SHA, "high"), queue=queue
    )

    assert result["status"] == "enqueued"
    assert queue.calls[0]["idempotency_key"] == f"code-review:acme/app:{SHA}"
    assert queue.calls[0]["trigger"] == "quick_scan"
