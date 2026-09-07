"""Unit coverage for academic embedding infrastructure."""

from __future__ import annotations

import asyncio
import math

import pytest

from app.core.config import Settings
from app.llm.embeddings import AcademicEmbeddingGateway, EmbeddingErrorCode, EmbeddingStatus


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


@pytest.mark.asyncio
async def test_reflection_embedding_returns_validated_vector_identity_and_dimension() -> None:
    fake = FakeEmbeddingModel([1, 2.5, 3.0])
    settings = Settings(
        _env_file=None,
        embedding_model="qwen3-embedding:0.6b",
        embedding_model_digest="embedding-digest",
    )

    result = await AcademicEmbeddingGateway(
        settings,
        embedding_model=fake,
    ).embed_reflection_text("I struggled with ECE 250 recursion.")

    assert result.status is EmbeddingStatus.VALID
    assert result.embedding is not None
    assert result.embedding.vector == [1.0, 2.5, 3.0]
    assert result.embedding.dimension == 3
    assert result.model_identity == "qwen3-embedding:0.6b@embedding-digest"
    assert result.telemetry.dimension == 3
    assert result.telemetry.input_characters == len("I struggled with ECE 250 recursion.")
    assert fake.texts == ["I struggled with ECE 250 recursion."]


@pytest.mark.asyncio
async def test_embedding_result_never_retains_raw_reflection_text_on_failure() -> None:
    raw_reflection = "I struggled with ECE 250 recursion."
    fake = FakeEmbeddingModel(error=RuntimeError(raw_reflection))

    result = await AcademicEmbeddingGateway(
        Settings(_env_file=None),
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
        Settings(_env_file=None, embedding_timeout_seconds=0.001),
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
        Settings(_env_file=None),
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
        Settings(_env_file=None),
        embedding_model=fake,
    ).embed_reflection_text("   ")

    assert result.status is EmbeddingStatus.FAILED
    assert result.error_code is EmbeddingErrorCode.EMPTY_INPUT
    assert result.embedding is None
    assert fake.texts == []


def test_academic_memory_settings_expose_lifecycle_defaults() -> None:
    settings = Settings(_env_file=None)
    diagnostics = settings.safe_diagnostics()

    assert settings.academic_memory_enabled is True
    assert settings.academic_memory_default_practice_minutes == 30
    assert settings.academic_memory_snooze_after_missed_checkins == 2
    assert settings.academic_memory_delete_after_missed_checkins == 5
    assert diagnostics["academic_memory_default_practice_minutes"] == 30
    assert diagnostics["academic_memory_delete_after_missed_checkins"] == 5


def test_academic_memory_snooze_threshold_cannot_exceed_delete_threshold() -> None:
    with pytest.raises(ValueError, match="snooze threshold cannot exceed delete threshold"):
        Settings(
            _env_file=None,
            academic_memory_snooze_after_missed_checkins=6,
            academic_memory_delete_after_missed_checkins=5,
        )
