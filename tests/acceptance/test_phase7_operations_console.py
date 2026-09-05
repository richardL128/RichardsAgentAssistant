from __future__ import annotations

import socket
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from threading import Thread
from typing import Any
from uuid import UUID, uuid4

import uvicorn
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, expect
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.finance import FinanceRepository
from app.db.models import (
    AgentRun,
    AuditEvent,
    Base,
    Delivery,
    DeliveryStatus,
    EvidenceClassification,
    EvidenceRef,
    HealthCheck,
    HealthState,
    RunStatus,
    RunStep,
    StepStatus,
    UIAcknowledgement,
)
from app.main import create_app

AUTH = ("operator", "private-password")
NOW = datetime(2026, 9, 4, 13, 5, tzinfo=UTC)
SECRET_MARKERS = (
    "discord-token-phase7-secret",
    "github-webhook-phase7-secret",
    "notion-token-phase7-secret",
)
LICENSED_BODY = "FULL LICENSED ARTICLE BODY MUST NEVER REACH THE CONSOLE"


class ConsoleHTMLProbe(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.main_depth = 0
        self.main_script_count = 0
        self.forms_in_main = 0
        self.buttons_in_main = 0
        self.table_rows = 0
        self.health_cards = 0
        self.hrefs: list[str] = []
        self.states: list[str] = []
        self.skip_link_seen = False
        self.viewport_seen = False
        self.htmx_config_seen = False
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag == "meta" and attr.get("name") == "viewport":
            self.viewport_seen = attr.get("content") == "width=device-width, initial-scale=1"
        if tag == "meta" and attr.get("name") == "htmx-config":
            self.htmx_config_seen = attr.get("content") == (
                '{"allowEval": false, "allowScriptTags": false}'
            )
        if tag == "main":
            self.main_depth += 1
        elif self.main_depth:
            if tag == "script":
                self.main_script_count += 1
            elif tag == "form":
                self.forms_in_main += 1
            elif tag == "button":
                self.buttons_in_main += 1
            elif tag == "tr":
                self.table_rows += 1

        if tag == "a":
            href = attr.get("href")
            if href is not None:
                self.hrefs.append(href)
            if href == "#main-content":
                self.skip_link_seen = True
        if tag == "article" and "health-card" in (attr.get("class") or ""):
            self.health_cards += 1
        if tag == "span" and (state := attr.get("data-state")):
            self.states.append(state)

    def handle_endtag(self, tag: str) -> None:
        if tag == "main" and self.main_depth:
            self.main_depth -= 1

    def handle_data(self, data: str) -> None:
        self.text_parts.append(data)

    @property
    def text(self) -> str:
        return " ".join(part.strip() for part in self.text_parts if part.strip())


class QueueTrap:
    def __init__(self) -> None:
        self.called = False

    async def __call__(self, *_: Any, **__: Any) -> None:
        self.called = True
        raise AssertionError("operations console must not enqueue jobs")


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path / f'phase7-{uuid4()}.db'}",
        artifact_root=tmp_path / "artifacts",
        ops_console_username=SecretStr(AUTH[0]),
        ops_console_password=SecretStr(AUTH[1]),
        discord_bot_token=SecretStr(SECRET_MARKERS[0]),
        github_webhook_secret=SecretStr(SECRET_MARKERS[1]),
        notion_token=SecretStr(SECRET_MARKERS[2]),
        notion_courses_database_id="courses",
        notion_assessments_database_id="assessments",
        notion_study_blocks_database_id="study-blocks",
        finance_source_allowlist_version="finance-sources-test",
    )


@contextmanager
def _client(tmp_path: Path) -> Generator[tuple[TestClient, Engine, QueueTrap]]:
    app = create_app(_settings(tmp_path))
    app.state.enqueue_code_review = queue_trap = QueueTrap()
    Base.metadata.create_all(app.state.database.engine)
    try:
        with TestClient(app) as client:
            _seed_console_data(app.state.database.engine)
            yield client, app.state.database.engine, queue_trap
    finally:
        app.state.database.dispose()


