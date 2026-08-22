"""Containment and atomicity of the workflow spec WRITE path (issue #583, Bolt 3, unit 2).

Guards ``workflow_spec_service._write_contained_spec_bytes`` — the only function permitted
to write bytes to a spec path. Its failure mode is a shipped CodeQL ``py/path-injection``
alert, not a wrong result, so containment is proven by **attempted escapes** rather than by
inspection (BR-3A2-11).

Five rejection cases, and the fifth is the one that matters most: a symlink AT the target
path pointing INSIDE the base. Containment cannot catch that one — both paths are contained
— yet following it writes a spec the caller never named (SR-3A2-2). A suite that only
exercised escapes would miss it entirely.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from cli_agent_orchestrator.constants import WORKFLOW_MAX_SPEC_BYTES
from cli_agent_orchestrator.services import workflow_spec_service as svc


def _write(base: Path, name: str, data: bytes = b"x = 1\n") -> str:
    return svc._write_contained_spec_bytes(str(base / name), data, base_dir=str(base))


def _dir_entries(base: Path) -> list[str]:
    """Every entry including dotfiles — a leftover temp file must be visible here."""
    return sorted(os.listdir(base))


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
def test_writes_the_exact_bytes_and_returns_the_resolved_path(tmp_path: Path) -> None:
    """BR-3A2-9: bytes in, same bytes on disk. No validation, no transformation."""
    payload = b"INPUTS = {'a': {'type': 'string'}}\n# \xc2\xa9 unicode survives\n"
    returned = _write(tmp_path, "wf.py", payload)

    assert returned == os.path.realpath(str(tmp_path / "wf.py"))
    assert isinstance(returned, str), "must be a bare str, never a Path (BR-3A2-3)"
    assert (tmp_path / "wf.py").read_bytes() == payload


def test_leaves_no_temp_file_behind_on_success(tmp_path: Path) -> None:
    """SR-3A2-4: the temp file is consumed by the rename, not left beside the target."""
    _write(tmp_path, "wf.py")
    assert _dir_entries(tmp_path) == ["wf.py"]


def test_overwrites_an_existing_spec_atomically(tmp_path: Path) -> None:
    (tmp_path / "wf.py").write_bytes(b"old\n")
    _write(tmp_path, "wf.py", b"new\n")
    assert (tmp_path / "wf.py").read_bytes() == b"new\n"
    assert _dir_entries(tmp_path) == ["wf.py"]


# ---------------------------------------------------------------------------
# Containment — the four escapes (BR-3A2-11)
# ---------------------------------------------------------------------------
def test_rejects_an_absolute_path_outside_the_base(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.py"
    with pytest.raises(ValueError, match="escapes its validated directory"):
        svc._write_contained_spec_bytes(str(outside), b"x", base_dir=str(tmp_path))
    assert not outside.exists()
    assert _dir_entries(tmp_path) == []


def test_rejects_a_dotdot_traversal_that_resolves_outside(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes its validated directory"):
        svc._write_contained_spec_bytes(
            str(tmp_path / ".." / "escaped.py"), b"x", base_dir=str(tmp_path)
        )
    assert not (tmp_path.parent / "escaped.py").exists()
    assert _dir_entries(tmp_path) == []


def test_rejects_a_symlink_inside_the_base_pointing_out(tmp_path: Path) -> None:
    """The classic escape: a link inside the base whose destination is outside it."""
    target = tmp_path.parent / "escape-target.py"
    link = tmp_path / "link.py"
    link.symlink_to(target)

    with pytest.raises(ValueError):
        svc._write_contained_spec_bytes(str(link), b"x", base_dir=str(tmp_path))
    assert not target.exists()
    assert _dir_entries(tmp_path) == ["link.py"], "no temp file created"


def test_rejects_a_path_resolving_to_the_base_directory_itself(tmp_path: Path) -> None:
    """BR-3A2-2's strict form: the guard must not admit ``real_path == safe_base``.

    A compound ``!= base and not startswith`` guard would let this branch through to a
    sink, which is why the single positive form is required.
    """
    with pytest.raises(ValueError, match="escapes its validated directory"):
        svc._write_contained_spec_bytes(str(tmp_path), b"x", base_dir=str(tmp_path))


def test_rejects_an_empty_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="required"):
        svc._write_contained_spec_bytes("   ", b"x", base_dir=str(tmp_path))


# ---------------------------------------------------------------------------
# The case containment CANNOT catch (SR-3A2-2)
# ---------------------------------------------------------------------------
def test_rejects_a_symlink_target_pointing_inside_the_base(tmp_path: Path) -> None:
    """SR-3A2-2 — the reason this check exists at all.

    Both paths are inside the base, so the containment guard is satisfied and would let
    the write through. Following the link would land the caller's bytes in ``real.py``,
    a spec it never named. This is the test that would go red if the ``islink`` check
    were removed, and NO containment test would notice.
    """
    real = tmp_path / "real.py"
    real.write_bytes(b"the real spec\n")
    link = tmp_path / "alias.py"
    link.symlink_to(real)

    with pytest.raises(ValueError, match="symlink"):
        svc._write_contained_spec_bytes(str(link), b"clobbered\n", base_dir=str(tmp_path))

    assert real.read_bytes() == b"the real spec\n", "the linked-to spec must be untouched"


# ---------------------------------------------------------------------------
# The size cap (SR-3A2-3)
# ---------------------------------------------------------------------------
def test_rejects_an_oversized_payload_before_creating_any_file(tmp_path: Path) -> None:
    """Ordering is the requirement: a rejected write must leave NOTHING on disk."""
    with pytest.raises(ValueError, match="exceeds"):
        _write(tmp_path, "wf.py", b"x" * (WORKFLOW_MAX_SPEC_BYTES + 1))
    assert _dir_entries(tmp_path) == [], "no target and no temp file"


def test_accepts_a_payload_exactly_at_the_cap(tmp_path: Path) -> None:
    _write(tmp_path, "wf.py", b"x" * WORKFLOW_MAX_SPEC_BYTES)
    assert (tmp_path / "wf.py").stat().st_size == WORKFLOW_MAX_SPEC_BYTES


# ---------------------------------------------------------------------------
# The index-glob hazard (SR-3A2-5)
# ---------------------------------------------------------------------------
def test_the_temp_name_cannot_match_the_index_glob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SR-3A2-5, asserted against the ACTUAL generated name, not the prefix constant.

    ``rebuild_index_from_files`` globs ``*.yaml``/``*.yml``/``*.py`` in this very
    directory on every list/get/delete, so a matching temp name would be parsed and
    indexed while half-written. Checking only the prefix literal would let a future
    change to it break this silently.
    """
    captured: list[str] = []
    real_mkstemp = svc.tempfile.mkstemp

    def _spy(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        fd, name = real_mkstemp(*args, **kwargs)  # type: ignore[arg-type]
        captured.append(name)
        return fd, name

    monkeypatch.setattr(svc.tempfile, "mkstemp", _spy)
    _write(tmp_path, "wf.py")

    assert captured, "mkstemp must have been used"
    for name in captured:
        base = os.path.basename(name)
        assert base.startswith("."), f"{base} must be a dotfile"
        for ext in (".py", ".yaml", ".yml"):
            assert not base.endswith(ext), f"{base} would match the {ext} index glob"


# ---------------------------------------------------------------------------
# Mode preservation (SR-3A2-6)
# ---------------------------------------------------------------------------
def test_preserves_an_existing_files_mode(tmp_path: Path) -> None:
    """Without this, mkstemp's 0600 silently makes every CAO-written spec owner-only."""
    target = tmp_path / "wf.py"
    target.write_bytes(b"old\n")
    os.chmod(target, 0o640)

    _write(tmp_path, "wf.py", b"new\n")

    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_a_new_file_does_not_inherit_mkstemps_0600(tmp_path: Path) -> None:
    """A fresh spec gets the explicit create mode, not mkstemp's owner-only default.

    This test found a real defect: the first implementation only chmod'd when an
    existing mode had to be restored, so a NEW spec kept mkstemp's 0600 through
    ``os.replace`` — the exact outcome SR-3A2-6 rejects. It fails against that
    implementation, which is what makes it worth having.
    """
    _write(tmp_path, "fresh.py")
    mode = stat.S_IMODE((tmp_path / "fresh.py").stat().st_mode)
    assert mode == svc._SPEC_FILE_CREATE_MODE
    assert mode != 0o600, "a new spec must not be forced owner-only"


# ---------------------------------------------------------------------------
# Cleanup on failure (SR-3A2-4) — proven by injection, not inspection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("failing", ["fsync", "chmod", "replace"])
def test_no_temp_file_survives_a_failure_at_any_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    """SR-3A2-4: a chmod or disk-full failure must not orphan the temp file.

    ``chmod`` only runs when a target already exists (mode preservation), so that case
    seeds one first.
    """
    if failing == "chmod":
        (tmp_path / "wf.py").write_bytes(b"old\n")

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError(f"injected {failing} failure")

    monkeypatch.setattr(os, failing, _boom)

    with pytest.raises(OSError, match=f"injected {failing} failure"):
        _write(tmp_path, "wf.py", b"new\n")

    leftovers = [e for e in _dir_entries(tmp_path) if e != "wf.py"]
    assert leftovers == [], f"temp file orphaned after {failing} failure: {leftovers}"
