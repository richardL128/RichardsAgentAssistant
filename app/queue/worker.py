"""Academic worker startup for Discord work, ingestion, and confirmations."""

from __future__ import annotations

from app.agents.academic_planner.clarification_job import (
    run_academic_clarification,
    run_academic_clarification_status,
)
from app.agents.academic_planner.discord_wake_job import run_discord_wake
from app.agents.academic_planner.material_ingestion import run_assessment_material_ingestion
from app.queue.app import procrastinate_app
from app.queue.tasks import (
    register_academic_clarification_handler,
    register_academic_clarification_status_handler,
    register_academic_material_ingestion_handler,
    register_discord_wake_handler,
)

register_academic_clarification_handler(run_academic_clarification)
register_academic_clarification_status_handler(run_academic_clarification_status)
register_academic_material_ingestion_handler(run_assessment_material_ingestion)
register_discord_wake_handler(run_discord_wake)

__all__ = ["procrastinate_app"]
