from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

BASH = "/bin/bash"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
START_SCRIPT = REPOSITORY_ROOT / "scripts/ollama_qwen_start.sh"
STATUS_SCRIPT = REPOSITORY_ROOT / "scripts/ollama_qwen_status.sh"
UNLOAD_SCRIPT = REPOSITORY_ROOT / "scripts/ollama_qwen_unload.sh"
HOST_RUNTIME_SCRIPT = REPOSITORY_ROOT / "scripts/lifeagent_host_runtime.sh"
DISCORD_WAKE_DAEMON_SCRIPT = REPOSITORY_ROOT / "scripts/lifeagent_discord_wake_daemon.sh"
LAUNCHD_COMMON_SCRIPT = REPOSITORY_ROOT / "scripts/lifeagent_launchd_common.sh"
OLLAMA_PLIST_TEMPLATE = REPOSITORY_ROOT / "scripts/com.lifeagent.ollama.plist.template"
DISCORD_WAKE_PLIST_TEMPLATE = REPOSITORY_ROOT / "scripts/com.lifeagent.discord-wake.plist.template"
EXECUTABLE_SCRIPT_PATHS = [
    START_SCRIPT,
    STATUS_SCRIPT,
    UNLOAD_SCRIPT,
    HOST_RUNTIME_SCRIPT,
    DISCORD_WAKE_DAEMON_SCRIPT,
]
SYNTAX_SCRIPT_PATHS = [*EXECUTABLE_SCRIPT_PATHS, LAUNCHD_COMMON_SCRIPT]


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _fake_tool_env(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path, Path]:
    fake_bin = tmp_path / "bin"
    state_dir = tmp_path / "state"
    runtime_dir = tmp_path / "runtime"
    launch_agents_dir = tmp_path / "LaunchAgents"
    fake_bin.mkdir()
    state_dir.mkdir()
    runtime_dir.mkdir()
    launch_agents_dir.mkdir()
    calls_file = state_dir / "calls.log"
    tags_file = state_dir / "tags.json"
    ps_file = state_dir / "ps.json"
    _write_json(tags_file, {"models": []})
    _write_json(ps_file, {"models": []})
    calls_file.write_text("", encoding="utf-8")

    _write_executable(
        fake_bin / "curl",
        """#!/usr/bin/env bash
set -eu
url="${!#}"
if [[ -f "$OLLAMA_FAKE_STATE_DIR/api_down" ]]; then
  exit 7
fi
case "$url" in
  */api/tags)
    cat "$OLLAMA_FAKE_TAGS_FILE"
    ;;
  */api/ps)
    cat "$OLLAMA_FAKE_PS_FILE"
    ;;
  *)
    exit 22
    ;;
esac
""",
    )
    _write_executable(
        fake_bin / "ollama",
        """#!/usr/bin/env bash
set -eu
cmd="${1:-}"
case "$cmd" in
  serve)
    printf 'serve:%s\\n' "${OLLAMA_HOST:-}" >> "$OLLAMA_FAKE_CALLS_FILE"
    if [[ "${OLLAMA_FAKE_SERVE_READY:-1}" == "1" ]]; then
      rm -f "$OLLAMA_FAKE_STATE_DIR/api_down"
    fi
    sleep "${OLLAMA_FAKE_SERVE_SLEEP:-30}"
    ;;
  pull)
    printf 'pull:%s\\n' "${2:-}" >> "$OLLAMA_FAKE_CALLS_FILE"
    if [[ -n "${OLLAMA_FAKE_TAGS_AFTER_PULL:-}" ]]; then
      cp "$OLLAMA_FAKE_TAGS_AFTER_PULL" "$OLLAMA_FAKE_TAGS_FILE"
    fi
    ;;
  stop)
    printf 'stop:%s\\n' "${2:-}" >> "$OLLAMA_FAKE_CALLS_FILE"
    ;;
  *)
    exit 2
    ;;
esac
""",
    )
    _write_executable(
        fake_bin / "launchctl",
        """#!/usr/bin/env bash
set -eu
cmd="${1:-}"
state_dir="$OLLAMA_FAKE_STATE_DIR"
calls_file="$OLLAMA_FAKE_CALLS_FILE"
label_from_target() {
  target="${1:-}"
  case "$target" in
    */com.lifeagent.ollama) printf '%s\\n' "com.lifeagent.ollama" ;;
    */com.lifeagent.discord-wake) printf '%s\\n' "com.lifeagent.discord-wake" ;;
    *.plist) basename "$target" .plist ;;
    *) printf '%s\\n' "$target" ;;
  esac
}
case "$cmd" in
  print)
    label="$(label_from_target "${2:-}")"
    if [[ -f "$state_dir/loaded-$label" ]]; then exit 0; fi
    exit 3
    ;;
  bootstrap)
    plist="${3:-}"
    label="$(label_from_target "$plist")"
    printf 'launchctl:bootstrap:%s\\n' "$label" >> "$calls_file"
    touch "$state_dir/loaded-$label"
    ;;
  bootout)
    target="${2:-}"
    if [[ "$target" == gui/* && "${3:-}" != "" ]]; then
      target="${3:-}"
    fi
    label="$(label_from_target "$target")"
    printf 'launchctl:bootout:%s\\n' "$label" >> "$calls_file"
    rm -f "$state_dir/loaded-$label"
    ;;
  enable)
    printf 'launchctl:enable:%s\\n' "${2:-}" >> "$calls_file"
    ;;
  kickstart)
    restart=no
    if [[ "${2:-}" == "-k" ]]; then
      restart=yes
      target="${3:-}"
    else
      target="${2:-}"
    fi
    label="$(label_from_target "$target")"
    printf 'launchctl:kickstart:%s:%s\\n' "$restart" "$label" >> "$calls_file"
    if [[ "$label" == "com.lifeagent.ollama" && "${OLLAMA_FAKE_KICKSTART_READY:-1}" == "1" ]]; then
      rm -f "$state_dir/api_down"
    fi
    ;;
  *)
    exit 2
    ;;
esac
""",
    )
    _write_executable(
        fake_bin / "docker",
        """#!/usr/bin/env bash
set -eu
printf 'docker:%s\\n' "$*" >> "$OLLAMA_FAKE_CALLS_FILE"
if [[ "${1:-}" == "image" && "${2:-}" == "inspect" ]]; then
  printf '%s\\n' "${OLLAMA_FAKE_IMAGE_ID:-sha256:lifeagent-test-image}"
  exit 0
fi
if [[ "${1:-}" == "compose" ]]; then
  exit 0
fi
exit 2
""",
    )
    _write_executable(
        fake_bin / "openssl",
        """#!/usr/bin/env bash
set -eu
if [[ "${1:-}" == "rand" && "${2:-}" == "-hex" && "${3:-}" == "32" ]]; then
  printf '%064d\\n' 0
  exit 0
fi
exit 2
""",
    )
    _write_executable(
        fake_bin / "python312",
        """#!/usr/bin/env bash
set -eu
printf 'python:%s\n' "$*" >> "$OLLAMA_FAKE_CALLS_FILE"
[ -n "${DISCORD_HOST_HANDOFF_SECRET:-}" ]
""",
    )

    env = {key: value for key, value in os.environ.items() if not key.startswith("OLLAMA_")}
    env.update(
        {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "OLLAMA_FAKE_STATE_DIR": str(state_dir),
            "OLLAMA_FAKE_TAGS_FILE": str(tags_file),
            "OLLAMA_FAKE_PS_FILE": str(ps_file),
            "OLLAMA_FAKE_CALLS_FILE": str(calls_file),
            "OLLAMA_QWEN_RUNTIME_DIR": str(runtime_dir),
            "LIFEAGENT_LAUNCH_AGENTS_DIR": str(launch_agents_dir),
            "LIFEAGENT_HOST_RUNTIME_DIR": str(runtime_dir),
            "LIFEAGENT_HOST_LOG_DIR": str(tmp_path / "logs"),
        }
    )
    return env, calls_file, tags_file, ps_file, runtime_dir


