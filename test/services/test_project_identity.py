"""SC-7 / U6 — stable project identity resolver tests.

Covers the three identity sources defined in ``aidlc-docs/phase2.5/tasks.md``
§U6.1 and the acceptance criteria in ``success-criteria.md``:

- **AC1** — Git repo at two different paths (main + worktree) resolves to the
  same ``project_id``.
- **AC2** — Directory rename does not orphan memories (cwd-hash is recorded as
  an alias under the canonical git-remote id).
- **AC3** — Non-git directory continues to work via the SHA256[:12] fallback
  (pre-U6 behavior preserved).
- **AC4** — The one-time migration from ``<hash>/`` → ``<canonical>/`` is
  documented *in code* via ``plan_project_dir_migration`` with a dry-run
  action classification (``none``/``rename``/``merge``/``conflict``) — the
  risk note at ``tasks.md:287`` requires dry-run before mutation.

Plus two covering tests for the ``_normalize_git_remote`` helper (shape of the
canonical id) and the explicit override source (env + settings).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine

from cli_agent_orchestrator.clients.database import (
    Base,
    get_project_id_by_alias,
    list_aliases_for_project,
)
from cli_agent_orchestrator.services.memory_service import (
    MemoryService,
    ProjectIdentityResolutionError,
    _git_remote_identity,
    _normalize_git_remote,
    _validate_project_id_override,
    resolve_project_id,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _make_engine(db_path: Path) -> Any:
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    return engine


def _make_svc(base_dir: Path, db_path: Path) -> MemoryService:
    """Build a MemoryService bound to an isolated SQLite DB.

    Also redirects the module-level ``SessionLocal`` used by the alias CRUD
    helpers to the test DB so alias bookkeeping is observable.
    """
    engine = _make_engine(db_path)
    svc = MemoryService(base_dir=base_dir, db_engine=engine)
    return svc


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the global SessionLocal at a test DB so alias writes land there."""
    db_path = tmp_path / "test.db"
    from cli_agent_orchestrator.clients import database as db_mod

    engine = _make_engine(db_path)
    from sqlalchemy.orm import sessionmaker

    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession, raising=True)
    return db_path


@pytest.fixture
def clear_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure explicit-override source is empty for tests that target 2/3."""
    monkeypatch.delenv("CAO_PROJECT_ID", raising=False)

    def _no_override() -> None:
        return None

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_project_id_override",
        _no_override,
        raising=True,
    )


def _init_repo(path: Path, remote_url: str) -> None:
    """Initialize a git repo at ``path`` with a single ``origin`` remote."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "remote", "add", "origin", remote_url],
        cwd=path,
        check=True,
        capture_output=True,
    )


def _git_available() -> bool:
    try:
        subprocess.run(
            ["git", "--version"], capture_output=True, check=True, timeout=2
        )
        return True
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(
    not _git_available(), reason="git executable required for U6 tests"
)


# ---------------------------------------------------------------------------
# AC1 — worktree / two paths → same canonical id
# ---------------------------------------------------------------------------


def test_ac1_same_git_remote_at_two_paths_resolves_same_project_id(
    tmp_path: Path, isolated_db: Path, clear_overrides: None
) -> None:
    """Two checkouts of the same remote must yield the same project_id.

    We simulate a main clone and a worktree by initialising two repos with the
    same ``origin`` URL. Both paths must resolve to the same canonical id and
    both cwd-hashes must be recorded as aliases under that id.
    """
    remote = "git@github.com:acme/widgets.git"
    main = tmp_path / "main"
    worktree = tmp_path / "wt"
    _init_repo(main, remote)
    _init_repo(worktree, remote)

    svc = _make_svc(tmp_path / "mem", isolated_db)

    id_main = svc.resolve_scope_id("project", {"cwd": str(main)})
    id_wt = svc.resolve_scope_id("project", {"cwd": str(worktree)})

    assert id_main is not None
    assert id_main == id_wt

    main_hash = hashlib.sha256(os.path.realpath(main).encode()).hexdigest()[:12]
    wt_hash = hashlib.sha256(
        os.path.realpath(worktree).encode()
    ).hexdigest()[:12]
    assert get_project_id_by_alias(main_hash) == id_main
    assert get_project_id_by_alias(wt_hash) == id_main


