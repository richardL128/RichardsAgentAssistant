from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "app" / "templates"


def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_ROOT),
        autoescape=select_autoescape(("html", "xml")),
    )


def test_base_template_defines_accessible_offline_shell() -> None:
    template = _environment().from_string(
        "{% extends 'base.html' %}"
        "{% block title %}Console &amp; Tests{% endblock %}"
        "{% block content %}<h1>{{ heading }}</h1>{% endblock %}"
    )

    html = template.render(active_page="activity", heading="<script>alert('x')</script>")

    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
    assert (
        """<meta name="htmx-config" content='{"allowEval": false, "allowScriptTags": false}'>"""
        in html
    )
    assert 'href="#main-content"' in html
    assert 'href="/"' in html
    assert 'href="/activity"' in html
    assert 'href="/settings/sources"' in html
    assert 'aria-current="page"' in html
    assert 'href="/static/app.css"' in html
    assert 'src="/static/htmx.min.js"' in html
    assert "Console &amp; Tests" in html
    assert "&lt;script&gt;alert(&#39;x&#39;)&lt;/script&gt;" in html
    assert "<script>alert" not in html


def test_health_badge_renders_text_and_shape_for_each_state() -> None:
    template = _environment().get_template("partials/health_badge.html")

    rendered = {
        state: template.render(state=state, label=f"{state}<script>")
        for state in ("healthy", "attention", "failed")
    }

    assert "health-badge--healthy" in rendered["healthy"]
    assert "health-badge--attention" in rendered["attention"]
    assert "health-badge--failed" in rendered["failed"]
    assert ">+<" in rendered["healthy"]
    assert ">!<" in rendered["attention"]
    assert ">x<" in rendered["failed"]
    assert "&lt;script&gt;" in rendered["healthy"]
    assert "<script>" not in rendered["healthy"]


def test_health_badge_falls_back_to_attention_for_invalid_state() -> None:
    html = (
        _environment()
        .get_template("partials/health_badge.html")
        .render(
            state="unknown",
            label="Investigate",
        )
    )

    assert 'data-state="attention"' in html
    assert "health-badge--attention" in html
    assert "Investigate" in html
