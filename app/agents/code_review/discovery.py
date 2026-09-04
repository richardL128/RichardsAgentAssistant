"""Account-scale repository discovery and resumable initial profiling.

Discovery is deliberately split into two durable steps.  A page of the
allowlisted installation repositories is committed together with its next
page, then ``ingest_next_repository_profile`` claims exactly one repository.
The profile row is the idempotent checkpoint; a worker interrupted after the
profile write but before the repository state update simply resumes that same
profile without downloading another archive.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents.code_review.ingestion import (
    GitHubRateLimiter,
    ProfileIngestionResult,
    RepositoryArchiveSource,
    ingest_repository_profile,
)
from app.artifacts.store import ArtifactStore
from app.connectors.github import RepositoryMetadata, RepositoryPage
from app.core.errors import ErrorCode, LifeAgentError, permanent_error
from app.db.code_review import CodeReviewRepository
from app.db.models import CodeRepository, RepositoryDiscoveryState
from app.db.repositories import utc_now

DEFAULT_DISCOVERY_PAGE_SIZE = 100
DEFAULT_DISCOVERY_VERSION = "discovery-v1"


class RepositoryDiscoverySource(Protocol):
    """The narrow account-scale reads required by this module."""

    async def list_installation_repositories(
        self, installation_id: int, *, page: int, per_page: int
    ) -> RepositoryPage: ...


class DefaultBranchSource(Protocol):
    """Resolve the default branch to an exact commit SHA."""

    async def get_default_branch_sha(
        self, repository: str, default_branch: str, installation_id: int
    ) -> str: ...


class InitialProfileSource(
    RepositoryArchiveSource, RepositoryDiscoverySource, DefaultBranchSource, Protocol
):
    """Combined connector surface required by the initial profile job."""


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    scope: str
    discovery_version: str
    pages_fetched: int
    repositories_discovered: int
    next_page: int
    complete: bool


@dataclass(frozen=True, slots=True)
class InitialProfileResult:
    """One repository's profile result and its durable repository identity."""

    repository_id: uuid.UUID
    ingestion: ProfileIngestionResult

    @property
    def repository(self) -> str:
        return self.ingestion.repository

    @property
    def resumed(self) -> bool:
        return self.ingestion.resumed


class _CachedMetadataSource(RepositoryArchiveSource):
    """Avoid a second metadata request after the SHA resolution read."""

    def __init__(self, source: RepositoryArchiveSource, metadata: RepositoryMetadata) -> None:
        self._source = source
        self._metadata = metadata

    async def get_repository_metadata(
        self, repository: str, installation_id: int, /
    ) -> RepositoryMetadata:
        return self._metadata

    async def download_repository_archive(
        self, repository: str, commit_sha: str, installation_id: int, /, *, max_bytes: int
    ) -> bytes:
        return await self._source.download_repository_archive(
            repository, commit_sha, installation_id, max_bytes=max_bytes
        )


def _validate_scope(scope: str, version: str, page_size: int) -> None:
    if not scope.strip() or len(scope) > 128:
        raise ValueError("discovery scope must be non-empty and at most 128 characters")
    if not version.strip() or len(version) > 128:
        raise ValueError("discovery version must be non-empty and at most 128 characters")
    if page_size < 1 or page_size > 100:
        raise ValueError("discovery page size must be between 1 and 100")


def _load_or_create_state(
    session: Session,
    *,
    scope: str,
    discovery_version: str,
) -> RepositoryDiscoveryState:
    state = session.scalar(
        select(RepositoryDiscoveryState).where(RepositoryDiscoveryState.scope == scope)
    )
    if state is None:
        state = RepositoryDiscoveryState(
            scope=scope,
            discovery_version=discovery_version,
            page=1,
            discovered_count=0,
            discovery_complete=False,
        )
        try:
            with session.begin_nested():
                session.add(state)
                session.flush()
        except IntegrityError:
            state = session.scalar(
                select(RepositoryDiscoveryState).where(RepositoryDiscoveryState.scope == scope)
            )
            if state is None:
                raise
    elif state.discovery_version != discovery_version:
        # A new discovery schema/version is a fresh sweep, while repository
        # profile rows remain intact and therefore continue to deduplicate.
        state.discovery_version = discovery_version
        state.page = 1
        state.cursor = None
        state.discovered_count = 0
        state.discovery_complete = False
        state.last_full_name = None
        state.last_error_code = None
        state.completed_at = None
    return state