# ---------------------------------------------------------------------------
# AC2 — directory rename keeps memories recallable via alias
# ---------------------------------------------------------------------------


def test_ac2_rename_keeps_memories_recallable_via_alias(
    tmp_path: Path, isolated_db: Path, clear_overrides: None
) -> None:
    """Storing under the canonical id must survive a directory rename.

    Even if the filesystem path changes (which would change the cwd-hash), the
    git remote resolves identically, so the memory must still surface via
    ``recall``.
    """
    remote = "https://example.com/acme/renameable.git"
    before = tmp_path / "before"
    _init_repo(before, remote)

    svc = _make_svc(tmp_path / "mem", isolated_db)
    _run(
        svc.store(
            content="remembered across a rename",
            scope="project",
            memory_type="project",
            terminal_context={"cwd": str(before)},
            key="rename-key",
        )
    )

    # Rename the directory — git metadata travels with it, so the remote URL
    # is unchanged.
    after = tmp_path / "after"
    before.rename(after)

    hits = _run(
        svc.recall(
            scope="project",
            terminal_context={"cwd": str(after)},
            query="rename-key",
        )
    )
    assert any(h.key == "rename-key" for h in hits), hits


# ---------------------------------------------------------------------------
# AC3 — non-git falls back to cwd-hash (pre-U6 behavior preserved)
# ---------------------------------------------------------------------------


def test_ac3_non_git_falls_back_to_cwd_hash(
    tmp_path: Path, isolated_db: Path, clear_overrides: None
) -> None:
    """A plain directory must resolve exactly as Phase 2 did — SHA256[:12]."""
    plain = tmp_path / "plain"
    plain.mkdir()

    svc = _make_svc(tmp_path / "mem", isolated_db)
    scope_id = svc.resolve_scope_id("project", {"cwd": str(plain)})

    expected = hashlib.sha256(
        os.path.realpath(plain).encode()
    ).hexdigest()[:12]
    assert scope_id == expected


# ---------------------------------------------------------------------------
# AC4 — dry-run migration planner
# ---------------------------------------------------------------------------


def test_ac4_plan_project_dir_migration_dry_run_actions(
    tmp_path: Path, isolated_db: Path, clear_overrides: None
) -> None:
    """``plan_project_dir_migration`` must never mutate and must classify
    action as ``none``/``rename``/``merge``/``conflict``.
    """
    base = tmp_path / "mem"
    svc = _make_svc(base, isolated_db)
    canonical = "github-com-acme-widgets"
    alias = "deadbeefcafe"

    # Case 1: alias dir absent → action=none.
    plan = svc.plan_project_dir_migration(canonical, alias)
    assert plan["action"] == "none"
    assert plan["dry_run"] is True

    # Case 2: alias dir has content, canonical absent → action=rename.
    (base / alias / "wiki" / "project").mkdir(parents=True)
    (base / alias / "wiki" / "project" / "note.md").write_text("x")
    plan = svc.plan_project_dir_migration(canonical, alias)
    assert plan["action"] == "rename"
    assert "wiki/project/note.md" in plan["files"]
    # Dry-run must NOT have touched the filesystem.
    assert (base / alias / "wiki" / "project" / "note.md").exists()
    assert not (base / canonical).exists()

    # Case 3: both exist with content → action=merge.
    (base / canonical / "wiki" / "project").mkdir(parents=True)
    (base / canonical / "wiki" / "project" / "other.md").write_text("y")
    plan = svc.plan_project_dir_migration(canonical, alias)
    assert plan["action"] == "merge"

    # Case 4: canonical exists empty → action=conflict (dir exists but nothing
    # to migrate is ambiguous and requires review).
    empty_alias = "0" * 12
    (base / empty_alias).mkdir()
    plan = svc.plan_project_dir_migration(canonical, empty_alias)
    assert plan["action"] == "conflict"


# ---------------------------------------------------------------------------
# Source 1 — explicit override (env + settings)
# ---------------------------------------------------------------------------


