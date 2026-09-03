"""Safe, deterministic code-review report rendering.

Reports contain only validated metadata and short explanatory fields. They
never read a checkout or include patch/source bodies.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from app.agents.code_review.contracts import ValidatedFinding
from app.agents.code_review.findings import merge_findings
from app.core.redaction import redact_text

_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")


def report_payload(
    *,
    repository: str,
    base_sha: str,
    head_sha: str,
    findings: Iterable[ValidatedFinding],
    summary: str | None = None,
) -> dict[str, Any]:
    """Build a JSON-safe report payload with stable finding order."""

    _validate_identity(repository, base_sha, head_sha)
    ordered = merge_findings(list(findings))
    payload: dict[str, Any] = {
        "repository": repository,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "findings": [_finding_payload(finding) for finding in ordered],
    }
    if summary is not None:
        payload["summary"] = _safe_text(summary, 2_000)
    return payload


def render_json_report(
    *,
    repository: str,
    base_sha: str,
    head_sha: str,
    findings: Iterable[ValidatedFinding],
    summary: str | None = None,
) -> str:
    """Render a stable, redacted JSON report."""

    return json.dumps(
        report_payload(
            repository=repository,
            base_sha=base_sha,
            head_sha=head_sha,
            findings=findings,
            summary=summary,
        ),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def render_markdown_report(
    *,
    repository: str,
    base_sha: str,
    head_sha: str,
    findings: Iterable[ValidatedFinding],
    summary: str | None = None,
) -> str:
    """Render a coding-harness-friendly Markdown report."""

    payload = report_payload(
        repository=repository,
        base_sha=base_sha,
        head_sha=head_sha,
        findings=findings,
        summary=summary,
    )
    bt = chr(96)
    lines = [
        "# Code review report",
        "",
        f"- Repository: {bt}{_code(repository)}{bt}",
        f"- Base SHA: {bt}{_code(base_sha)}{bt}",
        f"- Head SHA: {bt}{_code(head_sha)}{bt}",
        "",
    ]
    if summary is not None:
        lines.extend(["## Summary", "", _markdown_text(str(payload["summary"])), ""])
    if not payload["findings"]:
        lines.extend(["## Findings", "", "No actionable findings.", ""])
        return "\n".join(lines)

    lines.extend(["## Findings", ""])
    for number, finding in enumerate(payload["findings"], start=1):
        lines.extend(
            [
                f"### {number}. [{_code(str(finding['severity']).upper())}] "
                f"{_markdown_text(str(finding['title']))}",
                "",
                f"- Location: {bt}{_code(str(finding['path']))}:{finding['line']}{bt}",
                f"- Fingerprint: {bt}{_code(str(finding['fingerprint']))}{bt}",
                f"- Confidence: {float(finding['confidence']):.2f}",
                f"- Consequence: {_markdown_text(str(finding['explanation']))}",
                "- Reproduction or missing test: "
                f"{_markdown_text(str(finding['reproduction_or_missing_test']))}",
                f"- Assumptions: {_join_items(finding['assumptions'], bt)}",
                f"- Evidence IDs: {_join_items(finding['evidence_refs'], bt)}",
                "",
            ]
        )
    return "\n".join(lines)


def render_report(
    *,
    repository: str,
    base_sha: str,
    head_sha: str,
    findings: Iterable[ValidatedFinding],
    summary: str | None = None,
    format: str = "markdown",  # noqa: A002 - public API mirrors report formats
) -> str:
    """Render either the Markdown (default) or JSON report format."""

    if format == "markdown":
        return render_markdown_report(
            repository=repository,
            base_sha=base_sha,
            head_sha=head_sha,
            findings=findings,
            summary=summary,
        )
    if format == "json":
        return render_json_report(
            repository=repository,
            base_sha=base_sha,
            head_sha=head_sha,
            findings=findings,
            summary=summary,
        )
    raise ValueError("report format must be 'markdown' or 'json'")


def _finding_payload(finding: ValidatedFinding) -> dict[str, Any]:
    return {
        "fingerprint": finding.fingerprint,
        "severity": finding.severity,
        "path": finding.path,
        "line": finding.line,
        "title": _safe_text(finding.title, 300),
        "explanation": _safe_text(finding.explanation, 2_000),
        "reproduction_or_missing_test": _safe_text(finding.reproduction_or_missing_test, 2_000),
        "confidence": finding.confidence,
        "assumptions": sorted({_safe_text(item, 500) for item in finding.assumptions}),
        "evidence_refs": sorted(set(finding.evidence_refs)),
    }


def _validate_identity(repository: str, base_sha: str, head_sha: str) -> None:
    if not _REPOSITORY_RE.fullmatch(repository):
        raise ValueError("repository must be owner/name")
    if not _SHA_RE.fullmatch(base_sha) or not _SHA_RE.fullmatch(head_sha):
        raise ValueError("report identities must use a 40- or 64-character lowercase SHA")


def _safe_text(value: str, limit: int) -> str:
    return redact_text(value).replace("\x00", "").strip()[:limit]


def _code(value: str) -> str:
    return _safe_text(value, 2_000).replace("\\", "\\\\").replace(chr(96), "'").replace("\n", " ")


def _markdown_text(value: str) -> str:
    safe = _safe_text(value, 2_000)
    safe = (
        safe.replace("\\", "\\\\").replace(chr(96), "'").replace("<", "&lt;").replace(">", "&gt;")
    )
    safe = re.sub(r"(?m)^\s*([#>*+-])", r"\\\1", safe)
    return safe.replace("\n", " ")


def _join_items(values: Any, bt: str) -> str:
    items = [f"{bt}{_code(str(value))}{bt}" for value in values if _safe_text(str(value), 500)]
    return ", ".join(items) if items else "None recorded"


__all__ = [
    "render_json_report",
    "render_markdown_report",
    "render_report",
    "report_payload",
]