def _run_script(script: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [BASH, str(script), *args],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )


def test_ollama_qwen_scripts_have_valid_syntax_and_executable_modes() -> None:
    for script in SYNTAX_SCRIPT_PATHS:
        subprocess.run([BASH, "-n", str(script)], check=True)  # noqa: S603
    for script in EXECUTABLE_SCRIPT_PATHS:
        mode = script.stat().st_mode
        assert mode & stat.S_IXUSR
        assert mode & stat.S_IXGRP
        assert mode & stat.S_IXOTH


def test_start_uses_safe_env_allowlist_and_is_idempotent(tmp_path: Path) -> None:
    env, calls_file, tags_file, _ps_file, _runtime_dir = _fake_tool_env(tmp_path)
    model = "qwen-env:latest"
    digest = "sha256-good"
    _write_json(tags_file, {"models": [{"name": model, "digest": digest}]})
    pwned_file = tmp_path / "pwned"
    env_file = tmp_path / "life.env"
    env_file.write_text(
        "\n".join(
            [
                "OLLAMA_MODEL=wrong-file-model:latest",
                "OLLAMA_MODEL_DIGEST=wrong-file-digest",
                "OLLAMA_BASE_URL=http://host.docker.internal:11434",
                "DISCORD_BOT_TOKEN=secret-token",
                f"UNRELATED=$(touch {pwned_file})",
            ]
        ),
        encoding="utf-8",
    )
    env.update({"OLLAMA_MODEL": model, "OLLAMA_MODEL_DIGEST": digest})

    result = _run_script(START_SCRIPT, "--env-file", str(env_file), env=env)

    assert result.returncode == 0
    assert "Ollama API is reachable at http://127.0.0.1:11434" in result.stdout
    assert "Qwen runtime is ready" in result.stdout
    calls = calls_file.read_text(encoding="utf-8")
    assert "launchctl:bootstrap:com.lifeagent.ollama" in calls
    assert "launchctl:kickstart:no:com.lifeagent.ollama" in calls
    assert "serve:" not in calls
    assert not pwned_file.exists()
    assert "secret-token" not in result.stdout
    assert "secret-token" not in result.stderr