def test_explicit_project_id_from_env_wins_over_git(
    tmp_path: Path, isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``CAO_PROJECT_ID`` env must take precedence over git remote."""
    _init_repo(tmp_path / "repo", "git@github.com:someone/else.git")
    monkeypatch.setenv("CAO_PROJECT_ID", "my-canonical-id")

    svc = _make_svc(tmp_path / "mem", isolated_db)
    scope_id = svc.resolve_scope_id("project", {"cwd": str(tmp_path / "repo")})

    assert scope_id == "my-canonical-id"
    # cwd-hash must still be recorded as an alias so pre-U6 memories for this
    # directory stay discoverable.
    aliases = list_aliases_for_project("my-canonical-id")
    kinds = {a["kind"] for a in aliases}
    assert "cwd_hash" in kinds


def test_explicit_project_id_from_settings_when_env_absent(
    tmp_path: Path,
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """settings.json ``project_id`` is used when the env var is unset."""
    monkeypatch.delenv("CAO_PROJECT_ID", raising=False)

    fake_settings = tmp_path / "settings.json"
    # Nested under ``memory.project_id`` (U6 team-lead decision #6) — matches
    # the ``memory.enabled`` precedent so memory-subsystem config stays grouped.
    fake_settings.write_text('{"memory": {"project_id": "from-settings"}}')
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.SETTINGS_FILE",
        fake_settings,
        raising=True,
    )

    svc = _make_svc(tmp_path / "mem", isolated_db)
    scope_id = svc.resolve_scope_id(
        "project", {"cwd": str(tmp_path / "plain")}
    )

    assert scope_id == "from-settings"


# ---------------------------------------------------------------------------
# Helper — _normalize_git_remote shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("git@github.com:acme/widgets.git", "github-com-acme-widgets"),
        ("https://github.com/acme/widgets.git", "github-com-acme-widgets"),
        ("https://github.com/acme/widgets/", "github-com-acme-widgets"),
        (
            "https://user:token@git.example.com/a/b.git",
            "git-example-com-a-b",
        ),
        ("ssh://git@gitlab.com/org/repo", "gitlab-com-org-repo"),
        ("", "unknown"),
    ],
)
def test_normalize_git_remote_produces_safe_stable_id(
    url: str, expected: str
) -> None:
    assert _normalize_git_remote(url) == expected


# ---------------------------------------------------------------------------
# Defensive — git unavailable / non-repo cwd
# ---------------------------------------------------------------------------


def test_git_remote_identity_returns_none_for_non_repo(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _git_remote_identity(plain) is None


def test_resolver_survives_filenotfound_on_git(
    tmp_path: Path, isolated_db: Path, clear_overrides: None
) -> None:
    """If ``git`` is absent from PATH the resolver must fall back gracefully."""
    (tmp_path / "plain").mkdir()
    svc = _make_svc(tmp_path / "mem", isolated_db)
    with patch(
        "cli_agent_orchestrator.services.memory_service.subprocess.run",
        side_effect=FileNotFoundError("git not installed"),
    ):
        scope_id = svc.resolve_scope_id(
            "project", {"cwd": str(tmp_path / "plain")}
        )
    expected = hashlib.sha256(
        os.path.realpath(tmp_path / "plain").encode()
    ).hexdigest()[:12]
    assert scope_id == expected


# ---------------------------------------------------------------------------
# Regression lock — alias writes never mask a lookup failure
# ---------------------------------------------------------------------------


def test_record_alias_swallows_db_error_without_breaking_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alias bookkeeping is opportunistic — DB errors must not surface."""

    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("db exploded")

    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.record_project_alias",
        _boom,
        raising=True,
    )
    monkeypatch.setenv("CAO_PROJECT_ID", "stable-id")

    # No isolated_db used — only the alias write path matters here.
    svc = MemoryService(base_dir=tmp_path / "mem")
    scope_id = svc.resolve_scope_id("project", {"cwd": str(tmp_path)})
    assert scope_id == "stable-id"


# ---------------------------------------------------------------------------
# Team-lead decision #5 — reject-style override validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_value",
    [
        "has/slash",
        "has space",
        "has\x00null",
        "!@#$",
        "a" * 129,
        "",
    ],
)
def test_validate_project_id_override_rejects_bad_input(bad_value: str) -> None:
    """Explicit overrides are rejected rather than silently sanitized so
    misconfigured config fails loudly (team-lead decision #5).
    """
    with pytest.raises(ValueError):
        _validate_project_id_override(bad_value)


