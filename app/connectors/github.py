"""Least-privilege GitHub App connector for the code-review workflow.

The adapter deliberately has no general-purpose request method.  Every URL is
constructed from a validated repository name and one of the small set of
read-only GitHub REST operations needed by the review pipeline.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, cast
from urllib.parse import urlsplit

import httpx
import jwt
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from app.agents.code_review.contracts import PushEvent
from app.core.errors import ErrorCode, authorization_error, permanent_error, transient_error

GITHUB_API_BASE_URL: Final[str] = "https://api.github.com"
GITHUB_WEB_HOST: Final[str] = "github.com"
GITHUB_API_VERSION: Final[str] = "2022-11-28"
_REPOSITORY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$"
)
_SHA_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_DELIVERY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_REF_PATTERN: Final[re.Pattern[str]] = re.compile(r"^refs/heads/[A-Za-z0-9._/-]+$")


class InstallationToken(BaseModel):
    """An installation credential kept redacted by Pydantic's ``SecretStr``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    token: SecretStr
    expires_at: datetime | None = None


class RepositoryMetadata(BaseModel):
    """The bounded repository fields used by checkout and profile ingestion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str = Field(pattern=_REPOSITORY_PATTERN.pattern)
    clone_url: str = Field(min_length=1, max_length=1_000)
    default_branch: str = Field(min_length=1, max_length=255)
    private: bool
    visibility: str | None = None


class CommitComparison(BaseModel):
    """A compact, typed response from GitHub's compare endpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str = Field(pattern=_REPOSITORY_PATTERN.pattern)
    base_sha: str = Field(pattern=_SHA_PATTERN.pattern)
    head_sha: str = Field(pattern=_SHA_PATTERN.pattern)
    status: Literal["ahead", "behind", "diverged", "identical"] | str
    ahead_by: int = Field(ge=0)
    behind_by: int = Field(ge=0)
    total_commits: int = Field(ge=0)
    commit_shas: list[str] = Field(max_length=5_000)


def _secret_value(value: SecretStr | str) -> str:
    return value.get_secret_value() if isinstance(value, SecretStr) else value


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def verify_webhook_signature(
    payload: bytes,
    signature_header: str | None,
    secret: SecretStr | str,
) -> None:
    """Verify GitHub's raw-body HMAC before any JSON decoding occurs."""

    if not signature_header or not signature_header.startswith("sha256="):
        raise authorization_error("GitHub webhook signature is invalid")
    supplied = signature_header[7:]
    if len(supplied) != hashlib.sha256().digest_size * 2:
        raise authorization_error("GitHub webhook signature is invalid")
    try:
        supplied_bytes = bytes.fromhex(supplied)
    except ValueError:
        raise authorization_error("GitHub webhook signature is invalid") from None
    expected = hmac.new(_secret_value(secret).encode("utf-8"), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, supplied_bytes):
        raise authorization_error("GitHub webhook signature is invalid")


def _validate_repository(repository: str, allowlist: frozenset[str]) -> str:
    if not _REPOSITORY_PATTERN.fullmatch(repository):
        raise permanent_error(ErrorCode.INPUT_INVALID, "GitHub repository name is invalid")
    if repository not in allowlist:
        raise permanent_error(ErrorCode.INPUT_INVALID, "GitHub repository is not allowlisted")
    return repository


def _validate_clone_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != GITHUB_WEB_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise permanent_error(ErrorCode.INPUT_INVALID, "GitHub clone URL is not allowlisted")
    return value


