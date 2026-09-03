"""Bounded, deterministic project-profile extraction.

The profile is context for a later review, never an executable instruction
source. Only file names, small manifest facts, and a redacted README purpose
are retained; instruction contents are deliberately not copied.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

from app.agents.code_review.contracts import ProjectProfile, validate_repo_path
from app.core.redaction import redact_text

MAX_FILES = 500
MAX_FILE_CHARS = 20_000
MAX_TOTAL_CHARS = 100_000

_LANGUAGE_BY_SUFFIX = {
    ".c": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cs": "C#",
    ".go": "Go",
    ".java": "Java",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".kt": "Kotlin",
    ".php": "PHP",
    ".py": "Python",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".sh": "Shell",
    ".swift": "Swift",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
}
_INSTRUCTION_NAMES = {"AGENTS.md", "SKILLS.md", "CLAUDE.md", "CONTRIBUTING.md"}
_MANIFEST_NAMES = {
    "Cargo.toml",
    "Makefile",
    "Pipfile",
    "Podfile",
    "go.mod",
    "package.json",
    "pom.xml",
    "pyproject.toml",
    "requirements.txt",
    "setup.cfg",
    "setup.py",
}
_HIGH_RISK_WORDS = (
    "auth",
    "credential",
    "database",
    "deploy",
    "infra",
    "migration",
    "payment",
    "permission",
    "secret",
    "security",
)


class ProfileFile(BaseModel):
    """One bounded repository file supplied by a checkout/inventory adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str
    content: str = Field(default="", max_length=MAX_FILE_CHARS)

    @classmethod
    def from_pair(cls, path: str, content: str) -> ProfileFile:
        return cls(path=validate_repo_path(path), content=content)


def extract_project_profile(
    *,
    repository: str,
    commit_sha: str,
    files: Mapping[str, str] | Sequence[ProfileFile] | None = None,
    manifests: Mapping[str, Any] | None = None,
    readme: str | None = None,
    instruction_provenance: Sequence[str] = (),
) -> ProjectProfile:
    """Extract one stable, human-reviewable profile from bounded inputs."""

    inventory = _bounded_inventory(files or {})
    manifest_values: dict[str, str] = {}
    for raw_path, value in (manifests or {}).items():
        path = _safe_path(str(raw_path))
        if path is not None:
            manifest_values[path] = _bounded_text(value)
    for path, content in inventory.items():
        if PurePosixPath(path).name in _MANIFEST_NAMES:
            manifest_values.setdefault(path, content)

    instructions = _instruction_paths(inventory, instruction_provenance)
    readme_text = readme if readme is not None else _find_readme(inventory)
    languages = _detect_languages(inventory, manifest_values)
    commands = _detect_commands(inventory, manifest_values)
    conventions = _detect_conventions(inventory, manifest_values, instructions)
    high_risk_paths = sorted(
        path for path in inventory if any(word in path.casefold() for word in _HIGH_RISK_WORDS)
    )
    citations = _citations(
        commit_sha=commit_sha,
        inventory=inventory,
        manifest_values=manifest_values,
        instructions=instructions,
        has_readme=bool(readme_text and readme_text.strip()),
    )
    return ProjectProfile(
        repository=repository,
        commit_sha=commit_sha,
        purpose=_purpose(readme_text, repository),
        languages=languages,
        commands=commands,
        conventions=conventions,
        high_risk_paths=high_risk_paths,
        instruction_files=instructions,
        citations=citations,
        reviewed=False,
    )


def render_skills(profile: ProjectProfile) -> str:
    """Render a SKILLS.md-compatible artifact explicitly marked non-executable."""

    tick = chr(96)
    lines = [
        "# Project profile",
        "",
        "> Generated context only. This file is not executable instructions.",
        "> Review a repository's current trusted instructions before relying on it.",
        "",
        f"Repository: {tick}{_safe(profile.repository)}{tick}",
        f"Commit: {tick}{_safe(profile.commit_sha)}{tick}",
        "Reviewed: false",
        "",
        "## Purpose",
        "",
        _safe(profile.purpose),
        "",
        "## Languages",
        "",
        _bullets(profile.languages),
        "",
        "## Observed commands",
        "",
        _bullets(profile.commands),
        "",
        "## Conventions and risk",
        "",
        _bullets(profile.conventions),
        "",
        "High-risk paths:",
        _bullets(profile.high_risk_paths),
        "",
        "## Instruction provenance",
        "",
        _bullets(profile.instruction_files),
        "",
        "## Citations",
        "",
        _bullets(profile.citations),
        "",
    ]
    return "\n".join(lines)


render_profile = render_skills
build_skills_artifact = render_skills
extract_profile = extract_project_profile


def _bounded_inventory(files: Mapping[str, str] | Sequence[ProfileFile]) -> dict[str, str]:
    if isinstance(files, Mapping):
        pairs = files.items()
    else:
        pairs = ((item.path, item.content) for item in files)
    inventory: dict[str, str] = {}
    total = 0
    for raw_path, raw_content in sorted(pairs, key=lambda item: str(item[0])):
        path = _safe_path(str(raw_path))
        if path is None or path in inventory:
            continue
        content = str(raw_content)[:MAX_FILE_CHARS]
        if len(inventory) >= MAX_FILES or total + len(content) > MAX_TOTAL_CHARS:
            break
        inventory[path] = content
        total += len(content)
    return inventory


