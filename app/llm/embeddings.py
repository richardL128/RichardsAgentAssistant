"""Local embedding boundary for academic memory capture."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast
from uuid import UUID, uuid4

from langchain_ollama import OllamaEmbeddings
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.config import Settings, get_settings


class EmbeddingStatus(StrEnum):
    VALID = "valid"
    FAILED = "failed"


class EmbeddingErrorCode(StrEnum):
    EMPTY_INPUT = "empty_input"
    MODEL_TIMEOUT = "embedding_model_timeout"
    MODEL_ERROR = "embedding_model_error"
    INVALID_VECTOR = "invalid_embedding_vector"


class EmbeddingVector(BaseModel):
    """A validated vector and its storage dimension."""

    model_config = ConfigDict(extra="forbid")

    vector: list[float] = Field(min_length=1, repr=False)
    dimension: int = Field(gt=0)

    @field_validator("vector")
    @classmethod
    def vector_values_are_finite(cls, value: list[float]) -> list[float]:
        if not all(math.isfinite(item) for item in value):
            raise ValueError("embedding vector values must be finite")
        return value

    @model_validator(mode="after")
    def dimension_matches_vector(self) -> EmbeddingVector:
        if self.dimension != len(self.vector):
            raise ValueError("embedding dimension must match vector length")
        return self

    @classmethod
    def from_values(cls, values: object) -> EmbeddingVector:
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
            raise ValueError("embedding model returned a non-vector value")

        vector: list[float] = []
        for value in cast(Sequence[object], values):
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError("embedding vector values must be numeric")
            vector.append(float(value))
        return cls(vector=vector, dimension=len(vector))


class EmbeddingTelemetry(BaseModel):
    """Non-secret metadata for one embedding request."""

    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    model_identity: str
    config_version: str
    started_at: datetime
    finished_at: datetime
    latency_ms: float = Field(ge=0)
    input_characters: int = Field(ge=0)
    dimension: int | None = Field(default=None, gt=0)
    error_code: EmbeddingErrorCode | None = None


class EmbeddingResult(BaseModel):
    """Typed outcome for callers that still persist the host record on failure."""

    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    status: EmbeddingStatus
    model_identity: str
    config_version: str
    embedding: EmbeddingVector | None = None
    telemetry: EmbeddingTelemetry
    error_code: EmbeddingErrorCode | None = None
    error_diagnostic: str | None = None


AsyncEmbeddingModel = Any


class AcademicEmbeddingGateway:
    """Embed academic memory text through local Ollama without retaining raw text."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        embedding_model: AsyncEmbeddingModel | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._embedding_model = embedding_model or self._build_embedding_model()
        self.model_identity = self._model_identity()
        self.config_version = self._config_version()

    async def embed_reflection_text(self, text: str) -> EmbeddingResult:
        """Embed raw reflection text for academic memory storage.

        The input text is intentionally not copied into the result or telemetry.
        Callers can persist the host reflection row even when this returns a
        typed failure.
        """

        request_id = uuid4()
        started_at = datetime.now(UTC)
        started_monotonic = time.monotonic()

        if not text.strip():
            return self._result(
                request_id=request_id,
                started_at=started_at,
                started_monotonic=started_monotonic,
                input_characters=len(text),
                status=EmbeddingStatus.FAILED,
                error_code=EmbeddingErrorCode.EMPTY_INPUT,
                error_diagnostic="Embedding input was empty.",
            )

        try:
            raw_vector = await asyncio.wait_for(
                self._aembed_query(text),
                timeout=self.settings.embedding_timeout_seconds,
            )
            embedding = EmbeddingVector.from_values(raw_vector)
        except TimeoutError:
            return self._result(
                request_id=request_id,
                started_at=started_at,
                started_monotonic=started_monotonic,
                input_characters=len(text),
                status=EmbeddingStatus.FAILED,
                error_code=EmbeddingErrorCode.MODEL_TIMEOUT,
                error_diagnostic="Embedding request timed out.",
            )
        except ValueError:
            return self._result(
                request_id=request_id,
                started_at=started_at,
                started_monotonic=started_monotonic,
                input_characters=len(text),
                status=EmbeddingStatus.FAILED,
                error_code=EmbeddingErrorCode.INVALID_VECTOR,
                error_diagnostic="Embedding model returned an invalid vector.",
            )
        except Exception:
            return self._result(
                request_id=request_id,
                started_at=started_at,
                started_monotonic=started_monotonic,
                input_characters=len(text),
                status=EmbeddingStatus.FAILED,
                error_code=EmbeddingErrorCode.MODEL_ERROR,
                error_diagnostic="Embedding request failed.",
            )

        return self._result(
            request_id=request_id,
            started_at=started_at,
            started_monotonic=started_monotonic,
            input_characters=len(text),
            status=EmbeddingStatus.VALID,
            embedding=embedding,
        )

    async def _aembed_query(self, text: str) -> object:
        embedder = getattr(self._embedding_model, "aembed_query", None)
        if embedder is None or not callable(embedder):
            raise TypeError("injected embedding model does not provide aembed_query")
        result = embedder(text)
        if inspect.isawaitable(result):
            return await result
        return result

    def _build_embedding_model(self) -> OllamaEmbeddings:
        return OllamaEmbeddings(
            model=self.settings.embedding_model,
            base_url=self.settings.ollama_url,
            async_client_kwargs={"timeout": self.settings.embedding_timeout_seconds},
        )

    def _model_identity(self) -> str:
        digest = self.settings.embedding_model_digest
        return (
            f"{self.settings.embedding_model}@{digest}" if digest else self.settings.embedding_model
        )

    def _config_version(self) -> str:
        config = {
            "base_url": self.settings.ollama_url,
            "model": self.settings.embedding_model,
            "model_digest": self.settings.embedding_model_digest,
            "timeout_seconds": self.settings.embedding_timeout_seconds,
        }
        serialized = json.dumps(config, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _result(
        self,
        *,
        request_id: UUID,
        started_at: datetime,
        started_monotonic: float,
        input_characters: int,
        status: EmbeddingStatus,
        embedding: EmbeddingVector | None = None,
        error_code: EmbeddingErrorCode | None = None,
        error_diagnostic: str | None = None,
    ) -> EmbeddingResult:
        finished_at = datetime.now(UTC)
        telemetry = EmbeddingTelemetry(
            request_id=request_id,
            model_identity=self.model_identity,
            config_version=self.config_version,
            started_at=started_at,
            finished_at=finished_at,
            latency_ms=max(0.0, (time.monotonic() - started_monotonic) * 1000),
            input_characters=input_characters,
            dimension=embedding.dimension if embedding is not None else None,
            error_code=error_code,
        )
        return EmbeddingResult(
            request_id=request_id,
            status=status,
            model_identity=self.model_identity,
            config_version=self.config_version,
            embedding=embedding,
            telemetry=telemetry,
            error_code=error_code,
            error_diagnostic=error_diagnostic,
        )


__all__ = [
    "AcademicEmbeddingGateway",
    "EmbeddingErrorCode",
    "EmbeddingResult",
    "EmbeddingStatus",
    "EmbeddingVector",
]
