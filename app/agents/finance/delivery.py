"""Persistence-safe finance briefing delivery payload rendering."""

from __future__ import annotations

from app.agents.finance.contracts import BriefingPayload, EventCard

_RAW_MARKERS = ("raw_body", "article_body", "full_text", "licensed_body")


def render_discord_briefing(payload: BriefingPayload) -> str:
    """Render a compact briefing using only citations, facts, and approved excerpts."""

    lines = [
        f"LifeAgent finance briefing {payload.status}; "
        f"sources {payload.source_allowlist_version}; run {payload.run_id}"
    ]
    if payload.source_failures:
        failed = ", ".join(failure.source_id for failure in payload.source_failures)
        lines.append(f"Source failures reported without fallback: {failed}")
    for card in payload.cards:
        lines.extend(_card_lines(card))
    content = "\n".join(lines)
    lowered = content.casefold()
    if any(marker in lowered for marker in _RAW_MARKERS):
        raise ValueError("finance delivery payload contains a raw licensed-content marker")
    return content[:6_000]


def _card_lines(card: EventCard) -> list[str]:
    exposure_symbols = (
        *card.exposure.holding_symbols,
        *card.exposure.watchlist_symbols,
        *card.exposure.etf_symbols,
    )
    symbols = ", ".join(exposure_symbols)
    citations = ", ".join(card.citations)
    facts = "; ".join(card.verified_facts[:3])
    return [
        f"- {card.title} [{card.impact_label.value}]",
        f"  facts: {facts}",
        f"  exposure: {symbols or 'none mapped'}",
        f"  uncertainty: {card.uncertainty}",
        f"  counter-case: {card.counter_case}",
        f"  links/sources: {citations}",
    ]
