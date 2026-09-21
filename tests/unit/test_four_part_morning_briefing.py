from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.agents.academic_planner.calendar_roles import AcademicCalendarRole
from app.agents.academic_planner.morning_notification import (
    execute_scheduled_morning_notification,
)
from app.agents.academic_planner.sync import AcademicNotionSyncResult
from app.agents.calendar_briefing.contracts import (
    ActiveMorningCourse,
    CalendarActivityIntentStatus,
    CalendarEventSemanticResult,
    CalendarEventSemanticStatus,
    ScheduledMorningCalendarItem,
)
from app.agents.calendar_briefing.morning_composer import (
    ScheduleComposition,
    ScheduleInference,
)
from app.agents.calendar_briefing.morning_manifest import (
    MORNING_CATEGORY_ORDER,
    MorningBriefingDeliveryManifest,
    build_morning_briefing_manifest,
)
from app.agents.calendar_briefing.semantic_interpreter import CalendarEventSemanticOutcome
from app.queue.periodic import PeriodicOccurrence, stable_period_key

ZONE = ZoneInfo("America/Toronto")
LOCAL_OCCURRENCE = datetime(2026, 9, 10, 8, 0, tzinfo=ZONE)
OCCURRENCE = PeriodicOccurrence(
    local_time=LOCAL_OCCURRENCE,
    scheduled_at=LOCAL_OCCURRENCE.astimezone(UTC),
)
PERIOD_KEY = stable_period_key("academic-morning", OCCURRENCE)


def _item(
    event_id: str,
    *,
    area: str = "course",
    source_id: str = "course-1",
    source_label: str = "ECE 250",
    title: str = "Quiz 1",
    hour: int = 9,
    url: str | None = "https://www.notion.so/workspace/page",
    context: str | None = None,
    semantic_status: CalendarEventSemanticStatus = CalendarEventSemanticStatus.UNAVAILABLE,
    semantic_overview: str | None = None,
    semantic_description: str | None = None,
) -> ScheduledMorningCalendarItem:
    starts_at = datetime(2026, 9, 10, hour, 0, tzinfo=ZONE).astimezone(UTC)
    return ScheduledMorningCalendarItem(
        event_id=event_id,
        source_id=source_id,
        source_area=area,
        source_label=source_label,
        title=title,
        display_kind="Event",
        local_start_label=f"Thursday, September 10, 2026 at {hour:02d}:00 EDT",
        relative_date_label="Today",
        starts_at=starts_at,
        source_url=url,
        schedule_context=context,
        semantic_status=semantic_status,
        semantic_overview=semantic_overview,
        semantic_description=semantic_description,
        semantic_evidence_fragment_ids=(
            (f"{event_id}:host:title",)
            if semantic_status is CalendarEventSemanticStatus.VALID
            else ()
        ),
        semantic_description_fragment_ids=(
            (f"{event_id}:body:1",) if semantic_description is not None else ()
        ),
    )


def test_schedule_contract_rejects_notes_on_classes_and_unsupported_guesses() -> None:
    with pytest.raises(ValidationError):
        ScheduleInference(
            event_id="class-1",
            course="ECE 250",
            course_supported=True,
            session_type="Class",
            session_type_supported=True,
            location="RCH 101",
            location_supported=True,
            note="Bring the worksheet.",
            note_supported=True,
        )
    with pytest.raises(ValidationError):
        ScheduleInference(
            event_id="unknown-1",
            course="ECE 250",
            course_supported=False,
            session_type="Unclear",
            session_type_supported=False,
            location="-",
            location_supported=False,
        )


def test_host_manifest_has_four_ordered_bounded_embeds_and_safe_links() -> None:
    courses = (ActiveMorningCourse(course_id="course-1", course_code="ECE 250", title="ECE 250"),)
    jobs = (
        _item("job-1", area="jobs", title="Safe", url="https://notion.so/safe"),
        _item("job-2", area="jobs", title="Unsafe", url="https://evil.example/item", hour=10),
    )
    manifest = build_morning_briefing_manifest(
        local_date=LOCAL_OCCURRENCE.date(),
        delivery_key_prefix="planner-morning-four-v3:2026-09-10:0800",
        active_courses=courses,
        course_items=(),
        job_items=jobs,
        misc_items=(),
        schedule_items=(_item("class-1", area="learn", title="ECE 250 Lecture"),),
        job_composition=None,
        misc_composition=None,
        schedule_composition=None,
    )

    assert len(manifest.entries) == 4
    assert tuple(entry.category for entry in manifest.entries) == MORNING_CATEGORY_ORDER
    assert [entry.ordinal for entry in manifest.entries] == [1, 2, 3, 4]
    assert all(len(entry.embed.title) <= 256 for entry in manifest.entries)
    assert all(len(entry.embed.description) <= 4_096 for entry in manifest.entries)
    jobs_body = manifest.entries[1].embed.description
    assert "[Safe](https://notion.so/safe)" in jobs_body
    assert "[Unsafe]" not in jobs_body
    assert "Additional details were unavailable." in jobs_body
    schedule_body = manifest.entries[3].embed.description
    assert "TIME          COURSE" in schedule_body
    assert "Unclear" in schedule_body


