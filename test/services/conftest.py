"""Deterministic Kiro probe fixture for terminal-service unit lifecycles."""

import ast
from typing import List, Optional

import pytest

from cli_agent_orchestrator.providers.kiro_capabilities import KiroCapabilities


@pytest.fixture(autouse=True)
def mock_kiro_capability_probe(monkeypatch):
    """Keep service tests independent from a locally installed Kiro wrapper."""

    def probe(_engine, _requested):
        return KiroCapabilities(
            version="2.13.0",
            flags=frozenset(
                {
                    "--agent-engine",
                    "--v3",
                    "--agent",
                    "--model",
                    "--legacy-ui",
                    "--trust-all-tools",
                    "--require-mcp-startup",
                }
            ),
        )

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.probe_kiro_capabilities",
        probe,
    )


@pytest.fixture(autouse=True)
def kiro_agent_store_is_hermetic(hermetic_kiro_agent_store):
    """Every service test sees an empty Kiro agent store, home and launch cwd (#1074).

    Service tests create ``kiro_cli`` terminals throughout, and the agent check
    would otherwise read the developer's real ``~/.kiro/agents``. See
    ``hermetic_kiro_agent_store`` in ``test/conftest.py``.
    """


@pytest.fixture
def kiro_agent_store_not_under_test(monkeypatch):
    """Skip the Kiro agent-store check for tests that use ``kiro_cli`` as a stand-in.

    ``create_terminal`` refuses a Kiro launch whose agent no agent directory
    defines (#1074). Modules that exercise unrelated launch behaviour (rollback,
    plugin events, worktrees) with made-up profile names opt in here instead of
    installing an agent file per name; ``test_kiro_agent_identity_launch`` and
    ``test_delegation_ceiling`` cover the check itself.
    """
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.require_kiro_agent_resolves",
        lambda *args, **kwargs: None,
    )
    # These modules also mock the registry row (``db_create_terminal``), so a
    # healthy Kiro launch has no row to record its passed identity check on.
    # The record is still written to a real row when there is one, and the
    # provider double must still report ``identity_verified = True``: only the
    # missing row is forgiven (#1074).
    from cli_agent_orchestrator.services import terminal_service

    real_mark = terminal_service.mark_terminal_identity_verified
    monkeypatch.setattr(
        terminal_service,
        "mark_terminal_identity_verified",
        lambda terminal_id: real_mark(terminal_id) or True,
    )


@pytest.fixture
def scoped_ast_parse(monkeypatch):
    """Fake ``ast.parse`` for ONE source string; every other call parses for real.

    ``<module>.ast`` IS the ``ast`` module, so ``monkeypatch.setattr(mod.ast, "parse",
    fake)`` replaces the parser for the whole process -- including pytest itself, which
    calls ``ast.parse`` on the test file to render a failing assertion. A fake that
    raises unconditionally therefore turns an ordinary red test into ``INTERNALERROR``
    and ends the session with every later test unrun (#970 review, P3-3) -- the exact
    truncated-run shape the parser-depth tests exist to prevent.

    This is the ONLY sanctioned way to fake the parser in ``test/``
    (``test_no_test_fakes_ast_parse_globally`` enforces it). Usage::

        calls = scoped_ast_parse(source)                      # record only
        calls = scoped_ast_parse(source, RecursionError(...)) # raise for this source

    Returns the list of sources the fake intercepted, so a test can assert that the
    parser was NOT reached (``assert calls == []``) after the subject returns, rather
    than raising from inside the subject.
    """
    real_parse = ast.parse

    def install(source: str, raising: Optional[BaseException] = None) -> List[str]:
        calls: List[str] = []

        def fake(src, *args, **kwargs):
            if src == source:
                calls.append(src)
                if raising is not None:
                    raise raising
            return real_parse(src, *args, **kwargs)

        monkeypatch.setattr(ast, "parse", fake)
        return calls

    return install
