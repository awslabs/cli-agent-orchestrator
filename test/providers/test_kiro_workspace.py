"""The static workspace check that gates ``--trust-workspace`` on Kiro v3 (#1167, gate G9)."""

from __future__ import annotations

import os

import pytest

from cli_agent_orchestrator.providers.kiro_workspace import KiroWorkspaceError, check_kiro_workspace


@pytest.fixture
def tree(tmp_path):
    home = tmp_path / "home"
    repo = home / "src" / "repo"
    repo.mkdir(parents=True)
    return home, repo


def _check(repo, home):
    check_kiro_workspace(str(repo), home=str(home))


@pytest.mark.parametrize(
    "entry", ["agents", "hooks", "settings", "workflows", "extensions", "powers", "data", "mystery"]
)
def test_configuration_and_unknown_entries_refuse(tree, entry):
    home, repo = tree
    (repo / ".kiro" / entry).mkdir(parents=True)
    with pytest.raises(KiroWorkspaceError) as excinfo:
        _check(repo, home)
    assert excinfo.value.code == f"kiro_v3_workspace_config:{entry}"
    assert "engine 'v2'" in str(excinfo.value)


def test_an_unknown_file_refuses(tree):
    home, repo = tree
    (repo / ".kiro").mkdir()
    (repo / ".kiro" / "mcp.json").write_text("{}")
    with pytest.raises(KiroWorkspaceError) as excinfo:
        _check(repo, home)
    assert excinfo.value.code == "kiro_v3_workspace_config:mcp.json"


def test_content_entries_pass(tree):
    home, repo = tree
    (repo / ".kiro" / "steering").mkdir(parents=True)
    (repo / ".kiro" / "steering" / "cao-memory.md").write_text("memory")
    (repo / ".kiro" / "specs" / "feature").mkdir(parents=True)
    (repo / ".kiro" / "skills" / "s").mkdir(parents=True)
    (repo / ".kiro" / "AGENTS.md").write_text("notes")
    (repo / "AGENTS.md").write_text("root notes")
    _check(repo, home)


def test_no_kiro_directory_passes(tree):
    home, repo = tree
    _check(repo, home)


def test_the_users_own_kiro_is_skipped_even_with_agents(tree):
    home, repo = tree
    (home / ".kiro" / "agents").mkdir(parents=True)
    (home / ".kiro" / "settings").mkdir(parents=True)
    _check(repo, home)


def test_a_symlinked_user_kiro_is_still_skipped(tree, tmp_path):
    home, repo = tree
    real = tmp_path / "dotfiles-kiro"
    (real / "agents").mkdir(parents=True)
    os.symlink(real, home / ".kiro")
    _check(repo, home)


def test_an_ancestor_kiro_agents_refuses(tree):
    home, repo = tree
    (home / "src" / ".kiro" / "agents").mkdir(parents=True)
    with pytest.raises(KiroWorkspaceError) as excinfo:
        _check(repo, home)
    assert excinfo.value.code == "kiro_v3_workspace_config:agents"
    assert excinfo.value.path == str(home / "src" / ".kiro" / "agents")


@pytest.mark.parametrize(
    ("where", "entry"),
    [
        ("kiro", ".kiro (symlink)"),
        ("entry", "steering (symlink)"),
        ("nested", "steering (symlink)"),
    ],
)
def test_symlinks_refuse(tree, tmp_path, where, entry):
    home, repo = tree
    target = tmp_path / "elsewhere"
    target.mkdir()
    if where == "kiro":
        os.symlink(target, repo / ".kiro")
    elif where == "entry":
        (repo / ".kiro").mkdir()
        os.symlink(target, repo / ".kiro" / "steering")
    else:
        (repo / ".kiro" / "steering").mkdir(parents=True)
        os.symlink(target / "x.md", repo / ".kiro" / "steering" / "x.md")
    # The code, not the message: the message carries the path, and pytest names
    # tmp_path after the test, so a match on "symlink" would always pass.
    with pytest.raises(KiroWorkspaceError) as excinfo:
        _check(repo, home)
    assert excinfo.value.code == f"kiro_v3_workspace_config:{entry}"


def test_a_kiro_file_instead_of_a_directory_refuses(tree):
    home, repo = tree
    (repo / ".kiro").write_text("not a dir")
    with pytest.raises(KiroWorkspaceError) as excinfo:
        _check(repo, home)
    assert excinfo.value.code == "kiro_v3_workspace_config:.kiro (not a directory)"


def test_a_content_name_with_the_wrong_kind_refuses(tree):
    home, repo = tree
    (repo / ".kiro").mkdir()
    (repo / ".kiro" / "steering").write_text("a file, not a directory")
    with pytest.raises(KiroWorkspaceError) as excinfo:
        _check(repo, home)
    assert excinfo.value.code == "kiro_v3_workspace_config:steering"


def test_the_rescan_catches_an_entry_added_after_the_first_scan(tree):
    """Run again after the header, the same check sees what was planted since."""
    home, repo = tree
    (repo / ".kiro" / "steering").mkdir(parents=True)
    _check(repo, home)
    (repo / ".kiro" / "hooks").mkdir()
    with pytest.raises(KiroWorkspaceError) as excinfo:
        _check(repo, home)
    assert excinfo.value.code == "kiro_v3_workspace_config:hooks"


def test_an_unreadable_kiro_directory_refuses(tree):
    home, repo = tree
    kiro = repo / ".kiro"
    kiro.mkdir()
    kiro.chmod(0)
    try:
        with pytest.raises(KiroWorkspaceError) as excinfo:
            _check(repo, home)
    finally:
        kiro.chmod(0o755)
    assert excinfo.value.code == "kiro_v3_workspace_config:unreadable"