def _persist_discovery_page(
    engine: Engine,
    *,
    scope: str,
    discovery_version: str,
    installation_id: int,
    allowlist_version: str,
    page: RepositoryPage,
) -> DiscoveryResult:
    with Session(engine) as session, session.begin():
        state = _load_or_create_state(session, scope=scope, discovery_version=discovery_version)
        # A retry can arrive after another worker committed this page.  Treat
        # it as an idempotent no-op and let the caller continue from the
        # already-persisted page instead of incrementing the count twice.
        if page.page < state.page:
            return DiscoveryResult(
                scope=scope,
                discovery_version=discovery_version,
                pages_fetched=0,
                repositories_discovered=0,
                next_page=state.page,
                complete=state.discovery_complete,
            )
        if page.page > state.page:
            raise permanent_error(
                ErrorCode.INPUT_INVALID, "GitHub repository page skipped a cursor"
            )
        for metadata in page.repositories:
            repository = session.scalar(
                select(CodeRepository).where(CodeRepository.full_name == metadata.repository)
            )
            if repository is None:
                repository = CodeRepository(
                    full_name=metadata.repository,
                    clone_url=metadata.clone_url,
                    default_branch=metadata.default_branch,
                    installation_id=installation_id,
                    allowlist_version=allowlist_version,
                    enabled=True,
                )
                session.add(repository)
            elif repository.enabled:
                repository.clone_url = metadata.clone_url
                repository.default_branch = metadata.default_branch
                repository.installation_id = installation_id
                repository.allowlist_version = allowlist_version
            repository.discovery_version = discovery_version
            repository.discovered_at = utc_now()

        now = utc_now()
        state.page = page.page + 1
        state.cursor = None
        state.discovered_count += len(page.repositories)
        state.discovery_complete = not page.has_next
        state.last_full_name = (
            page.repositories[-1].repository if page.repositories else state.last_full_name
        )
        state.completed_at = now if state.discovery_complete else None
        state.updated_at = now
        session.flush()
        return DiscoveryResult(
            scope=scope,
            discovery_version=discovery_version,
            pages_fetched=1,
            repositories_discovered=len(page.repositories),
            next_page=state.page,
            complete=state.discovery_complete,
        )


async def discover_repositories(
    *,
    source: RepositoryDiscoverySource,
    engine: Engine,
    scope: str,
    discovery_version: str = DEFAULT_DISCOVERY_VERSION,
    installation_id: int,
    allowlist_version: str,
    page_size: int = DEFAULT_DISCOVERY_PAGE_SIZE,
    rate_limiter: GitHubRateLimiter | None = None,
    max_pages: int | None = None,
) -> DiscoveryResult:
    """Discover all (or ``max_pages``) allowlisted installation repositories.

    Every successfully read page is committed before the next API call.  A
    process interruption therefore restarts at the persisted ``state.page``.
    ``max_pages`` is useful for bounded worker jobs and deterministic tests.
    """

    _validate_scope(scope, discovery_version, page_size)
    if installation_id <= 0:
        raise permanent_error(ErrorCode.INPUT_INVALID, "installation ID must be positive")
    if not allowlist_version.strip():
        raise ValueError("allowlist_version must not be empty")
    if max_pages is not None and max_pages < 1:
        raise ValueError("max_pages must be positive")

    with Session(engine) as session:
        state = session.scalar(
            select(RepositoryDiscoveryState).where(RepositoryDiscoveryState.scope == scope)
        )
        if state is not None and state.discovery_version == discovery_version:
            if state.discovery_complete:
                return DiscoveryResult(
                    scope=scope,
                    discovery_version=discovery_version,
                    pages_fetched=0,
                    repositories_discovered=0,
                    next_page=state.page,
                    complete=True,
                )
            page_number = state.page
        else:
            page_number = 1

    limiter = rate_limiter
    pages = 0
    discovered = 0
    final: DiscoveryResult | None = None
    while max_pages is None or pages < max_pages:
        if limiter is not None:
            await limiter.acquire()
        response = await source.list_installation_repositories(
            installation_id, page=page_number, per_page=page_size
        )
        if response.page != page_number or response.per_page != page_size:
            raise permanent_error(ErrorCode.INPUT_INVALID, "GitHub repository page is inconsistent")
        final = await asyncio.to_thread(
            _persist_discovery_page,
            engine,
            scope=scope,
            discovery_version=discovery_version,
            installation_id=installation_id,
            allowlist_version=allowlist_version,
            page=response,
        )
        pages += 1
        discovered += final.repositories_discovered
        if final.complete:
            break
        page_number = final.next_page

    if final is None:
        raise RuntimeError("discovery did not process a page")
    return DiscoveryResult(
        scope=final.scope,
        discovery_version=final.discovery_version,
        pages_fetched=pages,
        repositories_discovered=discovered,
        next_page=final.next_page,
        complete=final.complete,
    )


