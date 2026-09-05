"""Discord adapter restricted to deterministic operational failure alerts."""

from __future__ import annotations

import asyncio
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
from app.db.models import Delivery, DeliveryStatus
from app.db.repositories import DeliveryRepository, utc_now
from app.health.checks import HealthState

_DISCORD_CONTENT_LIMIT = 2_000


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
                    "nonce": str(alert.delivery_id),
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
                    "nonce": str(summary.delivery_id),
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
                    "nonce": str(summary.delivery_id),
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
                    "nonce": str(message.delivery_id),
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
        blocks = list(getattr(plan, "blocks", ()))[:3]
        lines = ["Today's three highest-value study blocks:"]
        lines.extend(
            f"- {block.title} ({block.start_at.isoformat()}-{block.end_at.isoformat()}): "
            f"{block.rationale}"
            for block in blocks
        )
        if not blocks:
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

    async def send_ambiguity_question(self, fact: Any, *, idempotency_key: str) -> Delivery:
        content = f"Please confirm this academic fact before scheduling: {fact.question}"
        return await self._send(content, idempotency_key)

    async def send_confirmation(self, proposal: Any, *, idempotency_key: str) -> Delivery:
        content = (
            "Proposed academic updates:\n"
            + "\n".join(f"- {change.field}: {change.value}" for change in proposal.changes)
            + f"\nReply with the exact confirmation event: {proposal.confirmation_event}"
        )
        return await self._send(content, idempotency_key)


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
                    "nonce": str(message.delivery_id),
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
    "AcademicDiscordMessage",
    "DailyReviewSummary",
    "DiscordAcademicPlannerAdapter",
    "DiscordAcademicPlannerDelivery",
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
