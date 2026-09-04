"""Composition tests for the durable code-review workflow."""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from app.agents.code_review.contracts import FindingBatch, ScannerKind
from app.agents.code_review.scanners import ScannerSpec
from app.agents.code_review.workflow import run_code_review
from app.core.config import Settings
from app.db.models import (
    AgentRun,
    Base,
    CodeRepository,
    ReviewedCommit,
    ReviewFinding,
    RunStatus,
)
from app.llm.contracts import InvocationStatus

REPOSITORY = "acme/example"
CHANNEL_ID = "123456789012345678"


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


def _fake_scanner(tmp_path: Path, body: str, name: str) -> tuple[str, str]:
    script = tmp_path / f"{name}.py"
    script.write_text(f"{body}\n")
    return sys.executable, str(script)


class _FakeGateway:
    def __init__(self, status: InvocationStatus, output: FindingBatch | None) -> None:
        self._result = SimpleNamespace(status=status, output=output)
        self.calls = 0

    async def invoke_structured(self, *, prompt: str, response_model: type[Any]) -> Any:
        self.calls += 1
        assert "Review packet" in prompt
        return self._result


class _RecordingSender:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(delivered=True)


def _engine(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'workflow.db'}")
    Base.metadata.create_all(engine)
    return engine


def _settings(tmp_path: Path, *, channel: str | None = None) -> Settings:
    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        artifact_root=tmp_path / "artifacts",
        repository_allowlist=[REPOSITORY],
        repository_allowlist_version="v1",
        discord_code_review_channel_id=channel,
    )


def _seed(engine: Engine, *, base_sha: str, head_sha: str) -> tuple[uuid.UUID, uuid.UUID]:
    run_id = uuid.uuid4()
    with Session(engine) as session, session.begin():
        session.add(
            AgentRun(
                id=run_id,
                idempotency_key=f"code-review:{REPOSITORY}:{head_sha}",
                agent_name="code_review",
                trigger="github_push",
                status=RunStatus.QUEUED,
            )
        )
        repository = CodeRepository(
            full_name=REPOSITORY,
            clone_url=f"https://github.com/{REPOSITORY}.git",
            default_branch="main",
            installation_id=1,
            allowlist_version="v1",
            enabled=True,
        )
        session.add(repository)
        session.flush()
        commit = ReviewedCommit(
            repository_id=repository.id,
            run_id=run_id,
            delivery_id=f"d-{run_id}",
            ref="refs/heads/main",
            base_sha=base_sha,
            head_sha=head_sha,
        )
        session.add(commit)
        session.flush()
        return run_id, commit.id


def _valid_batch() -> FindingBatch:
    return FindingBatch(findings=[], review_summary="model narrative")


async def test_successful_run_persists_findings_writes_report_and_delivers(tmp_path: Path) -> None:
    source, base, head = _fixture_repo(tmp_path)
    engine = _engine(tmp_path)
    run_id, commit_id = _seed(engine, base_sha=base, head_sha=head)
    scanner_exe, scanner_script = _fake_scanner(
        tmp_path,
        'print(\'{"findings":[{"RuleID":"leaked-token","File":"app.py","StartLine":1}]}\')',
        "gitleaks",
    )
    spec = ScannerSpec(ScannerKind.GITLEAKS, scanner_exe, (scanner_script,), timeout_seconds=5)
    gateway = _FakeGateway(InvocationStatus.VALID, _valid_batch())
    sender = _RecordingSender()

    result = await run_code_review(
        str(run_id),
        f"code-review:{REPOSITORY}:{head}",
        settings=_settings(tmp_path, channel=CHANNEL_ID),
        engine=engine,
        gateway=gateway,
        scanner_specs=[spec],
        checkout_source=source,
        sender=sender,
    )

    assert result["status"] == "succeeded"
    assert result["total_findings"] == 1
    assert result["finding_counts"] == {"block": 1, "important": 0, "suggestion": 0}
    assert result["report_artifact_key"]
    assert result["degraded_scanners"] == []
    assert result["delivered"] == "sent"

    assert gateway.calls == 1
    assert len(sender.calls) == 1
    call = sender.calls[0]
    assert call["channel_id"] == CHANNEL_ID
    assert call["status"] == "succeeded"
    assert call["repository"] == REPOSITORY
    assert call["report_artifact_key"] == result["report_artifact_key"]

    with Session(engine) as session:
        findings = session.scalars(
            select(ReviewFinding).where(ReviewFinding.reviewed_commit_id == commit_id)
        ).all()
        assert len(findings) == 1
        assert findings[0].severity == "block"
        commit = session.get(ReviewedCommit, commit_id)
        assert commit is not None
        assert commit.status == "succeeded"
        assert commit.report_artifact_key == result["report_artifact_key"]
        run = session.get(AgentRun, run_id)
        assert run is not None
        assert run.status == RunStatus.SUCCEEDED

    report = (
        tmp_path
        / "artifacts"
        / result["report_artifact_key"][:2]
        / result["report_artifact_key"][2:]
    )
    assert report.read_text().startswith("# Code review report")