def _claim_next_repository(engine: Engine) -> tuple[uuid.UUID, str, int] | None:
    with Session(engine) as session, session.begin():
        repository = session.scalar(
            select(CodeRepository)
            .where(
                CodeRepository.enabled.is_(True),
                # ``profiling`` is reclaimable after cancellation or process
                # death.  ``failed`` is intentionally excluded so a permanent
                # failure cannot starve the remaining initial-ingestion queue.
                CodeRepository.profile_state.in_(("unprofiled", "profiling")),
            )
            .with_for_update(skip_locked=True)
            .order_by(CodeRepository.full_name)
            .limit(1)
        )
        if repository is None:
            return None
        repository.profile_state = "profiling"
        repository.profile_error_code = None
        repository.updated_at = utc_now()
        session.flush()
        return repository.id, repository.full_name, repository.installation_id


def _mark_profile_state(
    engine: Engine,
    repository_id: uuid.UUID,
    *,
    state: str,
    error_code: str | None = None,
) -> None:
    with Session(engine) as session, session.begin():
        repository = session.get(CodeRepository, repository_id)
        if repository is None:
            raise RuntimeError("repository disappeared during profile ingestion")
        repository.profile_state = state
        repository.profile_error_code = error_code
        repository.profiled_at = utc_now() if state == "profiled" else None
        repository.updated_at = utc_now()


def _queue_stale_profile_refreshes(engine: Engine, *, refresh_days: int) -> int:
    """Return stale profiles to the serial ingestion queue."""

    with Session(engine) as session, session.begin():
        repositories = CodeReviewRepository.profiles_due_for_refresh(
            session,
            as_of=utc_now(),
            refresh_after=timedelta(days=refresh_days),
        )
        repositories = [
            repository for repository in repositories if repository.profile_state == "profiled"
        ]
        for repository in repositories:
            repository.profile_state = "unprofiled"
            repository.profile_error_code = None
            repository.updated_at = utc_now()
        return len(repositories)


async def ingest_next_repository_profile(
    *,
    source: InitialProfileSource,
    engine: Engine,
    artifact_store: ArtifactStore,
    profile_version: str,
    rate_limiter: GitHubRateLimiter,
    archive_max_bytes: int = 50 * 1024 * 1024,
    archive_max_members: int = 20_000,
    archive_max_member_bytes: int = 2 * 1024 * 1024,
) -> InitialProfileResult | None:
    """Claim and ingest one repository, returning ``None`` when drained."""

    claimed = await asyncio.to_thread(_claim_next_repository, engine)
    if claimed is None:
        return None
    repository_id, repository, installation_id = claimed
    try:
        await rate_limiter.acquire()
        metadata = await source.get_repository_metadata(repository, installation_id)
        if metadata.repository != repository:
            raise permanent_error(ErrorCode.INPUT_INVALID, "GitHub returned a different repository")
        await rate_limiter.acquire()
        commit_sha = await source.get_default_branch_sha(
            repository, metadata.default_branch, installation_id
        )
        ingestion = await ingest_repository_profile(
            source=_CachedMetadataSource(source, metadata),
            engine=engine,
            artifact_store=artifact_store,
            repository=repository,
            repository_id=repository_id,
            installation_id=installation_id,
            commit_sha=commit_sha,
            profile_version=profile_version,
            rate_limiter=rate_limiter,
            archive_max_bytes=archive_max_bytes,
            archive_max_members=archive_max_members,
            archive_max_member_bytes=archive_max_member_bytes,
        )
    except asyncio.CancelledError:
        # Leave ``profiling`` so the next worker can safely reclaim it.
        raise
    except LifeAgentError as exc:
        await asyncio.to_thread(
            _mark_profile_state,
            engine,
            repository_id,
            state="failed",
            error_code=exc.record.code.value,
        )
        raise
    await asyncio.to_thread(_mark_profile_state, engine, repository_id, state="profiled")
    return InitialProfileResult(repository_id=repository_id, ingestion=ingestion)


