"""Transaction-friendly persistence for the Phase 3 code-review pipeline.

Only normalized metadata, validated findings, and artifact references cross
this boundary. Repository contents, diffs, scanner output, and model text are
kept out of PostgreSQL and belong in the redacting artifact store.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import Select, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, NoResultFound
from sqlalchemy.orm import Session

from app.agents.code_review.contracts import ProjectProfile, PushEvent, ValidatedFinding
from app.db.models import (
    CodeRepository,
    DailyReviewReport,
    RepositoryProfile,
    ReviewedCommit,
    ReviewFinding,
    ReviewFindingDismissal,
    RunStatus,
)
from app.db.repositories import AuditRepository, RunRepository, utc_now

ReviewStatus = Literal["queued", "running", "succeeded", "attention", "failed", "cancelled"]

if TYPE_CHECKING:
    from app.agents.code_review.operations import ReviewCommitRecord


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
        trigger: Literal["push", "quick_scan"] = "push",
        risk: Literal["high", "medium", "low"] = "medium",
    ) -> ReviewIntake:
        """Atomically accept one repository/SHA and collapse webhook replays."""

        if not allowlist_version.strip():
            raise ValueError("allowlist_version must not be empty")
        if trigger not in {"push", "quick_scan"}:
            raise ValueError("invalid push review trigger")
        if risk not in {"high", "medium", "low"}:
            raise ValueError("invalid push review risk")
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
                trigger=trigger,
                risk=risk,
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
        repository = session.get(CodeRepository, commit.repository_id)
        if repository is not None:
            repository.last_reviewed_at = now
            repository.updated_at = now
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

    @staticmethod
    def dismiss_finding(
        session: Session,
        *,
        repository_id: uuid.UUID,
        fingerprint: str,
        reason_code: str,
        dismissed_by: str,
        reason: str | None = None,
        run_id: uuid.UUID | None = None,
    ) -> ReviewFindingDismissal:
        """Persist one repository-scoped dismissal and its review reason."""

        invalid_fingerprint = len(fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in fingerprint
        )
        if invalid_fingerprint:
            raise ValueError("fingerprint must be a lowercase SHA-256 digest")
        if not reason_code.strip() or len(reason_code) > 64:
            raise ValueError("reason_code must be non-empty and at most 64 characters")
        if not dismissed_by.strip() or len(dismissed_by) > 255:
            raise ValueError("dismissed_by must be non-empty and at most 255 characters")
        if reason is not None and len(reason) > 2_000:
            raise ValueError("dismissal reason must be at most 2000 characters")
        existing = session.scalar(
            select(ReviewFindingDismissal).where(
                ReviewFindingDismissal.repository_id == repository_id,
                ReviewFindingDismissal.fingerprint == fingerprint,
            )
        )
        if existing is not None:
            return existing
        dismissal = ReviewFindingDismissal(
            repository_id=repository_id,
            fingerprint=fingerprint,
            reason_code=reason_code,
            reason=reason,
            dismissed_by=dismissed_by,
            run_id=run_id,
        )
        try:
            with session.begin_nested():
                session.add(dismissal)
                session.flush()
        except IntegrityError:
            winner = session.scalar(
                select(ReviewFindingDismissal).where(
                    ReviewFindingDismissal.repository_id == repository_id,
                    ReviewFindingDismissal.fingerprint == fingerprint,
                )
            )
            if winner is None:
                raise
            dismissal = winner
        AuditRepository.append(
            session,
            actor=dismissed_by,
            action="code_review.finding_dismissed",
            target_type="review_finding",
            target_id=fingerprint,
            result=reason_code,
            run_id=run_id,
        )
        return dismissal

    @staticmethod
    def mark_profile_reviewed(
        session: Session,
        *,
        profile_id: uuid.UUID,
        reviewed: bool = True,
    ) -> RepositoryProfile:
        """Record the human review state of a generated project profile."""

        profile = session.get(RepositoryProfile, profile_id)
        if profile is None:
            raise NoResultFound(f"repository profile {profile_id} was not found")
        profile.reviewed = reviewed
        profile.updated_at = utc_now()
        session.flush()
        return profile

    @staticmethod
    def profiles_due_for_refresh(
        session: Session,
        *,
        as_of: datetime,
        refresh_after: timedelta,
        limit: int = 100,
    ) -> list[CodeRepository]:
        """List enabled repositories whose durable profile checkpoint is stale."""

        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        if refresh_after <= timedelta(0):
            raise ValueError("refresh_after must be positive")
        if limit <= 0:
            raise ValueError("limit must be positive")
        threshold = as_of.astimezone(UTC) - refresh_after
        statement: Select[tuple[CodeRepository]] = (
            select(CodeRepository)
            .where(
                CodeRepository.enabled.is_(True),
                ((CodeRepository.profiled_at.is_(None)) | (CodeRepository.profiled_at < threshold)),
            )
            .order_by(CodeRepository.profiled_at.asc().nullsfirst(), CodeRepository.full_name)
            .limit(limit)
        )
        return list(session.scalars(statement))


def _aware_utc(value: datetime) -> datetime:
    """Normalize database timestamps, including SQLite's naive test values."""

    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class SQLAlchemyCodeReviewOperationsStore:
    """Session-scoped adapter for Phase 4 daily consolidation operations."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    def list_commits_between(
        self,
        start_utc: datetime,
        end_utc: datetime,
        *,
        limit: int,
    ) -> Sequence[ReviewCommitRecord]:
        if start_utc.tzinfo is None or end_utc.tzinfo is None:
            raise ValueError("daily report boundaries must be timezone-aware")
        if start_utc > end_utc:
            raise ValueError("daily report start must not be after its end")
        if limit <= 0:
            raise ValueError("limit must be positive")
        from app.agents.code_review.operations import ReviewCommitRecord

        statement = (
            select(ReviewedCommit, CodeRepository)
            .join(CodeRepository, CodeRepository.id == ReviewedCommit.repository_id)
            .where(
                ReviewedCommit.created_at >= start_utc,
                ReviewedCommit.created_at <= end_utc,
            )
            .order_by(ReviewedCommit.created_at, ReviewedCommit.head_sha)
            .limit(limit)
        )
        with Session(self._engine) as session:
            rows = session.execute(statement).all()
            return tuple(
                ReviewCommitRecord(
                    repository=repository.full_name,
                    repository_id=repository.id,
                    reviewed_commit_id=commit.id,
                    base_sha=commit.base_sha,
                    head_sha=commit.head_sha,
                    occurred_at=_aware_utc(commit.created_at),
                    status=commit.status,
                    risk=commit.risk,
                    trigger=commit.trigger,
                )
                for commit, repository in rows
            )

    def list_findings_for_commits(
        self,
        reviewed_commit_ids: Sequence[uuid.UUID | str],
    ) -> Sequence[Mapping[str, Any]]:
        identifiers = [
            identifier if isinstance(identifier, uuid.UUID) else uuid.UUID(str(identifier))
            for identifier in reviewed_commit_ids
        ]
        if not identifiers:
            return ()
        statement = (
            select(ReviewFinding, ReviewedCommit.repository_id)
            .join(ReviewedCommit, ReviewedCommit.id == ReviewFinding.reviewed_commit_id)
            .where(ReviewFinding.reviewed_commit_id.in_(identifiers))
            .order_by(
                ReviewFinding.reviewed_commit_id,
                ReviewFinding.path,
                ReviewFinding.line,
                ReviewFinding.fingerprint,
            )
        )
        with Session(self._engine) as session:
            return tuple(
                {
                    "reviewed_commit_id": finding.reviewed_commit_id,
                    "repository_id": repository_id,
                    "fingerprint": finding.fingerprint,
                    "severity": finding.severity,
                    "path": finding.path,
                    "line": finding.line,
                    "title": finding.title,
                    "explanation": finding.explanation,
                    "reproduction_or_missing_test": finding.reproduction_or_missing_test,
                    "confidence": finding.confidence,
                    "assumptions": tuple(finding.assumptions),
                    "evidence_refs": tuple(finding.evidence_refs),
                }
                for finding, repository_id in session.execute(statement)
            )

    def is_finding_dismissed(
        self,
        repository_id: uuid.UUID | str,
        fingerprint: str,
    ) -> bool:
        identifier = (
            repository_id if isinstance(repository_id, uuid.UUID) else uuid.UUID(str(repository_id))
        )
        with Session(self._engine) as session:
            return (
                session.scalar(
                    select(ReviewFindingDismissal.id).where(
                        ReviewFindingDismissal.repository_id == identifier,
                        ReviewFindingDismissal.fingerprint == fingerprint,
                    )
                )
                is not None
            )

    def get_daily_report(self, report_date: date) -> DailyReviewReport | None:
        with Session(self._engine) as session:
            report = session.scalar(
                select(DailyReviewReport).where(DailyReviewReport.report_date == report_date)
            )
            if report is not None:
                session.expunge(report)
            return report

    def create_daily_report(
        self,
        *,
        report_date: date,
        run_id: uuid.UUID,
        schedule_name: str,
    ) -> DailyReviewReport:
        with Session(self._engine) as session, session.begin():
            report = session.scalar(
                select(DailyReviewReport).where(DailyReviewReport.report_date == report_date)
            )
            if report is None:
                report = DailyReviewReport(
                    report_date=report_date,
                    run_id=run_id,
                    schedule_name=schedule_name,
                    finding_counts={},
                )
                try:
                    with session.begin_nested():
                        session.add(report)
                        session.flush()
                except IntegrityError:
                    report = session.scalar(
                        select(DailyReviewReport).where(
                            DailyReviewReport.report_date == report_date
                        )
                    )
                    if report is None:
                        raise
            session.expunge(report)
            return report

    def finish_daily_report(
        self,
        report_id: uuid.UUID | str,
        *,
        status: str,
        commit_count: int,
        repository_count: int,
        finding_counts: Mapping[str, int],
        artifact_key: str,
        delivery_id: uuid.UUID | str | None,
        error_code: str | None = None,
    ) -> None:
        if status not in {"succeeded", "attention", "failed"}:
            raise ValueError("invalid daily report status")
        identifier = report_id if isinstance(report_id, uuid.UUID) else uuid.UUID(str(report_id))
        parsed_delivery = (
            delivery_id
            if delivery_id is None or isinstance(delivery_id, uuid.UUID)
            else uuid.UUID(str(delivery_id))
        )
        with Session(self._engine) as session, session.begin():
            report = session.get(DailyReviewReport, identifier)
            if report is None:
                raise NoResultFound(f"daily review report {identifier} was not found")
            report.status = status
            report.commit_count = commit_count
            report.repository_count = repository_count
            report.finding_counts = dict(finding_counts)
            report.artifact_key = artifact_key
            report.delivery_id = parsed_delivery
            report.error_code = error_code
            report.generated_at = utc_now()
            report.updated_at = utc_now()
            RunRepository.set_status(
                session,
                report.run_id,
                {
                    "succeeded": RunStatus.SUCCEEDED,
                    "attention": RunStatus.ATTENTION,
                    "failed": RunStatus.FAILED,
                }[status],
                summary=(
                    f"Daily code review report for {report.report_date.isoformat()}: "
                    f"{sum(finding_counts.values())} actionable finding(s)."
                ),
                error_code=error_code,
                artifact_key=artifact_key,
            )

    async def deliver_daily_report(
        self,
        *,
        report_date: date,
        artifact_key: str,
        idempotency_key: str,
    ) -> uuid.UUID | None:
        """Send the report by Discord using the report run as delivery owner."""

        from app.connectors.discord import deliver_daily_review_report
        from app.core.config import get_settings

        settings = get_settings()
        channel_id = settings.discord_code_review_channel_id
        if channel_id is None:
            return None
        with Session(self._engine) as session:
            report = session.scalar(
                select(DailyReviewReport).where(DailyReviewReport.report_date == report_date)
            )
            if report is None:
                raise NoResultFound(
                    f"daily review report for {report_date.isoformat()} was not found"
                )
            run_id = report.run_id
        delivery = await deliver_daily_review_report(
            engine=self._engine,
            run_id=run_id,
            channel_id=channel_id,
            report_date=report_date,
            report_artifact_key=artifact_key,
            idempotency_key=idempotency_key,
        )
        return delivery.id


__all__ = [
    "CodeReviewRepository",
    "ReviewIntake",
    "ReviewStatus",
    "SQLAlchemyCodeReviewOperationsStore",
    "review_idempotency_key",
]
