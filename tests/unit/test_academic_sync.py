from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.agents.academic_planner.clarification_job import (
    AcademicClarificationJob,
    AcademicClarificationStatusJob,
)
from app.agents.academic_planner.sync import (
    AcademicClarificationService,
    AcademicNotionSync,
)
from app.connectors.discord import DiscordDeliveryReceipt
from app.connectors.discord_gateway import (
    DiscordClarificationAction,
    DiscordClarificationInteraction,
)
from app.connectors.notion import (
    NotionAssessment,
    NotionConnector,
    NotionCourse,
    NotionDateValue,
    NotionDiscoveryDiagnostic,
    NotionDiscoveryResult,
    NotionTitlePrecondition,
    NotionWriteConflict,
)
from app.core.errors import ErrorCode, LifeAgentError, transient_error

NOW = datetime(2026, 9, 6, 14, tzinfo=UTC)
CHANNEL = "987654321012345678"
USER = "123456789012345678"


class _Connector:
    def __init__(self, result: NotionDiscoveryResult) -> None:
        self.result = result
        self.renames: list[dict[str, Any]] = []
        self.conflict = False
        self.conflict_current_title: str | None = None
        self.transient_failures = 0

    async def discover_course_assessments(self) -> NotionDiscoveryResult:
        return self.result

    async def rename_assessment_title(self, **kwargs: Any) -> object:
        self.renames.append(kwargs)
        if self.transient_failures:
            self.transient_failures -= 1
            raise transient_error(ErrorCode.CONNECTOR_TRANSIENT, "notion timeout")
        if self.conflict:
            current = (
                NotionTitlePrecondition(
                    page_id=str(kwargs["page_id"]),
                    title_property_id=str(kwargs["title_property_id"]),
                    title_property_name="Name",
                    current_title=self.conflict_current_title,
                    last_edited_at=NOW,
                )
                if self.conflict_current_title is not None
                else None
            )
            raise NotionWriteConflict(current=current)
        return object()


class _BlockingConnector(_Connector):
    def __init__(self, result: NotionDiscoveryResult) -> None:
        super().__init__(result)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def rename_assessment_title(self, **kwargs: Any) -> object:
        self.renames.append(kwargs)
        self.started.set()
        await self.release.wait()
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


class _StatusAdapter:
    def __init__(self) -> None:
        self.edits: list[dict[str, str]] = []
        self.transient_failures = 0

    async def edit_clarification(
        self,
        *,
        channel_id: str,
        message_id: str,
        content: str,
    ) -> None:
        if self.transient_failures:
            self.transient_failures -= 1
            raise transient_error(ErrorCode.CONNECTOR_TRANSIENT, "discord edit timeout")
        self.edits.append({"channel_id": channel_id, "message_id": message_id, "content": content})


def _assessment(
    page_id: str,
    title: str,
    *,
    due: NotionDateValue | None = None,
) -> NotionAssessment:
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
        due=due or NotionDateValue(start="2026-09-08"),
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
    assert discord.clarifications[0].tutorial_title_preview == "Tutorial — Chapter 4"
    assert discord.clarifications[0].lab_title_preview == "Lab — Chapter 4"
    assert discord.clarifications[0].studying_block_title_preview == ("Studying Block — Chapter 4")
    assert len(discord.reminders) == 1
    assert second.clarification_count == 0


@pytest.mark.asyncio
async def test_sync_persists_valid_notion_date_end() -> None:
    discovery = NotionDiscoveryResult(
        courses_database_id="courses-db",
        courses_source_id="courses-source",
        courses_source_type="data_source",
        courses=(
            _course(
                "course-1",
                "ECE 250",
                assessments=(
                    _assessment(
                        "event-study",
                        "Studying Block - Race conditions",
                        due=NotionDateValue(
                            start="2026-09-10T23:00:00.000Z",
                            end="2026-09-10T23:45:00.000Z",
                        ),
                    ),
                ),
            ),
        ),
        synced_at=NOW,
    )
    store = _Store()
    syncer = AcademicNotionSync(
        connector=cast(NotionConnector, _Connector(discovery)),
        store=store,
    )

    result = await syncer.sync(now=NOW)

    assert result.status == "succeeded"
    assessment = store.assessments[0][0]
    assert assessment.due_at == datetime(2026, 9, 10, 23, tzinfo=UTC)
    assert assessment.ends_at == datetime(2026, 9, 10, 23, 45, tzinfo=UTC)
    assert assessment.fact_state == "confirmed"


