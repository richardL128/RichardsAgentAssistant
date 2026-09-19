from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.agents.academic_planner.morning_notification import (
    build_scheduled_morning_notification,
    execute_scheduled_morning_notification,
    scheduled_delivery_key,
)
from app.agents.academic_planner.sync import AcademicNotionSync, AcademicNotionSyncResult
from app.agents.calendar_briefing import (
    CalendarActivityIntent,
    CalendarActivityIntentStatus,
    CalendarEventEvidenceFragment,
    CalendarEventSemanticInterpreter,
    CalendarEventSemanticResult,
    CalendarEventSemanticStatus,
    CalendarEventSourceKind,
    ScheduledMorningCalendarItem,
)
from app.agents.calendar_briefing.semantic_interpreter import CalendarEventSemanticCritique
from app.agents.job_interviews.contracts import (
    InterviewEventSnapshot,
    InterviewReminderFact,
    PreparationPlanSnapshot,
)
from app.core.errors import (
    ErrorCode,
    LifeAgentError,
    authorization_error,
    transient_error,
)
from app.queue.periodic import PeriodicOccurrence, stable_period_key

OCCURRENCE = PeriodicOccurrence(
    local_time=datetime.fromisoformat("2026-09-10T08:00:00-04:00"),
    scheduled_at=datetime(2026, 9, 10, 12, tzinfo=UTC),
)
PERIOD_KEY = stable_period_key("academic-morning", OCCURRENCE)


class Store:
    def __init__(self, calendar_items: tuple[dict[str, object], ...] = ()) -> None:
        self.calendar_items = calendar_items
        self.loaded_at: list[datetime] = []

    def load_upcoming_calendar_items(self, *, occurrence: datetime, timezone: str):
        assert timezone == "America/Toronto"
        self.loaded_at.append(occurrence)
        return self.calendar_items


class Syncer:
    def __init__(self, result: AcademicNotionSyncResult) -> None:
        self.result = result
        self.calls: list[datetime | None] = []

    async def sync(self, *, now: datetime | None = None) -> AcademicNotionSyncResult:
        self.calls.append(now)
        return self.result


class Delivery:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def send_scheduled_notification(
        self,
        content: str,
        *,
        idempotency_key: str,
    ) -> object:
        self.messages.append((content, idempotency_key))
        return SimpleNamespace(status="sent")


class CareerStore:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, datetime]] = []

    def load_upcoming_interviews(self, *, now: datetime):
        assert now == OCCURRENCE.scheduled_at
        return (
            InterviewEventSnapshot(
                interview_page_id="interview-1",
                title="Shopify Technical Interview",
                local_date=date(2026, 9, 17),
                is_all_day=True,
                last_edited_at=OCCURRENCE.scheduled_at,
                content_fingerprint="interview-hash",
            ),
        )

    def load_upcoming_calendar_items(self, *, occurrence: datetime, timezone: str):
        assert occurrence == OCCURRENCE.scheduled_at
        assert timezone == "America/Toronto"
        return (
            {
                "event_id": "interview-1",
                "source_area": "jobs",
                "source_label": "Jobs/Interviews",
                "title": "Shopify Technical Interview",
                "display_kind": "Interview",
                "local_start_label": "Thursday, September 17, 2026",
                "relative_date_label": "In 7 days",
                "is_all_day": True,
                "completed": False,
                "semantic_status": "unavailable",
                "source_last_edited_at": OCCURRENCE.scheduled_at,
                "semantic_cache": {},
            },
        )

    def get_current_plan(self, interview_page_id: str):
        assert interview_page_id == "interview-1"
        return PreparationPlanSnapshot(
            interview_page_id=interview_page_id,
            revision=2,
            generated_at=OCCURRENCE.scheduled_at,
            plan_hash="plan-hash",
            summary="Grounded preparation plan",
            next_actions=("Practice the verified API-design requirement.",),
        )

    def record_reminder_delivery(
        self,
        reminder: InterviewReminderFact,
        *,
        status: str,
        included_at: datetime,
        delivery_id: object = None,
    ) -> None:
        del delivery_id
        self.records.append((reminder.interview_page_id, status, included_at))


