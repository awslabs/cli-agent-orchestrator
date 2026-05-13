"""Phase 3 U8 — Project-identity marker file tests.

Covers the 10 acceptance criteria in ``aidlc-docs/phase3/tasks/tasks.md`` §U8
plus the challenger-requested teeth tests for B1/B2/C1:

- AC-U8.1 — rename survival (non-git project)
- AC-U8.2 — cross-dir mv survival (simulated)
- AC-U8.3 — copy mints a fresh project_id
- AC-U8.4 — marker disabled → pre-U8 cwd_hash fallback, no ``.cao/`` created
- AC-U8.5 — git remote bypasses marker entirely
- AC-U8.6 — corrupt marker → WARNING + fall through to cwd_hash
- AC-U8.7 — malicious project_id → rejected + fall through
- AC-U8.8 — atomic write never leaves a half-written file
- AC-U8.9 — POSIX permissions 0o700 / 0o600
- AC-U8.10 — one-time stderr hint on first marker write

Plus challenger-extra:
- corrupt marker does NOT overwrite (B1 teeth)
- rename path requires nonce match (B2 teeth)
- precedence: override + git-remote still beat marker
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as db_mod
from cli_agent_orchestrator.clients.database import (
    Base,
    lookup_project_marker,
    record_project_marker,
)
from cli_agent_orchestrator.constants import (
    PROJECT_MARKER_DIRNAME,
    PROJECT_MARKER_FILENAME,
)
from cli_agent_orchestrator.services import memory_service as ms

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the module-level SessionLocal to a per-test SQLite DB."""
    db_path = tmp_path / "marker_test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession, raising=True)
    return db_path


@pytest.fixture
def no_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear explicit-override source so the non-git path is taken."""
    monkeypatch.delenv("CAO_PROJECT_ID", raising=False)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_project_id_override",
        lambda: None,
        raising=True,
    )


@pytest.fixture
def marker_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``memory.project_marker`` on regardless of user settings.json."""
    monkeypatch.delenv("CAO_PROJECT_MARKER", raising=False)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.is_project_marker_enabled",
        lambda: True,
        raising=True,
    )


@pytest.fixture
def clear_hint_cache() -> None:
    """Reset the in-process hint cache so ``_emit_gitignore_hint_once`` fires."""
    ms._GITIGNORE_HINT_EMITTED.clear()


def _marker_file(cwd: Path) -> Path:
    return cwd / PROJECT_MARKER_DIRNAME / PROJECT_MARKER_FILENAME


