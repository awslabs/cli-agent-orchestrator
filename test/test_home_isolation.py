"""The suite runs against a throwaway HOME, never the developer's real one.

See "HOME isolation" in ``test/conftest.py``. These tests are skipped when the
session deliberately keeps the real HOME (``CAO_TEST_REAL_HOME=1``, or an e2e
selection), because there is then nothing to assert.
"""

import os
from pathlib import Path
from test import conftest

import pytest

pytestmark = pytest.mark.skipif(
    "path" not in conftest._ISOLATED_HOME, reason="this session keeps the real HOME on purpose"
)


def test_home_is_the_session_fake_home():
    fake = Path(conftest._ISOLATED_HOME["path"])
    assert Path.home() == fake
    assert Path(os.path.expanduser("~")) == fake
    real = conftest._REAL_HOME_ENV.get("HOME")
    if real:
        assert os.path.realpath(real) != str(fake)


def test_import_time_cao_paths_derive_from_the_fake_home():
    from cli_agent_orchestrator import constants
    from cli_agent_orchestrator.services import settings_service

    fake = Path(conftest._ISOLATED_HOME["path"])
    assert constants.CAO_HOME_DIR == fake / ".aws" / "cli-agent-orchestrator"
    for path in (
        constants.AGENT_PLUGINS_DIR,
        constants.KIRO_AGENTS_DIR,
        constants.COPILOT_AGENTS_DIR,
        constants.OPENCODE_CONFIG_DIR,
        Path(settings_service._DEFAULTS["kiro_cli"]),
    ):
        assert path.is_relative_to(fake), path


def test_provider_root_variables_are_unset_or_outside_the_real_home():
    real = conftest._REAL_HOME_ENV.get("HOME")
    for name in conftest.HOME_ROOT_ENV_VARS:
        if name in conftest._PINNED_ROOT_ENV:
            assert os.environ[name] == conftest._PINNED_ROOT_ENV[name]
            if real:
                assert not conftest._is_within(os.environ[name], os.path.realpath(real)), name
        else:
            assert name not in os.environ, name


def test_is_within_rule(tmp_path):
    real = str(tmp_path / "real-home")
    os.makedirs(real)
    assert conftest._is_within(os.path.join(real, ".aws", "cli-agent-orchestrator"), real)
    assert conftest._is_within(real, real)
    assert not conftest._is_within(str(tmp_path / "real-home-sibling"), real)
    assert not conftest._is_within(str(tmp_path / "elsewhere"), real)


@pytest.mark.parametrize(
    "where, kept",
    [
        ("under-tmp", True),  # CAO_HOME_DIR=$(mktemp -d), or a nested pytest's own home
        ("inside-real-home", False),  # the developer's live config
        ("outside-home-not-tmp", False),  # e.g. a live CAO_HOME_DIR=/Volumes/...
        ("empty", False),
    ],
)
def test_keep_rule_keeps_only_throwaway_roots(tmp_path, where, kept):
    tmp_root = str(tmp_path / "tmp")
    real = str(tmp_path / "home")
    elsewhere = str(tmp_path / "volumes" / "work" / "cao-home")
    for d in (tmp_root, real, elsewhere):
        os.makedirs(d)
    value = {
        "under-tmp": os.path.join(tmp_root, "cao-home"),
        "inside-real-home": os.path.join(real, ".aws", "cli-agent-orchestrator"),
        "outside-home-not-tmp": elsewhere,
        "empty": "  ",
    }[where]
    assert (
        conftest._keeps_root(value, os.path.realpath(real), (os.path.realpath(tmp_root),)) is kept
    )


def test_a_temp_dir_inside_the_real_home_does_not_make_a_root_throwaway(tmp_path):
    real = os.path.realpath(str(tmp_path / "home"))
    tmp_root = os.path.join(real, "tmp")
    os.makedirs(tmp_root)
    assert not conftest._keeps_root(os.path.join(tmp_root, "cao"), real, (tmp_root,))


def test_no_test_reaches_a_real_llm_cli_by_default():
    from cli_agent_orchestrator.services import wiki_compiler, wiki_lint

    assert wiki_compiler._build_llm_client() is None
    assert wiki_compiler._build_llm_client("claude_code") is None
    assert wiki_lint._build_llm_client() is None


def test_antigravity_mcp_config_lives_under_the_fake_home():
    from cli_agent_orchestrator.providers.antigravity_cli import AntigravityCliProvider

    provider = AntigravityCliProvider("t1", "s", "w")
    assert provider._mcp_config_path().is_relative_to(Path(conftest._ISOLATED_HOME["path"]))


def test_a_leaked_home_is_repinned_for_the_next_test_part_one():
    # Deliberately not via monkeypatch: the autouse fixture must undo it.
    os.environ["HOME"] = "/nonexistent-leaked-home"
    os.environ["KIRO_HOME"] = "/nonexistent-leaked-kiro"


def test_a_leaked_home_is_repinned_for_the_next_test_part_two():
    assert Path.home() == Path(conftest._ISOLATED_HOME["path"])
    assert os.environ.get("KIRO_HOME") == conftest._PINNED_ROOT_ENV.get("KIRO_HOME")


@pytest.mark.real_home
def test_real_home_marker_restores_the_real_home():
    assert os.environ.get("HOME") == conftest._REAL_HOME_ENV.get("HOME")
