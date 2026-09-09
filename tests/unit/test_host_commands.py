from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from app.host.commands import (
    BackendLiveProbe,
    CommandResult,
    ComposeRuntime,
    DeploymentMarker,
    DockerDesktop,
    HostWakeError,
    OllamaLaunchAgent,
)
from app.host.settings import HostWakeSettings


class RecordingRunner:
    def __init__(
        self,
        returncodes: list[int] | None = None,
        stdout: list[str] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self._returncodes = returncodes or []
        self._stdout = stdout or []

    async def run(
        self,
        args: list[str] | tuple[str, ...],
        *,
        timeout_seconds: int,
    ) -> CommandResult:
        del timeout_seconds
        self.calls.append(list(args))
        returncode = self._returncodes.pop(0) if self._returncodes else 0
        output = self._stdout.pop(0) if self._stdout else ""
        return CommandResult(returncode=returncode, stdout=output)


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


@pytest.mark.asyncio
async def test_docker_desktop_uses_fixed_probe_and_start_commands(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runner = RecordingRunner(returncodes=[1, 0, 0])

    await DockerDesktop(settings, runner).ensure_ready()

    assert runner.calls == [
        ["/usr/local/bin/docker", "info", "--format", "{{json .ServerVersion}}"],
        ["/usr/local/bin/docker", "desktop", "start", "--timeout", "120"],
        ["/usr/local/bin/docker", "info", "--format", "{{json .ServerVersion}}"],
    ]


@pytest.mark.asyncio
async def test_compose_runtime_uses_no_build_no_pull_for_fixed_services(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runner = RecordingRunner()

    await ComposeRuntime(settings, runner).ensure_ready()

    assert runner.calls == [
        [
            "/usr/local/bin/docker",
            "compose",
            "-f",
            str(tmp_path / "compose.yaml"),
            "up",
            "-d",
            "--no-build",
            "--pull",
            "never",
            "--wait",
            "postgres",
            "api",
            "worker-academic-planner",
        ]
    ]
    assert "build" not in runner.calls[0]
    assert "prune" not in runner.calls[0]
    assert "stop" not in runner.calls[0]


@pytest.mark.asyncio
async def test_deployment_marker_maps_missing_or_mismatch_to_image_stale(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)

    with pytest.raises(HostWakeError) as missing:
        await DeploymentMarker(settings, RecordingRunner()).ensure_ready()
    assert missing.value.code == "image_stale"

    settings.deployed_image_marker_path.parent.mkdir(parents=True)
    settings.deployed_image_marker_path.write_text("sha256:old\n", encoding="utf-8")
    mismatched = replace(settings, expected_deployed_image_id="sha256:new")
    with pytest.raises(HostWakeError) as mismatch:
        await DeploymentMarker(mismatched, RecordingRunner()).ensure_ready()
    assert mismatch.value.code == "image_stale"

    matched = replace(settings, expected_deployed_image_id="sha256:old")
    await DeploymentMarker(matched, RecordingRunner(stdout=["sha256:old\n"])).ensure_ready()


@pytest.mark.asyncio
async def test_ollama_launch_agent_kickstarts_fixed_label_when_api_is_down(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    runner = RecordingRunner()
    responses = iter([httpx.Response(503), httpx.Response(200)])

    async def handler(_request: httpx.Request) -> httpx.Response:
        return next(responses)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await OllamaLaunchAgent(settings, runner, client=client, uid=501, sleep=0).ensure_ready()

    assert runner.calls == [["/bin/launchctl", "kickstart", "-k", "gui/501/com.lifeagent.ollama"]]


@pytest.mark.asyncio
async def test_backend_live_probe_times_out_safely(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), api_live_timeout_seconds=1)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HostWakeError) as raised:
            await BackendLiveProbe(settings, client=client, sleep=0).ensure_ready()

    assert raised.value.code == "api_unhealthy"
