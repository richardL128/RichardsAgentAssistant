"""Phase 4 code-review operations.

This module contains the policy and orchestration that sits above the Phase 3
single-commit workflow.  Persistence, queueing, GitHub discovery, profile
ingestion and delivery are deliberately expressed as protocols: the API and
worker integrations can provide SQLAlchemy-backed implementations without
making the time-window and report rules depend on a particular session model.

Only identifiers, counts, status values and artifact keys leave an operation.
Review text is written to an :class:`~app.artifacts.store.ArtifactStore` and is
never put in queue return values.
"""

from __future__ import annotations

import inspect
import json
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

from app.artifacts.store import ArtifactStore
from app.core.redaction import redact_text
from app.db.code_review import review_idempotency_key

TORONTO = ZoneInfo("America/Toronto")
_SHA_CHARS = frozenset("0123456789abcdef")
_SEVERITIES = ("block", "important", "suggestion")
_TERMINAL = frozenset({"succeeded", "attention", "failed", "cancelled"})


@dataclass(frozen=True, slots=True)
class TorontoDayWindow:
    """The current Toronto calendar day represented by UTC instants."""

    local_date: date
    start_utc: datetime
    end_utc: datetime


def toronto_day_window(
    now: datetime | None = None, *, timezone: ZoneInfo = TORONTO
) -> TorontoDayWindow:
    """Return ``[Toronto midnight, now]`` with DST-correct UTC boundaries.

    Constructing midnight in the IANA zone and converting it to UTC is
    important: subtracting 24 hours from ``now`` is wrong on both DST
    transition days.  ``now`` may be in any aware timezone; returned bounds
    are always aware UTC values suitable for a database query.
    """

    instant = now or datetime.now(UTC)
    _require_aware(instant, "now")
    instant_utc = instant.astimezone(UTC)
    local = instant_utc.astimezone(timezone)
    midnight = datetime.combine(local.date(), time.min, tzinfo=timezone)
    return TorontoDayWindow(
        local_date=local.date(),
        start_utc=midnight.astimezone(UTC),
        end_utc=instant_utc,
    )


