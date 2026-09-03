"""Explicit transient/permanent retry policy shared by queue workers."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from procrastinate.jobs import Job
from procrastinate.retry import RetryDecision, RetryStrategy

from app.core.errors import ErrorCategory, LifeAgentError

try:
    import httpx
except ImportError:  # pragma: no cover - optional for library consumers
    httpx = None  # type: ignore[assignment]


class RetryClassification(StrEnum):
    """The action a worker should take after an exception."""

    TRANSIENT = "transient"
    AUTHORIZATION = "authorization"
    PERMANENT = "permanent"


class RetryPolicy:
    """Configuration and deterministic delay calculation for retries."""

    def __init__(
        self,
        *,
        max_attempts: int = 3,
        base_delay_seconds: float = 2.0,
        max_delay_seconds: float = 300.0,
        jitter_ratio: float = 0.2,
        random_fn: Callable[[], float] = random.random,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if base_delay_seconds <= 0 or max_delay_seconds <= 0:
            raise ValueError("retry delays must be positive")
        if base_delay_seconds > max_delay_seconds:
            raise ValueError("base delay cannot exceed the maximum delay")
        if not 0 <= jitter_ratio <= 1:
            raise ValueError("jitter ratio must be between zero and one")
        self.max_attempts = max_attempts
        self.base_delay_seconds = base_delay_seconds
        self.max_delay_seconds = max_delay_seconds
        self.jitter_ratio = jitter_ratio
        self.random_fn = random_fn

    def classify(self, exception: BaseException) -> RetryClassification:
        return classify_retry_error(exception)

    def delay_seconds(self, attempt: int) -> float:
        """Return a capped exponential delay with injectable jitter.

        ``attempt`` is zero-based (the first retry after the initial attempt is
        attempt zero). Jitter only increases the delay and is bounded by the
        configured cap, making operational upper bounds straightforward.
        """

        if attempt < 0:
            raise ValueError("attempt must not be negative")
        exponential = min(self.max_delay_seconds, self.base_delay_seconds * (2**attempt))
        sample = min(1.0, max(0.0, self.random_fn()))
        jittered = exponential * (1 + self.jitter_ratio * sample)
        return min(self.max_delay_seconds, jittered)


def classify_retry_error(exception: BaseException) -> RetryClassification:
    """Classify connector and model failures without relying on error text alone."""

    if isinstance(exception, LifeAgentError):
        return {
            ErrorCategory.TRANSIENT: RetryClassification.TRANSIENT,
            ErrorCategory.AUTHORIZATION: RetryClassification.AUTHORIZATION,
            ErrorCategory.PERMANENT: RetryClassification.PERMANENT,
        }[exception.record.category]
    status_code = _status_code(exception)
    if status_code in {401, 403} or _has_auth_marker(exception):
        return RetryClassification.AUTHORIZATION
    if status_code == 429 or (status_code is not None and 500 <= status_code <= 599):
        return RetryClassification.TRANSIENT
    if isinstance(exception, (TimeoutError, asyncio.TimeoutError, ConnectionError)):
        return RetryClassification.TRANSIENT
    if httpx is not None and isinstance(exception, httpx.TransportError):
        return RetryClassification.TRANSIENT
    if _has_transient_marker(exception):
        return RetryClassification.TRANSIENT
    return RetryClassification.PERMANENT


class TransientRetryStrategy(RetryStrategy):
    """Procrastinate strategy that retries only explicitly transient failures."""

    def __init__(self, policy: RetryPolicy | None = None) -> None:
        self.policy = policy or RetryPolicy()

    def get_retry_decision(self, *, exception: BaseException, job: Job) -> RetryDecision | None:
        # Procrastinate exposes the number of attempts completed before the
        # currently running attempt. Include the current failure in the cap.
        if job.attempts + 1 >= self.policy.max_attempts:
            return None
        if self.policy.classify(exception) is not RetryClassification.TRANSIENT:
            return None
        delay = max(1, round(self.policy.delay_seconds(job.attempts)))
        return RetryDecision(retry_in={"seconds": delay})


def _status_code(exception: BaseException) -> int | None:
    value: Any = getattr(exception, "status_code", None)
    if value is None:
        response = getattr(exception, "response", None)
        value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def _has_auth_marker(exception: BaseException) -> bool:
    text = f"{exception.__class__.__name__} {exception}".lower()
    return any(
        marker in text for marker in ("invalid token", "invalid_token", "unauthorized", "forbidden")
    )


def _has_transient_marker(exception: BaseException) -> bool:
    text = f"{exception.__class__.__name__} {exception}".lower()
    return any(
        marker in text
        for marker in (
            "rate limit",
            "temporarily unavailable",
            "service unavailable",
            "connection reset",
            "model busy",
            "model overloaded",
            "model unavailable",
            "model transient",
            "ollama unavailable",
            "gateway timeout",
            "temporary",
            "transient",
        )
    )
