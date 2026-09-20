from __future__ import annotations

from datetime import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents.academic_planner import discord_service
from app.core.config import Settings


@pytest.mark.parametrize(
    ("enabled", "expected_configured"),
    [(False, False), (True, True)],
)
def test_academic_discord_service_respects_memory_enabled_setting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    expected_configured: bool,
) -> None:
    captured: list[dict[str, Any]] = []

    class CapturingHandler:
        def __init__(self, **kwargs: Any) -> None:
            captured.append(kwargs)

    monkeypatch.setattr(discord_service, "NativeAcademicDiscordHandler", CapturingHandler)
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+pysqlite:///{tmp_path / 'academic-discord-worker.db'}",
        artifact_root=tmp_path / "artifacts",
        discord_bot_token="test-token",
        discord_application_id="111111111111111111",
        discord_academic_channel_id="222222222222222222",
        discord_academic_authorized_user_ids=[333333333333333333],
        discord_academic_message_content_enabled=True,
        academic_memory_enabled=enabled,
        academic_end_of_day_schedule=time(22, 15),
    )

    service = discord_service.create_academic_discord_service(settings)
    try:
        assert bool(captured[0]["memory_service"]) is expected_configured
        if expected_configured:
            assert captured[0]["memory_service"]._end_of_day_time == time(22, 15)
        assert captured[0]["calendar_semantic_interpreter"] is not None
    finally:
        service.close()


@pytest.mark.asyncio
async def test_academic_discord_service_scopes_callbacks_and_close_disposes_database() -> None:
    calls: list[tuple[object, object, object]] = []

    class Handler:
        def __init__(self) -> None:
            self._abort_check = "initial-abort"
            self._activity_sink = "initial-activity"

        async def __call__(self, message: object) -> object:
            calls.append((message, self._abort_check, self._activity_sink))
            return SimpleNamespace(status="handled")

    class Database:
        def __init__(self) -> None:
            self.disposed = 0

        def dispose(self) -> None:
            self.disposed += 1

    handler = Handler()
    database = Database()
    service = discord_service.AcademicDiscordService(  # type: ignore[arg-type]
        handler=handler,
        database=database,
    )
    abort_check = object()
    activity_sink = object()

    result = await service.handle(
        "message",
        abort_check=abort_check,  # type: ignore[arg-type]
        activity_sink=activity_sink,  # type: ignore[arg-type]
    )

    assert result.status == "handled"
    assert calls == [("message", abort_check, activity_sink)]
    assert handler._abort_check == "initial-abort"
    assert handler._activity_sink == "initial-activity"
    service.close()
    assert database.disposed == 1
