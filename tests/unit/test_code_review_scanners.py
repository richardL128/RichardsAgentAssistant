"""Bounded, independently failing scanner runner tests (Phase 3).

Stub scanners are supplied as an interpreter argument (never chmod +x'd), so
they work on a ``noexec`` ``tmp_path`` -- the ``_fake_scanner`` pattern from
``test_code_review_repository.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.agents.code_review.contracts import ScannerKind, ScannerStatus
from app.agents.code_review.scanners import ScannerSpec, run_scanner, run_scanners


def _scanner(tmp_path: Path, body: str, name: str) -> tuple[str, str]:
    script = tmp_path / f"{name}.py"
    script.write_text(f"{body}\n")
    return sys.executable, str(script)


def _emit(tmp_path: Path, payload: object, name: str) -> tuple[str, str]:
    return _scanner(tmp_path, f"print({json.dumps(payload)!r})", name)


def _spec(kind: ScannerKind, exe: str, script: str, **kw: object) -> ScannerSpec:
    return ScannerSpec(kind, exe, (script,), **kw)  # type: ignore[arg-type]


def test_semgrep_parsing_keeps_safe_fields_and_drops_unsafe_paths(tmp_path: Path) -> None:
    payload = {
        "results": [
            {
                "path": "src/app.py",
                "start": {"line": 4},
                "check_id": "python.lang.security.audit",
                "extra": {"severity": "ERROR", "message": "tainted input"},
            },
            {"path": "../../etc/passwd", "start": {"line": 1}, "check_id": "evil"},
            {"path": "notes.py", "check_id": "no-line", "extra": {"message": "m"}},
        ]
    }
    exe, script = _emit(tmp_path, payload, "semgrep")
    run = run_scanner(_spec(ScannerKind.SEMGREP, exe, script, timeout_seconds=5), tmp_path)

    assert run.status is ScannerStatus.FINDINGS
    assert run.diagnostic_code == "scanner_findings"
    paths = {finding.path for finding in run.findings}
    assert paths == {"src/app.py", "notes.py"}  # traversal path dropped
    first = next(f for f in run.findings if f.path == "src/app.py")
    assert first.severity == "block"
    assert first.line == 4
    assert first.rule_id == "python.lang.security.audit"


def test_gitleaks_secret_and_match_never_reach_the_typed_output(tmp_path: Path) -> None:
    payload = [
        {
            "RuleID": "aws-access-key",
            "File": "src/config.py",
            "StartLine": 7,
            "Secret": "AKIAIOSFODNN7EXAMPLE",
            "Match": "aws_key = AKIAIOSFODNN7EXAMPLE",
            "SecretValue": "AKIAIOSFODNN7EXAMPLE",
        }
    ]
    exe, script = _emit(tmp_path, payload, "gitleaks")
    run = run_scanner(_spec(ScannerKind.GITLEAKS, exe, script, timeout_seconds=5), tmp_path)

    assert run.status is ScannerStatus.FINDINGS
    serialised = run.model_dump_json()
    assert "AKIAIOSFODNN7EXAMPLE" not in serialised
    assert "aws_key =" not in serialised
    finding = run.findings[0]
    assert finding.rule_id == "aws-access-key"
    assert finding.path == "src/config.py"
    assert finding.line == 7
    assert finding.severity == "block"
    assert "AKIA" not in finding.title
    assert "AKIA" not in finding.evidence
    assert "AKIA" not in finding.fingerprint


def test_trivy_parsing(tmp_path: Path) -> None:
    payload = {
        "Results": [
            {
                "Target": "go.mod",
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "CVE-2024-0001",
                        "Severity": "CRITICAL",
                        "Title": "remote code execution",
                        "PkgName": "left-pad",
                    }
                ],
            }
        ]
    }
    exe, script = _emit(tmp_path, payload, "trivy")
    run = run_scanner(_spec(ScannerKind.TRIVY, exe, script, timeout_seconds=5), tmp_path)
    assert run.status is ScannerStatus.FINDINGS
    finding = run.findings[0]
    assert finding.rule_id == "CVE-2024-0001"
    assert finding.severity == "block"
    assert finding.path == "go.mod"


def test_non_json_output_is_scanner_invalid_output(tmp_path: Path) -> None:
    exe, script = _scanner(tmp_path, "print('totally not json')", "bad")
    run = run_scanner(_spec(ScannerKind.SEMGREP, exe, script, timeout_seconds=5), tmp_path)
    assert run.status is ScannerStatus.FAILED
    assert run.diagnostic_code == "scanner_invalid_output"
    assert run.findings == []


def test_hostile_payload_shapes_do_not_crash(tmp_path: Path) -> None:
    for index, payload in enumerate(
        [
            {"results": "not-a-list"},
            {"results": [None, 1, "x", []]},
            [1, 2, 3],
            {"findings": [{"File": None, "StartLine": "nope"}]},
            {"Results": [{"Vulnerabilities": {"not": "a list"}}]},
        ]
    ):
        for kind in (ScannerKind.SEMGREP, ScannerKind.GITLEAKS, ScannerKind.TRIVY):
            exe, script = _emit(tmp_path, payload, f"hostile_{kind.value}_{index}")
            run = run_scanner(_spec(kind, exe, script, timeout_seconds=5), tmp_path)
            assert run.status in {ScannerStatus.SUCCEEDED, ScannerStatus.FINDINGS}
            assert run.findings == [] or all(f.path for f in run.findings)


def test_empty_output_is_clean(tmp_path: Path) -> None:
    exe, script = _scanner(tmp_path, "pass", "silent")
    run = run_scanner(_spec(ScannerKind.SEMGREP, exe, script, timeout_seconds=5), tmp_path)
    assert run.status is ScannerStatus.SUCCEEDED
    assert run.diagnostic_code == "scanner_clean"


def test_timeout_is_timed_out_with_scanner_timeout_code(tmp_path: Path) -> None:
    exe, script = _scanner(tmp_path, "import time; time.sleep(3)", "slow")
    run = run_scanner(_spec(ScannerKind.SEMGREP, exe, script, timeout_seconds=0.2), tmp_path)
    assert run.status is ScannerStatus.TIMED_OUT
    assert run.diagnostic_code == "scanner_timeout"
    assert run.findings == []


def test_missing_executable_is_unavailable(tmp_path: Path) -> None:
    spec = ScannerSpec(ScannerKind.SEMGREP, "/no/such/scanner-binary", (), timeout_seconds=5)
    run = run_scanner(spec, tmp_path)
    assert run.status is ScannerStatus.UNAVAILABLE
    assert run.diagnostic_code == "scanner_unavailable"


def test_output_limit_gate(tmp_path: Path) -> None:
    exe, script = _scanner(tmp_path, "import sys; sys.stdout.write('x' * 5000)", "chatty")
    run = run_scanner(
        _spec(ScannerKind.SEMGREP, exe, script, timeout_seconds=5, output_limit_bytes=1000),
        tmp_path,
    )
    assert run.status is ScannerStatus.FAILED
    assert run.diagnostic_code == "scanner_output_limit"


def test_native_command_allowlist_gate(tmp_path: Path) -> None:
    exe, script = _scanner(tmp_path, "print('{}')", "native")
    blocked = run_scanner(
        _spec(ScannerKind.NATIVE, exe, script, allowlisted_commands=frozenset()), tmp_path
    )
    assert blocked.status is ScannerStatus.UNAVAILABLE
    assert blocked.diagnostic_code == "native_command_not_allowlisted"


def test_non_zero_exit_beyond_one_is_scanner_failed(tmp_path: Path) -> None:
    exe, script = _scanner(tmp_path, "import sys; print('{}'); sys.exit(2)", "crash")
    run = run_scanner(_spec(ScannerKind.SEMGREP, exe, script, timeout_seconds=5), tmp_path)
    assert run.status is ScannerStatus.FAILED
    assert run.diagnostic_code == "scanner_failed"


def test_exit_code_one_is_still_parsed(tmp_path: Path) -> None:
    exe, script = _scanner(tmp_path, "import sys; print('{\"results\": []}'); sys.exit(1)", "rc1")
    run = run_scanner(_spec(ScannerKind.SEMGREP, exe, script, timeout_seconds=5), tmp_path)
    assert run.status is ScannerStatus.SUCCEEDED
    assert run.diagnostic_code == "scanner_clean"


def test_one_scanner_failing_never_affects_another(tmp_path: Path) -> None:
    slow_exe, slow_script = _scanner(tmp_path, "import time; time.sleep(3)", "iso_slow")
    ok_exe, ok_script = _emit(
        tmp_path,
        {
            "results": [
                {
                    "path": "src/app.py",
                    "start": {"line": 2},
                    "check_id": "rule",
                    "extra": {"severity": "WARNING", "message": "m"},
                }
            ]
        },
        "iso_ok",
    )
    runs = run_scanners(
        [
            _spec(ScannerKind.SEMGREP, slow_exe, slow_script, timeout_seconds=0.2),
            _spec(ScannerKind.SEMGREP, ok_exe, ok_script, timeout_seconds=5),
        ],
        tmp_path,
    )
    assert runs[0].status is ScannerStatus.TIMED_OUT
    assert runs[1].status is ScannerStatus.FINDINGS
    assert runs[1].findings[0].line == 2


def test_caller_supplied_secret_is_redacted_before_parsing(tmp_path: Path) -> None:
    exe, script = _emit(
        tmp_path,
        {
            "results": [
                {
                    "path": "src/app.py",
                    "start": {"line": 1},
                    "check_id": "rule",
                    "extra": {"message": "leaked hunter2token value"},
                }
            ]
        },
        "secretful",
    )
    run = run_scanner(
        _spec(ScannerKind.SEMGREP, exe, script, timeout_seconds=5),
        tmp_path,
        secrets=["hunter2token"],
    )
    assert "hunter2token" not in run.model_dump_json()


def test_invalid_config_is_rejected_before_spawning(tmp_path: Path) -> None:
    exe, script = _scanner(tmp_path, "print('{}')", "cfg")
    run = run_scanner(_spec(ScannerKind.SEMGREP, exe, script, timeout_seconds=0), tmp_path)
    assert run.status is ScannerStatus.FAILED
    assert run.diagnostic_code == "scanner_config_invalid"
