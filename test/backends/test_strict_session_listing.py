"""Adapter-level strict listing validation without using the operator's runtime."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cli_agent_orchestrator.backends.base import TerminalBackendError
from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend
from cli_agent_orchestrator.clients.tmux import TmuxClient, TmuxLookupError


def tmux_client(result):
    client = object.__new__(TmuxClient)
    client.server = Mock()
    client.server.cmd.return_value = result
    return client


def test_tmux_complete_listing_and_attached_count():
    client = tmux_client(
        SimpleNamespace(returncode=0, stdout=["cao-a\t2", "personal\t0"], stderr=[])
    )
    assert client.list_sessions_strict() == [
        {"id": "cao-a", "name": "cao-a", "status": "active"},
        {"id": "personal", "name": "personal", "status": "detached"},
    ]


@pytest.mark.parametrize("stdout", [["cao-a"], ["cao-a\t?"], ["cao-a\t0", "cao-a\t1"], ["\t0"]])
def test_tmux_rejects_malformed_or_duplicate_rows(stdout):
    client = tmux_client(SimpleNamespace(returncode=0, stdout=stdout, stderr=[]))
    with pytest.raises(TmuxLookupError):
        client.list_sessions_strict()


def test_tmux_confirmed_absence_vs_unknown_failure():
    client = tmux_client(
        SimpleNamespace(returncode=1, stdout=[], stderr=["no server running on /private/socket"])
    )
    assert client.list_sessions_strict() == []
    client.server.cmd.return_value.stderr = ["permission denied"]
    with pytest.raises(TmuxLookupError):
        client.list_sessions_strict()


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"workspaces": None},
        {"workspaces": [{}]},
        {"workspaces": [{"label": "a"}, {"label": "a"}]},
        {"workspaces": [{"label": 123}]},
    ],
)
def test_herdr_rejects_incomplete_listing(data):
    backend = object.__new__(HerdrBackend)
    backend._run_herdr = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps(data)))
    with pytest.raises(TerminalBackendError):
        backend.list_sessions_strict()


def test_herdr_empty_success_and_command_failure():
    backend = object.__new__(HerdrBackend)
    backend._run_herdr = Mock(
        return_value=SimpleNamespace(returncode=0, stdout='{"workspaces": []}')
    )
    assert backend.list_sessions_strict() == []
    backend._run_herdr.return_value.returncode = 1
    with pytest.raises(TerminalBackendError):
        backend.list_sessions_strict()
