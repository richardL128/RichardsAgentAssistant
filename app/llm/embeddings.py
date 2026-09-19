"""Local embedding boundary for academic memory capture."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final, cast
from uuid import UUID, uuid4

import httpx
from langchain_ollama import OllamaEmbeddings
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.config import Settings, get_settings

EMBEDDING_NORMALIZED: Final[bool] = True
EMBEDDING_INPUT_POLICY_VERSION: Final[str] = "academic-rag-v1"


class EmbeddingStatus(StrEnum):
    VALID = "valid"
    FAILED = "failed"


class EmbeddingErrorCode(StrEnum):
    EMPTY_INPUT = "empty_input"
    MODEL_MISSING = "embedding_model_missing"
    DIGEST_MISMATCH = "embedding_model_digest_mismatch"
    UNSUPPORTED_CAPABILITY = "embedding_model_unsupported_capability"
    MODEL_TIMEOUT = "embedding_model_timeout"
    MODEL_ERROR = "embedding_model_error"
    INVALID_VECTOR = "invalid_embedding_vector"
    WRONG_DIMENSION = "wrong_embedding_dimension"
    MALFORMED_RESPONSE = "embedding_model_malformed_response"


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
    def from_values(
        cls,
        values: object,
        *,
        expected_dimension: int | None = None,
        normalize: bool = False,
    ) -> EmbeddingVector:
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
            raise ValueError("embedding model returned a non-vector value")

        vector: list[float] = []
        for value in cast(Sequence[object], values):
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError("embedding vector values must be numeric")
            vector.append(float(value))
        embedding = cls(vector=vector, dimension=len(vector))
        if expected_dimension is not None and embedding.dimension != expected_dimension:
            raise EmbeddingDimensionError
        if normalize:
            norm = math.sqrt(sum(item * item for item in embedding.vector))
            if not math.isfinite(norm) or norm <= 0:
                raise ValueError("embedding vector norm must be positive")
            embedding = cls(
                vector=[item / norm for item in embedding.vector],
                dimension=embedding.dimension,
            )
        return embedding


class EmbeddingDimensionError(ValueError):
    """The model returned a finite vector with the wrong storage dimension."""


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


class EmbeddingBatchResult(BaseModel):
    """Atomic outcome for a document-embedding batch."""

    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    status: EmbeddingStatus
    model_identity: str
    config_version: str
    embeddings: list[EmbeddingVector] = Field(default_factory=lambda: list[EmbeddingVector]())
    telemetry: EmbeddingTelemetry
    error_code: EmbeddingErrorCode | None = None
    error_diagnostic: str | None = None


class EmbeddingReadiness(BaseModel):
    """Safe embedding readiness identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str
    digest: str | None = None
    dimension: int
    normalized: bool
    input_policy_version: str
    capability: str = "embedding"
    config_version: str


