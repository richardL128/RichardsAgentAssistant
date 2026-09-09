"""A small, local content-addressed artifact store.

Only generated SHA-256 keys are used to construct filesystem paths.  Metadata
is kept beside the payload as a JSON sidecar so relational rows can reference
an artifact without storing large bodies.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.core.redaction import redact_bytes

_KEY_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")
_CLASS_PATTERN = re.compile(r"\A[a-zA-Z][a-zA-Z0-9_.-]{0,63}\Z")
_IMMUTABLE_CLASSES = frozenset({"audit", "audit_event", "audit_metadata", "run_summary"})
_DEFAULT_RETENTION_DAYS = 30


class UnsafeArtifactError(ValueError):
    """Raised when unredacted binary data is presented to the store."""


@dataclass(frozen=True, slots=True)
class ArtifactMetadata:
    """Persisted metadata for one artifact payload."""

    key: str
    media_type: str
    data_class: str
    size: int
    created_at: datetime
    retention_policy: str
    expires_at: datetime | None

    def as_dict(self) -> dict[str, Any]:
        """Serialize metadata without including artifact contents."""

        result = asdict(self)
        result["created_at"] = self.created_at.isoformat()
        result["expires_at"] = self.expires_at.isoformat() if self.expires_at else None
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ArtifactMetadata:
        """Load and type-check a metadata sidecar."""

        created_at = _parse_timestamp(value["created_at"])
        expires_at_value = value.get("expires_at")
        expires_at = _parse_timestamp(expires_at_value) if expires_at_value else None
        return cls(
            key=_validate_key(str(value["key"])),
            media_type=str(value["media_type"]),
            data_class=_validate_data_class(str(value["data_class"])),
            size=int(value["size"]),
            created_at=created_at,
            retention_policy=str(value["retention_policy"]),
            expires_at=expires_at,
        )


class ArtifactStore:
    """Persist redacted artifacts under a configured root directory.

    ``retention_days_by_class`` maps a class to days, or ``None`` for retain
    forever.  Audit/run-summary classes are retained forever by default.
    """

    def __init__(
        self,
        root: Path,
        *,
        retention_days_by_class: Mapping[str, int | None] | None = None,
        default_retention_days: int = _DEFAULT_RETENTION_DAYS,
        clock: Callable[[], datetime] | None = None,
        create_root: bool = True,
    ) -> None:
        if default_retention_days < 1:
            raise ValueError("default_retention_days must be positive")
        self.root = root.expanduser().resolve()
        if create_root:
            self.root.mkdir(parents=True, exist_ok=True)
        self._retention_days = dict(retention_days_by_class or {})
        for data_class, days in self._retention_days.items():
            _validate_data_class(data_class)
            if days is not None and days < 1:
                raise ValueError("retention days must be positive or None")
        self._default_retention_days = default_retention_days
        self._clock = clock or (lambda: datetime.now(UTC))

    def put(
        self,
        data: bytes | str,
        *,
        media_type: str,
        data_class: str,
        secrets: tuple[str, ...] = (),
        already_redacted: bool = False,
    ) -> ArtifactMetadata:
        """Redact and atomically persist *data*, returning typed metadata."""

        data_class = _validate_data_class(data_class)
        media_type = _validate_media_type(media_type)
        literal_secrets = tuple(secrets)
        if any(
            secret and (secret in data_class or secret in media_type) for secret in literal_secrets
        ):
            raise ValueError("caller-supplied secrets cannot be included in artifact metadata")
        if isinstance(data, str):
            if not _is_text_media_type(media_type):
                raise UnsafeArtifactError("string data requires a textual media type")
            payload = redact_bytes(
                data.encode("utf-8"),
                media_type=media_type,
                secrets=literal_secrets,
                already_redacted=already_redacted,
            )
        else:
            try:
                payload = redact_bytes(
                    data,
                    media_type=media_type,
                    secrets=literal_secrets,
                    already_redacted=already_redacted,
                )
            except ValueError as exc:
                raise UnsafeArtifactError(str(exc)) from exc

        key = hashlib.sha256(payload).hexdigest()
        artifact_path, metadata_path = self._paths_for_key(key)
        created_at = _as_utc(self._clock())
        expires_at, retention_policy = self._retention_for(data_class, created_at)
        metadata = ArtifactMetadata(
            key=key,
            media_type=media_type,
            data_class=data_class,
            size=len(payload),
            created_at=created_at,
            retention_policy=retention_policy,
            expires_at=expires_at,
        )
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        if artifact_path.exists() or metadata_path.exists():
            if not artifact_path.exists() or not metadata_path.exists():
                raise OSError("artifact payload and metadata are inconsistent")
            existing = self.get_metadata(key)
            if existing.media_type != media_type or existing.data_class != data_class:
                raise ValueError("existing artifact metadata conflicts with this content")
            return existing
        _atomic_write(artifact_path, payload)
        _atomic_write(
            metadata_path,
            json.dumps(metadata.as_dict(), sort_keys=True, separators=(",", ":")).encode(),
        )
        return metadata

    def get(self, key: str) -> bytes:
        """Read an artifact by its generated SHA-256 key."""

        artifact_path, _ = self._paths_for_key(key)
        return artifact_path.read_bytes()

    def get_metadata(self, key: str) -> ArtifactMetadata:
        """Read typed metadata by its generated SHA-256 key."""

        _, metadata_path = self._paths_for_key(key)
        with metadata_path.open(encoding="utf-8") as handle:
            value = json.load(handle)
        return ArtifactMetadata.from_dict(value)

    def retention_candidate(self, key: str, *, now: datetime | None = None) -> bool:
        """Whether an artifact is eligible for retention pruning."""

        metadata = self.get_metadata(key)
        expires_at = metadata.expires_at
        return expires_at is not None and _as_utc(now or self._clock()) >= expires_at

    def prune(self, *, now: datetime | None = None) -> tuple[str, ...]:
        """Delete expired payload/sidecar pairs and return removed keys."""

        current = _as_utc(now or self._clock())
        removed: list[str] = []
        for metadata_path in self.root.glob("**/*.json"):
            if metadata_path.is_symlink():
                continue
            try:
                with metadata_path.open(encoding="utf-8") as handle:
                    metadata = ArtifactMetadata.from_dict(json.load(handle))
                if metadata.expires_at is None or current < metadata.expires_at:
                    continue
                artifact_path, expected_metadata_path = self._paths_for_key(metadata.key)
                if expected_metadata_path != metadata_path:
                    continue
                artifact_path.unlink(missing_ok=True)
                metadata_path.unlink(missing_ok=True)
                removed.append(metadata.key)
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                # A malformed/unrelated file is not a reason to delete anything.
                continue
        return tuple(sorted(removed))

    def _retention_for(self, data_class: str, created_at: datetime) -> tuple[datetime | None, str]:
        days = self._retention_days.get(data_class, self._default_retention_days)
        if data_class in _IMMUTABLE_CLASSES and data_class not in self._retention_days:
            return None, "retain-forever"
        if days is None:
            return None, "retain-forever"
        return created_at + timedelta(days=days), f"{days}d"

    def _paths_for_key(self, key: str) -> tuple[Path, Path]:
        key = _validate_key(key)
        directory = self.root / key[:2]
        artifact_path = directory / key[2:]
        metadata_path = directory / f"{key[2:]}.json"
        for path in (directory, artifact_path, metadata_path):
            _ensure_inside_root(self.root, path)
        return artifact_path, metadata_path


def _atomic_write(path: Path, data: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _ensure_inside_root(root: Path, path: Path) -> None:
    try:
        path.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise ValueError("artifact path escapes configured root") from exc


def _validate_key(key: str) -> str:
    if not _KEY_PATTERN.fullmatch(key):
        raise ValueError("artifact key must be a 64-character lowercase SHA-256 hex digest")
    return key


def _validate_data_class(data_class: str) -> str:
    if not _CLASS_PATTERN.fullmatch(data_class):
        raise ValueError("data_class must be a short safe identifier")
    return data_class


def _validate_media_type(media_type: str) -> str:
    normalized = media_type.strip()
    if not normalized or any(character in normalized for character in "\r\n"):
        raise ValueError("media_type must be a non-empty single-line value")
    return normalized


def _is_text_media_type(media_type: str) -> bool:
    from app.core.redaction import is_text_media_type

    return is_text_media_type(media_type)


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("metadata timestamp must be an ISO-8601 string")
    return _as_utc(datetime.fromisoformat(value))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("artifact timestamps must be timezone-aware")
    return value.astimezone(UTC)


__all__ = ["ArtifactMetadata", "ArtifactStore", "UnsafeArtifactError"]
