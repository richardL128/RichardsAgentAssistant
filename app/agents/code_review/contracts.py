"""Strict shared contracts for the Phase 3 code-review pipeline."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SHA_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
_REPOSITORY_PATTERN = r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$"


class ReviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def validate_repo_path(value: str) -> str:
    """Accept a normalized repository-relative POSIX path only."""

    if not value or "\\" in value or "\x00" in value:
        raise ValueError("repository path must be a non-empty POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value:
        raise ValueError("repository path must be normalized and relative")
    if len(value) > 500:
        raise ValueError("repository path is too long")
    return value


class PushEvent(ReviewModel):
    delivery_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    repository: str = Field(pattern=_REPOSITORY_PATTERN)
    clone_url: str = Field(min_length=1, max_length=1_000)
    default_branch: str = Field(min_length=1, max_length=255, pattern=r"^[^\s~^:?*\[\\]+$")
    installation_id: int = Field(gt=0)
    ref: str = Field(min_length=12, max_length=500, pattern=r"^refs/heads/.+$")
    before_sha: str = Field(pattern=_SHA_PATTERN)
    after_sha: str = Field(pattern=_SHA_PATTERN)
    received_at: datetime

    @field_validator("received_at")
    @classmethod
    def received_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("received_at must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("clone_url")
    @classmethod
    def clone_url_is_fixed_github_https(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("clone URL must be an uncredentialed github.com HTTPS URL")
        return value

    @model_validator(mode="after")
    def commit_range_changes(self) -> PushEvent:
        if self.before_sha == self.after_sha:
            raise ValueError("push must change the repository SHA")
        return self


class RiskLevel(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ChangedFile(ReviewModel):
    path: str
    status: Literal["added", "modified", "deleted", "renamed", "copied", "type_changed"]
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    patch: str = Field(max_length=30_000)
    changed_lines: list[int] = Field(max_length=5_000)
    is_binary: bool = False

    @field_validator("path")
    @classmethod
    def path_is_safe(cls, value: str) -> str:
        return validate_repo_path(value)

    @field_validator("changed_lines")
    @classmethod
    def lines_are_sorted_unique(cls, value: list[int]) -> list[int]:
        if any(line < 1 for line in value):
            raise ValueError("changed line numbers must be positive")
        if value != sorted(set(value)):
            raise ValueError("changed line numbers must be sorted and unique")
        return value


class ScannerKind(StrEnum):
    SEMGREP = "semgrep"
    GITLEAKS = "gitleaks"
    TRIVY = "trivy"
    NATIVE = "native"


class ScannerStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FINDINGS = "findings"
    TIMED_OUT = "timed_out"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class ScannerFinding(ReviewModel):
    scanner: ScannerKind
    rule_id: str = Field(min_length=1, max_length=300)
    severity: Literal["block", "important", "suggestion"]
    path: str
    line: int | None = Field(default=None, ge=1)
    title: str = Field(min_length=1, max_length=300)
    evidence: str = Field(min_length=1, max_length=1_000)
    fingerprint: str = Field(min_length=16, max_length=128, pattern=r"^[0-9a-f]+$")

    @field_validator("path")
    @classmethod
    def path_is_safe(cls, value: str) -> str:
        return validate_repo_path(value)


class ScannerRun(ReviewModel):
    scanner: ScannerKind
    status: ScannerStatus
    duration_ms: float = Field(ge=0)
    findings: list[ScannerFinding] = Field(max_length=1_000)
    diagnostic_code: str = Field(min_length=1, max_length=128)
    output_artifact_key: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ReviewPacket(ReviewModel):
    repository: str = Field(pattern=_REPOSITORY_PATTERN)
    base_sha: str = Field(pattern=_SHA_PATTERN)
    head_sha: str = Field(pattern=_SHA_PATTERN)
    risk: RiskLevel
    risk_reasons: list[str] = Field(max_length=20)
    files: list[ChangedFile] = Field(max_length=200)
    scanner_runs: list[ScannerRun] = Field(max_length=20)
    profile_artifact_key: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    instructions_provenance: list[str] = Field(max_length=50)

    @model_validator(mode="after")
    def packet_is_bounded(self) -> ReviewPacket:
        character_count = sum(len(file.patch) for file in self.files)
        character_count += sum(
            len(finding.evidence) for run in self.scanner_runs for finding in run.findings
        )
        if character_count > 120_000:
            raise ValueError("review packet exceeds the bounded character budget")
        return self


class FindingProposal(ReviewModel):
    finding_present: bool
    severity: Literal["block", "important", "suggestion"] | None
    path: str | None = None
    line: int | None = Field(default=None, ge=1)
    title: str | None = Field(default=None, max_length=300)
    explanation: str | None = Field(default=None, max_length=2_000)
    reproduction_or_missing_test: str | None = Field(default=None, max_length=2_000)
    confidence: float = Field(ge=0, le=1)
    assumptions: list[str] = Field(max_length=10)
    evidence_refs: list[str] = Field(max_length=20)

    @field_validator("path")
    @classmethod
    def optional_path_is_safe(cls, value: str | None) -> str | None:
        return validate_repo_path(value) if value is not None else None

    @model_validator(mode="after")
    def present_finding_is_complete(self) -> FindingProposal:
        required = (
            self.severity,
            self.path,
            self.line,
            self.title,
            self.explanation,
            self.reproduction_or_missing_test,
        )
        if self.finding_present and any(value is None for value in required):
            raise ValueError("present findings require severity, location, explanation, and test")
        if not self.finding_present and any(value is not None for value in required):
            raise ValueError("dismissed findings cannot retain finding details")
        return self


class FindingBatch(ReviewModel):
    findings: list[FindingProposal] = Field(max_length=50)
    review_summary: str = Field(min_length=1, max_length=2_000)


class ValidatedFinding(ReviewModel):
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    severity: Literal["block", "important", "suggestion"]
    path: str
    line: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=300)
    explanation: str = Field(min_length=1, max_length=2_000)
    reproduction_or_missing_test: str = Field(min_length=1, max_length=2_000)
    confidence: float = Field(ge=0, le=1)
    assumptions: list[str] = Field(max_length=10)
    evidence_refs: list[str] = Field(min_length=1, max_length=20)

    @field_validator("path")
    @classmethod
    def path_is_safe(cls, value: str) -> str:
        return validate_repo_path(value)


class ProjectProfile(ReviewModel):
    repository: str = Field(pattern=_REPOSITORY_PATTERN)
    commit_sha: str = Field(pattern=_SHA_PATTERN)
    purpose: str = Field(min_length=1, max_length=2_000)
    languages: list[str] = Field(max_length=30)
    commands: list[str] = Field(max_length=30)
    conventions: list[str] = Field(max_length=50)
    high_risk_paths: list[str] = Field(max_length=100)
    instruction_files: list[str] = Field(max_length=50)
    citations: list[str] = Field(min_length=1, max_length=100)
    reviewed: bool = False

    @field_validator("high_risk_paths", "instruction_files")
    @classmethod
    def paths_are_safe(cls, value: list[str]) -> list[str]:
        return [validate_repo_path(path) for path in value]


__all__ = [
    "ChangedFile",
    "FindingBatch",
    "FindingProposal",
    "ProjectProfile",
    "PushEvent",
    "ReviewPacket",
    "RiskLevel",
    "ScannerFinding",
    "ScannerKind",
    "ScannerRun",
    "ScannerStatus",
    "ValidatedFinding",
    "validate_repo_path",
]