def toronto_day_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Compatibility helper returning the UTC start/end pair."""

    window = toronto_day_window(now)
    return window.start_utc, window.end_utc


def bounded_catchup_bounds(
    now: datetime | None = None,
    *,
    max_days: int = 3,
    timezone: ZoneInfo = TORONTO,
) -> tuple[datetime, datetime]:
    """Return a bounded window for late previous-day review catch-up.

    The current Toronto day is intentionally excluded because the nightly
    operation owns it.  ``max_days=0`` is a valid opt-out and yields an empty
    interval.  Calendar arithmetic occurs before conversion to UTC so DST
    days retain their actual 23/25-hour lengths.
    """

    if max_days < 0:
        raise ValueError("max_days must not be negative")
    window = toronto_day_window(now, timezone=timezone)
    end_local = datetime.combine(window.local_date, time.min, tzinfo=timezone)
    start_local = end_local - timedelta(days=max_days)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def select_catchup_commits(
    commits: Iterable[ReviewCommitRecord | Mapping[str, Any] | Any],
    *,
    now: datetime | None = None,
    max_days: int = 3,
    enabled: bool = False,
    limit: int | None = None,
) -> tuple[ReviewCommitRecord | Mapping[str, Any] | Any, ...]:
    """Select only late pushes in the explicitly bounded catch-up window."""

    if limit is not None and limit <= 0:
        raise ValueError("limit must be positive when provided")
    if not enabled or max_days == 0:
        return ()
    start, end = bounded_catchup_bounds(now, max_days=max_days)
    selected = [record for record in commits if start <= _record_timestamp(record) < end]
    selected.sort(key=lambda record: (_record_timestamp(record), str(_get(record, "head_sha", ""))))
    return tuple(selected[:limit] if limit is not None else selected)


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ReviewCommitRecord:
    """Minimal commit metadata needed by account-scale operations."""

    repository: str
    head_sha: str
    base_sha: str
    repository_id: uuid.UUID | str | None = None
    reviewed_commit_id: uuid.UUID | str | None = None
    occurred_at: datetime | None = None
    status: str = "succeeded"
    risk: str = "medium"
    trigger: str = "push"

    def __post_init__(self) -> None:
        _validate_sha(self.head_sha, "head_sha")
        _validate_sha(self.base_sha, "base_sha")
        if self.occurred_at is not None:
            _require_aware(self.occurred_at, "occurred_at")


def _record_timestamp(record: Any) -> datetime:
    for name in ("occurred_at", "received_at", "created_at", "committed_at"):
        value = (
            cast(Mapping[str, Any], record).get(name)
            if isinstance(record, Mapping)
            else getattr(record, name, None)
        )
        if isinstance(value, datetime):
            _require_aware(value, name)
            return value.astimezone(UTC)
    raise ValueError("commit record must provide an aware occurred_at or created_at")


def _get(record: Any, name: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return cast(Mapping[str, Any], record).get(name, default)
    return getattr(record, name, default)


def select_todays_commits(
    commits: Iterable[ReviewCommitRecord | Mapping[str, Any] | Any],
    *,
    now: datetime | None = None,
    limit: int | None = None,
) -> tuple[ReviewCommitRecord | Mapping[str, Any] | Any, ...]:
    """Select and deterministically order commits pushed since Toronto midnight.

    The lower bound is inclusive and the upper bound is inclusive, which is
    useful when a webhook and a nightly query observe the same instant.  The
    records are sorted by UTC event time and SHA, then bounded after sorting so
    retries return the same subset.
    """

    if limit is not None and limit <= 0:
        raise ValueError("limit must be positive when provided")
    window = toronto_day_window(now)
    selected = [
        record
        for record in commits
        if window.start_utc <= _record_timestamp(record) <= window.end_utc
    ]
    selected.sort(key=lambda record: (_record_timestamp(record), str(_get(record, "head_sha", ""))))
    return tuple(selected[:limit] if limit is not None else selected)


def _validate_sha(value: str, name: str) -> None:
    if len(value) not in (40, 64) or any(char not in _SHA_CHARS for char in value):
        raise ValueError(f"{name} must be a lowercase 40- or 64-character SHA")


class CodeReviewOperationsStore(Protocol):
    """Persistence seam for Phase 4 operations.

    Implementations should perform each mutation in the caller's transaction
    boundary and use the unique keys in the Phase 4 migration to collapse
    retries.  Methods return metadata records, never source or patch bodies.
    """

    def list_commits_between(
        self, start_utc: datetime, end_utc: datetime, *, limit: int
    ) -> Sequence[ReviewCommitRecord]: ...

    def list_findings_for_commits(
        self, reviewed_commit_ids: Sequence[uuid.UUID | str]
    ) -> Sequence[Mapping[str, Any] | Any]: ...

    def is_finding_dismissed(self, repository_id: uuid.UUID | str, fingerprint: str) -> bool: ...

    def get_daily_report(self, report_date: date) -> Mapping[str, Any] | Any | None: ...

    def create_daily_report(
        self,
        *,
        report_date: date,
        run_id: uuid.UUID,
        schedule_name: str,
    ) -> Mapping[str, Any] | Any: ...

    def finish_daily_report(
        self,
        report_id: uuid.UUID | str,
        *,
        status: str,
        commit_count: int,
        repository_count: int,
        finding_counts: Mapping[str, int],
        artifact_key: str,
        delivery_id: uuid.UUID | str | None,
        error_code: str | None = None,
    ) -> None: ...


class ReviewQueue(Protocol):
    """Queue seam used by high-risk webhook quick scans."""

    async def enqueue_review(
        self,
        *,
        repository: str,
        head_sha: str,
        base_sha: str,
        idempotency_key: str,
        trigger: str,
    ) -> object: ...


class DeliveryReceipt(Protocol):
    """Durable delivery-intent/receipt seam for consolidated reports."""

    async def deliver_report(
        self,
        *,
        report_date: date,
        artifact_key: str,
        idempotency_key: str,
    ) -> uuid.UUID | str | None: ...


@dataclass(frozen=True, slots=True)
class HarnessFinding:
    """Finding fields that a coding harness needs to propose a fix."""

    repository: str
    base_sha: str
    head_sha: str
    fingerprint: str
    severity: str
    path: str
    line: int
    title: str
    explanation: str
    reproduction_or_missing_test: str
    confidence: float
    assumptions: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()


def _finding_from_record(
    record: Mapping[str, Any] | Any, commit_by_id: Mapping[str, Any]
) -> HarnessFinding | None:
    commit_id = _get(record, "reviewed_commit_id")
    commit = commit_by_id.get(str(commit_id))
    if commit is None:
        return None
    values = {
        name: _get(record, name)
        for name in (
            "fingerprint",
            "severity",
            "path",
            "line",
            "title",
            "explanation",
            "reproduction_or_missing_test",
            "confidence",
            "assumptions",
            "evidence_refs",
        )
    }
    required = (
        "fingerprint",
        "severity",
        "path",
        "line",
        "title",
        "explanation",
        "reproduction_or_missing_test",
        "confidence",
    )
    if any(values[name] is None for name in required):
        return None
    return HarnessFinding(
        repository=str(_get(commit, "repository", "")),
        base_sha=str(_get(commit, "base_sha", "")),
        head_sha=str(_get(commit, "head_sha", "")),
        fingerprint=str(values["fingerprint"]),
        severity=str(values["severity"]),
        path=str(values["path"]),
        line=int(values["line"]),
        title=str(values["title"]),
        explanation=str(values["explanation"]),
        reproduction_or_missing_test=str(values["reproduction_or_missing_test"]),
        confidence=float(values["confidence"]),
        assumptions=tuple(str(value) for value in (values["assumptions"] or ())),
        evidence_refs=tuple(str(value) for value in (values["evidence_refs"] or ())),
    )


def actionable_findings(
    findings: Iterable[Mapping[str, Any] | Any],
    *,
    commits: Sequence[ReviewCommitRecord | Mapping[str, Any] | Any],
    is_dismissed: Callable[[uuid.UUID | str, str], bool] | None = None,
) -> tuple[HarnessFinding, ...]:
    """Return only actionable, non-dismissed findings in stable order."""

    commit_by_id = {
        str(_get(commit, "reviewed_commit_id")): commit
        for commit in commits
        if _get(commit, "reviewed_commit_id") is not None
    }
    result: list[HarnessFinding] = []
    for record in findings:
        if bool(_get(record, "dismissed", False)) or _get(record, "is_actionable", True) is False:
            continue
        fingerprint = str(_get(record, "fingerprint", ""))
        repository_id = _get(record, "repository_id")
        if repository_id is None:
            commit_id = _get(record, "reviewed_commit_id")
            commit = commit_by_id.get(str(commit_id))
            repository_id = _get(commit, "repository_id") if commit is not None else None
        if (
            is_dismissed is not None
            and repository_id is not None
            and is_dismissed(repository_id, fingerprint)
        ):
            continue
        finding = _finding_from_record(record, commit_by_id)
        if finding is not None:
            result.append(finding)
    result.sort(
        key=lambda item: (
            item.repository,
            item.head_sha,
            item.path,
            item.line,
            item.fingerprint,
        )
    )
    return tuple(result)


def _safe_text(value: object, limit: int = 2_000) -> str:
    return redact_text(str(value)).replace("\x00", "").strip()[:limit]


def render_harness_report(
    findings: Iterable[HarnessFinding],
    *,
    report_date: date,
    format: str = "markdown",  # noqa: A002 - public format parameter
) -> str:
    """Render a bounded report that gives a harness exact fix locations."""

    ordered = tuple(findings)
    payload = {
        "report_date": report_date.isoformat(),
        "finding_count": len(ordered),
        "findings": [
            {
                "repository": item.repository,
                "base_sha": item.base_sha,
                "head_sha": item.head_sha,
                "fingerprint": item.fingerprint,
                "severity": item.severity,
                "location": {"path": item.path, "line": item.line},
                "title": _safe_text(item.title, 300),
                "explanation": _safe_text(item.explanation),
                "reproduction_or_missing_test": _safe_text(item.reproduction_or_missing_test),
                "confidence": item.confidence,
                "assumptions": [_safe_text(value, 500) for value in item.assumptions],
                "evidence_refs": list(item.evidence_refs),
            }
            for item in ordered
        ],
    }
    if format == "json":
        return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if format != "markdown":
        raise ValueError("report format must be 'markdown' or 'json'")
    lines = [
        "# Daily code review report",
        "",
        f"- Report date: `{report_date.isoformat()}`",
        f"- Actionable findings: `{len(ordered)}`",
        "",
    ]
    if not ordered:
        lines.extend(["No actionable findings.", ""])
        return "\n".join(lines)
    for index, item in enumerate(ordered, start=1):
        lines.extend(
            [
                f"## {index}. [{_safe_text(item.severity, 32).upper()}] "
                f"{_safe_text(item.title, 300)}",
                "",
                f"- Repository: `{_safe_text(item.repository, 201)}`",
                f"- Location: `{_safe_text(item.path, 500)}:{item.line}`",
                f"- Head SHA: `{_safe_text(item.head_sha, 64)}`",
                f"- Fingerprint: `{_safe_text(item.fingerprint, 128)}`",
                f"- Confidence: `{item.confidence:.2f}`",
                f"- Consequence: {_safe_text(item.explanation)}",
                f"- Reproduction or missing test: {_safe_text(item.reproduction_or_missing_test)}",
                "",
            ]
        )
    return "\n".join(lines)


def finding_counts(findings: Iterable[HarnessFinding]) -> dict[str, int]:
    """Return the allowlisted finding taxonomy used by delivery summaries."""

    counts = dict.fromkeys(_SEVERITIES, 0)
    for finding in findings:
        if finding.severity in counts:
            counts[finding.severity] += 1
    return {str(key): value for key, value in counts.items()}


@dataclass(frozen=True, slots=True)
class DailyConsolidationResult:
    report_date: date
    status: str
    commit_count: int
    repository_count: int
    finding_counts: Mapping[str, int]
    report_artifact_key: str | None
    delivery_id: uuid.UUID | str | None

    def as_safe_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "report_date": self.report_date.isoformat(),
            "commit_count": self.commit_count,
            "repository_count": self.repository_count,
            "finding_counts": dict(self.finding_counts),
            "report_artifact_key": self.report_artifact_key,
            "delivery_id": str(self.delivery_id) if self.delivery_id is not None else None,
        }


async def consolidate_daily_reviews(
    *,
    store: CodeReviewOperationsStore,
    artifact_store: ArtifactStore,
    now: datetime | None = None,
    report_format: str = "markdown",
    max_commits: int = 50,
    catchup_enabled: bool = False,
    catchup_max_days: int = 3,
    run_id: uuid.UUID | None = None,
    schedule_name: str = "code-review-daily",
    delivery: DeliveryReceipt | None = None,
) -> DailyConsolidationResult:
    """Build and persist one Toronto-day report, idempotently.

    A report row already in a terminal state is returned without generating a
    new artifact or delivery intent.  The caller-supplied delivery adapter is
    responsible for opening an intent before its provider call and storing the
    provider receipt durably.
    """

    if max_commits <= 0:
        raise ValueError("max_commits must be positive")
    window = toronto_day_window(now)
    existing = store.get_daily_report(window.local_date)
    if existing is not None and str(_get(existing, "status", "")) in _TERMINAL:
        return DailyConsolidationResult(
            report_date=window.local_date,
            status=str(_get(existing, "status")),
            commit_count=int(_get(existing, "commit_count", 0)),
            repository_count=int(_get(existing, "repository_count", 0)),
            finding_counts=dict(_get(existing, "finding_counts", {})),
            report_artifact_key=_get(existing, "artifact_key"),
            delivery_id=_get(existing, "delivery_id"),
        )
    query_start = window.start_utc
    if catchup_enabled and catchup_max_days > 0:
        query_start, _ = bounded_catchup_bounds(window.end_utc, max_days=catchup_max_days)
    candidates = tuple(store.list_commits_between(query_start, window.end_utc, limit=max_commits))
    catchup = select_catchup_commits(
        candidates,
        now=window.end_utc,
        max_days=catchup_max_days,
        enabled=catchup_enabled,
        limit=max_commits,
    )
    today = select_todays_commits(candidates, now=window.end_utc, limit=max_commits)
    commits = (*catchup, *today)[:max_commits]
    commit_ids = [
        value for commit in commits if (value := _get(commit, "reviewed_commit_id")) is not None
    ]
    records = store.list_findings_for_commits(commit_ids)
    findings = actionable_findings(
        records,
        commits=commits,
        is_dismissed=store.is_finding_dismissed,
    )
    report_text = render_harness_report(
        findings, report_date=window.local_date, format=report_format
    )
    artifact = artifact_store.put(
        report_text,
        media_type="application/json" if report_format == "json" else "text/markdown",
        data_class="code_review_daily_report",
    )
    report = existing or store.create_daily_report(
        report_date=window.local_date,
        run_id=run_id or uuid.uuid4(),
        schedule_name=schedule_name,
    )
    delivery_id: uuid.UUID | str | None = None
    if delivery is not None:
        key = f"code-review-daily:{window.local_date.isoformat()}:v1"
        delivery_id = await delivery.deliver_report(
            report_date=window.local_date,
            artifact_key=artifact.key,
            idempotency_key=key,
        )
    counts = finding_counts(findings)
    repository_count = len({str(_get(commit, "repository", "")) for commit in commits})
    store.finish_daily_report(
        _get(report, "id"),
        status="succeeded",
        commit_count=len(commits),
        repository_count=repository_count,
        finding_counts=counts,
        artifact_key=artifact.key,
        delivery_id=delivery_id,
    )
    return DailyConsolidationResult(
        report_date=window.local_date,
        status="succeeded",
        commit_count=len(commits),
        repository_count=repository_count,
        finding_counts=counts,
        report_artifact_key=artifact.key,
        delivery_id=delivery_id,
    )


class DailyReportDelivery(Protocol):
    """Delivery integration for a report artifact.

    Implementations must create the durable delivery intent before contacting
    the provider and persist the provider receipt (including uncertain sends)
    before returning its delivery ID.
    """

    async def deliver_report(
        self,
        *,
        report_date: date,
        artifact_key: str,
        idempotency_key: str,
    ) -> uuid.UUID | str | None: ...


class _StoreDelivery:
    """Adapt a store's optional delivery method to :class:`DailyReportDelivery`."""

    def __init__(self, store: object) -> None:
        method = getattr(store, "deliver_daily_report", None)
        if not callable(method):
            raise RuntimeError("daily report delivery integration is not configured")
        self._method = method

    async def deliver_report(
        self,
        *,
        report_date: date,
        artifact_key: str,
        idempotency_key: str,
    ) -> uuid.UUID | str | None:
        result = self._method(
            report_date=report_date,
            artifact_key=artifact_key,
            idempotency_key=idempotency_key,
        )
        if inspect.isawaitable(result):
            return cast(uuid.UUID | str | None, await result)
        return cast(uuid.UUID | str | None, result)


