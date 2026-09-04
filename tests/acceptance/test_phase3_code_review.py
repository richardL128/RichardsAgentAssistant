"""Phase 3 acceptance: a real local Git repository through code review."""

from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session

from app.agents.code_review.contracts import (
    FindingBatch,
    FindingProposal,
    PushEvent,
    ScannerKind,
)
from app.agents.code_review.scanners import ScannerSpec
from app.agents.code_review.workflow import run_code_review
from app.artifacts.store import ArtifactStore
from app.connectors.discord import (
    DISCORD_API_BASE_URL,
    DiscordReviewSummaryAdapter,
    deliver_review_summary,
)
from app.core.config import Settings
from app.db.code_review import CodeReviewRepository
from app.db.models import AgentRun, Base, Delivery, DeliveryStatus, ReviewedCommit, ReviewFinding
from app.llm.contracts import InvocationStatus

REPOSITORY = "acme/review-fixture"
CHANNEL_ID = "123456789012345678"
SECRET = "test-github-token-DO-NOT-USE-123"
FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "code_review"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["/usr/bin/git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _fixture_repository(tmp_path: Path) -> tuple[Path, str, str]:
    """Build base -> review head -> moving branch tip from committed snapshots."""

    repo = tmp_path / "review-source"
    shutil.copytree(FIXTURE_ROOT / "base", repo)
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.email", "acceptance@example.invalid")
    _git(repo, "config", "user.name", "Phase 3 acceptance")
    base_sha = _commit(repo, "base")

    shutil.copy2(
        FIXTURE_ROOT / "changes" / "regression" / "src" / "account.py",
        repo / "src" / "account.py",
    )
    shutil.copy2(
        FIXTURE_ROOT / "changes" / "secret" / "tests" / "test_secrets.py",
        repo / "tests" / "test_secrets.py",
    )
    _commit(repo, "seed regression and test secret")

    shutil.copy2(
        FIXTURE_ROOT / "changes" / "docs" / "docs" / "release.md",
        repo / "docs" / "release.md",
    )
    review_head = _commit(repo, "document the fixture")

    # The source branch moves after the push under review.  The scanner below
    # emits a sentinel if this tip is checked out instead of review_head.
    shutil.copy2(
        FIXTURE_ROOT / "changes" / "tip" / "docs" / "release.md",
        repo / "docs" / "release.md",
    )
    _commit(repo, "move branch after push")
    return repo, base_sha, review_head


def _engine(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'acceptance.db'}")
    Base.metadata.create_all(engine)
    return engine


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path / 'acceptance.db'}",
        artifact_root=tmp_path / "artifacts",
        repository_allowlist=[REPOSITORY],
        repository_allowlist_version="acceptance-v1",
        discord_code_review_channel_id=CHANNEL_ID,
    )


def _accept_push(
    engine: Engine,
    *,
    base_sha: str,
    head_sha: str,
    delivery_id: str,
) -> Any:
    event = PushEvent(
        delivery_id=delivery_id,
        repository=REPOSITORY,
        clone_url=f"https://github.com/{REPOSITORY}.git",
        default_branch="main",
        installation_id=7,
        ref="refs/heads/main",
        before_sha=base_sha,
        after_sha=head_sha,
        received_at=datetime.now(UTC),
    )
    with Session(engine) as session, session.begin():
        return CodeReviewRepository.accept_push(
            session,
            event=event,
            allowlist_version="acceptance-v1",
            model_version="fake-acceptance-model",
            config_version="acceptance-v1",
        )


def _scanner_script(tmp_path: Path) -> tuple[str, str]:
    script = tmp_path / "fake-gitleaks.py"
    script.write_text(
        """
import json
import sys
from pathlib import Path

checkout = Path(sys.argv[-1])
secret = {
    "RuleID": "test-token",
    "File": "tests/test_secrets.py",
    "StartLine": 1,
    "Secret": "test-github-token-DO-NOT-USE-123",
    "Match": "TEST_GITHUB_TOKEN = test-github-token-DO-NOT-USE-123",
}
records = [secret, secret]
if "TIP_ONLY_MARKER" in (checkout / "docs/release.md").read_text():
    records.append({"RuleID": "tip-only-branch", "File": "docs/release.md", "StartLine": 3})
print(json.dumps(records))
""".strip()
        + "\n"
    )
    return sys.executable, str(script)


def _regression_proposal() -> FindingProposal:
    return FindingProposal(
        finding_present=True,
        severity="important",
        path="src/account.py",
        line=6,
        title="Shell command injection in account closing",
        explanation=(
            "The changed command joins the account identifier into a shell string, "
            "allowing attacker-controlled metacharacters to execute arbitrary commands."
        ),
        reproduction_or_missing_test=(
            "Add a regression test with shell metacharacters and assert the subprocess "
            "receives one validated argument."
        ),
        confidence=0.91,
        assumptions=["Account identifiers can contain untrusted input."],
        evidence_refs=["diff:src/account.py:6"],
    )


def _doc_speculation() -> FindingProposal:
    return FindingProposal(
        finding_present=True,
        severity="suggestion",
        path="docs/release.md",
        line=3,
        title="The documentation might need improvement",
        explanation="This could perhaps be clearer for maintainers in some circumstances.",
        reproduction_or_missing_test="Maybe add a test for the documentation wording.",
        confidence=0.95,
        assumptions=[],
        evidence_refs=["diff:docs/release.md:3"],
    )


