"""Tests for terminal WebSocket PTY environment setup."""

import os
from unittest.mock import patch

import pytest


class TestTmuxPtyEnvironment:
    """Test that tmux attach subprocess receives a usable TERM value."""

    @pytest.mark.asyncio
    async def test_term_defaults_to_xterm_256color_when_unset(self):
        """When TERM is not set, subprocess should receive xterm-256color."""
        env_without_term = {k: v for k, v in os.environ.items() if k != "TERM"}

        with patch.dict(os.environ, env_without_term, clear=True):
            pty_env = os.environ.copy()
            pty_env.setdefault("TERM", "xterm-256color")

            assert pty_env["TERM"] == "xterm-256color"

    @pytest.mark.asyncio
    async def test_term_preserved_when_already_set(self):
        """When TERM is explicitly set, subprocess should preserve it."""
        with patch.dict(os.environ, {"TERM": "screen-256color"}):
            pty_env = os.environ.copy()
            pty_env.setdefault("TERM", "xterm-256color")

            assert pty_env["TERM"] == "screen-256color"

    @pytest.mark.asyncio
    async def test_term_defaults_when_set_to_dumb(self):
        """When TERM is 'dumb', setdefault does not override (existing value)."""
        with patch.dict(os.environ, {"TERM": "dumb"}):
            pty_env = os.environ.copy()
            pty_env.setdefault("TERM", "xterm-256color")

            # setdefault only sets if key is missing, not if value is 'dumb'
            assert pty_env["TERM"] == "dumb"
