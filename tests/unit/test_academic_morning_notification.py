from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Literal
from uuid import UUID

import pytest

from app.agents.academic_planner.contracts import (
    Assessment,
    AssessmentType,
    AvailabilityWindow,
    DailyPlan,
    IncompleteBlock,
    PlannerFacts,
    PracticeNeed,
    StudyBlock,
)
from app.agents.academic_planner.morning_notification import (
    build_scheduled_morning_notification,
    execute_scheduled_morning_notification,
    scheduled_delivery_key,
)
from app.agents.academic_planner.sync import AcademicNotionSync, AcademicNotionSyncResult
from app.agents.calendar_briefing import (
    CalendarEventEvidenceFragment,
    CalendarEventSemanticInterpreter,
    CalendarEventSemanticResult,
    CalendarEventSourceKind,
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
    def __init__(self, facts: PlannerFacts | None = None) -> None:
        self.facts = facts or PlannerFacts(horizon_days=14)
        self.saved: list[DailyPlan] = []
        self.loaded_at: list[datetime] = []

    def load_planner_facts(self, *, now: datetime, horizon_days: int) -> PlannerFacts:
        self.loaded_at.append(now)
        return self.facts.model_copy(update={"horizon_days": horizon_days})

    def save_daily_plan(self, plan: DailyPlan) -> None:
        self.saved.append(plan)


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


class SemanticGateway:
    model_identity = "qwen-test"
    config_version = "cfg-test"

    def __init__(self, outputs: list[object]) -> None:
        self.outputs = outputs

    async def invoke_structured(self, *, prompt: str, response_model: type[object]):
        del prompt, response_model
        return SimpleNamespace(output=self.outputs.pop(0))


class SemanticStore(Store):
    def __init__(self, *, many: int = 1) -> None:
        super().__init__()
        self.many = many
        self.semantic_saves: list[tuple[str, object]] = []

    def load_upcoming_calendar_items(self, *, occurrence: datetime, timezone: str):
        assert occurrence == OCCURRENCE.scheduled_at
        assert timezone == "America/Toronto"
        return tuple(
            {
                "event_id": f"event-{index}",
                "source_area": "course",
                "source_label": "ECE 250",
                "title": f"Graph traversal event {index} " + "x" * 80,
                "display_kind": "Quiz",
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


def _block(
    block_id: str,
    title: str,
    start_at: datetime,
    minutes: int,
    *,
    kind: Literal["assessment", "practice"] = "assessment",
    carried: bool = False,
) -> StudyBlock:
    return StudyBlock(
        id=block_id,
        assessment_id=f"assessment-{block_id}",
        learning_focus_id="focus-1" if kind == "practice" else None,
        block_kind=kind,
        title=title,
        start_at=start_at,
        end_at=start_at + timedelta(minutes=minutes),
        carried_over=carried,
        priority_score=10,
        rationale="deterministic test allocation",
    )


def test_formatter_includes_only_intended_local_day_with_exact_allocator_facts() -> None:
    plan = DailyPlan(
        plan_id=UUID("38da2acc-f0f8-4eff-a16c-2bbc160624cf"),
        created_at=OCCURRENCE.scheduled_at,
        blocks=(
            _block("later", "Tomorrow task", datetime(2026, 9, 11, 13, tzinfo=UTC), 30),
            _block(
                "carry",
                "MATH 239 Assignment 2",
                datetime(2026, 9, 10, 18, tzinfo=UTC),
                60,
                carried=True,
            ),
            _block(
                "practice",
                "Practice ECE 250 Graph traversals",
                datetime(2026, 9, 10, 13, tzinfo=UTC),
                45,
                kind="practice",
            ),
        ),
    )

    notification = build_scheduled_morning_notification(
        plan,
        period_key=PERIOD_KEY,
        occurrence=OCCURRENCE,
        source_synced_at=OCCURRENCE.scheduled_at,
        timezone_name="America/Toronto",
    )

    assert notification.intended_local_date.isoformat() == "2026-09-10"
    assert [block.block_id for block in notification.blocks] == ["practice", "carry"]
    assert "Thursday, September 10, 2026" in notification.message_text
    assert "09:00 — Practice ECE 250 Graph traversals (45 minutes, practice)" in (
        notification.message_text
    )
    assert "14:00 — MATH 239 Assignment 2 (60 minutes, assessment, carried forward)" in (
        notification.message_text
    )
    assert "Tomorrow task" not in notification.message_text


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
    assert result["block_count"] == 0
    assert store.loaded_at == [OCCURRENCE.scheduled_at]
    assert len(store.saved) == 1
    assert "no scheduled study blocks today" in delivery.messages[0][0]
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
    assert "no scheduled study blocks today" in message
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
    assert "no scheduled study blocks today" in delivery.messages[0][0]
    assert "academic plan is unaffected" in delivery.messages[0][0]


async def test_scheduled_path_renders_and_persists_critic_approved_semantics() -> None:
    store = SemanticStore()
    delivery = Delivery()
    fragment_id = "event-0:property:topics"
    candidate = CalendarEventSemanticResult(
        event_id="event-0",
        overview="A quiz focused on graph traversal techniques.",
        description_present=True,
        description="Prepare BFS, DFS, and runtime analysis.",
        evidence_fragment_ids=(fragment_id,),
        description_fragment_ids=(fragment_id,),
    )
    critique = CalendarEventSemanticCritique(
        accepted=True,
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


async def test_allocator_output_is_authoritative_for_populated_notification() -> None:
    facts = PlannerFacts(
        assessments=(
            Assessment(
                id="quiz-1",
                course="ECE 250",
                title="ECE 250 Quiz 1 review",
                assessment_type=AssessmentType.QUIZ,
                due_at=datetime(2026, 9, 11, 20, tzinfo=UTC),
                estimated_minutes=45,
                weight_percent=10,
            ),
            Assessment(
                id="assignment-2",
                course="MATH 239",
                title="MATH 239 Assignment 2",
                assessment_type=AssessmentType.ASSIGNMENT,
                due_at=datetime(2026, 9, 12, 20, tzinfo=UTC),
                estimated_minutes=60,
                weight_percent=20,
            ),
        ),
        incomplete_blocks=(
            IncompleteBlock(
                id="old",
                assessment_id="assignment-2",
                title="MATH 239 Assignment 2",
                remaining_minutes=60,
            ),
        ),
        practice_needs=(
            PracticeNeed(
                focus_id="focus-1",
                course_code="ECE 250",
                assessment_id="quiz-1",
                topic="graph traversals",
                target_minutes=30,
                next_review_at=datetime(2026, 9, 11, 12, tzinfo=UTC),
                source_action="reinforce_focus",
                rationale="verified reflection focus",
            ),
        ),
        availability=(
            AvailabilityWindow(
                start_at=datetime(2026, 9, 10, 13, tzinfo=UTC),
                end_at=datetime(2026, 9, 10, 21, tzinfo=UTC),
            ),
        ),
        horizon_days=14,
    )
    store = Store(facts)
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
    assert result["block_count"] == 3
    message = delivery.messages[0][0]
    assert "09:00 — Practice ECE 250 graph traversals (30 minutes, practice)" in message
    assert "carried forward" in message
    assert "ECE 250 Quiz 1 review" in message


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
    assert "no stale morning plan was sent" in delivery.messages[0][0]


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
