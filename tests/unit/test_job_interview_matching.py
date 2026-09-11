from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.agents.job_interviews.contracts import (
    ApplicationRowSnapshot,
    InterviewEventSnapshot,
    UrlCandidate,
)
from app.agents.job_interviews.matching import (
    ApplicationInterpretationDecision,
    InterviewMatchDecision,
    MatchEvidence,
    PostingUrlDecision,
    SemanticFieldEvidence,
    validate_application_interpretation,
    validate_interview_match,
    validate_posting_url_selection,
)


def _row(row_id: str, company: str, role: str) -> ApplicationRowSnapshot:
    return ApplicationRowSnapshot(
        table_block_id="table-1",
        row_block_id=row_id,
        row_order=1,
        cells=(company, role, "Interview"),
        normalized_cells=(company, role, "Interview"),
        content_fingerprint=f"fingerprint-{row_id}",
        last_seen_at=datetime(2026, 9, 10, tzinfo=UTC),
    )


def _interview() -> InterviewEventSnapshot:
    return InterviewEventSnapshot(
        interview_page_id="interview-1",
        title="Shopify Backend Developer Technical Round",
        local_date=date(2026, 9, 20),
        is_all_day=True,
        last_edited_at=datetime(2026, 9, 10, tzinfo=UTC),
        content_fingerprint="interview-fingerprint",
    )


def test_flexible_row_interpretation_requires_cell_evidence() -> None:
    row = _row("row-1", "Shopify", "Backend Developer")
    decision = ApplicationInterpretationDecision(
        application_row_id="row-1",
        display_name="Shopify Backend Developer",
        company="Shopify",
        role="Backend Developer",
        status="Interview",
        source_evidence=(
            SemanticFieldEvidence(field="display_name", cell_indexes=(0, 1)),
            SemanticFieldEvidence(field="company", cell_indexes=(0,)),
            SemanticFieldEvidence(field="role", cell_indexes=(1,)),
            SemanticFieldEvidence(field="status", cell_indexes=(2,)),
        ),
    )
    interpretation = validate_application_interpretation(
        decision,
        row,
        headers=("Whatever", "Changed order", "Free text"),
        interpreted_at=datetime(2026, 9, 10, tzinfo=UTC),
    )
    assert interpretation.company_name == "Shopify"
    assert {item.column_index for item in interpretation.evidence} == {0, 1, 2}


def test_unevidenced_derived_fact_is_rejected() -> None:
    with pytest.raises(ValueError, match="lacks source-cell evidence"):
        validate_application_interpretation(
            ApplicationInterpretationDecision(
                application_row_id="row-1",
                company="Shopify",
            ),
            _row("row-1", "Shopify", "Backend Developer"),
        )


def test_unique_semantic_match_accepts_only_known_quoted_sources() -> None:
    rows = (_row("shopify", "Shopify", "Backend Developer"), _row("stripe", "Stripe", "Backend"))
    match = validate_interview_match(
        InterviewMatchDecision(
            outcome="matched",
            application_row_id="shopify",
            reason="Company and role are unique.",
            candidate_ids=("shopify",),
            evidence=(
                MatchEvidence(source="interview_title", text="Shopify Backend Developer"),
                MatchEvidence(
                    source="application_cell",
                    text="Shopify",
                    application_row_id="shopify",
                    cell_index=0,
                ),
                MatchEvidence(
                    source="application_cell",
                    text="Backend Developer",
                    application_row_id="shopify",
                    cell_index=1,
                ),
            ),
        ),
        _interview(),
        rows,
    )
    assert match.row_block_id == "shopify"
    assert match.state == "matched"


def test_similar_roles_require_clarification_instead_of_guessing() -> None:
    rows = (_row("one", "Shopify", "Backend Developer"), _row("two", "Shopify", "Backend Engineer"))
    match = validate_interview_match(
        InterviewMatchDecision(
            outcome="needs_clarification",
            reason="Two Shopify backend applications remain plausible.",
            candidate_ids=("one", "two"),
            missing_key_identity=True,
        ),
        _interview(),
        rows,
    )
    assert match.state == "needs_clarification"
    assert match.row_block_id is None


def test_unknown_id_or_fabricated_evidence_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown candidate"):
        validate_interview_match(
            InterviewMatchDecision(
                outcome="needs_clarification",
                reason="Unknown candidate",
                candidate_ids=("not-supplied",),
            ),
            _interview(),
            (_row("known", "Shopify", "Backend Developer"),),
        )


def test_posting_url_selection_accepts_only_exact_supplied_source_pair() -> None:
    candidates = (
        UrlCandidate(
            url="https://careers.shopify.com/backend",
            source_kind="property",
            source_id="posting-property",
            label="Job posting",
        ),
        UrlCandidate(
            url="https://shopify.com/about",
            source_kind="block",
            source_id="about-block",
            label="Company",
        ),
    )
    selected = validate_posting_url_selection(
        PostingUrlDecision(
            outcome="selected",
            url=candidates[0].url,
            source_id=candidates[0].source_id,
            reason="The label identifies the job posting.",
        ),
        candidates,
    )
    assert selected == candidates[0]

    with pytest.raises(ValueError, match="unknown URL candidate"):
        validate_posting_url_selection(
            PostingUrlDecision(
                outcome="selected",
                url="https://attacker.invalid/invented",
                source_id="posting-property",
                reason="Invented by the model.",
            ),
            candidates,
        )


def test_posting_url_clarification_cannot_smuggle_a_selection() -> None:
    with pytest.raises(ValueError, match="cannot silently select"):
        validate_posting_url_selection(
            PostingUrlDecision(
                outcome="needs_clarification",
                url="https://careers.shopify.com/backend",
                source_id="posting-property",
                reason="Ambiguous.",
            ),
            (),
        )
