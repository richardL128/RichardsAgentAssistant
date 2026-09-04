from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.agents.code_review.contracts import ValidatedFinding
from app.agents.code_review.operations import actionable_findings
from app.db.code_review import CodeReviewRepository, SQLAlchemyCodeReviewOperationsStore
from app.db.models import (
    AgentRun,
    Base,
    CodeRepository,
    DailyReviewReport,
    RepositoryProfile,
    ReviewedCommit,
    ReviewFindingDismissal,
)
from app.db.repositories import RunRepository


def _engine(tmp_path: Path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'phase4.db'}")
    Base.metadata.create_all(engine)
    return engine


def _seed_review(engine, *, created_at: datetime) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    with Session(engine) as session, session.begin():
        run = RunRepository.create_or_get(
            session,
            idempotency_key="code-review:acme/widget:" + "b" * 40,
            agent_name="code_review",
            trigger="github_push",
        )
        repository = CodeRepository(
            full_name="acme/widget",
            clone_url="https://github.com/acme/widget.git",
            default_branch="main",
            installation_id=7,
            allowlist_version="allow-v1",
        )
        session.add(repository)
        session.flush()
        commit = ReviewedCommit(
            repository_id=repository.id,
            run_id=run.id,
            delivery_id="delivery-1",
            ref="refs/heads/main",
            base_sha="a" * 40,
            head_sha="b" * 40,
            status="succeeded",
            risk="high",
            created_at=created_at,
        )
        session.add(commit)
        session.flush()
        CodeReviewRepository.persist_findings(
            session,
            reviewed_commit_id=commit.id,
            findings=[
                ValidatedFinding(
                    fingerprint="c" * 64,
                    severity="important",
                    path="app/auth.py",
                    line=12,
                    title="Reject the unsafe authentication fallback",
                    explanation="The fallback accepts an invalid credential.",
                    reproduction_or_missing_test="Add a test with an invalid credential.",
                    confidence=0.98,
                    assumptions=[],
                    evidence_refs=["diff:app/auth.py:12"],
                )
            ],
        )
        return repository.id, commit.id, run.id


def test_operations_store_lists_history_and_filters_a_dismissed_finding(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    occurred_at = datetime(2026, 9, 3, 16, tzinfo=UTC)
    repository_id, commit_id, run_id = _seed_review(engine, created_at=occurred_at)
    store = SQLAlchemyCodeReviewOperationsStore(engine)

    commits = store.list_commits_between(
        occurred_at - timedelta(hours=1), occurred_at + timedelta(hours=1), limit=10
    )
    records = store.list_findings_for_commits([commit_id])
    visible = actionable_findings(records, commits=commits, is_dismissed=store.is_finding_dismissed)
    assert len(visible) == 1

    with Session(engine) as session, session.begin():
        first = CodeReviewRepository.dismiss_finding(
            session,
            repository_id=repository_id,
            fingerprint="c" * 64,
            reason_code="false_positive",
            reason="The fixture intentionally accepts this credential.",
            dismissed_by="local-user",
            run_id=run_id,
        )
        replay = CodeReviewRepository.dismiss_finding(
            session,
            repository_id=repository_id,
            fingerprint="c" * 64,
            reason_code="different-replay-value",
            dismissed_by="local-user",
            run_id=run_id,
        )
        assert replay.id == first.id
    visible = actionable_findings(records, commits=commits, is_dismissed=store.is_finding_dismissed)
    assert len(visible) == 0
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(ReviewFindingDismissal)) == 1


def test_daily_report_store_is_idempotent_and_finishes_the_run(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with Session(engine) as session, session.begin():
        run = RunRepository.create_or_get(
            session,
            idempotency_key="code-review-daily:2026-09-03:v1",
            agent_name="code_review_daily",
            trigger="schedule",
            schedule="code-review-daily",
        )
        run_id = run.id
    store = SQLAlchemyCodeReviewOperationsStore(engine)
    first = store.create_daily_report(
        report_date=date(2026, 9, 3), run_id=run_id, schedule_name="code-review-daily"
    )
    replay = store.create_daily_report(
        report_date=date(2026, 9, 3), run_id=run_id, schedule_name="code-review-daily"
    )
    assert replay.id == first.id

    store.finish_daily_report(
        first.id,
        status="succeeded",
        commit_count=2,
        repository_count=1,
        finding_counts={"block": 0, "important": 1, "suggestion": 0},
        artifact_key="d" * 64,
        delivery_id=None,
    )
    with Session(engine) as session:
        report = session.get(DailyReviewReport, first.id)
        run = session.get(AgentRun, run_id)
        assert report is not None
        assert report.generated_at is not None
        assert report.finding_counts["important"] == 1
        assert run is not None
        assert run.status == "succeeded"
        assert run.artifact_key == "d" * 64


def test_profile_review_and_refresh_state_are_queryable(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    repository_id, _, _ = _seed_review(engine, created_at=datetime(2026, 8, 1, 12, tzinfo=UTC))
    with Session(engine) as session, session.begin():
        repository = session.get(CodeRepository, repository_id)
        assert repository is not None
        repository.profile_state = "profiled"
        repository.profiled_at = datetime(2026, 7, 1, 12, tzinfo=UTC)
        profile = RepositoryProfile(
            repository_id=repository_id,
            commit_sha="b" * 40,
            profile_version="profile-v1",
            summary="Fixture profile",
            artifact_key="e" * 64,
            instruction_provenance=[],
        )
        session.add(profile)
        session.flush()
        reviewed = CodeReviewRepository.mark_profile_reviewed(session, profile_id=profile.id)
        assert reviewed.reviewed is True
        due = CodeReviewRepository.profiles_due_for_refresh(
            session,
            as_of=datetime(2026, 9, 3, 12, tzinfo=UTC),
            refresh_after=timedelta(days=30),
        )
        assert [item.id for item in due] == [repository_id]
