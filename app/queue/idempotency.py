"""Validation and construction of deterministic queue idempotency keys."""

from __future__ import annotations

import re
from collections.abc import Iterable

_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_NAMESPACE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,63}(?::[A-Za-z0-9][A-Za-z0-9._-]{0,127})+$")
MAX_IDEMPOTENCY_KEY_LENGTH = 512


class IdempotencyKeyError(ValueError):
    """Raised when a queue idempotency key is unsafe or malformed."""


def validate_idempotency_key(key: object) -> str:
    """Validate and return *key* in its original form.

    Keys are intentionally human-readable, ASCII, and bounded.  The caller
    must include a version segment (for example ``:v1``) when changing the
    semantics of an item; this function does not impose a version so existing
    provider keys such as ``review:repo:sha`` remain valid.
    """

    if not isinstance(key, str):
        raise IdempotencyKeyError("idempotency key must be a string")
    if not key or len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise IdempotencyKeyError("idempotency key must be 1-512 characters")
    if _KEY.fullmatch(key) is None:
        raise IdempotencyKeyError(
            "idempotency key must use colon-separated ASCII segments without whitespace"
        )
    return key


def build_idempotency_key(
    namespace: object,
    *parts: object,
    version: str | None = "v1",
) -> str:
    """Build a deterministic key from validated semantic parts.

    No timestamps, random values, or process-local identifiers are introduced;
    identical inputs therefore always produce one identical key.
    """

    if not isinstance(namespace, str) or _NAMESPACE.fullmatch(namespace) is None:
        raise IdempotencyKeyError("namespace must be lowercase ASCII and contain no spaces")
    valid_parts: list[str] = []
    all_parts: Iterable[object] = parts
    for part in all_parts:
        if not isinstance(part, str) or _PART.fullmatch(part) is None:
            raise IdempotencyKeyError("idempotency parts must be non-empty safe ASCII segments")
        valid_parts.append(part)
    if version is not None:
        if re.fullmatch(r"v[0-9]+", version) is None:
            raise IdempotencyKeyError("version must look like v1")
        valid_parts.append(version)
    if not valid_parts:
        raise IdempotencyKeyError("at least one semantic part is required")
    return validate_idempotency_key(":".join((namespace, *valid_parts)))
