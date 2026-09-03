"""Bounded, path-safe Git diff packet assembly."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable, Sequence
from pathlib import Path

from app.agents.code_review.contracts import (
    ChangedFile,
    ReviewPacket,
    RiskLevel,
    ScannerRun,
    validate_repo_path,
)
from app.agents.code_review.risk import classify_risk

_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_HUNK_RE = re.compile(r"^@@ -(?:\d+)(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_MAX_PATCH_CHARS = 30_000
_MAX_OUTPUT_BYTES = 2_000_000


class PacketAssemblyError(RuntimeError):
    """A safe packet construction error with a stable diagnostic code."""

    def __init__(self, diagnostic_code: str) -> None:
        self.diagnostic_code = diagnostic_code
        super().__init__(diagnostic_code)


def _git(
    checkout: Path,
    args: Sequence[str],
    *,
    timeout_seconds: float,
    max_bytes: int = _MAX_OUTPUT_BYTES,
) -> str:
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(  # noqa: S603
            ["/usr/bin/git", "-C", str(checkout), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            shell=False,
            start_new_session=True,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(Path.cwd()),
                "LANG": "C",
                "LC_ALL": "C",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
            },
        )
        stdout, _stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        if process is None:
            raise PacketAssemblyError("diff_unavailable") from exc
        process.kill()
        process.communicate()
        raise PacketAssemblyError("diff_timeout") from exc
    except OSError as exc:
        raise PacketAssemblyError("diff_unavailable") from exc
    if len(stdout) > max_bytes:
        raise PacketAssemblyError("diff_output_limit")
    if process.returncode != 0:
        raise PacketAssemblyError("diff_failed")
    return stdout.decode("utf-8", errors="replace")


def _changed_lines(patch: str) -> list[int]:
    lines: list[int] = []
    new_line = 0
    for line in patch.splitlines():
        match = _HUNK_RE.match(line)
        if match:
            new_line = int(match.group(1))
            continue
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            lines.append(new_line)
            new_line += 1
        elif line.startswith(("-", "\\")):
            continue
        elif new_line:
            new_line += 1
    return sorted(set(lines))


def _counts(patch: str) -> tuple[int, int, bool]:
    additions = sum(
        1 for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++")
    )
    deletions = sum(
        1 for line in patch.splitlines() if line.startswith("-") and not line.startswith("---")
    )
    binary = "Binary files" in patch or "GIT binary patch" in patch
    return additions, deletions, binary


def _parse_names(raw: str) -> list[tuple[str, str, str | None]]:
    fields = raw.split("\x00")
    result: list[tuple[str, str, str | None]] = []
    index = 0
    while index < len(fields) and fields[index]:
        status = fields[index]
        index += 1
        if status.startswith(("R", "C")):
            if index + 1 > len(fields):
                raise PacketAssemblyError("diff_parse_failed")
            old = fields[index]
            new = fields[index + 1] if index + 1 < len(fields) else ""
            index += 2
            result.append(("renamed" if status.startswith("R") else "copied", new, old))
        else:
            if index >= len(fields):
                raise PacketAssemblyError("diff_parse_failed")
            name = fields[index]
            index += 1
            mapped = {"A": "added", "M": "modified", "D": "deleted", "T": "type_changed"}.get(
                status[:1]
            )
            if mapped is None:
                raise PacketAssemblyError("diff_parse_failed")
            result.append((mapped, name, None))
    return result


def assemble_review_packet(
    repository: str,
    base_sha: str,
    head_sha: str,
    checkout: str | Path,
    *,
    scanner_runs: Iterable[ScannerRun] = (),
    instructions_provenance: Iterable[str] = (),
    risk: RiskLevel | None = None,
    risk_reasons: Iterable[str] | None = None,
    timeout_seconds: float = 30.0,
    max_files: int = 200,
    max_patch_chars: int = _MAX_PATCH_CHARS,
) -> ReviewPacket:
    """Assemble a bounded review packet for ``base_sha..head_sha``."""

    if not _SHA_RE.fullmatch(base_sha) or not _SHA_RE.fullmatch(head_sha):
        raise PacketAssemblyError("sha_invalid")
    if max_files < 1 or max_patch_chars < 1:
        raise PacketAssemblyError("packet_bounds_invalid")
    root = Path(checkout).resolve()
    if not root.is_dir():
        raise PacketAssemblyError("checkout_missing")
    names = _parse_names(
        _git(
            root,
            ["diff", "--name-status", "-z", "--find-renames", f"{base_sha}..{head_sha}"],
            timeout_seconds=timeout_seconds,
        )
    )
    if len(names) > max_files:
        raise PacketAssemblyError("packet_file_limit")
    files: list[ChangedFile] = []
    remaining = min(max_patch_chars, _MAX_PATCH_CHARS * max_files)
    for status, path, old_path in names:
        try:
            if any(ord(character) < 32 for character in path) or (
                old_path is not None and any(ord(character) < 32 for character in old_path)
            ):
                raise ValueError("control character in repository path")
            safe_path = validate_repo_path(path)
            if old_path is not None:
                validate_repo_path(old_path)
        except ValueError as exc:
            raise PacketAssemblyError("unsafe_repository_path") from exc
        patch = _git(
            root,
            [
                "diff",
                "--no-ext-diff",
                "--binary",
                "--full-index",
                f"{base_sha}..{head_sha}",
                "--",
                path,
            ],
            timeout_seconds=timeout_seconds,
            max_bytes=_MAX_OUTPUT_BYTES,
        )
        truncated = len(patch) > max_patch_chars
        if truncated:
            patch = patch[: max(0, max_patch_chars - 34)] + "\n[diff truncated]\n"
        additions, deletions, binary = _counts(patch)
        files.append(
            ChangedFile(
                path=safe_path,
                status=status,  # type: ignore[arg-type]
                additions=additions,
                deletions=deletions,
                patch=patch,
                changed_lines=_changed_lines(patch),
                is_binary=binary,
            )
        )
        remaining -= len(patch)
        if remaining < 0:
            raise PacketAssemblyError("packet_character_limit")
    levels, reasons = classify_risk(file.path for file in files)
    return ReviewPacket(
        repository=repository,
        base_sha=base_sha,
        head_sha=head_sha,
        risk=risk or levels,
        risk_reasons=list(risk_reasons) if risk_reasons is not None else reasons,
        files=files,
        scanner_runs=list(scanner_runs),
        instructions_provenance=list(instructions_provenance),
    )


build_review_packet = assemble_review_packet
assemble_packet = assemble_review_packet


__all__ = [
    "PacketAssemblyError",
    "assemble_packet",
    "assemble_review_packet",
    "build_review_packet",
]
