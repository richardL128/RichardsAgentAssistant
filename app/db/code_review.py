"""Transaction-friendly persistence for the Phase 3 code-review pipeline.

Only normalized metadata, validated findings, and artifact references cross
this boundary. Repository contents, diffs, scanner output, and model text are
kept out of PostgreSQL and belong in the redacting artifact store.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, NoResultFound
from sqlalchemy.orm import Session

from app.agents.code_review.contracts import ProjectProfile, PushEvent, ValidatedFinding
from app.db.models import (
    CodeRepository,
    RepositoryProfile,
    ReviewedCommit,
    ReviewFinding,
    RunStatus,
)
from app.db.repositories import AuditRepository, RunRepository, utc_now

ReviewStatus = Literal["queued", "running", "succeeded", "attention", "failed", "cancelled"]


@dataclass(frozen=True, slots=True)
class ReviewIntake:
    """Durable identity returned after accepting or replaying a push."""

    repository_id: uuid.UUID
    reviewed_commit_id: uuid.UUID
    run_id: uuid.UUID
    idempotency_key: str
    created: bool
    status: str


def review_idempotency_key(repository: str, head_sha: str) -> str:
    """Return the stable repository/SHA identity used by DB and queue layers."""

    return f"code-review:{repository}:{head_sha}"


class CodeReviewRepository:
    """Persist code-review lifecycle records without committing transactions."""

    @staticmethod
    def accept_push(
        session: Session,
        *,
        event: PushEvent,
        allowlist_version: str,
        model_version: str,
        config_version: str,
    ) -> ReviewIntake:
        """Atomically accept one repository/SHA and collapse webhook replays."""

        if not allowlist_version.strip():
            raise ValueError("allowlist_version must not be empty")
        repository = CodeReviewRepository._upsert_repository(
            session,
            event=event,
            allowlist_version=allowlist_version,
        )
        existing_delivery = session.scalar(
            select(ReviewedCommit).where(ReviewedCommit.delivery_id == event.delivery_id)
        )
        if existing_delivery is not None and (
            existing_delivery.repository_id != repository.id
            or existing_delivery.head_sha != event.after_sha
        ):
            raise ValueError("GitHub delivery ID was already used for another push")

        key = review_idempotency_key(event.repository, event.after_sha)
        run = RunRepository.create_or_get(
            session,
            idempotency_key=key,
            agent_name="code_review",
            trigger="github_push",
            model_version=model_version,
            config_version=config_version,
            input_version=allowlist_version,
        )
        existing = session.scalar(
            select(ReviewedCommit).where(
                ReviewedCommit.repository_id == repository.id,
                ReviewedCommit.head_sha == event.after_sha,
            )
        )
        created = existing is None
        if existing is None:
            existing = ReviewedCommit(
                repository_id=repository.id,
                run_id=run.id,
                delivery_id=event.delivery_id,
                ref=event.ref,
                base_sha=event.before_sha,
                head_sha=event.after_sha,
            )
            try:
                with session.begin_nested():
                    session.add(existing)
                    session.flush()
            except IntegrityError:
                existing = session.scalar(
                    select(ReviewedCommit).where(
                        ReviewedCommit.repository_id == repository.id,
                        ReviewedCommit.head_sha == event.after_sha,
                    )
                )
                if existing is None:
                    conflicting_delivery = session.scalar(
                        select(ReviewedCommit).where(
                            ReviewedCommit.delivery_id == event.delivery_id
                        )
                    )
                    if conflicting_delivery is not None:
                        raise ValueError(
                            "GitHub delivery ID was already used for another push"
                        ) from None
                    raise
                created = False

        if existing.run_id != run.id:
            raise RuntimeError("repository/SHA is associated with an inconsistent run")
        if created:
            AuditRepository.append(
                session,
                actor="github-webhook",
                action="code_review.queued",
                target_type="reviewed_commit",
                target_id=str(existing.id),
                result="accepted",
                run_id=run.id,
            )
        return ReviewIntake(
            repository_id=repository.id,
            reviewed_commit_id=existing.id,
            run_id=run.id,
            idempotency_key=key,
            created=created,
            status=str(existing.status),
        )

    @staticmethod
    def _upsert_repository(
        session: Session,
        *,
        event: PushEvent,
        allowlist_version: str,
    ) -> CodeRepository:
        values = {
            "full_name": event.repository,
            "clone_url": event.clone_url,
            "default_branch": event.default_branch,
            "installation_id": event.installation_id,
            "allowlist_version": allowlist_version,
            "enabled": True,
        }
        if session.get_bind().dialect.name == "postgresql":
            statement = (
                pg_insert(CodeRepository)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["full_name"],
                    set_={
                        "clone_url": event.clone_url,
                        "default_branch": event.default_branch,
                        "installation_id": event.installation_id,
                        "allowlist_version": allowlist_version,
                        "updated_at": utc_now(),
                    },
                    where=CodeRepository.enabled.is_(True),
                )
                .returning(CodeRepository.id)
            )
            repository_id = session.scalar(statement)
            if repository_id is None:
                raise PermissionError("repository is disabled")
            repository = session.get(CodeRepository, repository_id)
            if repository is None:
                raise RuntimeError("repository upsert returned an unknown row")
            return repository

        repository = session.scalar(
            select(CodeRepository).where(CodeRepository.full_name == event.repository)
        )
        if repository is None:
            repository = CodeRepository(**values)
            session.add(repository)
        else:
            if not repository.enabled:
                raise PermissionError("repository is disabled")
            for name, value in values.items():
                if name != "enabled":
                    setattr(repository, name, value)
        session.flush()
        return repository

    @staticmethod
    def get_commit_for_run(session: Session, run_id: uuid.UUID) -> ReviewedCommit:
        commit = session.scalar(select(ReviewedCommit).where(ReviewedCommit.run_id == run_id))
        if commit is None:
            raise NoResultFound(f"reviewed commit for run {run_id} was not found")
        return commit

    @staticmethod
    def start(session: Session, run_id: uuid.UUID) -> ReviewedCommit:
        commit = CodeReviewRepository.get_commit_for_run(session, run_id)
        if commit.status in {"succeeded", "attention", "failed", "cancelled"}:
            return commit
        now = utc_now()
        commit.status = "running"
        commit.started_at = commit.started_at or now
        commit.updated_at = now
        RunRepository.set_status(session, run_id, RunStatus.RUNNING)
        session.flush()
        return commit

    @staticmethod
    def persist_findings(
        session: Session,
        *,
        reviewed_commit_id: uuid.UUID,
        findings: tuple[ValidatedFinding, ...] | list[ValidatedFinding],
    ) -> list[ReviewFinding]:
        """Insert validated findings once by deterministic fingerprint."""

        persisted: list[ReviewFinding] = []
        for finding in findings:
            values = {
                "reviewed_commit_id": reviewed_commit_id,
                "fingerprint": finding.fingerprint,
                "severity": finding.severity,
                "path": finding.path,
                "line": finding.line,
                "title": finding.title,
                "explanation": finding.explanation,
                "reproduction_or_missing_test": finding.reproduction_or_missing_test,
                "confidence": finding.confidence,
                "assumptions": finding.assumptions,
                "evidence_refs": finding.evidence_refs,
                "published_inline": False,
            }
            existing = session.scalar(
                select(ReviewFinding).where(
                    ReviewFinding.reviewed_commit_id == reviewed_commit_id,
                    ReviewFinding.fingerprint == finding.fingerprint,
                )
            )
            if existing is None:
                existing = ReviewFinding(**values)
                try:
                    with session.begin_nested():
                        session.add(existing)
                        session.flush()
                except IntegrityError:
                    existing = session.scalar(
                        select(ReviewFinding).where(
                            ReviewFinding.reviewed_commit_id == reviewed_commit_id,
                            ReviewFinding.fingerprint == finding.fingerprint,
                        )
                    )
                    if existing is None:
                        raise
            persisted.append(existing)
        return persisted

    @staticmethod
    def finish(
        session: Session,
        *,
        run_id: uuid.UUID,
        status: ReviewStatus,
        risk: str,
        summary: str,
        report_artifact_key: str | None = None,
        error_code: str | None = None,
    ) -> ReviewedCommit:
        if status not in {"succeeded", "attention", "failed", "cancelled"}:
            raise ValueError("finish requires a terminal review status")
        if risk not in {"high", "medium", "low"}:
            raise ValueError("invalid review risk")
        commit = CodeReviewRepository.get_commit_for_run(session, run_id)
        if commit.status in {"succeeded", "attention", "failed", "cancelled"}:
            return commit
        now = utc_now()
        commit.status = status
        commit.risk = risk
        commit.report_artifact_key = report_artifact_key
        commit.error_code = error_code
        commit.finished_at = now
        commit.updated_at = now
        run_status = {
            "succeeded": RunStatus.SUCCEEDED,
            "attention": RunStatus.ATTENTION,
            "failed": RunStatus.FAILED,
            "cancelled": RunStatus.CANCELLED,
        }[status]
        RunRepository.set_status(
            session,
            run_id,
            run_status,
            summary=summary,
            error_code=error_code,
            artifact_key=report_artifact_key,
        )
        AuditRepository.append(
            session,
            actor="code-review-worker",
            action=f"code_review.{status}",
            target_type="reviewed_commit",
            target_id=str(commit.id),
            result=status,
            run_id=run_id,
            artifact_key=report_artifact_key,
        )
        session.flush()
        return commit

    @staticmethod
    def persist_profile(
        session: Session,
        *,
        repository_id: uuid.UUID,
        profile_version: str,
        profile: ProjectProfile,
        artifact_key: str,
    ) -> RepositoryProfile:
        if not profile_version.strip():
            raise ValueError("profile_version must not be empty")
        values = {
            "repository_id": repository_id,
            "commit_sha": profile.commit_sha,
            "profile_version": profile_version,
            "summary": profile.purpose,
            "artifact_key": artifact_key,
            "instruction_provenance": profile.instruction_files,
            "reviewed": profile.reviewed,
        }
        existing = session.scalar(
            select(RepositoryProfile).where(
                RepositoryProfile.repository_id == repository_id,
                RepositoryProfile.commit_sha == profile.commit_sha,
                RepositoryProfile.profile_version == profile_version,
            )
        )
        if existing is None:
            existing = RepositoryProfile(**values)
            try:
                with session.begin_nested():
                    session.add(existing)
                    session.flush()
            except IntegrityError:
                existing = session.scalar(
                    select(RepositoryProfile).where(
                        RepositoryProfile.repository_id == repository_id,
                        RepositoryProfile.commit_sha == profile.commit_sha,
                        RepositoryProfile.profile_version == profile_version,
                    )
                )
                if existing is None:
                    raise
        return existing


__all__ = [
    "CodeReviewRepository",
    "ReviewIntake",
    "ReviewStatus",
    "review_idempotency_key",
]
