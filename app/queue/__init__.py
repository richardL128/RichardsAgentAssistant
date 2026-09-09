"""Durable queue policies and Procrastinate task definitions."""

from app.queue.app import QUEUE_NAMES, procrastinate_app
from app.queue.idempotency import (
    IdempotencyKeyError,
    build_idempotency_key,
    validate_idempotency_key,
)
from app.queue.periodic import (
    PeriodicOccurrence,
    TorontoPeriodicSchedule,
    stable_period_key,
)
from app.queue.retry import (
    RetryClassification,
    RetryPolicy,
    TransientRetryStrategy,
    classify_retry_error,
)
from app.queue.tasks import (
    academic_material_ingestion_task,
    code_review_task,
    defer_academic_material_ingestion,
    defer_discord_wake,
    defer_idempotent,
    defer_idempotent_async,
    discord_wake_task,
    finance_task,
    register_academic_material_ingestion_handler,
    register_discord_wake_handler,
    register_task_handler,
)
from app.queue.visibility import (
    QueueJobMetadata,
    QueueVisibility,
    list_queue_jobs,
    queue_visibility,
)

__all__ = [
    "QUEUE_NAMES",
    "IdempotencyKeyError",
    "PeriodicOccurrence",
    "QueueJobMetadata",
    "QueueVisibility",
    "RetryClassification",
    "RetryPolicy",
    "TorontoPeriodicSchedule",
    "TransientRetryStrategy",
    "academic_material_ingestion_task",
    "build_idempotency_key",
    "classify_retry_error",
    "code_review_task",
    "defer_academic_material_ingestion",
    "defer_discord_wake",
    "defer_idempotent",
    "defer_idempotent_async",
    "discord_wake_task",
    "finance_task",
    "list_queue_jobs",
    "procrastinate_app",
    "queue_visibility",
    "register_academic_material_ingestion_handler",
    "register_discord_wake_handler",
    "register_task_handler",
    "stable_period_key",
    "validate_idempotency_key",
]
