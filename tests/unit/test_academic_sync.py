from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from app.agents.academic_planner.sync import (
    AcademicClarificationService,
    AcademicNotionSync,
)
from app.connectors.discord import DiscordDeliveryReceipt
from app.connectors.discord_gateway import DiscordClarificationInteraction
from app.connectors.notion import (
    NotionAssessment,
    NotionConnector,
    NotionCourse,
    NotionDateValue,
    NotionDiscoveryDiagnostic,
    NotionDiscoveryResult,
    NotionWriteConflict,
)

NOW = datetime(2026, 9, 6, 14, tzinfo=UTC)
CHANNEL = "987654321012345678"
USER = "123456789012345678"


class _Connector:
    def __init__(self, result: NotionDiscoveryResult) -> None:
        self.result = result
        self.renames: list[dict[str, Any]] = []
        self.conflict = False

    async def discover_course_assessments(self) -> NotionDiscoveryResult:
        return self.result

    async def rename_assessment_title(self, **kwargs: Any) -> object:
        self.renames.append(kwargs)
        if self.conflict:
            raise NotionWriteConflict()
        return object()


class _Store:
    def __init__(self) -> None:
        self.calendars: list[tuple[Any, str, str | None]] = []
        self.assessments: list[tuple[Any, str, str | None]] = []
        self.reconciled: list[tuple[str, tuple[str, ...]]] = []
        self.clarifications: dict[str, dict[str, Any]] = {}
        self.clarification_keys: dict[str, str] = {}
        self.reminders: list[tuple[str, str, str | None]] = []
        self.reminder_keys: set[tuple[str, str, object]] = set()
        self.cursors: dict[str, str | None] = {}

    def save_sync_cursor(self, database: str, cursor: str | None) -> None:
        self.cursors[database] = cursor

    def upsert_course_calendar(
        self,
        course: Any,
        *,
        status: str = "valid",
        diagnostic_code: str | None = None,
        schema_fingerprint: str | None = None,
    ) -> str:
        self.calendars.append((course, status, diagnostic_code))
        return str(uuid.uuid4())

    def upsert_synced_assessment(
        self,
        course: Any,
        assessment: Any,
        *,
        kind: str,
        label_source: str | None = None,
    ) -> str:
        self.assessments.append((assessment, kind, label_source))
        return str(uuid.uuid5(uuid.NAMESPACE_URL, assessment.notion_id))

    def reconcile_assessment_source(
        self,
        source_id: str,
        seen_ids: list[str],
        *,
        synced_at: datetime | None = None,
    ) -> int:
        self.reconciled.append((source_id, tuple(seen_ids)))
        return 1

    def expire_clarifications(self, *, now: datetime | None = None) -> int:
        return 0

    def create_or_get_clarification(self, **kwargs: Any) -> str:
        key = str(kwargs["idempotency_key"])
        existing = self.clarification_keys.get(key)
        if existing is not None:
            return existing
        clarification_id = str(uuid.uuid4())
        self.clarification_keys[key] = clarification_id
        self.clarifications[clarification_id] = {
            "id": clarification_id,
            "state": "pending",
            "delivery_id": None,
            "delivered_at": None,
            **kwargs,
        }
        return clarification_id

    def get_clarification(self, clarification_id: uuid.UUID | str) -> dict[str, Any] | None:
        return self.clarifications.get(str(clarification_id))

    def mark_clarification_delivered(
        self,
        clarification_id: uuid.UUID | str,
        *,
        delivery_id: str,
        delivered_at: datetime | None = None,
    ) -> None:
        row = self.clarifications[str(clarification_id)]
        row.update(state="delivered", delivery_id=delivery_id, delivered_at=delivered_at)

    def claim_clarification(
        self,
        clarification_id: uuid.UUID | str,
        action: str,
        actor_id: int,
        *,
        now: datetime | None = None,
    ) -> tuple[str, dict[str, Any]]:
        row = self.clarifications[str(clarification_id)]
        if row["state"] in {"claimed", "applied", "ignored", "conflict", "failed"}:
            return str(row["state"]), row
        row["decision"] = action
        if action == "ignore":
            row["state"] = "ignored"
            return "ignored", row
        row["state"] = "claimed"
        return "ready", row

    def mark_clarification_applied(
        self, clarification_id: uuid.UUID | str, *, applied_at: datetime | None = None
    ) -> None:
        self.clarifications[str(clarification_id)]["state"] = "applied"

    def mark_clarification_conflict(
        self, clarification_id: uuid.UUID | str, *, error_code: str
    ) -> None:
        self.clarifications[str(clarification_id)]["state"] = "conflict"

    def mark_clarification_failed(
        self, clarification_id: uuid.UUID | str, *, error_code: str
    ) -> None:
        self.clarifications[str(clarification_id)]["state"] = "failed"

    def setup_reminder_due(self, condition_code: str, fingerprint: str, day: object) -> bool:
        return (condition_code, fingerprint, day) not in self.reminder_keys

    def record_setup_reminder(
        self,
        condition_code: str,
        fingerprint: str,
        day: object,
        *,
        affected_course_codes: tuple[str, ...] = (),
        delivered_at: datetime | None = None,
        delivery_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        self.reminder_keys.add((condition_code, fingerprint, day))
        self.reminders.append((condition_code, delivery_id or "", error_code))

    def clear_setup_reminders(
        self, condition_code: str | None = None, fingerprint: str | None = None
    ) -> int:
        return 0


class _Discord:
    def __init__(self) -> None:
        self.clarifications: list[Any] = []
        self.reminders: list[Any] = []

    async def send_clarification(self, message: Any) -> DiscordDeliveryReceipt:
        self.clarifications.append(message)
        return DiscordDeliveryReceipt(external_id="111112222233333", permalink="https://example")

    async def send_setup_reminder(self, message: Any) -> DiscordDeliveryReceipt:
        self.reminders.append(message)
        return DiscordDeliveryReceipt(external_id="444445555566666", permalink="https://example")


def _assessment(page_id: str, title: str) -> NotionAssessment:
    return NotionAssessment(
        assessment_id=page_id,
        course_id="course-1",
        course_page_id="course-1",
        raw_parent_id="courses-db",
        child_database_id="child-db",
        assessments_source_id="assessment-source",
        assessments_source_type="data_source",
        page_id=page_id,
        source_url=f"https://www.notion.so/{page_id}",
        current_title=title,
        title_property_id="title-prop",
        title_property_name="Name",
        date_property_id="date-prop",
        date_property_name="Date",
        due=NotionDateValue(start="2026-09-08"),
        last_edited_at=NOW,
        properties={"Private Notes": "never send this to a model"},
    )


def _course(
    course_id: str,
    title: str,
    *,
    assessments: tuple[NotionAssessment, ...] = (),
    valid: bool = True,
) -> NotionCourse:
    return NotionCourse(
        course_id=course_id,
        course_page_id=course_id,
        course_title=title,
        raw_parent_id="courses-db",
        courses_source_id="courses-source",
        courses_source_type="data_source",
        last_edited_at=NOW,
        term="Fall 2026",
        assessments_database_id="child-db" if valid else None,
        child_data_source_id="assessment-source" if valid else None,
        assessments_source_id="assessment-source" if valid else None,
        assessments_source_type="data_source" if valid else None,
        title_property_id="title-prop" if valid else None,
        title_property_name="Name" if valid else None,
        date_property_id="date-prop" if valid else None,
        date_property_name="Date" if valid else None,
        assessments=assessments,
        properties={},
    )


@pytest.mark.asyncio
async def test_sync_continues_valid_courses_and_deduplicates_requests() -> None:
    valid = _course(
        "course-1",
        "BIO 101",
        assessments=(
            _assessment("event-quiz", "Quiz — Chapter 2"),
            _assessment("event-unknown", "Chapter 4"),
        ),
    )
    missing = _course("course-2", "HIST 202", valid=False)
    discovery = NotionDiscoveryResult(
        courses_database_id="courses-db",
        courses_source_id="courses-source",
        courses_source_type="data_source",
        courses=(valid, missing),
        diagnostics=(
            NotionDiscoveryDiagnostic(
                code="assessment_calendar_missing",
                severity="error",
                message="missing",
                course_page_id="course-2",
                course_title="HIST 202",
            ),
        ),
        synced_at=NOW,
    )
    store, discord = _Store(), _Discord()
    syncer = AcademicNotionSync(
        connector=cast(NotionConnector, _Connector(discovery)),
        store=store,
        discord=discord,
        discord_channel_id=CHANNEL,
    )

    first = await syncer.sync(now=NOW)
    second = await syncer.sync(now=NOW)

    assert first.status == "partial"
    assert first.valid_course_count == 1
    assert first.assessment_count == 2
    assert first.clarification_count == 1
    assert first.archived_count == 1
    assert [item[1] for item in store.assessments[:2]] == ["quiz", "event"]
    assert store.assessments[1][0].fact_state == "ambiguous"
    assert store.reconciled[0] == ("assessment-source", ("event-quiz", "event-unknown"))
    assert set(store.cursors) == {"courses-source", "assessment-source"}
    assert len(discord.clarifications) == 1
    assert len(discord.reminders) == 1
    assert second.clarification_count == 0


@pytest.mark.asyncio
async def test_missing_configuration_persists_setup_without_model_or_write() -> None:
    store = _Store()
    syncer = AcademicNotionSync(connector=None, store=store)

    result = await syncer.sync(now=NOW)

    assert result.status == "setup_required"
    assert result.diagnostic_codes == ("notion_configuration_missing",)
    assert store.assessments == []
    assert store.reminders[0][0] == "notion_configuration_missing"
    assert store.reminders[0][2] == "discord_unavailable"


@pytest.mark.asyncio
async def test_authorized_choice_is_exact_guarded_write_and_replay_is_harmless() -> None:
    store = _Store()
    clarification_id = store.create_or_get_clarification(
        event_notion_id="event-1",
        original_title="Chapter 4",
        quiz_preview_title="Quiz — Chapter 4",
        assignment_preview_title="Assignment — Chapter 4",
        expected_edited_at=NOW,
        expires_at=NOW,
        idempotency_key="one",
        title_property_id="title-prop",
    )
    connector = _Connector(NotionDiscoveryResult(courses_database_id="courses-db", synced_at=NOW))
    service = AcademicClarificationService(
        store=store,
        connector=cast(NotionConnector, connector),
    )
    interaction = DiscordClarificationInteraction(
        interaction_id="111112222233333",
        channel_id=CHANNEL,
        user_id=USER,
        clarification_id=uuid.UUID(clarification_id),
        action="quiz",
    )

    first = await service(interaction)
    replay = await service(interaction)

    assert first.status == "handled"
    assert replay.status == "duplicate"
    assert connector.renames == [
        {
            "page_id": "event-1",
            "title_property_id": "title-prop",
            "expected_title": "Chapter 4",
            "expected_last_edited_at": NOW,
            "new_title": "Quiz — Chapter 4",
        }
    ]
    assert store.clarifications[clarification_id]["state"] == "applied"


@pytest.mark.asyncio
async def test_ignore_never_writes_and_concurrent_edit_is_a_conflict() -> None:
    store = _Store()
    connector = _Connector(NotionDiscoveryResult(courses_database_id="courses-db", synced_at=NOW))
    service = AcademicClarificationService(
        store=store,
        connector=cast(NotionConnector, connector),
    )
    ignore_id = store.create_or_get_clarification(
        event_notion_id="event-ignore",
        original_title="Reading",
        quiz_preview_title="Quiz — Reading",
        assignment_preview_title="Assignment — Reading",
        expected_edited_at=NOW,
        expires_at=NOW,
        idempotency_key="ignore",
        title_property_id="title-prop",
    )
    ignored = await service(
        DiscordClarificationInteraction(
            interaction_id="111112222233333",
            channel_id=CHANNEL,
            user_id=USER,
            clarification_id=uuid.UUID(ignore_id),
            action="ignore",
        )
    )
    assert ignored.status == "ignored"
    assert connector.renames == []

    conflict_id = store.create_or_get_clarification(
        event_notion_id="event-conflict",
        original_title="Chapter 5",
        quiz_preview_title="Quiz — Chapter 5",
        assignment_preview_title="Assignment — Chapter 5",
        expected_edited_at=NOW,
        expires_at=NOW,
        idempotency_key="conflict",
        title_property_id="title-prop",
    )
    connector.conflict = True
    conflict = await service(
        DiscordClarificationInteraction(
            interaction_id="444445555566666",
            channel_id=CHANNEL,
            user_id=USER,
            clarification_id=uuid.UUID(conflict_id),
            action="assignment",
        )
    )
    assert conflict.status == "failed"
    assert store.clarifications[conflict_id]["state"] == "conflict"
