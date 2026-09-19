from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.agents.academic_planner.material_planning import (
    AssessmentMaterialPlanningProfile,
    MaterialPlanningDocumentVersion,
    MaterialPlanningModelIdentity,
    MaterialPlanningProfileCritique,
)
from app.db.academic import (
    AcademicRepository,
    DocumentChunkInput,
    SourceCitation,
    SQLAlchemyAcademicPlannerStore,
)
from app.db.models import AcademicAssessmentMaterialProfile, AcademicDocument, Base

NOW = datetime(2026, 9, 11, 16, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    created: Engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'profiles.db'}")
    Base.metadata.create_all(created)
    try:
        yield created
    finally:
        created.dispose()


def _seed_material(engine: Engine) -> tuple[str, dict[str, object]]:
    with Session(engine) as session, session.begin():
        course = AcademicRepository.upsert_course(
            session,
            notion_id="course-page",
            course_code="ECE 222",
            title="Linear Circuits",
            term="2026F",
            priority=80,
        )
        assessment = AcademicRepository.upsert_assessment(
            session,
            notion_id="assessment-page",
            course_id=course.id,
            title="Assignment 2",
            assessment_type="assignment",
            due_at=NOW + timedelta(days=14),
            grade_weight_percent=10,
            estimated_minutes=120,
            confidence=1,
            fact_state="confirmed",
            citation=SourceCitation(),
        )
        body = "Design the amplifier, submit a lab report, and justify the simulation evidence."
        digest = hashlib.sha256(body.encode()).hexdigest()
        document = AcademicRepository.upsert_document(
            session,
            notion_id="assessment-page",
            document_version=digest,
            title="Rubric",
            document_type="application/pdf",
            retrieved_at=NOW,
            artifact_key=digest,
            content_hash=digest,
            course_id=course.id,
            assessment_id=assessment.id,
            source_kind="notion_block_file",
            source_page_id="assessment-page",
            source_block_id="pdf-block",
            source_key="assessment-page:block:pdf-block",
            original_filename="rubric.pdf",
            media_type="application/pdf",
            extraction_status="extracted",
            active=True,
        )
        chunk = AcademicRepository.replace_document_chunks(
            session,
            document_id=document.id,
            chunks=[
                DocumentChunkInput(
                    ordinal=0,
                    heading="Deliverables",
                    content=body,
                    content_hash=hashlib.sha256(body.encode()).hexdigest(),
                    citation=SourceCitation(page=1, block="pdf-block"),
                )
            ],
        )[0]
        chunk_id = str(chunk.id)
        AcademicRepository.activate_document_version(session, document_id=document.id)
    chunk_mapping = SQLAlchemyAcademicPlannerStore(engine).list_material_chunks(
        "assessment-page",
        limit=20,
    )[0]
    assert chunk_mapping["chunk_id"] == chunk_id
    return "assessment-page", dict(chunk_mapping)


def _profile(
    assessment_id: str,
    chunk: dict[str, object],
    *,
    version_suffix: str = "a",
    effort_lower: int = 90,
    effort_upper: int = 180,
) -> AssessmentMaterialPlanningProfile:
    return AssessmentMaterialPlanningProfile(
        profile_id=f"sha256:{version_suffix * 64}",
        profile_version=f"sha256:{version_suffix * 64}",
        assessment_id=assessment_id,
        state="active",
        deliverables_summary="Amplifier design and report",
        success_criteria_summary="Simulation evidence and justification",
        study_topics_summary="Circuit analysis and report structure",
        effort_lower_minutes=effort_lower,
        effort_upper_minutes=effort_upper,
        scope_score=0.7,
        dependency_risk_score=0.4,
        explicit_grade_weight_percent=10,
        evidence_chunk_ids=(str(chunk["chunk_id"]),),
        document_versions=(
            MaterialPlanningDocumentVersion(
                document_id=str(chunk["document_id"]),
                document_version=str(chunk["document_version"]),
                content_hash=str(chunk["content_hash"]),
            ),
        ),
        model_identity=MaterialPlanningModelIdentity(
            generator_model="profile-generator:test",
            critic_model="profile-critic:test",
        ),
        critique=MaterialPlanningProfileCritique(
            accepted=True,
            entailed=True,
            relevant=True,
            safe_against_prompt_injection=True,
            same_assessment=True,
            no_date_or_commitment_claims=True,
        ),
    )


def test_profile_repository_activates_last_good_and_feeds_planner_facts(engine: Engine) -> None:
    assessment_id, chunk = _seed_material(engine)
    store = SQLAlchemyAcademicPlannerStore(engine)
    first = _profile(assessment_id, chunk, version_suffix="a")
    second = _profile(
        assessment_id,
        chunk,
        version_suffix="b",
        effort_lower=120,
        effort_upper=240,
    )

    store.save_profile(first)
    assert store.get_active_profile(assessment_id) is None
    store.activate_profile(first)
    active = store.get_active_profile(assessment_id)
    assert active is not None
    assert active.profile_version == first.profile_version
    store.save_profile(second)
    active = store.get_active_profile(assessment_id)
    assert active is not None
    assert active.profile_version == first.profile_version
    store.activate_profile(second)

    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(AcademicAssessmentMaterialProfile)
                .where(AcademicAssessmentMaterialProfile.state == "active")
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(AcademicAssessmentMaterialProfile)
                .where(AcademicAssessmentMaterialProfile.state == "inactive")
            )
            == 1
        )

    with Session(engine) as session:
        active = session.scalar(
            select(AcademicAssessmentMaterialProfile).where(
                AcademicAssessmentMaterialProfile.state == "active"
            )
        )
        assert active is not None
        assert active.effort_lower_minutes == 120
        assert active.evidence_chunk_ids == [str(chunk["chunk_id"])]


def test_profile_repository_rejects_stale_or_cross_assessment_evidence(engine: Engine) -> None:
    assessment_id, chunk = _seed_material(engine)
    stale = _profile(assessment_id, chunk, version_suffix="c").model_copy(
        update={
            "document_versions": (
                MaterialPlanningDocumentVersion(
                    document_id=str(chunk["document_id"]),
                    document_version=str(chunk["document_version"]),
                    content_hash="0" * 64,
                ),
            )
        }
    )

    with pytest.raises(ValueError, match="document versions are stale"):
        SQLAlchemyAcademicPlannerStore(engine).save_profile(stale)

    with Session(engine) as session, session.begin():
        document = session.get(AcademicDocument, UUID(str(chunk["document_id"])))
        assert document is not None
        document.active = False

    with pytest.raises(ValueError, match="inactive material"):
        SQLAlchemyAcademicPlannerStore(engine).save_profile(
            _profile(assessment_id, chunk, version_suffix="d")
        )