async def run_code_review_daily(
    run_id: str,
    idempotency_key: str,
    *,
    settings: Any | None = None,
    engine: Any | None = None,
    store: CodeReviewOperationsStore | None = None,
    artifact_store: ArtifactStore | None = None,
    delivery: DailyReportDelivery | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Run the durable nightly consolidation with injectable integrations.

    Worker startup may use this function directly: when no store is supplied,
    it lazily constructs the SQLAlchemy adapter supplied by the host.  Tests
    and alternate deployments inject all seams, avoiding database or network
    access.  A delivery integration is mandatory; reports without a durable
    receipt are not considered complete.
    """

    if not run_id.strip() or not idempotency_key.strip():
        raise ValueError("run_id and idempotency_key must not be empty")
    resolved_settings = settings
    if resolved_settings is None:
        from app.core.config import get_settings

        resolved_settings = get_settings()
    resolved_engine = engine
    if store is None:
        if resolved_engine is None:
            from app.db.session import Database

            resolved_engine = Database(resolved_settings).engine
        try:
            from app.db.code_review import SQLAlchemyCodeReviewOperationsStore

            store = SQLAlchemyCodeReviewOperationsStore(resolved_engine)
        except ImportError:
            raise RuntimeError("SQLAlchemy code-review operations store is unavailable") from None
    resolved_artifacts = artifact_store
    if resolved_artifacts is None:
        resolved_artifacts = ArtifactStore(resolved_settings.artifact_root)
    resolved_delivery = delivery or _StoreDelivery(store)
    result = await consolidate_daily_reviews(
        store=store,
        artifact_store=resolved_artifacts,
        now=now,
        report_format="markdown",
        max_commits=resolved_settings.code_review_daily_max_commits,
        catchup_enabled=resolved_settings.code_review_catchup_enabled,
        catchup_max_days=resolved_settings.code_review_catchup_max_days,
        run_id=uuid.UUID(run_id),
        schedule_name="code-review-daily",
        delivery=resolved_delivery,
    )
    safe = result.as_safe_dict()
    safe.update({"run_id": run_id, "idempotency_key": idempotency_key})
    return safe


@dataclass(frozen=True, slots=True)
class QuickScanEvent:
    repository: str
    base_sha: str
    head_sha: str
    risk: str


async def route_high_risk_quick_scan(
    event: QuickScanEvent | Mapping[str, Any] | Any,
    *,
    queue: ReviewQueue,
    enabled: bool = True,
) -> dict[str, object]:
    """Enqueue exactly one idempotent quick scan for a high-risk push."""

    if not enabled:
        return {"status": "disabled"}
    if str(_get(event, "risk", "")).lower() != "high":
        return {"status": "ignored", "reason": "not_high_risk"}
    repository = str(_get(event, "repository", ""))
    head_sha = str(_get(event, "head_sha", _get(event, "after_sha", "")))
    base_sha = str(_get(event, "base_sha", _get(event, "before_sha", "")))
    _validate_sha(head_sha, "head_sha")
    _validate_sha(base_sha, "base_sha")
    key = review_idempotency_key(repository, head_sha)
    queued = await queue.enqueue_review(
        repository=repository,
        head_sha=head_sha,
        base_sha=base_sha,
        idempotency_key=key,
        trigger="quick_scan",
    )
    return {
        "status": "enqueued",
        "repository": repository,
        "head_sha": head_sha,
        "idempotency_key": key,
        "queue_id": str(queued),
    }


__all__ = [
    "CodeReviewOperationsStore",
    "DailyConsolidationResult",
    "DailyReportDelivery",
    "DeliveryReceipt",
    "HarnessFinding",
    "QuickScanEvent",
    "ReviewCommitRecord",
    "ReviewQueue",
    "TorontoDayWindow",
    "actionable_findings",
    "bounded_catchup_bounds",
    "consolidate_daily_reviews",
    "finding_counts",
    "render_harness_report",
    "route_high_risk_quick_scan",
    "run_code_review_daily",
    "select_catchup_commits",
    "select_todays_commits",
    "toronto_day_bounds",
    "toronto_day_window",
]
