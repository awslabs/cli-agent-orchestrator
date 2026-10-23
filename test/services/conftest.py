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
