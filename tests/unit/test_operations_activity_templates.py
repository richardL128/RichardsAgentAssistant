from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.main import format_toronto_time
from app.operations.contracts import (
    AcknowledgementResult,
    ActivityDetail,
    ActivityItem,
    ActivityPage,
    DeliveryReceipt,
    EvidenceLink,
    ExternalLink,
    TimelineStep,
)

TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "app" / "templates"
NOW = datetime(2026, 9, 4, 13, 5, tzinfo=UTC)


def _environment() -> Environment:
    environment = Environment(
        loader=FileSystemLoader(TEMPLATE_ROOT),
        autoescape=select_autoescape(("html", "xml")),
    )
    environment.filters["toronto_time"] = format_toronto_time
    return environment


def _link(label: str, url: str) -> ExternalLink:
    return ExternalLink.model_validate({"label": label, "url": url})


def _item(*, acknowledged: bool = False) -> ActivityItem:
    return ActivityItem(
        run_id=uuid4(),
        timestamp=NOW,
        agent="finance",
        status="attention",
        severity="attention",
        summary="<script>alert('x')</script> raw **markdown** summary",
        delivery_links=(_link("Discord message", "https://discord.example/messages/1"),),
        evidence_links=(_link("Primary filing", "https://source.example/filing"),),
        unresolved="source_failed<script>",
        acknowledged=acknowledged,
    )


def test_activity_template_renders_filters_links_htmx_and_pagination() -> None:
    item = _item()
    page = ActivityPage(items=(item,), page=2, page_size=25, total=80, pages=4)
    filters = {
        "agent": "finance",
        "date_from": "2026-09-04T00:00:00-04:00",
        "date_to": "2026-09-04T23:59:59-04:00",
        "attention_only": True,
        "repository": "richard/lifeagent",
        "ticker_theme": "defense backlog",
        "course": "CS101",
    }

    html = (
        _environment()
        .get_template("activity.html")
        .render(
            active_page="activity",
            activity=page,
            filters=filters,
            error=None,
            previous_url="/activity?agent=finance&page=1",
            next_url="/activity?agent=finance&page=3",
        )
    )

    assert 'name="agent"' in html
    assert 'value="finance" selected' in html
    assert 'name="date_from"' in html
    assert 'name="date_to"' in html
    assert 'name="attention_only" value="true" checked' in html
    assert 'name="repository" value="richard/lifeagent"' in html
    assert 'name="ticker_theme" value="defense backlog"' in html
    assert 'name="course" value="CS101"' in html
    assert "2026-09-04 09:05:00 EDT" in html
    assert f'href="/activity/{item.run_id}"' in html
    assert "health-badge--attention" in html
    assert (
        'href="https://discord.example/messages/1" rel="noopener noreferrer" target="_blank"'
        in html
    )
    assert 'href="https://source.example/filing" rel="noopener noreferrer" target="_blank"' in html
    assert f'hx-post="/activity/{item.run_id}/ack"' in html
    assert f'hx-target="#ack-{item.run_id}"' in html
    assert 'hx-swap="outerHTML"' in html
    assert 'type="hidden" name="alert_key" value="run"' in html
    assert 'type="hidden" name="run_id"' not in html
    assert 'href="/activity?agent=finance&amp;page=1"' in html
    assert 'href="/activity?agent=finance&amp;page=3"' in html
    assert "Page 2 of 4" in html


def test_activity_template_escapes_summary_and_unresolved_text() -> None:
    html = (
        _environment()
        .get_template("activity.html")
        .render(
            active_page="activity",
            activity=ActivityPage(items=(_item(),), page=1, page_size=25, total=1, pages=1),
            filters={},
            error="<script>bad</script>",
        )
    )

    assert "&lt;script&gt;alert(&#39;x&#39;)&lt;/script&gt;" in html
    assert "source_failed&lt;script&gt;" in html
    assert "&lt;script&gt;bad&lt;/script&gt;" in html
    assert "<script>alert" not in html
    assert "<script>bad" not in html


def test_activity_detail_template_renders_sections_and_escapes_raw_record() -> None:
    item = _item()
    detail = ActivityDetail(
        item=item,
        timeline=(
            TimelineStep(
                name="fetch_sources",
                attempt=1,
                status="attention",
                started_at=NOW - timedelta(minutes=3),
                ended_at=NOW - timedelta(minutes=2),
                diagnostic="<b>one source failed</b>",
            ),
        ),
        deliveries=(
            DeliveryReceipt(
                channel="discord",
                status="sent",
                sent_at=NOW,
                link=_link("Discord message", "https://discord.example/messages/1"),
                error_code=None,
            ),
        ),
        evidence=(
            EvidenceLink.model_validate(
                {
                    "claim_id": "claim-1",
                    "title": "Primary <filing>",
                    "url": "https://source.example/filing",
                    "published_at": NOW - timedelta(hours=2),
                    "classification": "primary",
                }
            ),
        ),
        warnings=("warning <script>",),
        raw_record={
            "summary": "<script>alert('raw')</script>",
            "token": "[REDACTED]",
        },
    )

    html = (
        _environment()
        .get_template("activity_detail.html")
        .render(
            active_page="activity",
            detail=detail,
        )
    )

    assert "Run detail" in html
    assert "Processing timeline" in html
    assert "fetch_sources" in html
    assert "2026-09-04 09:05:00 EDT" in html
    assert 'href="https://source.example/filing" rel="noopener noreferrer" target="_blank"' in html
    assert (
        'href="https://discord.example/messages/1" rel="noopener noreferrer" target="_blank"'
        in html
    )
    assert "Warnings and errors" in html
    assert "Redacted raw record" in html
    assert "&lt;b&gt;one source failed&lt;/b&gt;" in html
    assert "Primary &lt;filing&gt;" in html
    assert "warning &lt;script&gt;" in html
    assert "&lt;script&gt;alert(&#39;raw&#39;)&lt;/script&gt;" in html
    assert "<script>alert" not in html
    assert "<b>one source failed</b>" not in html


def test_acknowledgement_partial_is_local_status_only() -> None:
    result = AcknowledgementResult(
        acknowledgement_id=uuid4(),
        run_id=uuid4(),
        alert_key="run",
        acknowledged_at=NOW,
    )

    html = _environment().get_template("partials/acknowledgement.html").render(result=result)

    assert f'id="ack-{result.run_id}"' in html
    assert "Acknowledged locally at" in html
    assert "2026-09-04 09:05:00 EDT" in html
    assert "hx-post" not in html
    assert "discord" not in html.lower()
    assert "github" not in html.lower()
    assert "notion" not in html.lower()