def test_start_requires_explicit_pull_for_missing_model(tmp_path: Path) -> None:
    env, calls_file, tags_file, _ps_file, _runtime_dir = _fake_tool_env(tmp_path)
    model = "qwen-missing:latest"
    env_file = tmp_path / "life.env"
    env_file.write_text(
        f"OLLAMA_MODEL={model}\nOLLAMA_MODEL_DIGEST=\n",
        encoding="utf-8",
    )
    after_pull_file = tmp_path / "after-pull.json"
    _write_json(after_pull_file, {"models": [{"name": model, "digest": "after-pull"}]})
    env["OLLAMA_FAKE_TAGS_AFTER_PULL"] = str(after_pull_file)

    missing = _run_script(START_SCRIPT, "--env-file", str(env_file), env=env)
    assert missing.returncode == 2
    assert "rerun with --pull" in missing.stderr
    assert "pull:" not in calls_file.read_text(encoding="utf-8")

    pulled = _run_script(START_SCRIPT, "--env-file", str(env_file), "--pull", env=env)
    assert pulled.returncode == 0
    assert f"pull:{model}" in calls_file.read_text(encoding="utf-8")
    assert json.loads(tags_file.read_text(encoding="utf-8"))["models"][0]["name"] == model


def test_start_installs_launchagent_and_waits_for_bounded_readiness(tmp_path: Path) -> None:
    env, calls_file, tags_file, _ps_file, _runtime_dir = _fake_tool_env(tmp_path)
    model = "qwen-start:latest"
    digest = "digest-start"
    _write_json(tags_file, {"models": [{"name": model, "digest": digest}]})
    env_file = tmp_path / "life.env"
    env_file.write_text(
        f"OLLAMA_MODEL={model}\nOLLAMA_MODEL_DIGEST={digest}\nOLLAMA_STARTUP_TIMEOUT_SECONDS=3\n",
        encoding="utf-8",
    )
    (tmp_path / "state" / "api_down").touch()
    result = _run_script(START_SCRIPT, "--env-file", str(env_file), env=env)

    assert result.returncode == 0
    calls = calls_file.read_text(encoding="utf-8")
    assert "launchctl:bootstrap:com.lifeagent.ollama" in calls
    assert "launchctl:kickstart:no:com.lifeagent.ollama" in calls
    plist = Path(env["LIFEAGENT_LAUNCH_AGENTS_DIR"]) / "com.lifeagent.ollama.plist"
    plist_text = plist.read_text(encoding="utf-8")
    assert str(REPOSITORY_ROOT) in plist_text
    assert "<key>RunAtLoad</key>" in plist_text
    assert "<key>KeepAlive</key>" in plist_text
    assert "<key>ThrottleInterval</key>" in plist_text
    assert "0.0.0.0:11434" in plist_text
    assert "nohup" not in calls
    assert not (Path(env["LIFEAGENT_HOST_RUNTIME_DIR"]) / "ollama.pid").exists()


def test_start_times_out_when_launchagent_api_never_becomes_ready(tmp_path: Path) -> None:
    env, calls_file, _tags_file, _ps_file, _runtime_dir = _fake_tool_env(tmp_path)
    env_file = tmp_path / "life.env"
    env_file.write_text("OLLAMA_STARTUP_TIMEOUT_SECONDS=0.1\n", encoding="utf-8")
    (tmp_path / "state" / "api_down").touch()
    env["OLLAMA_FAKE_KICKSTART_READY"] = "0"

    result = _run_script(START_SCRIPT, "--env-file", str(env_file), env=env)

    assert result.returncode == 1
    assert "did not become reachable" in result.stderr
    assert "launchctl:kickstart:no:com.lifeagent.ollama" in calls_file.read_text(encoding="utf-8")


