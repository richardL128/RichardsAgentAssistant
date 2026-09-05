from __future__ import annotations

from datetime import UTC, datetime

from app.main import templates
from app.operations.contracts import ConsoleState, HealthCard


def _card(
    component: str,
    label: str,
    state: ConsoleState,
    *,
    diagnostic: str,
    activity_url: str,
    last_success_at: datetime | None = None,
    next_expected_at: datetime | None = None,
) -> HealthCard:
    return HealthCard(
        component=component,
        label=label,
        state=state,
        last_success_at=last_success_at,
        next_expected_at=next_expected_at,
        diagnostic=diagnostic,
        activity_url=activity_url,
    )


def _cards() -> tuple[HealthCard, ...]:
    return (
        _card(
            "finance",
            "Finance briefing",
            "healthy",
            last_success_at=datetime(2026, 1, 15, 15, 30, tzinfo=UTC),
            next_expected_at=datetime(2026, 7, 15, 13, 0, tzinfo=UTC),
            diagnostic="Eight trusted sources checked.",
            activity_url="/activity?agent=finance",
        ),
        _card(
            "code_review",
            "Code review",
            "attention",
            diagnostic="One repository needs review.",
            activity_url="/activity?agent=code_review&attention_only=true",
        ),
        _card(
            "academic_planner",
            "Academic planner",
            "failed",
            diagnostic="Discord delivery failed at 08:00 ET.",
            activity_url="/activity?agent=academic_planner&attention_only=true",
        ),
        _card(
            "shared_services",
            "Shared services",
            "healthy",
            diagnostic="Database, queue, and connectors are healthy.",
            activity_url="/activity?agent=shared_services",
        ),
    )


def _render(cards: tuple[HealthCard, ...], failed_cards: tuple[HealthCard, ...]) -> str:
    return templates.env.get_template("health.html").render(
        active_page="health",
        cards=cards,
        failed_cards=failed_cards,
    )


def test_health_template_renders_four_cards_with_exact_toronto_times_and_links() -> None:
    cards = _cards()
    html = _render(cards, (cards[2],))

    assert html.count('class="health-card health-card--') == 4
    assert 'data-component="finance"' in html
    assert "Finance briefing" in html
    assert "Code review" in html
    assert "Academic planner" in html
    assert "Shared services" in html
    assert "2026-01-15 10:30:00 EST" in html
    assert "2026-07-15 09:00:00 EDT" in html
    assert "Never" in html
    assert "Eight trusted sources checked." in html
    assert "One repository needs review." in html
    assert 'href="/activity?agent=finance"' in html
    assert 'href="/activity?agent=code_review&amp;attention_only=true"' in html
    assert 'href="/activity?agent=academic_planner&amp;attention_only=true"' in html
    assert 'data-state="healthy"' in html
    assert 'data-state="attention"' in html
    assert 'data-state="failed"' in html


def test_health_template_warning_banner_only_renders_for_failed_cards() -> None:
    cards = _cards()

    failed_html = _render(cards, (cards[2],))
    healthy_html = _render(tuple(card for card in cards if card.state != "failed"), ())

    assert 'role="alert"' in failed_html
    assert "Failed components need attention" in failed_html
    assert "Academic planner: Discord delivery failed at 08:00 ET." in failed_html
    assert 'role="alert"' not in healthy_html
    assert "Failed components need attention" not in healthy_html


def test_health_template_escapes_backend_derived_text() -> None:
    cards = (
        _card(
            "finance",
            "Finance <script>alert('label')</script>",
            "failed",
            diagnostic="Diagnostic <script>alert('diagnostic')</script>",
            activity_url="/activity?agent=finance&attention_only=true",
        ),
    )

    html = _render(cards, cards)

    assert "Finance &lt;script&gt;alert(&#39;label&#39;)&lt;/script&gt;" in html
    assert "Diagnostic &lt;script&gt;alert(&#39;diagnostic&#39;)&lt;/script&gt;" in html
    assert "<script>alert" not in html
