"""Bounded, independently failing static-analysis command runners."""

# JSON scanner schemas are intentionally treated as untrusted dynamic input.
# The typed boundary is ScannerFinding below, after path and text validation.
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportOptionalMemberAccess=false

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from app.agents.code_review.contracts import (
    ScannerFinding,
    ScannerKind,
    ScannerRun,
    ScannerStatus,
    validate_repo_path,
)
from app.core.redaction import redact_text

DEFAULT_NATIVE_ALLOWLIST = frozenset({"cargo", "go", "make", "npm", "pnpm", "pytest", "ruff"})
_DEFAULT_TIMEOUT = 60.0
_DEFAULT_OUTPUT_BYTES = 2_000_000


@dataclass(frozen=True)
class ScannerSpec:
    """One executable invocation; ``args`` never contains credentials."""

    scanner: ScannerKind
    executable: str
    args: tuple[str, ...] = ()
    timeout_seconds: float = _DEFAULT_TIMEOUT
    output_limit_bytes: int = _DEFAULT_OUTPUT_BYTES
    allowlisted_commands: frozenset[str] = field(default_factory=lambda: DEFAULT_NATIVE_ALLOWLIST)


def _safe_fingerprint(scanner: ScannerKind, rule: str, path: str, line: int | None) -> str:
    return hashlib.sha256(f"{scanner.value}\x00{rule}\x00{path}\x00{line}".encode()).hexdigest()


def _safe_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if any(ord(character) < 32 for character in value):
        return None
    try:
        return validate_repo_path(value)
    except ValueError:
        return None


def _severity(value: Any) -> Literal["block", "important", "suggestion"]:
    text = str(value or "").lower()
    if text in {"error", "critical", "high", "block"}:
        return "block"
    if text in {"warning", "warn", "medium", "important"}:
        return "important"
    return "suggestion"


