"""Bounded, path-safe Git diff packet assembly tests (Phase 3).

These exercise changed-line extraction across file statuses, ``--find-renames``
parsing, path-safety rejection, truncation, and the stable ``PacketAssemblyError``
diagnostic codes.  Fixtures are real local Git repositories, mirroring the
``_fixture_repo`` pattern in ``test_code_review_repository.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.agents.code_review.contracts import RiskLevel
from app.agents.code_review.packet import PacketAssemblyError, assemble_review_packet


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["/usr/bin/git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _init_repo(tmp_path: Path, name: str = "source") -> Path:
    source = tmp_path / name
    source.mkdir()
    _git(source, "init", "--quiet")
    _git(source, "config", "user.email", "test@example.invalid")
    _git(source, "config", "user.name", "Test")
    return source


def _commit(source: Path, message: str) -> str:
    _git(source, "add", "-A")
    _git(source, "commit", "--quiet", "-m", message)
    return _git(source, "rev-parse", "HEAD")


def test_changed_lines_and_counts_for_added_modified_deleted(tmp_path: Path) -> None:
    source = _init_repo(tmp_path)
    (source / "keep.py").write_text("a = 1\nb = 2\nc = 3\n")
    (source / "gone.py").write_text("print('bye')\n")
    base = _commit(source, "base")
    (source / "keep.py").write_text("a = 1\nb = 20\nc = 3\nd = 4\n")
    (source / "gone.py").unlink()
    (source / "added.py").write_text("x = 1\ny = 2\n")
    head = _commit(source, "head")

    packet = assemble_review_packet("acme/example", base, head, source)
    by_path = {file.path: file for file in packet.files}

    assert by_path["keep.py"].status == "modified"
    assert by_path["keep.py"].changed_lines == [2, 4]
    assert by_path["keep.py"].additions == 2
    assert by_path["keep.py"].deletions == 1

    assert by_path["added.py"].status == "added"
    assert by_path["added.py"].changed_lines == [1, 2]

    assert by_path["gone.py"].status == "deleted"
    assert by_path["gone.py"].changed_lines == []
    assert by_path["gone.py"].additions == 0


def test_binary_file_is_flagged_and_has_no_changed_lines(tmp_path: Path) -> None:
    source = _init_repo(tmp_path)
    (source / "logo.bin").write_bytes(bytes(range(256)) * 8)
    base = _commit(source, "base")
    (source / "logo.bin").write_bytes(bytes(range(255, -1, -1)) * 8)
    head = _commit(source, "head")

    packet = assemble_review_packet("acme/example", base, head, source)
    assert packet.files[0].path == "logo.bin"
    assert packet.files[0].is_binary is True
    assert packet.files[0].changed_lines == []


def test_find_renames_parsing_yields_renamed_status(tmp_path: Path) -> None:
    source = _init_repo(tmp_path)
    (source / "old.py").write_text("def a():\n    return 1\n" * 6)
    base = _commit(source, "base")
    (source / "pkg").mkdir()
    _git(source, "mv", "old.py", "pkg/new.py")
    head = _commit(source, "rename")

    packet = assemble_review_packet("acme/example", base, head, source)
    assert [file.path for file in packet.files] == ["pkg/new.py"]
    assert packet.files[0].status == "renamed"


def test_rename_with_edit_keeps_new_path_and_renamed_status(tmp_path: Path) -> None:
    source = _init_repo(tmp_path)
    (source / "old.py").write_text("\n".join(f"line{i}" for i in range(40)) + "\n")
    base = _commit(source, "base")
    _git(source, "mv", "old.py", "renamed.py")
    (source / "renamed.py").write_text("\n".join(f"line{i}" for i in range(40)) + "\nextra\n")
    head = _commit(source, "rename-edit")

    packet = assemble_review_packet("acme/example", base, head, source)
    assert packet.files[0].path == "renamed.py"
    assert packet.files[0].status == "renamed"
    # The per-file diff does not re-run rename detection, so the whole new file
    # reads as an addition.  Line 41 (the added tail) is certainly present.
    assert 41 in packet.files[0].changed_lines


def test_control_character_in_path_is_rejected_as_unsafe(tmp_path: Path) -> None:
    source = _init_repo(tmp_path)
    (source / "ok.py").write_text("x = 1\n")
    base = _commit(source, "base")
    try:
        (source / "a\tb.py").write_text("y = 2\n")
    except OSError:
        pytest.skip("filesystem rejects control characters in names")
    head = _commit(source, "weird")

    with pytest.raises(PacketAssemblyError) as raised:
        assemble_review_packet("acme/example", base, head, source)
    assert raised.value.diagnostic_code == "unsafe_repository_path"


def test_truncation_marker_is_appended_when_a_patch_exceeds_the_cap(tmp_path: Path) -> None:
    source = _init_repo(tmp_path)
    (source / "big.py").write_text("seed = 0\n")
    base = _commit(source, "base")
    (source / "big.py").write_text("value = 1\n" * 600)
    head = _commit(source, "big")

    packet = assemble_review_packet("acme/example", base, head, source, max_patch_chars=600)
    patch = packet.files[0].patch
    assert "[diff truncated]" in patch
    assert len(patch) <= 600


def test_sha_invalid_and_bounds_and_checkout_missing_codes(tmp_path: Path) -> None:
    source = _init_repo(tmp_path)
    (source / "x.py").write_text("x = 1\n")
    base = _commit(source, "base")
    (source / "x.py").write_text("x = 2\n")
    head = _commit(source, "head")

    with pytest.raises(PacketAssemblyError) as bad_sha:
        assemble_review_packet("acme/example", "nothex", head, source)
    assert bad_sha.value.diagnostic_code == "sha_invalid"

    with pytest.raises(PacketAssemblyError) as bad_bounds:
        assemble_review_packet("acme/example", base, head, source, max_files=0)
    assert bad_bounds.value.diagnostic_code == "packet_bounds_invalid"

    with pytest.raises(PacketAssemblyError) as bad_chars:
        assemble_review_packet("acme/example", base, head, source, max_patch_chars=0)
    assert bad_chars.value.diagnostic_code == "packet_bounds_invalid"

    with pytest.raises(PacketAssemblyError) as missing:
        assemble_review_packet("acme/example", base, head, tmp_path / "no-such-dir")
    assert missing.value.diagnostic_code == "checkout_missing"


def test_file_limit_diagnostic(tmp_path: Path) -> None:
    source = _init_repo(tmp_path)
    for index in range(3):
        (source / f"f{index}.py").write_text("x = 1\n")
    base = _commit(source, "base")
    for index in range(3):
        (source / f"f{index}.py").write_text("x = 2\n")
    head = _commit(source, "head")

    with pytest.raises(PacketAssemblyError) as raised:
        assemble_review_packet("acme/example", base, head, source, max_files=2)
    assert raised.value.diagnostic_code == "packet_file_limit"


def test_packet_character_budget_is_whole_packet_not_per_file(tmp_path: Path) -> None:
    """Current behaviour: the total patch budget is capped at ``max_patch_chars``.

    See the defect note in the executor report for ``packet.py:166`` -- the
    per-file cap is reused as the whole-packet budget, so a multi-file diff
    whose files are each well under the cap still raises
    ``packet_character_limit``.  If that is fixed, this assertion flips to a
    successful assembly and the ``max_patch_chars=200_000`` branch below becomes
    the default expectation.
    """

    source = _init_repo(tmp_path)
    for index in range(10):
        (source / f"f{index}.py").write_text("seed = 0\n")
    base = _commit(source, "base")
    for index in range(10):
        (source / f"f{index}.py").write_text("value = 1\n" * 800)
    head = _commit(source, "head")

    with pytest.raises(PacketAssemblyError) as raised:
        assemble_review_packet("acme/example", base, head, source)
    assert raised.value.diagnostic_code == "packet_character_limit"

    # Raising the cap lets the same diff through, confirming the budget is the
    # single ``max_patch_chars`` value rather than ``cap * max_files``.
    packet = assemble_review_packet("acme/example", base, head, source, max_patch_chars=200_000)
    assert len(packet.files) == 10
    assert sum(len(file.patch) for file in packet.files) > 30_000


def test_empty_diff_produces_no_files_and_medium_risk(tmp_path: Path) -> None:
    source = _init_repo(tmp_path)
    (source / "x.py").write_text("x = 1\n")
    base = _commit(source, "base")

    packet = assemble_review_packet("acme/example", base, base, source)
    assert packet.files == []
    assert packet.risk is RiskLevel.MEDIUM


def test_risk_and_provenance_overrides_are_passed_through(tmp_path: Path) -> None:
    source = _init_repo(tmp_path)
    (source / "src").mkdir()
    (source / "src" / "auth.py").write_text("check = 1\n")
    base = _commit(source, "base")
    (source / "src" / "auth.py").write_text("check = 2\n")
    head = _commit(source, "head")

    derived = assemble_review_packet("acme/example", base, head, source)
    assert derived.risk is RiskLevel.HIGH  # "auth" marker

    overridden = assemble_review_packet(
        "acme/example",
        base,
        head,
        source,
        risk=RiskLevel.LOW,
        risk_reasons=["operator override"],
        instructions_provenance=["AGENTS.md@" + head],
    )
    assert overridden.risk is RiskLevel.LOW
    assert overridden.risk_reasons == ["operator override"]
    assert overridden.instructions_provenance == ["AGENTS.md@" + head]