@pytest.mark.asyncio
async def test_sync_rejects_invalid_notion_date_end_without_persisting_end() -> None:
    discovery = NotionDiscoveryResult(
        courses_database_id="courses-db",
        courses_source_id="courses-source",
        courses_source_type="data_source",
        courses=(
            _course(
                "course-1",
                "ECE 250",
                assessments=(
                    _assessment(
                        "event-study",
                        "Studying Block - Race conditions",
                        due=NotionDateValue(
                            start="2026-09-10T23:00:00.000Z",
                            end="2026-09-10T22:45:00.000Z",
                        ),
                    ),
                ),
            ),
        ),
        synced_at=NOW,
    )
    store = _Store()
    syncer = AcademicNotionSync(
        connector=cast(NotionConnector, _Connector(discovery)),
        store=store,
    )

    result = await syncer.sync(now=NOW)

    assert result.status == "succeeded"
    assessment = store.assessments[0][0]
    assert assessment.due_at == datetime(2026, 9, 10, 23, tzinfo=UTC)
    assert assessment.ends_at is None
    assert assessment.fact_state == "ambiguous"
    assert "end must be after the start" in str(assessment.ambiguity_reason)


@pytest.mark.asyncio
async def test_sync_queues_identifier_only_material_ingestion() -> None:
    discovery = NotionDiscoveryResult(
        courses_database_id="courses-db",
        courses_source_id="courses-source",
        courses_source_type="data_source",
        courses=(
            _course(
                "course-1",
                "ECE 222",
                assessments=(_assessment("event-assignment", "Assignment — Circuits"),),
            ),
        ),
        synced_at=NOW,
    )
    queued: list[tuple[str, str]] = []

    async def enqueue(page_id: str, fingerprint: str) -> object:
        queued.append((page_id, fingerprint))
        return object()

    syncer = AcademicNotionSync(
        connector=cast(NotionConnector, _Connector(discovery)),
        store=_Store(),
        material_enqueuer=enqueue,
    )

    result = await syncer.sync(now=NOW)

    assert result.material_job_count == 1
    assert queued[0][0] == "event-assignment"
    assert len(queued[0][1]) == 64
    assert "Assignment" not in queued[0][1]


@pytest.mark.asyncio
async def test_sync_persists_recognized_expanded_type_without_event_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid = _course(
        "course-1",
        "BIO 101",
        assessments=(_assessment("event-tutorial", "Tutorial — Week 2"),),
    )
    discovery = NotionDiscoveryResult(
        courses_database_id="courses-db",
        courses_source_id="courses-source",
        courses_source_type="data_source",
        courses=(valid,),
        synced_at=NOW,
    )
    store = _Store()
    monkeypatch.setattr(
        "app.agents.academic_planner.sync.classify_assessment_label",
        lambda *_args, **_kwargs: SimpleNamespace(
            kind=SimpleNamespace(value="tutorial"),
            source="label",
        ),
    )
    syncer = AcademicNotionSync(
        connector=cast(NotionConnector, _Connector(discovery)),
        store=store,
        discord=_Discord(),
        discord_channel_id=CHANNEL,
    )

    result = await syncer.sync(now=NOW)

    assert result.status == "succeeded"
    assert result.clarification_count == 0
    assert store.assessments[0][1] == "tutorial"
    assert store.assessments[0][0].fact_state == "confirmed"


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


@pytest.mark.parametrize(
    ("action", "expected_title"),
    [
        ("quiz", "Quiz — Chapter 4"),
        ("assignment", "Assignment — Chapter 4"),
        ("tutorial", "Tutorial — Chapter 4"),
        ("lab", "Lab — Chapter 4"),
        ("studying_block", "Studying Block — Chapter 4"),
    ],
)
@pytest.mark.asyncio
async def test_each_clarification_type_is_one_guarded_title_only_rename(
    action: DiscordClarificationAction,
    expected_title: str,
) -> None:
    store = _Store()
    clarification_id = store.create_or_get_clarification(
        event_notion_id=f"event-{action}",
        original_title="Chapter 4",
        quiz_preview_title="Quiz — Chapter 4",
        assignment_preview_title="Assignment — Chapter 4",
        expected_edited_at=NOW,
        expires_at=NOW,
        idempotency_key=f"one-{action}",
        title_property_id="title-prop",
    )
    connector = _Connector(NotionDiscoveryResult(courses_database_id="courses-db", synced_at=NOW))
    service = AcademicClarificationService(
        store=store,
        connector=cast(NotionConnector, connector),
    )

    first = await service.apply_choice(
        clarification_id=clarification_id,
        action=action,
        user_id=USER,
    )
    replay = await service.apply_choice(
        clarification_id=clarification_id,
        action=action,
        user_id=USER,
    )

    assert first.status == "handled"
    assert replay.status == "duplicate"
    assert connector.renames == [
        {
            "page_id": f"event-{action}",
            "title_property_id": "title-prop",
            "expected_title": "Chapter 4",
            "expected_last_edited_at": NOW,
            "new_title": expected_title,
        }
    ]
    assert store.clarifications[clarification_id]["state"] == "applied"


