from __future__ import annotations

from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agents.academic_planner.contracts import CheckinProposal
from app.api.academic import router

PROPOSAL_ID = UUID("11111111-1111-4111-8111-111111111111")


class Store:
    def __init__(self) -> None:
        self.proposal = None
        self.applied = False
        self.applying = False

    def save_checkin_proposal(self, proposal):
        self.proposal = proposal

    def get_checkin_proposal(self, proposal_id):
        return self.proposal if self.proposal and self.proposal.proposal_id == proposal_id else None

    def prepare_checkin_application(self, proposal_id, confirmation_event):
        proposal = self.get_checkin_proposal(proposal_id)
        if proposal is None:
            return "not_found", None
        if self.applied:
            return "already_applied", proposal
        if self.applying:
            return "in_progress", proposal
        if confirmation_event != proposal.confirmation_event:
            return "confirmation_required", proposal
        self.applying = True
        return "ready", proposal

    def mark_checkin_applied(self, proposal_id, confirmation_event=None):
        self.applied = True
        self.applying = False

    def reject_checkin_proposal(self, proposal_id, *, actor="academic_planner"):
        proposal = self.get_checkin_proposal(proposal_id)
        if proposal is None:
            return "not_pending", None
        if self.applied:
            return "already_applied", proposal
        self.proposal = proposal
        return "rejected", proposal


class Writer:
    def __init__(self) -> None:
        self.calls = 0

    async def apply_confirmed_changes(self, changes, *, proposal_id, confirmation_event):
        self.calls += 1


def _app(store: Store, writer: Writer) -> FastAPI:
    app = FastAPI()
    app.state.academic_store = store
    app.state.notion_writer = writer
    app.include_router(router)
    return app


def _seed_proposal(store: Store) -> CheckinProposal:
    proposal = CheckinProposal(
        proposal_id=PROPOSAL_ID,
        confirmation_event=f"confirm {PROPOSAL_ID}",
        changes=(),
    )
    store.save_checkin_proposal(proposal)
    return proposal


def test_legacy_checkin_creation_route_is_absent() -> None:
    store, writer = Store(), Writer()
    with TestClient(_app(store, writer)) as client:
        response = client.post("/academic/checkin", json={"reply": "completed essay"})

    assert response.status_code == 404
    assert store.proposal is None
    assert writer.calls == 0


def test_wrong_confirmation_does_not_write() -> None:
    store, writer = Store(), Writer()
    proposal = _seed_proposal(store)
    with TestClient(_app(store, writer)) as client:
        wrong = client.post(
            f"/academic/confirm/{proposal.proposal_id}",
            json={"confirmation_event": "yes"},
        )
    assert wrong.json()["status"] == "confirmation_required"
    assert writer.calls == 0


def test_exact_confirmation_is_the_only_write_path() -> None:
    store, writer = Store(), Writer()
    proposal = _seed_proposal(store)
    with TestClient(_app(store, writer)) as client:
        result = client.post(
            f"/academic/confirm/{proposal.proposal_id}",
            json={"confirmation_event": proposal.confirmation_event},
        )
    assert result.json()["status"] == "applied"
    assert writer.calls == 1
    assert store.applied


def test_rejection_does_not_require_or_call_notion_writer() -> None:
    store, writer = Store(), Writer()
    proposal = _seed_proposal(store)
    with TestClient(_app(store, writer)) as client:
        result = client.post(
            f"/academic/reject/{proposal.proposal_id}",
            json={"rejection_event": f"reject {proposal.proposal_id}"},
        )

    assert result.status_code == 200
    assert result.json()["status"] == "rejected"
    assert writer.calls == 0


def test_manual_sync_uses_the_production_sync_boundary() -> None:
    class Syncer:
        calls = 0

        async def sync(self):
            self.calls += 1
            return type(
                "Result",
                (),
                {
                    "as_dict": lambda self: {
                        "status": "partial",
                        "course_count": 2,
                        "valid_course_count": 1,
                        "assessment_count": 3,
                        "archived_count": 1,
                        "clarification_count": 1,
                        "invalid_calendar_count": 1,
                        "diagnostic_codes": ["assessment_calendar_missing"],
                    }
                },
            )()

    store, writer, syncer = Store(), Writer(), Syncer()
    app = _app(store, writer)
    app.state.academic_syncer = syncer

    with TestClient(app) as client:
        response = client.post("/academic/sync")

    assert response.status_code == 200
    assert response.json()["status"] == "partial"
    assert response.json()["valid_course_count"] == 1
    assert syncer.calls == 1
