from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.evaluate_native_tool_loop import (
    DEFAULT_RUNS,
    RuntimeState,
    _parser,
    _production_schema_map,
    _tools,
    evaluate_cases,
    load_cases,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "native_tool_loop_reliability.json"


def test_native_tool_loop_fixture_covers_phase8_acceptance_cases() -> None:
    cases = load_cases(FIXTURE)

    assert {case.case_id for case in cases} == {
        "direct_text",
        "read_then_prose",
        "preparatory_then_terminal_read",
        "source_unavailable",
        "partial_result",
        "identical_failed_loop",
        "mixed_batch",
        "proposal_review",
        "ambiguous_write_result",
        "replay_boundary",
        "nightly_finalization",
        "career_grounded_read",
        "learn_grounded_read",
    }
    assert _parser().parse_args([]).runs == DEFAULT_RUNS
    assert DEFAULT_RUNS >= 5


def test_live_evaluator_binds_every_production_domain_schema() -> None:
    case = next(case for case in load_cases(FIXTURE) if case.case_id == "read_then_prose")

    bound_names = {tool.name for tool in _tools(RuntimeState(case=case))}

    assert set(_production_schema_map()).issubset(bound_names)
    assert len(_production_schema_map()) >= 20


@pytest.mark.asyncio
async def test_fake_gateway_evaluation_passes_and_report_is_privacy_safe() -> None:
    cases = load_cases(FIXTURE)

    report = await evaluate_cases(cases, runs=5, live=False)

    assert report["metrics"] == {
        "case_count": len(cases),
        "run_count": len(cases),
        "passed": len(cases),
        "failed": 0,
    }
    encoded = json.dumps(report, sort_keys=True)
    assert "Reply with a short synthetic acknowledgement" not in encoded
    assert "Synthetic acknowledgement complete" not in encoded
    assert "http://" not in encoded
    assert "https://" not in encoded
    assert "prompt" not in encoded.lower()
    assert set(report["observability"]) == {
        "model_latency_ms",
        "estimated_input_tokens",
        "estimated_schema_tokens",
        "estimated_output_tokens",
    }
    assert all(record["model_call_count"] >= 1 for record in report["records"])
    assert report["safety"] == {
        "duplicate_effect_runs": 0,
        "fabricated_grounded_fact_count": 0,
        "unavailable_source_row_count": 0,
        "unconfirmed_write_count": 0,
    }
    mixed = next(record for record in report["records"] if record["case_id"] == "mixed_batch")
    assert mixed["any_tool_succeeded"] is True
    assert mixed["tool_errors"] == {"get_learn_scheduled_items": 1}


@pytest.mark.asyncio
async def test_replay_boundary_resumes_without_duplicate_tool_execution() -> None:
    replay_case = next(case for case in load_cases(FIXTURE) if case.case_id == "replay_boundary")

    report = await evaluate_cases((replay_case,), runs=1, live=False)

    record = report["records"][0]
    assert record["passed"] is True
    assert record["replayed"] is True
    assert record["tool_executions"] == {"search_calendar_items": 1}
    assert record["duplicate_side_effects"] is False


@pytest.mark.asyncio
async def test_repeated_failed_loop_reports_stable_outcome_code() -> None:
    failed_case = next(
        case for case in load_cases(FIXTURE) if case.case_id == "identical_failed_loop"
    )

    report = await evaluate_cases((failed_case,), runs=1, live=False)

    record = report["records"][0]
    assert record["passed"] is True
    assert record["outcome_code"] == "repeated_failed_tool_call"
    assert record["tool_errors"] == {"failing_source_read": 2}
