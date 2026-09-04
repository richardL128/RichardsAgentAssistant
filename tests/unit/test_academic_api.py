from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.academic import router


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


def test_checkin_response_is_proposal_and_wrong_confirmation_does_not_write() -> None:
    store, writer = Store(), Writer()
    with TestClient(_app(store, writer)) as client:
        response = client.post("/academic/checkin", json={"reply": "completed essay"})
        assert response.status_code == 202
        body = response.json()
        wrong = client.post(
            f"/academic/confirm/{body['proposal_id']}",
            json={"confirmation_event": "yes"},
        )
    assert wrong.json()["status"] == "confirmation_required"
    assert writer.calls == 0


def test_exact_confirmation_is_the_only_write_path() -> None:
    store, writer = Store(), Writer()
    with TestClient(_app(store, writer)) as client:
        response = client.post("/academic/checkin", json={"reply": "completed essay"})
        body = response.json()
        result = client.post(
            f"/academic/confirm/{body['proposal_id']}",
            json={"confirmation_event": body["confirmation_event"]},
        )
    assert result.json()["status"] == "applied"
    assert writer.calls == 1
    assert store.applied
