"""Standalone host-wake settings.

This module intentionally does not import application settings, persistence,
queue, workflow, or model code. The native daemon must start quickly enough to
hear Discord while the container stack is stopped.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr

_DISCORD_ID = re.compile(r"^[0-9]{5,24}$")


class HostWakeSettingsError(ValueError):
    """Configuration problem safe to show in host operator output."""


@dataclass(frozen=True, slots=True)
class HostWakeSettings:
    """Validated settings used only by the native host wake daemon."""

    repository_root: Path
    discord_bot_token: SecretStr
    discord_application_id: str
    discord_academic_channel_id: str
    discord_academic_authorized_user_ids: frozenset[str]
    host_handoff_secret: SecretStr
    docker_executable: Path
    launchctl_executable: Path
    ollama_executable: Path
    compose_file: Path
    outbox_path: Path
    deployed_image_marker_path: Path
    expected_deployed_image_id: str | None = None
    backend_handoff_url: str = "http://127.0.0.1:8000/internal/discord/academic/handoff"
    backend_live_url: str = "http://127.0.0.1:8000/health/live"
    discord_api_base_url: str = "https://discord.com/api/v10"
    ollama_local_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3-32gb:latest"
    ollama_model_digest: str | None = None
    ollama_launch_agent_label: str = "com.lifeagent.ollama"
    docker_desktop_timeout_seconds: int = 120
    compose_wait_timeout_seconds: int = 180
    api_live_timeout_seconds: int = 60
    ollama_startup_timeout_seconds: int = 30
    handoff_timeout_seconds: int = 10
    handoff_max_attempts: int = 3
    outbox_retention_seconds: int = 86_400
    message_content_enabled: bool = True

    def __post_init__(self) -> None:
        root = self.repository_root.resolve()
        object.__setattr__(self, "repository_root", root)
        compose_file = self.compose_file.resolve()
        if compose_file != root / "compose.yaml":
            raise HostWakeSettingsError("host wake must use the repository compose.yaml")
        object.__setattr__(self, "compose_file", compose_file)
        object.__setattr__(self, "outbox_path", self.outbox_path.resolve())
        object.__setattr__(
            self,
            "deployed_image_marker_path",
            self.deployed_image_marker_path.resolve(),
        )
        object.__setattr__(self, "docker_executable", _require_absolute(self.docker_executable))
        object.__setattr__(
            self,
            "launchctl_executable",
            _require_absolute(self.launchctl_executable),
        )
        object.__setattr__(self, "ollama_executable", _require_absolute(self.ollama_executable))
        _require_discord_id("DISCORD_APPLICATION_ID", self.discord_application_id)
        _require_discord_id("DISCORD_ACADEMIC_CHANNEL_ID", self.discord_academic_channel_id)
        if not self.discord_academic_authorized_user_ids:
            raise HostWakeSettingsError("DISCORD_ACADEMIC_AUTHORIZED_USER_IDS is required")
        for user_id in self.discord_academic_authorized_user_ids:
            _require_discord_id("DISCORD_ACADEMIC_AUTHORIZED_USER_IDS", user_id)
        _require_secret("DISCORD_BOT_TOKEN", self.discord_bot_token)
        _require_secret("DISCORD_HOST_HANDOFF_SECRET", self.host_handoff_secret)
        if not self.message_content_enabled:
            raise HostWakeSettingsError(
                "DISCORD_ACADEMIC_MESSAGE_CONTENT_ENABLED must be true for host wake"
            )
        if (
            self.expected_deployed_image_id is not None
            and not self.expected_deployed_image_id.strip()
        ):
            raise HostWakeSettingsError("expected deployed image ID must not be empty")
        _require_local_url("backend handoff URL", self.backend_handoff_url)
        _require_local_url("backend live URL", self.backend_live_url)
        _require_http_url("Discord API URL", self.discord_api_base_url)
        _require_http_url("Ollama URL", self.ollama_local_base_url)
        if not self.ollama_model.strip():
            raise HostWakeSettingsError("OLLAMA_MODEL is required")
        for name, value, upper in (
            ("docker_desktop_timeout_seconds", self.docker_desktop_timeout_seconds, 300),
            ("compose_wait_timeout_seconds", self.compose_wait_timeout_seconds, 600),
            ("api_live_timeout_seconds", self.api_live_timeout_seconds, 300),
            ("ollama_startup_timeout_seconds", self.ollama_startup_timeout_seconds, 300),
            ("handoff_timeout_seconds", self.handoff_timeout_seconds, 60),
            ("handoff_max_attempts", self.handoff_max_attempts, 10),
            ("outbox_retention_seconds", self.outbox_retention_seconds, 604_800),
        ):
            if value <= 0 or value > upper:
                raise HostWakeSettingsError(f"{name} must be positive and <= {upper}")

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        repository_root: Path | None = None,
    ) -> HostWakeSettings:
        """Load daemon settings without relying on the application settings object."""

        env = environ or os.environ
        root = (repository_root or Path(env.get("LIFEAGENT_REPOSITORY_ROOT", "."))).resolve()
        docker = _absolute_env_path(env, "LIFEAGENT_DOCKER", fallback_name="docker")
        launchctl = _absolute_env_path(env, "LIFEAGENT_LAUNCHCTL", fallback_name="launchctl")
        ollama = _absolute_env_path(env, "LIFEAGENT_OLLAMA", fallback_name="ollama")
        authorized = frozenset(_split_ids(_required(env, "DISCORD_ACADEMIC_AUTHORIZED_USER_IDS")))
        api_port = env.get("API_PORT", "8000")
        if not api_port.isdigit():
            raise HostWakeSettingsError("API_PORT must be numeric")
        backend_live = env.get(
            "LIFEAGENT_BACKEND_LIVE_URL", f"http://127.0.0.1:{api_port}/health/live"
        )
        backend_handoff = env.get(
            "LIFEAGENT_BACKEND_HANDOFF_URL",
            f"http://127.0.0.1:{api_port}/internal/discord/academic/handoff",
        )
        return cls(
            repository_root=root,
            discord_bot_token=SecretStr(_required(env, "DISCORD_BOT_TOKEN")),
            discord_application_id=_required(env, "DISCORD_APPLICATION_ID"),
            discord_academic_channel_id=_required(env, "DISCORD_ACADEMIC_CHANNEL_ID"),
            discord_academic_authorized_user_ids=authorized,
            host_handoff_secret=SecretStr(_required(env, "DISCORD_HOST_HANDOFF_SECRET")),
            docker_executable=docker,
            launchctl_executable=launchctl,
            ollama_executable=ollama,
            compose_file=root / "compose.yaml",
            outbox_path=root / ".artifacts" / "discord-wake" / "outbox.sqlite3",
            deployed_image_marker_path=(root / ".artifacts" / "discord-wake" / "deployed-image-id"),
            expected_deployed_image_id=env.get("LIFEAGENT_EXPECTED_IMAGE_ID") or None,
            backend_handoff_url=backend_handoff,
            backend_live_url=backend_live,
            discord_api_base_url=env.get("DISCORD_API_BASE_URL", "https://discord.com/api/v10"),
            ollama_local_base_url=env.get("OLLAMA_LOCAL_BASE_URL", "http://127.0.0.1:11434"),
            ollama_model=env.get("OLLAMA_MODEL", "qwen3-32gb:latest"),
            ollama_model_digest=env.get("OLLAMA_MODEL_DIGEST") or None,
            ollama_launch_agent_label=env.get(
                "LIFEAGENT_OLLAMA_LAUNCH_AGENT_LABEL",
                "com.lifeagent.ollama",
            ),
            docker_desktop_timeout_seconds=_env_int(
                env, "LIFEAGENT_DOCKER_DESKTOP_TIMEOUT_SECONDS", 120
            ),
            compose_wait_timeout_seconds=_env_int(env, "LIFEAGENT_COMPOSE_WAIT_SECONDS", 180),
            api_live_timeout_seconds=_env_int(env, "LIFEAGENT_API_LIVE_TIMEOUT_SECONDS", 60),
            ollama_startup_timeout_seconds=_env_int(env, "OLLAMA_STARTUP_TIMEOUT_SECONDS", 30),
            handoff_timeout_seconds=_env_int(
                env,
                "DISCORD_HANDOFF_REQUEST_TIMEOUT_SECONDS",
                10,
            ),
            handoff_max_attempts=_env_int(env, "DISCORD_HANDOFF_RETRY_ATTEMPTS", 3),
            outbox_retention_seconds=_env_int(
                env, "LIFEAGENT_WAKE_OUTBOX_RETENTION_SECONDS", 86_400
            ),
            message_content_enabled=_env_bool(
                env,
                "DISCORD_ACADEMIC_MESSAGE_CONTENT_ENABLED",
                False,
            ),
        )


def _required(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise HostWakeSettingsError(f"{key} is required")
    return value


def _split_ids(value: str) -> tuple[str, ...]:
    stripped = value.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        stripped = stripped[1:-1]
    return tuple(item.strip().strip('"').strip("'") for item in stripped.split(",") if item.strip())


def _env_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    if not raw.isdigit():
        raise HostWakeSettingsError(f"{key} must be a positive integer")
    return int(raw)


def _env_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key, "").strip().casefold()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise HostWakeSettingsError(f"{key} must be true or false")


def _absolute_env_path(env: Mapping[str, str], key: str, *, fallback_name: str) -> Path:
    raw = env.get(key, "").strip()
    if raw:
        path = Path(raw)
    else:
        found = shutil.which(fallback_name)
        if found is None:
            raise HostWakeSettingsError(f"{key} must be set to an absolute executable path")
        path = Path(found)
    return _require_absolute(path)


def _require_absolute(path: Path) -> Path:
    if not path.is_absolute():
        raise HostWakeSettingsError(f"executable path must be absolute: {path}")
    return path


def _require_discord_id(name: str, value: str) -> None:
    if _DISCORD_ID.fullmatch(value) is None:
        raise HostWakeSettingsError(f"{name} must be a numeric Discord ID")


def _require_secret(name: str, value: SecretStr) -> None:
    if not value.get_secret_value().strip():
        raise HostWakeSettingsError(f"{name} is required")


def _require_http_url(name: str, value: str) -> None:
    if not value.startswith(("http://", "https://")):
        raise HostWakeSettingsError(f"{name} must be an HTTP URL")


def _require_local_url(name: str, value: str) -> None:
    if not value.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise HostWakeSettingsError(f"{name} must be localhost-only")


__all__ = ["HostWakeSettings", "HostWakeSettingsError"]
