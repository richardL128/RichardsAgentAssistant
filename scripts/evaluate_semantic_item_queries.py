#!/usr/bin/env python3
# pyright: reportPrivateUsage=false
"""Evaluate configured-model semantic item routing without executing any tool."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("tests/fixtures/semantic_item_query_grounding.json"),
    )
    parser.add_argument("--ollama-base-url")
    return parser.parse_args()


async def _run(fixture: Path) -> int:
    from langchain_core.messages import HumanMessage, SystemMessage

    from app.agents.academic_planner.discord_harness import (
        _AcademicToolState,
        _system_message,
    )
    from app.agents.harness import TERMINAL_RESPONSE_TOOL_SCHEMA
    from app.llm.gateway import LLMGateway
    from app.llm.ollama_runtime import OllamaRuntime

    fixture_text = await asyncio.to_thread(fixture.read_text, encoding="utf-8")
    raw_cases: object = json.loads(fixture_text)
    if not isinstance(raw_cases, list):
        raise ValueError("semantic item evaluation fixture must be a list")
    cases = cast(list[dict[str, Any]], raw_cases)
    await OllamaRuntime().ensure_ready()
    gateway = LLMGateway()
    now = datetime.now(UTC)
    timezone = ZoneInfo("America/Toronto")
    state = _AcademicToolState(catalog=None, now=now, timezone=timezone)
    schemas = (
        *(
            tool.schema
            for tool in state.tools()
            if tool.name in {"search_calendar_items", "search_courses"}
        ),
        TERMINAL_RESPONSE_TOOL_SCHEMA,
    )
    system = SystemMessage(content=_system_message(now, timezone))
    failures = 0
    for case in cases:
        prompt = str(case["prompt"])
        result = await gateway.invoke_native(
            messages=(system, HumanMessage(content=prompt)),
            tools=schemas,
        )
        output = result.output
        calls = output.tool_calls if output is not None else []
        call = cast(dict[str, Any], calls[0]) if len(calls) == 1 else {}
        raw_args = call.get("args")
        args = cast(dict[str, Any], raw_args) if isinstance(raw_args, dict) else {}
        passed = bool(
            result.error_code is None
            and len(calls) == 1
            and call.get("name") == case["tool"]
            and (
                case["tool"] != "search_calendar_items"
                or (
                    args.get("view") == case["view"]
                    and isinstance(args.get("temporal"), dict)
                    and args["temporal"].get("scope") == case["temporal_scope"]
                )
            )
        )
        failures += not passed
        print(
            json.dumps(
                {
                    "prompt": prompt,
                    "passed": passed,
                    "tool": call.get("name"),
                    "args": args,
                    "error_code": result.error_code,
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
    return 1 if failures else 0


def main() -> int:
    args = _arguments()
    if args.ollama_base_url:
        os.environ["OLLAMA_BASE_URL"] = str(args.ollama_base_url)
    return asyncio.run(_run(args.fixture))


if __name__ == "__main__":
    raise SystemExit(main())