def test_schedule_table_allows_only_grounded_tutorial_or_lab_notes() -> None:
    schedule_item = _item(
        "lab-1",
        area="learn",
        title="ECE 250 Lab in E2 1303",
        context="ECE 250; Lab; E2 1303; bring the worksheet.",
    )
    composition = ScheduleComposition(
        events=(
            ScheduleInference(
                event_id="lab-1",
                course="ECE 250",
                course_supported=True,
                session_type="Lab",
                session_type_supported=True,
                location="E2 1303",
                location_supported=True,
                note="Bring the worksheet.",
                note_supported=True,
            ),
        )
    )

    manifest = build_morning_briefing_manifest(
        local_date=LOCAL_OCCURRENCE.date(),
        delivery_key_prefix="planner-morning-four-v3:2026-09-10:0800:v1",
        active_courses=(),
        course_items=(),
        job_items=(),
        misc_items=(),
        schedule_items=(schedule_item,),
        job_composition=None,
        misc_composition=None,
        schedule_composition=composition,
    )

    schedule_body = manifest.entries[3].embed.description
    assert "ECE 250      Lab       E2 1303" in schedule_body
    assert "• ECE 250: Bring the worksheet." in schedule_body


def test_grounded_course_rows_are_deterministic_and_host_dates_are_authoritative() -> None:
    courses = tuple(
        ActiveMorningCourse(
            course_id=f"course-{index}",
            course_code=f"COURSE {index}",
            title=f"Course {index}",
        )
        for index in range(2)
    )
    items = (
        _item(
            "event-1",
            source_id="course-0",
            source_label="ECE 190",
            title="Chemistry p-set from slides",
            semantic_status=CalendarEventSemanticStatus.VALID,
            semantic_overview="You have a chemistry problem set from the slide deck.",
        ),
        _item(
            "event-2",
            source_id="course-0",
            title="Quiz 1",
            hour=10,
            semantic_status=CalendarEventSemanticStatus.INVALID,
        ),
        _item(
            "event-3",
            source_id="course-0",
            title="Lab report window",
            hour=11,
        ).model_copy(update={"local_end_label": "Thursday, September 10, 2026 at 12:00 EDT"}),
    )

    manifest = build_morning_briefing_manifest(
        local_date=LOCAL_OCCURRENCE.date(),
        delivery_key_prefix="planner-morning-four-v3:2026-09-10:0800:v1",
        active_courses=courses,
        course_items=items,
        job_items=(),
        misc_items=(),
        schedule_items=(),
        job_composition=None,
        misc_composition=None,
        schedule_composition=None,
    )

    course_body = manifest.entries[0].embed.description
    assert len(course_body) <= 4_096
    assert course_body.index("chemistry problem set") < course_body.index("Quiz 1")
    assert course_body.count("chemistry problem set") == 1
    assert "Thursday, September 10, 2026 at 09:00 EDT" in course_body
    assert "Thursday, September 10, 2026 at 10:00 EDT" in course_body
    assert "through Thursday, September 10, 2026 at 12:00 EDT" in course_body
    assert "Additional interpretation was unavailable" in course_body
    assert course_body.count("Nothing pressing") == 1
    assert "[Notion](https://www.notion.so/workspace/page)" in course_body


