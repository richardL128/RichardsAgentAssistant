import asyncio
import os
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from app.host.commands import (
    AsyncSubprocessCommandRunner,
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
        self.timeouts: list[int] = []
        self._returncodes = returncodes or []
        self._stdout = stdout or []

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: int,
    ) -> CommandResult:
        self.calls.append(list(args))
        self.timeouts.append(timeout_seconds)
        returncode = self._returncodes.pop(0) if self._returncodes else 0
        output = self._stdout.pop(0) if self._stdout else ""
        return CommandResult(returncode=returncode, stdout=output)


class HangingPostStartProbeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: int,
    ) -> CommandResult:
        del timeout_seconds
        self.calls.append(list(args))
        if len(self.calls) == 1:
            return CommandResult(returncode=1)
        if list(args[1:3]) == ["desktop", "start"]:
            return CommandResult(returncode=0)
        await asyncio.sleep(10)
        return CommandResult(returncode=1)


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
async def test_async_subprocess_runner_kills_process_when_cancelled(tmp_path: Path) -> None:
    pid_path = tmp_path / "child.pid"
    script = (
        "import os, pathlib, sys, time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); "
        "time.sleep(30)"
    )
    runner = AsyncSubprocessCommandRunner()
    task = asyncio.create_task(
        runner.run(
            [sys.executable, "-c", script, str(pid_path)],
            timeout_seconds=30,
        )
    )
    deadline = asyncio.get_running_loop().time() + 2
    while not pid_path.exists():
        if asyncio.get_running_loop().time() >= deadline:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            pytest.fail("subprocess did not publish pid before test deadline")
        await asyncio.sleep(0.01)

    child_pid = int(pid_path.read_text(encoding="utf-8"))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    deadline = asyncio.get_running_loop().time() + 2
    while True:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail("cancelled subprocess was not killed and reaped")
        await asyncio.sleep(0.01)


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
async def test_docker_desktop_polls_after_cli_start_until_engine_ready(
    tmp_path: Path,
) -> None:
    settings = replace(_settings(tmp_path), docker_desktop_timeout_seconds=5)
    runner = RecordingRunner(returncodes=[1, 0, 1, 1, 0])

    await DockerDesktop(settings, runner, sleep=0).ensure_ready()

    assert runner.calls == [
        ["/usr/local/bin/docker", "info", "--format", "{{json .ServerVersion}}"],
        ["/usr/local/bin/docker", "desktop", "start", "--timeout", "5"],
        ["/usr/local/bin/docker", "info", "--format", "{{json .ServerVersion}}"],
        ["/usr/local/bin/docker", "info", "--format", "{{json .ServerVersion}}"],
        ["/usr/local/bin/docker", "info", "--format", "{{json .ServerVersion}}"],
    ]
    assert all(timeout <= 5 for timeout in runner.timeouts)


@pytest.mark.asyncio
async def test_docker_desktop_reports_start_command_failure_as_unavailable(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    runner = RecordingRunner(returncodes=[1, 2])

    with pytest.raises(HostWakeError) as raised:
        await DockerDesktop(settings, runner).ensure_ready()

    assert raised.value.code == "docker_unavailable"
    assert runner.calls == [
        ["/usr/local/bin/docker", "info", "--format", "{{json .ServerVersion}}"],
        ["/usr/local/bin/docker", "desktop", "start", "--timeout", "120"],
    ]


@pytest.mark.asyncio
async def test_docker_desktop_reports_post_start_engine_readiness_timeout(
    tmp_path: Path,
) -> None:
    settings = replace(_settings(tmp_path), docker_desktop_timeout_seconds=1)
    runner = HangingPostStartProbeRunner()

    with pytest.raises(HostWakeError) as raised:
        await DockerDesktop(settings, runner, sleep=0.01).ensure_ready()

    assert raised.value.code == "docker_timeout"
    assert runner.calls[:2] == [
        ["/usr/local/bin/docker", "info", "--format", "{{json .ServerVersion}}"],
        ["/usr/local/bin/docker", "desktop", "start", "--timeout", "1"],
    ]
    assert runner.calls[2:] == [
        ["/usr/local/bin/docker", "info", "--format", "{{json .ServerVersion}}"]
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
@pytest.mark.parametrize("status_code", [302, 401, 404, 503])
async def test_backend_live_probe_times_out_safely(tmp_path: Path, status_code: int) -> None:
    settings = replace(_settings(tmp_path), api_live_timeout_seconds=1)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HostWakeError) as raised:
            await BackendLiveProbe(settings, client=client, sleep=0).ensure_ready()

    assert raised.value.code == "api_unhealthy"
