"""Discord adapter restricted to deterministic operational failure alerts."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, cast
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.agents.finance.contracts import BriefingPayload
from app.agents.finance.delivery import render_discord_briefing
from app.core.config import DISCORD_API_BASE_URL, Settings
from app.core.errors import (
    ErrorCategory,
    ErrorCode,
    LifeAgentError,
    authorization_error,
    permanent_error,
    transient_error,
)
from app.db.code_review import review_idempotency_key
from app.db.models import Delivery, DeliveryStatus, RunStatus
from app.db.repositories import DeliveryRepository, RunRepository, utc_now
from app.health.checks import HealthState

_DISCORD_CONTENT_LIMIT = 2_000
_DISCORD_NONCE_LIMIT = 25


def _discord_nonce(delivery_id: UUID) -> str:
    """Encode a delivery UUID within Discord's 25-character nonce limit."""

    return delivery_id.hex[:_DISCORD_NONCE_LIMIT]


def _bounded_discord_content(content: str) -> str:
    if len(content) <= _DISCORD_CONTENT_LIMIT:
        return content
    marker = "\n[truncated]"
    return f"{content[: _DISCORD_CONTENT_LIMIT - len(marker)]}{marker}"


class FailureAlert(BaseModel):
    """Allowlisted facts used to construct an alert without private source text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    delivery_id: UUID
    run_id: UUID
    channel_id: str = Field(pattern=r"^[0-9]{5,24}$")
    component: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_-]+$")
    state: HealthState
    error_code: ErrorCode
    attempt: int = Field(ge=0)
    attempt_limit: int = Field(ge=0)

    @field_validator("state")
    @classmethod
    def state_requires_attention(cls, value: HealthState) -> HealthState:
        if value is HealthState.HEALTHY:
            raise ValueError("normal health must not generate Discord alerts")
        return value


class DiscordDeliveryReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    external_id: str
    permalink: str


class DiscordFailureAlertAdapter:
    """Send only failure/attention alerts to explicitly configured channels."""

    def __init__(
        self,
        *,
        token: SecretStr,
        allowed_channel_ids: set[str],
        base_url: str = DISCORD_API_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = token
        self._allowed_channel_ids = frozenset(allowed_channel_ids)
        self._base_url = base_url.rstrip("/")
        self._client = client

    async def send(self, alert: FailureAlert) -> DiscordDeliveryReceipt:
        if alert.channel_id not in self._allowed_channel_ids:
            raise ValueError("Discord alert target is not allowlisted")

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(10.0),
        )
        content = (
            f"LifeAgent {alert.state.value}: {alert.component} "
            f"({alert.error_code.value}); retry {alert.attempt} of {alert.attempt_limit}; "
            f"run {alert.run_id}"
        )
        try:
            response = await client.post(
                f"/channels/{alert.channel_id}/messages",
                headers={"Authorization": f"Bot {self._token.get_secret_value()}"},
                json={
                    "content": content,
                    "nonce": _discord_nonce(alert.delivery_id),
                    "enforce_nonce": True,
                    "allowed_mentions": {"parse": []},
                },
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {401, 403}:
                    raise authorization_error("Discord alert authorization is invalid") from None
                if exc.response.status_code == 429 or exc.response.status_code >= 500:
                    raise transient_error(
                        ErrorCode.CONNECTOR_TRANSIENT,
                        "Discord alert endpoint is temporarily unavailable",
                    ) from None
                raise permanent_error(
                    ErrorCode.INPUT_INVALID,
                    "Discord rejected the failure alert request",
                ) from None
            try:
                payload = response.json()
            except ValueError:
                raise transient_error(
                    ErrorCode.CONNECTOR_TRANSIENT,
                    "Discord returned an invalid alert receipt",
                ) from None
            external_id = payload.get("id")
            if not isinstance(external_id, str) or not external_id.isdigit():
                raise transient_error(
                    ErrorCode.CONNECTOR_TRANSIENT,
                    "Discord returned an invalid alert receipt",
                )
            guild_id = payload.get("guild_id")
            server = guild_id if isinstance(guild_id, str) and guild_id.isdigit() else "@me"
            return DiscordDeliveryReceipt(
                external_id=external_id,
                permalink=f"https://discord.com/channels/{server}/{alert.channel_id}/{external_id}",
            )
        except httpx.TransportError:
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT,
                "Discord alert transport is unavailable",
            ) from None
        finally:
            if owns_client:
                await client.aclose()


_REVIEW_FINDING_KEYS: frozenset[str] = frozenset({"block", "important", "suggestion"})
_REVIEW_DELIVERY_CHANNEL = "discord"


class ReviewSummary(BaseModel):
    """Allowlisted code-review facts safe to post: counts and identities only.

    No finding titles, explanations, patch text, or scanner output ever cross
    this boundary -- the model carries identifiers and integer counts so the
    rendered message cannot leak private source or analysis text.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    delivery_id: UUID
    run_id: UUID
    channel_id: str = Field(pattern=r"^[0-9]{5,24}$")
    repository: str = Field(pattern=r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
    head_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    risk: Literal["high", "medium", "low"]
    status: Literal["succeeded", "attention", "failed"]
    finding_counts: Mapping[str, int]
    report_artifact_key: str | None = None

    @field_validator("finding_counts")
    @classmethod
    def counts_are_allowlisted_and_nonnegative(cls, value: Mapping[str, int]) -> Mapping[str, int]:
        unknown = sorted(set(value) - _REVIEW_FINDING_KEYS)
        if unknown:
            raise ValueError(f"finding_counts has keys outside the review taxonomy: {unknown}")
        if any(count < 0 for count in value.values()):
            raise ValueError("finding_counts must not be negative")
        return value

    def message_content(self) -> str:
        """Render the outbound message body from counts and identities only."""

        counts = " ".join(
            f"{key}={self.finding_counts.get(key, 0)}"
            for key in ("block", "important", "suggestion")
        )
        content = (
            f"LifeAgent code review {self.status} for {self.repository} "
            f"at {self.head_sha}: risk {self.risk}; findings {counts}; run {self.run_id}"
        )
        if self.report_artifact_key is not None:
            content += f"; report {self.report_artifact_key}"
        return content


class DiscordReviewSummaryAdapter:
    """Post code-review summaries to explicitly configured channels only."""

    def __init__(
        self,
        *,
        token: SecretStr,
        allowed_channel_ids: set[str],
        base_url: str = DISCORD_API_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = token
        self._allowed_channel_ids = frozenset(allowed_channel_ids)
        self._base_url = base_url.rstrip("/")
        self._client = client

    async def send(self, summary: ReviewSummary) -> DiscordDeliveryReceipt:
        if summary.channel_id not in self._allowed_channel_ids:
            raise ValueError("Discord review summary target is not allowlisted")

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(10.0),
        )
        try:
            response = await client.post(
                f"/channels/{summary.channel_id}/messages",
                headers={"Authorization": f"Bot {self._token.get_secret_value()}"},
                json={
                    "content": summary.message_content(),
                    "nonce": _discord_nonce(summary.delivery_id),
                    "enforce_nonce": True,
                    "allowed_mentions": {"parse": []},
                },
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {401, 403}:
                    raise authorization_error(
                        "Discord review summary authorization is invalid"
                    ) from None
                if exc.response.status_code == 429 or exc.response.status_code >= 500:
                    raise transient_error(
                        ErrorCode.CONNECTOR_TRANSIENT,
                        "Discord review summary endpoint is temporarily unavailable",
                    ) from None
                raise permanent_error(
                    ErrorCode.INPUT_INVALID,
                    "Discord rejected the review summary request",
                ) from None
            try:
                payload = response.json()
            except ValueError:
                raise transient_error(
                    ErrorCode.CONNECTOR_TRANSIENT,
                    "Discord returned an invalid review summary receipt",
                ) from None
            external_id = payload.get("id")
            if not isinstance(external_id, str) or not external_id.isdigit():
                raise transient_error(
                    ErrorCode.CONNECTOR_TRANSIENT,
                    "Discord returned an invalid review summary receipt",
                )
            guild_id = payload.get("guild_id")
            server = guild_id if isinstance(guild_id, str) and guild_id.isdigit() else "@me"
            return DiscordDeliveryReceipt(
                external_id=external_id,
                permalink=(
                    f"https://discord.com/channels/{server}/{summary.channel_id}/{external_id}"
                ),
            )
        except httpx.TransportError:
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT,
                "Discord review summary transport is unavailable",
            ) from None
        finally:
            if owns_client:
                await client.aclose()


class DailyReviewSummary(BaseModel):
    """Bounded nightly-report identity; report contents remain in the artifact store."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    delivery_id: UUID
    run_id: UUID
    channel_id: str = Field(pattern=r"^[0-9]{5,24}$")
    report_date: date
    report_artifact_key: str = Field(pattern=r"^[0-9a-f]{64}$")

    def message_content(self) -> str:
        return (
            f"LifeAgent daily code review for {self.report_date.isoformat()}; "
            f"report {self.report_artifact_key}; run {self.run_id}"
        )


class DiscordDailyReviewAdapter:
    """Post a nightly artifact reference with an enforced delivery nonce."""

    def __init__(
        self,
        *,
        token: SecretStr,
        allowed_channel_ids: set[str],
        base_url: str = DISCORD_API_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = token
        self._allowed_channel_ids = frozenset(allowed_channel_ids)
        self._base_url = base_url.rstrip("/")
        self._client = client

    async def send(self, summary: DailyReviewSummary) -> DiscordDeliveryReceipt:
        if summary.channel_id not in self._allowed_channel_ids:
            raise ValueError("Discord daily-review target is not allowlisted")
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(10.0),
        )
        try:
            response = await client.post(
                f"/channels/{summary.channel_id}/messages",
                headers={"Authorization": f"Bot {self._token.get_secret_value()}"},
                json={
                    "content": summary.message_content(),
                    "nonce": _discord_nonce(summary.delivery_id),
                    "enforce_nonce": True,
                    "allowed_mentions": {"parse": []},
                },
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {401, 403}:
                    raise authorization_error(
                        "Discord daily-review authorization is invalid"
                    ) from None
                if exc.response.status_code == 429 or exc.response.status_code >= 500:
                    raise transient_error(
                        ErrorCode.CONNECTOR_TRANSIENT,
                        "Discord daily-review endpoint is temporarily unavailable",
                    ) from None
                raise permanent_error(
                    ErrorCode.INPUT_INVALID,
                    "Discord rejected the daily-review request",
                ) from None
            try:
                payload = response.json()
            except ValueError:
                raise transient_error(
                    ErrorCode.CONNECTOR_TRANSIENT,
                    "Discord returned an invalid daily-review receipt",
                ) from None
            external_id = payload.get("id")
            if not isinstance(external_id, str) or not external_id.isdigit():
                raise transient_error(
                    ErrorCode.CONNECTOR_TRANSIENT,
                    "Discord returned an invalid daily-review receipt",
                )
            guild_id = payload.get("guild_id")
            server = guild_id if isinstance(guild_id, str) and guild_id.isdigit() else "@me"
            return DiscordDeliveryReceipt(
                external_id=external_id,
                permalink=f"https://discord.com/channels/{server}/{summary.channel_id}/{external_id}",
            )
        except httpx.TransportError:
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT,
                "Discord daily-review transport is unavailable",
            ) from None
        finally:
            if owns_client:
                await client.aclose()


class AcademicDiscordMessage(BaseModel):
    """Bounded academic planner message with no source-document payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    delivery_id: UUID
    channel_id: str = Field(pattern=r"^[0-9]{5,24}$")
    content: str = Field(min_length=1, max_length=2_000)


class AcademicClarificationMessage(BaseModel):
    """Bounded assessment-type clarification with opaque button identifiers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    delivery_id: UUID
    clarification_id: UUID
    channel_id: str = Field(pattern=r"^[0-9]{5,24}$")
    current_title: str = Field(min_length=1, max_length=500)
    quiz_title_preview: str = Field(min_length=1, max_length=500)
    assignment_title_preview: str = Field(min_length=1, max_length=500)

    def message_content(self) -> str:
        content = (
            "Please classify this Notion assessment before any title change.\n"
            f"Current title: {self.current_title}\n"
            f"Quiz preview: {self.quiz_title_preview}\n"
            f"Assignment preview: {self.assignment_title_preview}"
        )
        return _bounded_discord_content(content)

    def components(self) -> list[dict[str, object]]:
        return [
            {
                "type": 1,
                "components": [
                    {
                        "type": 2,
                        "style": 1,
                        "label": "Quiz",
                        "custom_id": f"academic_clarify:{self.clarification_id}:quiz",
                    },
                    {
                        "type": 2,
                        "style": 1,
                        "label": "Assignment",
                        "custom_id": f"academic_clarify:{self.clarification_id}:assignment",
                    },
                    {
                        "type": 2,
                        "style": 2,
                        "label": "Ignore",
                        "custom_id": f"academic_clarify:{self.clarification_id}:ignore",
                    },
                ],
            }
        ]


class AcademicSetupReminderMessage(BaseModel):
    """Bounded setup diagnostic with no interactive components."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    delivery_id: UUID
    channel_id: str = Field(pattern=r"^[0-9]{5,24}$")
    condition: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9 ._:/()-]+$")
    affected_course_codes: tuple[str, ...] = Field(default=(), max_length=10)

    @field_validator("affected_course_codes")
    @classmethod
    def course_codes_are_bounded(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for code in value:
            if len(code) > 40 or not re.fullmatch(r"[A-Za-z0-9._ -]+", code):
                raise ValueError("affected course codes must be bounded and non-secret")
        return value

    def message_content(self) -> str:
        lines = [
            f"LifeAgent academic setup needs attention: {self.condition}.",
            (
                "Please share/configure the Courses database and create course pages from "
                "the New Course template."
            ),
            "No Notion changes were made.",
        ]
        if self.affected_course_codes:
            lines.append("Affected courses: " + ", ".join(self.affected_course_codes))
        return _bounded_discord_content("\n".join(lines))


class DiscordAcademicPlannerAdapter:
    """Send planner/check-in messages with a persisted UUID nonce."""

    def __init__(
        self,
        *,
        token: SecretStr,
        allowed_channel_ids: set[str],
        base_url: str = DISCORD_API_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = token
        self._allowed_channel_ids = frozenset(allowed_channel_ids)
        self._base_url = base_url.rstrip("/")
        self._client = client

    async def send(self, message: AcademicDiscordMessage) -> DiscordDeliveryReceipt:
        if message.channel_id not in self._allowed_channel_ids:
            raise ValueError("Discord academic target is not allowlisted")
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(10.0),
        )
        try:
            response = await client.post(
                f"/channels/{message.channel_id}/messages",
                headers={"Authorization": f"Bot {self._token.get_secret_value()}"},
                json={
                    "content": message.content,
                    "nonce": _discord_nonce(message.delivery_id),
                    "enforce_nonce": True,
                    "allowed_mentions": {"parse": []},
                },
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {401, 403}:
                    raise authorization_error("Discord academic authorization is invalid") from None
                if exc.response.status_code == 429 or exc.response.status_code >= 500:
                    raise transient_error(
                        ErrorCode.CONNECTOR_TRANSIENT,
                        "Discord academic endpoint is temporarily unavailable",
                    ) from None
                raise permanent_error(
                    ErrorCode.INPUT_INVALID,
                    "Discord rejected the academic message request",
                ) from None
            try:
                payload = response.json()
            except ValueError:
                raise transient_error(
                    ErrorCode.CONNECTOR_TRANSIENT,
                    "Discord returned an invalid academic receipt",
                ) from None
            external_id = payload.get("id")
            if not isinstance(external_id, str) or not external_id.isdigit():
                raise transient_error(
                    ErrorCode.CONNECTOR_TRANSIENT,
                    "Discord returned an invalid academic receipt",
                )
            guild_id = payload.get("guild_id")
            server = guild_id if isinstance(guild_id, str) and guild_id.isdigit() else "@me"
            return DiscordDeliveryReceipt(
                external_id=external_id,
                permalink=f"https://discord.com/channels/{server}/{message.channel_id}/{external_id}",
            )
        except httpx.TransportError:
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT,
                "Discord academic transport is unavailable",
            ) from None
        finally:
            if owns_client:
                await client.aclose()

    async def send_clarification(
        self,
        message: AcademicClarificationMessage,
    ) -> DiscordDeliveryReceipt:
        if message.channel_id not in self._allowed_channel_ids:
            raise ValueError("Discord academic clarification target is not allowlisted")
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(10.0),
        )
        try:
            response = await client.post(
                f"/channels/{message.channel_id}/messages",
                headers={"Authorization": f"Bot {self._token.get_secret_value()}"},
                json={
                    "content": message.message_content(),
                    "nonce": _discord_nonce(message.delivery_id),
                    "enforce_nonce": True,
                    "allowed_mentions": {"parse": []},
                    "components": message.components(),
                },
            )
            return _academic_receipt_from_response(
                response,
                channel_id=message.channel_id,
                authorization_message="Discord academic clarification authorization is invalid",
                transient_message=(
                    "Discord academic clarification endpoint is temporarily unavailable"
                ),
                rejected_message="Discord rejected the academic clarification request",
                invalid_receipt_message=(
                    "Discord returned an invalid academic clarification receipt"
                ),
            )
        except httpx.TransportError:
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT,
                "Discord academic clarification transport is unavailable",
            ) from None
        finally:
            if owns_client:
                await client.aclose()

    async def send_setup_reminder(
        self,
        message: AcademicSetupReminderMessage,
    ) -> DiscordDeliveryReceipt:
        if message.channel_id not in self._allowed_channel_ids:
            raise ValueError("Discord academic setup reminder target is not allowlisted")
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(10.0),
        )
        try:
            response = await client.post(
                f"/channels/{message.channel_id}/messages",
                headers={"Authorization": f"Bot {self._token.get_secret_value()}"},
                json={
                    "content": message.message_content(),
                    "nonce": _discord_nonce(message.delivery_id),
                    "enforce_nonce": True,
                    "allowed_mentions": {"parse": []},
                    "components": [],
                },
            )
            return _academic_receipt_from_response(
                response,
                channel_id=message.channel_id,
                authorization_message="Discord academic setup reminder authorization is invalid",
                transient_message=(
                    "Discord academic setup reminder endpoint is temporarily unavailable"
                ),
                rejected_message="Discord rejected the academic setup reminder request",
                invalid_receipt_message=(
                    "Discord returned an invalid academic setup reminder receipt"
                ),
            )
        except httpx.TransportError:
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT,
                "Discord academic setup reminder transport is unavailable",
            ) from None
        finally:
            if owns_client:
                await client.aclose()


