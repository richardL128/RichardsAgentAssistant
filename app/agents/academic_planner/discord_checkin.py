"""Private Discord message routing for academic proposals and decisions."""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any, Protocol, cast

from app.agents.academic_planner.agent_clarification import (
    AcademicAgentClarificationService,
    AgentClarificationInspectResult,
    AgentClarificationTurnResult,
)
from app.agents.academic_planner.agent_loop import (
    AcademicAgentCatalog,
    AcademicAgentGateway,
    AcademicAgentProgressSink,
    route_academic_request,
    run_academic_agent_loop,
)
from app.agents.academic_planner.commands import AcademicCommandAction, parse_academic_command
from app.agents.academic_planner.contracts import (
    AcademicAgentContinuationInput,
    AcademicAgentLoopOutcome,
    AcademicAgentProgressEvent,
    AcademicAgentProgressPhase,
    CheckinProposal,
)
from app.agents.academic_planner.proposal_review import (
    NotionAcademicWriter,
    confirm_checkin_proposal,
    reject_checkin_proposal,
)
from app.connectors.discord import DiscordAcademicProgressReporter
from app.connectors.discord_gateway import (
    DiscordAcademicMessageCreate,
    DiscordMessageCallbackResult,
)
from app.llm.ollama_runtime import OllamaRuntimeReady

_PROPOSAL_NAMESPACE = uuid.UUID("6f7240e2-48d0-44bd-b9bf-a8bd8d9adccc")


class DiscordCheckinStore(Protocol):
    confirmation_ttl_hours: int

    def get_latest_daily_plan(self) -> Any | None: ...

    def save_discord_checkin(
        self,
        proposal: CheckinProposal,
        *,
        external_event_id: str,
        channel: str,
        received_at: Any,
    ) -> Any: ...


class DiscordCheckinDelivery(Protocol):
    async def send_confirmation(
        self, proposal: CheckinProposal, *, idempotency_key: str
    ) -> object: ...

    async def send_response(self, content: str, *, idempotency_key: str) -> object: ...


WriterProvider = Callable[[], NotionAcademicWriter | None]


class AcademicMemoryHandler(Protocol):
    async def handle_memory_review(
        self,
        *,
        external_event_id: str,
        channel_id: str,
        user_id: str,
        raw_text: str,
        received_at: Any,
    ) -> Any: ...

    async def handle_reflection(
        self,
        *,
        external_event_id: str,
        channel_id: str,
        user_id: str,
        raw_text: str,
        received_at: Any,
    ) -> Any: ...


class AcademicOllamaRuntime(Protocol):
    async def ensure_ready(self) -> OllamaRuntimeReady: ...


