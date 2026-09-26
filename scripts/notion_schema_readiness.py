"""Read-only redacted readiness report for the explicit Notion schema."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Mapping, Sequence

import httpx

from app.connectors.notion import (
    NotionActionItemRecord,
    NotionApplicationRecord,
    NotionConnector,
    NotionDateValue,
    NotionInterviewRecord,
    NotionPreflightResult,
    NotionTypedPageRecord,
)
from app.core.config import Settings, get_settings


def _date_precision(value: NotionDateValue | None) -> str:
    if value is None or value.start is None:
        return "unscheduled"
    return "datetime" if "T" in value.start else "date"


def _date_precision_counts(values: Sequence[NotionDateValue | None]) -> dict[str, int]:
    counts = {"date": 0, "datetime": 0, "unscheduled": 0}
    for value in values:
        counts[_date_precision(value)] += 1
    return counts


def _relation_coverage(
    records: Sequence[NotionTypedPageRecord], relation_names: Sequence[str]
) -> dict[str, dict[str, int]]:
    coverage: dict[str, dict[str, int]] = {}
    total = len(records)
    for name in relation_names:
        linked = sum(1 for record in records if record.relation_ids.get(name))
        coverage[name] = {"linked": linked, "missing": total - linked}
    return coverage


def _blank_title_count(records: Sequence[NotionTypedPageRecord]) -> int:
    return sum(1 for record in records if not record.title.strip())


def _source_summary(
    records: Sequence[NotionTypedPageRecord],
    *,
    date_values: Sequence[NotionDateValue | None] = (),
    relation_names: Sequence[str] = (),
    review_count: int = 0,
) -> dict[str, object]:
    return {
        "ready": True,
        "count": len(records),
        "date_precision": _date_precision_counts(date_values),
        "relation_coverage": _relation_coverage(records, relation_names),
        "blank_titles": _blank_title_count(records),
        "review_count": review_count,
    }


def build_redacted_report(
    preflight: NotionPreflightResult,
    *,
    courses: Sequence[NotionTypedPageRecord],
    action_items: Sequence[NotionActionItemRecord],
    applications: Sequence[NotionApplicationRecord],
    interviews: Sequence[NotionInterviewRecord],
) -> dict[str, object]:
    """Build a report that excludes Notion IDs, titles, URLs, and raw values."""

    diagnostics = [
        {
            "code": diagnostic.code,
            "severity": diagnostic.severity,
            "property_name": diagnostic.property_name,
        }
        for diagnostic in preflight.diagnostics
    ]
    sources: dict[str, object] = {
        "courses": _source_summary(courses),
        "action_items": _source_summary(
            action_items,
            date_values=[record.date for record in action_items],
            relation_names=("Course", "Application", "Interview"),
            review_count=sum(1 for record in action_items if record.review_reason),
        ),
        "applications": _source_summary(
            applications,
            date_values=[
                value
                for record in applications
                for value in (record.applied_on, record.deadline, record.next_action_due)
            ],
        ),
        "interviews": _source_summary(
            interviews,
            date_values=[record.date for record in interviews],
            relation_names=("Application",),
        ),
    }
    for name in ("courses", "action_items", "applications", "interviews"):
        source_has_error = any(
            diagnostic.severity == "error" and diagnostic.code.startswith(f"{name}_")
            for diagnostic in preflight.diagnostics
        )
        if name not in preflight.sources or source_has_error:
            summary = dict(sources[name]) if isinstance(sources[name], Mapping) else {}
            summary["ready"] = False
            sources[name] = summary
    return {
        "ready": not diagnostics and all(
            isinstance(summary, Mapping) and summary.get("ready") is True
            for summary in sources.values()
        ),
        "sources": sources,
        "diagnostics": diagnostics,
    }


async def collect_readiness(settings: Settings) -> dict[str, object]:
    if (
        settings.notion_token is None
        or settings.notion_courses_database_id is None
        or settings.notion_action_items_database_id is None
        or settings.notion_applications_database_id is None
        or settings.notion_interviews_database_id is None
    ):
        return {
            "ready": False,
            "sources": {},
            "diagnostics": [
                {
                    "code": "notion_configuration_incomplete",
                    "severity": "error",
                    "property_name": None,
                }
            ],
        }
    timeout = httpx.Timeout(settings.connector_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        connector = NotionConnector(
            token=settings.notion_token,
            courses_database_id=settings.notion_courses_database_id,
            action_items_database_id=settings.notion_action_items_database_id,
            applications_database_id=settings.notion_applications_database_id,
            interviews_database_id=settings.notion_interviews_database_id,
            client=client,
            timeout_seconds=settings.connector_timeout_seconds,
        )
        preflight = await connector.preflight_configured_databases()
        if any(diagnostic.severity == "error" for diagnostic in preflight.diagnostics):
            return build_redacted_report(
                preflight,
                courses=(),
                action_items=(),
                applications=(),
                interviews=(),
            )
        courses, action_items, applications, interviews = await asyncio.gather(
            connector.read_courses(),
            connector.read_action_items(),
            connector.read_applications(),
            connector.read_interviews(),
        )
        return build_redacted_report(
            preflight,
            courses=courses,
            action_items=action_items,
            applications=applications,
            interviews=interviews,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compact", action="store_true", help="emit compact JSON")
    return parser


async def _async_main(args: argparse.Namespace) -> int:
    report = await collect_readiness(get_settings())
    indent = None if args.compact else 2
    print(json.dumps(report, ensure_ascii=True, indent=indent, sort_keys=True))
    return 0 if report.get("ready") is True else 1


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_async_main(_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
