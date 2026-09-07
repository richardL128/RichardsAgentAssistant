from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.agents.finance.polling import V2_SOURCE_IDS, build_v2_polling_plan, due_source_ids
from app.core.config import Settings


def test_v2_polling_plan_has_eight_independent_cadences() -> None:
    plan = build_v2_polling_plan(Settings(_env_file=None))

    assert tuple(policy.source_id for policy in plan) == V2_SOURCE_IDS
    assert len(set(V2_SOURCE_IDS)) == 8
    intervals = {policy.source_id: policy.interval for policy in plan}
    assert intervals["defense_gov_rss"] == timedelta(minutes=10)
    assert intervals["federal_register_energy"] == timedelta(minutes=60)
    assert intervals["eia_public_data"] == timedelta(minutes=720)
    assert intervals["issuer_etf_holdings"] == timedelta(days=1)
    assert all(
        policy.feed_latency_target == timedelta(minutes=15)
        for policy in plan
        if policy.source_id
        in {
            "defense_gov_rss",
            "breaking_defense_public",
            "sec_edgar",
            "company_ir_registry",
            "technology_official_feeds",
        }
    )


def test_polling_plan_selects_eia_api_only_at_configuration_time() -> None:
    plan = build_v2_polling_plan(
        Settings(_env_file=None, finance_eia_mode="api", eia_api_key="configured")
    )
    eia = next(policy for policy in plan if policy.source_id == "eia_public_data")

    assert eia.interval == timedelta(minutes=60)


def test_due_sources_recover_from_watermarks_without_extra_source_ids() -> None:
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    plan = build_v2_polling_plan(Settings(_env_file=None))
    recent = dict.fromkeys(V2_SOURCE_IDS, now)
    recent["defense_gov_rss"] = now - timedelta(minutes=11)
    recent["issuer_etf_holdings"] = now - timedelta(hours=23)

    assert due_source_ids(plan, last_success=recent, now=now) == ("defense_gov_rss",)
    assert due_source_ids(plan, last_success={}, now=now) == V2_SOURCE_IDS
