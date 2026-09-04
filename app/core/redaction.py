"""Deterministic redaction for data crossing the artifact boundary.

Redaction deliberately operates on text only.  Treating arbitrary bytes as
UTF-8 would make PDFs and other binary artifacts corrupt, so callers must
explicitly identify already-redacted binary data before it is persisted.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_REDACTED = "[REDACTED]"

# Keep the field name while removing its value.  These patterns cover both
# human-readable connector logs and JSON-ish key/value output.
_HEADER_PATTERN = re.compile(
    r"(?im)^(?P<label>\s*(?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*)"
    r"[^\r\n]*"
)
_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(?P<label>\b(?:[a-z0-9]+[_-])*(?:authorization|proxy-authorization|cookie|set-cookie|"
    r"api[_ -]?key|access[_ -]?token|refresh[_ -]?token|"
    r"auth(?:entication)?[_ -]?token|token|secret|password|passwd|private[_ -]?key|"
    r"client[_ -]?secret)\b[\"']?\s*[:=]\s*)"
    r"(?:"
    r"(?P<quote>[\"'])(?P<quoted_value>.*?)(?P=quote)"
    r"|(?P<already_redacted>\[REDACTED\])"
    r"|(?P<unquoted_value>[^\s,;\]}\"']+)"
    r")"
)
_URL_USERINFO_PATTERN = re.compile(
    r"(?i)(?P<prefix>https?://)(?P<userinfo>[^/\s@]+(?::[^/\s@]*)?@)(?P<host>[^/\s]+)"
)
_PRIVATE_LABEL_PATTERN = re.compile(
    r"(?im)(?P<label>\b(?:private(?:[ _-](?:discord|text|message|content))*|"
    r"discord[ _-]?private(?:[ _-](?:text|message|content))*|"
    r"portfolio[ _-]?(?:id|identifier))\b\s*[:=]\s*)"
    r"(?P<quote>[\"']?)(?P<value>[^\r\n\"']+?)(?P=quote)(?=\s*(?:$|[,;}]))"
)
_PRIVATE_BLOCK_PATTERN = re.compile(
    r"(?is)(?P<open>\[private\]|<private>) .*? (?P<close>\[/private\]|</private>)",
    re.VERBOSE,
)


def redact_text(value: str, *, secrets: Iterable[str] = ()) -> str:
    """Return *value* with credential and explicitly private values removed.

    Caller-supplied literals are replaced first and are sorted longest-first so
    overlapping values cannot expose a suffix of a longer secret.  The
    function does not log or retain either its input or the supplied secrets.
    """

    redacted = value
    literals = sorted({secret for secret in secrets if secret}, key=len, reverse=True)
    for secret in literals:
        redacted = redacted.replace(secret, _REDACTED)

    redacted = _URL_USERINFO_PATTERN.sub(
        lambda match: f"{match.group('prefix')}{_REDACTED}@{match.group('host')}", redacted
    )
    redacted = _ASSIGNMENT_PATTERN.sub(
        lambda match: _replace_assignment(match),
        redacted,
    )
    redacted = _HEADER_PATTERN.sub(lambda match: f"{match.group('label')}{_REDACTED}", redacted)
    redacted = _PRIVATE_LABEL_PATTERN.sub(
        lambda match: (
            f"{match.group('label')}{match.group('quote')}{_REDACTED}{match.group('quote')}"
        ),
        redacted,
    )
    return _PRIVATE_BLOCK_PATTERN.sub(
        lambda match: f"{match.group('open')}{_REDACTED}{match.group('close')}", redacted
    )


def is_text_media_type(media_type: str) -> bool:
    """Whether an artifact media type is safe to decode and redact as text."""

    normalized = media_type.split(";", 1)[0].strip().lower()
    return (
        normalized.startswith("text/")
        or normalized
        in {
            "application/json",
            "application/ld+json",
            "application/problem+json",
            "application/xml",
            "application/x-www-form-urlencoded",
        }
        or normalized.endswith(("+json", "+xml"))
    )


def redact_bytes(
    value: bytes,
    *,
    media_type: str,
    secrets: Iterable[str] = (),
    already_redacted: bool = False,
) -> bytes:
    """Redact textual bytes, or preserve explicitly safe binary bytes.

    ``ValueError`` is raised for binary data without an explicit redaction
    attestation.  This fail-closed behavior prevents accidental PDF/binary
    corruption and keeps the artifact boundary auditable.
    """

    if not is_text_media_type(media_type):
        if already_redacted:
            return value
        raise ValueError(
            "binary artifacts must be supplied with already_redacted=True; "
            "raw PDF/binary content is not transformed"
        )
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("text artifacts must contain valid UTF-8 bytes") from exc
    return redact_text(text, secrets=secrets).encode("utf-8")


__all__ = ["is_text_media_type", "redact_bytes", "redact_text"]


def _replace_assignment(match: re.Match[str]) -> str:
    if match.group("already_redacted") is not None:
        return match.group(0)
    quote = match.group("quote") or ""
    return f"{match.group('label')}{quote}{_REDACTED}{quote}"
