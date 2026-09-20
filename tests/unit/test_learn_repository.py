"""Focused LEARN persistence tests using SQLite as a fast repository seam."""

from __future__ import annotations

import importlib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from app.db.learn import (
    LearnAnnouncementInput,
    LearnCourseInput,
    LearnDatedImplicationInput,
    LearnDeliveryInput,
    LearnProposalLinkInput,
    LearnRepository,
    LearnScheduledItemInput,
    LearnSemanticResultInput,
)
from app.db.models import (
    Base,
    LearnDatedImplication,
    LearnNotificationDelivery,
)


@pytest.fixture
def engine(tmp_path: Path):
    created = create_engine(f"sqlite+pysqlite:///{tmp_path / 'learn.db'}")
    Base.metadata.create_all(created)
    try:
        yield created
    finally:
        created.dispose()


def _now() -> datetime:
    return datetime(2026, 9, 19, 12, tzinfo=UTC)


def _course(session: Session):
    return LearnRepository.upsert_course(
        session,
        LearnCourseInput(
            org_unit_id="12345",
            code="ECE 240",
            name="Electronic Circuits",
            term="2026-fall",
            active=True,
            url="https://learn.example.test/d2l/home/12345",
            seen_at=_now(),
        ),
    )


def _announcement(session: Session, *, fingerprint: str = "fp1"):
    course = _course(session)
    return LearnRepository.upsert_announcement(
        session,
        LearnAnnouncementInput(
            source_id="announcement-1",
            course_id=course.id,
            published_at=_now() - timedelta(hours=2),
            updated_at=None,
            effective_at=_now() - timedelta(hours=2),
            url="https://learn.example.test/news/1",
            fingerprint=fingerprint,
            seen_at=_now(),
        ),
    )


def _semantic(
    session: Session,
    *,
    fingerprint: str = "fp1",
    academic_date: date = date(2026, 9, 24),
):
    announcement = _announcement(session, fingerprint=fingerprint)
    return LearnRepository.save_announcement_semantics(
        session,
        LearnSemanticResultInput(
            announcement_id=announcement.id,
            course_id=announcement.course_id,
            source_fingerprint=fingerprint,
            summary="The course staff moved the lab briefing.",
            why_it_matters="You need the changed time before attending lab.",
            action_items=[{"text": "Check the lab room before leaving."}],
            evidence_fragments=[{"id": "p1", "quote": "short bounded citation"}],
            source_url=announcement.url,
            model_identity="qwen-test",
            prompt_version="learn-announcement-v1",
            interpreted_at=_now(),
            dated_implications=[
                LearnDatedImplicationInput(
                    implication_key="lab-briefing",
                    activity_type="lab",
                    academic_date=academic_date,
                    date_precision="date",
                    evidence_fragments=[{"id": "p2"}],
                )
            ],
        ),
    )


