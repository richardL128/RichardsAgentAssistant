"""Fixed host command boundary for the native wake daemon."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import httpx

from app.host.settings import HostWakeSettings

HostWakeErrorCode = Literal[
    "docker_timeout",
    "docker_unavailable",
    "image_stale",
    "compose_unhealthy",
    "api_unhealthy",
    "ollama_unavailable",
]


class HostWakeError(RuntimeError):
    """Typed safe failure for host startup work."""

    def __init__(self, code: HostWakeErrorCode) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Bounded command result; callers should log only the code."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner(Protocol):
    async def run(self, args: Sequence[str], *, timeout_seconds: int) -> CommandResult: ...


class AsyncSubprocessCommandRunner:
    """Run an already validated absolute command without a shell."""

    async def run(self, args: Sequence[str], *, timeout_seconds: int) -> CommandResult:
        if not args:
            raise ValueError("command args are required")
        executable = Path(args[0])
        if not executable.is_absolute():
            raise ValueError("command executable must be absolute")
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_seconds,
            )
        except (TimeoutError, asyncio.CancelledError):
            process.kill()
            await process.wait()
            raise
        return CommandResult(
            returncode=process.returncode or 0,
            stdout=stdout_bytes.decode("utf-8", errors="replace")[:4_096],
            stderr=stderr_bytes.decode("utf-8", errors="replace")[:4_096],
        )


class DockerDesktop:
    """Start Docker Desktop through a fixed, no-shell command path."""

    def __init__(
        self,
        settings: HostWakeSettings,
        runner: CommandRunner,
        *,
        sleep: float = 1.0,
    ) -> None:
        if sleep < 0:
            raise ValueError("sleep must be non-negative")
        self._settings = settings
        self._runner = runner
        self._sleep = sleep

    async def ensure_ready(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.docker_desktop_timeout_seconds
        try:
            if await self._probe(deadline):
                return
        except TimeoutError as exc:
            raise HostWakeError("docker_timeout") from exc
        try:
            result = await self._run_with_deadline(
                [
                    str(self._settings.docker_executable),
                    "desktop",
                    "start",
                    "--timeout",
                    str(self._settings.docker_desktop_timeout_seconds),
                ],
                deadline=deadline,
            )
        except TimeoutError as exc:
            raise HostWakeError("docker_timeout") from exc
        if result.returncode != 0:
            raise HostWakeError("docker_unavailable")
        try:
            await self._wait_for_engine(deadline)
        except TimeoutError as exc:
            raise HostWakeError("docker_timeout") from exc

    async def _wait_for_engine(self, deadline: float) -> None:
        while True:
            if await self._probe(deadline):
                return
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError
            await asyncio.sleep(min(self._sleep, remaining))

    async def _probe(self, deadline: float) -> bool:
        try:
            result = await self._run_with_deadline(
                [
                    str(self._settings.docker_executable),
                    "info",
                    "--format",
                    "{{json .ServerVersion}}",
                ],
                deadline=deadline,
                max_timeout_seconds=10,
            )
        except TimeoutError:
            if asyncio.get_running_loop().time() >= deadline:
                raise
            return False
        return result.returncode == 0

    async def _run_with_deadline(
        self,
        args: Sequence[str],
        *,
        deadline: float,
        max_timeout_seconds: int | None = None,
    ) -> CommandResult:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError
        timeout_seconds = math.ceil(remaining)
        if max_timeout_seconds is not None:
            timeout_seconds = min(timeout_seconds, max_timeout_seconds)
        if timeout_seconds <= 0:
            raise TimeoutError
        return await asyncio.wait_for(
            self._runner.run(args, timeout_seconds=timeout_seconds),
            timeout=remaining,
        )


class ComposeRuntime:
    """Bring up only the canonical services from the canonical compose file."""

    _SERVICES = ("postgres", "api", "worker-academic-planner")

    def __init__(self, settings: HostWakeSettings, runner: CommandRunner) -> None:
        self._settings = settings
        self._runner = runner

    async def ensure_ready(self) -> None:
        result = await self._runner.run(
            [
                str(self._settings.docker_executable),
                "compose",
                "-f",
                str(self._settings.compose_file),
                "up",
                "-d",
                "--no-build",
                "--pull",
                "never",
                "--wait",
                *self._SERVICES,
            ],
            timeout_seconds=self._settings.compose_wait_timeout_seconds,
        )
        if result.returncode != 0:
            raise HostWakeError("compose_unhealthy")


class DeploymentMarker:
    """Fail safely when the local image deployment marker is missing or stale."""

    def __init__(self, settings: HostWakeSettings, runner: CommandRunner) -> None:
        self._settings = settings
        self._runner = runner

    async def ensure_ready(self) -> None:
        try:
            deployed = self._settings.deployed_image_marker_path.read_text(
                encoding="utf-8",
            ).strip()
        except OSError as exc:
            raise HostWakeError("image_stale") from exc
        if not deployed:
            raise HostWakeError("image_stale")
        expected = self._settings.expected_deployed_image_id
        if expected is not None and deployed != expected:
            raise HostWakeError("image_stale")
        result = await self._runner.run(
            [
                str(self._settings.docker_executable),
                "image",
                "inspect",
                "lifeagent-app:local",
                "--format",
                "{{.Id}}",
            ],
            timeout_seconds=10,
        )
        if result.returncode != 0 or result.stdout.strip() != deployed:
            raise HostWakeError("image_stale")


class BackendLiveProbe:
    """Wait for the loopback API live endpoint after Compose reports healthy."""

    def __init__(
        self,
        settings: HostWakeSettings,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: float = 1.0,
    ) -> None:
        self._settings = settings
        self._client = client
        self._sleep = sleep

    async def ensure_ready(self) -> None:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=3.0)
        try:
            deadline = asyncio.get_running_loop().time() + self._settings.api_live_timeout_seconds
            while True:
                try:
                    response = await client.get(self._settings.backend_live_url)
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                if asyncio.get_running_loop().time() >= deadline:
                    raise HostWakeError("api_unhealthy")
                await asyncio.sleep(self._sleep)
        finally:
            if owns_client:
                await client.aclose()


class OllamaLaunchAgent:
    """Kickstart the fixed Ollama LaunchAgent and wait for the lightweight API."""

    def __init__(
        self,
        settings: HostWakeSettings,
        runner: CommandRunner,
        *,
        client: httpx.AsyncClient | None = None,
        uid: int | None = None,
        sleep: float = 1.0,
    ) -> None:
        self._settings = settings
        self._runner = runner
        self._client = client
        self._uid = uid
        self._sleep = sleep

    async def ensure_ready(self) -> None:
        if await self._probe():
            return
        uid = self._uid
        if uid is None:
            import os

            uid = os.getuid()
        result = await self._runner.run(
            [
                str(self._settings.launchctl_executable),
                "kickstart",
                "-k",
                f"gui/{uid}/{self._settings.ollama_launch_agent_label}",
            ],
            timeout_seconds=10,
        )
        if result.returncode != 0:
            raise HostWakeError("ollama_unavailable")
        deadline = asyncio.get_running_loop().time() + self._settings.ollama_startup_timeout_seconds
        while True:
            if await self._probe():
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise HostWakeError("ollama_unavailable")
            await asyncio.sleep(self._sleep)

    async def _probe(self) -> bool:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=2.0)
        try:
            try:
                response = await client.get(f"{self._settings.ollama_local_base_url}/api/tags")
            except httpx.HTTPError:
                return False
            return response.status_code < 400
        finally:
            if owns_client:
                await client.aclose()


__all__ = [
    "AsyncSubprocessCommandRunner",
    "BackendLiveProbe",
    "CommandResult",
    "CommandRunner",
    "ComposeRuntime",
    "DeploymentMarker",
    "DockerDesktop",
    "HostWakeError",
    "HostWakeErrorCode",
    "OllamaLaunchAgent",
]