def test_oversized_deterministic_course_category_stays_within_discord_limit() -> None:
    courses = tuple(
        ActiveMorningCourse(
            course_id=f"course-{index}",
            course_code=f"COURSE {index}",
            title=f"Course {index}",
        )
        for index in range(8)
    )
    items = tuple(
        _item(
            f"event-{index}",
            source_id=course.course_id,
            semantic_status=CalendarEventSemanticStatus.VALID,
            semantic_overview="x" * 700,
        )
        for index, course in enumerate(courses)
    )

    manifest = build_morning_briefing_manifest(
        local_date=LOCAL_OCCURRENCE.date(),
        delivery_key_prefix="planner-morning-four-v3:2026-09-10:0800:v1",
        active_courses=courses,
        course_items=items,
        job_items=(),
        misc_items=(),
        schedule_items=(),
        job_composition=None,
        misc_composition=None,
        schedule_composition=None,
    )

    course_body = manifest.entries[0].embed.description
    assert len(course_body) <= 4_096
    assert "Unavailable" in course_body
    assert "exceeded the safe Discord embed size" in course_body


class Store:
    def load_active_morning_courses(self):
        return ({"course_id": "course-1", "course_code": "ECE 250", "title": "ECE 250"},)

    def load_morning_calendar_items(self, *, occurrence: datetime, timezone: str):
        assert occurrence == OCCURRENCE.scheduled_at
        assert timezone == "America/Toronto"
        return ()


class GroundedCourseStore(Store):
    edited_at = datetime(2026, 9, 9, 12, tzinfo=UTC)

    def load_active_morning_courses(self):
        return ({"course_id": "course-1", "course_code": "ECE 190", "title": "ECE 190"},)

    def load_morning_calendar_items(self, *, occurrence: datetime, timezone: str):
        assert occurrence == OCCURRENCE.scheduled_at
        assert timezone == "America/Toronto"
        starts_at = datetime(2026, 9, 10, 4, tzinfo=UTC)
        return (
            {
                "event_id": "event-chemistry",
                "source_id": "course-1",
                "source_area": "course",
                "source_label": "ECE 190",
                "title": "Chemistry p-set from slides",
                "display_kind": "Assignment",
                "local_start_label": "Thursday, September 10, 2026",
                "local_end_label": None,
                "relative_date_label": "Today",
                "is_all_day": True,
                "completed": False,
                "semantic_status": "unavailable",
                "starts_at": starts_at,
                "ends_at": None,
                "source_last_edited_at": self.edited_at,
                "source_url": "https://www.notion.so/workspace/chemistry",
            },
        )


class EvidenceConnector:
    async def retrieve_calendar_event_evidence(self, page_id: str):
        assert page_id == "event-chemistry"
        return SimpleNamespace(
            event_id=page_id,
            last_edited_at=GroundedCourseStore.edited_at,
            fragments=(),
        )


class ReadyRuntime:
    async def ensure_ready(self) -> None:
        return None


class GroundedInterpreter:
    model_identity = "qwen-test"
    config_version = "cfg-test"

    async def analyze(self, event):
        title_id = f"{event.event_id}:host:title"
        result = CalendarEventSemanticResult(
            event_id=event.event_id,
            overview="You have a chemistry problem set from the slide deck.",
            description_present=False,
            description=None,
            evidence_fragment_ids=(title_id,),
            description_fragment_ids=(),
            classification_rationale="The title names a chemistry problem set sourced from slides.",
            activity_intent=None,
            intent_status=CalendarActivityIntentStatus.UNAVAILABLE,
            intent_evidence_fragment_ids=(),
            intent_rationale=None,
        )
        return CalendarEventSemanticOutcome(
            status=CalendarEventSemanticStatus.VALID,
            result=result,
            source_fingerprint=event.source_fingerprint,
            model_identity=self.model_identity,
            config_version=self.config_version,
        )


class Syncer:
    async def sync(self, *, now: datetime | None = None) -> AcademicNotionSyncResult:
        return AcademicNotionSyncResult(status="succeeded", synced_at=OCCURRENCE.scheduled_at)


class PartialLearnSyncer:
    async def sync(self, *, now: datetime | None = None) -> AcademicNotionSyncResult:
        return AcademicNotionSyncResult(
            status="partial",
            synced_at=OCCURRENCE.scheduled_at,
            unavailable_roles=(AcademicCalendarRole.LEARN,),
        )


class ManifestStore:
    def __init__(self) -> None:
        self.value: tuple[MorningBriefingDeliveryManifest, set[int]] | None = None

    def load(self, *, period_key: str):
        assert period_key == PERIOD_KEY
        return self.value

    def save(self, manifest, *, period_key: str, delivered_ordinals: set[int]) -> None:
        assert period_key == PERIOD_KEY
        self.value = (manifest, set(delivered_ordinals))