class EmbeddingReadinessError(RuntimeError):
    """Typed readiness failure that omits raw model output and private input."""

    def __init__(self, code: EmbeddingErrorCode) -> None:
        self.code = code
        self.diagnostic = {
            EmbeddingErrorCode.MODEL_MISSING: "The configured embedding model is not installed.",
            EmbeddingErrorCode.DIGEST_MISMATCH: (
                "The configured embedding model digest does not match."
            ),
            EmbeddingErrorCode.UNSUPPORTED_CAPABILITY: (
                "The configured model does not advertise embedding capability."
            ),
            EmbeddingErrorCode.MODEL_TIMEOUT: "The embedding readiness probe timed out.",
            EmbeddingErrorCode.MODEL_ERROR: "The embedding readiness probe failed.",
            EmbeddingErrorCode.INVALID_VECTOR: "The embedding model returned an invalid vector.",
            EmbeddingErrorCode.WRONG_DIMENSION: "The embedding model returned the wrong dimension.",
            EmbeddingErrorCode.MALFORMED_RESPONSE: (
                "The host Ollama API returned malformed embedding metadata."
            ),
            EmbeddingErrorCode.EMPTY_INPUT: "The embedding readiness probe input was empty.",
        }[code]
        super().__init__(self.diagnostic)


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
        self._readiness_lock = asyncio.Lock()
        self._readiness_task: asyncio.Task[EmbeddingReadiness] | None = None

    async def embed_reflection_text(self, text: str) -> EmbeddingResult:
        """Embed raw reflection text for academic memory storage.

        The input text is intentionally not copied into the result or telemetry.
        Callers can persist the host reflection row even when this returns a
        typed failure.
        """

        return await self.embed_academic_text(text)

    async def embed_academic_text(self, text: str) -> EmbeddingResult:
        """Embed private academic text without copying it into telemetry.

        This neutral boundary is shared by reflection memory and assessment
        material ingestion so model identity, timeouts, and validation cannot
        drift between two embedding clients.
        """

        return await self.embed_private_text(text)

    async def embed_private_text(self, text: str) -> EmbeddingResult:
        """Embed private text without retaining raw input in telemetry or results."""

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
            embedding = EmbeddingVector.from_values(
                raw_vector,
                expected_dimension=self.settings.embedding_dimensions,
                normalize=EMBEDDING_NORMALIZED,
            )
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
        except EmbeddingDimensionError:
            return self._result(
                request_id=request_id,
                started_at=started_at,
                started_monotonic=started_monotonic,
                input_characters=len(text),
                status=EmbeddingStatus.FAILED,
                error_code=EmbeddingErrorCode.WRONG_DIMENSION,
                error_diagnostic="Embedding model returned the wrong dimension.",
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

    async def embed_academic_texts(self, texts: Sequence[str]) -> EmbeddingBatchResult:
        """Embed a bounded batch atomically.

        No vector is returned unless every non-empty input produces a finite
        vector with the configured storage dimension.
        """

        return await self.embed_private_texts(texts)

    async def embed_private_texts(self, texts: Sequence[str]) -> EmbeddingBatchResult:
        """Embed a bounded batch of private texts atomically."""

        request_id = uuid4()
        started_at = datetime.now(UTC)
        started_monotonic = time.monotonic()
        input_characters = sum(len(text) for text in texts)

        if not texts or any(not text.strip() for text in texts):
            return self._batch_result(
                request_id=request_id,
                started_at=started_at,
                started_monotonic=started_monotonic,
                input_characters=input_characters,
                status=EmbeddingStatus.FAILED,
                error_code=EmbeddingErrorCode.EMPTY_INPUT,
                error_diagnostic="Embedding batch contained an empty input.",
            )

        try:
            raw_vectors_result = await asyncio.wait_for(
                self._aembed_documents(list(texts)),
                timeout=self.settings.embedding_timeout_seconds,
            )
            if not isinstance(raw_vectors_result, Sequence) or isinstance(
                raw_vectors_result, (str, bytes, bytearray)
            ):
                raise ValueError("embedding model returned a non-batch value")
            raw_vectors = cast(Sequence[object], raw_vectors_result)
            if len(raw_vectors) != len(texts):
                raise ValueError("embedding model returned the wrong batch size")
            embeddings = [
                EmbeddingVector.from_values(
                    raw_vector,
                    expected_dimension=self.settings.embedding_dimensions,
                    normalize=EMBEDDING_NORMALIZED,
                )
                for raw_vector in raw_vectors
            ]
        except TimeoutError:
            return self._batch_result(
                request_id=request_id,
                started_at=started_at,
                started_monotonic=started_monotonic,
                input_characters=input_characters,
                status=EmbeddingStatus.FAILED,
                error_code=EmbeddingErrorCode.MODEL_TIMEOUT,
                error_diagnostic="Embedding batch request timed out.",
            )
        except EmbeddingDimensionError:
            return self._batch_result(
                request_id=request_id,
                started_at=started_at,
                started_monotonic=started_monotonic,
                input_characters=input_characters,
                status=EmbeddingStatus.FAILED,
                error_code=EmbeddingErrorCode.WRONG_DIMENSION,
                error_diagnostic="Embedding model returned the wrong dimension.",
            )
        except ValueError:
            return self._batch_result(
                request_id=request_id,
                started_at=started_at,
                started_monotonic=started_monotonic,
                input_characters=input_characters,
                status=EmbeddingStatus.FAILED,
                error_code=EmbeddingErrorCode.INVALID_VECTOR,
                error_diagnostic="Embedding model returned an invalid vector batch.",
            )
        except Exception:
            return self._batch_result(
                request_id=request_id,
                started_at=started_at,
                started_monotonic=started_monotonic,
                input_characters=input_characters,
                status=EmbeddingStatus.FAILED,
                error_code=EmbeddingErrorCode.MODEL_ERROR,
                error_diagnostic="Embedding batch request failed.",
            )

        return self._batch_result(
            request_id=request_id,
            started_at=started_at,
            started_monotonic=started_monotonic,
            input_characters=input_characters,
            status=EmbeddingStatus.VALID,
            embeddings=embeddings,
        )

    async def embed_documents(self, texts: Sequence[str]) -> EmbeddingBatchResult:
        """Compatibility batch entrypoint for ingestion/backfill adapters."""

        return await self.embed_academic_texts(texts)

    async def ensure_ready(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> EmbeddingReadiness:
        """Verify installed identity, capability, and a finite dimension probe."""

        if http_client is not None:
            return await self._readiness_probe(http_client=http_client)

        async with self._readiness_lock:
            task = self._readiness_task
            if task is None or task.done():
                task = asyncio.create_task(
                    self._readiness_probe(),
                    name="academic-embedding-readiness",
                )
                self._readiness_task = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                async with self._readiness_lock:
                    if self._readiness_task is task:
                        self._readiness_task = None

    async def _aembed_query(self, text: str) -> object:
        embedder = getattr(self._embedding_model, "aembed_query", None)
        if embedder is None or not callable(embedder):
            raise TypeError("injected embedding model does not provide aembed_query")
        result = embedder(text)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _aembed_documents(self, texts: list[str]) -> object:
        embedder = getattr(self._embedding_model, "aembed_documents", None)
        if embedder is None or not callable(embedder):
            raise TypeError("injected embedding model does not provide aembed_documents")
        result = embedder(texts)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _readiness_probe(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> EmbeddingReadiness:
        owns_client = http_client is None
        client = http_client or httpx.AsyncClient(
            base_url=self.settings.ollama_url,
            timeout=httpx.Timeout(self.settings.embedding_timeout_seconds),
        )
        try:
            tag_digest = await self._verify_installed_identity(client)
            await self._verify_embedding_capability(client)
            await self._verify_embedding_probe(client)
        finally:
            if owns_client:
                await client.aclose()
        return EmbeddingReadiness(
            model=self.settings.embedding_model,
            digest=tag_digest,
            dimension=self.settings.embedding_dimensions,
            normalized=EMBEDDING_NORMALIZED,
            input_policy_version=EMBEDDING_INPUT_POLICY_VERSION,
            config_version=self.config_version,
        )

    async def _verify_installed_identity(self, client: httpx.AsyncClient) -> str | None:
        try:
            response = await client.get(f"{self.settings.ollama_url}/api/tags")
            response.raise_for_status()
            payload = _json_mapping(response)
        except httpx.TimeoutException as exc:
            raise EmbeddingReadinessError(EmbeddingErrorCode.MODEL_TIMEOUT) from exc
        except httpx.HTTPError as exc:
            raise EmbeddingReadinessError(EmbeddingErrorCode.MODEL_ERROR) from exc
        except (TypeError, ValueError) as exc:
            raise EmbeddingReadinessError(EmbeddingErrorCode.MALFORMED_RESPONSE) from exc

        models_value = payload.get("models")
        if not isinstance(models_value, list):
            raise EmbeddingReadinessError(EmbeddingErrorCode.MALFORMED_RESPONSE)
        models = cast(list[object], models_value)
        for item in models:
            if not isinstance(item, Mapping):
                raise EmbeddingReadinessError(EmbeddingErrorCode.MALFORMED_RESPONSE)
            model = cast(Mapping[str, object], item)
            if model.get("name") == self.settings.embedding_model:
                digest = model.get("digest")
                if digest is not None and not isinstance(digest, str):
                    raise EmbeddingReadinessError(EmbeddingErrorCode.MALFORMED_RESPONSE)
                expected_digest = self.settings.embedding_model_digest
                if expected_digest is not None and digest != expected_digest:
                    raise EmbeddingReadinessError(EmbeddingErrorCode.DIGEST_MISMATCH)
                return digest
        raise EmbeddingReadinessError(EmbeddingErrorCode.MODEL_MISSING)

    async def _verify_embedding_capability(self, client: httpx.AsyncClient) -> None:
        try:
            response = await client.post(
                f"{self.settings.ollama_url}/api/show",
                json={"name": self.settings.embedding_model},
            )
            response.raise_for_status()
            payload = _json_mapping(response)
        except httpx.TimeoutException as exc:
            raise EmbeddingReadinessError(EmbeddingErrorCode.MODEL_TIMEOUT) from exc
        except httpx.HTTPError as exc:
            raise EmbeddingReadinessError(EmbeddingErrorCode.MODEL_ERROR) from exc
        except (TypeError, ValueError) as exc:
            raise EmbeddingReadinessError(EmbeddingErrorCode.MALFORMED_RESPONSE) from exc

        capabilities_value = payload.get("capabilities")
        if not isinstance(capabilities_value, list):
            raise EmbeddingReadinessError(EmbeddingErrorCode.MALFORMED_RESPONSE)
        capability_items = cast(list[object], capabilities_value)
        if not all(isinstance(item, str) for item in capability_items):
            raise EmbeddingReadinessError(EmbeddingErrorCode.MALFORMED_RESPONSE)
        capability_values = cast(list[str], capability_items)
        if "embedding" not in {item.casefold() for item in capability_values}:
            raise EmbeddingReadinessError(EmbeddingErrorCode.UNSUPPORTED_CAPABILITY)

    async def _verify_embedding_probe(self, client: httpx.AsyncClient) -> None:
        try:
            response = await client.post(
                f"{self.settings.ollama_url}/api/embed",
                json={
                    "model": self.settings.embedding_model,
                    "input": ["LifeAgent embedding readiness probe."],
                    "dimensions": self.settings.embedding_dimensions,
                    "keep_alive": self.settings.embedding_model_keep_alive_seconds,
                },
            )
            response.raise_for_status()
            payload = _json_mapping(response)
        except httpx.TimeoutException as exc:
            raise EmbeddingReadinessError(EmbeddingErrorCode.MODEL_TIMEOUT) from exc
        except httpx.HTTPError as exc:
            raise EmbeddingReadinessError(EmbeddingErrorCode.MODEL_ERROR) from exc
        except (TypeError, ValueError) as exc:
            raise EmbeddingReadinessError(EmbeddingErrorCode.MALFORMED_RESPONSE) from exc

        vector: object | None = None
        embeddings_value = payload.get("embeddings")
        if isinstance(embeddings_value, list) and embeddings_value:
            vector = cast(list[object], embeddings_value)[0]
        elif "embedding" in payload:
            vector = payload.get("embedding")
        try:
            EmbeddingVector.from_values(
                vector,
                expected_dimension=self.settings.embedding_dimensions,
            )
        except EmbeddingDimensionError as exc:
            raise EmbeddingReadinessError(EmbeddingErrorCode.WRONG_DIMENSION) from exc
        except ValueError as exc:
            raise EmbeddingReadinessError(EmbeddingErrorCode.INVALID_VECTOR) from exc

    def _build_embedding_model(self) -> OllamaEmbeddings:
        return OllamaEmbeddings(
            model=self.settings.embedding_model,
            dimensions=self.settings.embedding_dimensions,
            base_url=self.settings.ollama_url,
            keep_alive=self.settings.embedding_model_keep_alive_seconds,
            async_client_kwargs={"timeout": self.settings.embedding_timeout_seconds},
        )

    def _model_identity(self) -> str:
        identity = self._semantic_identity_payload()
        digest = identity["model_digest"] or "unpinned"
        normalized = "true" if identity["normalized"] else "false"
        return (
            f"{identity['model']}@{digest};"
            f"dim={identity['dimensions']};"
            f"normalized={normalized};"
            f"input={identity['input_policy_version']}"
        )

    def _config_version(self) -> str:
        serialized = json.dumps(
            self._semantic_identity_payload(),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _semantic_identity_payload(self) -> dict[str, object]:
        return {
            "model": self.settings.embedding_model,
            "model_digest": self.settings.embedding_model_digest,
            "dimensions": self.settings.embedding_dimensions,
            "normalized": EMBEDDING_NORMALIZED,
            "input_policy_version": EMBEDDING_INPUT_POLICY_VERSION,
        }

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

    def _batch_result(
        self,
        *,
        request_id: UUID,
        started_at: datetime,
        started_monotonic: float,
        input_characters: int,
        status: EmbeddingStatus,
        embeddings: list[EmbeddingVector] | None = None,
        error_code: EmbeddingErrorCode | None = None,
        error_diagnostic: str | None = None,
    ) -> EmbeddingBatchResult:
        finished_at = datetime.now(UTC)
        dimension = None
        if embeddings:
            dimension = embeddings[0].dimension
        telemetry = EmbeddingTelemetry(
            request_id=request_id,
            model_identity=self.model_identity,
            config_version=self.config_version,
            started_at=started_at,
            finished_at=finished_at,
            latency_ms=max(0.0, (time.monotonic() - started_monotonic) * 1000),
            input_characters=input_characters,
            dimension=dimension,
            error_code=error_code,
        )
        return EmbeddingBatchResult(
            request_id=request_id,
            status=status,
            model_identity=self.model_identity,
            config_version=self.config_version,
            embeddings=embeddings or [],
            telemetry=telemetry,
            error_code=error_code,
            error_diagnostic=error_diagnostic,
        )


def _json_mapping(response: httpx.Response) -> Mapping[str, object]:
    payload = cast(object, response.json())
    if not isinstance(payload, Mapping):
        raise ValueError("Ollama response must be a JSON object")
    return cast(Mapping[str, object], payload)


__all__ = [
    "AcademicEmbeddingGateway",
    "EmbeddingBatchResult",
    "EmbeddingErrorCode",
    "EmbeddingReadiness",
    "EmbeddingReadinessError",
    "EmbeddingResult",
    "EmbeddingStatus",
    "EmbeddingVector",
]