async def run_code_review_ingest(
    run_id: str,
    idempotency_key: str,
    *,
    settings: Any | None = None,
    engine: Engine | None = None,
    source: InitialProfileSource | None = None,
    artifact_store: ArtifactStore | None = None,
    rate_limiter: GitHubRateLimiter | None = None,
) -> dict[str, object]:
    """Discover the configured installation and serially profile every pending repository."""

    if not run_id.strip() or not idempotency_key.strip():
        raise ValueError("run_id and idempotency_key must not be empty")
    resolved_settings = settings
    if resolved_settings is None:
        from app.core.config import get_settings

        resolved_settings = get_settings()
    installation_id = resolved_settings.github_installation_id
    allowlist_version = resolved_settings.repository_allowlist_version
    if installation_id is None or allowlist_version is None:
        from app.core.errors import authorization_error

        raise authorization_error("GitHub discovery is not configured")
    resolved_engine = engine
    if resolved_engine is None:
        from app.db.session import Database

        resolved_engine = Database(resolved_settings).engine
    resolved_source = source
    if resolved_source is None:
        from app.connectors.github import GitHubAppConnector
        from app.core.errors import authorization_error

        if resolved_settings.github_app_id is None or resolved_settings.github_private_key is None:
            raise authorization_error("GitHub App credentials are not configured")
        resolved_source = GitHubAppConnector(
            app_id=resolved_settings.github_app_id,
            private_key=resolved_settings.github_private_key,
            repository_allowlist=resolved_settings.repository_allowlist,
            timeout_seconds=resolved_settings.connector_timeout_seconds,
        )
    resolved_artifacts = artifact_store or ArtifactStore(resolved_settings.artifact_root)
    limiter = rate_limiter or GitHubRateLimiter(
        min_interval_seconds=resolved_settings.github_min_call_interval_seconds
    )
    discovery = await discover_repositories(
        source=resolved_source,
        engine=resolved_engine,
        scope=resolved_settings.code_review_discovery_scope,
        installation_id=installation_id,
        allowlist_version=allowlist_version,
        page_size=resolved_settings.github_discovery_page_size,
        rate_limiter=limiter,
    )
    refreshes_queued = await asyncio.to_thread(
        _queue_stale_profile_refreshes,
        resolved_engine,
        refresh_days=resolved_settings.code_review_profile_refresh_days,
    )
    profiled = 0
    resumed = 0
    while True:
        result = await ingest_next_repository_profile(
            source=resolved_source,
            engine=resolved_engine,
            artifact_store=resolved_artifacts,
            profile_version="project-profile-v1",
            rate_limiter=limiter,
        )
        if result is None:
            break
        profiled += 1
        resumed += int(result.resumed)
    return {
        "status": "succeeded",
        "run_id": run_id,
        "idempotency_key": idempotency_key,
        "repositories_discovered": discovery.repositories_discovered,
        "profiles_processed": profiled,
        "profiles_resumed": resumed,
        "profile_refreshes_queued": refreshes_queued,
        "discovery_complete": discovery.complete,
    }


__all__ = [
    "DEFAULT_DISCOVERY_PAGE_SIZE",
    "DEFAULT_DISCOVERY_VERSION",
    "DefaultBranchSource",
    "DiscoveryResult",
    "InitialProfileResult",
    "InitialProfileSource",
    "RepositoryDiscoverySource",
    "discover_repositories",
    "ingest_next_repository_profile",
    "run_code_review_ingest",
]