class AcademicDiscordCheckinHandler:
    """Route one already-normalized private message outside the Gateway loop."""

    def __init__(
        self,
        *,
        store: DiscordCheckinStore,
        delivery: DiscordCheckinDelivery,
        allowed_channel_ids: set[str],
        authorized_user_ids: set[str],
        writer_provider: WriterProvider,
        ollama_runtime: AcademicOllamaRuntime | None = None,
        agent_gateway: AcademicAgentGateway | None = None,
        semantic_router_gateway: Any | None = None,
        agent_catalog: AcademicAgentCatalog | None = None,
        assistant_user_id: str | None = None,
        timezone: str = "America/Toronto",
        memory_service: AcademicMemoryHandler | None = None,
        agent_clarification_service: AcademicAgentClarificationService | None = None,
    ) -> None:
        self._store = store
        self._delivery = delivery
        self._allowed_channel_ids = frozenset(allowed_channel_ids)
        self._authorized_user_ids = frozenset(authorized_user_ids)
        self._writer_provider = writer_provider
        self._ollama_runtime = ollama_runtime
        self._agent_gateway = agent_gateway
        self._semantic_router_gateway = semantic_router_gateway
        self._agent_catalog = agent_catalog
        self._assistant_user_id = assistant_user_id
        self._timezone = timezone
        self._memory_service = memory_service
        self._agent_clarification_service = agent_clarification_service
        if (agent_gateway is None) != (agent_catalog is None):
            raise ValueError("academic agent gateway and catalog must be configured together")
        if (agent_gateway is None) != (semantic_router_gateway is None):
            raise ValueError("academic agent and semantic router must be configured together")

    async def __call__(self, message: DiscordAcademicMessageCreate) -> DiscordMessageCallbackResult:
        if (
            message.channel_id not in self._allowed_channel_ids
            or message.author_id not in self._authorized_user_ids
        ):
            return DiscordMessageCallbackResult(status="unauthorized")
        raw_content = message.content.get_secret_value()
        command_content = raw_content.strip()
        command = parse_academic_command(command_content)
        if command is not None:
            return await self._handle_command(message, *command)
        was_mentioned = self._assistant_was_mentioned(message)
        content = self._without_assistant_mention(raw_content)

        if not was_mentioned:
            if self._agent_gateway is None or self._agent_clarification_service is None:
                return DiscordMessageCallbackResult(status="ignored")
            try:
                has_pending = await asyncio.to_thread(
                    self._agent_clarification_service.has_pending_clarification,
                    channel_id=message.channel_id,
                    user_id=message.author_id,
                    received_at=message.timestamp,
                )
            except (OSError, ValueError):
                return DiscordMessageCallbackResult(status="ignored")
            if not has_pending:
                return DiscordMessageCallbackResult(status="ignored")
        if self._is_command_like(command_content):
            return await self._reject_malformed_command(message)

        inspection: AgentClarificationInspectResult | None = None
        if self._agent_gateway is not None and self._agent_clarification_service is not None:
            try:
                inspection = await asyncio.to_thread(
                    self._agent_clarification_service.inspect_message,
                    external_event_id=message.message_id,
                    channel_id=message.channel_id,
                    user_id=message.author_id,
                    raw_text=content,
                    received_at=message.timestamp,
                )
            except (OSError, ValueError):
                await self._delivery.send_response(
                    "I could not safely inspect the clarification state. "
                    "Please send a new complete mention after checking storage health.",
                    idempotency_key=(
                        f"academic-discord-message:{message.message_id}:clarification-failed:v1"
                    ),
                )
                return DiscordMessageCallbackResult(status="failed")
            if inspection.status == "duplicate":
                return DiscordMessageCallbackResult(status="duplicate")
            if inspection.status in {"cancelled", "failed", "exhausted", "in_progress"}:
                await self._delivery.send_response(
                    inspection.response or "That clarification could not be continued safely.",
                    idempotency_key=(
                        f"academic-discord-message:{message.message_id}:"
                        f"clarification-{inspection.status}:v1"
                    ),
                )
                return DiscordMessageCallbackResult(
                    status="failed" if inspection.status == "failed" else "handled"
                )

        if self._is_command_like(content):
            return await self._reject_malformed_command(message)

        attempt_number = inspection.attempt_number if inspection is not None else 1
        reporter = (
            self._create_progress_reporter(message, attempt_number=attempt_number)
            if self._agent_gateway is not None or self._memory_service is not None
            else None
        )
        if self._agent_gateway is not None or self._memory_service is not None:
            if reporter is not None:
                await reporter.start(
                    AcademicAgentProgressEvent(
                        phase=AcademicAgentProgressPhase.RUNTIME_WAKING,
                        attempt_number=attempt_number,
                        attempt_limit=3,
                    )
                )
            if self._ollama_runtime is None:
                return await self._send_runtime_unavailable(message, reporter=reporter)
            try:
                await self._ollama_runtime.ensure_ready()
            except Exception:
                return await self._send_runtime_unavailable(message, reporter=reporter)

        return await self._handle_checkin(
            message,
            content,
            inspection=inspection,
            reporter=reporter,
        )

    async def _handle_checkin(
        self,
        message: DiscordAcademicMessageCreate,
        content: str,
        *,
        inspection: AgentClarificationInspectResult | None,
        reporter: DiscordAcademicProgressReporter | None,
    ) -> DiscordMessageCallbackResult:
        proposal_id = uuid.uuid5(_PROPOSAL_NAMESPACE, message.message_id)
        changes = ()
        question: str | None = None
        response: str | None = None
        turn: AgentClarificationTurnResult | None = None
        outcome: AcademicAgentLoopOutcome | None = None
        is_agent_continuation = inspection is not None and inspection.status == "resume_pending"
        memory_request: str | None = None
        if self._semantic_router_gateway is not None and not is_agent_continuation:
            if reporter is not None:
                await reporter.update(
                    AcademicAgentProgressEvent(
                        phase=AcademicAgentProgressPhase.MODEL_TURN,
                        attempt_number=1,
                        attempt_limit=3,
                        model_turn=1,
                        model_turn_limit=10,
                    )
                )
                await reporter.flush()
            routed = await route_academic_request(
                gateway=self._semantic_router_gateway,
                message=content,
            )
            if routed is None:
                return await self._send_agent_failure(
                    message,
                    reporter=reporter,
                    response=(
                        "Qwen could not safely route that academic request. No change was "
                        "prepared; please restate it after checking model health."
                    ),
                    suffix="semantic-route-failed",
                )
            memory_request = routed.memory_request
            content = routed.calendar_request or ""
            if routed.calendar_request is None:
                changes = ()
                outcome = AcademicAgentLoopOutcome.NOT_APPLICABLE
        if self._agent_gateway is not None and self._agent_catalog is not None:
            if outcome is AcademicAgentLoopOutcome.NOT_APPLICABLE:
                pass
            elif not content:
                changes = ()
                question = "What academic change would you like me to prepare?"
                outcome = AcademicAgentLoopOutcome.CLARIFICATION_REQUIRED
            else:
                continuation: AcademicAgentContinuationInput | None = None
                attempt_number = inspection.attempt_number if inspection is not None else 1
                if self._agent_clarification_service is not None:
                    try:
                        turn = await asyncio.to_thread(
                            self._agent_clarification_service.prepare_turn,
                            external_event_id=message.message_id,
                            channel_id=message.channel_id,
                            user_id=message.author_id,
                            raw_text=content,
                            received_at=message.timestamp,
                        )
                    except (OSError, ValueError):
                        return await self._send_agent_failure(
                            message,
                            reporter=reporter,
                            response=(
                                "I could not safely persist this agent turn. "
                                "Check storage health and send a new complete mention."
                            ),
                            suffix="state-failed",
                        )
                    if (
                        turn.status not in {"started", "resumed"}
                        or turn.context is None
                        or turn.session_id is None
                    ):
                        if turn.status == "duplicate":
                            return DiscordMessageCallbackResult(status="duplicate")
                        return await self._send_agent_failure(
                            message,
                            reporter=reporter,
                            response=(
                                turn.response or "That clarification could not be prepared safely."
                            ),
                            suffix=f"turn-{turn.status}",
                        )
                    attempt_number = turn.attempt_number
                    continuation = AcademicAgentContinuationInput(
                        original_user_request=(
                            turn.context.original_user_request.get_secret_value()
                        ),
                        prior_clarification_questions=(turn.context.prior_clarification_questions),
                        clarification_answers=tuple(
                            item.get_secret_value() for item in turn.context.clarification_answers
                        ),
                    )
                progress_sink = (
                    self._create_agent_progress_sink(reporter) if reporter is not None else None
                )
                planned = await run_academic_agent_loop(
                    gateway=self._agent_gateway,
                    catalog=self._agent_catalog,
                    message=content,
                    now=message.timestamp,
                    timezone=self._timezone,
                    progress_sink=progress_sink,
                    attempt_number=attempt_number,
                    attempt_limit=3,
                    continuation=continuation,
                )
                changes = planned.changes
                question = planned.question
                response = getattr(planned, "response", None)
                outcome = getattr(
                    planned,
                    "outcome",
                    (
                        AcademicAgentLoopOutcome.PROPOSAL_READY
                        if changes
                        else AcademicAgentLoopOutcome.CLARIFICATION_REQUIRED
                    ),
                )
        else:
            return await self._send_agent_failure(
                message,
                reporter=reporter,
                response=(
                    "Qwen semantic planning is not configured, so no academic change was "
                    "prepared. Check the academic agent configuration and try again."
                ),
                suffix="agent-not-configured",
            )

        memory_result = None
        if (
            self._memory_service is not None
            and memory_request is not None
            and not is_agent_continuation
        ):
            memory_result = await self._handle_learning_memory(
                message,
                memory_request,
            )
            if memory_result.status == "duplicate":
                return DiscordMessageCallbackResult(status="duplicate")
            if memory_result.response:
                await self._delivery.send_response(
                    memory_result.response,
                    idempotency_key=(f"academic-discord-message:{message.message_id}:memory:v2"),
                )

        if outcome is AcademicAgentLoopOutcome.ANSWER_READY:
            if (
                turn is not None
                and turn.session_id is not None
                and self._agent_clarification_service is not None
            ):
                await asyncio.to_thread(
                    self._agent_clarification_service.complete_resolved,
                    session_id=turn.session_id,
                    now=message.timestamp,
                )
            if reporter is not None:
                await reporter.finish_completed()
            await self._delivery.send_response(
                response or "I could not prepare a grounded answer.",
                idempotency_key=f"academic-discord-message:{message.message_id}:answer:v1",
            )
            return DiscordMessageCallbackResult(status="handled")

        if outcome is AcademicAgentLoopOutcome.NOT_APPLICABLE:
            if (
                turn is not None
                and turn.session_id is not None
                and self._agent_clarification_service is not None
            ):
                await asyncio.to_thread(
                    self._agent_clarification_service.complete_resolved,
                    session_id=turn.session_id,
                    now=message.timestamp,
                )
            if reporter is not None:
                await reporter.finish_completed()
            if memory_result is None or memory_result.status == "not_applicable":
                await self._delivery.send_response(
                    "I could not identify a supported academic calendar or learning-memory "
                    "change in that request. Please restate what you want changed.",
                    idempotency_key=(
                        f"academic-discord-message:{message.message_id}:not-applicable:v1"
                    ),
                )
            return DiscordMessageCallbackResult(status="handled")

        latest_plan = self._store.get_latest_daily_plan()
        plan_id = getattr(latest_plan, "plan_id", None)
        proposal = CheckinProposal(
            proposal_id=proposal_id,
            confirmation_event=f"confirm {proposal_id}",
            changes=changes,
            source_plan_id=plan_id,
            expires_at=message.timestamp + timedelta(hours=self._store.confirmation_ttl_hours),
            question=(None if changes else question or "No supported change was safely extracted."),
        )
        persisted = self._store.save_discord_checkin(
            proposal,
            external_event_id=message.message_id,
            channel=message.channel_id,
            received_at=message.timestamp,
        )
        is_replayed = getattr(persisted, "status", None) == "replayed"
        if is_replayed:
            return DiscordMessageCallbackResult(status="duplicate")

        if changes:
            if (
                turn is not None
                and turn.session_id is not None
                and self._agent_clarification_service is not None
            ):
                await asyncio.to_thread(
                    self._agent_clarification_service.complete_resolved,
                    session_id=turn.session_id,
                    now=message.timestamp,
                )
            if reporter is not None:
                await reporter.finish_proposal_ready()
            await self._delivery.send_confirmation(
                proposal,
                idempotency_key=f"academic-discord-message:{message.message_id}:proposal:v1",
            )
            return DiscordMessageCallbackResult(status="handled")

        if outcome is AcademicAgentLoopOutcome.CLARIFICATION_REQUIRED:
            if (
                turn is not None
                and turn.session_id is not None
                and self._agent_clarification_service is not None
            ):
                completion = await asyncio.to_thread(
                    self._agent_clarification_service.persist_clarification_or_exhaust,
                    session_id=turn.session_id,
                    question=proposal.question or "Please clarify the academic request.",
                    now=message.timestamp,
                )
                if completion.status == "exhausted":
                    if reporter is not None:
                        await reporter.finish_failed()
                    await self._delivery.send_response(
                        completion.response
                        or (
                            "The automatic clarification limit was reached. "
                            "Start over with a new complete bot mention."
                        ),
                        idempotency_key=(
                            f"academic-discord-message:{message.message_id}:"
                            "clarification-exhausted:v1"
                        ),
                    )
                    return DiscordMessageCallbackResult(status="handled")
            if reporter is not None:
                await reporter.finish_waiting_for_clarification()
            await self._delivery.send_response(
                f"{proposal.question}\nReply with the requested details, or say cancel.",
                idempotency_key=(f"academic-discord-message:{message.message_id}:clarification:v1"),
            )
            return DiscordMessageCallbackResult(status="handled")

        if (
            turn is not None
            and turn.session_id is not None
            and self._agent_clarification_service is not None
        ):
            await asyncio.to_thread(
                self._agent_clarification_service.complete_failed,
                session_id=turn.session_id,
                now=message.timestamp,
            )
        if outcome is not None:
            return await self._send_agent_failure(
                message,
                reporter=reporter,
                response=_agent_failure_response(outcome),
                suffix=outcome.value,
            )

        await self._delivery.send_confirmation(
            proposal,
            idempotency_key=f"academic-discord-message:{message.message_id}:proposal:v1",
        )
        return DiscordMessageCallbackResult(status="handled")

    async def _handle_learning_memory(
        self,
        message: DiscordAcademicMessageCreate,
        semantic_request: str,
    ) -> Any:
        if self._memory_service is None:
            raise RuntimeError("academic memory service is not configured")
        review_handler = getattr(self._memory_service, "handle_memory_review", None)
        if review_handler is not None:
            review_result = await review_handler(
                external_event_id=message.message_id,
                channel_id=message.channel_id,
                user_id=message.author_id,
                raw_text=semantic_request,
                received_at=message.timestamp,
            )
            if review_result.status != "not_applicable":
                return review_result
        return await self._memory_service.handle_reflection(
            external_event_id=message.message_id,
            channel_id=message.channel_id,
            user_id=message.author_id,
            raw_text=semantic_request,
            received_at=message.timestamp,
        )

    def _without_assistant_mention(self, content: str) -> str:
        if self._assistant_user_id is None:
            return content.strip()
        return re.sub(
            rf"<@!?{re.escape(self._assistant_user_id)}>",
            " ",
            content,
        ).strip()

    def _assistant_was_mentioned(
        self,
        message: DiscordAcademicMessageCreate,
    ) -> bool:
        if self._assistant_user_id is None:
            return True
        return message.has_verified_mention(self._assistant_user_id)

    @staticmethod
    def _is_command_like(content: str) -> bool:
        return content.casefold().startswith(("confirm", "reject"))

    def _create_progress_reporter(
        self,
        message: DiscordAcademicMessageCreate,
        *,
        attempt_number: int,
    ) -> DiscordAcademicProgressReporter | None:
        factory = getattr(self._delivery, "create_progress_reporter", None)
        if factory is None:
            return None
        try:
            factory_kwargs: dict[str, object] = {
                "root_event_id": message.message_id,
                "attempt_number": attempt_number,
                "attempt_limit": 3,
                "edit_every_n_updates": 2,
            }
            if message.progress_message_id is not None:
                factory_kwargs["existing_message_id"] = message.progress_message_id
            reporter = factory(
                **factory_kwargs,
            )
        except (TypeError, ValueError):
            return None
        return reporter if isinstance(reporter, DiscordAcademicProgressReporter) else None

    @staticmethod
    def _create_agent_progress_sink(
        reporter: DiscordAcademicProgressReporter,
    ) -> AcademicAgentProgressSink:
        async def progress_sink(event: AcademicAgentProgressEvent) -> None:
            await reporter.update(event)
            if event.phase is AcademicAgentProgressPhase.MODEL_TURN and event.model_turn == 1:
                await reporter.flush()

        return progress_sink

    async def _reject_malformed_command(
        self,
        message: DiscordAcademicMessageCreate,
    ) -> DiscordMessageCallbackResult:
        await self._delivery.send_response(
            "Command not accepted. Use exactly `confirm <proposal-uuid>` or "
            "`reject <proposal-uuid>` from the authorized private channel.",
            idempotency_key=f"academic-command-invalid:{message.message_id}:v1",
        )
        return DiscordMessageCallbackResult(status="invalid")

    async def _send_runtime_unavailable(
        self,
        message: DiscordAcademicMessageCreate,
        *,
        reporter: DiscordAcademicProgressReporter | None = None,
    ) -> DiscordMessageCallbackResult:
        if reporter is not None:
            await reporter.finish_failed()
        await self._delivery.send_response(
            "Qwen is unavailable on this Mac; run scripts/ollama_qwen_start.sh and try again.",
            idempotency_key=f"academic-discord-message:{message.message_id}:ollama-unavailable:v1",
        )
        return DiscordMessageCallbackResult(status="failed")

    async def _send_agent_failure(
        self,
        message: DiscordAcademicMessageCreate,
        *,
        reporter: DiscordAcademicProgressReporter | None,
        response: str,
        suffix: str,
    ) -> DiscordMessageCallbackResult:
        if reporter is not None:
            await reporter.finish_failed()
        await self._delivery.send_response(
            response,
            idempotency_key=f"academic-discord-message:{message.message_id}:{suffix}:v1",
        )
        return DiscordMessageCallbackResult(status="failed")

    async def _handle_command(
        self,
        message: DiscordAcademicMessageCreate,
        action: AcademicCommandAction,
        proposal_id: uuid.UUID,
    ) -> DiscordMessageCallbackResult:
        event = f"{action} {proposal_id}"
        if action == "reject":
            result = reject_checkin_proposal(
                store=cast(Any, self._store),
                proposal_id=proposal_id,
                rejection_event=event,
                now=message.timestamp,
            )
            status = str(result["status"])
            if status in {"rejected", "already_rejected"}:
                response = f"Proposal {proposal_id} is rejected. No Notion change was made."
            else:
                response = f"Proposal {proposal_id} could not be rejected (state: {status})."
        else:
            writer = self._writer_provider()
            if writer is None:
                response = (
                    f"Proposal {proposal_id} was not applied: the scoped Notion writer is "
                    "not configured. Review the Notion mapping setup, then confirm again."
                )
            else:
                try:
                    result = await confirm_checkin_proposal(
                        store=cast(Any, self._store),
                        writer=writer,
                        proposal_id=proposal_id,
                        confirmation_event=event,
                        now=message.timestamp,
                    )
                    status = str(result["status"])
                    response = (
                        f"Proposal {proposal_id} applied."
                        if status == "applied"
                        else f"Proposal {proposal_id} was not applied (state: {status})."
                    )
                except Exception:
                    response = (
                        f"Proposal {proposal_id} could not be safely verified as applied. "
                        "The batch may be partially applied. No automatic retry was issued; "
                        "inspect academic connector health and the Notion course calendar."
                    )
        await self._delivery.send_response(
            response,
            idempotency_key=f"academic-discord-message:{message.message_id}:{action}:v1",
        )
        return DiscordMessageCallbackResult(status="handled")


