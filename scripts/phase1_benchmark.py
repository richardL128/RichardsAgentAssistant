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
MINIMUM_FREE_MEMORY_PERCENT = 12.5


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
    return {
        "total_bytes": int(total_match.group(1)) if total_match else None,
        "free_percent": float(free_match.group(1)) if free_match else None,
        "swapouts": int(swapout_match.group(1)) if swapout_match else None,
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


def _memory_summary(
    samples: list[dict[str, object]],
    before: Mapping[str, object],
    after: Mapping[str, object],
) -> dict[str, object]:
    peak_size = 0
    peak_vram = 0
    context_lengths: set[int] = set()
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
        for model in cast(list[object], models):
            if not isinstance(model, dict):
                continue
            model_record = cast(dict[str, object], model)
            size = model_record.get("size")
            size_vram = model_record.get("size_vram")
            context = model_record.get("context_length")
            if isinstance(size, int):
                peak_size = max(peak_size, size)
            if isinstance(size_vram, int):
                peak_vram = max(peak_vram, size_vram)
            if isinstance(context, int):
                context_lengths.add(context)
    before_swapouts = before.get("swapouts")
    after_swapouts = after.get("swapouts")
    swapout_delta = None
    if isinstance(before_swapouts, int) and isinstance(after_swapouts, int):
        swapout_delta = max(0, after_swapouts - before_swapouts)
    return {
        "sample_count": len(samples),
        "sample_errors": sample_errors,
        "peak_model_size_bytes": peak_size,
        "peak_model_vram_bytes": peak_vram,
        "observed_context_lengths": sorted(context_lengths),
        "minimum_host_free_percent": min(free_percentages) if free_percentages else None,
        "swapout_delta": swapout_delta,
        "before": dict(before),
        "after": dict(after),
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

        settings = Settings(
            ollama_base_url=base_url,
            ollama_model=args.model,
            ollama_model_digest=args.expected_digest,
            ollama_num_ctx=args.num_ctx,
            ollama_max_concurrency=1,
            ollama_max_output_tokens=args.max_output_tokens,
            ollama_timeout_seconds=args.timeout_seconds,
            ollama_seed=args.seed,
        )
        gateway = LLMGateway(settings)
        harness = EvaluationHarness(gateway, benchmark_version="phase1-live-v1")
        memory_before = _host_memory_snapshot()
        memory_samples: list[dict[str, object]] = []
        stop_sampling = asyncio.Event()
        sampler = asyncio.create_task(_sample_memory(client, stop_sampling, memory_samples))
        try:
            with tempfile.TemporaryDirectory(prefix="lifeagent-phase1-") as report_dir:
                report_root = Path(report_dir)
                cold = await harness.run(
                    [valid_fixtures[0]], report_root / "cold.json", max_concurrency=1
                )
                warm_inputs = [
                    fixture for _ in range(args.warm_repetitions) for fixture in valid_fixtures
                ]
                warm = await harness.run(warm_inputs, report_root / "warm.json", max_concurrency=1)

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
        memory_after = _host_memory_snapshot()

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
    memory = _memory_summary(memory_samples, memory_before, memory_after)
    minimum_free = memory["minimum_host_free_percent"]
    checks: dict[str, bool] = {
        "cold_fixture_passed": cold.metrics.pass_rate == 1,
        "warm_fixtures_passed": warm.metrics.pass_rate == 1,
        "warm_p95_within_timeout": warm.metrics.p95_latency_ms <= LATENCY_P95_LIMIT_MS,
        "model_allocation_within_limit": (
            isinstance(memory["peak_model_vram_bytes"], int)
            and 0 < memory["peak_model_vram_bytes"] <= MODEL_ALLOCATION_LIMIT_BYTES
        ),
        "host_headroom_observed": (
            isinstance(minimum_free, (int, float)) and minimum_free >= MINIMUM_FREE_MEMORY_PERCENT
        ),
        "no_swapout_growth": memory["swapout_delta"] == 0,
        "context_length_matches": memory["observed_context_lengths"] == [args.num_ctx],
        "two_tasks_completed": all(
            result.status is InvocationStatus.VALID for result in marker_results
        ),
        "single_active_model_call": concurrency_metrics == {"active": 0, "peak": 1, "limit": 1},
        "concurrent_results_not_mixed": marker_outputs == list(markers),
        "model_intervals_non_overlapping": non_overlapping,
    }
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
        },
        "settings": {
            "num_ctx": args.num_ctx,
            "max_output_tokens": args.max_output_tokens,
            "max_concurrency": 1,
            "timeout_seconds": args.timeout_seconds,
            "seed": args.seed,
            "temperature": 0,
            "repair_attempts": settings.ollama_repair_attempts,
            "config_version": gateway.config_version,
        },
        "cold": cold.model_dump(mode="json"),
        "warm": warm.model_dump(mode="json"),
        "concurrency": concurrency,
        "memory": memory,
        "acceptance_checks": checks,
        "gate_passed": all(checks.values()),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="qwen3.8:27b")
    parser.add_argument("--expected-digest", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--fixture-dir", type=Path, default=Path("tests/fixtures/evaluation"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--warm-repetitions", type=int, default=10)
    parser.add_argument("--num-ctx", type=int, default=8192)
    parser.add_argument("--max-output-tokens", type=int, default=1024)
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
