"""Deterministic, delivery-independent polling plan for public finance sources."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.config import Settings

V2_SOURCE_IDS = (
    "defense_gov_rss",
    "breaking_defense_public",
    "eia_public_data",
    "federal_register_energy",
    "sec_edgar",
    "company_ir_registry",
    "issuer_etf_holdings",
    "technology_official_feeds",
)


@dataclass(frozen=True, slots=True)
class SourcePollPolicy:
    source_id: str
    interval: timedelta
    lookback: timedelta
    feed_latency_target: timedelta | None


def build_v2_polling_plan(settings: Settings) -> tuple[SourcePollPolicy, ...]:
    """Resolve all cadence choices once from startup settings."""

    feed_interval = timedelta(minutes=settings.finance_feed_poll_minutes)
    bounded_backfill = timedelta(hours=settings.finance_cold_start_backfill_hours)
    intervals = {
        "defense_gov_rss": feed_interval,
        "breaking_defense_public": feed_interval,
        "eia_public_data": timedelta(
            minutes=(
                settings.finance_eia_api_poll_minutes
                if settings.finance_eia_mode == "api"
                else settings.finance_eia_bulk_poll_minutes
            )
        ),
        "federal_register_energy": timedelta(
            minutes=settings.finance_federal_register_poll_minutes
        ),
        "sec_edgar": feed_interval,
        "company_ir_registry": feed_interval,
        "issuer_etf_holdings": timedelta(minutes=settings.finance_etf_poll_minutes),
        "technology_official_feeds": feed_interval,
    }
    feed_sources = {
        "defense_gov_rss",
        "breaking_defense_public",
        "sec_edgar",
        "company_ir_registry",
        "technology_official_feeds",
    }
    return tuple(
        SourcePollPolicy(
            source_id=source_id,
            interval=intervals[source_id],
            lookback=bounded_backfill,
            feed_latency_target=(
                timedelta(minutes=15) if source_id in feed_sources else None
            ),
        )
        for source_id in V2_SOURCE_IDS
    )


def due_source_ids(
    plan: tuple[SourcePollPolicy, ...],
    *,
    last_success: Mapping[str, datetime],
    now: datetime,
) -> tuple[str, ...]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("polling time must be timezone-aware")
    current = now.astimezone(UTC)
    due: list[str] = []
    for policy in plan:
        previous = last_success.get(policy.source_id)
        if previous is None:
            due.append(policy.source_id)
            continue
        if previous.tzinfo is None or previous.utcoffset() is None:
            raise ValueError("source watermark timestamps must be timezone-aware")
        if current - previous.astimezone(UTC) >= policy.interval:
            due.append(policy.source_id)
    return tuple(due)


__all__ = ["V2_SOURCE_IDS", "SourcePollPolicy", "build_v2_polling_plan", "due_source_ids"]