class DiscordAcademicPlannerDelivery:
    """Workflow delivery implementation with one intent per planner message."""

    def __init__(
        self,
        *,
        engine: Engine,
        run_id: UUID,
        channel_id: str,
        adapter: DiscordAcademicPlannerAdapter,
    ) -> None:
        self._engine = engine
        self._run_id = run_id
        self._channel_id = channel_id
        self._adapter = adapter

    async def _send(self, content: str, idempotency_key: str) -> Delivery:
        return await deliver_academic_message(
            engine=self._engine,
            run_id=self._run_id,
            channel_id=self._channel_id,
            content=content,
            idempotency_key=idempotency_key,
            adapter=self._adapter,
        )

    async def send_morning_plan(self, plan: Any, *, idempotency_key: str) -> Delivery:
        blocks = list(getattr(plan, "blocks", ()))
        practice = [block for block in blocks if getattr(block, "block_kind", None) == "practice"]
        assessments = [block for block in blocks if block not in practice][:3]
        lines = ["Today's academic plan:"]
        if practice:
            lines.append("Separate practice blocks:")
            lines.extend(_academic_block_line(block) for block in practice)
        if assessments:
            lines.append("Highest-value assessment blocks:")
            lines.extend(_academic_block_line(block) for block in assessments)
        deferred_practice = tuple(getattr(plan, "deferred_practice_focus_ids", ()))
        if deferred_practice:
            lines.append(
                f"Could not fit {len(deferred_practice)} required practice block(s) before "
                "their review time; add availability so they can be scheduled."
            )
        if not practice and not assessments:
            lines.append("- No schedulable blocks were found.")
        return await self._send("\n".join(lines), idempotency_key)

    async def send_checkin(self, *, plan: Any | None, idempotency_key: str) -> Delivery:
        content = (
            "End-of-day check-in:\n"
            "1. What progress was made on each planned to-do?\n"
            "2. How did each test or quiz today go?\n"
            "3. Any new tasks, deadlines, tests, or events for Notion?"
        )
        return await self._send(content, idempotency_key)

    async def send_focus_reviews(
        self,
        reviews: tuple[dict[str, Any], ...],
        *,
        idempotency_key: str,
    ) -> Delivery:
        lines = ["Learning-focus follow-up:"]
        for review in reviews:
            label = " ".join(
                part for part in (review.get("course_code"), review.get("topic")) if part
            )
            focus_id = review.get("focus_id")
            kind = review.get("kind")
            count = int(review.get("reminder_count", 0))
            delete_after = int(review.get("delete_after_reminders", 5))
            if kind == "review":
                lines.append(
                    f"- Do you still need practice on {label}? Reply yes to keep it active "
                    f"or no to delete it. (focus {focus_id})"
                )
            elif kind == "remind":
                lines.append(
                    f"- Reminder {count}/{delete_after}: do you still need practice on {label}? "
                    f"(focus {focus_id})"
                )
            elif kind == "snoozed_and_remind":
                lines.append(
                    f"- Reminder {count}/{delete_after}: {label} is snoozed, so no new "
                    "practice block "
                    f"will be scheduled until you reply. (focus {focus_id})"
                )
            elif kind == "deleted":
                lines.append(
                    f"- Deleted {label} and its reflection memory after {delete_after} "
                    "unanswered daily reminders."
                )
        return await self._send(_bounded_discord_content("\n".join(lines)), idempotency_key)

    async def send_ambiguity_question(self, fact: Any, *, idempotency_key: str) -> Delivery:
        content = f"Please confirm this academic fact before scheduling: {fact.question}"
        return await self._send(content, idempotency_key)

    async def send_confirmation(self, proposal: Any, *, idempotency_key: str) -> Delivery:
        content = _academic_proposal_preview(proposal)
        return await self._send(content, idempotency_key)


