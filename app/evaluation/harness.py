"""Async, offline benchmark runner for the frozen LLM gateway contract."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Awaitable, Sequence
from math import ceil
from pathlib import Path
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from app.llm.contracts import InvocationResult, InvocationStatus, ModelCallTelemetry

from .fixtures import EvaluationFixture


class StructuredGateway(Protocol):
    """Narrow dependency boundary; the production gateway can be async or sync."""

    def invoke_structured(
        self, *, prompt: str, response_model: type[BaseModel]
    ) -> InvocationResult[BaseModel] | Awaitable[InvocationResult[BaseModel]]: ...


class EvaluationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fixture_id: str
    domain: str
    expected_status: InvocationStatus
    status: InvocationStatus
    valid: bool
    assertions_passed: bool
    assertion_failures: list[str]
    passed: bool
    latency_ms: float = Field(ge=0)
    telemetry: list[ModelCallTelemetry]
    error_code: str | None = None
    error_diagnostic: str | None = None


class BenchmarkMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_evaluations: int = Field(ge=0)
    valid_evaluations: int = Field(ge=0)
    validity_rate: float = Field(ge=0, le=1)
    passed_evaluations: int = Field(ge=0)
    pass_rate: float = Field(ge=0, le=1)
    p50_latency_ms: float = Field(ge=0)
    p95_latency_ms: float = Field(ge=0)


class BenchmarkReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    benchmark_version: str
    model_identities: list[str]
    config_versions: list[str]
    evaluations: list[EvaluationRecord]
    metrics: BenchmarkMetrics


def _percentile(values: Sequence[float], percentile: float) -> float:
    """Return a deterministic nearest-rank percentile (including p50/p95)."""

    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, ceil(percentile * len(ordered)) - 1)]


class EvaluationHarness:
    """Run versioned fixtures through an injected gateway with bounded concurrency."""

    def __init__(self, gateway: StructuredGateway, *, benchmark_version: str = "phase1-v1") -> None:
        self._gateway = gateway
        self._benchmark_version = benchmark_version

    async def run(
        self,
        fixtures: Sequence[EvaluationFixture],
        report_path: Path,
        *,
        max_concurrency: int = 1,
    ) -> BenchmarkReport:
        """Evaluate all fixtures and write a stable JSON report to ``report_path``."""

        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least one")
        semaphore = asyncio.Semaphore(max_concurrency)

        async def evaluate(fixture: EvaluationFixture) -> EvaluationRecord:
            async with semaphore:
                return await self._evaluate_one(fixture)

        records = list(await asyncio.gather(*(evaluate(fixture) for fixture in fixtures)))
        latencies = [record.latency_ms for record in records]
        valid_count = sum(record.valid for record in records)
        passed_count = sum(record.passed for record in records)
        telemetry = [item for record in records for item in record.telemetry]
        report = BenchmarkReport(
            benchmark_version=self._benchmark_version,
            model_identities=sorted({item.model_identity for item in telemetry}),
            config_versions=sorted({item.config_version for item in telemetry}),
            evaluations=records,
            metrics=BenchmarkMetrics(
                total_evaluations=len(records),
                valid_evaluations=valid_count,
                validity_rate=(valid_count / len(records)) if records else 0.0,
                passed_evaluations=passed_count,
                pass_rate=(passed_count / len(records)) if records else 0.0,
                p50_latency_ms=_percentile(latencies, 0.50),
                p95_latency_ms=_percentile(latencies, 0.95),
            ),
        )
        self.write_report(report, report_path)
        return report

    async def run_concurrent(
        self,
        fixtures: Sequence[EvaluationFixture],
        report_path: Path,
    ) -> BenchmarkReport:
        """Queue concurrent evaluations behind one model slot for acceptance tests."""

        return await self.run(fixtures, report_path, max_concurrency=2)

    @staticmethod
    def write_report(report: BenchmarkReport, report_path: Path) -> None:
        """Write sorted, indented JSON without timestamps or raw model output."""

        report_path.parent.mkdir(parents=True, exist_ok=True)
        payload = report.model_dump(mode="json")
        report_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )

    async def _evaluate_one(self, fixture: EvaluationFixture) -> EvaluationRecord:
        started = time.perf_counter()
        telemetry: list[ModelCallTelemetry] = []
        assertions_passed = False
        assertion_failures: list[str] = []
        try:
            response = self._gateway.invoke_structured(
                prompt=fixture.prompt,
                response_model=fixture.model_type,
            )
            if inspect.isawaitable(response):
                result = await response
            else:
                result = response
            telemetry = list(result.telemetry)
            valid = result.status is InvocationStatus.VALID and self._validate_output_schema(
                result, fixture
            )
            if valid:
                assertion_failures = self._assertion_failures(result, fixture)
                assertions_passed = not assertion_failures
            status = result.status
            error_code = result.error_code
            error_diagnostic = result.error_diagnostic
        except Exception:
            result = None
            valid = False
            status = InvocationStatus.FAILED
            error_code = "gateway_exception"
            error_diagnostic = "Gateway evaluation raised an exception."
        measured_latency = (time.perf_counter() - started) * 1_000
        latency = sum(item.latency_ms for item in telemetry) if telemetry else measured_latency
        expected_status = InvocationStatus(fixture.expected_status)
        passed = status is expected_status and (
            assertions_passed
            if expected_status is InvocationStatus.VALID
            else error_code is not None
        )
        if not passed and error_code is None:
            error_code = (
                "evaluation_assertion_failed"
                if valid and not assertions_passed
                else "evaluation_status_mismatch"
            )
        return EvaluationRecord(
            fixture_id=fixture.fixture_id,
            domain=fixture.domain,
            expected_status=expected_status,
            status=status,
            valid=valid,
            assertions_passed=assertions_passed,
            assertion_failures=assertion_failures,
            passed=passed,
            latency_ms=latency,
            telemetry=telemetry,
            error_code=error_code,
            error_diagnostic=error_diagnostic,
        )

    @staticmethod
    def _validate_output_schema(
        result: InvocationResult[BaseModel], fixture: EvaluationFixture
    ) -> bool:
        if result.output is None:
            return False
        try:
            fixture.model_type.model_validate(result.output)
        except (TypeError, ValueError):
            return False
        return True

    @staticmethod
    def _assertion_failures(
        result: InvocationResult[BaseModel], fixture: EvaluationFixture
    ) -> list[str]:
        if result.output is None:
            return ["output"]
        output = fixture.model_type.model_validate(result.output)
        payload = output.model_dump(mode="json")
        failures: list[str] = []
        for path, expected in fixture.assertions.equals.items():
            if _value_at_path(payload, path) != expected:
                failures.append(path)
        for path, terms in fixture.assertions.contains.items():
            value = _value_at_path(payload, path)
            if not isinstance(value, str):
                failures.append(path)
                continue
            lowered = value.casefold()
            if any(term.casefold() not in lowered for term in terms):
                failures.append(path)
        return sorted(set(failures))


def _value_at_path(payload: object, path: str) -> object:
    """Resolve a dotted dictionary/list path used by checked-in assertions."""

    current = payload
    for part in path.split("."):
        if isinstance(current, dict):
            current_mapping = cast(dict[str, object], current)
            if part not in current_mapping:
                return None
            current = current_mapping[part]
        elif isinstance(current, list) and part.isdigit():
            current_list = cast(list[object], current)
            index = int(part)
            if index >= len(current_list):
                return None
            current = current_list[index]
        else:
            return None
    return current
