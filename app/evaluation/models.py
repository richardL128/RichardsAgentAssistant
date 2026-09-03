"""Pydantic schemas used by the small, versioned Phase 1 evaluation set."""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvaluationModel(BaseModel):
    """Common strict configuration for model-produced evaluation output."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Citation(EvaluationModel):
    """A compact pointer to evidence, never the source body itself."""

    source: str = Field(min_length=1, max_length=200)
    locator: str = Field(min_length=1, max_length=120)
    url: str | None = Field(default=None, max_length=500)


class CodeFindingTriage(EvaluationModel):
    """Triage one code finding.

    When ``finding_present`` is true, ``severity``, ``file``, and ``line`` must
    all be non-null. When it is false, all three must be JSON null. Never mix a
    dismissed finding with a retained severity or location.
    """

    finding_present: bool
    severity: Literal["block", "important", "suggestion"] | None
    file: str | None = Field(max_length=500)
    line: int | None = Field(ge=1)
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1, max_length=2_000)
    evidence: str = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def finding_has_location_and_severity(self) -> CodeFindingTriage:
        if self.finding_present and (
            self.severity is None or self.file is None or self.line is None
        ):
            raise ValueError("a finding requires severity, file, and line")
        if not self.finding_present and any(
            value is not None for value in (self.severity, self.file, self.line)
        ):
            raise ValueError("a dismissed finding cannot retain a severity or location")
        return self


class FinanceFact(EvaluationModel):
    """One source-backed fact, kept separate from model reasoning."""

    claim: str = Field(min_length=1, max_length=1_000)
    citation: Citation


class FinanceInference(EvaluationModel):
    """A clearly labelled inference and the evidence it is based on."""

    claim: str = Field(min_length=1, max_length=1_000)
    basis: list[Citation] = Field(min_length=1, max_length=5)
    uncertainty: str = Field(min_length=1, max_length=1_000)


class FinanceFactInference(EvaluationModel):
    """Separate cited facts from labelled inferences without trade directives.

    Inference claims, uncertainty, thesis impact, and counter-case must not use
    buy, sell, short, or purchase as instructions.
    """

    facts: list[FinanceFact] = Field(max_length=10)
    inferences: list[FinanceInference] = Field(max_length=10)
    thesis_impact: Literal["monitor", "revisit thesis", "no action"]
    counter_case: str = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def reject_trade_directives(self) -> FinanceFactInference:
        # Facts may quote a source that uses these words. LifeAgent's own
        # inference, uncertainty, counter-case, and impact must not direct a
        # transaction.
        analysis_text = " ".join(
            [
                self.thesis_impact,
                self.counter_case,
                *(inference.claim for inference in self.inferences),
                *(inference.uncertainty for inference in self.inferences),
            ]
        )
        if re.search(r"\b(?:buy|sell|short|purchase)\b", analysis_text, re.IGNORECASE):
            raise ValueError("finance evaluation cannot contain a buy/sell directive")
        return self


class AcademicExtraction(EvaluationModel):
    """Extract assessment facts with page/block citations.

    When the source contains conflicting due dates, set ``ambiguous`` true,
    explain the conflict in ``ambiguity_reason``, and set ``due_date`` to JSON
    null rather than choosing either date.
    """

    course: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    assessment_type: Literal["assignment", "quiz", "midterm", "final", "event"]
    due_date: date | None
    grade_weight_percent: float | None = Field(
        ge=0,
        le=100,
        description="Copy an explicitly stated percentage; otherwise use JSON null.",
    )
    scope: str | None = Field(max_length=1_000)
    ambiguous: bool
    ambiguity_reason: str | None = Field(max_length=1_000)
    citations: list[Citation] = Field(max_length=10)

    @model_validator(mode="after")
    def validate_ambiguity_and_evidence(self) -> AcademicExtraction:
        if self.ambiguous and not self.ambiguity_reason:
            raise ValueError("ambiguous extraction requires an ambiguity reason")
        if self.ambiguous and self.due_date is not None:
            raise ValueError("an ambiguous due date cannot become a hard constraint")
        if not self.citations:
            raise ValueError("academic extraction requires a source citation")
        return self


class StructuredSmokeOutput(EvaluationModel):
    """Small generic schema for valid JSON and malformed-output cases."""

    answer: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0, le=1)


# Friendly names for callers that prefer the domain-specific suffix.
CodeFindingTriageOutput = CodeFindingTriage
FinanceFactInferenceOutput = FinanceFactInference
AcademicExtractionOutput = AcademicExtraction
