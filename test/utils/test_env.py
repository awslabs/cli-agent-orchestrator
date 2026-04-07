"""Tests for CAO environment variable utilities."""

import os

from cli_agent_orchestrator.utils import env as env_utils


def test_load_env_vars_returns_values_from_existing_file(tmp_path, monkeypatch):
    """load_env_vars should parse key-value pairs from the managed env file."""
    env_file = tmp_path / ".env"
    env_file.write_text("API_KEY=secret\nBASE_URL=http://localhost:27124\n")
    monkeypatch.setattr(env_utils, "CAO_ENV_FILE", env_file)

    assert env_utils.load_env_vars() == {
        "API_KEY": "secret",
        "BASE_URL": "http://localhost:27124",
    }


def test_load_env_vars_returns_empty_dict_when_file_missing(tmp_path, monkeypatch):
    """Missing env files should be treated as empty."""
    monkeypatch.setattr(env_utils, "CAO_ENV_FILE", tmp_path / ".env")

    assert env_utils.load_env_vars() == {}


def test_load_env_vars_returns_empty_dict_for_empty_file(tmp_path, monkeypatch):
    """Empty env files should not raise and should return no values."""
    env_file = tmp_path / ".env"
    env_file.write_text("")
    monkeypatch.setattr(env_utils, "CAO_ENV_FILE", env_file)

    assert env_utils.load_env_vars() == {}


def test_resolve_env_vars_substitutes_known_values(tmp_path, monkeypatch):
    """Known placeholders should resolve from the managed env file."""
    env_file = tmp_path / ".env"
    env_file.write_text("API_KEY=secret\n")
    monkeypatch.setattr(env_utils, "CAO_ENV_FILE", env_file)

    assert env_utils.resolve_env_vars("token=${API_KEY}") == "token=secret"


def test_resolve_env_vars_leaves_unknown_values_intact(tmp_path, monkeypatch):
    """Unknown placeholders should remain visible for debugging."""
    monkeypatch.setattr(env_utils, "CAO_ENV_FILE", tmp_path / ".env")

    assert env_utils.resolve_env_vars("token=${MISSING_KEY}") == "token=${MISSING_KEY}"


def test_resolve_env_vars_is_passthrough_without_placeholders(tmp_path, monkeypatch):
    """Text without placeholders should be returned unchanged."""
    env_file = tmp_path / ".env"
    env_file.write_text("API_KEY=secret\n")
    monkeypatch.setattr(env_utils, "CAO_ENV_FILE", env_file)

    text = "plain text without substitutions"
    assert env_utils.resolve_env_vars(text) == text


def test_resolve_env_vars_supports_partial_resolution(tmp_path, monkeypatch):
    """Known placeholders should resolve while unknown placeholders remain intact."""
    env_file = tmp_path / ".env"
    env_file.write_text("KNOWN=value\n")
    monkeypatch.setattr(env_utils, "CAO_ENV_FILE", env_file)

    resolved = env_utils.resolve_env_vars("known=${KNOWN} unknown=${UNKNOWN}")
    assert resolved == "known=value unknown=${UNKNOWN}"


def test_resolve_env_vars_does_not_read_process_environment(tmp_path, monkeypatch):
    """Resolution should only use the managed env file, not os.environ."""
    monkeypatch.setattr(env_utils, "CAO_ENV_FILE", tmp_path / ".env")
    monkeypatch.setenv("HOME", "/should/not/be/used")

    assert env_utils.resolve_env_vars("home=${HOME}") == "home=${HOME}"
    assert os.environ["HOME"] == "/should/not/be/used"


def test_set_env_var_creates_file_when_missing(tmp_path, monkeypatch):
    """set_env_var should create the env file and write the key."""
    env_file = tmp_path / "nested" / ".env"
    monkeypatch.setattr(env_utils, "CAO_ENV_FILE", env_file)

    env_utils.set_env_var("API_KEY", "secret")

    assert env_file.exists()
    assert env_utils.load_env_vars() == {"API_KEY": "secret"}


def test_set_env_var_creates_file_with_owner_only_permissions(tmp_path, monkeypatch):
    """Newly created env files must be owner-readable/writable only (0600)."""
    env_file = tmp_path / ".env"
    monkeypatch.setattr(env_utils, "CAO_ENV_FILE", env_file)

    env_utils.set_env_var("SECRET", "value")

    assert env_file.stat().st_mode & 0o777 == 0o600


def test_set_env_var_updates_existing_key(tmp_path, monkeypatch):
    """set_env_var should overwrite existing keys."""
    env_file = tmp_path / ".env"
    env_file.write_text("API_KEY=old\n")
    monkeypatch.setattr(env_utils, "CAO_ENV_FILE", env_file)

    env_utils.set_env_var("API_KEY", "new")

    assert env_utils.load_env_vars() == {"API_KEY": "new"}


def test_unset_env_var_removes_key(tmp_path, monkeypatch):
    """unset_env_var should remove existing keys without affecting others."""
    env_file = tmp_path / ".env"
    env_file.write_text("API_KEY=secret\nBASE_URL=http://localhost\n")
    monkeypatch.setattr(env_utils, "CAO_ENV_FILE", env_file)

    env_utils.unset_env_var("API_KEY")

    assert env_utils.load_env_vars() == {"BASE_URL": "http://localhost"}


def test_unset_env_var_noop_when_key_not_in_file(tmp_path, monkeypatch):
    """Unsetting a nonexistent key from an existing file should not raise."""
    env_file = tmp_path / ".env"
    env_file.write_text("OTHER_KEY=value\n")
    monkeypatch.setattr(env_utils, "CAO_ENV_FILE", env_file)

    env_utils.unset_env_var("MISSING_KEY")

    assert env_utils.load_env_vars() == {"OTHER_KEY": "value"}


def test_list_env_vars_returns_current_contents(tmp_path, monkeypatch):
    """list_env_vars is a semantic alias for loading current env values."""
    env_file = tmp_path / ".env"
    env_file.write_text("API_KEY=secret\n")
    monkeypatch.setattr(env_utils, "CAO_ENV_FILE", env_file)

    assert env_utils.list_env_vars() == {"API_KEY": "secret"}
