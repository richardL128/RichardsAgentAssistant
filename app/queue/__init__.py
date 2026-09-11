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
    academic_clarification_status_task,
    academic_clarification_task,
    academic_material_ingestion_task,
    academic_morning_notification_periodic,
    academic_morning_notification_task,
    artifact_retention_periodic,
    defer_academic_clarification,
    defer_academic_clarification_status,
    defer_academic_material_ingestion,
    defer_academic_morning_notification,
    defer_discord_wake,
    discord_wake_task,
    register_academic_clarification_handler,
    register_academic_clarification_status_handler,
    register_academic_material_ingestion_handler,
    register_academic_morning_notification_handler,
    register_discord_wake_handler,
    shared_services_periodic,
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
    "academic_clarification_status_task",
    "academic_clarification_task",
    "academic_material_ingestion_task",
    "academic_morning_notification_periodic",
    "academic_morning_notification_task",
    "artifact_retention_periodic",
    "build_idempotency_key",
    "classify_retry_error",
    "defer_academic_clarification",
    "defer_academic_clarification_status",
    "defer_academic_material_ingestion",
    "defer_academic_morning_notification",
    "defer_discord_wake",
    "discord_wake_task",
    "list_queue_jobs",
    "procrastinate_app",
    "queue_visibility",
    "register_academic_clarification_handler",
    "register_academic_clarification_status_handler",
    "register_academic_material_ingestion_handler",
    "register_academic_morning_notification_handler",
    "register_discord_wake_handler",
    "shared_services_periodic",
    "stable_period_key",
    "validate_idempotency_key",
]
