"""Unit coverage for the offline Phase 1 evaluation harness."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.evaluation import EvaluationHarness, load_fixtures
from app.evaluation.models import (
    AcademicExtraction,
    CodeFindingTriage,
    FinanceFactInference,
    StructuredSmokeOutput,
)
from app.llm.contracts import InvocationResult, InvocationStatus, ModelCallTelemetry

FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "evaluation"
FIXED_TIME = datetime(2026, 1, 1, tzinfo=UTC)


class FixtureGateway:
    """Deterministic fake gateway; no Ollama process is contacted by these tests."""

    def __init__(self, fixture_outputs: dict[str, object]) -> None:
        self.fixture_outputs = fixture_outputs
        self.active = 0
        self.peak_active = 0
        self.invocations = 0

    async def invoke_structured(
        self, *, prompt: str, response_model: type[object]
    ) -> InvocationResult[object]:
        fixture_id = prompt.split("fixture_id=", 1)[-1].split(maxsplit=1)[0]
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        self.invocations += 1
        try:
            await asyncio.sleep(0)
            raw_output = self.fixture_outputs[fixture_id]
            telemetry = [
                ModelCallTelemetry(
                    request_id=uuid4(),
                    attempt=1,
                    model_identity="synthetic-model:v1",
                    config_version="phase1-test-config-v1",
                    started_at=FIXED_TIME,
                    finished_at=FIXED_TIME,
                    model_started_at=FIXED_TIME,
                    model_finished_at=FIXED_TIME,
                    latency_ms=10.0 + self.invocations,
                    queue_wait_ms=0,
                    model_latency_ms=10.0 + self.invocations,
                    input_characters=100,
                    output_characters=len(json.dumps(raw_output, default=str)),
                    estimated_input_tokens=25,
                    estimated_output_tokens=12,
                    output_valid=True,
                )
            ]
            if isinstance(raw_output, str):
                return InvocationResult(
                    request_id=telemetry[0].request_id,
                    status=InvocationStatus.INVALID_OUTPUT,
                    raw_text=raw_output,
                    telemetry=telemetry,
                    error_code="invalid_json",
                )
            try:
                output = response_model.model_validate(raw_output)  # type: ignore[attr-defined]
            except ValidationError:
                return InvocationResult(
                    request_id=telemetry[0].request_id,
                    status=InvocationStatus.INVALID_OUTPUT,
                    raw_text=json.dumps(raw_output),
                    telemetry=telemetry,
                    error_code="schema_validation_failed",
                )
            return InvocationResult(
                request_id=telemetry[0].request_id,
                status=InvocationStatus.VALID,
                output=output,
                raw_text=json.dumps(raw_output),
                telemetry=telemetry,
            )
        finally:
            self.active -= 1


def _fixtures() -> list[object]:
    return load_fixtures(FIXTURE_DIR)


def test_domain_schemas_enforce_evidence_and_finance_boundary() -> None:
    with pytest.raises(ValidationError):
        FinanceFactInference(
            facts=[],
            inferences=[],
            thesis_impact="buy",
            counter_case="Not a trade instruction.",
        )

    with pytest.raises(ValidationError):
        AcademicExtraction(
            course="HIST-201",
            title="Research brief",
            assessment_type="assignment",
            ambiguous=True,
            citations=[],
        )

    finding = CodeFindingTriage(
        finding_present=False,
        confidence=0.8,
        rationale="No actionable issue was substantiated.",
        evidence="The deterministic check found no changed behavior.",
    )
    assert finding.severity is None
    assert StructuredSmokeOutput(answer="ok", confidence=1).confidence == 1


def test_load_versioned_fixture_set_covers_required_cases() -> None:
    fixtures = _fixtures()
    fixture_ids = {fixture.fixture_id for fixture in fixtures}  # type: ignore[union-attr]
    assert len(fixtures) == 6
    assert {fixture.domain for fixture in fixtures} >= {  # type: ignore[union-attr]
        "code_review",
        "finance",
        "academic",
        "structured_output",
    }
    assert "malformed-json-v1" in fixture_ids
    assert "invalid-structured-output-v1" in fixture_ids


def test_harness_validates_outputs_reports_percentiles_and_is_deterministic(tmp_path: Path) -> None:
    fixtures = load_fixtures(FIXTURE_DIR)
    # Include the fixture id in each prompt so the fake can route without
    # exposing a second gateway API or contacting Ollama.
    fixtures = [
        fixture.model_copy(update={"prompt": f"fixture_id={fixture.fixture_id} {fixture.prompt}"})
        for fixture in fixtures
    ]
    outputs = {fixture.fixture_id: fixture.expected_output for fixture in fixtures}
    gateway = FixtureGateway(outputs)
    harness = EvaluationHarness(gateway)

    async def run() -> object:
        return await harness.run(fixtures, tmp_path / "reports" / "benchmark.json")

    report = asyncio.run(run())
    assert report.metrics.total_evaluations == 6
    assert report.metrics.valid_evaluations == 4
    assert report.metrics.validity_rate == pytest.approx(2 / 3)
    assert report.metrics.passed_evaluations == 6
    assert report.metrics.pass_rate == 1
    assert all(record.passed for record in report.evaluations)
    assert report.metrics.p50_latency_ms == 13
    assert report.metrics.p95_latency_ms == 16
    assert all(not record.model_dump().get("raw_text") for record in report.evaluations)

    report_path = tmp_path / "reports" / "benchmark.json"
    first = report_path.read_text(encoding="utf-8")
    EvaluationHarness.write_report(report, report_path)
    assert report_path.read_text(encoding="utf-8") == first
    assert "unterminated" not in first
    assert "synthetic-model:v1" in first


def test_two_queued_evaluations_serialize_model_slots(tmp_path: Path) -> None:
    fixtures = load_fixtures(FIXTURE_DIR)[:2]
    fixtures = [
        fixture.model_copy(update={"prompt": f"fixture_id={fixture.fixture_id}"})
        for fixture in fixtures
    ]
    gateway = FixtureGateway({fixture.fixture_id: fixture.expected_output for fixture in fixtures})

    async def run() -> object:
        return await EvaluationHarness(gateway).run_concurrent(fixtures, tmp_path / "queued.json")

    report = asyncio.run(run())
    assert report.metrics.valid_evaluations == 2
    assert gateway.invocations == 2
    assert gateway.peak_active == 2
