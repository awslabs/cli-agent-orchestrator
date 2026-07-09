"""Fixture overrides for the script-runner e2e subpackage (issue #312, F8).

These tests are self-contained subprocess proofs — no server/tmux needed. They
exercise the REAL ``asyncio.create_subprocess_exec`` path directly (spawn/drain/
reap), never round-tripping over HTTP to a CAO server and never touching tmux.
The parent ``test/e2e/conftest.py`` defines session-scoped ``autouse`` fixtures
(``require_cao_server``, ``warmup_mcp_server_cache``, ``require_tmux``) that
skip the ENTIRE e2e session when no server/tmux is present — that gating is
correct for the provider-handoff e2e tests but wrongly skips these
self-contained ones too. Overriding the same fixture names here (pytest fixture
resolution: nearest conftest wins) with no-ops scopes the server/tmux
requirement OUT of this subpackage only, without touching the parent module.
"""

import pytest


@pytest.fixture(scope="session", autouse=True)
def require_cao_server():
    """No-op override: these tests never call the CAO server."""
    return


@pytest.fixture(scope="session", autouse=True)
def warmup_mcp_server_cache():
    """No-op override: these tests never launch a provider CLI via cao-mcp-server."""
    return


@pytest.fixture(scope="session", autouse=True)
def require_tmux():
    """No-op override: these tests never create a tmux terminal."""
    return