class Delivery:
    def __init__(self, *, fail_ordinal: int | None = None) -> None:
        self.fail_ordinal = fail_ordinal
        self.calls: list[tuple[object, str]] = []

    async def send_scheduled_notification(self, content: str, *, idempotency_key: str):
        raise AssertionError("normal morning delivery must use embeds")

    async def send_scheduled_embed(self, embed, *, idempotency_key: str):
        ordinal = len(self.calls) + 1
        if self.fail_ordinal == ordinal:
            self.fail_ordinal = None
            raise RuntimeError("Discord unavailable")
        self.calls.append((embed, idempotency_key))
        return SimpleNamespace(status="sent", id=None)


@pytest.mark.asyncio
async def test_scheduled_boundary_sends_four_embeds_and_resumes_undelivered_categories() -> None:
    store = ManifestStore()
    first_delivery = Delivery(fail_ordinal=3)
    with pytest.raises(RuntimeError, match="Discord unavailable"):
        await execute_scheduled_morning_notification(
            store=Store(),
            syncer=Syncer(),
            delivery=first_delivery,
            occurrence=OCCURRENCE,
            period_key=PERIOD_KEY,
            executed_at=OCCURRENCE.scheduled_at,
            manifest_store=store,
        )
    assert len(first_delivery.calls) == 2
    assert store.value is not None
    assert store.value[1] == {1, 2}

    resumed_delivery = Delivery()
    result = await execute_scheduled_morning_notification(
        store=Store(),
        syncer=Syncer(),
        delivery=resumed_delivery,
        occurrence=OCCURRENCE,
        period_key=PERIOD_KEY,
        executed_at=OCCURRENCE.scheduled_at,
        manifest_store=store,
    )

    assert result["resumed_manifest"] is True
    assert result["part_count"] == 4
    assert result["delivery_count"] == 2
    assert len(resumed_delivery.calls) == 2
    assert [key.rsplit(":", 2)[-2] for _, key in resumed_delivery.calls] == ["misc", "schedule"]


@pytest.mark.asyncio
async def test_invalid_reserved_learn_calendar_isolated_to_schedule_embed() -> None:
    delivery = Delivery()
    result = await execute_scheduled_morning_notification(
        store=Store(),
        syncer=PartialLearnSyncer(),
        delivery=delivery,
        occurrence=OCCURRENCE,
        period_key=PERIOD_KEY,
        executed_at=OCCURRENCE.scheduled_at,
    )

    assert result["status"] == "succeeded"
    assert len(delivery.calls) == 4
    descriptions = [embed.description for embed, _key in delivery.calls]
    assert "Nothing pressing" in descriptions[0]
    assert "No incomplete Misc events" in descriptions[2]
    assert "Unavailable" in descriptions[3]
    assert "No stale calendar data was used" in descriptions[3]


@pytest.mark.asyncio
async def test_scheduled_delivery_renders_grounded_title_only_coursework() -> None:
    delivery = Delivery()

    result = await execute_scheduled_morning_notification(
        store=GroundedCourseStore(),
        syncer=Syncer(),
        delivery=delivery,
        occurrence=OCCURRENCE,
        period_key=PERIOD_KEY,
        executed_at=OCCURRENCE.scheduled_at,
        evidence_connector=EvidenceConnector(),
        semantic_interpreter=GroundedInterpreter(),
        ollama_runtime=ReadyRuntime(),
    )

    assert result["status"] == "succeeded"
    assert len(delivery.calls) == 4
    course_body = delivery.calls[0][0].description
    assert "**ECE 190**" in course_body
    assert "chemistry problem set from the slide deck" in course_body
    assert "Today (Thursday, September 10, 2026)" in course_body
    assert "Nothing pressing" not in course_body


@pytest.mark.asyncio
async def test_scheduled_delivery_keeps_title_and_due_date_when_model_is_unavailable() -> None:
    delivery = Delivery()

    result = await execute_scheduled_morning_notification(
        store=GroundedCourseStore(),
        syncer=Syncer(),
        delivery=delivery,
        occurrence=OCCURRENCE,
        period_key=PERIOD_KEY,
        executed_at=OCCURRENCE.scheduled_at,
    )

    assert result["status"] == "succeeded"
    course_body = delivery.calls[0][0].description
    assert "Chemistry p-set from slides" in course_body
    assert "Today (Thursday, September 10, 2026)" in course_body
    assert "Additional interpretation was unavailable" in course_body
    assert "Nothing pressing" not in course_body