def _academic_block_line(block: Any) -> str:
    return (
        f"- {block.title} ({block.start_at.isoformat()}-{block.end_at.isoformat()}): "
        f"{block.rationale}"
    )


class DiscordAcademicResponseDelivery:
    """Create one durable run and delivery intent per inbound academic response."""

    def __init__(
        self,
        *,
        engine: Engine,
        channel_id: str,
        adapter: DiscordAcademicPlannerAdapter,
    ) -> None:
        self._engine = engine
        self._channel_id = channel_id
        self._adapter = adapter

    async def send_confirmation(self, proposal: Any, *, idempotency_key: str) -> Delivery:
        return await self.send_response(
            _academic_proposal_preview(proposal),
            idempotency_key=idempotency_key,
        )

    async def send_response(self, content: str, *, idempotency_key: str) -> Delivery:
        run_id = await asyncio.to_thread(
            _academic_response_run_id,
            self._engine,
            idempotency_key,
        )
        try:
            delivery = await deliver_academic_message(
                engine=self._engine,
                run_id=run_id,
                channel_id=self._channel_id,
                content=_bounded_discord_content(content),
                idempotency_key=idempotency_key,
                adapter=self._adapter,
            )
        except LifeAgentError as exc:
            await asyncio.to_thread(
                _finish_academic_response_run,
                self._engine,
                run_id,
                RunStatus.ATTENTION
                if exc.record.category is ErrorCategory.TRANSIENT
                else RunStatus.FAILED,
                exc.record.code.value,
            )
            raise
        await asyncio.to_thread(
            _finish_academic_response_run,
            self._engine,
            run_id,
            RunStatus.SUCCEEDED,
            None,
        )
        return delivery