def test_scripts_do_not_publish_docker_ports_or_configure_router_forwarding() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [START_SCRIPT, STATUS_SCRIPT, UNLOAD_SCRIPT, DISCORD_WAKE_DAEMON_SCRIPT]
    )
    forbidden_fragments = [
        "docker run",
        "-p 11434",
        "--publish",
        "pfctl",
        "upnpc",
        "nohup",
        "ollama.pid",
    ]
    assert not any(fragment in combined for fragment in forbidden_fragments)
    wake_daemon = DISCORD_WAKE_DAEMON_SCRIPT.read_text(encoding="utf-8")
    assert " build" not in wake_daemon
    assert " pull" not in wake_daemon


def test_status_reports_exit_codes_without_model_metadata(tmp_path: Path) -> None:
    env, _calls_file, tags_file, ps_file, _runtime_dir = _fake_tool_env(tmp_path)
    model = "qwen-status:latest"
    digest = "digest-status"
    env_file = tmp_path / "life.env"
    env_file.write_text(
        f"OLLAMA_MODEL={model}\nOLLAMA_MODEL_DIGEST={digest}\n",
        encoding="utf-8",
    )
    _write_json(tags_file, {"models": [{"name": model, "digest": digest, "size": 12345}]})
    _write_json(ps_file, {"models": [{"name": model, "size": 99999}]})

    ok = _run_script(STATUS_SCRIPT, "--env-file", str(env_file), env=env)
    assert ok.returncode == 0
    assert "ollama_launchd=unloaded" in ok.stdout
    assert "ollama_api=reachable" in ok.stdout
    assert "model_installed=yes" in ok.stdout
    assert "digest_match=yes" in ok.stdout
    assert "qwen_resident=yes" in ok.stdout
    assert "12345" not in ok.stdout
    assert "99999" not in ok.stdout

    _write_json(tags_file, {"models": []})
    missing = _run_script(STATUS_SCRIPT, "--env-file", str(env_file), env=env)
    assert missing.returncode == 2
    assert "model_installed=no" in missing.stdout

    _write_json(tags_file, {"models": [{"name": model, "digest": "wrong"}]})
    mismatch = _run_script(STATUS_SCRIPT, "--env-file", str(env_file), env=env)
    assert mismatch.returncode == 3
    assert "digest_match=no" in mismatch.stdout

    (tmp_path / "state" / "api_down").touch()
    unreachable = _run_script(STATUS_SCRIPT, "--env-file", str(env_file), env=env)
    assert unreachable.returncode == 1
    assert unreachable.stdout.strip().splitlines() == [
        "ollama_launchd=unloaded",
        "ollama_api=unreachable",
    ]


def test_unload_stops_only_configured_model(tmp_path: Path) -> None:
    env, calls_file, _tags_file, _ps_file, _runtime_dir = _fake_tool_env(tmp_path)
    env_file = tmp_path / "life.env"
    env_file.write_text(
        "OLLAMA_MODEL=qwen-unload:latest\nUNRELATED_MODEL=other:latest\n",
        encoding="utf-8",
    )

    result = _run_script(UNLOAD_SCRIPT, "--env-file", str(env_file), env=env)

    assert result.returncode == 0
    assert "unloaded configured Qwen model: qwen-unload:latest" in result.stdout
    assert calls_file.read_text(encoding="utf-8") == "stop:qwen-unload:latest\n"


def test_plist_templates_have_launchd_supervision_fields_and_no_secrets() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [OLLAMA_PLIST_TEMPLATE, DISCORD_WAKE_PLIST_TEMPLATE]
    )

    for required in [
        "<key>WorkingDirectory</key>",
        "<key>KeepAlive</key>",
        "<key>RunAtLoad</key>",
        "<key>ThrottleInterval</key>",
        "<key>StandardOutPath</key>",
        "<key>StandardErrorPath</key>",
    ]:
        assert required in combined
    assert "__REPO_DIR__" in combined
    assert "__LOG_DIR__" in combined
    assert "DISCORD_BOT_TOKEN" not in combined
    assert "NOTION_TOKEN" not in combined
    assert "GITHUB_PRIVATE_KEY" not in combined


