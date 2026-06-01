"""SC-7 / U6 — stable project identity resolver tests.

Covers:
- **AC1** — Same git remote at two paths resolves to the same project_id.
- **AC2** — Directory rename does not orphan memories.
- **AC3** — Non-git directory falls back to SHA256[:12].
- **AC4** — ``plan_project_dir_migration`` is dry-run only.
- Source 1 — explicit override (env + settings.json memory.project_id).
- Helper coverage for ``_normalize_git_remote`` and override validation.
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
from sqlalchemy.orm import sessionmaker

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


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _make_engine(db_path: Path) -> Any:
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return engine


def _make_svc(base_dir: Path, db_path: Path) -> MemoryService:
    engine = _make_engine(db_path)
    return MemoryService(base_dir=base_dir, db_engine=engine)


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the global SessionLocal at a test DB so alias writes land there."""
    db_path = tmp_path / "test.db"
    from cli_agent_orchestrator.clients import database as db_mod

    engine = _make_engine(db_path)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession, raising=True)
    return db_path


@pytest.fixture
def clear_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure explicit-override source is empty for tests targeting git/hash."""
    monkeypatch.delenv("CAO_PROJECT_ID", raising=False)

    def _no_override() -> dict:
        return {"enabled": True, "flush_threshold": 0.85}

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_memory_settings",
        _no_override,
        raising=True,
    )


def _init_repo(path: Path, remote_url: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", remote_url],
        cwd=path,
        check=True,
        capture_output=True,
    )


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True, timeout=2)
        return True
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(not _git_available(), reason="git executable required for U6 tests")


# ---------------------------------------------------------------------------
# AC1 — same remote at two paths → same project_id
# ---------------------------------------------------------------------------


def test_ac1_same_git_remote_at_two_paths_resolves_same_project_id(
    tmp_path: Path, isolated_db: Path, clear_overrides: None
) -> None:
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
    wt_hash = hashlib.sha256(os.path.realpath(worktree).encode()).hexdigest()[:12]
    assert get_project_id_by_alias(main_hash) == id_main
    assert get_project_id_by_alias(wt_hash) == id_main


def test_credentialed_remote_url_is_not_persisted_as_alias(
    tmp_path: Path, isolated_db: Path, clear_overrides: None
) -> None:
    """A remote URL with embedded credentials must never reach the alias table.

    Git remotes commonly look like ``https://user:token@host/org/repo.git``.
    Resolving project identity records aliases, but the raw URL (and therefore
    the secret) must not be persisted — only the auth-stripped canonical id
    and the cwd-hash. See Copilot PR #262 HIGH finding.
    """
    secret = "s3cr3t-token"
    remote = f"https://alice:{secret}@github.com/acme/widgets.git"
    repo = tmp_path / "repo"
    _init_repo(repo, remote)

    svc = _make_svc(tmp_path / "mem", isolated_db)
    canonical = svc.resolve_scope_id("project", {"cwd": str(repo)})

    # Canonical id is auth-stripped.
    assert canonical == "github-com-acme-widgets"
    assert secret not in canonical

    # No alias row anywhere holds the raw URL, the secret, or kind=git_remote.
    aliases = list_aliases_for_project(canonical)
    assert all(a["kind"] != "git_remote" for a in aliases), "git_remote alias must not be recorded"
    for a in aliases:
        assert secret not in a["alias"], "credential leaked into alias value"
        assert remote not in a["alias"], "raw remote URL leaked into alias value"

    # Only the cwd-hash alias is recorded, and it still resolves correctly.
    cwd_hash = hashlib.sha256(os.path.realpath(repo).encode()).hexdigest()[:12]
    assert get_project_id_by_alias(cwd_hash) == canonical


# ---------------------------------------------------------------------------
# AC2 — rename keeps memories recallable
# ---------------------------------------------------------------------------


def test_ac2_rename_keeps_memories_recallable_via_alias(
    tmp_path: Path, isolated_db: Path, clear_overrides: None
) -> None:
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
# AC3 — non-git falls back to cwd-hash
# ---------------------------------------------------------------------------


def test_ac3_non_git_falls_back_to_cwd_hash(
    tmp_path: Path, isolated_db: Path, clear_overrides: None
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    svc = _make_svc(tmp_path / "mem", isolated_db)
    scope_id = svc.resolve_scope_id("project", {"cwd": str(plain)})

    expected = hashlib.sha256(os.path.realpath(plain).encode()).hexdigest()[:12]
    assert scope_id == expected


# ---------------------------------------------------------------------------
# AC4 — dry-run migration planner
# ---------------------------------------------------------------------------


def test_ac4_plan_project_dir_migration_dry_run_actions(
    tmp_path: Path, isolated_db: Path, clear_overrides: None
) -> None:
    base = tmp_path / "mem"
    svc = _make_svc(base, isolated_db)
    canonical = "github-com-acme-widgets"
    alias = "deadbeefcafe"

    # Case 1: alias dir absent → none.
    plan = svc.plan_project_dir_migration(canonical, alias)
    assert plan["action"] == "none"
    assert plan["dry_run"] is True

    # Case 2: alias dir has content, canonical absent → rename.
    (base / alias / "wiki" / "project").mkdir(parents=True)
    (base / alias / "wiki" / "project" / "note.md").write_text("x")
    plan = svc.plan_project_dir_migration(canonical, alias)
    assert plan["action"] == "rename"
    assert "wiki/project/note.md" in plan["files"]
    assert (base / alias / "wiki" / "project" / "note.md").exists()
    assert not (base / canonical).exists()

    # Case 3: both exist with content → merge.
    (base / canonical / "wiki" / "project").mkdir(parents=True)
    (base / canonical / "wiki" / "project" / "other.md").write_text("y")
    plan = svc.plan_project_dir_migration(canonical, alias)
    assert plan["action"] == "merge"

    # Case 4: canonical exists empty → conflict.
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
    _init_repo(tmp_path / "repo", "git@github.com:someone/else.git")
    monkeypatch.setenv("CAO_PROJECT_ID", "my-canonical-id")

    svc = _make_svc(tmp_path / "mem", isolated_db)
    scope_id = svc.resolve_scope_id("project", {"cwd": str(tmp_path / "repo")})

    assert scope_id == "my-canonical-id"
    aliases = list_aliases_for_project("my-canonical-id")
    kinds = {a["kind"] for a in aliases}
    assert "cwd_hash" in kinds


def test_explicit_project_id_from_settings_when_env_absent(
    tmp_path: Path,
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """settings.json ``memory.project_id`` is used when the env var is unset."""
    monkeypatch.delenv("CAO_PROJECT_ID", raising=False)

    def _settings() -> dict:
        return {"enabled": True, "flush_threshold": 0.85, "project_id": "from-settings"}

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_memory_settings",
        _settings,
        raising=True,
    )

    svc = _make_svc(tmp_path / "mem", isolated_db)
    scope_id = svc.resolve_scope_id("project", {"cwd": str(tmp_path / "plain")})

    assert scope_id == "from-settings"


# ---------------------------------------------------------------------------
# Alias uniqueness — an alias maps to exactly one canonical project_id
# ---------------------------------------------------------------------------


def test_alias_upserts_to_single_project_id(isolated_db: Path) -> None:
    """Re-recording an alias for a new project_id repoints it, never duplicates.

    A cwd-hash first resolved via an explicit override and later via its git
    remote must end up mapping to exactly one canonical id, so reverse lookups
    are deterministic.
    """
    from cli_agent_orchestrator.clients.database import record_project_alias

    alias = "deadbeefcafe"

    # First resolution maps the cwd-hash to an override-supplied id.
    record_project_alias("override-id", alias, "cwd_hash")
    assert get_project_id_by_alias(alias) == "override-id"

    # Later, the same cwd resolves via its git remote: the alias repoints.
    record_project_alias("github-com-acme-widgets", alias, "cwd_hash")
    assert get_project_id_by_alias(alias) == "github-com-acme-widgets"

    # Exactly one mapping exists for the alias — no duplicate rows.
    old = [a for a in list_aliases_for_project("override-id") if a["alias"] == alias]
    new = [a for a in list_aliases_for_project("github-com-acme-widgets") if a["alias"] == alias]
    assert old == []
    assert len(new) == 1


# ---------------------------------------------------------------------------
# _normalize_git_remote shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("git@github.com:acme/widgets.git", "github-com-acme-widgets"),
        ("https://github.com/acme/widgets.git", "github-com-acme-widgets"),
        ("https://github.com/acme/widgets/", "github-com-acme-widgets"),
        ("https://user:token@git.example.com/a/b.git", "git-example-com-a-b"),
        ("ssh://git@gitlab.com/org/repo", "gitlab-com-org-repo"),
        ("", "unknown"),
    ],
)
def test_normalize_git_remote_produces_safe_stable_id(url: str, expected: str) -> None:
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
    (tmp_path / "plain").mkdir()
    svc = _make_svc(tmp_path / "mem", isolated_db)
    with patch(
        "cli_agent_orchestrator.services.memory_service.subprocess.run",
        side_effect=FileNotFoundError("git not installed"),
    ):
        scope_id = svc.resolve_scope_id("project", {"cwd": str(tmp_path / "plain")})
    expected = hashlib.sha256(os.path.realpath(tmp_path / "plain").encode()).hexdigest()[:12]
    assert scope_id == expected


# ---------------------------------------------------------------------------
# Alias bookkeeping is opportunistic
# ---------------------------------------------------------------------------


def test_record_alias_swallows_db_error_without_breaking_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("db exploded")

    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.record_project_alias",
        _boom,
        raising=True,
    )
    monkeypatch.setenv("CAO_PROJECT_ID", "stable-id")

    svc = MemoryService(base_dir=tmp_path / "mem")
    scope_id = svc.resolve_scope_id("project", {"cwd": str(tmp_path)})
    assert scope_id == "stable-id"


# ---------------------------------------------------------------------------
# Override validation — reject, don't sanitize
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_value",
    ["has/slash", "has space", "has\x00null", "!@#$", "a" * 129, ""],
)
def test_validate_project_id_override_rejects_bad_input(bad_value: str) -> None:
    with pytest.raises(ValueError):
        _validate_project_id_override(bad_value)


@pytest.mark.parametrize(
    "good_value",
    ["my-project", "acme.widgets", "CamelCase_123", "a", "a" * 128],
)
def test_validate_project_id_override_accepts_whitelist(good_value: str) -> None:
    assert _validate_project_id_override(good_value) == good_value


# ---------------------------------------------------------------------------
# resolve_project_id raises when all sources fail
# ---------------------------------------------------------------------------


def test_resolve_project_id_raises_when_all_sources_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CAO_PROJECT_ID", raising=False)

    def _no_override() -> dict:
        return {"enabled": True, "flush_threshold": 0.85}

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_memory_settings",
        _no_override,
        raising=True,
    )
    with pytest.raises(ProjectIdentityResolutionError):
        resolve_project_id(None)


# ---------------------------------------------------------------------------
# Legacy cwd-hash dir remains searchable via alias
# ---------------------------------------------------------------------------


def test_legacy_cwd_hash_dir_remains_searchable_after_alias_recorded(
    tmp_path: Path, isolated_db: Path, clear_overrides: None
) -> None:
    base = tmp_path / "mem"
    base.mkdir()
    repo = tmp_path / "repo"
    _init_repo(repo, "https://example.com/acme/legacy.git")

    svc = _make_svc(base, isolated_db)

    canonical = svc.resolve_scope_id("project", {"cwd": str(repo)})
    assert canonical is not None

    legacy_hash = hashlib.sha256(os.path.realpath(repo).encode()).hexdigest()[:12]
    assert legacy_hash != canonical

    legacy_wiki_dir = base / legacy_hash / "wiki" / "project"
    legacy_wiki_dir.mkdir(parents=True)
    (base / canonical).mkdir()

    dirs = svc._get_search_dirs("project", {"cwd": str(repo)})
    dir_names = {d.name for d in dirs}
    assert legacy_hash in dir_names
    assert canonical in dir_names