class _FakeGateway:
    def __init__(self) -> None:
        duplicate = _regression_proposal().model_copy(
            update={
                "title": "Shell injection reaches account closing",
                "confidence": 0.94,
                "assumptions": ["The account endpoint accepts attacker input."],
            }
        )
        self.batch = FindingBatch(
            findings=[_regression_proposal(), duplicate, _doc_speculation()],
            review_summary="Review fixture contains a regression, a test secret, and docs.",
        )
        self.prompts: list[str] = []

    async def invoke_structured(self, *, prompt: str, response_model: type[Any]) -> Any:
        assert response_model is FindingBatch
        self.prompts.append(prompt)
        return SimpleNamespace(status=InvocationStatus.VALID, output=self.batch)


class _RecordingDiscordSender:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []

    def _transport(self) -> httpx.MockTransport:
        def respond(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(
                200,
                json={"id": "987654321012345678", "guild_id": "42424242424242424"},
            )

        return httpx.MockTransport(respond)

    async def __call__(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        async with httpx.AsyncClient(
            base_url=DISCORD_API_BASE_URL, transport=self._transport()
        ) as client:
            adapter = DiscordReviewSummaryAdapter(
                token=SecretStr("fake-discord-token"),
                allowed_channel_ids={CHANNEL_ID},
                client=client,
            )
            return await deliver_review_summary(adapter=adapter, **kwargs)


async def test_phase3_repository_review_is_exact_sha_redacted_deduplicated_and_report_only(
    tmp_path: Path,
) -> None:
    source, base_sha, review_head = _fixture_repository(tmp_path)
    engine = _engine(tmp_path)
    accepted = _accept_push(
        engine, base_sha=base_sha, head_sha=review_head, delivery_id="push-first"
    )
    replay = _accept_push(
        engine, base_sha=base_sha, head_sha=review_head, delivery_id="push-replay"
    )
    assert accepted.created is True
    assert replay.created is False
    assert replay.run_id == accepted.run_id
    assert replay.reviewed_commit_id == accepted.reviewed_commit_id

    executable, script = _scanner_script(tmp_path)
    scanner = ScannerSpec(
        ScannerKind.GITLEAKS, executable, (script,), timeout_seconds=5, output_limit_bytes=50_000
    )
    gateway = _FakeGateway()
    sender = _RecordingDiscordSender()
    store = ArtifactStore(tmp_path / "artifacts")

    result = await run_code_review(
        str(accepted.run_id),
        accepted.idempotency_key,
        settings=_settings(tmp_path),
        engine=engine,
        gateway=gateway,
        store=store,
        scanner_specs=[scanner],
        checkout_source=source,
        sender=sender,
    )

    assert result["status"] == "succeeded"
    assert result["head_sha"] == review_head
    assert result["total_findings"] == 2
    assert result["finding_counts"] == {"block": 1, "important": 1, "suggestion": 0}
    assert result["degraded_scanners"] == []
    assert result["delivered"] == "sent"
    assert len(gateway.prompts) == 1
    assert "Review packet" in gateway.prompts[0]
    assert SECRET not in gateway.prompts[0]
    assert "tip-only-branch" not in gateway.prompts[0]

    with Session(engine) as session:
        findings = session.scalars(
            select(ReviewFinding).where(
                ReviewFinding.reviewed_commit_id == accepted.reviewed_commit_id
            )
        ).all()
        assert {(finding.path, finding.line, finding.severity) for finding in findings} == {
            ("tests/test_secrets.py", 1, "block"),
            ("src/account.py", 6, "important"),
        }
        assert len(findings) == 2  # duplicate model proposal and duplicate scanner record collapsed
        assert all(finding.published_inline is False for finding in findings)
        assert session.scalar(select(func.count()).select_from(ReviewedCommit)) == 1
        assert session.scalar(select(func.count()).select_from(AgentRun)) == 1
        delivery = session.scalar(select(Delivery))
        assert delivery is not None
        assert delivery.status == DeliveryStatus.SENT
        assert delivery.attempt_count == 1

    report = store.get(result["report_artifact_key"]).decode("utf-8")
    assert "src/account.py:6" in report
    assert "tests/test_secrets.py:1" in report
    assert "docs/release.md" not in report
    assert SECRET not in report
    assert len(sender.requests) == 1
    assert SECRET not in sender.requests[0].content.decode("utf-8")

    # A completed run is a durable replay no-op, and the delivery helper also
    # returns its SENT intent without posting a second Discord message.
    replay_result = await run_code_review(
        str(accepted.run_id),
        accepted.idempotency_key,
        settings=_settings(tmp_path),
        engine=engine,
        gateway=gateway,
        store=store,
        scanner_specs=[scanner],
        checkout_source=source,
        sender=sender,
    )
    assert replay_result == {
        "status": "succeeded",
        "run_id": str(accepted.run_id),
        "note": "already_complete",
    }
    assert len(gateway.prompts) == 1
    assert len(sender.calls) == 1
    assert len(sender.requests) == 1

    async with httpx.AsyncClient(
        base_url=DISCORD_API_BASE_URL,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"id": "unused", "guild_id": "unused"})
        ),
    ) as client:
        adapter = DiscordReviewSummaryAdapter(
            token=SecretStr("fake-discord-token"),
            allowed_channel_ids={CHANNEL_ID},
            client=client,
        )
        delivery = await deliver_review_summary(
            engine=engine,
            run_id=accepted.run_id,
            channel_id=CHANNEL_ID,
            repository=REPOSITORY,
            head_sha=review_head,
            risk=result["risk"],
            status="succeeded",
            finding_counts=result["finding_counts"],
            report_artifact_key=result["report_artifact_key"],
            adapter=adapter,
        )
    assert delivery.status == DeliveryStatus.SENT
