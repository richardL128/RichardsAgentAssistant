"""Safe, deterministic code-review report rendering tests (Phase 3).

Reports carry only validated metadata: no patch bodies, no scanner evidence
text, stable ordering, byte-identical repeat renders, neutralised Markdown,
and compact sorted JSON.
"""

from __future__ import annotations

import json

import pytest

from app.agents.code_review.contracts import ValidatedFinding
from app.agents.code_review.report import (
    render_json_report,
    render_markdown_report,
    render_report,
    report_payload,
)

REPO = "acme/example"
BASE = "a" * 40
HEAD = "b" * 40


def _finding(**overrides: object) -> ValidatedFinding:
    data: dict[str, object] = {
        "fingerprint": "a" * 64,
        "severity": "block",
        "path": "src/app.py",
        "line": 5,
        "title": "Unvalidated input reaches a command",
        "explanation": "The value reaches subprocess execution and allows argument injection.",
        "reproduction_or_missing_test": "Add a test asserting one validated argument is passed.",
        "confidence": 0.9,
        "assumptions": ["The endpoint is reachable by an untrusted caller."],
        "evidence_refs": ["diff:src/app.py:5"],
    }
    data.update(overrides)
    return ValidatedFinding(**data)  # type: ignore[arg-type]


def test_markdown_repeat_render_is_byte_identical() -> None:
    findings = [_finding(), _finding(fingerprint="c" * 64, path="src/other.py", line=2)]
    first = render_markdown_report(
        repository=REPO, base_sha=BASE, head_sha=HEAD, findings=findings, summary="ok"
    )
    second = render_markdown_report(
        repository=REPO, base_sha=BASE, head_sha=HEAD, findings=findings, summary="ok"
    )
    assert first == second


def test_json_ordering_is_stable_regardless_of_input_order() -> None:
    low = _finding(fingerprint="1" * 64, severity="suggestion", path="z.py", line=1)
    high = _finding(fingerprint="2" * 64, severity="block", path="a.py", line=9)
    mid = _finding(fingerprint="3" * 64, severity="important", path="m.py", line=3)

    forward = render_json_report(
        repository=REPO, base_sha=BASE, head_sha=HEAD, findings=[low, high, mid]
    )
    shuffled = render_json_report(
        repository=REPO, base_sha=BASE, head_sha=HEAD, findings=[mid, low, high]
    )
    assert forward == shuffled
    payload = json.loads(forward)
    # block first, then important, then suggestion.
    assert [item["path"] for item in payload["findings"]] == ["a.py", "m.py", "z.py"]


def test_json_is_compact_and_sorted() -> None:
    rendered = render_json_report(
        repository=REPO, base_sha=BASE, head_sha=HEAD, findings=[_finding()]
    )
    assert ", " not in rendered
    assert ": " not in rendered
    payload = json.loads(rendered)
    assert list(payload.keys()) == sorted(payload.keys())
    assert list(payload["findings"][0].keys()) == sorted(payload["findings"][0].keys())


def test_markdown_injection_is_neutralised() -> None:
    hostile = _finding(
        title="`whoami` <script>x</script>",
        explanation="line one\n# fake heading\n> quote\n- bullet\n* star\n+ plus",
        assumptions=["`x`"],
    )
    rendered = render_markdown_report(
        repository=REPO,
        base_sha=BASE,
        head_sha=HEAD,
        findings=[hostile],
        summary="`rm` <b> # top",
    )
    assert "`whoami`" not in rendered
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    # Leading block-level markers are escaped (or entity-encoded for angle
    # brackets) before the newlines are flattened to spaces.
    assert "\\# fake heading" in rendered
    assert "&gt; quote" in rendered
    assert "\\- bullet" in rendered
    assert "\\* star" in rendered
    assert "\\+ plus" in rendered
    assert "\n# fake heading" not in rendered
    # Triple backticks can never appear.
    assert "```" not in rendered


def test_no_patch_or_scanner_evidence_fields_leak_into_either_format() -> None:
    finding = _finding()
    payload = report_payload(repository=REPO, base_sha=BASE, head_sha=HEAD, findings=[finding])
    finding_keys = set(payload["findings"][0])
    assert finding_keys == {
        "fingerprint",
        "severity",
        "path",
        "line",
        "title",
        "explanation",
        "reproduction_or_missing_test",
        "confidence",
        "assumptions",
        "evidence_refs",
    }
    assert "patch" not in finding_keys
    assert "evidence" not in finding_keys

    markdown = render_markdown_report(
        repository=REPO, base_sha=BASE, head_sha=HEAD, findings=[finding]
    )
    # Only evidence identifiers, never scanner evidence prose.
    assert "diff:src/app.py:5" in markdown


def test_validate_identity_rejects_bad_repository_and_sha() -> None:
    with pytest.raises(ValueError, match="owner/name"):
        report_payload(repository="not-a-repo", base_sha=BASE, head_sha=HEAD, findings=[])
    with pytest.raises(ValueError, match="SHA"):
        report_payload(repository=REPO, base_sha="z" * 40, head_sha=HEAD, findings=[])
    with pytest.raises(ValueError, match="SHA"):
        report_payload(repository=REPO, base_sha=BASE.upper(), head_sha=HEAD, findings=[])


def test_render_report_dispatches_and_rejects_unknown_format() -> None:
    assert render_report(
        repository=REPO, base_sha=BASE, head_sha=HEAD, findings=[], format="markdown"
    ).startswith("# Code review report")
    assert render_report(
        repository=REPO, base_sha=BASE, head_sha=HEAD, findings=[], format="json"
    ).startswith("{")
    with pytest.raises(ValueError, match=r"markdown.*json"):
        render_report(repository=REPO, base_sha=BASE, head_sha=HEAD, findings=[], format="yaml")


def test_empty_findings_render_a_no_findings_section() -> None:
    markdown = render_markdown_report(repository=REPO, base_sha=BASE, head_sha=HEAD, findings=[])
    assert "No actionable findings." in markdown


def test_duplicate_findings_are_merged_before_rendering() -> None:
    first = _finding(evidence_refs=["diff:src/app.py:5"])
    second = _finding(
        fingerprint="d" * 64,
        title="Unvalidated input reaches command execution",
        explanation="The value reaches subprocess execution, allowing argument injection now.",
        evidence_refs=["file:src/app.py:5"],
    )
    payload = report_payload(
        repository=REPO, base_sha=BASE, head_sha=HEAD, findings=[first, second]
    )
    assert len(payload["findings"]) == 1
    assert set(payload["findings"][0]["evidence_refs"]) == {
        "diff:src/app.py:5",
        "file:src/app.py:5",
    }
