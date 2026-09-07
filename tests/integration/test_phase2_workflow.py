"""PostgreSQL restart/resume acceptance test for the Phase 2 base graph."""

from __future__ import annotations

from uuid import uuid4

from testcontainers.community.postgres import PostgresContainer

from app.workflows.approval import (
    ApprovalDecision,
    build_approval_graph,
    resume_approval,
    start_approval,
)
from app.workflows.checkpoint import postgres_checkpointer


def test_paused_approval_survives_checkpointer_restart() -> None:
    run_id = uuid4()
    approval_request_id = uuid4()
    artifact_key = "b" * 64

    with PostgresContainer(
        "pgvector/pgvector:0.8.6-pg16-bookworm",
        driver="psycopg",
    ) as postgres:
        database_url = postgres.get_connection_url()

        with postgres_checkpointer(database_url, setup=True) as first_process:
            first_graph = build_approval_graph(first_process)
            paused = start_approval(
                first_graph,
                run_id=run_id,
                approval_request_id=approval_request_id,
                proposal_artifact_key=artifact_key,
            )
            assert "__interrupt__" in paused

        # A new saver and freshly compiled graph simulate an API/worker restart.
        with postgres_checkpointer(database_url) as restarted_process:
            restarted_graph = build_approval_graph(restarted_process)
            resumed = resume_approval(
                restarted_graph,
                run_id=run_id,
                decision=ApprovalDecision(decision="approved"),
            )

        assert resumed["run_id"] == str(run_id)
        assert resumed["approval_request_id"] == str(approval_request_id)
        assert resumed["proposal_artifact_key"] == artifact_key
        assert resumed["outcome"] == "approved"
