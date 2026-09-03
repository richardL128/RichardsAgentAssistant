"""Small, deterministic helpers for model response parsing and telemetry."""

from __future__ import annotations

import json
from collections.abc import Mapping
from math import ceil
from typing import Any, cast

from pydantic import BaseModel


def estimate_tokens(value: str) -> int:
    """Return a conservative, dependency-free token estimate.

    Ollama reports model-specific token counts after a call.  Before a call,
    a character-based estimate is deliberately used so this boundary does not
    need a tokenizer (or model weights) just to enforce the input budget.
    """

    return ceil(len(value) / 4) if value else 0


def response_text(response: Any) -> str:
    """Extract text from an Ollama/LangChain response without logging it."""

    if isinstance(response, str):
        return response
    if isinstance(response, Mapping):
        response_map = cast(Mapping[str, Any], response)
        content = response_map.get("content")
        if content is not None:
            return _content_text(content)
        return json.dumps(response_map, ensure_ascii=False, separators=(",", ":"))
    content = getattr(response, "content", None)
    if content is not None:
        return _content_text(content)
    return str(response)


def response_metadata(response: Any) -> Mapping[str, Any]:
    """Return non-secret usage metadata exposed by a model response."""

    metadata = getattr(response, "response_metadata", None)
    if isinstance(metadata, Mapping):
        return cast(Mapping[str, Any], metadata)
    if isinstance(response, Mapping):
        response_map = cast(Mapping[str, Any], response)
        response_metadata_value = response_map.get("response_metadata")
        if isinstance(response_metadata_value, Mapping):
            return cast(Mapping[str, Any], response_metadata_value)
    return {}


def usage_metadata(response: Any) -> Mapping[str, Any]:
    """Return LangChain's normalized usage metadata when available."""

    metadata = getattr(response, "usage_metadata", None)
    if isinstance(metadata, Mapping):
        return cast(Mapping[str, Any], metadata)
    if isinstance(response, Mapping):
        response_map = cast(Mapping[str, Any], response)
        usage_metadata_value = response_map.get("usage_metadata")
        if isinstance(usage_metadata_value, Mapping):
            return cast(Mapping[str, Any], usage_metadata_value)
    return {}


def reported_token_count(response: Any, *, input_tokens: bool) -> int | None:
    """Extract a reported input/output count from common Ollama shapes."""

    usage = usage_metadata(response)
    usage_key = "input_tokens" if input_tokens else "output_tokens"
    value = usage.get(usage_key)
    if isinstance(value, int) and value >= 0:
        return value

    metadata = response_metadata(response)
    metadata_key = "prompt_eval_count" if input_tokens else "eval_count"
    value = metadata.get(metadata_key)
    if isinstance(value, int) and value >= 0:
        return value
    return None


def parse_model_json(raw_text: str, response_model: type[BaseModel]) -> BaseModel:
    """Parse JSON and validate it against the requested Pydantic model."""

    candidate = raw_text.strip()
    if candidate.startswith("```") and candidate.endswith("```"):
        lines = candidate.splitlines()
        if len(lines) >= 2:
            candidate = "\n".join(lines[1:-1]).strip()
    parsed = json.loads(candidate)
    return response_model.model_validate(parsed)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        content_map = cast(Mapping[str, Any], content)
        text = content_map.get("text")
        if isinstance(text, str):
            return text
        return json.dumps(content_map, ensure_ascii=False, separators=(",", ":"))
    if isinstance(content, list):
        parts: list[str] = []
        for block in cast(list[Any], content):
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping):
                block_map = cast(Mapping[str, Any], block)
                text = block_map.get("text")
                if isinstance(text, str):
                    parts.append(text)
        if parts:
            return "".join(parts)
    return str(cast(Any, content))


__all__ = [
    "estimate_tokens",
    "parse_model_json",
    "reported_token_count",
    "response_metadata",
    "response_text",
    "usage_metadata",
]