def test_announcement_fingerprint_change_supersedes_semantics_deliveries_and_links(engine) -> None:
    with Session(engine) as session, session.begin():
        first = _semantic(session, fingerprint="fp1")
        implication = session.scalar(
            select(LearnDatedImplication).where(
                LearnDatedImplication.semantic_result_id == first.id
            )
        )
        assert implication is not None
        delivery, created = LearnRepository.record_delivery(
            session,
            LearnDeliveryInput(
                message_key="morning:2026-09-20:announcement-1:fp1",
                delivery_kind="announcement_window",
                occurrence_date=date(2026, 9, 20),
                announcement_id=first.announcement_id,
                source_fingerprint="fp1",
            ),
        )
        assert created is True
        proposal_status, proposal = LearnRepository.create_proposal_link(
            session,
            LearnProposalLinkInput(
                source_kind="announcement_implication",
                source_id="announcement-1:lab-briefing",
                source_fingerprint="fp1",
                dated_implication_id=implication.id,
                operation="create_learn_calendar_event",
                idempotency_key="learn:proposal:announcement-1:fp1",
                reserved_calendar_notion_id="classes-row",
                learn_context_property_id="learn-context",
                learn_context_property_name="LEARN Context",
            ),
        )
        assert proposal_status == "created"

        updated = LearnRepository.upsert_announcement(
            session,
            LearnAnnouncementInput(
                source_id="announcement-1",
                course_id=first.course_id,
                effective_at=_now(),
                fingerprint="fp2",
                seen_at=_now(),
                url="https://learn.example.test/news/1",
            ),
        )
        second = LearnRepository.save_announcement_semantics(
            session,
            LearnSemanticResultInput(
                announcement_id=updated.id,
                course_id=updated.course_id,
                source_fingerprint="fp2",
                summary="The course staff changed the lab briefing again.",
                why_it_matters="The newer announcement replaces the earlier reminder.",
                action_items=[],
                evidence_fragments=[{"id": "p3"}],
                source_url=updated.url,
                model_identity="qwen-test",
                prompt_version="learn-announcement-v1",
                interpreted_at=_now(),
                dated_implications=[
                    LearnDatedImplicationInput(
                        implication_key="lab-briefing-new",
                        activity_type="lab",
                        academic_date=date(2026, 9, 25),
                        date_precision="date",
                        evidence_fragments=[],
                    )
                ],
            ),
        )

        assert first.status == "superseded"
        assert implication.status == "superseded"
        assert delivery.status == "superseded"
        assert proposal.state == "superseded"
        assert second.status == "valid"

    with Session(engine) as session:
        current = LearnRepository.announcement_summaries_for_window(
            session,
            since=_now() - timedelta(hours=1),
            until=_now() + timedelta(minutes=1),
        )
        assert [row.source_fingerprint for row in current] == ["fp2"]
        reminders = LearnRepository.day_before_implications(
            session,
            reminder_date=date(2026, 9, 24),
        )
        assert [row.source_fingerprint for row in reminders] == ["fp2"]


def test_missing_announcement_fails_closed_without_raw_body_fallback(engine) -> None:
    with Session(engine) as session, session.begin():
        result = _semantic(session)
        removed = LearnRepository.mark_announcements_missing(
            session,
            seen_source_ids=set(),
            now=_now() + timedelta(hours=1),
        )
        implication = session.scalar(
            select(LearnDatedImplication).where(
                LearnDatedImplication.semantic_result_id == result.id
            )
        )

        assert removed == 1
        assert result.status == "source_removed"
        assert implication is not None
        assert implication.status == "source_removed"

    with Session(engine) as session:
        assert (
            LearnRepository.announcement_summaries_for_window(
                session,
                since=_now() - timedelta(days=1),
                until=_now() + timedelta(days=1),
            )
            == ()
        )


def test_delivery_records_are_idempotent(engine) -> None:
    with Session(engine) as session, session.begin():
        first, first_created = LearnRepository.record_delivery(
            session,
            LearnDeliveryInput(
                message_key="reconnect:learn-login-required:2026-09-19",
                delivery_kind="reconnect_alert",
                occurrence_date=date(2026, 9, 19),
            ),
        )
        second, second_created = LearnRepository.record_delivery(
            session,
            LearnDeliveryInput(
                message_key="reconnect:learn-login-required:2026-09-19",
                delivery_kind="reconnect_alert",
                occurrence_date=date(2026, 9, 19),
            ),
        )

        assert first.id == second.id
        assert first_created is True
        assert second_created is False

    with Session(engine) as session:
        assert session.scalar(select(LearnNotificationDelivery)).message_key.startswith(
            "reconnect:"
        )


