"""Tests for the per-node tracked-terminal cap (CAO_MAX_TERMINALS).

Covers ``settings_service.get_max_terminals`` precedence/validation and the
enforcement in ``terminal_service.create_terminal`` (one-agent-per-pod k8s
topology: worker pods set CAO_MAX_TERMINALS=1).
"""

import asyncio
import os
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalLimitError
from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.settings_service import get_max_terminals

_TS = "cli_agent_orchestrator.services.terminal_service"
_SS = "cli_agent_orchestrator.services.settings_service"


class TestGetMaxTerminals:
    def test_unset_means_unlimited(self):
        with patch.dict(os.environ, {}, clear=True), patch(f"{_SS}._load", return_value={}):
            assert get_max_terminals() is None

    def test_env_var_wins(self):
        with patch.dict(os.environ, {"CAO_MAX_TERMINALS": "1"}, clear=False):
            assert get_max_terminals() == 1

    def test_env_var_beats_settings_file(self):
        with (
            patch.dict(os.environ, {"CAO_MAX_TERMINALS": "2"}, clear=False),
            patch(f"{_SS}._load", return_value={"server": {"max_terminals": 7}}),
        ):
            assert get_max_terminals() == 2

    def test_settings_file_used_when_env_unset(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(f"{_SS}._load", return_value={"server": {"max_terminals": 3}}),
        ):
            assert get_max_terminals() == 3

    def test_invalid_env_value_means_unlimited(self):
        with patch.dict(os.environ, {"CAO_MAX_TERMINALS": "banana"}, clear=False):
            assert get_max_terminals() is None

    def test_non_positive_means_unlimited_not_bricked(self):
        """0 or negative must not brick all terminal creation on a typo."""
        for value in ("0", "-1"):
            with patch.dict(os.environ, {"CAO_MAX_TERMINALS": value}, clear=False):
                assert get_max_terminals() is None

    def test_blank_env_falls_through_to_settings(self):
        with (
            patch.dict(os.environ, {"CAO_MAX_TERMINALS": "  "}, clear=False),
            patch(f"{_SS}._load", return_value={"server": {"max_terminals": 4}}),
        ):
            assert get_max_terminals() == 4


class _StopBeforeAllocation(Exception):
    """Sentinel raised by the first post-cap step so tests can prove the cap
    check PASSED without standing up tmux/provider machinery."""


class TestCreateTerminalCap:
    def test_rejects_when_at_cap(self):
        with (
            patch(f"{_TS}.get_max_terminals", return_value=1),
            patch(f"{_TS}.list_all_terminals", return_value=[{"id": "aaaa0001"}]) as mock_list,
        ):
            with pytest.raises(TerminalLimitError, match="Terminal limit reached"):
                asyncio.run(
                    terminal_service.create_terminal(provider="mock_cli", agent_profile="developer")
                )
        mock_list.assert_called_once()

    def test_rejects_when_over_cap(self):
        with (
            patch(f"{_TS}.get_max_terminals", return_value=1),
            patch(f"{_TS}.list_all_terminals", return_value=[{"id": "a"}, {"id": "b"}]),
        ):
            with pytest.raises(TerminalLimitError):
                asyncio.run(
                    terminal_service.create_terminal(provider="mock_cli", agent_profile="developer")
                )

    def test_under_cap_proceeds_past_check(self):
        with (
            patch(f"{_TS}.get_max_terminals", return_value=2),
            patch(f"{_TS}.list_all_terminals", return_value=[{"id": "aaaa0001"}]),
            patch(f"{_TS}.load_agent_profile", side_effect=_StopBeforeAllocation()),
        ):
            with pytest.raises(_StopBeforeAllocation):
                asyncio.run(
                    terminal_service.create_terminal(provider="mock_cli", agent_profile="developer")
                )

    def test_unlimited_skips_terminal_listing(self):
        with (
            patch(f"{_TS}.get_max_terminals", return_value=None),
            patch(f"{_TS}.list_all_terminals") as mock_list,
            patch(f"{_TS}.load_agent_profile", side_effect=_StopBeforeAllocation()),
        ):
            with pytest.raises(_StopBeforeAllocation):
                asyncio.run(
                    terminal_service.create_terminal(provider="mock_cli", agent_profile="developer")
                )
        mock_list.assert_not_called()

    def test_error_message_names_the_knob_and_counts(self):
        with (
            patch(f"{_TS}.get_max_terminals", return_value=1),
            patch(f"{_TS}.list_all_terminals", return_value=[{"id": "aaaa0001"}]),
        ):
            with pytest.raises(TerminalLimitError) as excinfo:
                asyncio.run(
                    terminal_service.create_terminal(provider="mock_cli", agent_profile="developer")
                )
        message = str(excinfo.value)
        assert "CAO_MAX_TERMINALS" in message
        assert "1 tracked" in message