def _academic_proposal_preview(proposal: Any) -> str:
    proposal_id = str(proposal.proposal_id)
    changes = tuple(proposal.changes)[:20]
    if not changes:
        question = str(proposal.question or "Please use a supported check-in format.")[:500]
        return _bounded_discord_content(
            f"Academic check-in needs clarification (proposal {proposal_id}).\n{question}\n"
            "Supported forms: completed <assessment-id> or "
            "logged <assessment-id> <minutes>. No Notion change is ready to confirm."
        )
    lines = [f"Proposed academic updates (proposal {proposal_id}):"]
    for change in changes:
        if change.field == "create_assessment":
            kind = getattr(change.assessment_type, "value", change.assessment_type) or "event"
            course = change.course_code or change.course_id or "the selected course"
            due = change.due_at.isoformat() if change.due_at is not None else "an unset date"
            lines.append(f"- Create {kind} `{change.title}` in {course}, due {due}.")
            continue
        if change.field == "update_assessment":
            updates: list[str] = []
            if change.title is not None:
                updates.append(f"title to `{change.title}`")
            if change.due_at is not None:
                updates.append(f"due date to {change.due_at.isoformat()}")
            lines.append(
                f"- Update `{change.expected_title or change.assessment_id}`: "
                + ", ".join(updates)
                + "."
            )
            continue
        if change.field == "archive_assessment":
            lines.append(
                f"- Archive `{change.expected_title or change.assessment_id}` (Notion delete)."
            )
            continue
        target = f" for {change.assessment_id}" if change.assessment_id is not None else ""
        lines.append(f"- {change.field}{target}: {change.value}")
    expiry = getattr(proposal, "expires_at", None)
    if expiry is not None:
        lines.append(f"Expires: {expiry.isoformat()}")
    lines.extend(
        (
            f"Confirm exactly: confirm {proposal_id}",
            f"Reject exactly: reject {proposal_id}",
        )
    )
    return _bounded_discord_content("\n".join(lines))


