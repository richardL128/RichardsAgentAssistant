"""Streamed model thinking in the editable Discord progress message."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.agents.academic_planner.discord_harness import NativeAcademicDiscordHandler
from app.connectors.discord import (
    DiscordAcademicProgressHandle,
    DiscordAcademicProgressReporter,
    _render_progress_content,
)

_INTERVAL = 0.05


class FakeProgressDelivery:
    def __init__(self, *, fail_edits: int = 0) -> None:
        self.started: list[str] = []
        self.edits: list[str] = []
        self.fail_edits = fail_edits

    async def start_progress(self, *, root_event_id: str, content: str) -> object:
        self.started.append(content)
        return DiscordAcademicProgressHandle.model_construct(
            channel_id="111111111111111111",
            message_id="222222222222222222",
        )

    async def adopt_progress(self, *, root_event_id: str, message_id: str) -> object:
        raise AssertionError("not used")

    async def edit_progress(self, handle: object, *, content: str) -> object:
        if self.fail_edits:
            self.fail_edits -= 1
            raise ValueError("rate limited")
        self.edits.append(content)
        return None


def _reporter(
    delivery: FakeProgressDelivery,
    *,
    interval: float | None = _INTERVAL,
) -> DiscordAcademicProgressReporter:
    return DiscordAcademicProgressReporter(
        delivery=delivery,  # pyright: ignore[reportArgumentType]
        root_event_id="333333333333333333",
        thinking_edit_interval_seconds=interval,
    )


async def _settle() -> None:
    await asyncio.sleep(_INTERVAL * 3)


def _turn(number: int) -> dict[str, object]:
    return {"phase": "model_turn_started", "model_turn_number": number, "model_turn_limit": 12}


@pytest.mark.asyncio
async def test_thinking_streams_as_quoted_tail_under_stages() -> None:
    delivery = FakeProgressDelivery()
    reporter = _reporter(delivery)
    await reporter.start("runtime_ready")
    await reporter.update(_turn(1))

    await reporter.stream_thinking("The user asks about ")
    await reporter.stream_thinking("their math course.")
    await _settle()

    latest = delivery.edits[-1]
    assert latest.startswith("- The Qwen runtime is ready.\n- Qwen is thinking")
    assert "**Thinking**\n> The user asks about their math course." in latest
    assert "||" not in latest


@pytest.mark.asyncio
async def test_thinking_edits_are_throttled() -> None:
    delivery = FakeProgressDelivery()
    reporter = _reporter(delivery)
    await reporter.start(_turn(1))

    for word in ("one ", "two ", "three "):
        await reporter.stream_thinking(word)
    await asyncio.sleep(0)
    assert delivery.edits == [
        "- Qwen is thinking through your request (turn 1).\n\n**Thinking**\n> one two three"
    ]

    # Deltas arriving right after an edit wait for the interval, then coalesce.
    await reporter.stream_thinking("four ")
    await reporter.stream_thinking("five")
    await asyncio.sleep(_INTERVAL / 5)
    assert len(delivery.edits) == 1
    await _settle()

    assert len(delivery.edits) == 2
    assert delivery.edits[-1].endswith("> one two three four five")


@pytest.mark.asyncio
async def test_terminal_render_keeps_thinking_behind_spoiler() -> None:
    delivery = FakeProgressDelivery()
    reporter = _reporter(delivery)
    await reporter.start(_turn(1))
    await reporter.stream_thinking("Check the calendar first.")
    await _settle()

    await reporter.finish_completed()
    await reporter.stream_thinking("late delta")
    await _settle()

    final = delivery.edits[-1]
    assert final.endswith("**Thinking**\n||Check the calendar first.||")
    assert "> " not in final
    assert "late delta" not in final


@pytest.mark.asyncio
async def test_new_model_turn_marks_thinking_section() -> None:
    delivery = FakeProgressDelivery()
    reporter = _reporter(delivery)
    await reporter.start(_turn(1))
    await reporter.stream_thinking("Look up courses.")
    await reporter.update({"phase": "tool_activity", "tool_activity": "course_data"})
    await reporter.update(_turn(2))
    await reporter.stream_thinking("Now answer.")
    await _settle()

    assert "> Look up courses.\n> (turn 2)\n> Now answer." in delivery.edits[-1]


@pytest.mark.asyncio
async def test_failed_thinking_edit_does_not_disable_stage_updates() -> None:
    delivery = FakeProgressDelivery()
    reporter = _reporter(delivery)
    await reporter.start(_turn(1))
    delivery.fail_edits = 1

    await reporter.stream_thinking("first thought")
    await _settle()
    await reporter.update({"phase": "reply_preparation"})

    assert delivery.edits
    assert "Preparing your reply." in delivery.edits[-1]
    assert "> first thought" in delivery.edits[-1]


@pytest.mark.asyncio
async def test_failed_stage_edit_still_disables_reporter() -> None:
    delivery = FakeProgressDelivery(fail_edits=1)
    reporter = _reporter(delivery)
    await reporter.start("runtime_ready")

    await reporter.update(_turn(1))
    await reporter.stream_thinking("ignored")
    await _settle()

    assert delivery.edits == []


@pytest.mark.asyncio
async def test_thinking_is_ignored_when_not_enabled() -> None:
    delivery = FakeProgressDelivery()
    reporter = _reporter(delivery, interval=None)
    await reporter.start(_turn(1))

    await reporter.stream_thinking("hidden")
    await _settle()
    await reporter.finish_completed()

    assert all("Thinking" not in content for content in [*delivery.started, *delivery.edits])


def test_render_prioritises_stages_and_bounds_thinking_tail() -> None:
    stages = [f"Stage number {index} with some descriptive text." for index in range(7)]
    thinking = " ".join(f"word{index}" for index in range(2_000))

    live = _render_progress_content(stages, thinking=thinking)
    final = _render_progress_content(stages, thinking=thinking, final=True)

    for content in (live, final):
        assert len(content) <= 2_000
        assert content.startswith("- Stage number 0")
        assert "word1999" in content
        assert "word0 " not in content
        assert "[truncated]" not in content
    assert "\n> …word" in live
    assert "\n||…word" in final
    assert final.endswith("word1999||")


def test_render_omits_thinking_when_stages_leave_no_room() -> None:
    content = _render_progress_content(["x" * 1_950], thinking="some thought")

    assert "Thinking" not in content


def test_render_escapes_discord_markdown_in_thinking() -> None:
    content = _render_progress_content(
        ["Working."],
        thinking="# plan\n- a || b `code` *bold*\n\n\n\nnext",
        final=True,
    )

    body = content.split("**Thinking**\n", 1)[1]
    assert body == "||\\# plan\n\\- a \\|\\| b \\`code\\` \\*bold\\*\nnext||"


class _ReporterFactoryDelivery:
    def __init__(self) -> None:
        self.kwargs: list[dict[str, object]] = []
        self.reporter = _reporter(FakeProgressDelivery())

    def create_progress_reporter(self, **kwargs: object) -> DiscordAcademicProgressReporter:
        self.kwargs.append(kwargs)
        return self.reporter


def _handler(
    delivery: _ReporterFactoryDelivery,
    thinking_edit_interval_seconds: float | None,
) -> NativeAcademicDiscordHandler:
    return NativeAcademicDiscordHandler(
        store=object(),
        delivery=delivery,
        allowed_channel_ids={"222222222222222222"},
        authorized_user_ids={"333333333333333333"},
        writer_provider=lambda: None,
        ollama_runtime=object(),
        agent_gateway=object(),  # pyright: ignore[reportArgumentType]
        agent_catalog=object(),
        assistant_user_id="444444444444444444",
        thinking_edit_interval_seconds=thinking_edit_interval_seconds,
    )


def _message() -> object:
    return SimpleNamespace(message_id="555555555555555555", progress_message_id=None)


def test_handler_streams_thinking_into_progress_reporter_when_enabled() -> None:
    delivery = _ReporterFactoryDelivery()
    handler = _handler(delivery, 1.5)

    reporter = handler._create_progress_reporter(_message(), attempt_number=1)  # pyright: ignore[reportPrivateUsage, reportArgumentType]
    sink = handler._reasoning_sink(reporter)  # pyright: ignore[reportPrivateUsage]

    assert delivery.kwargs[0]["thinking_edit_interval_seconds"] == 1.5
    assert sink == delivery.reporter.stream_thinking


def test_handler_does_not_stream_thinking_when_disabled() -> None:
    delivery = _ReporterFactoryDelivery()
    handler = _handler(delivery, None)

    reporter = handler._create_progress_reporter(_message(), attempt_number=1)  # pyright: ignore[reportPrivateUsage, reportArgumentType]

    assert "thinking_edit_interval_seconds" not in delivery.kwargs[0]
    assert handler._reasoning_sink(reporter) is None  # pyright: ignore[reportPrivateUsage]
    assert handler._reasoning_sink(None) is None  # pyright: ignore[reportPrivateUsage]
