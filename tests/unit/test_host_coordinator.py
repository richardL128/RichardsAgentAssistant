import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import SecretStr

from app.connectors.discord_gateway import (
    DiscordAcademicMessageCreate,
    DiscordClarificationInteraction,
)
from app.host.commands import HostWakeError
from app.host.coordinator import HOST_WAKE_ACKNOWLEDGEMENT, HostWakeCoordinator
from app.host.discord import HOST_COMMAND_ACKNOWLEDGEMENT
from app.host.handoff import (
    DiscordHostHandoff,
    DiscordHostHandoffAccepted,
    DiscordHostHandoffEvent,
    DiscordHostInteractionHandoffEvent,
)
from app.host.outbox import WakeOutbox
from app.host.settings import HostWakeSettings


class FakeDiscord:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.acks: list[tuple[str, str]] = []
        self.ack_contents: list[str] = []
        self.edits: list[str] = []

    async def send_acknowledgement(
        self,
        *,
        channel_id: str,
        root_message_id: str,
        content: str = HOST_WAKE_ACKNOWLEDGEMENT,
    ) -> str:
        self.events.append("ack")
        self.acks.append((channel_id, root_message_id))
        self.ack_contents.append(content)
        return f"44{root_message_id[2:]}"

    async def edit_acknowledgement(
        self,
        *,
        channel_id: str,
        acknowledgement_message_id: str,
        content: str,
    ) -> None:
        del channel_id, acknowledgement_message_id
        self.events.append("edit")
        self.edits.append(content)


class FakeService:
    def __init__(
        self,
        name: str,
        events: list[str],
        *,
        release: asyncio.Event | None = None,
        error: Exception | None = None,
    ) -> None:
        self.name = name
        self.events = events
        self.release = release
        self.error = error
        self.calls = 0

    async def ensure_ready(self) -> None:
        self.calls += 1
        self.events.append(self.name)
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error


class FakeHandoff:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.events_submitted: list[DiscordHostHandoff] = []

    async def submit(self, event: DiscordHostHandoff) -> DiscordHostHandoffAccepted:
        self.events.append("handoff")
        self.events_submitted.append(event)
        return DiscordHostHandoffAccepted(status="accepted")


def _settings(tmp_path: Path) -> HostWakeSettings:
    return HostWakeSettings(
        repository_root=tmp_path,
        discord_bot_token=SecretStr("token"),
        discord_application_id="111111111111111111",
        discord_academic_channel_id="222222222222222222",
        discord_academic_authorized_user_ids=frozenset({"333333333333333333"}),
        host_handoff_secret=SecretStr("handoff-secret"),
        docker_executable=Path("/usr/local/bin/docker"),
        launchctl_executable=Path("/bin/launchctl"),
        ollama_executable=Path("/usr/local/bin/ollama"),
        compose_file=tmp_path / "compose.yaml",
        outbox_path=tmp_path / ".artifacts" / "discord-wake" / "outbox.sqlite3",
        deployed_image_marker_path=tmp_path / ".artifacts" / "discord-wake" / "deployed-image-id",
    )


def _message(
    message_id: str = "555555555555555555",
    *,
    mentioned: bool = True,
) -> DiscordAcademicMessageCreate:
    content = "<@111111111111111111> create a study block" if mentioned else "create a study block"
    mentions = ("111111111111111111",) if mentioned else ()
    return DiscordAcademicMessageCreate(
        message_id=message_id,
        channel_id="222222222222222222",
        author_id="333333333333333333",
        timestamp=datetime(2026, 9, 9, tzinfo=UTC),
        content=SecretStr(content),
        mentioned_user_ids=mentions,
    )


def _coordinator(
    tmp_path: Path,
) -> tuple[HostWakeCoordinator, WakeOutbox, FakeDiscord, FakeHandoff]:
    settings = _settings(tmp_path)
    events: list[str] = []
    discord = FakeDiscord()
    handoff = FakeHandoff(events)
    outbox = WakeOutbox(settings.outbox_path)
    return (
        HostWakeCoordinator(
            settings=settings,
            outbox=outbox,
            discord=discord,
            docker=FakeService("docker", events),
            compose=FakeService("compose", events),
            ollama=FakeService("ollama", events),
            deployment=FakeService("deploy", events),
            backend_live=FakeService("api", events),
            handoff=handoff,
        ),
        outbox,
        discord,
        handoff,
    )


