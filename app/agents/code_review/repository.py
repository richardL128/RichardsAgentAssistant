"""Safe, exact-commit repository checkout helpers.

The checkout boundary is intentionally small: callers provide a GitHub full
name and an optional local fixture source.  No shell is ever involved and no
credential is accepted as part of a command argument.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_FULL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_OUTPUT_LIMIT = 64 * 1024


class RepositoryCheckoutError(RuntimeError):
    """An operationally safe checkout error.

    ``diagnostic_code`` is suitable for persistence.  ``detail`` is bounded
    and redacted command diagnostics; it never contains command arguments.
    """

    def __init__(self, diagnostic_code: str, detail: str = "") -> None:
        self.diagnostic_code = diagnostic_code
        self.detail = detail[:500]
        super().__init__(diagnostic_code)


def _safe_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a minimal locale/path environment, optionally with credentials.

    Git credentials belong in environment/askpass plumbing, never argv.  The
    caller may pass an explicit environment containing an askpass helper; the
    value is not copied into diagnostics.
    """

    path = os.environ.get("PATH", "/usr/bin:/bin")
    result = {
        "PATH": path,
        "HOME": tempfile.gettempdir(),
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    if extra:
        result.update({str(key): str(value) for key, value in extra.items()})
    return result


def _run_git(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout_seconds: float,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute Git with bounded output and no shell."""

    if timeout_seconds <= 0:
        raise RepositoryCheckoutError("checkout_timeout")
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(  # noqa: S603
            list(argv),
            cwd=cwd,
            env=_safe_env(env),
            shell=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    process.kill()
                process.wait()
        raise RepositoryCheckoutError("checkout_timeout") from exc
    except OSError as exc:
        raise RepositoryCheckoutError("checkout_clone_failed") from exc
    if (
        len(stdout.encode("utf-8", errors="replace")) > _OUTPUT_LIMIT
        or len(stderr.encode("utf-8", errors="replace")) > _OUTPUT_LIMIT
    ):
        raise RepositoryCheckoutError("checkout_output_limit")
    if process.returncode != 0:
        # Git stderr is deliberately not retained: credential helpers and
        # remote implementations are outside this process's control.
        raise RepositoryCheckoutError("checkout_clone_failed")
    return subprocess.CompletedProcess(list(argv), process.returncode, stdout, stderr)


def _validate_source(repository: str, source: str | Path | None) -> str:
    if not _FULL_NAME_RE.fullmatch(repository):
        raise RepositoryCheckoutError("repository_not_allowlisted")
    if source is None:
        return f"https://github.com/{repository}.git"
    if isinstance(source, Path) or (not str(source).startswith(("http://", "https://"))):
        path = Path(source).expanduser()
        source_text = str(source)
        if source_text.startswith("git@github.com:"):
            remote = source_text.removeprefix("git@github.com:").removesuffix(".git").rstrip("/")
            if remote == repository:
                return source_text
            raise RepositoryCheckoutError("checkout_source_rejected")
        if not path.exists() or not path.is_dir():
            raise RepositoryCheckoutError("checkout_source_missing")
        return str(path.resolve())
    parsed = urlsplit(str(source))
    if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.username:
        raise RepositoryCheckoutError("checkout_source_rejected")
    if (
        not parsed.path.rstrip("/").endswith(f"/{repository}")
        and parsed.path.rstrip("/") != f"/{repository}.git"
    ):
        raise RepositoryCheckoutError("checkout_source_rejected")
    return str(source)


@contextmanager
def checkout_repository(
    repository: str,
    requested_sha: str,
    *,
    allowlist: Sequence[str] | set[str] | frozenset[str],
    source: str | Path | None = None,
    timeout_seconds: float = 120.0,
    checkout_root: str | Path | None = None,
    credential_env: Mapping[str, str] | None = None,
) -> Generator[Path, None, None]:
    """Clone a repository into a temporary directory and checkout one SHA.

    The context always removes the temporary directory, including failed
    clone/checkout and verification paths.  ``source`` is intended for local
    fixture repositories; production defaults to the matching GitHub HTTPS
    URL.  The allowlist is checked before any process is spawned.
    """

    if repository not in set(allowlist) or not _FULL_NAME_RE.fullmatch(repository):
        raise RepositoryCheckoutError("repository_not_allowlisted")
    if not _SHA_RE.fullmatch(requested_sha):
        raise RepositoryCheckoutError("sha_invalid")
    clone_source = _validate_source(repository, source)
    parent: Path | None = Path(checkout_root).resolve() if checkout_root is not None else None
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
    checkout_dir = Path(tempfile.mkdtemp(prefix="lifeagent-review-", dir=parent))
    try:
        _run_git(
            (
                "git",
                "clone",
                "--no-checkout",
                "--filter=blob:none",
                clone_source,
                str(checkout_dir),
            ),
            timeout_seconds=timeout_seconds,
            env=credential_env,
        )
        _run_git(
            ("git", "-C", str(checkout_dir), "checkout", "--detach", requested_sha),
            timeout_seconds=timeout_seconds,
            env=credential_env,
        )
        verified = _run_git(
            ("git", "-C", str(checkout_dir), "rev-parse", "HEAD"),
            timeout_seconds=timeout_seconds,
            env=credential_env,
        ).stdout.strip()
        if verified != requested_sha:
            raise RepositoryCheckoutError("sha_verification_failed")
        yield checkout_dir
    except RepositoryCheckoutError:
        raise
    finally:
        shutil.rmtree(checkout_dir, ignore_errors=True)


__all__ = ["RepositoryCheckoutError", "checkout_repository"]