def normalize_push_event(
    payload: bytes,
    headers: Mapping[str, str],
    *,
    webhook_secret: SecretStr | str,
    repository_allowlist: Iterable[str],
    received_at: datetime,
) -> PushEvent:
    """Authenticate and normalize a GitHub push webhook into ``PushEvent``."""

    # This call intentionally precedes delivery-header checks and JSON parsing.
    verify_webhook_signature(payload, _header(headers, "X-Hub-Signature-256"), webhook_secret)
    delivery_id = _header(headers, "X-GitHub-Delivery")
    if not delivery_id or not _DELIVERY_PATTERN.fullmatch(delivery_id):
        raise permanent_error(ErrorCode.INPUT_INVALID, "GitHub delivery ID is invalid")
    try:
        body_value: Any = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        raise permanent_error(ErrorCode.INPUT_INVALID, "GitHub webhook JSON is invalid") from None
    if not isinstance(body_value, dict):
        raise permanent_error(ErrorCode.INPUT_INVALID, "GitHub webhook payload is invalid")
    body = cast(dict[str, Any], body_value)
    repository_data_value = body.get("repository")
    installation_data_value = body.get("installation")
    if not isinstance(repository_data_value, dict) or not isinstance(installation_data_value, dict):
        raise permanent_error(ErrorCode.INPUT_INVALID, "GitHub webhook payload is incomplete")
    repository_data = cast(dict[str, Any], repository_data_value)
    installation_data = cast(dict[str, Any], installation_data_value)
    repository = repository_data.get("full_name")
    clone_url = repository_data.get("clone_url")
    default_branch = repository_data.get("default_branch")
    installation_id = installation_data.get("id")
    ref = body.get("ref")
    before_sha = body.get("before")
    after_sha = body.get("after")
    if (
        not isinstance(repository, str)
        or not isinstance(clone_url, str)
        or not isinstance(default_branch, str)
        or not isinstance(installation_id, int)
        or isinstance(installation_id, bool)
        or not isinstance(ref, str)
        or not isinstance(before_sha, str)
        or not isinstance(after_sha, str)
    ):
        raise permanent_error(ErrorCode.INPUT_INVALID, "GitHub webhook payload is incomplete")
    allowlist = frozenset(repository_allowlist)
    _validate_repository(repository, allowlist)
    _validate_clone_url(clone_url)
    if not _REF_PATTERN.fullmatch(ref):
        raise permanent_error(ErrorCode.INPUT_INVALID, "GitHub push ref is invalid")
    try:
        return PushEvent(
            delivery_id=delivery_id,
            repository=repository,
            clone_url=clone_url,
            default_branch=default_branch,
            installation_id=installation_id,
            ref=ref,
            before_sha=before_sha,
            after_sha=after_sha,
            received_at=received_at,
        )
    except ValidationError:
        raise permanent_error(
            ErrorCode.INPUT_INVALID, "GitHub webhook fields are invalid"
        ) from None


