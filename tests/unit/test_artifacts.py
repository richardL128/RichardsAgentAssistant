"""Unit coverage for the redacted local artifact store."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.artifacts import ArtifactStore, UnsafeArtifactError
from app.core.redaction import redact_text

_NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


def test_redacts_credentials_urls_private_markers_and_supplied_literals() -> None:
    source = (
        "Authorization: Bearer bearer-value\n"
        "Cookie: session=session-value\n"
        "api_key=key-value password: pass-value\n"
        "https://user:password@example.test/private\n"
        '{"authorization": "Bearer json-secret", "password": "two words"}\n'
        "portfolio_id: portfolio-123\n"
        "private_discord_text: hello from Discord\n"
        "[private]a Discord message[/private]"
    )

    redacted = redact_text(source, secrets=("bearer-value", "portfolio-123"))

    assert "bearer-value" not in redacted
    assert "session-value" not in redacted
    assert "key-value" not in redacted
    assert "pass-value" not in redacted
    assert "json-secret" not in redacted
    assert "two words" not in redacted
    assert "user:password@" not in redacted
    assert "portfolio-123" not in redacted
    assert "private_discord_text: [REDACTED]" in redacted
    assert "[REDACTED]" in redacted


def test_same_redacted_content_deduplicates_and_has_safe_metadata(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path, clock=lambda: _NOW)
    first = store.put(
        "Authorization: Bearer first-secret\nbody",
        media_type="text/plain",
        data_class="tool_log",
        secrets=("first-secret",),
    )
    second = store.put(
        "Authorization: Bearer second-secret\nbody",
        media_type="text/plain",
        data_class="tool_log",
        secrets=("second-secret",),
    )

    assert first.key == second.key
    assert store.get(first.key) == b"Authorization: [REDACTED]\nbody"
    metadata = store.get_metadata(first.key)
    assert metadata.key == first.key
    assert metadata.media_type == "text/plain"
    assert metadata.data_class == "tool_log"
    assert metadata.size == len(store.get(first.key))
    assert "first-secret" not in metadata.key
    assert "first-secret" not in str(metadata.as_dict())
    assert list(tmp_path.rglob("*.tmp")) == []
    with pytest.raises(ValueError, match="metadata"):
        store.put(
            "safe body",
            media_type="text/plain",
            data_class="secret-label",
            secrets=("secret-label",),
        )


def test_binary_and_pdf_content_require_redaction_attestation(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)

    with pytest.raises(UnsafeArtifactError, match="already_redacted"):
        store.put(b"%PDF-raw-content", media_type="application/pdf", data_class="extract")

    payload = b"%PDF-already-redacted-content\x00\xff"
    metadata = store.put(
        payload,
        media_type="application/pdf",
        data_class="extract",
        already_redacted=True,
    )
    assert store.get(metadata.key) == payload


def test_text_is_redacted_even_when_caller_attests_binary_safety(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)

    metadata = store.put(
        "token=must-not-persist",
        media_type="text/plain",
        data_class="tool_log",
        secrets=("must-not-persist",),
        already_redacted=True,
    )

    assert store.get(metadata.key) == b"token=[REDACTED]"


def test_key_validation_prevents_traversal(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)

    with pytest.raises(ValueError, match="64-character"):
        store.get("../../outside")
    with pytest.raises(ValueError, match="safe identifier"):
        store.put("data", media_type="text/plain", data_class="../outside")
    with pytest.raises(ValueError, match="single-line"):
        store.put("data", media_type="text/plain\nX-Leak: secret", data_class="log")


def test_retention_prunes_expired_data_but_keeps_immutable_metadata(tmp_path: Path) -> None:
    store = ArtifactStore(
        tmp_path,
        retention_days_by_class={"raw_extract": 1, "keep": None},
        clock=lambda: _NOW,
    )
    expired = store.put("old", media_type="text/plain", data_class="raw_extract")
    retained = store.put("summary", media_type="text/plain", data_class="run_summary")
    configured = store.put("keep", media_type="text/plain", data_class="keep")
    later = _NOW + timedelta(days=2)

    assert store.retention_candidate(expired.key, now=later)
    assert not store.retention_candidate(retained.key, now=later)
    assert store.prune(now=later) == (expired.key,)
    with pytest.raises(FileNotFoundError):
        store.get(expired.key)
    assert store.get(retained.key) == b"summary"
    assert store.get(configured.key) == b"keep"


def test_default_retention_and_conflicting_metadata_are_enforced(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path, default_retention_days=7, clock=lambda: _NOW)
    metadata = store.put("same", media_type="text/plain", data_class="raw_extract")

    assert metadata.expires_at == _NOW + timedelta(days=7)
    with pytest.raises(ValueError, match="metadata conflicts"):
        store.put("same", media_type="text/plain", data_class="tool_log")