@pytest.mark.asyncio
async def test_valid_mention_acknowledges_before_wake_and_handoff(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    events: list[str] = []
    discord = FakeDiscord()
    handoff = FakeHandoff(events)
    outbox = WakeOutbox(settings.outbox_path)
    coordinator = HostWakeCoordinator(
        settings=settings,
        outbox=outbox,
        discord=discord,
        docker=FakeService("docker", events),
        compose=FakeService("compose", events),
        ollama=FakeService("ollama", events),
        deployment=FakeService("deploy", events),
        backend_live=FakeService("api", events),
        handoff=handoff,
    )

    result = await coordinator.process_message(_message())

    assert result == "handled"
    assert discord.events[0] == "ack"
    assert discord.acks == [("222222222222222222", "555555555555555555")]
    assert events.index("deploy") < events.index("compose")
    assert events[-1] == "handoff"
    assert handoff.events_submitted[0].acknowledgement_message_id == "445555555555555555"


@pytest.mark.asyncio
async def test_authorized_unmentioned_prose_is_acknowledged_and_handed_to_harness(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    events: list[str] = []
    discord = FakeDiscord()
    handoff = FakeHandoff(events)
    outbox = WakeOutbox(settings.outbox_path)
    coordinator = HostWakeCoordinator(
        settings=settings,
        outbox=outbox,
        discord=discord,
        docker=FakeService("docker", events),
        compose=FakeService("compose", events),
        ollama=FakeService("ollama", events),
        deployment=FakeService("deploy", events),
        backend_live=FakeService("api", events),
        handoff=handoff,
    )

    result = await coordinator.process_message(_message(mentioned=False))

    assert result == "handled"
    assert discord.events == ["ack"]
    assert events[-1] == "handoff"
    assert handoff.events_submitted[0].acknowledgement_message_id == "445555555555555555"
    assert outbox.get("555555555555555555").request_kind == "mention"


@pytest.mark.asyncio
async def test_exact_confirmation_command_uses_model_free_ack_and_handoff(tmp_path: Path) -> None:
    coordinator, outbox, discord, handoff = _coordinator(tmp_path)
    message = DiscordAcademicMessageCreate(
        message_id="777777777777777777",
        channel_id="222222222222222222",
        author_id="333333333333333333",
        timestamp=datetime(2026, 9, 9, tzinfo=UTC),
        content=SecretStr("confirm 88888888-8888-4888-8888-888888888888"),
    )

    assert await coordinator.process_message(message) == "handled"
    assert discord.ack_contents == [HOST_COMMAND_ACKNOWLEDGEMENT]
    assert outbox.get(message.message_id).request_kind == "command"
    submitted = handoff.events_submitted[0]
    assert isinstance(submitted, DiscordHostHandoffEvent)
    assert submitted.message_id == message.message_id


@pytest.mark.asyncio
async def test_interaction_is_spooled_before_background_handoff(tmp_path: Path) -> None:
    coordinator, outbox, _discord, handoff = _coordinator(tmp_path)
    interaction = DiscordClarificationInteraction(
        interaction_id="999999999999999999",
        channel_id="222222222222222222",
        user_id="333333333333333333",
        clarification_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        action="quiz",
    )

    result = await coordinator.handle_interaction(interaction)
    spooled = outbox.get_interaction(interaction.interaction_id)
    assert result.status == "queued"
    assert spooled.state == "pending"
    assert spooled.action == "quiz"

    await coordinator.drain_interaction_tasks()

    assert outbox.get_interaction(interaction.interaction_id).state == "accepted"
    submitted = handoff.events_submitted[0]
    assert isinstance(submitted, DiscordHostInteractionHandoffEvent)
    assert submitted.clarification_id == interaction.clarification_id


@pytest.mark.asyncio
async def test_concurrent_mentions_share_wake_attempt_but_handoff_each(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    events: list[str] = []
    release = asyncio.Event()
    docker = FakeService("docker", events, release=release)
    ollama = FakeService("ollama", events, release=release)
    coordinator = HostWakeCoordinator(
        settings=settings,
        outbox=WakeOutbox(settings.outbox_path),
        discord=FakeDiscord(),
        docker=docker,
        compose=FakeService("compose", events),
        ollama=ollama,
        deployment=FakeService("deploy", events),
        backend_live=FakeService("api", events),
        handoff=FakeHandoff(events),
    )

    first = asyncio.create_task(coordinator.process_message(_message("555555555555555555")))
    second = asyncio.create_task(coordinator.process_message(_message("666666666666666666")))
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.gather(first, second) == ["handled", "handled"]

    assert docker.calls == 1
    assert ollama.calls == 1
    assert events.count("handoff") == 2


@pytest.mark.asyncio
async def test_wake_failure_edits_existing_ack_with_safe_text(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    events: list[str] = []
    discord = FakeDiscord()
    coordinator = HostWakeCoordinator(
        settings=settings,
        outbox=WakeOutbox(settings.outbox_path),
        discord=discord,
        docker=FakeService("docker", events, error=HostWakeError("docker_timeout")),
        compose=FakeService("compose", events),
        ollama=FakeService("ollama", events),
        deployment=FakeService("deploy", events),
        backend_live=FakeService("api", events),
        handoff=FakeHandoff(events),
    )

    result = await coordinator.process_message(_message())

    assert result == "failed"
    assert discord.events == ["ack", "edit"]
    assert discord.edits[0].startswith("LifeAgent could not start Docker Desktop.")


@pytest.mark.asyncio
async def test_replay_pending_reuses_outbox_row(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    outbox = WakeOutbox(settings.outbox_path)
    outbox.record_event(
        message_id="555555555555555555",
        channel_id="222222222222222222",
        author_id="333333333333333333",
        event_timestamp=datetime(2026, 9, 9, tzinfo=UTC),
    )
    events: list[str] = []
    discord = FakeDiscord()
    coordinator = HostWakeCoordinator(
        settings=settings,
        outbox=outbox,
        discord=discord,
        docker=FakeService("docker", events),
        compose=FakeService("compose", events),
        ollama=FakeService("ollama", events),
        deployment=FakeService("deploy", events),
        backend_live=FakeService("api", events),
        handoff=FakeHandoff(events),
    )

    assert await coordinator.replay_pending() == 1
    assert outbox.get("555555555555555555").state == "accepted"
    assert discord.acks == [("222222222222222222", "555555555555555555")]


def test_exact_acknowledgement_matches_plan() -> None:
    assert (
        HOST_WAKE_ACKNOWLEDGEMENT
        == "I’m waking up LifeAgent and Qwen. Please give me a little time to respond."  # noqa: RUF001
    )
