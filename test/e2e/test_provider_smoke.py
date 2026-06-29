"""Opt-in e2e smoke test: each preferred provider reaches IDLE on init.

This is the automated twin of ``cao doctor --live``. For each provider in
``PREFERRED_PROVIDERS`` it spawns a throwaway session and asserts the agent
reaches IDLE within ``provider_init_timeout``, then tears it down.

Requires a running cao-server, tmux, and the provider CLI installed + authed.
Excluded from default runs by ``addopts = -m 'not e2e'``; run with:

    uv run cao-server
    uv run pytest -m e2e test/e2e/test_provider_smoke.py -v
"""

import shutil
from test.e2e.conftest import cleanup_terminal, create_terminal, wait_for_status

import pytest

from cli_agent_orchestrator.constants import PREFERRED_PROVIDERS
from cli_agent_orchestrator.utils.providers import provider_binary

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("provider", PREFERRED_PROVIDERS)
def test_provider_reaches_idle(provider):
    """A throwaway session for ``provider`` should reach IDLE on init."""
    binary = provider_binary(provider)
    if not binary or shutil.which(binary) is None:
        pytest.skip(f"{provider} CLI ({binary}) not installed")

    session_name = f"smoke-{provider}"
    terminal_id, actual_session = create_terminal(
        provider=provider,
        agent_profile="code_supervisor",
        session_name=session_name,
    )
    try:
        reached = wait_for_status(terminal_id, "idle", timeout=90)
        assert reached, f"{provider} did not reach IDLE within 90s"
    finally:
        cleanup_terminal(terminal_id, actual_session)
