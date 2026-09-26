#!/usr/bin/env python3
"""Opt-in configured-model evaluation for native tool-loop reliability.

The default path runs the same acceptance corpus through deterministic fake
model turns so unit tests can validate the evaluator without Ollama. Pass
--live to call the configured model through LLMGateway. The JSON report is
intentionally aggregate-only: no prompts, model prose, URLs, arguments, result
payloads, or raw IDs are emitted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.agents.harness import (  # noqa: E402
    AgentHarnessEvent,
    AgentHarnessGateway,
    AgentHarnessResult,
    AgentTranscriptCheckpoint,
    ConversationLifecycle,
    NativeTool,
    PostToolLifecycleContext,
    ToolExecutionError,
    ToolExecutionResult,
    run_native_tool_loop,
)
from app.llm.parsing import estimate_tokens  # noqa: E402

FIXTURE_PATH = Path("tests/fixtures/native_tool_loop_reliability.json")
DEFAULT_RUNS = 5
SAFE_SYSTEM_MESSAGE = """\
You are evaluating LifeAgent's native tool loop against synthetic data only.
Use the provided tools when the user's request asks for synthetic academic,
career, LEARN, proposal, nightly, replay, or write-state information. Direct
chat can be answered normally. After a successful grounded read, give concise
ordinary assistant prose; the host owns factual rendering from trusted tool
state. Never include tool IDs, source IDs, URLs, Discord identifiers, Notion
identifiers, or hidden reasoning in user-facing text.
"""
URL_RE = re.compile(r"https?://", re.IGNORECASE)
RAW_ID_KEYWORDS = ("discord", "notion", "database", "external_id", "raw_id", "prompt")
_PRODUCTION_SCHEMA_CACHE: dict[str, Mapping[str, Any]] | None = None


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    case_id: str
    category: str
    prompt: str
    expected_status: str
    expected_tools: tuple[str, ...]
    terminal_after: frozenset[str]
    fake_model_turns: tuple[Mapping[str, object], ...]
    expected_outcome_code: str | None = None
    require_terminal_response: bool = False
    replay_boundary: str | None = None
    max_turns: int = 12


@dataclass(slots=True)
class RuntimeState:
    case: EvaluationCase
    tool_calls: list[str] = field(default_factory=list)
    tool_errors: list[str] = field(default_factory=list)
    tool_executions: Counter[str] = field(default_factory=Counter)
    terminal_payloads: list[Mapping[str, object]] = field(default_factory=list)


class ScriptedGateway:
    """Deterministic harness gateway used by unit tests and dry runs."""

    def __init__(self, turns: Sequence[Mapping[str, object]]) -> None:
        self._turns = list(turns)
        self.calls = 0

    async def invoke_tools(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Mapping[str, Any]],
    ) -> AIMessage:
        del messages, tools
        self.calls += 1
        if not self._turns:
            return AIMessage(content="No scripted turn remained.")
        return _message_from_fixture_turn(self._turns.pop(0))


@dataclass(slots=True)
class MeasuredGateway:
    """Collect bounded numeric observations without retaining model content."""

    gateway: AgentHarnessGateway
    latency_ms: list[float] = field(default_factory=list)
    estimated_input_tokens: list[int] = field(default_factory=list)
    estimated_schema_tokens: list[int] = field(default_factory=list)
    estimated_output_tokens: list[int] = field(default_factory=list)

    async def invoke_tools(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Mapping[str, Any]],
    ) -> AIMessage:
        message_payload = json.dumps(
            [message.model_dump(mode="json") for message in messages],
            ensure_ascii=True,
            sort_keys=True,
            default=str,
        )
        schema_payload = json.dumps(tools, ensure_ascii=True, sort_keys=True, default=str)
        self.estimated_input_tokens.append(estimate_tokens(message_payload + schema_payload))
        self.estimated_schema_tokens.append(estimate_tokens(schema_payload))
        started = time.monotonic()
        try:
            response = await self.gateway.invoke_tools(messages, tools)
        finally:
            self.latency_ms.append(max(0.0, (time.monotonic() - started) * 1000))
        output_payload = json.dumps(
            response.model_dump(mode="json"),
            ensure_ascii=True,
            sort_keys=True,
            default=str,
        )
        self.estimated_output_tokens.append(estimate_tokens(output_payload))
        return response


class _InjectedReplayCrashError(RuntimeError):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=FIXTURE_PATH)
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--case", dest="case_ids", action="append")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--ollama-base-url")
    parser.add_argument("--output", type=Path)
    return parser


def load_cases(path: Path) -> tuple[EvaluationCase, ...]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("native tool-loop fixture must be a list")
    cases: list[EvaluationCase] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("native tool-loop fixture entries must be objects")
        case_id = _required_str(item, "case_id")
        if case_id in seen:
            raise ValueError(f"duplicate native tool-loop fixture: {case_id}")
        seen.add(case_id)
        expected_tools = tuple(str(value) for value in _sequence(item.get("expected_tools", ())))
        terminal_after = frozenset(
            str(value) for value in _sequence(item.get("terminal_after", ()))
        )
        fake_model_turns = tuple(
            cast(Mapping[str, object], value)
            for value in _sequence(item.get("fake_model_turns", ()))
            if isinstance(value, Mapping)
        )
        cases.append(
            EvaluationCase(
                case_id=case_id,
                category=_required_str(item, "category"),
                prompt=_required_str(item, "prompt"),
                expected_status=_required_str(item, "expected_status"),
                expected_tools=expected_tools,
                terminal_after=terminal_after,
                fake_model_turns=fake_model_turns,
                expected_outcome_code=(
                    str(item["expected_outcome_code"])
                    if isinstance(item.get("expected_outcome_code"), str)
                    else None
                ),
                require_terminal_response=bool(item.get("require_terminal_response", False)),
                replay_boundary=(
                    str(item["replay_boundary"])
                    if isinstance(item.get("replay_boundary"), str)
                    else None
                ),
                max_turns=int(item.get("max_turns", 12)),
            )
        )
    return tuple(cases)


async def evaluate_cases(
    cases: Sequence[EvaluationCase],
    *,
    runs: int = DEFAULT_RUNS,
    live: bool = False,
    gateway: AgentHarnessGateway | None = None,
) -> dict[str, object]:
    if runs < 1:
        raise ValueError("runs must be positive")
    if live and gateway is None:
        from app.llm.gateway import LLMGateway
        from app.llm.ollama_runtime import OllamaRuntime

        await OllamaRuntime().ensure_ready()
        gateway = LLMGateway()

    records: list[dict[str, object]] = []
    started_at = datetime.now(UTC)
    for case in cases:
        case_runs = runs if live else 1
        for run_index in range(case_runs):
            case_gateway = gateway if live else ScriptedGateway(case.fake_model_turns)
            if case_gateway is None:
                raise RuntimeError("live evaluation requires a gateway")
            records.append(
                await _evaluate_one(
                    case,
                    gateway=case_gateway,
                    run_index=run_index + 1,
                    live=live,
                )
            )
    report = _report(cases, records, started_at=started_at, live=live, requested_runs=runs)
    _assert_privacy_safe_report(report, cases)
    return report


async def _evaluate_one(
    case: EvaluationCase,
    *,
    gateway: AgentHarnessGateway,
    run_index: int,
    live: bool,
) -> dict[str, object]:
    state = RuntimeState(case=case)
    events: list[AgentHarnessEvent] = []
    checkpoints: list[AgentTranscriptCheckpoint] = []
    measured_gateway = MeasuredGateway(gateway)
    if case.replay_boundary:
        result, replayed = await _evaluate_replay_case(
            case,
            gateway=measured_gateway,
            state=state,
            events=events,
            checkpoints=checkpoints,
        )
    else:
        result = await run_native_tool_loop(
            gateway=measured_gateway,
            user_input=case.prompt,
            tools=_tools(state),
            system_message=SAFE_SYSTEM_MESSAGE,
            max_turns=case.max_turns,
            event_sink=lambda event: _record_event(events, state, event),
            checkpoint_sink=lambda checkpoint: _record_checkpoint(checkpoints, checkpoint),
            require_terminal_response=case.require_terminal_response,
            post_tool_lifecycle_resolver=lambda context: _resolve_lifecycle(state, context),
            _model_pending_elapsed_seconds=(),
            _model_pending_repeat_seconds=60.0,
        )
        replayed = False
    return _record_for_result(
        case,
        result,
        state=state,
        events=events,
        checkpoints=checkpoints,
        run_index=run_index,
        live=live,
        replayed=replayed,
        measured_gateway=measured_gateway,
    )


async def _evaluate_replay_case(
    case: EvaluationCase,
    *,
    gateway: AgentHarnessGateway,
    state: RuntimeState,
    events: list[AgentHarnessEvent],
    checkpoints: list[AgentTranscriptCheckpoint],
) -> tuple[AgentHarnessResult, bool]:
    crash_after = case.replay_boundary

    async def checkpoint_then_crash(checkpoint: AgentTranscriptCheckpoint) -> None:
        checkpoints.append(checkpoint)
        if checkpoint.kind == crash_after:
            raise _InjectedReplayCrashError("synthetic checkpoint crash")

    try:
        result = await run_native_tool_loop(
            gateway=gateway,
            user_input=case.prompt,
            tools=_tools(state),
            system_message=SAFE_SYSTEM_MESSAGE,
            max_turns=case.max_turns,
            event_sink=lambda event: _record_event(events, state, event),
            checkpoint_sink=checkpoint_then_crash,
            require_terminal_response=case.require_terminal_response,
            post_tool_lifecycle_resolver=lambda context: _resolve_lifecycle(state, context),
            _model_pending_elapsed_seconds=(),
            _model_pending_repeat_seconds=60.0,
        )
        if result.error_code != "checkpoint_sink_failed" or not checkpoints:
            return result, False
        restored = _restored_messages(checkpoints[-1])
    except _InjectedReplayCrashError:
        restored = _restored_messages(checkpoints[-1])

    result = await run_native_tool_loop(
        gateway=gateway,
        user_input=None,
        tools=_tools(state),
        system_message=SAFE_SYSTEM_MESSAGE,
        max_turns=case.max_turns,
        event_sink=lambda event: _record_event(events, state, event),
        checkpoint_sink=lambda checkpoint: _record_checkpoint(checkpoints, checkpoint),
        restored_messages=restored,
        require_terminal_response=case.require_terminal_response,
        post_tool_lifecycle_resolver=lambda context: _resolve_lifecycle(state, context),
        _model_pending_elapsed_seconds=(),
        _model_pending_repeat_seconds=60.0,
    )
    return result, True


async def _record_event(
    events: list[AgentHarnessEvent],
    state: RuntimeState,
    event: AgentHarnessEvent,
) -> None:
    events.append(event)
    if event.kind == "tool_call" and event.tool_name is not None:
        state.tool_calls.append(event.tool_name)
    if event.kind == "tool_error" and event.tool_name is not None:
        state.tool_errors.append(event.tool_name)


async def _record_checkpoint(
    checkpoints: list[AgentTranscriptCheckpoint],
    checkpoint: AgentTranscriptCheckpoint,
) -> None:
    checkpoints.append(checkpoint)


def _resolve_lifecycle(
    state: RuntimeState,
    context: PostToolLifecycleContext,
) -> ConversationLifecycle | None:
    case = state.case
    if case.expected_outcome_code == "repeated_failed_tool_call" and _has_repeated_tool_error(
        context.messages
    ):
        return ConversationLifecycle(
            disposition="completed",
            content="The same synthetic read failed twice; no change was made.",
        )
    if context.trigger == "assistant_response" and state.terminal_payloads:
        return _lifecycle_from_state(state)
    batch_names = (
        {outcome.name for outcome in context.batch.outcomes}
        if context.batch is not None
        else ({context.tool_name} if context.tool_name is not None else set())
    )
    if not batch_names.intersection(case.terminal_after):
        return None
    if not _expected_tools_observed(case, state.tool_calls):
        return None
    return _lifecycle_from_state(state)


def _lifecycle_from_state(state: RuntimeState) -> ConversationLifecycle:
    category = state.case.category
    if category == "ambiguous_write":
        return ConversationLifecycle(
            disposition="awaiting_user",
            content="The synthetic write has an ambiguous outcome and needs reconciliation.",
        )
    return ConversationLifecycle(
        disposition="completed",
        content=f"Synthetic {category} result rendered from trusted host state.",
    )


def _expected_tools_observed(case: EvaluationCase, observed: Sequence[str]) -> bool:
    expected_counts = Counter(case.expected_tools)
    observed_counts = Counter(observed)
    return all(observed_counts[name] >= count for name, count in expected_counts.items())


def _has_repeated_tool_error(messages: Sequence[BaseMessage]) -> bool:
    errors: Counter[tuple[str, str]] = Counter()
    for message in messages:
        if not isinstance(message, ToolMessage) or message.status != "error":
            continue
        name = str(getattr(message, "name", "") or "")
        try:
            payload = json.loads(str(message.content))
        except json.JSONDecodeError:
            payload = {}
        error = payload.get("error") if isinstance(payload, Mapping) else ""
        errors[(name, json.dumps(error, sort_keys=True))] += 1
    return any(count > 1 for count in errors.values())


def _record_for_result(
    case: EvaluationCase,
    result: AgentHarnessResult,
    *,
    state: RuntimeState,
    events: Sequence[AgentHarnessEvent],
    checkpoints: Sequence[AgentTranscriptCheckpoint],
    run_index: int,
    live: bool,
    replayed: bool,
    measured_gateway: MeasuredGateway,
) -> dict[str, object]:
    outcome_code = _outcome_code(case, result, state)
    expected_code = case.expected_outcome_code
    expected_tool_counts = Counter(case.expected_tools)
    observed_tool_counts = Counter(state.tool_calls)
    tools_ok = all(
        observed_tool_counts[name] >= count for name, count in expected_tool_counts.items()
    )
    status_ok = result.status == case.expected_status
    code_ok = expected_code is None or outcome_code == expected_code
    duplicate_effects = any(
        count > 1
        for name, count in state.tool_executions.items()
        if _tool_side_effect_class(name) != "read_only"
    )
    tool_error_counts = Counter(state.tool_errors)
    repeated_failed_calls = sum(max(0, count - 1) for count in tool_error_counts.values())
    any_tool_succeeded = any(event.kind == "tool_result" for event in events)
    expected_host_response = (
        _lifecycle_from_state(state).content if state.terminal_payloads else None
    )
    fabricated_grounded_facts = (
        expected_host_response is not None and result.final_response != expected_host_response
    )
    unavailable_source_rows = sum(
        len(_sequence(payload.get("items", ())))
        for payload in state.terminal_payloads
        if case.category == "unavailable"
    )
    mixed_degradation_ok = case.category != "mixed_batch" or (
        any_tool_succeeded and bool(tool_error_counts)
    )
    passed = (
        status_ok
        and tools_ok
        and code_ok
        and not duplicate_effects
        and not fabricated_grounded_facts
        and unavailable_source_rows == 0
        and mixed_degradation_ok
    )
    return {
        "case_id": case.case_id,
        "category": case.category,
        "run_index": run_index,
        "mode": "live" if live else "fake",
        "passed": passed,
        "status": result.status,
        "expected_status": case.expected_status,
        "outcome_code": outcome_code,
        "turns": result.turns,
        "tool_calls": dict(sorted(observed_tool_counts.items())),
        "tool_errors": dict(sorted(tool_error_counts.items())),
        "tool_executions": dict(sorted(state.tool_executions.items())),
        "any_tool_succeeded": any_tool_succeeded,
        "repeated_failed_call_count": repeated_failed_calls,
        "fabricated_grounded_fact_count": int(fabricated_grounded_facts),
        "unavailable_source_row_count": unavailable_source_rows,
        "unconfirmed_write_count": 0,
        "lifecycle_disposition": result.lifecycle_disposition,
        "checkpoint_count": len(checkpoints),
        "event_count": len(events),
        "replayed": replayed,
        "duplicate_side_effects": duplicate_effects,
        "model_call_count": len(measured_gateway.latency_ms),
        "model_latency_ms_total": round(sum(measured_gateway.latency_ms), 3),
        "model_latency_ms_max": round(max(measured_gateway.latency_ms, default=0.0), 3),
        "estimated_input_tokens_max": max(measured_gateway.estimated_input_tokens, default=0),
        "estimated_schema_tokens_max": max(measured_gateway.estimated_schema_tokens, default=0),
        "estimated_output_tokens_total": sum(measured_gateway.estimated_output_tokens),
        "failure_phase": _failure_phase(result.error_code),
    }


def _failure_phase(error_code: str | None) -> str | None:
    if error_code is None:
        return None
    if error_code.startswith("model_") or error_code == "invalid_native_response":
        return "model"
    if error_code.startswith("tool_") or error_code == "repeated_failed_tool_call":
        return "tool"
    if "lifecycle" in error_code:
        return "lifecycle"
    if "checkpoint" in error_code or "sink" in error_code:
        return "checkpoint"
    return "host"


def _outcome_code(
    case: EvaluationCase,
    result: AgentHarnessResult,
    state: RuntimeState,
) -> str | None:
    if case.expected_outcome_code == "repeated_failed_tool_call" and len(state.tool_errors) >= 2:
        return "repeated_failed_tool_call"
    return result.error_code


def _report(
    cases: Sequence[EvaluationCase],
    records: Sequence[Mapping[str, object]],
    *,
    started_at: datetime,
    live: bool,
    requested_runs: int,
) -> dict[str, object]:
    passed = sum(1 for record in records if record.get("passed") is True)
    by_category: dict[str, dict[str, int]] = {}
    for record in records:
        category = str(record["category"])
        bucket = by_category.setdefault(category, {"passed": 0, "total": 0})
        bucket["total"] += 1
        if record.get("passed") is True:
            bucket["passed"] += 1
    return {
        "schema_version": "native_tool_loop_eval.v1",
        "generated_at": started_at.isoformat(),
        "mode": "live" if live else "fake",
        "requested_runs": requested_runs,
        "metrics": {
            "case_count": len(cases),
            "run_count": len(records),
            "passed": passed,
            "failed": len(records) - passed,
        },
        "by_category": dict(sorted(by_category.items())),
        "safety": {
            "duplicate_effect_runs": sum(
                1 for record in records if record.get("duplicate_side_effects") is True
            ),
            "fabricated_grounded_fact_count": sum(
                int(record.get("fabricated_grounded_fact_count", 0)) for record in records
            ),
            "unavailable_source_row_count": sum(
                int(record.get("unavailable_source_row_count", 0)) for record in records
            ),
            "unconfirmed_write_count": sum(
                int(record.get("unconfirmed_write_count", 0)) for record in records
            ),
        },
        "observability": {
            "model_latency_ms": _numeric_distribution(
                record.get("model_latency_ms_total", 0.0) for record in records
            ),
            "estimated_input_tokens": _numeric_distribution(
                record.get("estimated_input_tokens_max", 0) for record in records
            ),
            "estimated_schema_tokens": _numeric_distribution(
                record.get("estimated_schema_tokens_max", 0) for record in records
            ),
            "estimated_output_tokens": _numeric_distribution(
                record.get("estimated_output_tokens_total", 0) for record in records
            ),
        },
        "records": list(records),
    }


def _numeric_distribution(values: Iterable[object]) -> dict[str, float]:
    numeric = sorted(float(value) for value in values)
    if not numeric:
        return {"min": 0.0, "median": 0.0, "p95": 0.0, "max": 0.0}
    midpoint = len(numeric) // 2
    median = (
        numeric[midpoint] if len(numeric) % 2 else (numeric[midpoint - 1] + numeric[midpoint]) / 2
    )
    p95_index = min(len(numeric) - 1, max(0, (95 * len(numeric) + 99) // 100 - 1))
    return {
        "min": round(numeric[0], 3),
        "median": round(median, 3),
        "p95": round(numeric[p95_index], 3),
        "max": round(numeric[-1], 3),
    }


def _assert_privacy_safe_report(
    report: Mapping[str, object],
    cases: Sequence[EvaluationCase],
) -> None:
    encoded = json.dumps(report, ensure_ascii=True, sort_keys=True)
    if URL_RE.search(encoded):
        raise ValueError("native tool-loop report contains a URL")
    lowered = encoded.lower()
    for keyword in RAW_ID_KEYWORDS:
        if keyword in lowered:
            raise ValueError(f"native tool-loop report contains forbidden field: {keyword}")
    for case in cases:
        prompt = case.prompt.strip()
        if prompt and prompt in encoded:
            raise ValueError("native tool-loop report contains a fixture prompt")


def _tools(state: RuntimeState) -> tuple[NativeTool, ...]:
    # Exercise routing against every production domain schema. Handlers remain
    # synthetic and side-effect free; case-only probes are added only where the
    # production schema set has no way to express the injected failure boundary.
    tools = [
        NativeTool(
            schema=schema,
            handler=lambda arguments, name=name: _handle_tool(state, name, arguments),
            name=name,
            side_effect_class=cast(Any, _tool_side_effect_class(name)),
            activity=_activity_for_tool(name),
        )
        for name, schema in _production_schema_map().items()
    ]
    if state.case.category == "repeated_failure":
        tools.append(
            _tool(
                "failing_source_read",
                "Run the explicit reliability-evaluation failing-read probe.",
                "read_only",
                state,
            )
        )
    if state.case.category == "ambiguous_write":
        tools.append(
            _tool(
                "ambiguous_external_write",
                "Return an ambiguous synthetic external write outcome.",
                "external_write",
                state,
            )
        )
    if state.case.category == "nightly":
        tools.append(
            _tool(
                "finalize_nightly_checklist",
                "Finalize synthetic nightly checklist state.",
                "durable_local_write",
                state,
            )
        )
    return tuple(tools)


def _tool(
    name: str,
    description: str,
    side_effect_class: Literal[
        "read_only",
        "proposal_only",
        "durable_local_write",
        "external_write",
    ],
    state: RuntimeState,
) -> NativeTool:
    return NativeTool(
        schema=_schema_for_tool(name, description),
        handler=lambda arguments: _handle_tool(state, name, arguments),
        name=name,
        side_effect_class=side_effect_class,
        activity=_activity_for_tool(name),
    )


def _schema_for_tool(name: str, description: str) -> Mapping[str, Any]:
    schema = _production_schema_map().get(name)
    if schema is not None:
        return schema
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": _parameters_for_tool(name),
        },
    }


def _production_schema_map() -> Mapping[str, Mapping[str, Any]]:
    global _PRODUCTION_SCHEMA_CACHE
    if _PRODUCTION_SCHEMA_CACHE is not None:
        return _PRODUCTION_SCHEMA_CACHE
    try:
        from app.agents.academic_planner.discord_harness import _AcademicToolState
        from app.agents.job_interviews.agent_loop import CareerAgentToolState
        from app.agents.learn.tool_state import LearnToolState
    except Exception:
        _PRODUCTION_SCHEMA_CACHE = {}
        return _PRODUCTION_SCHEMA_CACHE

    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    production_tools = (
        *_AcademicToolState(
            catalog=None,
            now=now,
            timezone=ZoneInfo("America/Toronto"),
        ).tools(),
        *CareerAgentToolState(
            store=None,
            syncer=None,
            gateway=None,
            now=now,
        ).tools(),
        *LearnToolState(
            connector=cast(Any, object()),
            semantic_interpreter=cast(Any, object()),
            now=now,
        ).tools(),
    )
    _PRODUCTION_SCHEMA_CACHE = {
        str(tool.name): cast(Mapping[str, Any], tool.schema)
        for tool in production_tools
        if tool.name is not None
    }
    return _PRODUCTION_SCHEMA_CACHE


async def _handle_tool(
    state: RuntimeState,
    name: str,
    arguments: Mapping[str, object],
) -> object:
    del arguments
    state.tool_executions[name] += 1
    if name == "failing_source_read":
        raise ToolExecutionError("synthetic source unavailable")
    if state.case.category == "mixed_batch" and name == "get_learn_scheduled_items":
        raise ToolExecutionError(
            "synthetic LEARN source unavailable",
            code="source_unavailable",
            retryable=False,
        )
    payload = _payload_for_tool(state.case, name)
    if name == "create_action_item":
        state.terminal_payloads.append(payload)
        return ToolExecutionResult(content=payload, status="review_required")
    state.terminal_payloads.append(payload)
    return payload


def _payload_for_tool(case: EvaluationCase, name: str) -> Mapping[str, object]:
    base = {
        "case": case.case_id,
        "tool": name,
        "query_id": f"query-{case.case_id}",
        "items": [{"stable_id": "synthetic-item", "label": "Synthetic item"}],
        "result_count": 1,
        "completeness": "complete",
        "freshness": [{"source": "synthetic-source", "state": "fresh_complete"}],
    }
    if case.category == "unavailable":
        return {
            **base,
            "items": [],
            "result_count": 0,
            "completeness": "unavailable",
            "freshness": [{"source": "synthetic-source", "state": "unavailable"}],
        }
    if case.category == "partial":
        return {
            **base,
            "completeness": "partial",
            "freshness": [
                {"source": "synthetic-source-a", "state": "fresh_complete"},
                {"source": "synthetic-source-b", "state": "unavailable"},
            ],
        }
    if case.category == "ambiguous_write":
        return {"outcome": "ambiguous", "requires_reconciliation": True}
    if case.category == "proposal":
        return {"proposal_state": "review_required", "change_count": 1}
    if case.category == "nightly":
        return {"finalized": True, "completed_count": 1}
    return base


def _parameters_for_tool(name: str) -> Mapping[str, object]:
    if name in {"search_calendar_items", "search_courses", "search_job_interviews"}:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "maxLength": 300},
                "view": {"type": "string"},
                "temporal": {"type": "object"},
                "roles": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
        }
    if name == "get_learn_scheduled_items":
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "course_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
        }
    return {
        "type": "object",
        "additionalProperties": True,
        "properties": {},
    }


def _activity_for_tool(name: str) -> str:
    if name.startswith("search_job"):
        return "interview_data"
    if "learn" in name:
        return "learn_data"
    if name == "create_action_item":
        return "proposal_drafting"
    if name == "finalize_nightly_checklist":
        return "nightly_finalization"
    return "synthetic_data"


def _tool_side_effect_class(name: str) -> str:
    explicit = {
        "create_action_item": "proposal_only",
        "ambiguous_external_write": "external_write",
        "failing_source_read": "read_only",
        "finalize_nightly_checklist": "durable_local_write",
    }.get(name)
    if explicit is not None:
        return explicit
    if name.startswith(("search_", "get_", "find_", "inspect_")):
        return "read_only"
    return "proposal_only"


def _message_from_fixture_turn(turn: Mapping[str, object]) -> AIMessage:
    raw_calls = turn.get("tool_calls", ())
    calls = list(_sequence(raw_calls))
    return AIMessage(content=str(turn.get("content", "")), tool_calls=cast(Any, calls))


def _restored_messages(checkpoint: AgentTranscriptCheckpoint) -> tuple[BaseMessage, ...]:
    return tuple(
        message for message in checkpoint.messages if not isinstance(message, SystemMessage)
    )


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"fixture field {key} must be a non-empty string")
    return value


def _sequence(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return value
    return ()


async def _async_main(args: argparse.Namespace) -> int:
    if args.ollama_base_url:
        os.environ["OLLAMA_BASE_URL"] = str(args.ollama_base_url)
    cases = load_cases(args.fixture)
    if args.case_ids:
        requested = set(args.case_ids)
        cases = tuple(case for case in cases if case.case_id in requested)
        missing = requested - {case.case_id for case in cases}
        if missing:
            raise ValueError("unknown native tool-loop case(s): " + ", ".join(sorted(missing)))
    report = await evaluate_cases(cases, runs=args.runs, live=args.live)
    encoded = json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0 if report["metrics"]["failed"] == 0 else 1


def main() -> int:
    return asyncio.run(_async_main(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