def _text(value: Any, fallback: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return fallback
    return redact_text(value)[:1_000] or fallback


def _parse_semgrep(payload: Any) -> list[ScannerFinding]:
    findings: list[ScannerFinding] = []
    for item in payload.get("results", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        path = _safe_path(item.get("path"))
        if path is None:
            continue
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        line_value = (
            item.get("start", {}).get("line") if isinstance(item.get("start"), dict) else None
        )
        line = line_value if isinstance(line_value, int) and line_value > 0 else None
        rule = str(item.get("check_id") or "semgrep.unknown")[:300]
        findings.append(
            ScannerFinding(
                scanner=ScannerKind.SEMGREP,
                rule_id=rule,
                severity=_severity(extra.get("severity")),
                path=path,
                line=line,
                title=_text(extra.get("message"), "Semgrep finding"),
                evidence=_text(extra.get("message"), "Semgrep finding"),
                fingerprint=_safe_fingerprint(ScannerKind.SEMGREP, rule, path, line),
            )
        )
    return findings


def _parse_gitleaks(payload: Any) -> list[ScannerFinding]:
    findings: list[ScannerFinding] = []
    records = (
        payload
        if isinstance(payload, list)
        else payload.get("findings", [])
        if isinstance(payload, dict)
        else []
    )
    for item in records:
        if not isinstance(item, dict):
            continue
        path = _safe_path(item.get("File") or item.get("file"))
        if path is None:
            continue
        line_value = item.get("StartLine") or item.get("startLine")
        line = line_value if isinstance(line_value, int) and line_value > 0 else None
        rule = str(item.get("RuleID") or item.get("ruleID") or "gitleaks.unknown")[:300]
        # Never use Match/Secret/SecretValue in evidence, title, diagnostics,
        # fingerprints, or any other typed output.  Rule/path/line are safe.
        title = "Gitleaks detected a possible secret"
        findings.append(
            ScannerFinding(
                scanner=ScannerKind.GITLEAKS,
                rule_id=rule,
                severity="block",
                path=path,
                line=line,
                title=title,
                evidence="Secret content redacted; inspect the repository securely.",
                fingerprint=_safe_fingerprint(ScannerKind.GITLEAKS, rule, path, line),
            )
        )
    return findings


def _parse_trivy(payload: Any) -> list[ScannerFinding]:
    findings: list[ScannerFinding] = []
    results = payload.get("Results", []) if isinstance(payload, dict) else []
    for result in results:
        if not isinstance(result, dict):
            continue
        path = _safe_path(result.get("Target")) or "trivy/unknown"
        vulnerabilities = result.get("Vulnerabilities", [])
        for item in vulnerabilities if isinstance(vulnerabilities, list) else []:
            if not isinstance(item, dict):
                continue
            rule = str(item.get("VulnerabilityID") or "trivy.unknown")[:300]
            title = _text(item.get("Title"), f"Trivy vulnerability {rule}")
            findings.append(
                ScannerFinding(
                    scanner=ScannerKind.TRIVY,
                    rule_id=rule,
                    severity=_severity(item.get("Severity")),
                    path=path,
                    line=None,
                    title=title,
                    evidence=_text(item.get("PkgName"), "Trivy reported a vulnerability"),
                    fingerprint=_safe_fingerprint(ScannerKind.TRIVY, rule, path, None),
                )
            )
    return findings


def _terminate(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
        process.wait()


def _run_process(spec: ScannerSpec, checkout: Path) -> tuple[ScannerStatus, str]:
    if spec.timeout_seconds <= 0 or spec.output_limit_bytes < 1:
        return ScannerStatus.FAILED, "scanner_config_invalid"
    executable = Path(spec.executable)
    command_name = executable.name
    if spec.scanner is ScannerKind.NATIVE and command_name not in spec.allowlisted_commands:
        return ScannerStatus.UNAVAILABLE, "native_command_not_allowlisted"
    argv = [spec.executable, *spec.args, str(checkout)]
    try:
        process = subprocess.Popen(  # noqa: S603
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(Path.cwd()),
                "LANG": "C",
                "LC_ALL": "C",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0",
            },
        )
        try:
            stdout, _stderr = process.communicate(timeout=spec.timeout_seconds)
        except subprocess.TimeoutExpired:
            _terminate(process)
            return ScannerStatus.TIMED_OUT, "scanner_timeout"
    except OSError:
        return ScannerStatus.UNAVAILABLE, "scanner_unavailable"
    if len(stdout) > spec.output_limit_bytes:
        return ScannerStatus.FAILED, "scanner_output_limit"
    output = redact_text(stdout.decode("utf-8", errors="replace"))
    if process.returncode not in (0, 1):
        return ScannerStatus.FAILED, "scanner_failed"
    return ScannerStatus.SUCCEEDED, output


def run_scanner(
    spec: ScannerSpec, checkout: str | Path, *, secrets: Iterable[str] = ()
) -> ScannerRun:
    """Run one scanner and return an independently safe typed result."""

    started = time.monotonic()
    status, output = _run_process(spec, Path(checkout).resolve())
    duration = round((time.monotonic() - started) * 1000, 3)
    if status is not ScannerStatus.SUCCEEDED:
        return ScannerRun(
            scanner=spec.scanner,
            status=status,
            duration_ms=duration,
            findings=[],
            diagnostic_code=output,
        )
    # Apply caller-supplied secret redaction before parsing as a final defense.
    for secret in secrets:
        if secret:
            output = output.replace(secret, "[REDACTED]")
    try:
        payload = json.loads(output) if output.strip() else {}
        if spec.scanner is ScannerKind.SEMGREP:
            findings = _parse_semgrep(payload)
        elif spec.scanner is ScannerKind.GITLEAKS:
            findings = _parse_gitleaks(payload)
        elif spec.scanner is ScannerKind.TRIVY:
            findings = _parse_trivy(payload)
        else:
            findings = []
    except (ValueError, TypeError, json.JSONDecodeError):
        return ScannerRun(
            scanner=spec.scanner,
            status=ScannerStatus.FAILED,
            duration_ms=duration,
            findings=[],
            diagnostic_code="scanner_invalid_output",
        )
    return ScannerRun(
        scanner=spec.scanner,
        status=ScannerStatus.FINDINGS if findings else ScannerStatus.SUCCEEDED,
        duration_ms=duration,
        findings=findings,
        diagnostic_code="scanner_findings" if findings else "scanner_clean",
    )


def run_scanners(
    specs: Iterable[ScannerSpec], checkout: str | Path, *, secrets: Iterable[str] = ()
) -> list[ScannerRun]:
    """Run all configured scanners; one timeout/failure never blocks others."""

    return [run_scanner(spec, checkout, secrets=secrets) for spec in specs]


def run_semgrep(
    checkout: str | Path,
    *,
    executable: str = "semgrep",
    args: Sequence[str] = ("--json",),
    timeout_seconds: float = _DEFAULT_TIMEOUT,
    output_limit_bytes: int = _DEFAULT_OUTPUT_BYTES,
    secrets: Iterable[str] = (),
) -> ScannerRun:
    return run_scanner(
        ScannerSpec(
            ScannerKind.SEMGREP, executable, tuple(args), timeout_seconds, output_limit_bytes
        ),
        checkout,
        secrets=secrets,
    )


def run_gitleaks(
    checkout: str | Path,
    *,
    executable: str = "gitleaks",
    args: Sequence[str] = ("detect", "--report-format", "json"),
    timeout_seconds: float = _DEFAULT_TIMEOUT,
    output_limit_bytes: int = _DEFAULT_OUTPUT_BYTES,
    secrets: Iterable[str] = (),
) -> ScannerRun:
    return run_scanner(
        ScannerSpec(
            ScannerKind.GITLEAKS, executable, tuple(args), timeout_seconds, output_limit_bytes
        ),
        checkout,
        secrets=secrets,
    )


def run_trivy(
    checkout: str | Path,
    *,
    executable: str = "trivy",
    args: Sequence[str] = ("fs", "--format", "json"),
    timeout_seconds: float = _DEFAULT_TIMEOUT,
    output_limit_bytes: int = _DEFAULT_OUTPUT_BYTES,
    secrets: Iterable[str] = (),
) -> ScannerRun:
    return run_scanner(
        ScannerSpec(
            ScannerKind.TRIVY, executable, tuple(args), timeout_seconds, output_limit_bytes
        ),
        checkout,
        secrets=secrets,
    )


__all__ = [
    "DEFAULT_NATIVE_ALLOWLIST",
    "ScannerSpec",
    "run_gitleaks",
    "run_scanner",
    "run_scanners",
    "run_semgrep",
    "run_trivy",
]
