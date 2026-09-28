"""The runtime token is normalized to an owner-only file at load (#802).

After ``load_runtime_token`` the value is never in ``CAO_RUNTIME_TOKEN`` again:
either it came from a file (left in place) or it was moved out of the env into a
0600 file that ``CAO_RUNTIME_TOKEN_FILE`` then points at. Everything downstream
reads the path, never the value.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch, tmp_path):
    # constants.CAO_HOME_DIR is computed at import; patch the resolved value so
    # the token file lands under the test's tmp dir, not the developer's home.
    monkeypatch.setattr(
        "cli_agent_orchestrator.constants.CAO_HOME_DIR", str(tmp_path), raising=False
    )
    import cli_agent_orchestrator.utils.runtime_token as rt

    rt._reset_cache_for_tests()
    monkeypatch.delenv("CAO_RUNTIME_TOKEN", raising=False)
    monkeypatch.delenv("CAO_RUNTIME_TOKEN_FILE", raising=False)
    yield
    rt._reset_cache_for_tests()


def test_loading_from_the_env_moves_the_value_into_an_owner_only_file(monkeypatch, tmp_path):
    import pathlib

    from cli_agent_orchestrator.utils import runtime_token as rt

    monkeypatch.setenv("CAO_RUNTIME_TOKEN", "s3cret-value")

    assert rt.load_runtime_token() == "s3cret-value"
    # The value is gone from the environment; only the path remains.
    assert "CAO_RUNTIME_TOKEN" not in os.environ
    file_path = os.environ["CAO_RUNTIME_TOKEN_FILE"]
    p = pathlib.Path(file_path)
    assert p.read_text() == "s3cret-value"
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    assert rt.runtime_token_file() == file_path
    assert rt.runtime_token() == "s3cret-value"


def test_loading_from_a_file_reads_it_and_writes_nothing(monkeypatch, tmp_path):
    from cli_agent_orchestrator.utils import runtime_token as rt

    existing = tmp_path / "preexisting-token"
    existing.write_text("from-file-value")
    monkeypatch.setenv("CAO_RUNTIME_TOKEN_FILE", str(existing))

    assert rt.load_runtime_token() == "from-file-value"
    # The file is left exactly where it was; the runtime dir was not populated.
    assert os.environ["CAO_RUNTIME_TOKEN_FILE"] == str(existing)
    assert not (tmp_path / "tmp" / "runtime-token").exists()
    assert "CAO_RUNTIME_TOKEN" not in os.environ


def test_an_unreadable_file_yields_none_so_callers_fail_closed(monkeypatch):
    from cli_agent_orchestrator.utils import runtime_token as rt

    monkeypatch.setenv("CAO_RUNTIME_TOKEN_FILE", "/nonexistent/dir/runtime-token")
    assert rt.load_runtime_token() is None
    assert rt.runtime_token() is None


def test_no_variable_set_is_a_quiet_none(monkeypatch):
    from cli_agent_orchestrator.utils import runtime_token as rt

    assert rt.load_runtime_token() is None
    assert rt.runtime_token_file() is None


def test_load_is_idempotent_and_cached(monkeypatch):
    from cli_agent_orchestrator.utils import runtime_token as rt

    monkeypatch.setenv("CAO_RUNTIME_TOKEN", "once")
    first = rt.load_runtime_token()
    # A later env change is ignored until the process restarts (cache clear).
    monkeypatch.setenv("CAO_RUNTIME_TOKEN", "changed")
    assert rt.load_runtime_token() == first == "once"