@pytest.mark.parametrize(
    "good_value",
    [
        "my-project",
        "acme.widgets",
        "CamelCase_123",
        "a",
        "a" * 128,
    ],
)
def test_validate_project_id_override_accepts_whitelist(good_value: str) -> None:
    assert _validate_project_id_override(good_value) == good_value


# ---------------------------------------------------------------------------
# Team-lead decision #10 — ProjectIdentityResolutionError when all sources fail
# ---------------------------------------------------------------------------


def test_resolve_project_id_raises_when_all_sources_fail(
    clear_overrides: None,
) -> None:
    """No cwd + no override → explicit error, not silent None."""
    with pytest.raises(ProjectIdentityResolutionError):
        resolve_project_id(None)


# ---------------------------------------------------------------------------
# Team-lead decision #4 — legacy cwd-hash dirs remain searchable via alias
# ---------------------------------------------------------------------------


def test_legacy_cwd_hash_dir_remains_searchable_after_alias_recorded(
    tmp_path: Path, isolated_db: Path, clear_overrides: None
) -> None:
    """Pre-U6 memories stored under ``<cwd_hash>/`` must stay readable once a
    canonical id takes over. ``_get_search_dirs`` walks both the canonical dir
    and any ``cwd_hash``-kind alias dirs (decision #4).
    """
    base = tmp_path / "mem"
    base.mkdir()
    repo = tmp_path / "repo"
    _init_repo(repo, "https://example.com/acme/legacy.git")

    svc = _make_svc(base, isolated_db)

    # Resolve once to populate the alias table with repo's cwd-hash.
    canonical = svc.resolve_scope_id("project", {"cwd": str(repo)})
    assert canonical is not None

    legacy_hash = hashlib.sha256(
        os.path.realpath(repo).encode()
    ).hexdigest()[:12]
    assert legacy_hash != canonical

    # Create a legacy wiki file under the cwd-hash dir (as if pre-U6).
    legacy_wiki_dir = base / legacy_hash / "wiki" / "project"
    legacy_wiki_dir.mkdir(parents=True)
    # Also create empty canonical dir so its existence check passes.
    (base / canonical).mkdir()

    dirs = svc._get_search_dirs("project", {"cwd": str(repo)})
    dir_names = {d.name for d in dirs}
    assert legacy_hash in dir_names
    assert canonical in dir_names


# ---------------------------------------------------------------------------
# Team-lead decision #9 — U2 INFO-1 containment guard absorption
# ---------------------------------------------------------------------------


def test_tampered_index_relative_path_rejected(
    tmp_path: Path, isolated_db: Path, clear_overrides: None
) -> None:
    """If index.md contains a traversal-laden ``relative_path`` the file
    fallback must refuse to read outside base_dir rather than following it.
    """
    base = tmp_path / "mem"
    svc = _make_svc(base, isolated_db)

    scope_id = "plainproject"
    wiki_dir = base / scope_id / "wiki"
    wiki_dir.mkdir(parents=True)
    # Plant a "secret" file outside base_dir.
    secret = tmp_path / "secret.md"
    secret.write_text(
        "<!-- id: 00000000-0000-0000-0000-000000000000 | tags: | "
        "scope: project | type: project -->\n"
        "## 2026-01-01T00:00:00Z\nstolen data\n"
    )
    # Build a tampered index.md that points at the secret via ../.
    index_md = wiki_dir / "index.md"
    index_md.write_text(
        "# Memory Index\n\n"
        "## project\n"
        "- [secret](../../../secret.md) — type:project tags: ~1tok "
        "updated:2026-01-01T00:00:00Z\n"
    )

    hits = svc._recall_file_fallback(
        query=None,
        scope="project",
        memory_type=None,
        limit=10,
        terminal_context={"cwd": str(tmp_path / "anywhere")},
        scan_all=True,
    )
    # The traversal entry must have been dropped, not followed.
    assert not any(h.key == "secret" for h in hits)
