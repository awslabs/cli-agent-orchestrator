"""Fixture overrides for the examples-gallery e2e subpackage (U6, FR-7.1).

These tests drive real subprocesses via ``run_script_workflow`` and mock the
agent-terminal boundary at ``run_step``'s HTTP call, never round-tripping to
a real ``cao-server`` or tmux (the shim's own transport is exercised for
real; the server it talks to is not). Same self-contained rationale as
``test/e2e/script_runner/conftest.py`` — overriding the parent
``test/e2e/conftest.py``'s autouse server/tmux-requiring fixtures with no-ops
scopes that requirement OUT of this subpackage only.
"""

import pytest


@pytest.fixture(scope="session", autouse=True)
def require_cao_server():
    """No-op override: these tests mock run_step's HTTP call, no real server."""
    return


@pytest.fixture(scope="session", autouse=True)
def warmup_mcp_server_cache():
    """No-op override: these tests never launch a provider CLI via cao-mcp-server."""
    return


@pytest.fixture(scope="session", autouse=True)
def require_tmux():
    """No-op override: these tests never create a tmux terminal."""
    return
