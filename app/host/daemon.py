"""Native Discord wake daemon entry point."""

from __future__ import annotations

import asyncio

from app.connectors.discord_gateway import DiscordGatewayListener
from app.host.commands import (
    AsyncSubprocessCommandRunner,
    BackendLiveProbe,
    ComposeRuntime,
    DeploymentMarker,
    DockerDesktop,
    OllamaLaunchAgent,
)
from app.host.coordinator import HostWakeCoordinator
from app.host.discord import DiscordWakeAckAdapter
from app.host.handoff import DiscordHostHandoffClient
from app.host.outbox import WakeOutbox
from app.host.settings import HostWakeSettings


def build_coordinator(settings: HostWakeSettings) -> HostWakeCoordinator:
    runner = AsyncSubprocessCommandRunner()
    return HostWakeCoordinator(
        settings=settings,
        outbox=WakeOutbox(settings.outbox_path),
        discord=DiscordWakeAckAdapter(
            token=settings.discord_bot_token,
            allowed_channel_id=settings.discord_academic_channel_id,
            base_url=settings.discord_api_base_url,
        ),
        docker=DockerDesktop(settings, runner),
        compose=ComposeRuntime(settings, runner),
        ollama=OllamaLaunchAgent(settings, runner),
        deployment=DeploymentMarker(settings, runner),
        backend_live=BackendLiveProbe(settings),
        handoff=DiscordHostHandoffClient(
            endpoint_url=settings.backend_handoff_url,
            secret=settings.host_handoff_secret,
            timeout_seconds=settings.handoff_timeout_seconds,
            max_attempts=settings.handoff_max_attempts,
        ),
    )


def build_listener(settings: HostWakeSettings) -> DiscordGatewayListener:
    coordinator = build_coordinator(settings)
    return DiscordGatewayListener(
        token=settings.discord_bot_token,
        api_base_url=settings.discord_api_base_url,
        allowed_channel_ids={settings.discord_academic_channel_id},
        authorized_user_ids=set(settings.discord_academic_authorized_user_ids),
        clarification_enqueuer=coordinator.handle_interaction,
        message_content_enabled=settings.message_content_enabled,
        message_handler=coordinator.handle_message,
    )


async def run(settings: HostWakeSettings | None = None) -> None:
    app_settings = settings or HostWakeSettings.from_env()
    coordinator = build_coordinator(app_settings)
    coordinator.prune_outbox()
    listener = DiscordGatewayListener(
        token=app_settings.discord_bot_token,
        api_base_url=app_settings.discord_api_base_url,
        allowed_channel_ids={app_settings.discord_academic_channel_id},
        authorized_user_ids=set(app_settings.discord_academic_authorized_user_ids),
        clarification_enqueuer=coordinator.handle_interaction,
        message_content_enabled=app_settings.message_content_enabled,
        message_handler=coordinator.handle_message,
    )
    while True:
        await coordinator.replay_pending()
        await listener.run_forever(max_attempts=1)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()


__all__ = ["build_coordinator", "build_listener", "main", "run"]
