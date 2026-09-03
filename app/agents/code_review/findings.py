"""Deterministic validation and consolidation of code-review findings."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from difflib import SequenceMatcher
from enum import IntEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from app.agents.code_review.contracts import (
    FindingBatch,
    FindingProposal,
    ReviewPacket,
    ScannerFinding,
    ValidatedFinding,
)
from app.core.redaction import redact_text

MIN_FINDING_CONFIDENCE: Final[float] = 0.60
_VAGUE_RE = re.compile(
    r"\b(?:might|may|could|possibly|potentially|seems?|appears?|perhaps|likely|"
    r"I think|it is possible|concern(?:ing)?|something|various|somehow|generally)\b",
    re.IGNORECASE,
)
_ACTION_RE = re.compile(
    r"\b(?:causes?|allows?|leaks?|exposes?|bypasses?|fails?|breaks?|returns?|"
    r"raises?|writes?|deletes?|skips?|accepts?|rejects?|prevents?|reprodu(?:ce|ces)|"
    r"assert|test|fix|validate|sanitize|escape|limit|check)\b",
    re.IGNORECASE,
)
_SPECULATION_RE = re.compile(
    r"\b(?:could|might|may|possibly|potentially|if someone|in theory|hypothetically|"
    r"unclear|unknown|not sure|seems?)\b",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[a-z0-9]+")
_DOC_SUFFIXES = {".md", ".markdown", ".rst", ".txt", ".adoc", ".asciidoc"}


class FindingValidationError(ValueError):
    """Raised when a model proposal cannot be tied to packet-owned facts."""

    def __init__(self, reason: str, *, code: str = "invalid_finding") -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class RejectedFinding(BaseModel):
    """Safe diagnostic for a proposal omitted from a review result."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    index: int = Field(ge=0)
    reason_code: str = Field(min_length=1, max_length=80)


