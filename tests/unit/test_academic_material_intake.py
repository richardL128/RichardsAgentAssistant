from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import fitz
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.agents.academic_planner.material_intake import (
    AcademicMaterialIntakeService,
    InboundMaterialArtifactError,
    load_verified_inbound_pdf,
)
from app.artifacts.store import ArtifactStore
from app.db.academic import (
    AcademicInboundMaterialInput,
    AcademicInboundMaterialRepository,
    AcademicRepository,
    SourceCitation,
    SQLAlchemyAcademicPlannerStore,
)
from app.db.models import AcademicInboundMaterial, AcademicProposedChange, AuditEvent, Base

NOW = datetime(2026, 9, 11, 16, tzinfo=UTC)
OWNER = "111111111111111111"
CHANNEL = "222222222222222222"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    created: Engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'intake.db'}")
    Base.metadata.create_all(created)
    try:
        yield created
    finally:
        created.dispose()


@pytest.fixture
def artifact_store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifacts")


def _pdf_bytes(text: bytes = b"rubric") -> bytes:
    return b"%PDF-1.4\n" + text + b"\n%%EOF"


def _real_pdf(text: str) -> bytes:
    document: Any = fitz.open()
    page: Any = document.new_page()
    page.insert_text((72, 72), text)
    content = cast(bytes, document.tobytes())
    document.close()
    return content


def _material_input(
    artifact_store: ArtifactStore,
    *,
    message_id: str = "333333333333333333",
    attachment_id: str = "444444444444444444",
    content: bytes | None = None,
) -> AcademicInboundMaterialInput:
    payload = content or _pdf_bytes()
    artifact = artifact_store.put(
        payload,
        media_type="application/pdf",
        data_class="academic_inbound_material_private",
        already_redacted=True,
    )
    return AcademicInboundMaterialInput(
        discord_message_id=message_id,
        discord_attachment_id=attachment_id,
        owner_discord_user_id=OWNER,
        discord_channel_id=CHANNEL,
        filename="ece222-a2-rubric.pdf",
        media_type="application/pdf",
        declared_byte_size=len(payload),
        observed_byte_size=len(payload),
        content_hash=hashlib.sha256(payload).hexdigest(),
        raw_artifact_key=artifact.key,
        captured_at=NOW,
        expires_at=NOW + timedelta(days=30),
    )


def _proposal(
    session: Session,
    *,
    key: str,
    event_id: str,
    owner: str | None = OWNER,
    channel: str | None = CHANNEL,
) -> AcademicProposedChange:
    checkin = AcademicRepository.create_checkin(
        session,
        idempotency_key=f"discord:{event_id}",
        external_event_id=event_id,
        channel=f"discord:{channel or CHANNEL}",
        received_at=NOW,
        redacted_summary="Create assessment proposal pending confirmation.",
        status="proposal_pending",
    )
    return AcademicRepository.create_proposed_change(
        session,
        checkin_id=checkin.id,
        idempotency_key=key,
        operation="notion_update",
        target_type="academic_checkin",
        target_id=key,
        payload={
            "changes": [
                {
                    "field": "create_assessment",
                    "value": "ECE 222 Assignment 2 due Oct 8",
                }
            ]
        },
        redacted_preview="Create ECE 222 Assignment 2.",
        confirmation_token=f"confirm {key}",
        expires_at=NOW + timedelta(days=30),
        owner_discord_user_id=owner,
        discord_channel_id=channel,
    )


def _assessment_id(session: Session) -> uuid.UUID:
    course = AcademicRepository.upsert_course(
        session,
        notion_id="course-ece222",
        course_code="ECE 222",
        title="Linear Circuits",
        term="2026F",
    )
    return AcademicRepository.upsert_assessment(
        session,
        notion_id="assessment-a2",
        course_id=course.id,
        title="Assignment 2",
        assessment_type="assignment",
        due_at=NOW + timedelta(days=14),
        grade_weight_percent=10,
        estimated_minutes=180,
        confidence=1,
        fact_state="confirmed",
        citation=SourceCitation(),
    ).id


