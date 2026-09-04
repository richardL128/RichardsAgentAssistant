"""Account-scale repository discovery and resumable profile tests."""

from __future__ import annotations

import asyncio
import io
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session

from app.agents.code_review.discovery import (
    discover_repositories,
    ingest_next_repository_profile,
    run_code_review_ingest,
)
from app.agents.code_review.ingestion import GitHubRateLimiter
from app.artifacts.store import ArtifactStore
from app.connectors.github import RepositoryMetadata, RepositoryPage
from app.db.models import Base, CodeRepository, RepositoryDiscoveryState, RepositoryProfile

REPOSITORY_1 = "octo-org/one"
REPOSITORY_2 = "octo-org/two"
SHA_1 = "a" * 40
SHA_2 = "b" * 40


def _archive() -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as tar:
        root = tarfile.TarInfo("octo-org-one-aaaaaaa")
        root.type = tarfile.DIRTYPE
        tar.addfile(root)
        body = b"# Purpose\n"
        info = tarfile.TarInfo("octo-org-one-aaaaaaa/README.md")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    return output.getvalue()


class _Source:
    def __init__(self) -> None:
        self.pages: list[int] = []
        self.archive_calls: list[str] = []

    async def list_installation_repositories(
        self, installation_id: int, *, page: int, per_page: int
    ) -> RepositoryPage:
        self.pages.append(page)
        entries = [REPOSITORY_1] if page == 1 else [REPOSITORY_2]
        return RepositoryPage(
            repositories=[
                RepositoryMetadata(
                    repository=name,
                    clone_url=f"https://github.com/{name}.git",
                    default_branch="main",
                    private=True,
                    visibility="private",
                )
                for name in entries
            ],
            page=page,
            per_page=per_page,
            has_next=page == 1,
        )

    async def get_repository_metadata(
        self, repository: str, installation_id: int
    ) -> RepositoryMetadata:
        return RepositoryMetadata(
            repository=repository,
            clone_url=f"https://github.com/{repository}.git",
            default_branch="main",
            private=True,
            visibility="private",
        )

    async def get_default_branch_sha(
        self, repository: str, default_branch: str, installation_id: int
    ) -> str:
        return SHA_1 if repository == REPOSITORY_1 else SHA_2

    async def download_repository_archive(
        self, repository: str, commit_sha: str, installation_id: int, *, max_bytes: int
    ) -> bytes:
        self.archive_calls.append(repository)
        return _archive()


class _InterruptingSource(_Source):
    def __init__(self) -> None:
        super().__init__()
        self.interrupt = True

    async def get_repository_metadata(
        self, repository: str, installation_id: int
    ) -> RepositoryMetadata:
        if self.interrupt:
            self.interrupt = False
            raise asyncio.CancelledError
        return await super().get_repository_metadata(repository, installation_id)


def _engine(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'discovery.db'}")
    Base.metadata.create_all(engine)
    return engine


@pytest.mark.asyncio
async def test_discovery_commits_page_cursor_and_resumes(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    source = _Source()
    first = await discover_repositories(
        source=source,
        engine=engine,
        scope="github-installation",
        installation_id=123,
        allowlist_version="allow-v1",
        page_size=1,
        max_pages=1,
    )
    assert first.complete is False
    assert first.next_page == 2
    second = await discover_repositories(
        source=source,
        engine=engine,
        scope="github-installation",
        installation_id=123,
        allowlist_version="allow-v1",
        page_size=1,
    )
    assert second.complete is True
    assert source.pages == [1, 2]
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(CodeRepository)) == 2
        state = session.scalar(select(RepositoryDiscoveryState))
        assert state is not None
        assert state.discovery_complete is True
        assert state.page == 3


@pytest.mark.asyncio
async def test_initial_profile_is_one_at_a_time_and_replay_is_checkpointed(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    source = _Source()
    await discover_repositories(
        source=source,
        engine=engine,
        scope="github-installation",
        installation_id=123,
        allowlist_version="allow-v1",
        page_size=2,
    )
    store = ArtifactStore(tmp_path / "artifacts")
    limiter = GitHubRateLimiter(min_interval_seconds=0)
    first = await ingest_next_repository_profile(
        source=source,
        engine=engine,
        artifact_store=store,
        profile_version="profile-v1",
        rate_limiter=limiter,
    )
    assert first is not None
    assert first.repository == REPOSITORY_1
    assert source.archive_calls == [REPOSITORY_1]
    # The next claim advances to the next repository, never creating a second
    # profile for the first one.
    second = await ingest_next_repository_profile(
        source=source,
        engine=engine,
        artifact_store=store,
        profile_version="profile-v1",
        rate_limiter=limiter,
    )
    assert second is not None
    assert second.repository == REPOSITORY_2
    assert (
        await ingest_next_repository_profile(
            source=source,
            engine=engine,
            artifact_store=store,
            profile_version="profile-v1",
            rate_limiter=limiter,
        )
        is None
    )
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(RepositoryProfile)) == 2
        states = session.scalars(select(CodeRepository.profile_state)).all()
        assert states == ["profiled", "profiled"]


@pytest.mark.asyncio
async def test_interrupted_claim_is_reclaimed_without_duplicate_profile(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    discovery_source = _Source()
    await discover_repositories(
        source=discovery_source,
        engine=engine,
        scope="github-installation",
        installation_id=123,
        allowlist_version="allow-v1",
        page_size=2,
    )
    source = _InterruptingSource()
    store = ArtifactStore(tmp_path / "artifacts")
    limiter = GitHubRateLimiter(min_interval_seconds=0)
    with pytest.raises(asyncio.CancelledError):
        await ingest_next_repository_profile(
            source=source,
            engine=engine,
            artifact_store=store,
            profile_version="profile-v1",
            rate_limiter=limiter,
        )
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(RepositoryProfile)) == 0
        first_state = session.scalar(
            select(CodeRepository.profile_state).where(CodeRepository.full_name == REPOSITORY_1)
        )
        assert first_state == "profiling"

    resumed = await ingest_next_repository_profile(
        source=source,
        engine=engine,
        artifact_store=store,
        profile_version="profile-v1",
        rate_limiter=limiter,
    )
    assert resumed is not None
    assert resumed.repository == REPOSITORY_1
    assert resumed.resumed is False


@pytest.mark.asyncio
async def test_ingestion_handler_discovers_and_serially_profiles_the_installation(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    source = _Source()
    settings = SimpleNamespace(
        github_installation_id=123,
        repository_allowlist_version="allow-v1",
        code_review_discovery_scope="github-installation",
        github_discovery_page_size=2,
        github_min_call_interval_seconds=0,
        code_review_profile_refresh_days=30,
        artifact_root=tmp_path / "unused",
    )
    first = await run_code_review_ingest(
        "00000000-0000-0000-0000-000000000001",
        "code-review-ingest:fixture:v1",
        settings=settings,
        engine=engine,
        source=source,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        rate_limiter=GitHubRateLimiter(min_interval_seconds=0),
    )
    assert first["status"] == "succeeded"
    assert first["profiles_processed"] == 2
    assert source.archive_calls == [REPOSITORY_1, REPOSITORY_2]

    replay = await run_code_review_ingest(
        "00000000-0000-0000-0000-000000000001",
        "code-review-ingest:fixture:v1",
        settings=settings,
        engine=engine,
        source=source,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        rate_limiter=GitHubRateLimiter(min_interval_seconds=0),
    )
    assert replay["profiles_processed"] == 0
    assert source.archive_calls == [REPOSITORY_1, REPOSITORY_2]
