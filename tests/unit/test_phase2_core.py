"""Unit coverage for Phase 2 shared contracts and deterministic health."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import SecretStr, ValidationError

from app.connectors.discord import DiscordFailureAlertAdapter, FailureAlert
from app.core.config import Settings
from app.core.errors import (
    ErrorCategory,
    ErrorCode,
    LifeAgentError,
    authorization_error,
    transient_error,
)
from app.core.runtime import AgentKind, RunContext, TriggerKind
from app.health.checks import HealthState
from app.health.evaluator import (
    DeliveryStatus,
    OperationalFacts,
    ProcessingStatus,
    evaluate_operational_health,
)
from app.workflows.approval import (
    ApprovalDecision,
    build_approval_graph,
    resume_approval,
    start_approval,
)


def test_run_context_requires_aware_timestamp_and_stable_versions() -> None:
    context = RunContext.new(
        agent=AgentKind.CODE_REVIEW,
        trigger=TriggerKind.TEST,
        idempotency_key="review:repo:abc123",
        config_version="config-v1",
        input_version="sha256-input",
        model_identifier="qwen3-32gb:latest",
        model_digest="digest",
    )

    assert context.started_at.tzinfo is UTC
    with pytest.raises(ValidationError, match="timezone-aware"):
        RunContext(
            **{
                **context.model_dump(),
                "started_at": datetime.fromisoformat("2026-09-03T00:00:00"),
            }
        )


def test_standard_errors_expose_only_stable_safe_metadata() -> None:
    transient = transient_error(ErrorCode.CONNECTOR_TRANSIENT, "provider unavailable")
    auth = authorization_error()

    assert transient.record.retryable is True
    assert auth.record.retryable is False
    assert str(transient) == ErrorCode.CONNECTOR_TRANSIENT.value
    assert not hasattr(transient.record, "raw_message")


def test_retry_and_schedule_settings_validate_cross_field_boundaries() -> None:
    with pytest.raises(ValidationError, match="base delay"):
        Settings(retry_base_delay_seconds=60, retry_max_delay_seconds=30)
    with pytest.raises(ValidationError, match="minute precision"):
        Settings(code_review_schedule="18:00:30")


@pytest.mark.parametrize(
    ("processing", "delivery", "authenticated", "overdue", "expected"),
    [
        (
            ProcessingStatus.SUCCEEDED,
            DeliveryStatus.SUCCEEDED,
            True,
            False,
            HealthState.HEALTHY,
        ),
        (
            ProcessingStatus.WAITING_RETRY,
            DeliveryStatus.INTENT,
            True,
            False,
            HealthState.ATTENTION,
        ),
        (
            ProcessingStatus.RUNNING,
            DeliveryStatus.INTENT,
            True,
            True,
            HealthState.ATTENTION,
        ),
        (
            ProcessingStatus.FAILED,
            DeliveryStatus.NOT_REQUIRED,
            True,
            False,
            HealthState.FAILED,
        ),
        (
            ProcessingStatus.SUCCEEDED,
            DeliveryStatus.FAILED,
            True,
            False,
            HealthState.FAILED,
        ),
        (
            ProcessingStatus.SUCCEEDED,
            DeliveryStatus.SUCCEEDED,
            False,
            False,
            HealthState.FAILED,
        ),
    ],
)
def test_health_is_derived_from_fixture_facts(
    processing: ProcessingStatus,
    delivery: DeliveryStatus,
    authenticated: bool,
    overdue: bool,
    expected: HealthState,
) -> None:
    now = datetime(2026, 9, 3, 16, 0, tzinfo=UTC)
    due = now - timedelta(minutes=1) if overdue else now + timedelta(minutes=1)
    facts = OperationalFacts(
        component="fixture-agent",
        processing=processing,
        delivery=delivery,
        connector_authenticated=authenticated,
        evaluated_at=now,
        next_expected_at=due,
        retry_attempt=1,
        retry_limit=3,
        diagnostic_code="fixture_state",
    )

    result = evaluate_operational_health(facts)

    assert result.state is expected
    assert result.diagnostic == "fixture_state; retry 1 of 3"


def test_approval_graph_pauses_and_resumes_same_run_in_memory() -> None:
    run_id = uuid4()
    request_id = uuid4()
    graph = build_approval_graph(InMemorySaver())

    paused = start_approval(
        graph,
        run_id=run_id,
        approval_request_id=request_id,
        proposal_artifact_key="a" * 64,
    )
    resumed = resume_approval(
        graph,
        run_id=run_id,
        decision=ApprovalDecision(decision="approved"),
    )

    assert "__interrupt__" in paused
    assert resumed["run_id"] == str(run_id)
    assert resumed["approval_request_id"] == str(request_id)
    assert resumed["outcome"] == "approved"


async def test_discord_adapter_sends_only_allowlisted_failure_alerts() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "123456789012345678"})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10",
        transport=httpx.MockTransport(handler),
    ) as client:
        adapter = DiscordFailureAlertAdapter(
            token=SecretStr("never-print-this-token"),
            allowed_channel_ids={"987654321012345678"},
            client=client,
        )
        alert = FailureAlert(
            delivery_id=uuid4(),
            run_id=uuid4(),
            channel_id="987654321012345678",
            component="code_review",
            state=HealthState.FAILED,
            error_code=ErrorCode.AUTHORIZATION_INVALID,
            attempt=1,
            attempt_limit=3,
        )
        receipt = await adapter.send(alert)

    assert receipt.external_id == "123456789012345678"
    assert len(seen) == 1
    assert seen[0].url.path == "/api/v10/channels/987654321012345678/messages"
    assert b"never-print-this-token" not in seen[0].content

    with pytest.raises(ValidationError, match="normal health"):
        FailureAlert(
            delivery_id=uuid4(),
            run_id=uuid4(),
            channel_id="987654321012345678",
            component="code_review",
            state=HealthState.HEALTHY,
            error_code=ErrorCode.INTERNAL,
            attempt=0,
            attempt_limit=0,
        )


async def test_discord_authorization_failure_is_redacted_and_not_retried() -> None:
    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10",
        transport=httpx.MockTransport(lambda _: httpx.Response(401)),
    ) as client:
        adapter = DiscordFailureAlertAdapter(
            token=SecretStr("never-print-this-token"),
            allowed_channel_ids={"987654321012345678"},
            client=client,
        )
        alert = FailureAlert(
            delivery_id=uuid4(),
            run_id=uuid4(),
            channel_id="987654321012345678",
            component="code_review",
            state=HealthState.FAILED,
            error_code=ErrorCode.AUTHORIZATION_INVALID,
            attempt=1,
            attempt_limit=3,
        )
        with pytest.raises(LifeAgentError) as raised:
            await adapter.send(alert)

    assert raised.value.record.category is ErrorCategory.AUTHORIZATION
    assert raised.value.record.retryable is False
    assert "never-print-this-token" not in str(raised.value)
