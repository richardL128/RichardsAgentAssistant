"""Deterministic risk-classification tests (Phase 3).

Determinism is an explicit Phase 2 contract: identical input must always
produce identical level and reasons, sorted and deduplicated.
"""

from __future__ import annotations

from app.agents.code_review.contracts import RiskLevel
from app.agents.code_review.risk import classify_paths, classify_risk


def test_high_marker_beats_documentation_only() -> None:
    level, reasons = classify_risk(["docs/README.md", "src/auth/login.py"])
    assert level is RiskLevel.HIGH
    assert reasons == ["high-risk path marker: auth", "high-risk path marker: login"]


def test_documentation_and_format_only_is_low() -> None:
    assert classify_risk(["docs/guide.md", "CHANGELOG.rst", "styles/site.css"])[0] is RiskLevel.LOW
    assert classify_risk(["notes.txt"])[1] == ["documentation/format/generated-only change"]


def test_ordinary_source_is_medium() -> None:
    level, reasons = classify_risk(["src/service.py", "lib/util.go"])
    assert level is RiskLevel.MEDIUM
    assert reasons == ["application change requires review"]


def test_no_paths_is_medium() -> None:
    assert classify_risk([])[0] is RiskLevel.MEDIUM


def test_mixed_docs_and_source_is_medium_not_low() -> None:
    assert classify_risk(["docs/guide.md", "src/service.py"])[0] is RiskLevel.MEDIUM


def test_reasons_are_sorted_deduplicated_and_stable_for_identical_input() -> None:
    paths = [
        "src/payments/charge.py",
        "src/auth/login.py",
        "src/auth/login.py",
        "infra/main.tf",
    ]
    level, reasons = classify_risk(paths)
    assert level is RiskLevel.HIGH
    # Sorted, no duplicate "auth" despite the repeated path.
    assert reasons == [
        "high-risk path marker: auth",
        "high-risk path marker: infra",
        "high-risk path marker: login",
        "high-risk path marker: payments",
    ]
    assert reasons == sorted(reasons)
    # Byte-for-byte stable across repeated calls and input orderings.
    assert classify_risk(paths) == (level, reasons)
    assert classify_risk(list(reversed(paths))) == (level, reasons)


def test_high_marker_matches_manifest_filenames() -> None:
    assert classify_risk(["pyproject.toml"])[0] is RiskLevel.HIGH
    assert classify_risk(["frontend/package-lock.json"])[0] is RiskLevel.HIGH


def test_classify_paths_returns_only_the_level() -> None:
    assert classify_paths(["docs/x.md"]) is RiskLevel.LOW
    assert classify_paths(["src/app.py"]) is RiskLevel.MEDIUM
    assert classify_paths(["src/auth.py"]) is RiskLevel.HIGH
