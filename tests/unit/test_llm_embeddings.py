"""Unit coverage for academic embedding infrastructure."""

from __future__ import annotations

import asyncio
import math

import httpx
import pytest

from app.core.config import Settings
from app.llm.embeddings import (
    AcademicEmbeddingGateway,
    EmbeddingErrorCode,
    EmbeddingReadiness,
    EmbeddingReadinessError,
    EmbeddingStatus,
)


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"_env_file": None}
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


class FakeEmbeddingModel:
    def __init__(
        self,
        response: object = [0.1, 0.2, 0.3],
        *,
        delay: float = 0.0,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.delay = delay
        self.error = error
        self.texts: list[str] = []

    async def aembed_query(self, text: str) -> object:
        self.texts.append(text)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.response

    async def aembed_documents(self, texts: list[str]) -> object:
        self.texts.extend(texts)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return [self.response for _text in texts]


@pytest.mark.asyncio
async def test_reflection_embedding_returns_validated_vector_identity_and_dimension() -> None:
    fake = FakeEmbeddingModel([1, 2.5, 3.0])
    settings = _settings(
        embedding_model="qwen3-embedding:4b",
        embedding_model_digest="embedding-digest",
        embedding_dimensions=3,
    )

    result = await AcademicEmbeddingGateway(
        settings,
        embedding_model=fake,
    ).embed_reflection_text("I struggled with ECE 250 recursion.")

    assert result.status is EmbeddingStatus.VALID
    assert result.embedding is not None
    norm = math.sqrt((1.0 * 1.0) + (2.5 * 2.5) + (3.0 * 3.0))
    assert result.embedding.vector == [1.0 / norm, 2.5 / norm, 3.0 / norm]
    assert result.embedding.dimension == 3
    assert result.model_identity == (
        "qwen3-embedding:4b@embedding-digest;dim=3;normalized=true;input=academic-rag-v1"
    )
    assert result.telemetry.dimension == 3
    assert result.telemetry.input_characters == len("I struggled with ECE 250 recursion.")
    assert fake.texts == ["I struggled with ECE 250 recursion."]


@pytest.mark.asyncio
async def test_embedding_result_never_retains_raw_reflection_text_on_failure() -> None:
    raw_reflection = "I struggled with ECE 250 recursion."
    fake = FakeEmbeddingModel(error=RuntimeError(raw_reflection))

    result = await AcademicEmbeddingGateway(
        _settings(),
        embedding_model=fake,
    ).embed_reflection_text(raw_reflection)

    assert result.status is EmbeddingStatus.FAILED
    assert result.error_code is EmbeddingErrorCode.MODEL_ERROR
    assert result.error_diagnostic == "Embedding request failed."
    assert raw_reflection not in repr(result)
    assert raw_reflection not in str(result.model_dump())


@pytest.mark.asyncio
async def test_embedding_timeout_is_typed_and_safe() -> None:
    result = await AcademicEmbeddingGateway(
        _settings(embedding_timeout_seconds=0.001),
        embedding_model=FakeEmbeddingModel(delay=0.05),
    ).embed_reflection_text("practice recursion")

    assert result.status is EmbeddingStatus.FAILED
    assert result.embedding is None
    assert result.error_code is EmbeddingErrorCode.MODEL_TIMEOUT
    assert result.telemetry.error_code is EmbeddingErrorCode.MODEL_TIMEOUT
    assert result.error_diagnostic == "Embedding request timed out."


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_vector", [[], [0.1, math.nan], [0.1, "2.0"], [True, 0.2]])
async def test_invalid_embedding_vectors_return_typed_failure(bad_vector: object) -> None:
    result = await AcademicEmbeddingGateway(
        _settings(),
        embedding_model=FakeEmbeddingModel(bad_vector),
    ).embed_reflection_text("practice recursion")

    assert result.status is EmbeddingStatus.FAILED
    assert result.embedding is None
    assert result.error_code is EmbeddingErrorCode.INVALID_VECTOR
    assert result.error_diagnostic == "Embedding model returned an invalid vector."


@pytest.mark.asyncio
async def test_empty_embedding_input_is_typed_without_model_call() -> None:
    fake = FakeEmbeddingModel()

    result = await AcademicEmbeddingGateway(
        _settings(),
        embedding_model=fake,
    ).embed_reflection_text("   ")

    assert result.status is EmbeddingStatus.FAILED
    assert result.error_code is EmbeddingErrorCode.EMPTY_INPUT
    assert result.embedding is None
    assert fake.texts == []


@pytest.mark.asyncio
async def test_embedding_dimension_mismatch_is_typed() -> None:
    result = await AcademicEmbeddingGateway(
        _settings(embedding_dimensions=4),
        embedding_model=FakeEmbeddingModel([0.1, 0.2, 0.3]),
    ).embed_reflection_text("practice recursion")

    assert result.status is EmbeddingStatus.FAILED
    assert result.embedding is None
    assert result.error_code is EmbeddingErrorCode.WRONG_DIMENSION


@pytest.mark.asyncio
async def test_embedding_batch_is_atomic_and_validated() -> None:
    fake = FakeEmbeddingModel([0.1, 0.2, 0.3])

    result = await AcademicEmbeddingGateway(
        _settings(embedding_dimensions=3),
        embedding_model=fake,
    ).embed_academic_texts(["chunk one", "chunk two"])

    assert result.status is EmbeddingStatus.VALID
    assert [embedding.dimension for embedding in result.embeddings] == [3, 3]
    assert fake.texts == ["chunk one", "chunk two"]


@pytest.mark.asyncio
async def test_embedding_documents_alias_uses_batch_contract() -> None:
    fake = FakeEmbeddingModel([0.1, 0.2, 0.3])

    result = await AcademicEmbeddingGateway(
        _settings(embedding_dimensions=3),
        embedding_model=fake,
    ).embed_documents(["chunk one", "chunk two"])

    assert result.status is EmbeddingStatus.VALID
    assert len(result.embeddings) == 2
    assert fake.texts == ["chunk one", "chunk two"]


@pytest.mark.asyncio
async def test_private_embedding_aliases_preserve_academic_gateway_contracts() -> None:
    fake = FakeEmbeddingModel([0.1, 0.2, 0.3])
    gateway = AcademicEmbeddingGateway(
        _settings(embedding_dimensions=3),
        embedding_model=fake,
    )

    single = await gateway.embed_private_text("private preference")
    batch = await gateway.embed_private_texts(["private one", "private two"])

    assert single.status is EmbeddingStatus.VALID
    assert batch.status is EmbeddingStatus.VALID
    assert len(batch.embeddings) == 2
    assert fake.texts == ["private preference", "private one", "private two"]


@pytest.mark.asyncio
async def test_embedding_readiness_verifies_identity_capability_and_probe() -> None:
    settings = _settings(
        ollama_base_url="http://ollama.test:11434",
        embedding_model="qwen3-embedding:4b",
        embedding_model_digest="embedding-digest",
        embedding_dimensions=3,
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "/api/tags": {
                        "models": [{"name": "qwen3-embedding:4b", "digest": "embedding-digest"}]
                    },
                    "/api/show": {"capabilities": ["embedding"]},
                    "/api/embed": {"embeddings": [[0.1, 0.2, 0.3]]},
                }[request.url.path],
                request=request,
            )
        ),
    ) as client:
        result = await AcademicEmbeddingGateway(
            settings,
            embedding_model=FakeEmbeddingModel([0.1, 0.2, 0.3]),
        ).ensure_ready(http_client=client)

    assert result == EmbeddingReadiness(
        model="qwen3-embedding:4b",
        digest="embedding-digest",
        dimension=3,
        normalized=True,
        input_policy_version="academic-rag-v1",
        config_version=result.config_version,
    )