def test_host_runtime_deploy_builds_once_preflights_and_installs_agents(tmp_path: Path) -> None:
    env, calls_file, _tags_file, _ps_file, runtime_dir = _fake_tool_env(tmp_path)
    env["LIFEAGENT_TEST_COMMAND"] = "true"
    env_file = tmp_path / "life.env"
    env_file.write_text("OLLAMA_HOST=127.0.0.1:11434\n", encoding="utf-8")

    result = _run_script(HOST_RUNTIME_SCRIPT, "deploy", "--env-file", str(env_file), env=env)

    assert result.returncode == 0
    calls = calls_file.read_text(encoding="utf-8")
    assert calls.count("docker:compose --env-file") == 4
    assert f"docker:compose --env-file {env_file} build api" in calls
    assert f"docker:compose --env-file {env_file} up -d --no-build postgres" in calls
    assert f"docker:compose --env-file {env_file} run --rm api alembic upgrade head" in calls
    assert f"docker:compose --env-file {env_file} stop api worker-academic-planner" in calls
    assert "launchctl:bootstrap:com.lifeagent.ollama" in calls
    assert "launchctl:bootstrap:com.lifeagent.discord-wake" in calls
    assert "launchctl:kickstart:yes:com.lifeagent.ollama" in calls
    assert "launchctl:kickstart:yes:com.lifeagent.discord-wake" in calls
    image_file = runtime_dir / "deployed-image-id"
    secret_file = runtime_dir / "discord-wake-hmac.key"
    assert image_file.read_text(encoding="utf-8").strip() == "sha256:lifeagent-test-image"
    assert secret_file.read_text(encoding="utf-8").strip() == "0" * 64
    assert stat.S_IMODE(image_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600
    assert "0" * 64 not in result.stdout


def test_host_runtime_status_and_uninstall_report_both_launchagents(tmp_path: Path) -> None:
    env, _calls_file, tags_file, ps_file, _runtime_dir = _fake_tool_env(tmp_path)
    model = "qwen-status:latest"
    digest = "digest-status"
    env_file = tmp_path / "life.env"
    env_file.write_text(f"OLLAMA_MODEL={model}\nOLLAMA_MODEL_DIGEST={digest}\n", encoding="utf-8")
    _write_json(tags_file, {"models": [{"name": model, "digest": digest}]})
    _write_json(ps_file, {"models": [{"name": model}]})

    install = _run_script(HOST_RUNTIME_SCRIPT, "install", "--env-file", str(env_file), env=env)
    assert install.returncode == 0
    status_result = _run_script(HOST_RUNTIME_SCRIPT, "status", "--env-file", str(env_file), env=env)
    uninstall = _run_script(HOST_RUNTIME_SCRIPT, "uninstall", "--env-file", str(env_file), env=env)

    assert status_result.returncode == 0
    assert "discord_wake_launchd=loaded" in status_result.stdout
    assert "ollama_launchd=loaded" in status_result.stdout
    assert uninstall.returncode == 0
    assert not (Path(env["LIFEAGENT_LAUNCH_AGENTS_DIR"]) / "com.lifeagent.ollama.plist").exists()
    assert not (
        Path(env["LIFEAGENT_LAUNCH_AGENTS_DIR"]) / "com.lifeagent.discord-wake.plist"
    ).exists()


def test_discord_wake_daemon_runs_native_python_without_build_or_pull(tmp_path: Path) -> None:
    env, calls_file, _tags_file, _ps_file, runtime_dir = _fake_tool_env(tmp_path)
    fake_bin = Path(env["PATH"].split(":", 1)[0])
    env_file = tmp_path / "life.env"
    env_file.write_text(
        "\n".join(
            (
                "DISCORD_BOT_TOKEN=test-token",
                "DISCORD_APPLICATION_ID=111111111111111111",
                "DISCORD_ACADEMIC_CHANNEL_ID=222222222222222222",
                "DISCORD_ACADEMIC_AUTHORIZED_USER_IDS=[333333333333333333]",
                "DISCORD_ACADEMIC_MESSAGE_CONTENT_ENABLED=true",
            )
        ),
        encoding="utf-8",
    )
    secret_file = runtime_dir / "discord-wake-hmac.key"
    secret_file.write_text("secret-not-printed\n", encoding="utf-8")

    result = _run_script(
        DISCORD_WAKE_DAEMON_SCRIPT,
        "--env-file",
        str(env_file),
        "--hmac-secret-file",
        str(secret_file),
        "--python",
        str(fake_bin / "python312"),
        "--docker",
        str(fake_bin / "docker"),
        "--launchctl",
        str(fake_bin / "launchctl"),
        "--ollama",
        str(fake_bin / "ollama"),
        env=env,
    )

    assert result.returncode == 0
    calls = calls_file.read_text(encoding="utf-8")
    assert "python:-m app.host.daemon" in calls
    assert " build" not in calls
    assert " pull" not in calls
    assert "secret-not-printed" not in result.stdout
    assert "secret-not-printed" not in result.stderr
