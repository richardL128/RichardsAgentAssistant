"""Rate-limited, resumable, single-repository project-profile ingestion.

One invocation ingests exactly one repository at one commit.  Account-scale
discovery is Phase 4 and is deliberately not built here.  The job is:

* rate limited -- a minimum interval between GitHub calls, enforced through an
  injectable clock and sleep so tests stay deterministic and fast;
* resumable -- the persisted ``RepositoryProfile`` row is the explicit,
  queryable checkpoint (see :func:`find_profile_checkpoint`).  Re-running for an
  already ingested ``(repository, commit, profile_version)`` re-downloads
  nothing and creates neither a duplicate profile row nor a duplicate artifact;
* bounded -- the GitHub tarball is expanded in memory with a bounded member
  count, bounded per-member size, path-traversal rejection (no absolute paths,
  no ``..``, no symlinks), and the file-count and character budgets from
  :mod:`app.agents.code_review.profile`.

Instruction-file bodies are never fed into the profile as guidance: their
contents are dropped before extraction, matching ``profile.py``'s policy of
recording instruction-file *names* only.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import re
import tarfile
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Protocol

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.agents.code_review.contracts import ProjectProfile, validate_repo_path
from app.agents.code_review.profile import (
    MAX_FILE_CHARS,
    MAX_FILES,
    MAX_TOTAL_CHARS,
    extract_project_profile,
    render_skills,
)
from app.artifacts.store import ArtifactMetadata, ArtifactStore
from app.connectors.github import RepositoryMetadata
from app.core.errors import ErrorCode, permanent_error
from app.db.code_review import CodeReviewRepository
from app.db.models import RepositoryProfile

PROFILE_MEDIA_TYPE = "text/markdown"
PROFILE_DATA_CLASS = "project_profile"

DEFAULT_MIN_GITHUB_INTERVAL_SECONDS = 1.0
DEFAULT_ARCHIVE_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_ARCHIVE_MAX_MEMBERS = 20_000
DEFAULT_ARCHIVE_MAX_MEMBER_BYTES = 2 * 1024 * 1024
DEFAULT_ARCHIVE_MAX_TOTAL_BYTES = 64 * 1024 * 1024

_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
# Mirrors ``profile.py``'s instruction-file set; kept local so this module does
# not reach into another module's private constant.
_INSTRUCTION_FILE_NAMES = frozenset({"AGENTS.md", "SKILLS.md", "CLAUDE.md", "CONTRIBUTING.md"})


class RepositoryArchiveSource(Protocol):
    """The two read-only GitHub operations the ingestion job depends on."""

    async def get_repository_metadata(
        self, repository: str, installation_id: int, /
    ) -> RepositoryMetadata: ...

    async def download_repository_archive(
        self, repository: str, commit_sha: str, installation_id: int, /, *, max_bytes: int
    ) -> bytes: ...


class GitHubRateLimiter:
    """Enforce a minimum wall-clock interval between GitHub calls.

    The clock and sleep are injected so tests never touch real time.  ``clock``
    returns an aware UTC ``datetime``; ``sleep`` awaits a number of seconds.
    """

    def __init__(
        self,
        *,
        min_interval_seconds: float = DEFAULT_MIN_GITHUB_INTERVAL_SECONDS,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must not be negative")
        self._min_interval_seconds = min_interval_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep or asyncio.sleep
        self._last_call: datetime | None = None

    async def acquire(self) -> None:
        """Block until at least ``min_interval_seconds`` have elapsed."""

        now = self._now()
        if self._last_call is not None:
            elapsed = (now - self._last_call).total_seconds()
            wait = self._min_interval_seconds - elapsed
            if wait > 0:
                await self._sleep(wait)
                now = self._now()
        self._last_call = now

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("rate-limiter clock must return an aware datetime")
        return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ProfileIngestionResult:
    """The outcome of one ingestion invocation.

    ``resumed`` is ``True`` when a checkpoint row already existed and nothing
    was downloaded or written.
    """

    repository: str
    commit_sha: str
    profile_version: str
    profile_id: uuid.UUID
    artifact_key: str
    summary: str
    instruction_files: tuple[str, ...]
    resumed: bool
    archive_downloaded: bool


def find_profile_checkpoint(
    session: Session,
    *,
    repository_id: uuid.UUID,
    commit_sha: str,
    profile_version: str,
) -> RepositoryProfile | None:
    """Return the persisted profile row that marks this ingestion complete.

    This is the explicit resume point: ``persist_profile`` is idempotent on
    ``(repository_id, commit_sha, profile_version)`` and this query reads back
    exactly that key.
    """

    return session.scalar(
        select(RepositoryProfile).where(
            RepositoryProfile.repository_id == repository_id,
            RepositoryProfile.commit_sha == commit_sha,
            RepositoryProfile.profile_version == profile_version,
        )
    )


def archive_to_files(
    archive: bytes,
    *,
    max_members: int = DEFAULT_ARCHIVE_MAX_MEMBERS,
    max_member_bytes: int = DEFAULT_ARCHIVE_MAX_MEMBER_BYTES,
    max_total_bytes: int = DEFAULT_ARCHIVE_MAX_TOTAL_BYTES,
) -> dict[str, str]:
    """Expand a GitHub tarball into a bounded repository-relative file map.

    GitHub tarballs nest everything under a single top-level prefix directory;
    that prefix is stripped and every remaining path is validated with
    :func:`validate_repo_path`.  Absolute paths, ``..`` traversal, symlinks and
    hard links are rejected.  Member count, per-member size and total declared
    size are bounded, and the resulting map honours ``profile.py``'s file-count
    and character budgets.
    """

    files: dict[str, str] = {}
    kept_chars = 0
    total_declared = 0
    seen = 0
    prefix: str | None = None
    try:
        # SIM115: the handle is closed deterministically by ``contextlib.closing``
        # below; the open has to stay outside the ``with`` so that an unreadable
        # archive becomes a typed permanent error rather than a raw TarError.
        opened_archive = contextlib.closing(
            tarfile.open(fileobj=io.BytesIO(archive), mode="r:*")  # noqa: SIM115
        )
    except tarfile.TarError:
        raise permanent_error(
            ErrorCode.INPUT_INVALID, "repository archive is not a readable tarball"
        ) from None
    with opened_archive as tar:
        for member in tar:
            seen += 1
            if seen > max_members:
                raise permanent_error(
                    ErrorCode.INPUT_INVALID, "repository archive has too many members"
                )
            components = _safe_components(member.name)
            if prefix is None:
                prefix = components[0]
            if components[0] != prefix:
                raise permanent_error(
                    ErrorCode.INPUT_INVALID, "repository archive member escapes the archive root"
                )
            if member.issym() or member.islnk():
                raise permanent_error(
                    ErrorCode.INPUT_INVALID, "repository archive contains a link member"
                )
            if not member.isfile():
                continue
            if member.size < 0 or member.size > max_member_bytes:
                raise permanent_error(
                    ErrorCode.INPUT_INVALID, "repository archive member exceeds the size limit"
                )
            total_declared += member.size
            if total_declared > max_total_bytes:
                raise permanent_error(
                    ErrorCode.INPUT_INVALID, "repository archive exceeds the total size limit"
                )
            relative_parts = components[1:]
            if not relative_parts:
                continue
            try:
                relative = validate_repo_path("/".join(relative_parts))
            except ValueError:
                raise permanent_error(
                    ErrorCode.INPUT_INVALID, "repository archive member has an unsafe path"
                ) from None
            if len(files) >= MAX_FILES or relative in files:
                continue
            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            raw = extracted.read(max_member_bytes + 1)
            if len(raw) > max_member_bytes:
                raise permanent_error(
                    ErrorCode.INPUT_INVALID, "repository archive member exceeds the size limit"
                )
            content = raw.decode("utf-8", errors="replace")[:MAX_FILE_CHARS]
            if kept_chars + len(content) > MAX_TOTAL_CHARS:
                continue
            files[relative] = content
            kept_chars += len(content)
    return files


async def ingest_repository_profile(
    *,
    source: RepositoryArchiveSource,
    engine: Engine,
    artifact_store: ArtifactStore,
    repository: str,
    repository_id: uuid.UUID,
    installation_id: int,
    commit_sha: str,
    profile_version: str,
    rate_limiter: GitHubRateLimiter,
    archive_max_bytes: int = DEFAULT_ARCHIVE_MAX_BYTES,
    archive_max_members: int = DEFAULT_ARCHIVE_MAX_MEMBERS,
    archive_max_member_bytes: int = DEFAULT_ARCHIVE_MAX_MEMBER_BYTES,
) -> ProfileIngestionResult:
    """Ingest one repository's project profile at one exact commit.

    Returns early -- without any GitHub call -- when a checkpoint row already
    exists for this ``(repository_id, commit_sha, profile_version)``.
    """

    if not _REPOSITORY_RE.fullmatch(repository):
        raise permanent_error(ErrorCode.INPUT_INVALID, "repository must be owner/name")
    if not _SHA_RE.fullmatch(commit_sha):
        raise permanent_error(
            ErrorCode.INPUT_INVALID, "commit SHA must be a 40- or 64-character hex digest"
        )
    if not profile_version.strip():
        raise ValueError("profile_version must not be empty")
    if installation_id <= 0:
        raise permanent_error(ErrorCode.INPUT_INVALID, "installation ID must be positive")

    checkpoint = await asyncio.to_thread(
        _load_checkpoint,
        engine,
        repository=repository,
        repository_id=repository_id,
        commit_sha=commit_sha,
        profile_version=profile_version,
    )
    if checkpoint is not None:
        return checkpoint

    await rate_limiter.acquire()
    metadata = await source.get_repository_metadata(repository, installation_id)
    if metadata.repository != repository:
        raise permanent_error(
            ErrorCode.INPUT_INVALID, "GitHub returned metadata for a different repository"
        )

    await rate_limiter.acquire()
    archive = await source.download_repository_archive(
        repository, commit_sha, installation_id, max_bytes=archive_max_bytes
    )

    profile, rendered = await asyncio.to_thread(
        _build_profile,
        repository,
        commit_sha,
        archive,
        archive_max_members,
        archive_max_member_bytes,
    )
    artifact = await asyncio.to_thread(_store_profile_artifact, artifact_store, rendered)
    profile_id = await asyncio.to_thread(
        _persist_profile, engine, repository_id, profile_version, profile, artifact.key
    )
    return ProfileIngestionResult(
        repository=repository,
        commit_sha=commit_sha,
        profile_version=profile_version,
        profile_id=profile_id,
        artifact_key=artifact.key,
        summary=profile.purpose,
        instruction_files=tuple(profile.instruction_files),
        resumed=False,
        archive_downloaded=True,
    )


def _load_checkpoint(
    engine: Engine,
    *,
    repository: str,
    repository_id: uuid.UUID,
    commit_sha: str,
    profile_version: str,
) -> ProfileIngestionResult | None:
    with Session(engine) as session:
        row = find_profile_checkpoint(
            session,
            repository_id=repository_id,
            commit_sha=commit_sha,
            profile_version=profile_version,
        )
        if row is None:
            return None
        return ProfileIngestionResult(
            repository=repository,
            commit_sha=commit_sha,
            profile_version=profile_version,
            profile_id=row.id,
            artifact_key=row.artifact_key,
            summary=row.summary,
            instruction_files=tuple(row.instruction_provenance),
            resumed=True,
            archive_downloaded=False,
        )


def _build_profile(
    repository: str,
    commit_sha: str,
    archive: bytes,
    max_members: int,
    max_member_bytes: int,
) -> tuple[ProjectProfile, str]:
    files = archive_to_files(archive, max_members=max_members, max_member_bytes=max_member_bytes)
    files = _without_instruction_bodies(files)
    profile = extract_project_profile(repository=repository, commit_sha=commit_sha, files=files)
    return profile, render_skills(profile)


def _store_profile_artifact(store: ArtifactStore, rendered: str) -> ArtifactMetadata:
    return store.put(rendered, media_type=PROFILE_MEDIA_TYPE, data_class=PROFILE_DATA_CLASS)


def _persist_profile(
    engine: Engine,
    repository_id: uuid.UUID,
    profile_version: str,
    profile: ProjectProfile,
    artifact_key: str,
) -> uuid.UUID:
    with Session(engine) as session, session.begin():
        row = CodeReviewRepository.persist_profile(
            session,
            repository_id=repository_id,
            profile_version=profile_version,
            profile=profile,
            artifact_key=artifact_key,
        )
        return row.id


def _without_instruction_bodies(files: dict[str, str]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for path, content in files.items():
        name = PurePosixPath(path).name
        if name in _INSTRUCTION_FILE_NAMES or name.casefold().startswith("agents."):
            cleaned[path] = ""
        else:
            cleaned[path] = content
    return cleaned


def _safe_components(name: str) -> tuple[str, ...]:
    if not name or "\x00" in name or "\\" in name:
        raise permanent_error(
            ErrorCode.INPUT_INVALID, "repository archive member has an unsafe path"
        )
    pure = PurePosixPath(name)
    if pure.is_absolute() or ".." in pure.parts or "." in pure.parts:
        raise permanent_error(
            ErrorCode.INPUT_INVALID, "repository archive member has an unsafe path"
        )
    parts = pure.parts
    if not parts:
        raise permanent_error(
            ErrorCode.INPUT_INVALID, "repository archive member has an unsafe path"
        )
    return parts


__all__ = [
    "DEFAULT_ARCHIVE_MAX_BYTES",
    "DEFAULT_ARCHIVE_MAX_MEMBERS",
    "DEFAULT_ARCHIVE_MAX_MEMBER_BYTES",
    "DEFAULT_MIN_GITHUB_INTERVAL_SECONDS",
    "PROFILE_DATA_CLASS",
    "PROFILE_MEDIA_TYPE",
    "GitHubRateLimiter",
    "ProfileIngestionResult",
    "RepositoryArchiveSource",
    "archive_to_files",
    "find_profile_checkpoint",
    "ingest_repository_profile",
]