@pytest.mark.asyncio
async def test_transient_choice_failure_retries_until_final_attempt() -> None:
    store = _Store()
    clarification_id = store.create_or_get_clarification(
        event_notion_id="event-retry",
        original_title="Chapter 6",
        quiz_preview_title="Quiz — Chapter 6",
        assignment_preview_title="Assignment — Chapter 6",
        expected_edited_at=NOW,
        expires_at=NOW,
        idempotency_key="retry",
        title_property_id="title-prop",
    )
    connector = _Connector(NotionDiscoveryResult(courses_database_id="courses-db", synced_at=NOW))
    connector.transient_failures = 1
    service = AcademicClarificationService(
        store=store,
        connector=cast(NotionConnector, connector),
    )

    with pytest.raises(LifeAgentError, match="connector_transient"):
        await service.apply_choice(
            clarification_id=clarification_id,
            action="quiz",
            user_id=USER,
            attempt=1,
            attempt_limit=2,
        )

    assert store.clarifications[clarification_id]["state"] == "claimed"

    result = await service.apply_choice(
        clarification_id=clarification_id,
        action="quiz",
        user_id=USER,
        attempt=2,
        attempt_limit=2,
    )

    assert result.status == "handled"
    assert store.clarifications[clarification_id]["state"] == "applied"
    assert len(connector.renames) == 2


@pytest.mark.asyncio
async def test_final_transient_choice_failure_marks_clarification_failed() -> None:
    store = _Store()
    clarification_id = store.create_or_get_clarification(
        event_notion_id="event-final-failure",
        original_title="Chapter 7",
        quiz_preview_title="Quiz — Chapter 7",
        assignment_preview_title="Assignment — Chapter 7",
        expected_edited_at=NOW,
        expires_at=NOW,
        idempotency_key="final-failure",
        title_property_id="title-prop",
    )
    connector = _Connector(NotionDiscoveryResult(courses_database_id="courses-db", synced_at=NOW))
    connector.transient_failures = 1
    service = AcademicClarificationService(
        store=store,
        connector=cast(NotionConnector, connector),
    )

    result = await service.apply_choice(
        clarification_id=clarification_id,
        action="assignment",
        user_id=USER,
        attempt=2,
        attempt_limit=2,
    )

    assert result.status == "failed"
    assert store.clarifications[clarification_id]["state"] == "failed"


@pytest.mark.asyncio
async def test_retry_conflict_with_intended_title_is_idempotent_success() -> None:
    store = _Store()
    clarification_id = store.create_or_get_clarification(
        event_notion_id="event-unknown-result",
        original_title="Chapter 10",
        quiz_preview_title="Quiz — Chapter 10",
        assignment_preview_title="Assignment — Chapter 10",
        expected_edited_at=NOW,
        expires_at=NOW,
        idempotency_key="unknown-result",
        title_property_id="title-prop",
    )
    connector = _Connector(NotionDiscoveryResult(courses_database_id="courses-db", synced_at=NOW))
    connector.conflict = True
    connector.conflict_current_title = "Quiz — Chapter 10"
    service = AcademicClarificationService(
        store=store,
        connector=cast(NotionConnector, connector),
    )

    result = await service.apply_choice(
        clarification_id=clarification_id,
        action="quiz",
        user_id=USER,
        attempt=2,
        attempt_limit=2,
    )

    assert result.status == "handled"
    assert store.clarifications[clarification_id]["state"] == "applied"
    assert len(connector.renames) == 1