@dataclass(frozen=True, slots=True)
class _PlannerChange:
    field: str
    value: str
    inbound_material_ids: tuple[object, ...] = ()
    supersedes_proposal_id: object | None = None

    def model_dump(self, **kwargs: object) -> dict[str, object]:
        del kwargs
        result: dict[str, object] = {"field": self.field, "value": self.value}
        if self.inbound_material_ids:
            result["inbound_material_ids"] = [str(item) for item in self.inbound_material_ids]
        if self.supersedes_proposal_id is not None:
            result["supersedes_proposal_id"] = str(self.supersedes_proposal_id)
        return result


@dataclass(frozen=True, slots=True)
class _PlannerProposal:
    proposal_id: object
    confirmation_event: str
    changes: tuple[_PlannerChange, ...]
    expires_at: datetime | None = None


def test_create_or_replay_is_durable_owner_scoped_and_verifies_artifact(
    engine: Engine,
    artifact_store: ArtifactStore,
) -> None:
    material = _material_input(artifact_store)
    with Session(engine) as session, session.begin():
        first = AcademicInboundMaterialRepository.create_or_replay(session, material)
        second = AcademicInboundMaterialRepository.create_or_replay(session, material)
        assert first.status == "created"
        assert second.status == "replayed"
        assert first.row.id == second.row.id
        assert (
            AcademicInboundMaterialRepository.get_owned(
                session,
                first.row.id,
                owner_discord_user_id="999999999999999999",
                discord_channel_id=CHANNEL,
            )
            is None
        )
        material_id = first.row.id

    store = SQLAlchemyAcademicPlannerStore(engine)
    snapshot = store.get_inbound_material(material_id)
    assert snapshot is not None
    verified = load_verified_inbound_pdf(artifact_store, snapshot)
    assert verified.content == _pdf_bytes()
    assert verified.content_hash == material.content_hash

    bad_snapshot = replace(snapshot, content_hash="0" * 64)
    with pytest.raises(InboundMaterialArtifactError, match="hash mismatch"):
        load_verified_inbound_pdf(artifact_store, bad_snapshot)


def test_service_marks_and_finds_only_owner_channel_recent_unresolved_intake(
    engine: Engine,
    artifact_store: ArtifactStore,
) -> None:
    store = SQLAlchemyAcademicPlannerStore(engine)
    service = AcademicMaterialIntakeService(
        store=store,
        artifact_store=artifact_store,
        max_bytes=20 * 1024 * 1024,
    )
    _, first = store.create_or_replay_inbound_material(_material_input(artifact_store))
    _, second = store.create_or_replay_inbound_material(
        _material_input(
            artifact_store,
            message_id="333333333333333334",
            attachment_id="444444444444444445",
        )
    )

    assert service.mark_awaiting_target(
        (first.id,),
        owner_discord_user_id=OWNER,
        discord_channel_id=CHANNEL,
        now=NOW,
    ) == (first.id,)
    found = service.find_recent_unresolved(
        owner_discord_user_id=OWNER,
        discord_channel_id=CHANNEL,
        now=NOW,
    )

    assert {item.inbound_material_id for item in found} == {first.id, second.id}
    assert {item.state for item in found} == {"captured", "awaiting_target"}
    assert (
        service.find_recent_unresolved(
            owner_discord_user_id="999999999999999999",
            discord_channel_id=CHANNEL,
            now=NOW,
        )
        == ()
    )