def _agent_failure_response(outcome: AcademicAgentLoopOutcome) -> str:
    if outcome is AcademicAgentLoopOutcome.MODEL_TIMEOUT:
        return (
            "Qwen timed out before a safe proposal was ready. No change was prepared; "
            "send a new complete mention after the model recovers."
        )
    if outcome is AcademicAgentLoopOutcome.MODEL_FAILED:
        return (
            "Qwen failed before a safe proposal was ready. No change was prepared; "
            "send a new complete mention after checking model health."
        )
    if outcome is AcademicAgentLoopOutcome.MODEL_INVALID_OUTPUT:
        return (
            "Qwen returned an invalid structured response, so no proposal was created. "
            "Send a new complete mention to try again."
        )
    if outcome is AcademicAgentLoopOutcome.AGENT_TURN_LIMIT_EXHAUSTED:
        return (
            "The agent reached its ten-turn limit without a safe proposal. "
            "Send a new fully specified mention to start over."
        )
    return (
        "The request failed host safety validation, so no proposal was created. "
        "Review the synchronized academic data and send a new complete mention."
    )


__all__ = [
    "AcademicCommandAction",
    "AcademicDiscordCheckinHandler",
    "AcademicMemoryHandler",
    "AcademicOllamaRuntime",
    "DiscordCheckinDelivery",
    "DiscordCheckinStore",
    "WriterProvider",
    "parse_academic_command",
]