class CareerSyncer:
    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails

    async def sync(self, *, now: datetime | None = None):
        if self.fails:
            raise RuntimeError("career source unavailable")
        return SimpleNamespace(status="succeeded")


class ReadyRuntime:
    async def ensure_ready(self):
        return SimpleNamespace(model="qwen-test")


class ProgressRecorder:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, int, str]] = []

    def record(
        self,
        phase: str,
        status: str,
        *,
        attempt: int,
        diagnostic: str,
    ) -> None:
        self.records.append((phase, status, attempt, diagnostic))


class SemanticGateway:
    model_identity = "qwen-test"
    config_version = "cfg-test"

    def __init__(self, outputs: list[object]) -> None:
        self.outputs = outputs

    async def invoke_structured(self, *, prompt: str, response_model: type[object]):
        del prompt, response_model
        return SimpleNamespace(output=self.outputs.pop(0))


class SemanticStore(Store):
    def __init__(
        self,
        *,
        many: int = 1,
        source_area: str = "course",
        source_label: str = "ECE 250",
        display_kind: str = "Quiz",
        title_prefix: str = "Graph traversal event",
    ) -> None:
        super().__init__()
        self.many = many
        self.source_area = source_area
        self.source_label = source_label
        self.display_kind = display_kind
        self.title_prefix = title_prefix
        self.semantic_saves: list[tuple[str, object]] = []

    def load_upcoming_calendar_items(self, *, occurrence: datetime, timezone: str):
        assert occurrence == OCCURRENCE.scheduled_at
        assert timezone == "America/Toronto"
        return tuple(
            {
                "event_id": f"event-{index}",
                "source_area": self.source_area,
                "source_label": self.source_label,
                "title": f"{self.title_prefix} {index} " + "x" * 80,
                "display_kind": self.display_kind,
                "local_start_label": "Friday, September 11, 2026 at 10:00 EDT",
                "relative_date_label": "Tomorrow",
                "is_all_day": False,
                "completed": False,
                "semantic_status": "unavailable",
                "source_last_edited_at": OCCURRENCE.scheduled_at,
                "semantic_cache": {},
            }
            for index in range(self.many)
        )

    def save_assessment_calendar_semantics(self, event_id: str, semantics: object) -> bool:
        self.semantic_saves.append((event_id, semantics))
        return True


class EvidenceConnector:
    async def retrieve_calendar_event_evidence(self, page_id: str):
        fragment = CalendarEventEvidenceFragment(
            fragment_id=f"{page_id}:property:topics",
            event_id=page_id,
            source_kind=CalendarEventSourceKind.PROPERTY,
            source_label="Unexpected syllabus wording",
            text="Prepare BFS, DFS, and runtime analysis.",
            ordinal=0,
        )
        return SimpleNamespace(
            event_id=page_id,
            last_edited_at=OCCURRENCE.scheduled_at,
            fragments=(fragment,),
        )


class MemoryManifestStore:
    def __init__(self) -> None:
        self.value = None

    def load(self, *, period_key: str):
        del period_key
        return self.value

    def save(self, manifest, *, period_key: str, delivered_ordinals: set[int]) -> None:
        del period_key
        self.value = (manifest, set(delivered_ordinals))


class PartialFailureDelivery(Delivery):
    def __init__(self) -> None:
        super().__init__()
        self.failed_once = False
        self.delivered_keys: list[str] = []

    async def send_scheduled_notification(self, content: str, *, idempotency_key: str):
        ordinal = int(idempotency_key.rsplit(":", 1)[-1])
        if ordinal == 2 and not self.failed_once:
            self.failed_once = True
            raise RuntimeError("forced part-two failure")
        self.messages.append((content, idempotency_key))
        self.delivered_keys.append(idempotency_key)
        return SimpleNamespace(status="sent")


