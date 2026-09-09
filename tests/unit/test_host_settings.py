from pathlib import Path

import pytest
from pydantic import SecretStr

from app.host.settings import HostWakeSettings, HostWakeSettingsError


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


def test_host_settings_are_standalone_and_restrict_local_handoff(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    assert settings.compose_file == tmp_path / "compose.yaml"
    assert settings.backend_handoff_url.startswith("http://127.0.0.1:")
    assert settings.discord_academic_authorized_user_ids == frozenset({"333333333333333333"})


def test_host_settings_reject_noncanonical_compose_file(tmp_path: Path) -> None:
    with pytest.raises(HostWakeSettingsError):
        HostWakeSettings(
            repository_root=tmp_path,
            discord_bot_token=SecretStr("token"),
            discord_application_id="111111111111111111",
            discord_academic_channel_id="222222222222222222",
            discord_academic_authorized_user_ids=frozenset({"333333333333333333"}),
            host_handoff_secret=SecretStr("handoff-secret"),
            docker_executable=Path("/usr/local/bin/docker"),
            launchctl_executable=Path("/bin/launchctl"),
            ollama_executable=Path("/usr/local/bin/ollama"),
            compose_file=tmp_path / "other.yaml",
            outbox_path=tmp_path / ".artifacts" / "discord-wake" / "outbox.sqlite3",
            deployed_image_marker_path=(
                tmp_path / ".artifacts" / "discord-wake" / "deployed-image-id"
            ),
        )


def test_host_settings_from_env_requires_handoff_secret(tmp_path: Path) -> None:
    env = {
        "DISCORD_BOT_TOKEN": "token",
        "DISCORD_APPLICATION_ID": "111111111111111111",
        "DISCORD_ACADEMIC_CHANNEL_ID": "222222222222222222",
        "DISCORD_ACADEMIC_AUTHORIZED_USER_IDS": "[333333333333333333]",
        "DISCORD_ACADEMIC_MESSAGE_CONTENT_ENABLED": "true",
        "DISCORD_HOST_HANDOFF_SECRET": "handoff-secret",
        "LIFEAGENT_DOCKER": "/usr/local/bin/docker",
        "LIFEAGENT_LAUNCHCTL": "/bin/launchctl",
        "LIFEAGENT_OLLAMA": "/usr/local/bin/ollama",
    }

    loaded = HostWakeSettings.from_env(env, repository_root=tmp_path)

    assert loaded.host_handoff_secret.get_secret_value() == "handoff-secret"


def test_host_settings_from_env_requires_discord_handoff_secret(tmp_path: Path) -> None:
    env = {
        "DISCORD_BOT_TOKEN": "token",
        "DISCORD_APPLICATION_ID": "111111111111111111",
        "DISCORD_ACADEMIC_CHANNEL_ID": "222222222222222222",
        "DISCORD_ACADEMIC_AUTHORIZED_USER_IDS": "[333333333333333333]",
        "DISCORD_ACADEMIC_MESSAGE_CONTENT_ENABLED": "true",
        "LIFEAGENT_DOCKER": "/usr/local/bin/docker",
        "LIFEAGENT_LAUNCHCTL": "/bin/launchctl",
        "LIFEAGENT_OLLAMA": "/usr/local/bin/ollama",
    }

    with pytest.raises(HostWakeSettingsError, match="DISCORD_HOST_HANDOFF_SECRET"):
        HostWakeSettings.from_env(env, repository_root=tmp_path)


def test_host_settings_require_message_content_for_mention_wake(tmp_path: Path) -> None:
    env = {
        "DISCORD_BOT_TOKEN": "token",
        "DISCORD_APPLICATION_ID": "111111111111111111",
        "DISCORD_ACADEMIC_CHANNEL_ID": "222222222222222222",
        "DISCORD_ACADEMIC_AUTHORIZED_USER_IDS": "[333333333333333333]",
        "DISCORD_ACADEMIC_MESSAGE_CONTENT_ENABLED": "false",
        "DISCORD_HOST_HANDOFF_SECRET": "handoff-secret",
        "LIFEAGENT_DOCKER": "/usr/local/bin/docker",
        "LIFEAGENT_LAUNCHCTL": "/bin/launchctl",
        "LIFEAGENT_OLLAMA": "/usr/local/bin/ollama",
    }

    with pytest.raises(HostWakeSettingsError, match="MESSAGE_CONTENT_ENABLED"):
        HostWakeSettings.from_env(env, repository_root=tmp_path)


def test_host_modules_do_not_import_backend_runtime_layers() -> None:
    forbidden = (
        "app.core",
        "app.db",
        "app.llm",
        "app.agents",
        "sqlalchemy",
        "langgraph",
        "notion",
    )
    host_root = Path(__file__).parents[2] / "app" / "host"

    for path in host_root.glob("*.py"):
        source = path.read_text(encoding="utf-8").casefold()
        for name in forbidden:
            assert name not in source, f"{path.name} imports or mentions {name}"