def test_validate_bind_and_state_transitions_reject_competing_or_backward_moves(
    engine: Engine,
    artifact_store: ArtifactStore,
) -> None:
    material = _material_input(artifact_store)
    store = SQLAlchemyAcademicPlannerStore(engine)
    _, snapshot = store.create_or_replay_inbound_material(material)

    with Session(engine) as session, session.begin():
        assessment_id = _assessment_id(session)
        proposal = _proposal(session, key="proposal-a", event_id="event-a")
        validated = AcademicInboundMaterialRepository.validate_for_proposal(
            session,
            (snapshot.id,),
            owner_discord_user_id=OWNER,
            discord_channel_id=CHANNEL,
        )
        assert validated == (snapshot.id,)
        bound = AcademicInboundMaterialRepository.bind_to_proposal(
            session,
            snapshot.id,
            owner_discord_user_id=OWNER,
            discord_channel_id=CHANNEL,
            proposal_id=proposal.id,
            assessment_id=assessment_id,
        )
        assert bound.state == "proposal_pending"
        assert bound.assessment_id == assessment_id
        competing = _proposal(session, key="proposal-b", event_id="event-b")
        with pytest.raises(ValueError, match="competing live proposal"):
            AcademicInboundMaterialRepository.validate_for_proposal(
                session,
                (snapshot.id,),
                owner_discord_user_id=OWNER,
                discord_channel_id=CHANNEL,
                proposal_id=competing.id,
            )
        with pytest.raises(ValueError, match="backwards"):
            AcademicInboundMaterialRepository.advance_state(
                session,
                snapshot.id,
                owner_discord_user_id=OWNER,
                discord_channel_id=CHANNEL,
                state="captured",
            )
        proposal_id = proposal.id

    seeded = store.mark_inbound_material_seeded(
        snapshot.id,
        owner_discord_user_id=OWNER,
        discord_channel_id=CHANNEL,
        notion_page_id="notion-page",
        notion_block_id="pdf-block",
        notion_upload_id="upload-id",
        proposal_id=proposal_id,
        assessment_id=assessment_id,
    )
    assert seeded.state == "seeded"
    assert seeded.notion_block_id == "pdf-block"


def test_pending_create_lookup_and_supersede_links_materials(
    engine: Engine,
    artifact_store: ArtifactStore,
) -> None:
    material = _material_input(artifact_store)
    with Session(engine) as session, session.begin():
        intake = AcademicInboundMaterialRepository.create_or_replay(session, material).row
        old = _proposal(session, key="proposal-old", event_id="event-old")
        new = _proposal(session, key="proposal-new", event_id="event-new")
        _proposal(
            session,
            key="proposal-other-owner",
            event_id="event-other-owner",
            owner="999999999999999999",
        )

        status, found = AcademicInboundMaterialRepository.find_single_pending_create_proposal(
            session,
            owner_discord_user_id=OWNER,
            discord_channel_id=CHANNEL,
            now=NOW,
        )
        assert status == "ambiguous"
        assert found is None

        AcademicRepository.reject_proposed_change(session, proposal_id=new.id, now=NOW)
        status, found = AcademicInboundMaterialRepository.find_single_pending_create_proposal(
            session,
            owner_discord_user_id=OWNER,
            discord_channel_id=CHANNEL,
            now=NOW,
        )
        assert status == "found"
        assert found is not None
        assert found.id == old.id

        replacement = _proposal(session, key="proposal-replacement", event_id="event-replacement")
        AcademicInboundMaterialRepository.supersede_pending_create_with_materials(
            session,
            old_proposal_id=old.id,
            new_proposal_id=replacement.id,
            material_ids=(intake.id,),
            owner_discord_user_id=OWNER,
            discord_channel_id=CHANNEL,
            now=NOW,
        )
        intake_id = intake.id
        old_id = old.id
        replacement_id = replacement.id

    with Session(engine) as session:
        stored_old = session.get(AcademicProposedChange, old_id)
        stored_intake = session.get(AcademicInboundMaterial, intake_id)
        assert stored_old is not None
        assert stored_old.state == "superseded"
        assert stored_old.superseded_by_id == replacement_id
        assert stored_old.superseded_reason == "replacement_with_inbound_material"
        assert stored_intake is not None
        assert stored_intake.proposal_id == replacement_id
        assert stored_intake.state == "proposal_pending"
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 2


