"""Unit tests for :mod:`cli_agent_orchestrator.tui.provider_preflight` (U4).

Focus: the FR-5.2 / BR-2 sole-source guarantee. The pre-flight rows come only
from :meth:`ServerClient.providers` (mocked here — no live server), so the
internal ``mock_cli`` and the phantom ``q_cli``/``gemini_cli`` names can never
appear. Also asserts the NFR-6 "yes"/"no" TEXT status, the BR-3 no-authenticated
invariant, and a source-scan proving the module never reaches for
``constants.PROVIDERS`` or the web ``FALLBACK_PROVIDERS`` list.
"""

from __future__ import annotations

from pathlib import Path
from typing import List
from unittest import mock

from cli_agent_orchestrator.tui import provider_preflight as pp
from cli_agent_orchestrator.tui.provider_preflight import PreflightRow, ProviderPreflight
from cli_agent_orchestrator.tui.server_client import ProviderStatus

# The real 9-provider set the f570de1 endpoint returns — no mock_cli/q_cli/gemini_cli.
REAL_NINE = [
    ProviderStatus("kiro_cli", "kiro-cli", True),
    ProviderStatus("claude_code", "claude", True),
    ProviderStatus("codex", "codex", False),
    ProviderStatus("hermes", "hermes", False),
    ProviderStatus("kimi_cli", "kimi", False),
    ProviderStatus("copilot_cli", "copilot", False),
    ProviderStatus("opencode_cli", "opencode", True),
    ProviderStatus("cursor_cli", "agent", False),
    ProviderStatus("antigravity_cli", "agy", False),
]

EXCLUDED_NAMES = ("mock_cli", "q_cli", "gemini_cli")


def _preflight_with(providers: List[ProviderStatus]) -> ProviderPreflight:
    """Build a ProviderPreflight over a client whose providers() is stubbed."""

    client = mock.MagicMock()
    client.providers.return_value = providers
    return ProviderPreflight(client=client)


def test_rows_contain_exactly_the_endpoint_providers() -> None:
    """FR-5.2: rows mirror the endpoint set, in order, one-to-one."""

    rows = _preflight_with(REAL_NINE).rows()

    assert [r.name for r in rows] == [p.name for p in REAL_NINE]
    assert [r.binary for r in rows] == [p.binary for p in REAL_NINE]
    assert all(isinstance(r, PreflightRow) for r in rows)


def test_excluded_provider_names_never_appear() -> None:
    """FR-5.2 EXCLUSION: mock_cli / q_cli / gemini_cli are absent from rows()."""

    rows = _preflight_with(REAL_NINE).rows()
    names = {r.name for r in rows}
    for banned in EXCLUDED_NAMES:
        assert banned not in names, f"{banned} leaked into pre-flight rows"


def test_installed_flag_maps_to_yes_no_text() -> None:
    """NFR-6 / BR-8: install status is the TEXT 'yes'/'no', never colour."""

    rows = _preflight_with(REAL_NINE).rows()
    by_name = {r.name: r for r in rows}

    assert by_name["kiro_cli"].installed_text == "yes"
    assert by_name["codex"].installed_text == "no"
    # Every row's status is one of the two text literals — nothing else.
    assert {r.installed_text for r in rows} <= {"yes", "no"}


def test_empty_providers_yields_empty_rows() -> None:
    """Edge case: an empty endpoint response yields no rows (no crash)."""

    assert _preflight_with([]).rows() == []


def test_rows_source_only_from_providers_endpoint() -> None:
    """BR-2: rows() calls ServerClient.providers() and nothing else."""

    client = mock.MagicMock()
    client.providers.return_value = REAL_NINE
    ProviderPreflight(client=client).rows()

    client.providers.assert_called_once_with()
    # No other read method is consulted for provider data.
    client.sessions.assert_not_called()
    client.profiles.assert_not_called()


def test_no_authenticated_field_on_row_or_provider_status() -> None:
    """BR-3: neither PreflightRow nor ProviderStatus exposes 'authenticated'."""

    row = _preflight_with(REAL_NINE).rows()[0]
    assert not hasattr(row, "authenticated")
    assert not hasattr(ProviderStatus("x", "y", True), "authenticated")
    assert "authenticated" not in PreflightRow.__dataclass_fields__


def _executable_code(module_file: str) -> str:
    """Return the module's source with comments and string/docstrings removed.

    Docstrings document *why* the excluded lists are avoided, so those names
    legitimately appear in prose. Tokenizing and dropping COMMENT and STRING
    tokens leaves only executable code (identifiers, attribute accesses), which
    is what the sole-source rule must honour.
    """

    import io
    import token
    import tokenize

    source = Path(module_file).read_text(encoding="utf-8")
    pieces: List[str] = []
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    for tok in tokens:
        if tok.type in (token.COMMENT, token.STRING):
            continue
        pieces.append(tok.string)
    return " ".join(pieces)


def test_module_source_does_not_reference_forbidden_provider_lists() -> None:
    """FR-5.2 source-scan: no constants.PROVIDERS / FALLBACK_PROVIDERS reference.

    The sole-source rule is only as strong as the code honouring it — assert the
    module's *executable code* never reaches for a static provider list nor the
    excluded names (docstring prose about the rule is excluded by tokenizing).
    """

    body = _executable_code(pp.__file__)

    assert "PROVIDERS" not in body  # no constants.PROVIDERS / static list import
    assert "FALLBACK_PROVIDERS" not in body
    for banned in EXCLUDED_NAMES:
        assert banned not in body, f"{banned} referenced in module code"
