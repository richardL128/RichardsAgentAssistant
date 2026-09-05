from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.operations.contracts import ApprovedSource, SourceSettings

TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "app" / "templates"


def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_ROOT),
        autoescape=select_autoescape(("html", "xml")),
    )


def _settings() -> SourceSettings:
    sources = tuple(
        ApprovedSource(
            slot=index,
            name=f"Source {index}",
            hostname=f"source{index}.example",
            entitlement=f"Seat {index} entitlement",
            enabled=index == 8,
        )
        for index in range(1, 9)
    )
    return SourceSettings(
        allowlist_version="finance-sources-2026.09",
        schedule_enabled=False,
        approval_complete=False,
        sources=sources,
        diagnostic="Finance schedule is gated; 0 of 8 sources are approved",
    )


def test_settings_sources_template_renders_the_read_only_allowlist() -> None:
    html = (
        _environment()
        .get_template("settings_sources.html")
        .render(active_page="sources", settings=_settings())
    )

    assert "finance-sources-2026.09" in html
    assert "Finance schedule is gated; 0 of 8 sources are approved" in html
    assert "Source approval, enablement, vendor credentials, and contract changes happen" in html
    assert "outside this UI" in html
    assert "Gated" in html
    assert "Disabled" in html
    assert "Enabled" in html
    for index in range(1, 9):
        assert f"Source {index}" in html
        assert f"source{index}.example" in html
        assert f"Seat {index} entitlement" in html
        assert f"Source {index} is" in html

    assert "<form" not in html
    assert "<button" not in html
    assert "<input" not in html
    assert "hx-post" not in html
    assert 'href="https://source1.example' not in html


def test_settings_sources_template_escapes_untrusted_source_fields() -> None:
    settings = SourceSettings(
        allowlist_version="<script>version()</script>",
        schedule_enabled=False,
        approval_complete=False,
        sources=(
            ApprovedSource(
                slot=1,
                name="<script>alert('name')</script>",
                hostname="evil.example",
                entitlement="<img src=x onerror=alert('entitlement')>",
                enabled=False,
            ),
        ),
        diagnostic="<script>alert('diagnostic')</script>",
    )

    html = (
        _environment()
        .get_template("settings_sources.html")
        .render(active_page="sources", settings=settings)
    )

    assert "&lt;script&gt;version()&lt;/script&gt;" in html
    assert "&lt;script&gt;alert(&#39;name&#39;)&lt;/script&gt;" in html
    assert "&lt;img src=x onerror=alert(&#39;entitlement&#39;)&gt;" in html
    assert "&lt;script&gt;alert(&#39;diagnostic&#39;)&lt;/script&gt;" in html
    assert "<script>version()" not in html
    assert "<script>alert" not in html
    assert "<img src=x" not in html
