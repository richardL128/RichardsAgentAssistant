"""Compose the Phase 3 code-review modules into one durable run.

The workflow is a thin orchestrator: every analytical decision already lives in
the modules it calls.  It loads the queued commit, checks out the exact head
SHA, assembles a bounded diff packet, runs the isolated scanners, asks the
model for a structured proposal, validates and persists the findings, renders a
redacted report artifact, delivers a summary, and records a terminal review
status.  Only safe metadata leaves this module: the returned dict is logged by
Procrastinate and must never carry patch text, model text, scanner output, or
raw exception strings.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.agents.code_review.contracts import (
    FindingBatch,
    ReviewPacket,
    ScannerStatus,
    ValidatedFinding,
)
from app.agents.code_review.findings import validate_and_merge
from app.agents.code_review.packet import PacketAssemblyError, assemble_review_packet
from app.agents.code_review.report import render_markdown_report
from app.agents.code_review.repository import RepositoryCheckoutError, checkout_repository
from app.agents.code_review.scanners import ScannerSpec, run_scanners
from app.artifacts.store import ArtifactStore
from app.core.config import Settings, get_settings
from app.core.errors import LifeAgentError
from app.core.redaction import redact_text
from app.db.code_review import CodeReviewRepository, ReviewStatus
from app.db.models import CodeRepository
from app.db.session import Database
from app.llm.contracts import InvocationStatus
from app.llm.gateway import LLMGateway

_TERMINAL_STATUSES = frozenset({"succeeded", "attention", "failed", "cancelled"})
_DEGRADED_SCANNER_STATUSES = frozenset(
    {ScannerStatus.TIMED_OUT, ScannerStatus.UNAVAILABLE, ScannerStatus.FAILED}
)
_SEVERITIES = ("block", "important", "suggestion")
_REPORT_DATA_CLASS = "code_review_report"


class ReviewSummarySender(Protocol):
    """Delivery seam satisfied by ``app.connectors.discord.deliver_review_summary``.

    The concrete function is written by a sibling executor, so it is imported
    lazily (function-local) and injected in tests; this module never hard-imports
    it at module scope.
    """

    async def __call__(
        self,
        *,
        engine: Engine,
        run_id: uuid.UUID,
        channel_id: str,
        repository: str,
        head_sha: str,
        risk: str,
        status: str,
        finding_counts: Mapping[str, int],
        report_artifact_key: str | None,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class _RunContext:
    reviewed_commit_id: uuid.UUID
    repository: str
    base_sha: str
    head_sha: str
    terminal: bool
    status: str


@dataclass(frozen=True, slots=True)
class _PreparedReview:
    packet: ReviewPacket
    degraded: tuple[tuple[str, str, str], ...]


def _load_context(engine: Engine, run_id: uuid.UUID) -> _RunContext:
    with Session(engine) as session, session.begin():
        commit = CodeReviewRepository.get_commit_for_run(session, run_id)
        repository = session.get(CodeRepository, commit.repository_id)
        if repository is None:
            raise RuntimeError("reviewed commit references an unknown repository")
        full_name = repository.full_name
        base_sha = commit.base_sha
        head_sha = commit.head_sha
        started = CodeReviewRepository.start(session, run_id)
        return _RunContext(
            reviewed_commit_id=started.id,
            repository=full_name,
            base_sha=base_sha,
            head_sha=head_sha,
            terminal=started.status in _TERMINAL_STATUSES,
            status=str(started.status),
        )


def _checkout_and_prepare(
    *,
    repository: str,
    base_sha: str,
    head_sha: str,
    allowlist: Sequence[str],
    timeout_seconds: float,
    scanner_specs: Sequence[ScannerSpec],
    checkout_source: str | Path | None,
) -> _PreparedReview:
    with checkout_repository(
        repository,
        head_sha,
        allowlist=allowlist,
        source=checkout_source,
        timeout_seconds=timeout_seconds,
    ) as checkout:
        # Assemble once first so a malformed diff fails fast (PacketAssemblyError)
        # before any scanner time is spent, then rebuild with the scanner runs so
        # the model prompt sees them.
        assemble_review_packet(repository, base_sha, head_sha, checkout)
        scanner_runs = run_scanners(scanner_specs, checkout)
        packet = assemble_review_packet(
            repository,
            base_sha,
            head_sha,
            checkout,
            scanner_runs=scanner_runs,
        )
        degraded = tuple(
            (run.scanner.value, run.status.value, run.diagnostic_code)
            for run in scanner_runs
            if run.status in _DEGRADED_SCANNER_STATUSES
        )
    return _PreparedReview(packet=packet, degraded=degraded)


def _persist_findings(
    engine: Engine,
    *,
    reviewed_commit_id: uuid.UUID,
    findings: tuple[ValidatedFinding, ...],
) -> None:
    with Session(engine) as session, session.begin():
        CodeReviewRepository.persist_findings(
            session,
            reviewed_commit_id=reviewed_commit_id,
            findings=findings,
        )


def _write_report(
    store: ArtifactStore,
    *,
    repository: str,
    base_sha: str,
    head_sha: str,
    findings: tuple[ValidatedFinding, ...],
    summary: str,
) -> str:
    markdown = render_markdown_report(
        repository=repository,
        base_sha=base_sha,
        head_sha=head_sha,
        findings=findings,
        summary=summary,
    )
    metadata = store.put(
        markdown,
        media_type="text/markdown",
        data_class=_REPORT_DATA_CLASS,
    )
    return metadata.key


def _finish(
    engine: Engine,
    *,
    run_id: uuid.UUID,
    status: ReviewStatus,
    risk: str,
    summary: str,
    report_artifact_key: str | None,
    error_code: str | None,
) -> None:
    with Session(engine) as session, session.begin():
        CodeReviewRepository.finish(
            session,
            run_id=run_id,
            status=status,
            risk=risk,
            summary=summary,
            report_artifact_key=report_artifact_key,
            error_code=error_code,
        )


def _redact_prompt_value(value: Any) -> Any:
    """Redact packet strings before JSON escaping can obscure assignments."""

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_redact_prompt_value(item) for item in cast(list[Any], value)]
    if isinstance(value, dict):
        values = cast(dict[str, Any], value)
        return {key: _redact_prompt_value(item) for key, item in values.items()}
    return value


def _build_prompt(packet: ReviewPacket) -> str:
    safe_packet = _redact_prompt_value(packet.model_dump(mode="json"))
    facts = json.dumps(safe_packet, sort_keys=True, separators=(",", ":"))
    return (
        "Review the following code change. Use only the facts in this packet. "
        "Report actionable defects located on changed lines, and cite packet "
        "evidence identifiers for each finding.\n"
        f"Review packet:\n{facts}"
    )


def _finding_counts(findings: Sequence[ValidatedFinding]) -> dict[str, int]:
    counts: dict[str, int] = dict.fromkeys(_SEVERITIES, 0)
    for finding in findings:
        counts[finding.severity] += 1
    return counts


def _build_summary(
    *,
    repository: str,
    head_sha: str,
    risk: str,
    counts: Mapping[str, int],
    total: int,
    model_valid: bool,
    scanner_degraded: bool,
    delivery_note: str | None,
) -> str:
    parts = [
        f"Automated code review of {repository} at {head_sha[:12]}: risk {risk}; "
        f"{total} finding(s) ({counts['block']} block / {counts['important']} important / "
        f"{counts['suggestion']} suggestion)."
    ]
    if not model_valid:
        parts.append("Model analysis was unavailable; findings are scanner-derived only.")
    if scanner_degraded:
        parts.append("One or more scanners did not complete; results may be incomplete.")
    if delivery_note:
        parts.append(delivery_note)
    return " ".join(parts)[:2000]


def _delivery_note(delivered: str, channel_id: str | None) -> str:
    if channel_id is None:
        return "Summary delivery skipped: no code-review channel configured."
    if delivered == "sent":
        return "Summary delivered to the code-review channel."
    return "Summary delivery did not complete."


async def run_code_review(
    run_id: str,
    idempotency_key: str,
    *,
    settings: Settings | None = None,
    engine: Engine | None = None,
    gateway: LLMGateway | None = None,
    store: ArtifactStore | None = None,
    scanner_specs: Sequence[ScannerSpec] | None = None,
    checkout_source: str | Path | None = None,
    sender: ReviewSummarySender | None = None,
) -> dict[str, Any]:
    """Run the durable code-review pipeline for one queued commit."""

    resolved_settings = settings or get_settings()
    resolved_engine = engine if engine is not None else Database(resolved_settings).engine
    parsed_run_id = uuid.UUID(run_id)
    specs = tuple(scanner_specs or ())

    context = await asyncio.to_thread(_load_context, resolved_engine, parsed_run_id)
    if context.terminal:
        return {"status": context.status, "run_id": run_id, "note": "already_complete"}

    try:
        prepared = await asyncio.to_thread(
            _checkout_and_prepare,
            repository=context.repository,
            base_sha=context.base_sha,
            head_sha=context.head_sha,
            allowlist=resolved_settings.repository_allowlist,
            timeout_seconds=resolved_settings.git_clone_timeout_seconds,
            scanner_specs=specs,
            checkout_source=checkout_source,
        )
    except (RepositoryCheckoutError, PacketAssemblyError) as exc:
        diagnostic = exc.diagnostic_code
        summary = (
            f"Automated code review of {context.repository} at {context.head_sha[:12]} "
            f"could not start: {diagnostic}."
        )
        await asyncio.to_thread(
            _finish,
            resolved_engine,
            run_id=parsed_run_id,
            status="failed",
            risk="medium",
            summary=summary,
            report_artifact_key=None,
            error_code=diagnostic,
        )
        return {
            "status": "failed",
            "run_id": run_id,
            "repository": context.repository,
            "head_sha": context.head_sha,
            "base_sha": context.base_sha,
            "risk": "medium",
            "error_code": diagnostic,
            "finding_counts": dict.fromkeys(_SEVERITIES, 0),
            "total_findings": 0,
            "report_artifact_key": None,
            "degraded_scanners": [],
            "model_output_valid": False,
            "delivered": "skipped_failed",
        }

    packet = prepared.packet
    resolved_gateway = gateway if gateway is not None else LLMGateway(resolved_settings)
    resolved_store = store if store is not None else ArtifactStore(resolved_settings.artifact_root)

    invocation = await resolved_gateway.invoke_structured(
        prompt=_build_prompt(packet),
        response_model=FindingBatch,
    )
    batches: list[FindingBatch] = []
    if invocation.status is InvocationStatus.VALID and invocation.output is not None:
        batches.append(invocation.output)
    model_valid = bool(batches)

    merge_result = validate_and_merge(packet, batches)
    findings = tuple(merge_result.findings)
    counts = _finding_counts(findings)
    total = len(findings)
    scanner_degraded = bool(prepared.degraded)

    if not model_valid:
        review_status: ReviewStatus = "attention"
        error_code: str | None = "analysis_invalid_output"
    elif scanner_degraded:
        review_status = "attention"
        error_code = "scanner_degraded"
    else:
        review_status = "succeeded"
        error_code = None

    await asyncio.to_thread(
        _persist_findings,
        resolved_engine,
        reviewed_commit_id=context.reviewed_commit_id,
        findings=findings,
    )

    report_summary = _build_summary(
        repository=packet.repository,
        head_sha=packet.head_sha,
        risk=packet.risk.value,
        counts=counts,
        total=total,
        model_valid=model_valid,
        scanner_degraded=scanner_degraded,
        delivery_note=None,
    )
    report_key = await asyncio.to_thread(
        _write_report,
        resolved_store,
        repository=packet.repository,
        base_sha=packet.base_sha,
        head_sha=packet.head_sha,
        findings=findings,
        summary=report_summary,
    )

    channel_id = resolved_settings.discord_code_review_channel_id
    delivered = "skipped_no_channel"
    if channel_id is not None:
        resolved_sender = sender
        if resolved_sender is None:
            from app.connectors.discord import deliver_review_summary

            resolved_sender = deliver_review_summary
        try:
            await resolved_sender(
                engine=resolved_engine,
                run_id=parsed_run_id,
                channel_id=channel_id,
                repository=packet.repository,
                head_sha=packet.head_sha,
                risk=packet.risk.value,
                status=review_status,
                finding_counts=counts,
                report_artifact_key=report_key,
            )
            delivered = "sent"
        except LifeAgentError as exc:
            delivered = f"failed:{exc.record.code.value}"
        except Exception:
            # Delivery is best-effort here: a finished review must not be
            # discarded because the summary channel is unavailable.
            delivered = "failed:internal"

    final_summary = _build_summary(
        repository=packet.repository,
        head_sha=packet.head_sha,
        risk=packet.risk.value,
        counts=counts,
        total=total,
        model_valid=model_valid,
        scanner_degraded=scanner_degraded,
        delivery_note=_delivery_note(delivered, channel_id),
    )
    await asyncio.to_thread(
        _finish,
        resolved_engine,
        run_id=parsed_run_id,
        status=review_status,
        risk=packet.risk.value,
        summary=final_summary,
        report_artifact_key=report_key,
        error_code=error_code,
    )

    return {
        "status": review_status,
        "run_id": run_id,
        "repository": packet.repository,
        "head_sha": packet.head_sha,
        "base_sha": packet.base_sha,
        "risk": packet.risk.value,
        "finding_counts": dict(counts),
        "total_findings": total,
        "report_artifact_key": report_key,
        "model_output_valid": model_valid,
        "degraded_scanners": [
            {"scanner": scanner, "status": status, "diagnostic_code": code}
            for scanner, status, code in prepared.degraded
        ],
        "delivered": delivered,
    }


__all__ = ["ReviewSummarySender", "run_code_review"]