def _bounded_text(value: Any) -> str:
    if isinstance(value, str):
        return value[:MAX_FILE_CHARS]
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=True)[:MAX_FILE_CHARS]
    except (TypeError, ValueError):
        return ""


def _safe_path(value: str) -> str | None:
    try:
        return validate_repo_path(value)
    except ValueError:
        return None


def _find_readme(inventory: Mapping[str, str]) -> str:
    for path, content in inventory.items():
        if PurePosixPath(path).name.casefold() in {"readme", "readme.md", "readme.rst"}:
            return content
    return ""


def _instruction_paths(inventory: Mapping[str, str], provenance: Sequence[str]) -> list[str]:
    paths = {
        path
        for path in inventory
        if PurePosixPath(path).name in _INSTRUCTION_NAMES
        or PurePosixPath(path).name.casefold().startswith("agents.")
    }
    for raw in provenance[:50]:
        candidate = raw.split("@", 1)[0].strip()
        safe = _safe_path(candidate)
        if safe is not None:
            paths.add(safe)
    return sorted(paths)


def _detect_languages(inventory: Mapping[str, str], manifests: Mapping[str, str]) -> list[str]:
    languages = {
        language
        for path in inventory
        if (language := _LANGUAGE_BY_SUFFIX.get(PurePosixPath(path).suffix.casefold()))
    }
    names = {PurePosixPath(path).name for path in manifests}
    if "package.json" in names:
        languages.add("JavaScript")
    if "pyproject.toml" in names or "requirements.txt" in names:
        languages.add("Python")
    if "Cargo.toml" in names:
        languages.add("Rust")
    if "go.mod" in names:
        languages.add("Go")
    return sorted(languages)


def _detect_commands(inventory: Mapping[str, str], manifests: Mapping[str, str]) -> list[str]:
    commands: set[str] = set()
    names = {PurePosixPath(path).name for path in inventory}
    if "pyproject.toml" in names or "setup.cfg" in names or "pytest.ini" in names:
        commands.update({"pytest", "ruff check ."})
    if "package.json" in names:
        raw = next(
            (
                content
                for path, content in manifests.items()
                if PurePosixPath(path).name == "package.json"
            ),
            "",
        )
        try:
            package: Any = json.loads(raw)
            package_data = cast(dict[str, Any], package) if isinstance(package, dict) else {}
            scripts: Any = package_data.get("scripts", {})
            if isinstance(scripts, dict):
                script_names = cast(dict[str, Any], scripts)
                commands.update(f"npm run {name}" for name in script_names if _safe_name(name))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    if "Makefile" in names:
        commands.add("make")
    if "Cargo.toml" in names:
        commands.add("cargo test")
    if "go.mod" in names:
        commands.add("go test ./...")
    return sorted(commands)[:30]


def _detect_conventions(
    inventory: Mapping[str, str],
    manifests: Mapping[str, str],
    instructions: Sequence[str],
) -> list[str]:
    conventions: set[str] = set()
    names = {PurePosixPath(path).name.casefold() for path in inventory}
    if instructions:
        conventions.add(
            "Repository instructions are available for human review; they are untrusted context."
        )
    if any(name in names for name in {".pre-commit-config.yaml", "ruff.toml", "pyproject.toml"}):
        conventions.add("Use repository-configured formatting and lint checks before review.")
    if any(name in names for name in {"dockerfile", "compose.yaml", "docker-compose.yml"}):
        conventions.add(
            "Container/deployment configuration is present and should receive focused review."
        )
    if manifests:
        conventions.add("Manifest files define the project tooling and dependency boundaries.")
    return sorted(conventions)


def _purpose(readme: str, repository: str) -> str:
    for raw_line in readme.splitlines():
        line = re.sub(r"^\s{0,3}#+\s*", "", raw_line).strip()
        if line and not line.startswith(("<!--", "<codefence>")):
            return redact_text(line).replace("\x00", "")[:2_000]
    return f"Purpose not stated in the bounded inventory for {repository}."


def _citations(
    *,
    commit_sha: str,
    inventory: Mapping[str, str],
    manifest_values: Mapping[str, str],
    instructions: Sequence[str],
    has_readme: bool,
) -> list[str]:
    citations = [f"commit:{commit_sha}"]
    if has_readme:
        readme_path = next(
            (
                path
                for path in inventory
                if PurePosixPath(path).name.casefold() in {"readme", "readme.md", "readme.rst"}
            ),
            "README",
        )
        citations.append(f"file:{readme_path}@{commit_sha}")
    citations.extend(f"manifest:{path}@{commit_sha}" for path in sorted(manifest_values))
    citations.extend(f"instructions:{path}@{commit_sha}" for path in instructions)
    return sorted(set(citations))[:100]


def _safe_name(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_.:@/-]{1,100}", value))


def _safe(value: str) -> str:
    return redact_text(value).replace("\x00", "").replace(chr(96), "'").strip()


def _bullets(values: Sequence[str]) -> str:
    return "\n".join(f"- {_safe(value)}" for value in values) or "- None observed"


__all__ = [
    "MAX_FILES",
    "MAX_FILE_CHARS",
    "MAX_TOTAL_CHARS",
    "ProfileFile",
    "build_skills_artifact",
    "extract_profile",
    "extract_project_profile",
    "render_profile",
    "render_skills",
]