class FailureSyncStore:
    def __init__(self) -> None:
        self.reminders: list[tuple[str, str | None]] = []

    def expire_clarifications(self, *, now: datetime | None = None) -> int:
        return 0

    def clear_setup_reminders(
        self,
        condition_code: str | None = None,
        fingerprint: str | None = None,
    ) -> int:
        return 0

    def setup_reminder_due(self, condition_code: str, fingerprint: str, day: object) -> bool:
        return True

    def record_setup_reminder(
        self,
        condition_code: str,
        fingerprint: str,
        day: object,
        *,
        affected_course_codes: object = (),
        delivered_at: datetime | None = None,
        delivery_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        self.reminders.append((condition_code, error_code))


class FailingConnector:
    def __init__(self, error: LifeAgentError) -> None:
        self.error = error

    async def discover_course_assessments(self) -> object:
        raise self.error


def test_formatter_renders_neutral_calendar_agenda_without_study_blocks() -> None:
    item = ScheduledMorningCalendarItem(
        event_id="quiz-1",
        source_area="course",
        source_label="ECE 250",
        title="Graph Traversal Quiz",
        display_kind="Quiz",
        local_start_label="Friday, September 11, 2026 at 10:00 EDT",
        relative_date_label="Tomorrow",
    )
    notification = build_scheduled_morning_notification(
        period_key=PERIOD_KEY,
        occurrence=OCCURRENCE,
        source_synced_at=OCCURRENCE.scheduled_at,
        timezone_name="America/Toronto",
        course_calendar_items=(item,),
    )

    assert notification.intended_local_date.isoformat() == "2026-09-10"
    assert "Thursday, September 10, 2026" in notification.message_text
    assert "Upcoming course dates" in notification.message_text
    assert "Tomorrow — ECE 250 — Graph Traversal Quiz" in notification.message_text
    assert "Quiz — Graph Traversal Quiz" not in notification.message_text
    assert "study blocks" not in notification.message_text
    assert "StudyBlock" not in notification.message_text
    assert "practice)" not in notification.message_text


async def test_successful_empty_refresh_sends_natural_light_day_message() -> None:
    store = Store()
    delivery = Delivery()
    syncer = Syncer(AcademicNotionSyncResult(status="succeeded", synced_at=OCCURRENCE.scheduled_at))

    result = await execute_scheduled_morning_notification(
        store=store,
        syncer=syncer,
        delivery=delivery,
        occurrence=OCCURRENCE,
        period_key=PERIOD_KEY,
        executed_at=OCCURRENCE.scheduled_at,
    )

    assert result["status"] == "succeeded"
    assert store.loaded_at == [OCCURRENCE.scheduled_at]
    assert "Today's calendar" in delivery.messages[0][0]
    assert "No course calendar dates in this window." in delivery.messages[0][0]
    assert "study blocks" not in delivery.messages[0][0]
    assert delivery.messages[0][1] == f"{scheduled_delivery_key(PERIOD_KEY, OCCURRENCE)}:001"


async def test_combined_morning_delivers_academic_and_interview_once_with_audit() -> None:
    career_store = CareerStore()
    delivery = Delivery()
    result = await execute_scheduled_morning_notification(
        store=Store(),
        syncer=Syncer(
            AcademicNotionSyncResult(status="succeeded", synced_at=OCCURRENCE.scheduled_at)
        ),
        delivery=delivery,
        occurrence=OCCURRENCE,
        period_key=PERIOD_KEY,
        executed_at=OCCURRENCE.scheduled_at,
        career_store=career_store,
        career_syncer=CareerSyncer(),
    )

    assert len(delivery.messages) == 1
    message = delivery.messages[0][0]
    assert "Today's calendar" in message
    assert "study blocks" not in message
    assert "**INTERVIEW IN 7 DAYS**" in message
    assert "Thursday, September 17, 2026" in message
    assert "Practice the verified API-design requirement." in message
    assert result["interview_count"] == 1
    assert result["reminder_audit_status"] == "recorded"
    assert career_store.records == [("interview-1", "sent", OCCURRENCE.scheduled_at)]


async def test_career_failure_keeps_academic_morning_delivery_honest() -> None:
    delivery = Delivery()
    result = await execute_scheduled_morning_notification(
        store=Store(),
        syncer=Syncer(
            AcademicNotionSyncResult(status="succeeded", synced_at=OCCURRENCE.scheduled_at)
        ),
        delivery=delivery,
        occurrence=OCCURRENCE,
        period_key=PERIOD_KEY,
        executed_at=OCCURRENCE.scheduled_at,
        career_store=CareerStore(),
        career_syncer=CareerSyncer(fails=True),
    )

    assert result["status"] == "succeeded"
    assert result["career_sync_status"] == "failed"
    assert "Today's calendar" in delivery.messages[0][0]
    assert "study blocks" not in delivery.messages[0][0]
    assert "academic calendar is unaffected" in delivery.messages[0][0]


async def test_scheduled_path_renders_and_persists_critic_approved_semantics() -> None:
    store = SemanticStore()
    delivery = Delivery()
    fragment_id = "event-0:property:topics"
    title_id = "event-0:host:title"
    candidate = CalendarEventSemanticResult(
        event_id="event-0",
        overview="A quiz focused on graph traversal techniques.",
        description_present=True,
        description="Prepare BFS, DFS, and runtime analysis.",
        evidence_fragment_ids=(title_id, fragment_id),
        description_fragment_ids=(fragment_id,),
        classification_rationale="The supplied topics contain substantive preparation details.",
        activity_intent=CalendarActivityIntent.STUDY,
        intent_status=CalendarActivityIntentStatus.VALID,
        intent_evidence_fragment_ids=(title_id, fragment_id),
        intent_rationale="The event title and preparation topics support study.",
    )
    critique = CalendarEventSemanticCritique(
        accepted=True,
        intent_supported=True,
        overview_supported=True,
        description_supported=True,
        no_invented_claims=True,
        no_instruction_following=True,
        same_event=True,
        cites_only_supplied_fragments=True,
    )
    interpreter = CalendarEventSemanticInterpreter(SemanticGateway([candidate, critique]))

    result = await execute_scheduled_morning_notification(
        store=store,
        syncer=Syncer(
            AcademicNotionSyncResult(status="succeeded", synced_at=OCCURRENCE.scheduled_at)
        ),
        delivery=delivery,
        occurrence=OCCURRENCE,
        period_key=PERIOD_KEY,
        executed_at=OCCURRENCE.scheduled_at,
        evidence_connector=EvidenceConnector(),
        semantic_interpreter=interpreter,
        ollama_runtime=ReadyRuntime(),
    )

    assert result["semantic_calls"] == 1
    assert result["valid_descriptions"] == 1
    assert "Overview: A quiz focused on graph traversal techniques." in delivery.messages[0][0]
    assert "Description: Prepare BFS, DFS, and runtime analysis." in delivery.messages[0][0]
    assert len(store.semantic_saves) == 1
    saved = store.semantic_saves[0][1]
    assert saved.intent_value == CalendarActivityIntent.STUDY.value
    assert saved.intent_status == CalendarActivityIntentStatus.VALID.value
    assert saved.intent_evidence_ids == (title_id, fragment_id)


async def test_semantic_diagnostics_are_per_event_bounded_and_non_sensitive() -> None:
    store = SemanticStore(many=2)
    progress = ProgressRecorder()
    interpreter = CalendarEventSemanticInterpreter(SemanticGateway([None, None]))

    result = await execute_scheduled_morning_notification(
        store=store,
        syncer=Syncer(
            AcademicNotionSyncResult(status="succeeded", synced_at=OCCURRENCE.scheduled_at)
        ),
        delivery=Delivery(),
        occurrence=OCCURRENCE,
        period_key=PERIOD_KEY,
        executed_at=OCCURRENCE.scheduled_at,
        evidence_connector=EvidenceConnector(),
        semantic_interpreter=interpreter,
        ollama_runtime=ReadyRuntime(),
        progress=progress,  # type: ignore[arg-type]
    )

    semantic_records = [
        record for record in progress.records if record[0].endswith("semantic_interpretation")
    ]
    assert result["unavailable_semantics"] == 2
    assert [record[0] for record in semantic_records] == [
        "event_001.semantic_interpretation",
        "event_001.semantic_interpretation",
        "event_002.semantic_interpretation",
        "event_002.semantic_interpretation",
    ]
    assert [record[1] for record in semantic_records] == [
        "running",
        "failed",
        "running",
        "failed",
    ]
    assert semantic_records[1][3] == (
        "semantic_status:unavailable;error_code:calendar_semantic_model_unavailable"
    )
    assert semantic_records[3][3] == semantic_records[1][3]
    serialized = repr(progress.records)
    assert "Graph traversal event" not in serialized
    assert "Prepare BFS" not in serialized
    assert "event-0" not in serialized
    assert "event-1" not in serialized


async def test_misc_calendar_items_render_separately_and_persist_academic_semantics() -> None:
    store = SemanticStore(
        source_area="misc",
        source_label="misc",
        display_kind="Task",
        title_prefix="Task — scrub the toilets",
    )
    delivery = Delivery()
    fragment_id = "event-0:property:topics"
    title_id = "event-0:host:title"
    candidate = CalendarEventSemanticResult(
        event_id="event-0",
        overview="A household task scheduled for the upcoming window.",
        description_present=True,
        description="Clean the bathrooms before the evening.",
        evidence_fragment_ids=(title_id, fragment_id),
        description_fragment_ids=(fragment_id,),
        classification_rationale="The supplied task details contain a substantive description.",
        activity_intent=CalendarActivityIntent.REGULAR,
        intent_status=CalendarActivityIntentStatus.VALID,
        intent_evidence_fragment_ids=(title_id, fragment_id),
        intent_rationale="The task title and details support a regular non-study activity.",
    )
    critique = CalendarEventSemanticCritique(
        accepted=True,
        intent_supported=True,
        overview_supported=True,
        description_supported=True,
        no_invented_claims=True,
        no_instruction_following=True,
        same_event=True,
        cites_only_supplied_fragments=True,
    )
    interpreter = CalendarEventSemanticInterpreter(SemanticGateway([candidate, critique]))

    result = await execute_scheduled_morning_notification(
        store=store,
        syncer=Syncer(
            AcademicNotionSyncResult(status="succeeded", synced_at=OCCURRENCE.scheduled_at)
        ),
        delivery=delivery,
        occurrence=OCCURRENCE,
        period_key=PERIOD_KEY,
        executed_at=OCCURRENCE.scheduled_at,
        evidence_connector=EvidenceConnector(),
        semantic_interpreter=interpreter,
        ollama_runtime=ReadyRuntime(),
    )

    message = delivery.messages[0][0]
    assert result["misc_event_count"] == 1
    assert result["academic_event_count"] == 0
    assert result["valid_descriptions"] == 1
    assert "Upcoming course dates" in message
    assert "- No course calendar dates in this window." in message
    assert "Upcoming miscellaneous tasks:" in message
    assert "- Tomorrow — Task — scrub the toilets 0 " in message
    assert "Overview: A household task scheduled for the upcoming window." in message
    assert len(store.semantic_saves) == 1
    saved = store.semantic_saves[0][1]
    assert saved.status == CalendarEventSemanticStatus.VALID.value
    assert saved.intent_value == CalendarActivityIntent.REGULAR.value
    assert saved.intent_status == CalendarActivityIntentStatus.VALID.value
    assert saved.intent_evidence_ids == (title_id, fragment_id)


async def test_multipart_retry_resumes_persisted_manifest_without_duplicate_part_one() -> None:
    store = SemanticStore(many=40)
    syncer = Syncer(AcademicNotionSyncResult(status="succeeded", synced_at=OCCURRENCE.scheduled_at))
    delivery = PartialFailureDelivery()
    manifest_store = MemoryManifestStore()

    with pytest.raises(RuntimeError, match="forced part-two failure"):
        await execute_scheduled_morning_notification(
            store=store,
            syncer=syncer,
            delivery=delivery,
            occurrence=OCCURRENCE,
            period_key=PERIOD_KEY,
            executed_at=OCCURRENCE.scheduled_at,
            manifest_store=manifest_store,
        )

    resumed = await execute_scheduled_morning_notification(
        store=store,
        syncer=syncer,
        delivery=delivery,
        occurrence=OCCURRENCE,
        period_key=PERIOD_KEY,
        executed_at=OCCURRENCE.scheduled_at,
        manifest_store=manifest_store,
    )

    assert resumed["resumed_manifest"] is True
    assert resumed["part_count"] >= 3
    assert syncer.calls == [OCCURRENCE.scheduled_at]
    part_one_key = f"{scheduled_delivery_key(PERIOD_KEY, OCCURRENCE)}:001"
    assert delivery.delivered_keys.count(part_one_key) == 1
    assert all(len(content) <= 2_000 for content, _key in delivery.messages)
    combined = "\n".join(content for content, _key in delivery.messages)
    for index in range(40):
        assert combined.count(f"Graph traversal event {index} ") == 1


async def test_loaded_calendar_items_are_authoritative_for_populated_notification() -> None:
    store = Store(
        (
            {
                "event_id": "quiz-1",
                "source_area": "course",
                "source_label": "ECE 250",
                "title": "ECE 250 Quiz 1 review",
                "display_kind": "Quiz",
                "local_start_label": "Friday, September 11, 2026 at 16:00 EDT",
                "relative_date_label": "Tomorrow",
                "is_all_day": False,
                "completed": False,
                "semantic_status": "unavailable",
                "source_last_edited_at": OCCURRENCE.scheduled_at,
                "semantic_cache": {},
            },
            {
                "event_id": "assignment-2",
                "source_area": "course",
                "source_label": "MATH 239",
                "title": "MATH 239 Assignment 2",
                "display_kind": "Assignment",
                "local_start_label": "Saturday, September 12, 2026 at 16:00 EDT",
                "relative_date_label": "In 2 days",
                "is_all_day": False,
                "completed": False,
                "semantic_status": "unavailable",
                "source_last_edited_at": OCCURRENCE.scheduled_at,
                "semantic_cache": {},
            },
        )
    )
    delivery = Delivery()

    result = await execute_scheduled_morning_notification(
        store=store,
        syncer=Syncer(
            AcademicNotionSyncResult(status="succeeded", synced_at=OCCURRENCE.scheduled_at)
        ),
        delivery=delivery,
        occurrence=OCCURRENCE,
        period_key=PERIOD_KEY,
        executed_at=OCCURRENCE.scheduled_at,
    )

    assert result["status"] == "succeeded"
    message = delivery.messages[0][0]
    assert "Tomorrow — ECE 250 — ECE 250 Quiz 1 review" in message
    assert "In 2 days — MATH 239 — MATH 239 Assignment 2" in message
    assert "Quiz — ECE 250 Quiz 1 review" not in message
    assert "Assignment — MATH 239 Assignment 2" not in message
    assert "ECE 250 Quiz 1 review" in message
    assert "study blocks" not in message


@pytest.mark.parametrize(
    ("sync_result", "expected_code", "expected_status"),
    [
        (
            AcademicNotionSyncResult(
                status="setup_required",
                diagnostic_codes=("notion_configuration_missing",),
                error_code=ErrorCode.SOURCE_SETUP_REQUIRED.value,
            ),
            ErrorCode.SOURCE_SETUP_REQUIRED,
            "attention",
        ),
        (
            AcademicNotionSyncResult(
                status="partial",
                diagnostic_codes=("assessment_calendar_missing",),
                error_code=ErrorCode.SOURCE_SYNC_PARTIAL.value,
            ),
            ErrorCode.SOURCE_SYNC_PARTIAL,
            "attention",
        ),
        (
            AcademicNotionSyncResult(
                status="succeeded",
                synced_at=OCCURRENCE.scheduled_at - timedelta(minutes=6),
            ),
            ErrorCode.SOURCE_STALE,
            "attention",
        ),
    ],
)
async def test_untrusted_source_states_never_send_light_day(
    sync_result: AcademicNotionSyncResult,
    expected_code: ErrorCode,
    expected_status: str,
) -> None:
    store = Store()
    delivery = Delivery()

    result = await execute_scheduled_morning_notification(
        store=store,
        syncer=Syncer(sync_result),
        delivery=delivery,
        occurrence=OCCURRENCE,
        period_key=PERIOD_KEY,
        executed_at=OCCURRENCE.scheduled_at,
    )

    assert result["status"] == expected_status
    assert result["error_code"] == expected_code.value
    assert store.loaded_at == []
    assert len(delivery.messages) == 1
    assert "no scheduled study blocks" not in delivery.messages[0][0]
    assert expected_code.value in delivery.messages[0][1]


async def test_transient_source_failure_retries_then_surfaces_final_failure() -> None:
    result = AcademicNotionSyncResult(
        status="failed",
        diagnostic_codes=("notion_sync_failed",),
        error_code=ErrorCode.CONNECTOR_TRANSIENT.value,
        retryable=True,
    )
    delivery = Delivery()

    with pytest.raises(LifeAgentError, match=ErrorCode.CONNECTOR_TRANSIENT.value):
        await execute_scheduled_morning_notification(
            store=Store(),
            syncer=Syncer(result),
            delivery=delivery,
            occurrence=OCCURRENCE,
            period_key=PERIOD_KEY,
            executed_at=OCCURRENCE.scheduled_at,
            attempt=1,
            attempt_limit=2,
        )
    assert delivery.messages == []

    final = await execute_scheduled_morning_notification(
        store=Store(),
        syncer=Syncer(result),
        delivery=delivery,
        occurrence=OCCURRENCE,
        period_key=PERIOD_KEY,
        executed_at=OCCURRENCE.scheduled_at,
        attempt=2,
        attempt_limit=2,
    )
    assert final["status"] == "failed"
    assert len(delivery.messages) == 1


async def test_retry_after_grace_does_not_send_stale_morning_greeting() -> None:
    delivery = Delivery()
    store = Store()

    result = await execute_scheduled_morning_notification(
        store=store,
        syncer=Syncer(
            AcademicNotionSyncResult(status="succeeded", synced_at=OCCURRENCE.scheduled_at)
        ),
        delivery=delivery,
        occurrence=OCCURRENCE,
        period_key=PERIOD_KEY,
        executed_at=OCCURRENCE.scheduled_at + timedelta(minutes=31),
        catchup_grace_minutes=30,
    )

    assert result["status"] == "attention"
    assert result["error_code"] == ErrorCode.SCHEDULE_LATE.value
    assert store.loaded_at == []
    assert "no stale morning calendar was sent" in delivery.messages[0][0]


def test_period_key_must_match_explicit_occurrence() -> None:
    with pytest.raises(ValueError, match="period key"):
        scheduled_delivery_key("academic-morning:2026-09-11:0800:v1", OCCURRENCE)


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code", "retryable"),
    [
        (
            transient_error(ErrorCode.CONNECTOR_TRANSIENT, "temporary Notion timeout"),
            "failed",
            ErrorCode.CONNECTOR_TRANSIENT.value,
            True,
        ),
        (
            authorization_error("Notion token rejected"),
            "setup_required",
            ErrorCode.AUTHORIZATION_INVALID.value,
            False,
        ),
    ],
)
async def test_notion_sync_preserves_retry_classification_without_exposing_raw_errors(
    error: LifeAgentError,
    expected_status: str,
    expected_code: str,
    retryable: bool,
) -> None:
    store = FailureSyncStore()
    syncer = AcademicNotionSync(
        connector=FailingConnector(error),  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
    )

    result = await syncer.sync(now=OCCURRENCE.scheduled_at)

    assert result.status == expected_status
    assert result.error_code == expected_code
    assert result.retryable is retryable
    assert result.diagnostic_codes == ("notion_sync_failed",)
    assert error.record.diagnostic not in repr(result)
