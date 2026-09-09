"""Minimal durable approval pause used as the base for later workflows."""

from __future__ import annotations

from typing import Any, Literal, NotRequired, Protocol, TypedDict, cast
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, ConfigDict

from app.workflows.checkpoint import thread_config


class ApprovalState(TypedDict):
    run_id: str
    approval_request_id: str
    proposal_artifact_key: str
    decision: NotRequired[Literal["approved", "rejected"]]
    outcome: NotRequired[Literal["approved", "rejected"]]


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal["approved", "rejected"]


def _await_approval(state: ApprovalState) -> ApprovalState:
    """Pause without performing a side effect; resume data is schema validated."""

    resumed = interrupt(
        {
            "run_id": state["run_id"],
            "approval_request_id": state["approval_request_id"],
            "proposal_artifact_key": state["proposal_artifact_key"],
        }
    )
    decision = ApprovalDecision.model_validate(resumed)
    return {
        **state,
        "decision": decision.decision,
        "outcome": decision.decision,
    }


class ApprovalGraph(Protocol):
    """Small surface used by the workflow wrapper, insulated from library generics."""

    def invoke(
        self,
        value: ApprovalState | Command[Any],
        config: RunnableConfig,
    ) -> dict[str, Any]: ...


def build_approval_graph(checkpointer: BaseCheckpointSaver[Any]) -> ApprovalGraph:
    """Compile the base approval graph against a durable checkpointer."""

    builder = StateGraph(ApprovalState)
    builder.add_node("await_approval", _await_approval)  # pyright: ignore[reportUnknownMemberType]
    builder.add_edge(START, "await_approval")
    builder.add_edge("await_approval", END)
    compiled = builder.compile(  # pyright: ignore[reportUnknownMemberType]
        checkpointer=checkpointer,
        name="lifeagent-approval",
    )
    return cast(ApprovalGraph, compiled)


def start_approval(
    graph: ApprovalGraph,
    *,
    run_id: UUID,
    approval_request_id: UUID,
    proposal_artifact_key: str,
) -> ApprovalState:
    """Start and persist a workflow that will pause at its approval boundary."""

    result = graph.invoke(
        ApprovalState(
            run_id=str(run_id),
            approval_request_id=str(approval_request_id),
            proposal_artifact_key=proposal_artifact_key,
        ),
        thread_config(str(run_id)),
    )
    return cast(ApprovalState, result)


def resume_approval(
    graph: ApprovalGraph,
    *,
    run_id: UUID,
    decision: ApprovalDecision,
) -> ApprovalState:
    """Resume the original run/thread after an explicit validated decision."""

    result = graph.invoke(
        Command(resume=decision.model_dump(mode="json")),
        thread_config(str(run_id)),
    )
    return cast(ApprovalState, result)


__all__ = [
    "ApprovalDecision",
    "ApprovalState",
    "build_approval_graph",
    "resume_approval",
    "start_approval",
]