@pytest.mark.asyncio
async def test_embedding_readiness_failures_are_typed_and_redacted() -> None:
    settings = _settings(
        ollama_base_url="http://ollama.test:11434",
        embedding_model="qwen3-embedding:4b",
        embedding_model_digest="expected-digest",
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"models": [{"name": "qwen3-embedding:4b", "digest": "wrong-digest"}]},
                request=request,
            )
        ),
    ) as client:
        with pytest.raises(EmbeddingReadinessError) as raised:
            await AcademicEmbeddingGateway(
                settings,
                embedding_model=FakeEmbeddingModel([0.0] * 1024),
            ).ensure_ready(http_client=client)

    assert raised.value.code is EmbeddingErrorCode.DIGEST_MISMATCH
    assert "wrong-digest" not in raised.value.diagnostic


def test_academic_memory_settings_expose_lifecycle_defaults() -> None:
    settings = _settings()
    diagnostics = settings.safe_diagnostics()

    assert settings.academic_memory_enabled is True
    assert settings.academic_memory_default_practice_minutes == 30
    assert settings.academic_memory_snooze_after_missed_checkins == 2
    assert settings.academic_memory_delete_after_missed_checkins == 5
    assert diagnostics["academic_memory_default_practice_minutes"] == 30
    assert diagnostics["academic_memory_delete_after_missed_checkins"] == 5


def test_academic_memory_snooze_threshold_cannot_exceed_delete_threshold() -> None:
    with pytest.raises(ValueError, match="snooze threshold cannot exceed delete threshold"):
        _settings(
            academic_memory_snooze_after_missed_checkins=6,
            academic_memory_delete_after_missed_checkins=5,
        )
