from __future__ import annotations

import asyncio

import httpx

from scripts.phase1_benchmark import _benchmark_gate_passed, _embedding_probe, _memory_summary


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
