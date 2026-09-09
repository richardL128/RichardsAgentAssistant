from __future__ import annotations

import asyncio

import httpx
import pytest

from app.core.config import Settings
from app.llm.ollama_runtime import OllamaRuntime, OllamaRuntimeError, OllamaRuntimeReady

MODEL = "qwen-test:latest"
DIGEST = "sha256-test-digest"


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "ollama_base_url": "http://ollama.test:11434",
        "ollama_model": MODEL,
        "ollama_model_digest": DIGEST,
        "ollama_startup_timeout_seconds": 0.1,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_expected_model_and_digest_are_ready() -> None:
    async with httpx.AsyncClient(
        base_url="http://ollama.test:11434",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"models": [{"name": MODEL, "digest": DIGEST}]},
                request=request,
            )
        ),
    ) as client:
        result = await OllamaRuntime(_settings(), http_client=client).ensure_ready()

    assert result == OllamaRuntimeReady(model=MODEL, digest=DIGEST)


@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        ({"models": []}, "model_missing"),
        ({"models": [{"name": MODEL, "digest": "wrong"}]}, "digest_mismatch"),
        ({"unexpected": []}, "malformed_response"),
        ({"models": ["not-a-model"]}, "malformed_response"),
    ],
)
@pytest.mark.asyncio
async def test_model_list_failures_are_typed_and_redacted(
    response: object,
    expected_code: str,
) -> None:
    async with httpx.AsyncClient(
        base_url="http://ollama.test:11434",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=response, request=request)
        ),
    ) as client:
        with pytest.raises(OllamaRuntimeError) as raised:
            await OllamaRuntime(_settings(), http_client=client).ensure_ready()

    assert raised.value.code == expected_code
    assert "ollama.test" not in raised.value.diagnostic
    assert DIGEST not in raised.value.diagnostic


@pytest.mark.asyncio
async def test_unreachable_server_is_typed_and_redacted() -> None:
    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private endpoint detail", request=request)

    async with httpx.AsyncClient(
        base_url="http://ollama.test:11434",
        transport=httpx.MockTransport(unavailable),
    ) as client:
        with pytest.raises(OllamaRuntimeError) as raised:
            await OllamaRuntime(_settings(), http_client=client).ensure_ready()

    assert raised.value.code == "unavailable"
    assert "private endpoint detail" not in raised.value.diagnostic


@pytest.mark.asyncio
async def test_malformed_json_is_typed() -> None:
    async with httpx.AsyncClient(
        base_url="http://ollama.test:11434",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="not json", request=request)
        ),
    ) as client:
        with pytest.raises(OllamaRuntimeError) as raised:
            await OllamaRuntime(_settings(), http_client=client).ensure_ready()

    assert raised.value.code == "malformed_response"


@pytest.mark.asyncio
async def test_startup_timeout_is_typed() -> None:
    class SlowClient:
        async def get(self, _url: str) -> httpx.Response:
            await asyncio.sleep(1)
            raise AssertionError("timeout should cancel the probe")

    runtime = OllamaRuntime(
        _settings(ollama_startup_timeout_seconds=0.001),
        http_client=SlowClient(),
    )
    with pytest.raises(OllamaRuntimeError) as raised:
        await runtime.ensure_ready()

    assert raised.value.code == "startup_timeout"


@pytest.mark.asyncio
async def test_concurrent_readiness_checks_share_one_probe() -> None:
    class CountingClient:
        def __init__(self) -> None:
            self.calls = 0

        async def get(self, url: str) -> httpx.Response:
            self.calls += 1
            await asyncio.sleep(0.01)
            return httpx.Response(
                200,
                json={"models": [{"name": MODEL, "digest": DIGEST}]},
                request=httpx.Request("GET", f"http://ollama.test:11434{url}"),
            )

    client = CountingClient()
    runtime = OllamaRuntime(_settings(), http_client=client)
    first, second = await asyncio.gather(runtime.ensure_ready(), runtime.ensure_ready())

    assert first == second == OllamaRuntimeReady(model=MODEL, digest=DIGEST)
    assert client.calls == 1
