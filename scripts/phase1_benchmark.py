"""Run the gated Phase 1 benchmark against host-native Ollama."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import cast

import httpx

from app.core.config import Settings
from app.evaluation import EvaluationHarness, StructuredSmokeOutput, load_fixtures
from app.llm import InvocationResult, InvocationStatus, LLMGateway, ModelCallTelemetry

GIB = 1024**3
LATENCY_P95_LIMIT_MS = 300_000
MODEL_ALLOCATION_LIMIT_BYTES = 28 * GIB
MINIMUM_FREE_MEMORY_PERCENT = 10.0
STEADY_SWAPOUT_LIMIT_BYTES = GIB
ACCEPTED_MODEL = "qwen3:14b"
ACCEPTED_NUM_CTX = 32_768
ACCEPTED_MAX_INPUT_TOKENS = 26_624
ACCEPTED_MAX_OUTPUT_TOKENS = 2_048
ACCEPTED_CONTEXT_RESERVE_TOKENS = 4_096
ACCEPTED_COMPACTION_TRIGGER_TOKENS = 19_968
ACCEPTED_COMPACTION_TARGET_TOKENS = 14_336
ACCEPTED_RECENT_TAIL_MAX_TOKENS = 8_192


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _fixture_manifest_sha256(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(directory.glob("*.json")):
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _host_memory_snapshot() -> dict[str, int | float | None]:
    """Read numeric macOS pressure/swap counters without retaining process data."""

    pressure = subprocess.run(
        ["/usr/bin/memory_pressure", "-Q"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout
    vm_stat = subprocess.run(
        ["/usr/bin/vm_stat"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout
    total_match = re.search(r"system has (\d+)", pressure)
    free_match = re.search(r"memory free percentage: (\d+)%", pressure)
    swapout_match = re.search(r"Swapouts:\s+(\d+)\.", vm_stat)
    page_size_match = re.search(r"page size of (\d+) bytes", vm_stat)
    return {
        "total_bytes": int(total_match.group(1)) if total_match else None,
        "free_percent": float(free_match.group(1)) if free_match else None,
        "swapouts": int(swapout_match.group(1)) if swapout_match else None,
        "page_size_bytes": int(page_size_match.group(1)) if page_size_match else None,
    }


async def _ollama_json(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    response = await client.request(method, path, json=payload)
    response.raise_for_status()
    value: object = response.json()
    if not isinstance(value, dict):
        raise RuntimeError(f"Ollama {path} returned a non-object response")
    return cast(dict[str, object], value)


async def _sample_memory(
    client: httpx.AsyncClient,
    stop: asyncio.Event,
    samples: list[dict[str, object]],
) -> None:
    while not stop.is_set():
        try:
            payload = await _ollama_json(client, "GET", "/api/ps")
            host = await asyncio.to_thread(_host_memory_snapshot)
            models = payload.get("models")
            if not isinstance(models, list):
                models = []
            samples.append(
                {
                    "sampled_at": datetime.now(UTC).isoformat(),
                    "models": models,
                    "host": host,
                }
            )
        except (httpx.HTTPError, RuntimeError, subprocess.SubprocessError):
            samples.append(
                {
                    "sampled_at": datetime.now(UTC).isoformat(),
                    "error": "memory_sample_failed",
                }
            )
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=1.0)


async def _embedding_probe(
    client: httpx.AsyncClient,
    *,
    model: str,
    expected_dimensions: int,
) -> dict[str, object]:
    started_at = datetime.now(UTC)
    try:
        payload = await _ollama_json(
            client,
            "POST",
            "/api/embed",
            {
                "model": model,
                "input": "LifeAgent benchmark embedding residency probe.",
                "dimensions": expected_dimensions,
                "keep_alive": "300s",
            },
        )
        raw_embeddings = payload.get("embeddings")
        embeddings = cast(list[object], raw_embeddings) if isinstance(raw_embeddings, list) else []
        first = embeddings[0] if embeddings else None
        dimensions = len(cast(list[object], first)) if isinstance(first, list) else None
        return {
            "status": "valid" if dimensions == expected_dimensions else "invalid_dimensions",
            "model": model,
            "expected_dimensions": expected_dimensions,
            "observed_dimensions": dimensions,
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
        }
    except (httpx.HTTPError, RuntimeError, TypeError, ValueError) as exc:
        return {
            "status": "failed",
            "model": model,
            "expected_dimensions": expected_dimensions,
            "observed_dimensions": None,
            "error_code": exc.__class__.__name__,
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
        }


def _memory_summary(
    samples: list[dict[str, object]],
    before: Mapping[str, object],
    after: Mapping[str, object],
    *,
    reasoning_model: str,
) -> dict[str, object]:
    peak_size = 0
    peak_vram = 0
    peak_combined_size = 0
    peak_combined_vram = 0
    context_lengths: set[int] = set()
    reasoning_context_lengths: set[int] = set()
    free_percentages: list[float] = []
    sample_errors = 0
    for sample in samples:
        if sample.get("error"):
            sample_errors += 1
        host = sample.get("host")
        if isinstance(host, dict):
            free = cast(dict[str, object], host).get("free_percent")
            if isinstance(free, (int, float)):
                free_percentages.append(float(free))
        models = sample.get("models")
        if not isinstance(models, list):
            continue
        sample_size = 0
        sample_vram = 0
        for model in cast(list[object], models):
            if not isinstance(model, dict):
                continue
            model_record = cast(dict[str, object], model)
            name = model_record.get("name")
            size = model_record.get("size")
            size_vram = model_record.get("size_vram")
            context = model_record.get("context_length")
            if isinstance(size, int):
                peak_size = max(peak_size, size)
                sample_size += size
            if isinstance(size_vram, int):
                peak_vram = max(peak_vram, size_vram)
                sample_vram += size_vram
            if isinstance(context, int):
                context_lengths.add(context)
                if name == reasoning_model:
                    reasoning_context_lengths.add(context)
        peak_combined_size = max(peak_combined_size, sample_size)
        peak_combined_vram = max(peak_combined_vram, sample_vram)
    before_swapouts = before.get("swapouts")
    after_swapouts = after.get("swapouts")
    swapout_delta_pages = None
    swapout_delta_bytes = None
    if isinstance(before_swapouts, int) and isinstance(after_swapouts, int):
        swapout_delta_pages = max(0, after_swapouts - before_swapouts)
        page_size = after.get("page_size_bytes") or before.get("page_size_bytes")
        if isinstance(page_size, int):
            swapout_delta_bytes = swapout_delta_pages * page_size
    return {
        "sample_count": len(samples),
        "sample_errors": sample_errors,
        "peak_model_size_bytes": peak_size,
        "peak_model_vram_bytes": peak_vram,
        "peak_combined_model_size_bytes": peak_combined_size,
        "peak_combined_model_vram_bytes": peak_combined_vram,
        "observed_context_lengths": sorted(context_lengths),
        "observed_reasoning_context_lengths": sorted(reasoning_context_lengths),
        "minimum_host_free_percent": min(free_percentages) if free_percentages else None,
        "swapout_delta_pages": swapout_delta_pages,
        "swapout_delta_bytes": swapout_delta_bytes,
        "before": dict(before),
        "after": dict(after),
    }


def _benchmark_gate_passed(checks: Mapping[str, bool], advisories: Mapping[str, bool]) -> bool:
    return all(checks.values()) and all(advisories.values())


def _benchmark_context_profile(num_ctx: int, max_output_tokens: int) -> dict[str, int]:
    accepted_default_profile = (
        num_ctx == ACCEPTED_NUM_CTX and max_output_tokens == ACCEPTED_MAX_OUTPUT_TOKENS
    )
    reserve = (
        ACCEPTED_CONTEXT_RESERVE_TOKENS
        if accepted_default_profile
        else min(4096, max(0, num_ctx // 8))
    )
    max_input = num_ctx - max_output_tokens - reserve
    if max_input <= 2:
        raise RuntimeError("benchmark context window is too small for the requested output")
    return {
        "max_input_tokens": max_input,
        "context_reserve_tokens": reserve,
        "compaction_trigger_tokens": (
            ACCEPTED_COMPACTION_TRIGGER_TOKENS
            if accepted_default_profile
            else max(2, int(max_input * 0.75))
        ),
        "compaction_target_tokens": (
            ACCEPTED_COMPACTION_TARGET_TOKENS
            if accepted_default_profile
            else max(1, int(max_input * 0.5))
        ),
        "recent_tail_max_tokens": (
            ACCEPTED_RECENT_TAIL_MAX_TOKENS
            if accepted_default_profile
            else max(1, min(8192, max(1, int(max_input * 0.5))))
        ),
    }


async def _run(args: argparse.Namespace) -> dict[str, object]:
    fixture_dir = args.fixture_dir.resolve()
    fixtures = load_fixtures(fixture_dir)
    valid_fixtures = [item for item in fixtures if item.expected_status == "valid"]
    if not valid_fixtures:
        raise RuntimeError("benchmark requires at least one expected-valid fixture")

    base_url = args.base_url.rstrip("/")
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
        version = await _ollama_json(client, "GET", "/api/version")
        tags = await _ollama_json(client, "GET", "/api/tags")
        models = tags.get("models")
        if not isinstance(models, list):
            raise RuntimeError("Ollama tags response has no model list")
        model_records = [
            cast(dict[str, object], model)
            for model in cast(list[object], models)
            if isinstance(model, dict)
        ]
        matching = [model for model in model_records if model.get("name") == args.model]
        if len(matching) != 1 or matching[0].get("digest") != args.expected_digest:
            raise RuntimeError("configured model tag/digest does not match Ollama /api/tags")

        # Make the cold measurement explicit and unload only models that are
        # already resident. Asking Ollama to unload every installed tag can
        # load a dormant model before immediately evicting it.
        running = await _ollama_json(client, "GET", "/api/ps")
        resident_models = running.get("models")
        if not isinstance(resident_models, list):
            raise RuntimeError("Ollama process response has no model list")
        for model in cast(list[object], resident_models):
            if not isinstance(model, dict):
                continue
            model = cast(dict[str, object], model)
            if isinstance(model.get("name"), str):
                await _ollama_json(
                    client,
                    "POST",
                    "/api/generate",
                    {"model": model["name"], "keep_alive": 0, "stream": False},
                )
        if resident_models:
            await asyncio.sleep(5)

        context_profile = _benchmark_context_profile(args.num_ctx, args.max_output_tokens)
        settings = Settings(
            ollama_base_url=base_url,
            ollama_model=args.model,
            ollama_model_digest=args.expected_digest,
            ollama_num_ctx=args.num_ctx,
            ollama_num_batch=args.num_batch,
            ollama_max_concurrency=1,
            ollama_max_input_tokens=context_profile["max_input_tokens"],
            ollama_max_output_tokens=args.max_output_tokens,
            ollama_context_reserve_tokens=context_profile["context_reserve_tokens"],
            ollama_timeout_seconds=args.timeout_seconds,
            ollama_seed=args.seed,
            ollama_reasoning=args.reasoning,
            ollama_structured_output_transport=args.structured_output_transport,
            conversation_compaction_trigger_tokens=context_profile["compaction_trigger_tokens"],
            conversation_compaction_target_tokens=context_profile["compaction_target_tokens"],
            conversation_recent_tail_max_tokens=context_profile["recent_tail_max_tokens"],
            conversation_compaction_max_output_tokens=args.max_output_tokens,
        )
        gateway = LLMGateway(settings)
        harness = EvaluationHarness(gateway, benchmark_version="phase1-live-v1")
        cold_memory_before = _host_memory_snapshot()
        memory_samples: list[dict[str, object]] = []
        stop_sampling = asyncio.Event()
        sampler = asyncio.create_task(_sample_memory(client, stop_sampling, memory_samples))
        try:
            with tempfile.TemporaryDirectory(prefix="lifeagent-phase1-") as report_dir:
                report_root = Path(report_dir)
                cold = await harness.run(
                    [valid_fixtures[0]], report_root / "cold.json", max_concurrency=1
                )
                await asyncio.sleep(5)
                cold_memory_after = await asyncio.to_thread(_host_memory_snapshot)
                cold_memory_samples = list(memory_samples)
                memory_samples.clear()
                warm_memory_before = await asyncio.to_thread(_host_memory_snapshot)
                warm_inputs = [
                    fixture for _ in range(args.warm_repetitions) for fixture in valid_fixtures
                ]
                warm = await harness.run(warm_inputs, report_root / "warm.json", max_concurrency=1)
                embedding_probe = await _embedding_probe(
                    client,
                    model=args.embedding_model,
                    expected_dimensions=args.embedding_dimensions,
                )
                await asyncio.sleep(5)

            markers = ("CONCURRENCY-MARKER-ONE", "CONCURRENCY-MARKER-TWO")

            async def marker_call(marker: str) -> InvocationResult[StructuredSmokeOutput]:
                return await gateway.invoke_structured(
                    prompt=(
                        f"Set answer to exactly '{marker}' and confidence to exactly 1.0. "
                        "Do not repeat the other task's marker."
                    ),
                    response_model=StructuredSmokeOutput,
                )

            LLMGateway.reset_concurrency_metrics()
            marker_results = await asyncio.gather(*(marker_call(marker) for marker in markers))
        finally:
            stop_sampling.set()
            await sampler
        warm_memory_after = _host_memory_snapshot()
        final_tags = await _ollama_json(client, "GET", "/api/tags")
        final_models = final_tags.get("models")
        final_model_records = (
            [
                cast(dict[str, object], model)
                for model in cast(list[object], final_models)
                if isinstance(model, dict)
            ]
            if isinstance(final_models, list)
            else []
        )
        identity_stable = any(
            model.get("name") == args.model and model.get("digest") == args.expected_digest
            for model in final_model_records
        )

    marker_outputs: list[str | None] = [
        result.output.answer if result.output is not None else None for result in marker_results
    ]
    call_intervals: list[ModelCallTelemetry] = sorted(
        [item for result in marker_results for item in result.telemetry],
        key=lambda item: item.model_started_at,
    )
    non_overlapping = all(
        later.model_started_at >= earlier.model_finished_at
        for earlier, later in pairwise(call_intervals)
    )
    concurrency_metrics = LLMGateway.concurrency_metrics()
    concurrency: dict[str, object] = {
        "statuses": [result.status.value for result in marker_results],
        "request_ids": [str(result.request_id) for result in marker_results],
        "outputs_match_markers": marker_outputs == list(markers),
        "model_intervals_non_overlapping": non_overlapping,
        "gateway_metrics": concurrency_metrics,
    }
    cold_memory = _memory_summary(
        cold_memory_samples,
        cold_memory_before,
        cold_memory_after,
        reasoning_model=args.model,
    )
    memory = _memory_summary(
        memory_samples,
        warm_memory_before,
        warm_memory_after,
        reasoning_model=args.model,
    )
    minimum_free = memory["minimum_host_free_percent"]
    peak_combined_model_vram = max(
        cast(int, cold_memory["peak_combined_model_vram_bytes"]),
        cast(int, memory["peak_combined_model_vram_bytes"]),
    )
    observed_reasoning_context_lengths = sorted(
        set(cast(list[int], cold_memory["observed_reasoning_context_lengths"]))
        | set(cast(list[int], memory["observed_reasoning_context_lengths"]))
    )
    checks: dict[str, bool] = {
        "cold_fixture_passed": cold.metrics.pass_rate == 1,
        "warm_fixtures_passed": warm.metrics.pass_rate == 1,
        "warm_p95_within_timeout": warm.metrics.p95_latency_ms <= LATENCY_P95_LIMIT_MS,
        "model_allocation_within_limit": 0
        < peak_combined_model_vram
        <= MODEL_ALLOCATION_LIMIT_BYTES,
        "context_length_matches": observed_reasoning_context_lengths == [args.num_ctx],
        "two_tasks_completed": all(
            result.status is InvocationStatus.VALID for result in marker_results
        ),
        "single_active_model_call": concurrency_metrics == {"active": 0, "peak": 1, "limit": 1},
        "concurrent_results_not_mixed": marker_outputs == list(markers),
        "model_intervals_non_overlapping": non_overlapping,
        "model_identity_stable": identity_stable,
        "embedding_residency_probe_passed": embedding_probe["status"] == "valid",
    }
    advisories: dict[str, bool] = {
        "steady_host_headroom_observed": (
            isinstance(minimum_free, (int, float)) and minimum_free >= MINIMUM_FREE_MEMORY_PERCENT
        ),
        "steady_swapout_within_limit": (
            isinstance(memory["swapout_delta_bytes"], int)
            and memory["swapout_delta_bytes"] <= STEADY_SWAPOUT_LIMIT_BYTES
        ),
    }
    gate_passed = _benchmark_gate_passed(checks, advisories)
    return {
        "benchmark_version": "phase1-live-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": sys.version.split()[0],
        },
        "provenance": {
            "uv_lock_sha256": _sha256(Path("uv.lock")),
            "fixture_manifest_sha256": _fixture_manifest_sha256(fixture_dir),
        },
        "ollama": {
            "version": version.get("version"),
            "model": args.model,
            "digest": args.expected_digest,
            "tag_record": matching[0],
            "identity_stable_at_finish": identity_stable,
        },
        "settings": {
            "num_ctx": args.num_ctx,
            "num_batch": args.num_batch,
            "max_output_tokens": args.max_output_tokens,
            "max_input_tokens": context_profile["max_input_tokens"],
            "context_reserve_tokens": context_profile["context_reserve_tokens"],
            "max_concurrency": 1,
            "embedding_model": args.embedding_model,
            "embedding_dimensions": args.embedding_dimensions,
            "timeout_seconds": args.timeout_seconds,
            "seed": args.seed,
            "temperature": 0,
            "reasoning": settings.ollama_reasoning,
            "structured_output_transport": settings.ollama_structured_output_transport,
            "repair_attempts": settings.ollama_repair_attempts,
            "config_version": gateway.config_version,
        },
        "acceptance_thresholds": {
            "warm_p95_limit_ms": LATENCY_P95_LIMIT_MS,
            "model_allocation_limit_bytes": MODEL_ALLOCATION_LIMIT_BYTES,
            "minimum_steady_host_free_percent": MINIMUM_FREE_MEMORY_PERCENT,
            "steady_swapout_limit_bytes": STEADY_SWAPOUT_LIMIT_BYTES,
        },
        "cold": cold.model_dump(mode="json"),
        "warm": warm.model_dump(mode="json"),
        "concurrency": concurrency,
        "embedding_probe": embedding_probe,
        "cold_start_memory": cold_memory,
        "memory": memory,
        "acceptance_checks": checks,
        "advisory_checks": advisories,
        "gate_passed": gate_passed,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=ACCEPTED_MODEL)
    parser.add_argument("--expected-digest", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--fixture-dir", type=Path, default=Path("tests/fixtures/evaluation"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--warm-repetitions", type=int, default=10)
    parser.add_argument("--num-ctx", type=int, default=ACCEPTED_NUM_CTX)
    parser.add_argument("--num-batch", type=int, default=32)
    parser.add_argument("--max-output-tokens", type=int, default=ACCEPTED_MAX_OUTPUT_TOKENS)
    parser.add_argument(
        "--reasoning",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="enable or disable model reasoning during the benchmark",
    )
    parser.add_argument(
        "--structured-output-transport",
        choices=("json_schema", "json"),
        default="json_schema",
        help="use constrained JSON Schema decoding or JSON mode with schema validation",
    )
    parser.add_argument("--embedding-model", default="qwen3-embedding:4b")
    parser.add_argument("--embedding-dimensions", type=int, default=1024)
    parser.add_argument("--timeout-seconds", type=float, default=300)
    parser.add_argument("--seed", type=int, default=1729)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.warm_repetitions < 1:
        raise SystemExit("--warm-repetitions must be at least one")
    report = asyncio.run(_run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(args.output), "gate_passed": report["gate_passed"]}))
    return 0 if report["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