@contextmanager
def _live_server(tmp_path: Path) -> Generator[str]:
    app = create_app(_settings(tmp_path))
    app.state.enqueue_code_review = QueueTrap()
    Base.metadata.create_all(app.state.database.engine)
    _seed_console_data(app.state.database.engine)

    listen_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen_socket.bind(("127.0.0.1", 0))
    listen_socket.listen()
    port = listen_socket.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = Thread(
        target=server.run,
        kwargs={"sockets": [listen_socket]},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        listen_socket.close()
        raise RuntimeError("Phase 7 acceptance server did not start")

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listen_socket.close()
        app.state.database.dispose()


def _run(agent_name: str, status: RunStatus, summary: str, offset_hours: int) -> AgentRun:
    started_at = NOW - timedelta(hours=offset_hours)
    return AgentRun(
        id=uuid4(),
        idempotency_key=f"{agent_name}:{uuid4()}",
        agent_name=agent_name,
        trigger="schedule",
        schedule="daily",
        status=status,
        summary=summary,
        error_code="source_failed" if status in {RunStatus.ATTENTION, RunStatus.FAILED} else None,
        started_at=started_at,
        finished_at=started_at + timedelta(minutes=5),
    )


def _seed_console_data(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        finance = _run(
            "finance",
            RunStatus.ATTENTION,
            "Finance source failed; <script>window.__phase7Executed = true</script>",
            0,
        )
        code = _run("code_review", RunStatus.SUCCEEDED, "richard/lifeagent reviewed", 24)
        academic = _run("academic_planner", RunStatus.FAILED, "CS101 plan delivery failed", 48)
        session.add_all((finance, code, academic))
        session.flush()

        session.add_all(
            (
                Delivery(
                    run_id=finance.id,
                    channel="discord",
                    target="finance",
                    idempotency_key=f"discord:{finance.id}",
                    status=DeliveryStatus.SENT,
                    attempt_count=1,
                    last_attempt_at=finance.finished_at,
                    external_url="https://discord.example/messages/finance",
                ),
                EvidenceRef(
                    run_id=finance.id,
                    claim_id="claim-1",
                    title="Primary filing",
                    url="https://source.example/filing",
                    published_at=NOW - timedelta(hours=2),
                    retrieved_at=NOW,
                    classification=EvidenceClassification.PRIMARY,
                ),
                RunStep(
                    run_id=finance.id,
                    node_name="fetch_sources",
                    attempt=1,
                    status=StepStatus.ATTENTION,
                    started_at=NOW - timedelta(minutes=4),
                    ended_at=NOW - timedelta(minutes=3),
                    diagnostic="One source failed; body stored only in artifact",
                ),
                AuditEvent(
                    actor="finance",
                    action="record_theme",
                    target_type="ticker_theme",
                    target_id=f"defense backlog {LICENSED_BODY}",
                    result="succeeded",
                    run_id=finance.id,
                ),
                AuditEvent(
                    actor="code_review",
                    action="record_repository",
                    target_type="repository",
                    target_id="richard/lifeagent",
                    result="succeeded",
                    run_id=code.id,
                ),
                AuditEvent(
                    actor="academic_planner",
                    action="record_course",
                    target_type="course",
                    target_id="CS101",
                    result="succeeded",
                    run_id=academic.id,
                ),
            )
        )

        for component, state, diagnostic in (
            ("finance", HealthState.ATTENTION, "One finance source failed"),
            ("code_review", HealthState.HEALTHY, "Code review is current"),
            ("academic_planner", HealthState.FAILED, "Discord delivery failed"),
            ("shared_services", HealthState.HEALTHY, "Database and queue are healthy"),
        ):
            session.add(
                HealthCheck(
                    check_name=component,
                    rule="phase7-acceptance",
                    state=state,
                    last_success_at=NOW - timedelta(hours=1),
                    next_due_at=NOW + timedelta(hours=23),
                    checked_at=NOW,
                    diagnostic=diagnostic,
                )
            )

    with Session(engine) as session, session.begin():
        for index in range(1, 9):
            FinanceRepository.upsert_approved_source(
                session,
                source_id=f"source{index}",
                name=f"Source {index}",
                base_url=f"https://source{index}.example/api",
                source_version="v1",
                allowlist_version="finance-sources-test",
                license_note="Links only.",
                entitlement=f"Entitlement {index}",
                enabled=False,
            )


def _assert_absent_from_responses(*payloads: str) -> None:
    combined = "\n".join(payloads)
    for marker in (*SECRET_MARKERS, LICENSED_BODY):
        assert marker not in combined


def _probe(html: str) -> ConsoleHTMLProbe:
    probe = ConsoleHTMLProbe()
    probe.feed(html)
    return probe


def test_phase7_health_page_renders_each_card_with_exact_links(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, _, _):
        response = client.get("/", auth=AUTH)
        assert response.status_code == 200
        filtered_activity = client.get("/activity?agent=finance", auth=AUTH)

    assert filtered_activity.status_code == 200
    probe = _probe(response.text)

    assert probe.health_cards == 4
    for label in ("Finance briefing", "Code review", "Academic planner", "Shared services"):
        assert label in probe.text
    assert "2026-09-04 08:05:00 EDT" in probe.text
    assert "2026-09-05 08:05:00 EDT" in probe.text
    assert "/activity?agent=finance" in probe.hrefs
    assert {"healthy", "attention", "failed"}.issubset(set(probe.states))
    assert probe.viewport_seen
    assert probe.htmx_config_seen


def test_phase7_activity_filters_and_escaping(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, _, _):
        finance = client.get(
            "/api/operations/activity",
            params={
                "agent": "finance",
                "date_from": (NOW - timedelta(hours=1)).isoformat(),
                "date_to": (NOW + timedelta(hours=1)).isoformat(),
                "attention_only": "true",
                "ticker_theme": "defense",
            },
            auth=AUTH,
        )
        repository = client.get(
            "/api/operations/activity",
            params={"repository": "lifeagent"},
            auth=AUTH,
        )
        course = client.get(
            "/api/operations/activity",
            params={"course": "CS101"},
            auth=AUTH,
        )
        html_response = client.get("/activity?agent=finance&attention_only=true", auth=AUTH)

    assert finance.status_code == 200
    assert repository.status_code == 200
    assert course.status_code == 200
    assert finance.json()["total"] == 1
    assert repository.json()["items"][0]["agent"] == "code_review"
    assert course.json()["items"][0]["agent"] == "academic_planner"

    probe = _probe(html_response.text)
    assert probe.main_script_count == 0
    assert probe.text.count("window.__phase7Executed") == 1
    main_html = html_response.text.split("<main", 1)[1]
    assert "<script>window.__phase7Executed" not in main_html
    assert "&lt;script&gt;window.__phase7Executed" in main_html


def test_phase7_acknowledgement_writes_only_ui_metadata(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, engine, queue_trap):
        run_id = UUID(
            client.get("/api/operations/activity?agent=finance", auth=AUTH).json()["items"][0][
                "run_id"
            ]
        )
        before_deliveries = _count(engine, Delivery)
        before_audits = _count(engine, AuditEvent)
        response = client.post(
            f"/api/operations/activity/{run_id}/acknowledgements",
            json={"alert_key": "source_failed"},
            auth=AUTH,
        )

        assert response.status_code == 200
        assert _count(engine, UIAcknowledgement) == 1
        assert _count(engine, Delivery) == before_deliveries
        assert _count(engine, AuditEvent) == before_audits
        assert queue_trap.called is False


def _count(engine: Engine, model: type[object]) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def test_phase7_secrets_and_private_bodies_are_absent_from_api_and_html(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, _, _):
        activity = client.get("/api/operations/activity", auth=AUTH)
        detail_run_id = activity.json()["items"][0]["run_id"]
        responses = (
            client.get("/", auth=AUTH).text,
            activity.text,
            client.get(f"/api/operations/activity/{detail_run_id}", auth=AUTH).text,
            client.get("/api/operations/settings/sources", auth=AUTH).text,
            client.get("/activity", auth=AUTH).text,
            client.get(f"/activity/{detail_run_id}", auth=AUTH).text,
            client.get("/settings/sources", auth=AUTH).text,
        )

    _assert_absent_from_responses(*responses)


def test_phase7_source_settings_are_read_only_and_keyboard_reachable(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, _, _):
        response = client.get("/settings/sources", auth=AUTH)

    assert response.status_code == 200
    probe = _probe(response.text)

    assert "Approved source allowlist" in probe.text
    assert "finance-sources-test" in probe.text
    assert probe.table_rows == 9
    assert probe.forms_in_main == 0
    assert probe.buttons_in_main == 0
    assert probe.skip_link_seen
    assert probe.viewport_seen


def test_phase7_console_is_keyboard_navigable_in_a_narrow_browser(
    tmp_path: Path,
    browser: Browser,
) -> None:
    with _live_server(tmp_path) as base_url:
        context = browser.new_context(
            http_credentials={"username": AUTH[0], "password": AUTH[1]},
            viewport={"width": 375, "height": 667},
        )
        page = context.new_page()
        page.goto(base_url)

        expect(page.get_by_role("heading", name="Agent and service status")).to_be_visible()
        expect(page.locator("article.health-card")).to_have_count(4)
        page.keyboard.press("Tab")
        expect(page.get_by_role("link", name="Skip to content")).to_be_focused()
        page.keyboard.press("Enter")
        expect(page.locator("#main-content")).to_be_focused()

        page.get_by_role("link", name="Activity", exact=True).click()
        expect(page.get_by_role("heading", name="Cross-agent activity")).to_be_visible()
        expect(page.locator("main script")).to_have_count(0)
        assert page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
        )

        first_run = page.locator("article[id^='run-']").first
        first_run.get_by_role("link", name="Open raw run record").click()
        expect(page.get_by_text("Run detail", exact=True)).to_be_visible()
        page.go_back()
        page.get_by_role("button", name="Acknowledge locally").first.click()
        expect(page.get_by_text("Acknowledged locally").first).to_be_visible()

        page.get_by_role("link", name="Sources", exact=True).click()
        expect(page.get_by_role("heading", name="Approved source allowlist")).to_be_visible()
        expect(page.get_by_role("button")).to_have_count(0)
        assert page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
        )
        context.close()
