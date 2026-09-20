from __future__ import annotations

import asyncio

import httpx

from scripts.phase1_benchmark import (
    ACCEPTED_MAX_INPUT_TOKENS,
    ACCEPTED_MAX_OUTPUT_TOKENS,
    ACCEPTED_MODEL,
    ACCEPTED_NUM_CTX,
    _benchmark_context_profile,
    _benchmark_gate_passed,
    _embedding_probe,
    _memory_summary,
    _parser,
)


def test_benchmark_gate_requires_resource_advisories() -> None:
    checks = {
        "cold_fixture_passed": True,
        "warm_fixtures_passed": True,
        "context_length_matches": True,
    }

    assert _benchmark_gate_passed(
        checks,
        {
            "steady_host_headroom_observed": True,
            "steady_swapout_within_limit": True,
        },
    )
    assert not _benchmark_gate_passed(
        checks,
        {
            "steady_host_headroom_observed": False,
            "steady_swapout_within_limit": True,
        },
    )


def test_embedding_probe_validates_dimensions() -> None:
    requests: list[tuple[str, str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = request.read()
        requests.append((request.method, request.url.path, payload))
        return httpx.Response(200, json={"embeddings": [[0.0] * 1024]})

    async def run() -> dict[str, object]:
        async with httpx.AsyncClient(
            base_url="http://ollama.test",
            transport=httpx.MockTransport(respond),
        ) as client:
            return await _embedding_probe(
                client,
                model="qwen3-embedding:4b",
                expected_dimensions=1024,
            )

    result = asyncio.run(run())

    assert result["status"] == "valid"
    assert result["observed_dimensions"] == 1024
    assert requests[0][0:2] == ("POST", "/api/embed")
    assert b"qwen3-embedding:4b" in requests[0][2]


def test_memory_summary_tracks_reasoning_context_separately_from_embedding() -> None:
    summary = _memory_summary(
        [
            {
                "models": [
                    {
                        "name": "qwen3:14b",
                        "context_length": 24_576,
                        "size": 9_000_000_000,
                        "size_vram": 8_000_000_000,
                    },
                    {
                        "name": "qwen3-embedding:4b",
                        "context_length": 32_768,
                        "size": 3_000_000_000,
                        "size_vram": 2_000_000_000,
                    },
                ],
                "host": {"free_percent": 42.0},
            }
        ],
        {"swapouts": 10, "page_size_bytes": 4096},
        {"swapouts": 11, "page_size_bytes": 4096},
        reasoning_model="qwen3:14b",
    )

    assert summary["observed_context_lengths"] == [24_576, 32_768]
    assert summary["observed_reasoning_context_lengths"] == [24_576]
    assert summary["peak_model_size_bytes"] == 9_000_000_000
    assert summary["peak_model_vram_bytes"] == 8_000_000_000
    assert summary["peak_combined_model_size_bytes"] == 12_000_000_000
    assert summary["peak_combined_model_vram_bytes"] == 10_000_000_000


def test_memory_summary_accepts_32k_reasoning_context_with_embedding_resident() -> None:
    summary = _memory_summary(
        [
            {
                "models": [
                    {
                        "name": "qwen3:14b",
                        "context_length": 32_768,
                        "size": 11_000_000_000,
                        "size_vram": 10_000_000_000,
                    },
                    {
                        "name": "qwen3-embedding:4b",
                        "context_length": 2_048,
                        "size": 3_000_000_000,
                        "size_vram": 2_000_000_000,
                    },
                ],
                "host": {"free_percent": 24.0},
            }
        ],
        {"swapouts": 10, "page_size_bytes": 4096},
        {"swapouts": 10, "page_size_bytes": 4096},
        reasoning_model="qwen3:14b",
    )

    assert summary["observed_context_lengths"] == [2_048, 32_768]
    assert summary["observed_reasoning_context_lengths"] == [32_768]


def test_benchmark_cli_defaults_to_accepted_14b_profile() -> None:
    parser = _parser()
    args = parser.parse_args(
        [
            "--expected-digest",
            "bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8",
            "--output",
            "benchmark.json",
        ]
    )

    assert args.model == ACCEPTED_MODEL
    assert args.num_ctx == ACCEPTED_NUM_CTX
    assert args.num_batch == 32
    assert args.max_output_tokens == ACCEPTED_MAX_OUTPUT_TOKENS
    assert args.reasoning is False
    assert args.structured_output_transport == "json_schema"


def test_benchmark_context_profile_defaults_to_accepted_32k_budget() -> None:
    assert _benchmark_context_profile(ACCEPTED_NUM_CTX, ACCEPTED_MAX_OUTPUT_TOKENS) == {
        "max_input_tokens": ACCEPTED_MAX_INPUT_TOKENS,
        "context_reserve_tokens": 4_096,
        "compaction_trigger_tokens": 19_968,
        "compaction_target_tokens": 14_336,
        "recent_tail_max_tokens": 8_192,
    }