def test_proposal_idempotency_blocks_same_fingerprint_until_explicit_or_source_change(
    engine,
) -> None:
    with Session(engine) as session, session.begin():
        course = _course(session)
        scheduled = LearnRepository.upsert_scheduled_item(
            session,
            LearnScheduledItemInput(
                source_id="scheduled-1",
                course_id=course.id,
                title="Tutorial",
                start_date=date(2026, 9, 22),
                date_precision="date",
                fingerprint="sched-fp1",
                seen_at=_now(),
            ),
        )
        input_base = {
            "source_kind": "scheduled_item",
            "source_id": "scheduled-1",
            "source_fingerprint": "sched-fp1",
            "scheduled_item_id": scheduled.id,
            "operation": "create_learn_calendar_event",
            "reserved_calendar_notion_id": "classes-row",
            "learn_context_property_id": "learn-context",
            "learn_context_property_name": "LEARN Context",
        }
        status, link = LearnRepository.create_proposal_link(
            session,
            LearnProposalLinkInput(
                idempotency_key="learn:proposal:scheduled-1:sched-fp1",
                **input_base,
            ),
        )
        replay_status, replay = LearnRepository.create_proposal_link(
            session,
            LearnProposalLinkInput(
                idempotency_key="learn:proposal:scheduled-1:sched-fp1:replay",
                **input_base,
            ),
        )
        LearnRepository.set_proposal_link_state(session, link_id=link.id, state="rejected")
        rejected_status, rejected_link = LearnRepository.create_proposal_link(
            session,
            LearnProposalLinkInput(
                idempotency_key="learn:proposal:scheduled-1:sched-fp1:after-reject",
                **input_base,
            ),
        )
        explicit_status, explicit_link = LearnRepository.create_proposal_link(
            session,
            LearnProposalLinkInput(
                idempotency_key="learn:proposal:scheduled-1:sched-fp1:explicit",
                explicit_request_key="discord-message-2",
                **input_base,
            ),
            explicit_request=True,
        )
        LearnRepository.upsert_scheduled_item(
            session,
            LearnScheduledItemInput(
                source_id="scheduled-1",
                course_id=course.id,
                title="Tutorial revised",
                start_date=date(2026, 9, 23),
                date_precision="date",
                fingerprint="sched-fp2",
                seen_at=_now() + timedelta(minutes=5),
            ),
        )
        changed_status, changed_link = LearnRepository.create_proposal_link(
            session,
            LearnProposalLinkInput(
                source_kind="scheduled_item",
                source_id="scheduled-1",
                source_fingerprint="sched-fp2",
                scheduled_item_id=scheduled.id,
                operation="create_learn_calendar_event",
                idempotency_key="learn:proposal:scheduled-1:sched-fp2",
                reserved_calendar_notion_id="classes-row",
                learn_context_property_id="learn-context",
                learn_context_property_name="LEARN Context",
            ),
        )

        assert status == "created"
        assert replay_status == "existing"
        assert replay.id == link.id
        assert rejected_status == "requires_explicit_request"
        assert rejected_link.id == link.id
        assert explicit_status == "created"
        assert explicit_link.explicit_request_key == "discord-message-2"
        assert explicit_link.state == "superseded"
        assert changed_status == "created"
        assert changed_link.source_fingerprint == "sched-fp2"


def test_0030_migration_adds_learn_tables_and_reserved_calendar_columns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = importlib.import_module("app.db.migrations.versions.0030_learn_persistence")
    assert migration.revision == "0030_learn_persistence"
    assert migration.down_revision == "0029_context_memory_persistence"

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'migration-0030.db'}")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE academic_course_calendars (id CHAR(32) PRIMARY KEY)"
            )
            connection.exec_driver_sql(
                "CREATE TABLE academic_proposed_changes (id CHAR(32) PRIMARY KEY)"
            )
            context = MigrationContext.configure(connection)
            monkeypatch.setattr(migration, "op", Operations(context))

            migration.upgrade()

            inspector = inspect(connection)
            table_names = set(inspector.get_table_names())
            assert {
                "learn_courses",
                "learn_scheduled_items",
                "learn_announcement_sources",
                "learn_announcement_semantic_results",
                "learn_dated_implications",
                "learn_notification_deliveries",
                "learn_notion_proposal_links",
            }.issubset(table_names)
            calendar_columns = {
                column["name"] for column in inspector.get_columns("academic_course_calendars")
            }
            assert {
                "learn_context_property_id",
                "learn_context_property_name",
            }.issubset(calendar_columns)
            announcement_columns = {
                column["name"] for column in inspector.get_columns("learn_announcement_sources")
            }
            assert "fingerprint" in announcement_columns
            assert "body" not in announcement_columns
            assert "title" not in announcement_columns

            migration.downgrade()
            downgraded = inspect(connection)
            assert "learn_courses" not in downgraded.get_table_names()
            calendar_columns_after = {
                column["name"] for column in downgraded.get_columns("academic_course_calendars")
            }
            assert "learn_context_property_id" not in calendar_columns_after
    finally:
        engine.dispose()