def test_material_intake_service_inspects_bounded_owner_scoped_pdf(
    engine: Engine,
    artifact_store: ArtifactStore,
) -> None:
    payload = _real_pdf("# Requirements\nSubmit circuit analysis and cite simulation evidence.")
    material = _material_input(
        artifact_store,
        message_id="555555555555555555",
        attachment_id="666666666666666666",
        content=payload,
    )
    store = SQLAlchemyAcademicPlannerStore(engine)
    _, snapshot = store.create_or_replay_inbound_material(material)
    service = AcademicMaterialIntakeService(
        store=store,
        artifact_store=artifact_store,
        max_bytes=1024 * 1024,
    )

    inspection = service.inspect_inbound_pdf(
        snapshot.id,
        owner_discord_user_id=OWNER,
        discord_channel_id=CHANNEL,
    )

    assert inspection.filename == "ece222-a2-rubric.pdf"
    assert inspection.extraction_status == "extracted"
    assert inspection.page_count == 1
    assert inspection.preview
    assert "simulation evidence" in inspection.preview[0].text
    assert service.validate_for_proposal(
        (snapshot.id,),
        owner_discord_user_id=OWNER,
        discord_channel_id=CHANNEL,
    ) == (snapshot.id,)


def test_material_intake_service_searches_safe_pending_create_options(
    engine: Engine,
    artifact_store: ArtifactStore,
) -> None:
    public_id = uuid4()
    with Session(engine) as session, session.begin():
        _proposal(
            session,
            key=f"academic-proposal:{public_id}",
            event_id="event-searchable-create",
        )
    service = AcademicMaterialIntakeService(
        store=SQLAlchemyAcademicPlannerStore(engine),
        artifact_store=artifact_store,
        max_bytes=1024 * 1024,
    )

    results = service.search_pending_assessment_creates(
        "assignment",
        owner_discord_user_id=OWNER,
        discord_channel_id=CHANNEL,
        now=NOW,
    )

    assert len(results) == 1
    assert results[0].proposal_id == public_id
    assert results[0].preview == "Create ECE 222 Assignment 2."
    assert "artifact" not in str(results[0].as_dict()).lower()


def test_save_discord_checkin_supersedes_pending_create_and_binds_material_atomically(
    engine: Engine,
    artifact_store: ArtifactStore,
) -> None:
    old_public_id = uuid4()
    new_public_id = uuid4()
    store = SQLAlchemyAcademicPlannerStore(engine)
    _, material = store.create_or_replay_inbound_material(_material_input(artifact_store))
    with Session(engine) as session, session.begin():
        old = _proposal(
            session,
            key=f"academic-proposal:{old_public_id}",
            event_id="event-original-create",
        )
        old_row_id = old.id

    result = store.save_discord_checkin(
        _PlannerProposal(
            proposal_id=new_public_id,
            confirmation_event=f"confirm {new_public_id}",
            changes=(
                _PlannerChange(
                    field="create_assessment",
                    value="ECE 222 Assignment 2 with attached rubric",
                    inbound_material_ids=(material.id,),
                    supersedes_proposal_id=old_public_id,
                ),
            ),
            expires_at=NOW + timedelta(days=30),
        ),
        external_event_id="event-replacement-create",
        channel=CHANNEL,
        received_at=NOW,
        owner_discord_user_id=OWNER,
    )

    assert result.status == "created"
    assert result.proposal_row_id is not None
    with Session(engine) as session, session.begin():
        old = session.get(AcademicProposedChange, old_row_id)
        replacement = session.get(AcademicProposedChange, result.proposal_row_id)
        stored_material = session.get(AcademicInboundMaterial, material.id)
        assert old is not None
        assert replacement is not None
        assert stored_material is not None
        assert old.state == "superseded"
        assert old.superseded_by_id == replacement.id
        assert stored_material.proposal_id == replacement.id
        status, _ = AcademicRepository.begin_confirmed_change(
            session,
            proposal_id=old.id,
            confirmation_event=old.confirmation_token,
            now=NOW,
        )
        assert status == "confirmation_required"
