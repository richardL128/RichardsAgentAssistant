from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from pydantic import SecretStr

from app.host.learn_bridge import (
    LearnBridgeRequestVerifier,
    LearnBridgeSettings,
    LearnBrowserAdapter,
    NonceStore,
    SnapshotRequest,
    _announcement_records,
    _fragments,
    _is_login_page,
    _normalized_body,
    _parse_dom_datetime,
    _scheduled_item_records,
    build_http_server,
    clean_text,
    ensure_private_profile_dir,
    signed_headers,
    verify_bridge_response,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts/lifeagent_learn_bridge.sh"
PLIST_TEMPLATE = REPOSITORY_ROOT / "scripts/com.lifeagent.learn-bridge.plist.template"
SECRET = SecretStr("s" * 32)


class StubLearnAdapter:
    def __init__(self, status: Literal["ready", "login_required", "browser_unavailable"] = "ready"):
        self.status = status
        self.snapshots = 0

    def health(self) -> Literal["ready", "login_required", "browser_unavailable"]:
        return self.status

    def snapshot(self, request: SnapshotRequest):
        self.snapshots += 1
        return {
            "status": self.status,
            "generated_at": "2026-09-19T12:00:00+00:00",
            "courses": [
                {
                    "org_unit_id": "12345",
                    "code": "ECE 240",
                    "name": "ECE 240 - Fall 2026",
                    "term": "Fall 2026",
                    "active": True,
                    "url": "https://learn.uwaterloo.ca/d2l/home/12345",
                }
            ],
            "scheduled_items": [],
            "announcements": [],
            "echo_course_ids": request.get("course_ids", []),
        }

    def login(self) -> None:
        return

    def close(self) -> None:
        return


def _settings(tmp_path: Path, **overrides: object) -> LearnBridgeSettings:
    values = {
        "secret": SECRET,
        "profile_dir": tmp_path / "host-profile",
        "repository_root": REPOSITORY_ROOT,
        "port": 0,
    }
    values.update(overrides)
    return LearnBridgeSettings(**values)


@contextmanager
def _server(
    settings: LearnBridgeSettings,
    adapter: LearnBrowserAdapter,
) -> Iterator[tuple[str, LearnBridgeSettings]]:
    server = build_http_server(settings, adapter)
    host, port = server.server_address[:2]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        effective = LearnBridgeSettings(
            secret=settings.secret,
            profile_dir=settings.profile_dir,
            repository_root=settings.repository_root,
            host=str(host),
            port=int(port),
            max_request_bytes=settings.max_request_bytes,
            max_response_bytes=settings.max_response_bytes,
        )
        yield f"http://{host}:{port}", effective
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(
    base_url: str,
    settings: LearnBridgeSettings,
    method: str,
    path: str,
    *,
    body: bytes = b"",
    nonce: str = "nonce-1234567890123456",
    timestamp: int | None = None,
    headers: dict[str, str] | None = None,
):
    all_headers = {
        **signed_headers(
            secret=settings.secret,
            method=method,
            path=path,
            body=body,
            nonce=nonce,
            timestamp=timestamp if timestamp is not None else int(time.time()),
        ),
        **(headers or {}),
    }
    request = Request(  # noqa: S310
        f"{base_url}{path}",
        data=body if method == "POST" else None,
        headers=all_headers,
        method=method,
    )
    return urlopen(request, timeout=5)  # noqa: S310


def _read_signed_response(response, settings: LearnBridgeSettings, path: str) -> dict[str, object]:
    body = response.read()
    timestamp = int(response.headers["x-lifeagent-learn-timestamp"])
    nonce = response.headers["x-lifeagent-learn-nonce"]
    signature = response.headers["x-lifeagent-learn-signature"]
    assert verify_bridge_response(
        secret=settings.secret,
        status_code=response.status,
        path=path,
        timestamp=timestamp,
        nonce=nonce,
        body=body,
        signature=signature,
    )
    return json.loads(body.decode("utf-8"))


def test_settings_require_loopback_profile_outside_repo_and_private_permissions(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    assert settings.host == "127.0.0.1"

    with pytest.raises(ValueError, match="loopback"):
        _settings(tmp_path, host="192.0.2.10")
    with pytest.raises(ValueError, match="outside the repository"):
        _settings(tmp_path, profile_dir=REPOSITORY_ROOT / ".learn-profile")

    public_profile = tmp_path / "public-profile"
    public_profile.mkdir(mode=0o755)
    ensure_private_profile_dir(public_profile)
    assert stat.S_IMODE(public_profile.stat().st_mode) == 0o700


def test_health_requires_valid_signature_and_signs_response(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with _server(settings, StubLearnAdapter(),) as (base_url, effective):
        with pytest.raises(HTTPError) as unsigned:
            urlopen(f"{base_url}/health", timeout=5)  # noqa: S310
        assert unsigned.value.code == 401
        unsigned_payload = _read_signed_response(unsigned.value, effective, "/health")
        assert unsigned_payload == {"error": "unsigned"}

        response = _request(base_url, effective, "GET", "/health")
        payload = _read_signed_response(response, effective, "/health")

    assert payload == {"status": "ready"}


def test_rejects_stale_replayed_bad_signature_and_oversized_requests(tmp_path: Path) -> None:
    settings = _settings(tmp_path, max_request_bytes=32)
    with _server(settings, StubLearnAdapter(),) as (base_url, effective):
        with pytest.raises(HTTPError) as stale:
            _request(base_url, effective, "GET", "/health", timestamp=1)
        assert stale.value.code == 401
        assert _read_signed_response(stale.value, effective, "/health") == {"error": "stale"}

        ok = _request(base_url, effective, "GET", "/health", nonce="nonce-unique-123456")
        assert _read_signed_response(ok, effective, "/health") == {"status": "ready"}

        with pytest.raises(HTTPError) as replay:
            _request(base_url, effective, "GET", "/health", nonce="nonce-unique-123456")
        assert replay.value.code == 409
        assert _read_signed_response(replay.value, effective, "/health") == {"error": "replay"}

        with pytest.raises(HTTPError) as bad_signature:
            _request(
                base_url,
                effective,
                "GET",
                "/health",
                nonce="nonce-bad-signature1",
                headers={"x-lifeagent-learn-signature": "sha256=bad"},
            )
        assert bad_signature.value.code == 401

        body = json.dumps({"course_ids": ["12345"], "padding": "x" * 64}).encode("utf-8")
        with pytest.raises(HTTPError) as oversized:
            _request(
                base_url,
                effective,
                "POST",
                "/v1/snapshot",
                body=body,
                nonce="nonce-large-1234567",
            )
        assert oversized.value.code == 413
        assert _read_signed_response(oversized.value, effective, "/v1/snapshot") == {
            "error": "request_too_large"
        }


def test_snapshot_is_bounded_validated_and_does_not_include_browser_secrets(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    adapter = StubLearnAdapter()
    with _server(settings, adapter) as (base_url, effective):
        body = json.dumps({"course_ids": ["12345"], "item_limit": 1}).encode("utf-8")
        response = _request(base_url, effective, "POST", "/v1/snapshot", body=body)
        payload = _read_signed_response(response, effective, "/v1/snapshot")

    encoded = json.dumps(payload).casefold()
    assert payload["status"] == "ready"
    assert adapter.snapshots == 1
    assert "cookie" not in encoded
    assert "localstorage" not in encoded
    assert "authorization" not in encoded
    assert "storage_state" not in encoded


def test_request_verifier_rejects_non_local_clients(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    verifier = LearnBridgeRequestVerifier(settings, NonceStore(settings.nonce_ttl_seconds))
    headers = signed_headers(
        secret=settings.secret,
        method="GET",
        path="/health",
        timestamp=1_800_000_000,
        nonce="nonce-nonlocal-1234",
    )

    with pytest.raises(Exception, match="non_local"):
        verifier.verify(
            client_host="192.0.2.10",
            method="GET",
            path="/health",
            headers=headers,
            body=b"",
        )


def test_clean_text_removes_html_controls_and_bounds_content() -> None:
    cleaned = clean_text("Hello&nbsp;\x00 <b>world</b>\n\nagain", max_chars=18)
    assert cleaned == "Hello <b>world</b..."


def test_announcement_dom_metadata_is_separate_from_body_dates() -> None:
    class Page:
        def eval_on_selector_all(self, selector: str, script: str):
            assert "article" in selector
            assert "time[datetime]" in script
            return [
                {
                    "source_key": "announcement-42",
                    "href": "https://learn.uwaterloo.ca/d2l/le/news/1/42",
                    "text": "The assessment is due 2026-12-04.",
                    "published_at": "2026-09-18T13:30:00-04:00",
                    "updated_at": None,
                    "attachments_present": False,
                }
            ]

    records = _announcement_records(Page())

    assert records[0]["published_at"] == "2026-09-18T13:30:00-04:00"
    parsed = _parse_dom_datetime(records[0]["published_at"])
    assert parsed is not None
    assert parsed.isoformat() == "2026-09-18T17:30:00+00:00"
    assert _parse_dom_datetime("2026-12-04") is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://learn.uwaterloo.ca/d2l/home", False),
        ("https://learn.uwaterloo.ca/d2l/login", True),
        ("https://learn.uwaterloo.ca/", False),
        ("https://login.microsoftonline.com/example", True),
    ],
)
def test_login_detection_uses_only_origin_and_explicit_login_path(
    url: str,
    expected: bool,
) -> None:
    class Page:
        def __init__(self) -> None:
            self.url = url

    assert _is_login_page(Page()) is expected


def test_scheduled_item_uses_stable_dom_identity_and_machine_dates() -> None:
    class Page:
        def eval_on_selector_all(self, selector: str, script: str):
            assert "listitem" in selector
            assert "data-event-id" in script
            return [
                {
                    "source_key": "calendar-event-7",
                    "href": "https://learn.uwaterloo.ca/d2l/le/calendar/1/7",
                    "text": "Tutorial moved",
                    "start_at": "2026-09-21T10:30:00-04:00",
                    "due_at": None,
                    "end_at": "2026-09-21T11:20:00-04:00",
                    "completed": False,
                }
            ]

    records = _scheduled_item_records(Page())

    assert records[0]["source_key"] == "calendar-event-7"
    assert records[0]["start_at"] == "2026-09-21T10:30:00-04:00"


def test_announcement_body_fragmenting_preserves_all_bounded_paragraphs() -> None:
    normalized = _normalized_body("First paragraph.\n\n" + "x" * 9_000)
    fragments = _fragments(normalized, max_fragments=80, max_chars=4_000)

    assert "".join(fragments) == normalized.replace("\n\n", "")
    assert all(len(fragment) <= 4_000 for fragment in fragments)


def test_learn_bridge_script_and_plist_are_safe(tmp_path: Path) -> None:
    subprocess.run(["/bin/bash", "-n", str(SCRIPT)], check=True)  # noqa: S603
    assert os.access(SCRIPT, os.X_OK)
    plist = PLIST_TEMPLATE.read_text(encoding="utf-8")
    assert "com.lifeagent.learn-bridge" in plist
    assert "LEARN_BRIDGE_HMAC_SECRET" not in plist
    assert "WATERLOO" not in plist.upper()
    assert "<key>KeepAlive</key>" in plist
    assert "<key>RunAtLoad</key>" in plist

    fake_python = tmp_path / "python"
    fake_python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_python.chmod(0o755)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("LEARN_BRIDGE")
    }
    result = subprocess.run(  # noqa: S603
        [
            "/bin/bash",
            str(SCRIPT),
            "health",
            "--python",
            str(fake_python),
            "--secret-file",
            str(tmp_path / "secret.key"),
            "--profile-dir",
            str(tmp_path / "profile"),
        ],
        check=False,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0
    assert stat.S_IMODE((tmp_path / "secret.key").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "profile").stat().st_mode) == 0o700
    assert "created LEARN bridge HMAC secret file" in result.stdout


def test_learn_bridge_install_passes_valid_launchd_targets(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls_file = tmp_path / "launchctl.calls"
    launchctl = fake_bin / "launchctl"
    launchctl.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$LAUNCHCTL_CALLS_FILE\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    launchctl.chmod(0o755)
    fake_uv = tmp_path / "uv"
    fake_uv.write_text(
        "#!/usr/bin/env bash\n"
        "mkdir -p \"$UV_PROJECT_ENVIRONMENT/bin\"\n"
        f"printf '%s\\n' '#!/usr/bin/env bash' 'exec \"{sys.executable}\" \"$@\"' "
        '> "$UV_PROJECT_ENVIRONMENT/bin/python"\n'
        'chmod 755 "$UV_PROJECT_ENVIRONMENT/bin/python"\n',
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    bridge_dir = tmp_path / "learn-bridge"
    runtime = bridge_dir / "runtime"
    env_file = tmp_path / "lifeagent.env"
    env_file.write_text(
        "LEARN_BRIDGE_MAX_COURSES=42\n"
        "LEARN_BRIDGE_HMAC_SECRET=must-not-copy\n"
        "DISCORD_BOT_TOKEN=must-not-copy\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "LAUNCHCTL_CALLS_FILE": str(calls_file),
        "LIFEAGENT_UV": str(fake_uv),
        "LEARN_BRIDGE_DIR": str(bridge_dir),
        "LEARN_BRIDGE_STARTUP_WAIT_SECONDS": "0",
    }

    result = subprocess.run(  # noqa: S603
        [
            "/bin/bash",
            str(SCRIPT),
            "install",
            "--env-file",
            str(env_file),
            "--python",
            sys.executable,
            "--secret-file",
            str(tmp_path / "secret.key"),
            "--profile-dir",
            str(tmp_path / "profile"),
        ],
        check=False,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    uid = os.getuid()
    calls = calls_file.read_text(encoding="utf-8").splitlines()
    assert calls[0] == f"bootout gui/{uid}/com.lifeagent.learn-bridge"
    assert calls[1].startswith(f"bootstrap gui/{uid} ")
    assert calls[2] == f"kickstart -k gui/{uid}/com.lifeagent.learn-bridge"
    plist = home / "Library" / "LaunchAgents" / "com.lifeagent.learn-bridge.plist"
    plist_text = plist.read_text(encoding="utf-8")
    assert str(runtime) in plist_text
    assert str(REPOSITORY_ROOT) not in plist_text
    runtime_env = (runtime / ".env").read_text(encoding="utf-8")
    assert "LEARN_BRIDGE_MAX_COURSES=42" in runtime_env
    assert "must-not-copy" not in runtime_env
    assert "DISCORD_BOT_TOKEN" not in runtime_env
    assert (runtime / "app" / "host" / "learn_bridge.py").is_file()
    assert (runtime / "scripts" / "lifeagent_learn_bridge.sh").is_file()
