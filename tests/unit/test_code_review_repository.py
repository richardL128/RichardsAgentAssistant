"""Security and bounding tests for the Phase 3 repository boundary."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.agents.code_review.contracts import RiskLevel, ScannerKind, ScannerStatus
from app.agents.code_review.packet import PacketAssemblyError, assemble_review_packet
from app.agents.code_review.repository import RepositoryCheckoutError, checkout_repository
from app.agents.code_review.risk import classify_risk
from app.agents.code_review.scanners import ScannerSpec, run_scanner, run_scanners


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["/usr/bin/git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _fixture_repo(tmp_path: Path) -> tuple[Path, str, str]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "--quiet")
    _git(source, "config", "user.email", "test@example.invalid")
    _git(source, "config", "user.name", "Test")
    (source / "README.md").write_text("initial\n")
    (source / "app.py").write_text("authorize()\n")
    _git(source, "add", ".")
    _git(source, "commit", "--quiet", "-m", "base")
    base = _git(source, "rev-parse", "HEAD")
    (source / "app.py").write_text("delete_account()\n")
    _git(source, "commit", "--quiet", "-am", "head")
    head = _git(source, "rev-parse", "HEAD")
    return source, base, head


def test_checkout_is_exact_sha_and_cleaned(tmp_path: Path) -> None:
    source, base, head = _fixture_repo(tmp_path)
    checkout_path: Path
    with checkout_repository(
        "acme/example",
        base,
        allowlist={"acme/example"},
        source=source,
        checkout_root=tmp_path / "checkouts",
    ) as checkout_path:
        assert _git(checkout_path, "rev-parse", "HEAD") == base
        assert _git(checkout_path, "rev-parse", "HEAD") != head
        assert checkout_path.exists()
    assert not checkout_path.exists()


def test_checkout_rejects_allowlist_traversal_and_bad_source(tmp_path: Path) -> None:
    source, base, _head = _fixture_repo(tmp_path)
    with (
        pytest.raises(RepositoryCheckoutError) as rejected,
        checkout_repository("acme/../example", base, allowlist={"acme/../example"}, source=source),
    ):
        pass
    assert rejected.value.diagnostic_code == "repository_not_allowlisted"
    with (
        pytest.raises(RepositoryCheckoutError) as rejected_source,
        checkout_repository(
            "acme/example",
            base,
            allowlist={"acme/example"},
            source="https://evil.invalid/acme/example",
        ),
    ):
        pass
    assert rejected_source.value.diagnostic_code == "checkout_source_rejected"


def test_checkout_failure_does_not_leave_directory(tmp_path: Path) -> None:
    root = tmp_path / "checkouts"
    with (
        pytest.raises(RepositoryCheckoutError) as failure,
        checkout_repository(
            "acme/example",
            "0" * 40,
            allowlist={"acme/example"},
            source=tmp_path / "missing",
            checkout_root=root,
        ),
    ):
        pass
    assert failure.value.diagnostic_code == "checkout_source_missing"
    assert not list(root.glob("lifeagent-review-*")) if root.exists() else True


def test_checkout_clone_or_checkout_failure_has_safe_code(tmp_path: Path) -> None:
    source, _base, _head = _fixture_repo(tmp_path)
    with (
        pytest.raises(RepositoryCheckoutError) as failure,
        checkout_repository(
            "acme/example",
            "0" * 40,
            allowlist={"acme/example"},
            source=source,
            timeout_seconds=2,
        ),
    ):
        pass
    assert failure.value.diagnostic_code == "checkout_clone_failed"

    with (
        pytest.raises(RepositoryCheckoutError) as timeout,
        checkout_repository(
            "acme/example", "0" * 40, allowlist={"acme/example"}, source=source, timeout_seconds=0
        ),
    ):
        pass
    assert timeout.value.diagnostic_code == "checkout_timeout"


def test_risk_is_deterministic_and_high_precedes_docs() -> None:
    assert classify_risk(["docs/README.md"])[0] is RiskLevel.LOW
    level, reasons = classify_risk(["docs/README.md", "src/auth/login.py"])
    assert level is RiskLevel.HIGH
    assert reasons == ["high-risk path marker: auth", "high-risk path marker: login"]


def test_packet_extracts_changed_lines_and_bounds(tmp_path: Path) -> None:
    source, base, head = _fixture_repo(tmp_path)
    packet = assemble_review_packet("acme/example", base, head, source)
    assert packet.files[0].path == "app.py"
    assert packet.files[0].changed_lines == [1]
    assert packet.files[0].additions == 1
    assert packet.files[0].deletions == 1
    with pytest.raises(PacketAssemblyError, match="packet_character_limit"):
        assemble_review_packet("acme/example", base, head, source, max_patch_chars=1)


def _fake_scanner(tmp_path: Path, body: str, name: str = "fake-scanner") -> tuple[str, str]:
    """Return an (executable, script) pair for a stub scanner process.

    The script is passed as an interpreter argument rather than made
    executable: pytest's ``tmp_path`` can live on a ``noexec`` mount, and the
    host is not guaranteed to have a system interpreter at a fixed path.
    """

    script = tmp_path / f"{name}.py"
    script.write_text(f"{body}\n")
    return sys.executable, str(script)


def test_scanners_redact_gitleaks_and_isolate_timeout(tmp_path: Path) -> None:
    output = (
        '{"findings":[{"RuleID":"token","File":"app.py","StartLine":1,"Secret":"DO_NOT_LEAK"}]}'
    )
    clean_exe, clean_script = _fake_scanner(tmp_path, f"print({output!r})", "clean-scanner")
    slow_exe, slow_script = _fake_scanner(tmp_path, "import time; time.sleep(2)", "slow-scanner")
    clean_spec = ScannerSpec(ScannerKind.GITLEAKS, clean_exe, (clean_script,), timeout_seconds=2)
    slow_spec = ScannerSpec(ScannerKind.SEMGREP, slow_exe, (slow_script,), timeout_seconds=0.05)
    runs = run_scanners([clean_spec, slow_spec], tmp_path, secrets=["DO_NOT_LEAK"])
    assert runs[0].status is ScannerStatus.FINDINGS
    assert "DO_NOT_LEAK" not in runs[0].model_dump_json()
    assert runs[1].status is ScannerStatus.TIMED_OUT
    assert runs[1].diagnostic_code == "scanner_timeout"


def test_native_commands_require_allowlist(tmp_path: Path) -> None:
    executable, script = _fake_scanner(tmp_path, "print('{}')")
    spec = ScannerSpec(ScannerKind.NATIVE, executable, (script,), allowlisted_commands=frozenset())
    result = run_scanner(spec, tmp_path)
    assert result.status is ScannerStatus.UNAVAILABLE
    assert result.diagnostic_code == "native_command_not_allowlisted"
