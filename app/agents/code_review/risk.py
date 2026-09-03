"""Deterministic code-review risk classification."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import PurePosixPath

from app.agents.code_review.contracts import RiskLevel

_HIGH_MARKERS = frozenset(
    {
        "auth",
        "authentication",
        "authorization",
        "identity",
        "login",
        "oauth",
        "permission",
        "permissions",
        "rbac",
        "acl",
        "payment",
        "payments",
        "billing",
        "checkout",
        "migration",
        "migrations",
        "database",
        "infra",
        "infrastructure",
        "terraform",
        "kubernetes",
        "dockerfile",
        "deploy",
        "deployment",
        "requirements",
        "pyproject.toml",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "poetry.lock",
        "uv.lock",
    }
)
_LOW_SUFFIXES = (
    ".md",
    ".mdx",
    ".rst",
    ".txt",
    ".adoc",
    ".html",
    ".css",
    ".scss",
    ".less",
    ".prettierrc",
)
_LOW_MARKERS = frozenset({"docs", "doc", "documentation", "format", "formatting", "generated"})


def _tokens(path: str) -> tuple[str, ...]:
    normalized = path.replace("\\", "/").lower()
    parts = PurePosixPath(normalized).parts
    tokens: list[str] = []
    for part in parts:
        tokens.extend(part.replace(".", "_").replace("-", "_").split("_"))
        tokens.append(part)
    return tuple(tokens)


def classify_risk(paths: Iterable[str]) -> tuple[RiskLevel, list[str]]:
    """Return a stable risk level and reasons for changed repository paths.

    High-risk markers take precedence over documentation/format-only changes;
    ordinary source changes are medium.  Reasons are sorted and deduplicated
    to ensure identical commits produce identical packet metadata.
    """

    high: set[str] = set()
    low = True
    saw_path = False
    for raw_path in paths:
        saw_path = True
        path = raw_path.lower().replace("\\", "/")
        tokens = set(_tokens(path))
        matched_high = sorted(tokens & _HIGH_MARKERS)
        if matched_high:
            high.update(matched_high)
        is_low = path.endswith(_LOW_SUFFIXES) or bool(tokens & _LOW_MARKERS)
        low = low and is_low
    if high:
        reasons = [f"high-risk path marker: {marker}" for marker in sorted(high)]
        return RiskLevel.HIGH, reasons
    if saw_path and low:
        return RiskLevel.LOW, ["documentation/format/generated-only change"]
    return RiskLevel.MEDIUM, ["application change requires review"]


def classify_paths(paths: Iterable[str]) -> RiskLevel:
    """Convenience level-only classifier."""

    return classify_risk(paths)[0]


__all__ = ["classify_paths", "classify_risk"]