class FinanceDiscordBriefingMessage(BaseModel):
    """Rendered finance briefing text safe for a single Discord message."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    delivery_id: UUID
    run_id: UUID
    channel_id: str = Field(pattern=r"^[0-9]{5,24}$")
    content: str = Field(min_length=1, max_length=_DISCORD_CONTENT_LIMIT)


class DiscordFinanceBriefingAdapter:
    """Post finance briefing summaries to explicitly configured channels only."""

    def __init__(
        self,
        *,
        token: SecretStr,
        allowed_channel_ids: set[str],
        base_url: str = DISCORD_API_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = token
        self._allowed_channel_ids = frozenset(allowed_channel_ids)
        self._base_url = base_url.rstrip("/")
        self._client = client

    async def send(self, message: FinanceDiscordBriefingMessage) -> DiscordDeliveryReceipt:
        if message.channel_id not in self._allowed_channel_ids:
            raise ValueError("Discord finance target is not allowlisted")
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(10.0),
        )
        try:
            response = await client.post(
                f"/channels/{message.channel_id}/messages",
                headers={"Authorization": f"Bot {self._token.get_secret_value()}"},
                json={
                    "content": message.content,
                    "nonce": _discord_nonce(message.delivery_id),
                    "enforce_nonce": True,
                    "allowed_mentions": {"parse": []},
                },
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {401, 403}:
                    raise authorization_error("Discord finance authorization is invalid") from None
                if exc.response.status_code == 429 or exc.response.status_code >= 500:
                    raise transient_error(
                        ErrorCode.CONNECTOR_TRANSIENT,
                        "Discord finance endpoint is temporarily unavailable",
                    ) from None
                raise permanent_error(
                    ErrorCode.INPUT_INVALID,
                    "Discord rejected the finance briefing request",
                ) from None
            try:
                payload = response.json()
            except ValueError:
                raise transient_error(
                    ErrorCode.CONNECTOR_TRANSIENT,
                    "Discord returned an invalid finance receipt",
                ) from None
            external_id = payload.get("id")
            if not isinstance(external_id, str) or not external_id.isdigit():
                raise transient_error(
                    ErrorCode.CONNECTOR_TRANSIENT,
                    "Discord returned an invalid finance receipt",
                )
            guild_id = payload.get("guild_id")
            server = guild_id if isinstance(guild_id, str) and guild_id.isdigit() else "@me"
            return DiscordDeliveryReceipt(
                external_id=external_id,
                permalink=(
                    f"https://discord.com/channels/{server}/{message.channel_id}/{external_id}"
                ),
            )
        except httpx.TransportError:
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT,
                "Discord finance transport is unavailable",
            ) from None
        finally:
            if owns_client:
                await client.aclose()


class DiscordFinanceBriefingDelivery:
    """Workflow delivery implementation with one intent per finance briefing."""

    def __init__(
        self,
        *,
        engine: Engine,
        run_id: UUID,
        channel_id: str,
        adapter: DiscordFinanceBriefingAdapter,
    ) -> None:
        self._engine = engine
        self._run_id = run_id
        self._channel_id = channel_id
        self._adapter = adapter

    async def send_briefing(
        self,
        payload: BriefingPayload,
        *,
        idempotency_key: str,
    ) -> Delivery:
        return await deliver_finance_briefing(
            engine=self._engine,
            run_id=self._run_id,
            channel_id=self._channel_id,
            payload=payload,
            idempotency_key=idempotency_key,
            adapter=self._adapter,
        )


async def deliver_academic_message(
    *,
    engine: Engine,
    run_id: UUID,
    channel_id: str,
    content: str,
    idempotency_key: str,
    adapter: DiscordAcademicPlannerAdapter,
) -> Delivery:
    """Open an academic delivery intent, send once, and persist its receipt."""

    intent = await asyncio.to_thread(
        _open_review_intent,
        engine,
        run_id=run_id,
        target=channel_id,
        key=idempotency_key,
    )
    if intent.already_delivered:
        return intent.delivery
    message = AcademicDiscordMessage(
        delivery_id=intent.delivery.id,
        channel_id=channel_id,
        content=content,
    )
    try:
        receipt = await adapter.send(message)
    except LifeAgentError as exc:
        status = (
            DeliveryStatus.UNCERTAIN
            if exc.record.category is ErrorCategory.TRANSIENT
            else DeliveryStatus.FAILED
        )
        code = (
            ErrorCode.DELIVERY_UNCERTAIN.value
            if status is DeliveryStatus.UNCERTAIN
            else exc.record.code.value
        )
        await asyncio.to_thread(
            _record_review_attempt,
            engine,
            delivery_id=intent.delivery.id,
            status=status,
            external_url=None,
            error_code=code,
        )
        raise
    return await asyncio.to_thread(
        _record_review_attempt,
        engine,
        delivery_id=intent.delivery.id,
        status=DeliveryStatus.SENT,
        external_url=receipt.permalink,
        error_code=None,
    )


async def deliver_finance_briefing(
    *,
    engine: Engine,
    run_id: UUID,
    channel_id: str,
    payload: BriefingPayload,
    idempotency_key: str,
    adapter: DiscordFinanceBriefingAdapter | None = None,
) -> Delivery:
    """Deliver one finance briefing through a durable, idempotent intent."""

    intent = await asyncio.to_thread(
        _open_review_intent,
        engine,
        run_id=run_id,
        target=channel_id,
        key=idempotency_key,
    )
    if intent.already_delivered:
        return intent.delivery
    try:
        _validate_finance_idempotency_key(payload, idempotency_key)
        if adapter is None:
            adapter = _finance_adapter_from_settings(channel_id)
        message = FinanceDiscordBriefingMessage(
            delivery_id=intent.delivery.id,
            run_id=run_id,
            channel_id=channel_id,
            content=_finance_briefing_content(payload),
        )
        receipt = await adapter.send(message)
    except LifeAgentError as exc:
        status = (
            DeliveryStatus.UNCERTAIN
            if exc.record.category is ErrorCategory.TRANSIENT
            else DeliveryStatus.FAILED
        )
        error_code = (
            ErrorCode.DELIVERY_UNCERTAIN.value
            if status is DeliveryStatus.UNCERTAIN
            else exc.record.code.value
        )
        await asyncio.to_thread(
            _record_review_attempt,
            engine,
            delivery_id=intent.delivery.id,
            status=status,
            external_url=None,
            error_code=error_code,
        )
        raise
    except ValueError:
        await asyncio.to_thread(
            _record_review_attempt,
            engine,
            delivery_id=intent.delivery.id,
            status=DeliveryStatus.FAILED,
            external_url=None,
            error_code=ErrorCode.INPUT_INVALID.value,
        )
        raise
    return await asyncio.to_thread(
        _record_review_attempt,
        engine,
        delivery_id=intent.delivery.id,
        status=DeliveryStatus.SENT,
        external_url=receipt.permalink,
        error_code=None,
    )


@dataclass(frozen=True, slots=True)
class _ReviewIntent:
    delivery: Delivery
    already_delivered: bool


def _academic_response_run_id(engine: Engine, idempotency_key: str) -> UUID:
    with Session(engine) as session, session.begin():
        run = RunRepository.create_or_get(
            session,
            idempotency_key=f"{idempotency_key}:run",
            agent_name="academic_planner",
            trigger="discord_message",
            input_version="discord-academic-message-v1",
        )
        return run.id


def _finish_academic_response_run(
    engine: Engine,
    run_id: UUID,
    status: RunStatus,
    error_code: str | None,
) -> None:
    with Session(engine) as session, session.begin():
        RunRepository.set_status(
            session,
            run_id,
            status,
            summary="Academic Discord response delivery completed.",
            error_code=error_code,
        )


def _open_review_intent(engine: Engine, *, run_id: UUID, target: str, key: str) -> _ReviewIntent:
    with Session(engine) as session, session.begin():
        delivery = DeliveryRepository.create_or_get_intent(
            session,
            channel=_REVIEW_DELIVERY_CHANNEL,
            target=target,
            idempotency_key=key,
            run_id=run_id,
        )
        already = delivery.status in {DeliveryStatus.SENT, DeliveryStatus.ACKNOWLEDGED}
        if not already:
            delivery.status = DeliveryStatus.SENDING
            delivery.updated_at = utc_now()
            session.flush()
        session.expunge(delivery)
        return _ReviewIntent(delivery=delivery, already_delivered=already)


def _record_review_attempt(
    engine: Engine,
    *,
    delivery_id: UUID,
    status: DeliveryStatus,
    external_url: str | None,
    error_code: str | None,
) -> Delivery:
    with Session(engine) as session, session.begin():
        delivery = DeliveryRepository.record_attempt(
            session,
            delivery_id,
            status,
            external_url=external_url,
            error_code=error_code,
        )
        session.expunge(delivery)
        return delivery


def _academic_receipt_from_response(
    response: httpx.Response,
    *,
    channel_id: str,
    authorization_message: str,
    transient_message: str,
    rejected_message: str,
    invalid_receipt_message: str,
) -> DiscordDeliveryReceipt:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {401, 403}:
            raise authorization_error(authorization_message) from None
        if exc.response.status_code == 429 or exc.response.status_code >= 500:
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT,
                transient_message,
            ) from None
        raise permanent_error(
            ErrorCode.INPUT_INVALID,
            rejected_message,
        ) from None
    try:
        payload = response.json()
    except ValueError:
        raise transient_error(
            ErrorCode.CONNECTOR_TRANSIENT,
            invalid_receipt_message,
        ) from None
    external_id = payload.get("id")
    if not isinstance(external_id, str) or not external_id.isdigit():
        raise transient_error(
            ErrorCode.CONNECTOR_TRANSIENT,
            invalid_receipt_message,
        )
    guild_id = payload.get("guild_id")
    server = guild_id if isinstance(guild_id, str) and guild_id.isdigit() else "@me"
    return DiscordDeliveryReceipt(
        external_id=external_id,
        permalink=f"https://discord.com/channels/{server}/{channel_id}/{external_id}",
    )


def _review_adapter_from_settings() -> DiscordReviewSummaryAdapter:
    settings = Settings()
    token = settings.discord_bot_token
    if token is None:
        raise authorization_error("Discord bot token is not configured")
    allowed = set(settings.discord_target_channels)
    if settings.discord_code_review_channel_id is not None:
        allowed.add(settings.discord_code_review_channel_id)
    return DiscordReviewSummaryAdapter(
        token=token,
        allowed_channel_ids=allowed,
        base_url=settings.discord_api_url,
    )


def _failure_alert_adapter_from_settings(channel_id: str) -> DiscordFailureAlertAdapter:
    settings = Settings()
    token = settings.discord_bot_token
    if token is None:
        raise authorization_error("Discord bot token is not configured")
    allowed = set(settings.discord_target_channels)
    allowed.add(channel_id)
    return DiscordFailureAlertAdapter(
        token=token,
        allowed_channel_ids=allowed,
        base_url=settings.discord_api_url,
    )


def _finance_adapter_from_settings(channel_id: str) -> DiscordFinanceBriefingAdapter:
    settings = Settings()
    token = settings.discord_bot_token
    if token is None:
        raise authorization_error("Discord bot token is not configured")
    if settings.discord_finance_channel_id != channel_id:
        raise authorization_error("Discord finance channel is not configured")
    return DiscordFinanceBriefingAdapter(
        token=token,
        allowed_channel_ids={channel_id},
        base_url=settings.discord_api_url,
    )


def _validate_finance_idempotency_key(
    payload: BriefingPayload,
    idempotency_key: str,
) -> None:
    expected = f"finance:{payload.generated_at.date().isoformat()}:market-open:v1"
    if idempotency_key != expected:
        raise ValueError("finance Discord idempotency key does not match briefing date")


def _finance_briefing_content(payload: BriefingPayload) -> str:
    content = render_discord_briefing(payload)
    if len(content) <= _DISCORD_CONTENT_LIMIT:
        return content
    marker = "\n[truncated; see persisted finance briefing payload]"
    return f"{content[: _DISCORD_CONTENT_LIMIT - len(marker)]}{marker}"


async def deliver_review_summary(
    *,
    engine: Engine,
    run_id: UUID,
    channel_id: str,
    repository: str,
    head_sha: str,
    risk: str,
    status: str,
    finding_counts: Mapping[str, int],
    report_artifact_key: str | None,
    adapter: DiscordReviewSummaryAdapter | None = None,
) -> Delivery:
    """Idempotently deliver one code-review summary, keyed by repository/SHA.

    The persisted delivery UUID is the enforced Discord nonce.  A summary whose
    intent is already ``SENT``/``ACKNOWLEDGED`` is returned without posting.  A
    transient/transport failure records ``UNCERTAIN`` (Discord may have accepted
    the message); an authorization or permanent failure records ``FAILED`` with
    a safe error code.  The originating error is always re-raised.
    """

    key = review_idempotency_key(repository, head_sha)
    intent = await asyncio.to_thread(
        _open_review_intent, engine, run_id=run_id, target=channel_id, key=key
    )
    if intent.already_delivered:
        return intent.delivery

    delivery_id = intent.delivery.id
    summary = ReviewSummary(
        delivery_id=delivery_id,
        run_id=run_id,
        channel_id=channel_id,
        repository=repository,
        head_sha=head_sha,
        risk=cast('Literal["high", "medium", "low"]', risk),
        status=cast('Literal["succeeded", "attention", "failed"]', status),
        finding_counts=finding_counts,
        report_artifact_key=report_artifact_key,
    )
    resolved_adapter = adapter or _review_adapter_from_settings()

    try:
        receipt = await resolved_adapter.send(summary)
    except LifeAgentError as exc:
        if exc.record.category is ErrorCategory.TRANSIENT:
            await asyncio.to_thread(
                _record_review_attempt,
                engine,
                delivery_id=delivery_id,
                status=DeliveryStatus.UNCERTAIN,
                external_url=None,
                error_code=ErrorCode.DELIVERY_UNCERTAIN.value,
            )
        else:
            await asyncio.to_thread(
                _record_review_attempt,
                engine,
                delivery_id=delivery_id,
                status=DeliveryStatus.FAILED,
                external_url=None,
                error_code=exc.record.code.value,
            )
        raise
    except ValueError:
        await asyncio.to_thread(
            _record_review_attempt,
            engine,
            delivery_id=delivery_id,
            status=DeliveryStatus.FAILED,
            external_url=None,
            error_code=ErrorCode.INPUT_INVALID.value,
        )
        raise

    return await asyncio.to_thread(
        _record_review_attempt,
        engine,
        delivery_id=delivery_id,
        status=DeliveryStatus.SENT,
        external_url=receipt.permalink,
        error_code=None,
    )


async def deliver_failure_alert(
    *,
    engine: Engine,
    run_id: UUID,
    channel_id: str,
    component: str,
    state: HealthState,
    error_code: ErrorCode,
    attempt: int,
    attempt_limit: int,
    idempotency_key: str,
    adapter: DiscordFailureAlertAdapter | None = None,
) -> Delivery:
    """Deliver one operational failure alert through a durable, idempotent intent."""

    intent = await asyncio.to_thread(
        _open_review_intent,
        engine,
        run_id=run_id,
        target=channel_id,
        key=idempotency_key,
    )
    if intent.already_delivered:
        return intent.delivery
    alert = FailureAlert(
        delivery_id=intent.delivery.id,
        run_id=run_id,
        channel_id=channel_id,
        component=component,
        state=state,
        error_code=error_code,
        attempt=attempt,
        attempt_limit=attempt_limit,
    )
    resolved_adapter = adapter or _failure_alert_adapter_from_settings(channel_id)
    try:
        receipt = await resolved_adapter.send(alert)
    except LifeAgentError as exc:
        status = (
            DeliveryStatus.UNCERTAIN
            if exc.record.category is ErrorCategory.TRANSIENT
            else DeliveryStatus.FAILED
        )
        error = (
            ErrorCode.DELIVERY_UNCERTAIN.value
            if status is DeliveryStatus.UNCERTAIN
            else exc.record.code.value
        )
        await asyncio.to_thread(
            _record_review_attempt,
            engine,
            delivery_id=intent.delivery.id,
            status=status,
            external_url=None,
            error_code=error,
        )
        raise
    except ValueError:
        await asyncio.to_thread(
            _record_review_attempt,
            engine,
            delivery_id=intent.delivery.id,
            status=DeliveryStatus.FAILED,
            external_url=None,
            error_code=ErrorCode.INPUT_INVALID.value,
        )
        raise
    return await asyncio.to_thread(
        _record_review_attempt,
        engine,
        delivery_id=intent.delivery.id,
        status=DeliveryStatus.SENT,
        external_url=receipt.permalink,
        error_code=None,
    )


async def deliver_daily_review_report(
    *,
    engine: Engine,
    run_id: UUID,
    channel_id: str,
    report_date: date,
    report_artifact_key: str,
    idempotency_key: str,
    adapter: DiscordDailyReviewAdapter | None = None,
) -> Delivery:
    """Deliver one nightly report reference through a durable, idempotent intent."""

    intent = await asyncio.to_thread(
        _open_review_intent,
        engine,
        run_id=run_id,
        target=channel_id,
        key=idempotency_key,
    )
    if intent.already_delivered:
        return intent.delivery
    summary = DailyReviewSummary(
        delivery_id=intent.delivery.id,
        run_id=run_id,
        channel_id=channel_id,
        report_date=report_date,
        report_artifact_key=report_artifact_key,
    )
    if adapter is None:
        settings = Settings()
        if settings.discord_bot_token is None:
            raise authorization_error("Discord bot token is not configured")
        allowed = set(settings.discord_target_channels)
        allowed.add(channel_id)
        adapter = DiscordDailyReviewAdapter(
            token=settings.discord_bot_token,
            allowed_channel_ids=allowed,
            base_url=settings.discord_api_url,
        )
    try:
        receipt = await adapter.send(summary)
    except LifeAgentError as exc:
        status = (
            DeliveryStatus.UNCERTAIN
            if exc.record.category is ErrorCategory.TRANSIENT
            else DeliveryStatus.FAILED
        )
        error_code = (
            ErrorCode.DELIVERY_UNCERTAIN.value
            if status is DeliveryStatus.UNCERTAIN
            else exc.record.code.value
        )
        await asyncio.to_thread(
            _record_review_attempt,
            engine,
            delivery_id=intent.delivery.id,
            status=status,
            external_url=None,
            error_code=error_code,
        )
        raise
    except ValueError:
        await asyncio.to_thread(
            _record_review_attempt,
            engine,
            delivery_id=intent.delivery.id,
            status=DeliveryStatus.FAILED,
            external_url=None,
            error_code=ErrorCode.INPUT_INVALID.value,
        )
        raise
    return await asyncio.to_thread(
        _record_review_attempt,
        engine,
        delivery_id=intent.delivery.id,
        status=DeliveryStatus.SENT,
        external_url=receipt.permalink,
        error_code=None,
    )


__all__ = [
    "AcademicClarificationMessage",
    "AcademicDiscordMessage",
    "AcademicSetupReminderMessage",
    "DailyReviewSummary",
    "DiscordAcademicPlannerAdapter",
    "DiscordAcademicPlannerDelivery",
    "DiscordAcademicResponseDelivery",
    "DiscordDailyReviewAdapter",
    "DiscordDeliveryReceipt",
    "DiscordFailureAlertAdapter",
    "DiscordFinanceBriefingAdapter",
    "DiscordFinanceBriefingDelivery",
    "DiscordReviewSummaryAdapter",
    "FailureAlert",
    "FinanceDiscordBriefingMessage",
    "ReviewSummary",
    "deliver_academic_message",
    "deliver_daily_review_report",
    "deliver_failure_alert",
    "deliver_finance_briefing",
    "deliver_review_summary",
]