class FindingMergeResult(BaseModel):
    """Accepted findings and non-sensitive rejection diagnostics."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    findings: list[ValidatedFinding]
    rejected: list[RejectedFinding]


class _SeverityRank(IntEnum):
    SUGGESTION = 1
    IMPORTANT = 2
    BLOCK = 3


def packet_evidence_ids(packet: ReviewPacket) -> frozenset[str]:
    """Return deterministic evidence IDs present in a bounded review packet."""

    evidence: set[str] = set()
    for changed_file in packet.files:
        for line in changed_file.changed_lines:
            evidence.update(
                {f"diff:{changed_file.path}:{line}", f"file:{changed_file.path}:{line}"}
            )
    for run in packet.scanner_runs:
        for finding in run.findings:
            evidence.update(
                {
                    finding.fingerprint,
                    f"scanner:{finding.fingerprint}",
                    f"scanner:{finding.scanner.value}:{finding.fingerprint}",
                }
            )
    return frozenset(evidence)


def finding_fingerprint(
    *,
    path: str,
    line: int,
    title: str,
    explanation: str,
    reproduction_or_missing_test: str = "",
) -> str:
    """Return a canonical SHA-256 identity for an actionable finding."""

    parts = (
        _normalize(path),
        str(line),
        _normalize(title),
        _normalize(explanation),
        _normalize(reproduction_or_missing_test),
    )
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


canonical_fingerprint = finding_fingerprint
fingerprint_finding = finding_fingerprint


def validate_finding(proposal: FindingProposal, packet: ReviewPacket) -> ValidatedFinding:
    """Validate one model proposal against packet-owned facts."""

    if not proposal.finding_present:
        raise FindingValidationError("proposal does not contain a finding", code="dismissed")
    if proposal.confidence < MIN_FINDING_CONFIDENCE:
        raise FindingValidationError(
            "finding confidence is below the actionable threshold", code="low_confidence"
        )
    assert proposal.path is not None
    assert proposal.line is not None
    assert proposal.title is not None
    assert proposal.explanation is not None
    assert proposal.reproduction_or_missing_test is not None

    file_by_path = {changed_file.path: changed_file for changed_file in packet.files}
    changed_file = file_by_path.get(proposal.path)
    if changed_file is None:
        raise FindingValidationError(
            "finding path is absent from the review packet", code="unknown_path"
        )
    if proposal.line not in changed_file.changed_lines:
        raise FindingValidationError(
            "finding line is not a changed line in the packet", code="unknown_line"
        )
    evidence_ids = packet_evidence_ids(packet)
    if not proposal.evidence_refs:
        raise FindingValidationError("finding must cite packet evidence", code="missing_evidence")
    if any(ref not in evidence_ids for ref in proposal.evidence_refs):
        raise FindingValidationError(
            "finding cites evidence absent from the packet", code="unknown_evidence"
        )
    _validate_actionability(proposal, changed_file.path)

    assert proposal.severity is not None
    return ValidatedFinding(
        fingerprint=finding_fingerprint(
            path=proposal.path,
            line=proposal.line,
            title=proposal.title,
            explanation=proposal.explanation,
            reproduction_or_missing_test=proposal.reproduction_or_missing_test,
        ),
        severity=proposal.severity,
        path=proposal.path,
        line=proposal.line,
        title=_safe_field(proposal.title, 300),
        explanation=_safe_field(proposal.explanation, 2_000),
        reproduction_or_missing_test=_safe_field(proposal.reproduction_or_missing_test, 2_000),
        confidence=proposal.confidence,
        assumptions=_safe_list(proposal.assumptions, 10),
        evidence_refs=sorted(set(proposal.evidence_refs)),
    )


def normalize_scanner_finding(
    finding: ScannerFinding, packet: ReviewPacket
) -> ValidatedFinding | None:
    """Convert one scanner result into the same safe finding representation."""

    changed_file = next((item for item in packet.files if item.path == finding.path), None)
    if (
        changed_file is None
        or finding.line is None
        or finding.line not in changed_file.changed_lines
    ):
        return None
    title = _safe_field(f"{finding.scanner.value}: {finding.title}", 300)
    explanation = _safe_field(
        f"{finding.scanner.value} rule {finding.rule_id} reported this changed location.", 2_000
    )
    reproduction = (
        "Re-run the configured scanner rule and add a regression test for the reported behavior."
    )
    return ValidatedFinding(
        fingerprint=finding_fingerprint(
            path=finding.path,
            line=finding.line,
            title=title,
            explanation=explanation,
            reproduction_or_missing_test=reproduction,
        ),
        severity=finding.severity,
        path=finding.path,
        line=finding.line,
        title=title,
        explanation=explanation,
        reproduction_or_missing_test=reproduction,
        confidence=1.0,
        assumptions=[],
        evidence_refs=[finding.fingerprint],
    )


def scanner_findings(packet: ReviewPacket) -> list[ValidatedFinding]:
    """Normalize all attributable scanner results from a review packet."""

    return [
        normalized
        for run in packet.scanner_runs
        for scanner_finding in run.findings
        if (normalized := normalize_scanner_finding(scanner_finding, packet)) is not None
    ]


def validate_and_merge(
    packet: ReviewPacket,
    batches: Iterable[FindingBatch] = (),
    *,
    include_scanner_findings: bool = True,
) -> FindingMergeResult:
    """Validate model batches, add scanner findings, and deduplicate stably."""

    accepted: list[ValidatedFinding] = scanner_findings(packet) if include_scanner_findings else []
    rejected: list[RejectedFinding] = []
    index = 0
    for batch in batches:
        for proposal in batch.findings:
            try:
                accepted.append(validate_finding(proposal, packet))
            except FindingValidationError as exc:
                rejected.append(RejectedFinding(index=index, reason_code=exc.code))
            index += 1
    return FindingMergeResult(findings=merge_findings(accepted), rejected=rejected)


def merge_findings(findings: Sequence[ValidatedFinding]) -> list[ValidatedFinding]:
    """Merge exact and near-equivalent findings, retaining strongest evidence."""

    merged: list[ValidatedFinding] = []
    for finding in findings:
        match_index = next(
            (index for index, existing in enumerate(merged) if _equivalent(existing, finding)),
            None,
        )
        if match_index is None:
            merged.append(finding)
        else:
            merged[match_index] = _merge_pair(merged[match_index], finding)
    return sorted(merged, key=_sort_key)


def _validate_actionability(proposal: FindingProposal, path: str) -> None:
    text = " ".join(
        value
        for value in (proposal.title, proposal.explanation, proposal.reproduction_or_missing_test)
        if value
    )
    if len(_WORD_RE.findall(text)) < 8 or not _ACTION_RE.search(text):
        raise FindingValidationError("finding text is vague or not actionable", code="vague")
    if _VAGUE_RE.search(proposal.title or "") and not _ACTION_RE.search(proposal.explanation or ""):
        raise FindingValidationError(
            "finding title is speculative and lacks an actionable explanation",
            code="speculative",
        )
    suffix = path.rsplit("/", 1)[-1].lower()
    if any(suffix.endswith(extension) for extension in _DOC_SUFFIXES) and _SPECULATION_RE.search(
        text
    ):
        raise FindingValidationError(
            "documentation-only finding is speculative", code="documentation_speculation"
        )


def _equivalent(left: ValidatedFinding, right: ValidatedFinding) -> bool:
    if left.path != right.path or left.line != right.line:
        return False
    if left.fingerprint == right.fingerprint:
        return True
    left_text = _normalize(f"{left.title} {left.explanation}")
    right_text = _normalize(f"{right.title} {right.explanation}")
    ratio = SequenceMatcher(None, left_text, right_text, autojunk=False).ratio()
    left_words = set(_WORD_RE.findall(left_text))
    right_words = set(_WORD_RE.findall(right_text))
    overlap = len(left_words & right_words) / max(1, len(left_words | right_words))
    return ratio >= 0.80 or overlap >= 0.75


def _merge_pair(left: ValidatedFinding, right: ValidatedFinding) -> ValidatedFinding:
    stronger, weaker = (left, right) if _strength(left) >= _strength(right) else (right, left)
    return stronger.model_copy(
        update={
            "confidence": max(left.confidence, right.confidence),
            "assumptions": sorted(set(left.assumptions) | set(right.assumptions)),
            "evidence_refs": sorted(set(left.evidence_refs) | set(right.evidence_refs)),
            "severity": _stronger_severity(left.severity, right.severity),
            "explanation": _longer_text(stronger.explanation, weaker.explanation),
            "reproduction_or_missing_test": _longer_text(
                stronger.reproduction_or_missing_test, weaker.reproduction_or_missing_test
            ),
        }
    )


def _strength(finding: ValidatedFinding) -> tuple[float, int, int, int]:
    return (
        finding.confidence,
        _SeverityRank[finding.severity.upper()],
        len(finding.evidence_refs),
        len(finding.explanation),
    )


def _stronger_severity(left: str, right: str) -> str:
    return left if _SeverityRank[left.upper()] >= _SeverityRank[right.upper()] else right


def _longer_text(left: str, right: str) -> str:
    return left if len(left) >= len(right) else right


def _sort_key(finding: ValidatedFinding) -> tuple[int, str, int, str]:
    return (
        -_SeverityRank[finding.severity.upper()],
        finding.path,
        finding.line,
        finding.fingerprint,
    )


def _normalize(value: str) -> str:
    return " ".join(_WORD_RE.findall(value.casefold()))


def _safe_field(value: str, limit: int) -> str:
    return redact_text(value).replace("\x00", "")[:limit].strip()


def _safe_list(values: Iterable[str], limit: int) -> list[str]:
    safe_values = {_safe_field(value, 500) for value in values}
    return sorted(value for value in safe_values if value)[:limit]


__all__ = [
    "MIN_FINDING_CONFIDENCE",
    "FindingMergeResult",
    "FindingValidationError",
    "RejectedFinding",
    "canonical_fingerprint",
    "finding_fingerprint",
    "fingerprint_finding",
    "merge_findings",
    "normalize_scanner_finding",
    "packet_evidence_ids",
    "scanner_findings",
    "validate_and_merge",
    "validate_finding",
]
