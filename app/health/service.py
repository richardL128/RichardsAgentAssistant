"""Persistence bridge for model-free operational health evaluations."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import HealthState as DatabaseHealthState
from app.db.repositories import HealthRepository
from app.health.evaluator import OperationalFacts, OperationalHealth, evaluate_operational_health


def evaluate_and_persist(session: Session, facts: OperationalFacts) -> OperationalHealth:
    """Evaluate ordered rules and upsert only their redacted structured result."""

    health = evaluate_operational_health(facts)
    HealthRepository.upsert(
        session,
        check_name=health.component,
        rule=health.rule,
        state=DatabaseHealthState(health.state.value),
        last_success_at=health.last_success_at,
        next_due_at=health.next_expected_at,
        diagnostic=health.diagnostic,
    )
    return health


__all__ = ["evaluate_and_persist"]