class GitHubAppConnector:
    """GitHub App authentication plus narrowly scoped repository reads."""

    def __init__(
        self,
        *,
        app_id: int,
        private_key: SecretStr | str,
        repository_allowlist: Iterable[str] = (),
        allowed_repositories: Iterable[str] | None = None,
        webhook_secret: SecretStr | str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
        clock: Callable[[], datetime] | None = None,
        time: Callable[[], float] | None = None,
    ) -> None:
        if app_id <= 0:
            raise ValueError("GitHub App ID must be positive")
        if timeout_seconds <= 0 or timeout_seconds > 120:
            raise ValueError("GitHub timeout must be finite and between 0 and 120 seconds")
        selected = repository_allowlist if allowed_repositories is None else allowed_repositories
        allowlist = frozenset(selected)
        if any(not _REPOSITORY_PATTERN.fullmatch(repo) for repo in allowlist):
            raise ValueError("GitHub repository allowlist contains an invalid repository")
        self._app_id = app_id
        self._private_key = private_key
        self._allowlist = allowlist
        self._webhook_secret = webhook_secret
        self._client = client
        self._timeout_seconds = timeout_seconds
        if clock is not None and time is not None:
            raise ValueError("pass only one GitHub clock")
        self._clock = clock or (
            (lambda: datetime.fromtimestamp(time(), tz=UTC))
            if time is not None
            else lambda: datetime.now(UTC)
        )
        self._installation_tokens: dict[int, InstallationToken] = {}

    def create_app_jwt(self) -> str:
        """Create a short-lived RS256 App JWT without exposing the private key."""

        now = self._clock().astimezone(UTC).replace(microsecond=0)
        claims = {
            "iat": int(now.timestamp()) - 60,
            "exp": int((now + timedelta(minutes=9)).timestamp()),
            "iss": str(self._app_id),
        }
        try:
            return str(jwt.encode(claims, _secret_value(self._private_key), algorithm="RS256"))
        except (ValueError, TypeError, jwt.PyJWTError):
            raise authorization_error("GitHub App private key is invalid") from None

    async def normalize_push_webhook(
        self,
        payload: bytes,
        headers: Mapping[str, str],
        *,
        received_at: datetime | None = None,
    ) -> PushEvent:
        if self._webhook_secret is None:
            raise authorization_error("GitHub webhook secret is not configured")
        return normalize_push_event(
            payload,
            headers,
            webhook_secret=self._webhook_secret,
            repository_allowlist=self._allowlist,
            received_at=received_at or self._clock(),
        )

    async def exchange_installation_token(self, installation_id: int) -> InstallationToken:
        """Exchange the App JWT for one installation token."""

        if installation_id <= 0:
            raise permanent_error(ErrorCode.INPUT_INVALID, "GitHub installation ID is invalid")
        cached = self._installation_tokens.get(installation_id)
        now = self._clock()
        if cached and (cached.expires_at is None or cached.expires_at > now + timedelta(minutes=1)):
            return cached
        response = await self._request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {self.create_app_jwt()}"},
        )
        data = self._json_object(response, "GitHub installation token response")
        token = data.get("token")
        expires_at = data.get("expires_at")
        if not isinstance(token, str) or not token or not isinstance(expires_at, str):
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT, "GitHub returned an invalid installation token"
            )
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT, "GitHub returned an invalid token expiry"
            ) from None
        result = InstallationToken(token=SecretStr(token), expires_at=expiry)
        self._installation_tokens[installation_id] = result
        return result

    async def get_repository_metadata(
        self, repository: str, installation_id: int
    ) -> RepositoryMetadata:
        """Read metadata for one allowlisted repository."""

        repository = _validate_repository(repository, self._allowlist)
        token = await self.exchange_installation_token(installation_id)
        response = await self._request(
            "GET",
            f"/repos/{repository}",
            headers={"Authorization": f"Bearer {token.token.get_secret_value()}"},
        )
        data = self._json_object(response, "GitHub repository metadata")
        full_name = data.get("full_name")
        clone_url = data.get("clone_url")
        default_branch = data.get("default_branch")
        private = data.get("private")
        visibility = data.get("visibility")
        if (
            full_name != repository
            or not isinstance(clone_url, str)
            or not isinstance(default_branch, str)
            or not isinstance(private, bool)
            or (visibility is not None and not isinstance(visibility, str))
        ):
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT, "GitHub returned invalid repository metadata"
            )
        _validate_clone_url(clone_url)
        return RepositoryMetadata(
            repository=repository,
            clone_url=clone_url,
            default_branch=default_branch,
            private=private,
            visibility=visibility,
        )

    async def compare_commits(
        self,
        repository: str,
        base_sha: str,
        head_sha: str,
        installation_id: int,
    ) -> CommitComparison:
        """Read GitHub's comparison for two exact commit SHAs."""

        repository = _validate_repository(repository, self._allowlist)
        if not _SHA_PATTERN.fullmatch(base_sha) or not _SHA_PATTERN.fullmatch(head_sha):
            raise permanent_error(ErrorCode.INPUT_INVALID, "GitHub commit SHA is invalid")
        token = await self.exchange_installation_token(installation_id)
        response = await self._request(
            "GET",
            f"/repos/{repository}/compare/{base_sha}...{head_sha}",
            headers={"Authorization": f"Bearer {token.token.get_secret_value()}"},
        )
        data = self._json_object(response, "GitHub commit comparison")
        status = data.get("status")
        ahead_by = data.get("ahead_by")
        behind_by = data.get("behind_by")
        total_commits = data.get("total_commits")
        commits_value = data.get("commits")
        if (
            not isinstance(status, str)
            or not isinstance(ahead_by, int)
            or not isinstance(behind_by, int)
            or not isinstance(total_commits, int)
            or not isinstance(commits_value, list)
        ):
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT, "GitHub returned invalid commit comparison"
            )
        commits = cast(list[Any], commits_value)
        commit_shas: list[str] = []
        for item in commits:
            if not isinstance(item, dict):
                raise transient_error(
                    ErrorCode.CONNECTOR_TRANSIENT, "GitHub returned invalid commit comparison"
                )
            commit = cast(dict[str, Any], item)
            if not isinstance(commit.get("sha"), str):
                raise transient_error(
                    ErrorCode.CONNECTOR_TRANSIENT, "GitHub returned invalid commit comparison"
                )
            commit_shas.append(cast(str, commit["sha"]))
        try:
            return CommitComparison(
                repository=repository,
                base_sha=base_sha,
                head_sha=head_sha,
                status=status,
                ahead_by=ahead_by,
                behind_by=behind_by,
                total_commits=total_commits,
                commit_shas=commit_shas,
            )
        except ValidationError:
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT, "GitHub returned invalid commit comparison"
            ) from None

    async def download_repository_archive(
        self,
        repository: str,
        commit_sha: str,
        installation_id: int,
        *,
        max_bytes: int = 50 * 1024 * 1024,
    ) -> bytes:
        """Download a bounded tarball at an exact SHA for an ephemeral checkout."""

        repository = _validate_repository(repository, self._allowlist)
        if not _SHA_PATTERN.fullmatch(commit_sha):
            raise permanent_error(ErrorCode.INPUT_INVALID, "GitHub archive SHA is invalid")
        if max_bytes <= 0:
            raise ValueError("archive size limit must be positive")
        token = await self.exchange_installation_token(installation_id)
        response = await self._request(
            "GET",
            f"/repos/{repository}/tarball/{commit_sha}",
            headers={"Authorization": f"Bearer {token.token.get_secret_value()}"},
        )
        if len(response.content) > max_bytes:
            raise permanent_error(
                ErrorCode.INPUT_INVALID, "GitHub repository archive exceeds the size limit"
            )
        return response.content

    async def _request(
        self,
        method: Literal["GET", "POST"],
        path: str,
        *,
        headers: Mapping[str, str],
    ) -> httpx.Response:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(self._timeout_seconds))
        request_headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            **headers,
        }
        try:
            response = await client.request(
                method,
                f"{GITHUB_API_BASE_URL}{path}",
                headers=request_headers,
                timeout=self._timeout_seconds,
            )
        except httpx.TransportError:
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT, "GitHub transport is unavailable"
            ) from None
        finally:
            if owns_client:
                await client.aclose()
        if response.status_code in {401, 403}:
            raise authorization_error("GitHub authorization is invalid")
        if response.status_code == 429 or response.status_code >= 500:
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT, "GitHub is temporarily unavailable"
            )
        if response.status_code >= 400:
            raise permanent_error(ErrorCode.INPUT_INVALID, "GitHub rejected the request")
        return response

    @staticmethod
    def _json_object(response: httpx.Response, diagnostic: str) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT, diagnostic + " response is invalid"
            ) from None
        if not isinstance(data, dict):
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT, diagnostic + " response is invalid"
            )
        return cast(dict[str, Any], data)


__all__ = [
    "GITHUB_API_BASE_URL",
    "GITHUB_API_VERSION",
    "GITHUB_WEB_HOST",
    "CommitComparison",
    "GitHubAppConnector",
    "InstallationToken",
    "RepositoryMetadata",
    "normalize_push_event",
    "verify_webhook_signature",
]