def _cwd_hash(cwd: Path) -> str:
    return hashlib.sha256(os.path.realpath(str(cwd)).encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# AC-U8.1 — rename survival
# ---------------------------------------------------------------------------


def test_marker_survives_rename(
    tmp_path: Path,
    isolated_db: Path,
    no_override: None,
    marker_enabled: None,
    clear_hint_cache: None,
) -> None:
    foo = tmp_path / "foo"
    foo.mkdir()
    first_id = ms.resolve_project_id(foo)
    assert _marker_file(foo).exists()

    bar = tmp_path / "bar"
    foo.rename(bar)
    second_id = ms.resolve_project_id(bar)
    assert second_id == first_id


# ---------------------------------------------------------------------------
# AC-U8.2 — cross-dir mv survival (simulated by moving between tmp subdirs)
# ---------------------------------------------------------------------------


def test_marker_survives_mv_across_dirs(
    tmp_path: Path,
    isolated_db: Path,
    no_override: None,
    marker_enabled: None,
    clear_hint_cache: None,
) -> None:
    src_parent = tmp_path / "srcroot"
    dst_parent = tmp_path / "dstroot"
    src_parent.mkdir()
    dst_parent.mkdir()
    original = src_parent / "proj"
    original.mkdir()

    first_id = ms.resolve_project_id(original)
    moved = dst_parent / "proj"
    original.rename(moved)

    second_id = ms.resolve_project_id(moved)
    assert second_id == first_id


# ---------------------------------------------------------------------------
# AC-U8.3 — copy detection mints a new id
# ---------------------------------------------------------------------------


def test_copy_mints_new_project_id(
    tmp_path: Path,
    isolated_db: Path,
    no_override: None,
    marker_enabled: None,
    clear_hint_cache: None,
) -> None:
    import shutil

    foo = tmp_path / "foo"
    foo.mkdir()
    original_id = ms.resolve_project_id(foo)

    baz = tmp_path / "baz"
    shutil.copytree(foo, baz)
    copy_id = ms.resolve_project_id(baz)

    assert copy_id != original_id
    # Original is untouched.
    payload_foo = json.loads(_marker_file(foo).read_text())
    assert payload_foo["project_id"] == original_id
    # New marker lives in the copy.
    payload_baz = json.loads(_marker_file(baz).read_text())
    assert payload_baz["project_id"] == copy_id


# ---------------------------------------------------------------------------
# AC-U8.4 — marker disabled → cwd_hash, no ``.cao/``
# ---------------------------------------------------------------------------


def test_marker_disabled_falls_through_to_cwd_hash(
    tmp_path: Path,
    isolated_db: Path,
    no_override: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.is_project_marker_enabled",
        lambda: False,
        raising=True,
    )
    foo = tmp_path / "foo"
    foo.mkdir()
    pid = ms.resolve_project_id(foo)
    assert pid == _cwd_hash(foo)
    assert not (foo / PROJECT_MARKER_DIRNAME).exists()


# ---------------------------------------------------------------------------
# AC-U8.5 — git remote bypasses marker entirely
# ---------------------------------------------------------------------------


def test_git_remote_bypasses_marker_entirely(
    tmp_path: Path,
    isolated_db: Path,
    no_override: None,
    marker_enabled: None,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    try:
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://example.com/user/repo.git"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        pytest.skip("git unavailable in test env")

    pid = ms.resolve_project_id(repo)
    assert "example-com-user-repo" in pid
    assert not (repo / PROJECT_MARKER_DIRNAME).exists()


# ---------------------------------------------------------------------------
# AC-U8.6 — corrupt marker → fall through, no overwrite (challenger B1 teeth)
# ---------------------------------------------------------------------------


def test_corrupt_marker_falls_through_to_cwd_hash(
    tmp_path: Path,
    isolated_db: Path,
    no_override: None,
    marker_enabled: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    foo = tmp_path / "foo"
    foo.mkdir()
    marker = _marker_file(foo)
    marker.parent.mkdir(parents=True)
    marker.write_text("{not valid json")
    original_bytes = marker.read_bytes()

    with caplog.at_level(logging.WARNING, logger=ms.logger.name):
        pid = ms.resolve_project_id(foo)

    # B1 teeth: result MUST equal cwd_hash, not a minted id.
    assert pid == _cwd_hash(foo)
    # The corrupt file is NOT overwritten — preserves admin-recoverable state.
    assert marker.read_bytes() == original_bytes
    assert any("corrupt" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# AC-U8.7 — malicious project_id → rejected + fall through
# ---------------------------------------------------------------------------


def test_malicious_project_id_falls_through_to_cwd_hash(
    tmp_path: Path,
    isolated_db: Path,
    no_override: None,
    marker_enabled: None,
) -> None:
    foo = tmp_path / "foo"
    foo.mkdir()
    marker = _marker_file(foo)
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps(
            {
                "project_id": "../evil",
                "nonce": "0123456789abcdef",
                "created_at": "2026-05-12T00:00:00Z",
            }
        )
    )

    pid = ms.resolve_project_id(foo)
    assert pid == _cwd_hash(foo)
    # Not overwritten.
    parsed = json.loads(marker.read_text())
    assert parsed["project_id"] == "../evil"


# ---------------------------------------------------------------------------
# AC-U8.8 — atomic write leaves no partial file on IO failure
# ---------------------------------------------------------------------------


def test_atomic_write_no_partial_file(
    tmp_path: Path,
    isolated_db: Path,
    no_override: None,
    marker_enabled: None,
    clear_hint_cache: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    foo = tmp_path / "foo"
    foo.mkdir()

    real_replace = os.replace

    def boom(src: str, dst: str) -> None:  # type: ignore[no-untyped-def]
        # Simulate a crash between temp write and atomic rename. Leave the
        # tempfile behind; the marker file must NOT appear.
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)
    pid = ms.resolve_project_id(foo)

    # Marker file does not exist (the atomic rename failed).
    assert not _marker_file(foo).exists()
    # Resolver still returns a stable id — falls through to cwd_hash.
    monkeypatch.setattr(os, "replace", real_replace)
    assert pid == _cwd_hash(foo)


# ---------------------------------------------------------------------------
# AC-U8.9 — POSIX permissions
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only mode bits")
def test_marker_permissions(
    tmp_path: Path,
    isolated_db: Path,
    no_override: None,
    marker_enabled: None,
    clear_hint_cache: None,
) -> None:
    foo = tmp_path / "foo"
    foo.mkdir()
    ms.resolve_project_id(foo)
    marker_dir = foo / PROJECT_MARKER_DIRNAME
    marker_file = marker_dir / PROJECT_MARKER_FILENAME
    assert (marker_dir.stat().st_mode & 0o777) == 0o700
    assert (marker_file.stat().st_mode & 0o777) == 0o600


# ---------------------------------------------------------------------------
# AC-U8.10 — hint emitted once per cwd per process
# ---------------------------------------------------------------------------


def test_gitignore_hint_emitted_once(
    tmp_path: Path,
    isolated_db: Path,
    no_override: None,
    marker_enabled: None,
    clear_hint_cache: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    foo = tmp_path / "foo"
    foo.mkdir()
    ms.resolve_project_id(foo)
    first = capsys.readouterr().err
    assert ".cao/" in first

    ms.resolve_project_id(foo)
    second = capsys.readouterr().err
    assert ".cao/" not in second


# ---------------------------------------------------------------------------
# Challenger B2 teeth — rename requires nonce match
# ---------------------------------------------------------------------------


def test_rename_requires_nonce_match(
    tmp_path: Path,
    isolated_db: Path,
    no_override: None,
    marker_enabled: None,
    clear_hint_cache: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    # Pre-seed SQLite as though the marker's project_id was recorded against a
    # now-missing path with a *different* nonce. A hostile repo plants a
    # marker claiming that project_id + attacker-chosen nonce.
    victim_id = "abcdef123456"
    good_nonce = "0123456789abcdef"
    gone_path = tmp_path / "ghost-home"  # never created on disk
    record_project_marker(victim_id, good_nonce, str(gone_path))

    hostile = tmp_path / "hostile"
    hostile.mkdir()
    marker = _marker_file(hostile)
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps(
            {
                "project_id": victim_id,
                "nonce": "deadbeefcafef00d",  # forged
                "created_at": "2026-05-12T00:00:00Z",
            }
        )
    )

    with caplog.at_level(logging.WARNING, logger=ms.logger.name):
        pid = ms.resolve_project_id(hostile)

    # Hijack prevented — resolver fell through to cwd_hash.
    assert pid == _cwd_hash(hostile)
    # SQLite row untouched.
    row = lookup_project_marker(victim_id)
    assert row is not None
    assert row["nonce"] == good_nonce
    assert row["realpath"] == str(gone_path)
    assert any("nonce mismatch" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Precedence — override + git-remote still beat marker
# ---------------------------------------------------------------------------


def test_marker_precedence_below_override_and_git(
    tmp_path: Path,
    isolated_db: Path,
    marker_enabled: None,
    clear_hint_cache: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    foo = tmp_path / "foo"
    foo.mkdir()

    # Plant a valid-looking marker so we can prove override still wins.
    marker = _marker_file(foo)
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps(
            {
                "project_id": "marker-would-win",
                "nonce": "0123456789abcdef",
                "created_at": "2026-05-12T00:00:00Z",
            }
        )
    )

    # Override path.
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_project_id_override",
        lambda: "explicit-wins",
        raising=True,
    )
    assert ms.resolve_project_id(foo) == "explicit-wins"

    # Git-remote path.
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_project_id_override",
        lambda: None,
        raising=True,
    )
    with patch.object(ms, "_git_remote_identity", return_value="https://example.com/o/r.git"):
        pid = ms.resolve_project_id(foo)
    assert "example-com-o-r" in pid
    assert pid != "marker-would-win"