@pytest.mark.asyncio
async def test_clarification_job_applies_choice_and_enqueues_status_edit() -> None:
    store = _Store()
    clarification_id = store.create_or_get_clarification(
        event_notion_id="event-job",
        original_title="Chapter 8",
        quiz_preview_title="Quiz — Chapter 8",
        assignment_preview_title="Assignment — Chapter 8",
        expected_edited_at=NOW,
        expires_at=NOW,
        idempotency_key="job",
        title_property_id="title-prop",
    )
    store.mark_clarification_delivered(
        clarification_id,
        delivery_id="555556666677777",
        delivered_at=NOW,
    )
    connector = _Connector(NotionDiscoveryResult(courses_database_id="courses-db", synced_at=NOW))
    deferred: list[tuple[str, str]] = []

    async def defer_status(queued_id: str, queued_action: str) -> None:
        deferred.append((queued_id, queued_action))

    job = AcademicClarificationJob(
        store=store,
        connector=cast(NotionConnector, connector),
        status_deferrer=defer_status,
    )

    result = await job(clarification_id, "assignment", USER, 1, 2)

    assert result["status"] == "applied"
    assert store.clarifications[clarification_id]["state"] == "applied"
    assert deferred == [(clarification_id, "assignment")]
    assert len(connector.renames) == 1


@pytest.mark.asyncio
async def test_status_job_has_independent_retry_budget_after_notion_success() -> None:
    store = _Store()
    clarification_id = store.create_or_get_clarification(
        event_notion_id="event-status",
        original_title="Chapter 9",
        quiz_preview_title="Quiz — Chapter 9",
        assignment_preview_title="Assignment — Chapter 9",
        expected_edited_at=NOW,
        expires_at=NOW,
        idempotency_key="status",
        title_property_id="title-prop",
    )
    store.mark_clarification_delivered(
        clarification_id,
        delivery_id="666667777788888",
        delivered_at=NOW,
    )
    connector = _Connector(NotionDiscoveryResult(courses_database_id="courses-db", synced_at=NOW))
    deferred: list[tuple[str, str]] = []

    async def defer_status(queued_id: str, queued_action: str) -> None:
        deferred.append((queued_id, queued_action))

    apply_job = AcademicClarificationJob(
        store=store,
        connector=cast(NotionConnector, connector),
        status_deferrer=defer_status,
    )
    await apply_job(clarification_id, "quiz", USER, 2, 2)
    adapter = _StatusAdapter()
    adapter.transient_failures = 1
    status_job = AcademicClarificationStatusJob(
        store=store,
        adapter=adapter,
        channel_id=CHANNEL,
    )

    with pytest.raises(LifeAgentError, match="connector_transient"):
        await status_job(clarification_id, "quiz", 1, 3)

    result = await status_job(clarification_id, "quiz", 2, 3)

    assert result["status"] == "edited"
    assert store.clarifications[clarification_id]["state"] == "applied"
    assert deferred == [(clarification_id, "quiz")]
    assert len(connector.renames) == 1
    assert adapter.edits == [
        {
            "channel_id": CHANNEL,
            "message_id": "666667777788888",
            "content": "Confirmed choice: Quiz. The Notion assessment was updated once.",
        }
    ]


@pytest.mark.asyncio
async def test_per_clarification_runtime_lock_prevents_duplicate_claimed_rename() -> None:
    store = _Store()
    clarification_id = store.create_or_get_clarification(
        event_notion_id="event-locked",
        original_title="Chapter 11",
        quiz_preview_title="Quiz — Chapter 11",
        assignment_preview_title="Assignment — Chapter 11",
        expected_edited_at=NOW,
        expires_at=NOW,
        idempotency_key="locked",
        title_property_id="title-prop",
    )
    store.mark_clarification_delivered(
        clarification_id,
        delivery_id="777778888899999",
        delivered_at=NOW,
    )
    connector = _BlockingConnector(
        NotionDiscoveryResult(courses_database_id="courses-db", synced_at=NOW)
    )
    deferred: list[tuple[str, str]] = []

    async def defer_status(queued_id: str, queued_action: str) -> None:
        deferred.append((queued_id, queued_action))

    job = AcademicClarificationJob(
        store=store,
        connector=cast(NotionConnector, connector),
        status_deferrer=defer_status,
    )
    runtime_lock = asyncio.Lock()

    async def run_locked_job() -> dict[str, Any]:
        async with runtime_lock:
            return await job(clarification_id, "quiz", USER, 1, 2)

    first = asyncio.create_task(run_locked_job())
    await connector.started.wait()
    second = asyncio.create_task(run_locked_job())
    await asyncio.sleep(0)

    assert len(connector.renames) == 1

    connector.release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result["status"] == "applied"
    assert second_result["status"] == "applied"
    assert store.clarifications[clarification_id]["state"] == "applied"
    assert len(connector.renames) == 1
    assert deferred == [(clarification_id, "quiz"), (clarification_id, "quiz")]


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