async def test_returned_metadata_contains_no_patch_model_or_scanner_text(tmp_path: Path) -> None:
    source, base, head = _fixture_repo(tmp_path)
    engine = _engine(tmp_path)
    run_id, _commit_id = _seed(engine, base_sha=base, head_sha=head)
    scanner_exe, scanner_script = _fake_scanner(
        tmp_path,
        'print(\'{"findings":[{"RuleID":"SENTINEL_RULE","File":"app.py","StartLine":1}]}\')',
        "gitleaks",
    )
    spec = ScannerSpec(ScannerKind.GITLEAKS, scanner_exe, (scanner_script,), timeout_seconds=5)
    gateway = _FakeGateway(
        InvocationStatus.VALID,
        FindingBatch(findings=[], review_summary="SENTINEL_MODEL_TEXT"),
    )

    result = await run_code_review(
        str(run_id),
        f"code-review:{REPOSITORY}:{head}",
        settings=_settings(tmp_path),
        engine=engine,
        gateway=gateway,
        scanner_specs=[spec],
        checkout_source=source,
        sender=_RecordingSender(),
    )

    blob = json.dumps(result)
    assert "delete_account" not in blob
    assert "authorize" not in blob
    assert "SENTINEL_MODEL_TEXT" not in blob
    assert "SENTINEL_RULE" not in blob


async def test_scanner_timeout_yields_attention_and_does_not_abort_run(tmp_path: Path) -> None:
    source, base, head = _fixture_repo(tmp_path)
    engine = _engine(tmp_path)
    run_id, commit_id = _seed(engine, base_sha=base, head_sha=head)
    slow_exe, slow_script = _fake_scanner(tmp_path, "import time; time.sleep(2)", "slow")
    slow = ScannerSpec(ScannerKind.SEMGREP, slow_exe, (slow_script,), timeout_seconds=0.05)
    clean_exe, clean_script = _fake_scanner(tmp_path, "print('{}')", "clean")
    clean = ScannerSpec(ScannerKind.TRIVY, clean_exe, (clean_script,), timeout_seconds=5)
    gateway = _FakeGateway(InvocationStatus.VALID, _valid_batch())
    sender = _RecordingSender()

    result = await run_code_review(
        str(run_id),
        f"code-review:{REPOSITORY}:{head}",
        settings=_settings(tmp_path),
        engine=engine,
        gateway=gateway,
        scanner_specs=[slow, clean],
        checkout_source=source,
        sender=sender,
    )

    assert result["status"] == "attention"
    degraded = {entry["scanner"]: entry["diagnostic_code"] for entry in result["degraded_scanners"]}
    assert degraded == {"semgrep": "scanner_timeout"}
    assert result["report_artifact_key"]
    assert result["delivered"] == "skipped_no_channel"
    assert sender.calls == []

    with Session(engine) as session:
        commit = session.get(ReviewedCommit, commit_id)
        assert commit is not None
        assert commit.status == "attention"
        assert commit.error_code == "scanner_degraded"
        run = session.get(AgentRun, run_id)
        assert run is not None
        assert run.status == RunStatus.ATTENTION


async def test_clone_failure_yields_failed_with_checkout_diagnostic_and_no_delivery(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    base_sha, head_sha = "a" * 40, "b" * 40
    run_id, commit_id = _seed(engine, base_sha=base_sha, head_sha=head_sha)
    sender = _RecordingSender()
    gateway = _FakeGateway(InvocationStatus.VALID, _valid_batch())

    result = await run_code_review(
        str(run_id),
        f"code-review:{REPOSITORY}:{head_sha}",
        settings=_settings(tmp_path, channel=CHANNEL_ID),
        engine=engine,
        gateway=gateway,
        scanner_specs=[],
        checkout_source=tmp_path / "missing-checkout",
        sender=sender,
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "checkout_source_missing"
    assert result["report_artifact_key"] is None
    assert result["total_findings"] == 0
    assert sender.calls == []
    assert gateway.calls == 0

    with Session(engine) as session:
        commit = session.get(ReviewedCommit, commit_id)
        assert commit is not None
        assert commit.status == "failed"
        assert commit.error_code == "checkout_source_missing"
        run = session.get(AgentRun, run_id)
        assert run is not None
        assert run.status == RunStatus.FAILED


def test_importing_worker_registers_a_code_review_handler() -> None:
    import app.queue.worker  # noqa: F401
    from app.agents.code_review.workflow import run_code_review as workflow_handler
    from app.queue import tasks

    assert tasks._handlers.get("code_review") is workflow_handler


@pytest.mark.parametrize(
    "invalid_status", [InvocationStatus.INVALID_OUTPUT, InvocationStatus.FAILED]
)
async def test_invalid_model_output_drives_attention_without_failing_run(
    tmp_path: Path, invalid_status: InvocationStatus
) -> None:
    source, base, head = _fixture_repo(tmp_path)
    engine = _engine(tmp_path)
    run_id, commit_id = _seed(engine, base_sha=base, head_sha=head)
    gateway = _FakeGateway(invalid_status, None)

    result = await run_code_review(
        str(run_id),
        f"code-review:{REPOSITORY}:{head}",
        settings=_settings(tmp_path),
        engine=engine,
        gateway=gateway,
        scanner_specs=[],
        checkout_source=source,
        sender=_RecordingSender(),
    )

    assert result["status"] == "attention"
    assert result["model_output_valid"] is False
    with Session(engine) as session:
        commit = session.get(ReviewedCommit, commit_id)
        assert commit is not None
        assert commit.status == "attention"
        assert commit.error_code == "analysis_invalid_output"
