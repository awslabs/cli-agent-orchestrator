"""Unit tests for :mod:`cli_agent_orchestrator.tui.server_client` (U4).

The whole module ``requests`` symbol is mocked — no live cao-server is ever
contacted, so the suite is hermetic and portable. Response fixtures mirror the
real ``f570de1`` JSON shapes (captured from ``api/main.py``: ``GET /health``,
``/agents/providers``, ``/sessions``, ``/sessions/{n}/terminals``, ``/workflows``,
``/agents/profiles``, ``/agents/profiles/{name}``).

Covers: each GET method happy-path; BR-1 (read-only — only ``requests.get`` is
ever called, never post/put/delete/patch across every method); BR-5
(``RequestException`` → ``ServerUnavailable``); BR-6 (malformed shape →
``ServerClientError``); the ``profile(name)`` 404 → ``ProfileNotFound`` path;
plus edge cases (empty-list responses; a timeout → ``ServerUnavailable``).
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest import mock

import pytest
import requests

from cli_agent_orchestrator.tui import server_client as sc
from cli_agent_orchestrator.tui.server_client import (
    HealthInfo,
    ProfileDetail,
    ProfileNotFound,
    ProfileSummary,
    ProviderStatus,
    ServerClient,
    ServerClientError,
    ServerUnavailable,
    SessionInfo,
    TerminalInfo,
    WorkflowSummary,
)

# --------------------------------------------------------------------------- #
# f570de1 response fixtures (faithful to api/main.py).                          #
# --------------------------------------------------------------------------- #

HEALTH_PAYLOAD: Dict[str, Any] = {
    "status": "ok",
    "service": "cli-agent-orchestrator",
    "terminal_backend": "tmux",
    "components": {"cao": "ok", "herdr": "unavailable", "claude": "ok"},
}

# The real 9-provider response — endpoint order, no mock_cli/q_cli/gemini_cli.
PROVIDERS_PAYLOAD: List[Dict[str, Any]] = [
    {"name": "kiro_cli", "binary": "kiro-cli", "installed": True},
    {"name": "claude_code", "binary": "claude", "installed": True},
    {"name": "codex", "binary": "codex", "installed": False},
    {"name": "hermes", "binary": "hermes", "installed": False},
    {"name": "kimi_cli", "binary": "kimi", "installed": False},
    {"name": "copilot_cli", "binary": "copilot", "installed": False},
    {"name": "opencode_cli", "binary": "opencode", "installed": True},
    {"name": "cursor_cli", "binary": "agent", "installed": False},
    {"name": "antigravity_cli", "binary": "agy", "installed": False},
]

SESSIONS_PAYLOAD: List[Dict[str, Any]] = [
    {"id": "cao-abc123", "name": "cao-abc123", "status": "detached"},
    {"id": "cao-def456", "name": "cao-def456", "status": "active"},
]

TERMINALS_PAYLOAD: List[Dict[str, Any]] = [
    {
        "id": "cao-abc123-0",
        "tmux_session": "cao-abc123",
        "tmux_window": "0",
        "provider": "claude_code",
        "agent_profile": "developer",
        "last_active": "2026-07-25T10:00:00",
    },
    {
        "id": "cao-abc123-1",
        "tmux_session": "cao-abc123",
        "tmux_window": "1",
        "provider": None,
        "agent_profile": None,
        "last_active": None,
    },
]

WORKFLOWS_PAYLOAD: List[Dict[str, Any]] = [
    {
        "name": "review",
        "source_path": "/w/review.yaml",
        "mode": "sequential",
        "step_count": 3,
        "description": "code review",
        "indexed_at": "2026-07-25T09:00:00",
    },
    {
        "name": "adhoc",
        "source_path": "/w/adhoc.py",
        "mode": "script",
        "step_count": None,
        "description": "",
        "indexed_at": "2026-07-25T09:00:00",
    },
]

PROFILES_PAYLOAD: List[Dict[str, Any]] = [
    {
        "name": "developer",
        "source": "built-in",
        "loadable": True,
        "description": "senior developer",
        "role": "developer",
        "capabilities": ["code", "test"],
        "tags": ["core"],
        "duplicated_in": [],
    },
    {
        "name": "reviewer",
        "source": "built-in",
        "loadable": True,
        "description": "reviewer",
        "role": "reviewer",
        "capabilities": [],
        "tags": [],
        "duplicated_in": ["custom"],
    },
]

PROFILE_DETAIL_PAYLOAD: Dict[str, Any] = {
    "name": "developer",
    "description": "senior developer",
    "provider": "claude_code",
    "role": "developer",
    "system_prompt": "You are a senior developer.",
    "capabilities": ["code", "test"],
    "tags": ["core"],
}

MUTATING_VERBS = ("post", "put", "delete", "patch")


# --------------------------------------------------------------------------- #
# Fake requests.Response.                                                        #
# --------------------------------------------------------------------------- #


class FakeResponse:
    """Minimal stand-in for ``requests.Response`` used by the mocked GET.

    ``raise_for_status`` raises a real ``requests.exceptions.HTTPError`` (a
    ``RequestException`` subclass) for a non-2xx status, matching library
    behaviour so the client's error mapping is exercised for real.
    """

    def __init__(self, json_body: Any = None, status_code: int = 200) -> None:
        self._json_body = json_body
        self.status_code = status_code

    def json(self) -> Any:
        if isinstance(self._json_body, ValueError):
            raise self._json_body
        return self._json_body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error")


def _mock_requests(response: FakeResponse) -> mock.MagicMock:
    """Return a MagicMock that mimics the ``requests`` module.

    ``get`` returns ``response``; the mutating verbs are present as attributes
    so a test can assert they were *never* called (BR-1), and the real
    exception classes are preserved so ``except requests.exceptions.*`` works.
    """

    fake = mock.MagicMock(name="requests")
    fake.get.return_value = response
    fake.exceptions = requests.exceptions
    return fake


# --------------------------------------------------------------------------- #
# Happy-path: each GET method against the real f570de1 shape.                    #
# --------------------------------------------------------------------------- #


def test_health_happy_path() -> None:
    fake = _mock_requests(FakeResponse(HEALTH_PAYLOAD))
    with mock.patch.object(sc, "requests", fake):
        result = ServerClient().health()

    assert isinstance(result, HealthInfo)
    assert result.status == "ok"
    assert result.terminal_backend == "tmux"
    assert result.components["claude"] == "ok"
    # URL built from the base + path.
    assert fake.get.call_args.args[0].endswith("/health")


def test_providers_happy_path() -> None:
    fake = _mock_requests(FakeResponse(PROVIDERS_PAYLOAD))
    with mock.patch.object(sc, "requests", fake):
        result = ServerClient().providers()

    assert [p.name for p in result] == [row["name"] for row in PROVIDERS_PAYLOAD]
    assert all(isinstance(p, ProviderStatus) for p in result)
    kiro = result[0]
    assert kiro.binary == "kiro-cli" and kiro.installed is True
    # BR-3: ProviderStatus carries no authenticated field.
    assert not hasattr(kiro, "authenticated")


def test_sessions_happy_path() -> None:
    fake = _mock_requests(FakeResponse(SESSIONS_PAYLOAD))
    with mock.patch.object(sc, "requests", fake):
        result = ServerClient().sessions()

    assert [s.id for s in result] == ["cao-abc123", "cao-def456"]
    assert isinstance(result[0], SessionInfo)
    assert result[1].status == "active"


def test_terminals_happy_path_encodes_session_in_path() -> None:
    fake = _mock_requests(FakeResponse(TERMINALS_PAYLOAD))
    with mock.patch.object(sc, "requests", fake):
        result = ServerClient().terminals("cao-abc123")

    assert isinstance(result[0], TerminalInfo)
    assert result[0].provider == "claude_code"
    # Nullable provider/agent on a plain window stays None.
    assert result[1].provider is None and result[1].agent_profile is None
    assert fake.get.call_args.args[0].endswith("/sessions/cao-abc123/terminals")


def test_workflows_happy_path() -> None:
    fake = _mock_requests(FakeResponse(WORKFLOWS_PAYLOAD))
    with mock.patch.object(sc, "requests", fake):
        result = ServerClient().workflows()

    assert [w.name for w in result] == ["review", "adhoc"]
    assert isinstance(result[0], WorkflowSummary)
    assert result[0].step_count == 3
    assert result[1].step_count is None  # script spec: no step count


def test_profiles_happy_path() -> None:
    fake = _mock_requests(FakeResponse(PROFILES_PAYLOAD))
    with mock.patch.object(sc, "requests", fake):
        result = ServerClient().profiles()

    assert [p.name for p in result] == ["developer", "reviewer"]
    assert isinstance(result[0], ProfileSummary)
    assert result[0].capabilities == ["code", "test"]
    assert result[1].duplicated_in == ["custom"]


def test_profile_happy_path() -> None:
    fake = _mock_requests(FakeResponse(PROFILE_DETAIL_PAYLOAD))
    with mock.patch.object(sc, "requests", fake):
        result = ServerClient().profile("developer")

    assert isinstance(result, ProfileDetail)
    assert result.name == "developer"
    assert result.provider == "claude_code"
    assert result.system_prompt == "You are a senior developer."
    assert fake.get.call_args.args[0].endswith("/agents/profiles/developer")


# --------------------------------------------------------------------------- #
# BR-1 — read-only seam: only requests.get, never a mutating verb.              #
# --------------------------------------------------------------------------- #


def test_no_mutating_verb_is_ever_called_across_all_methods() -> None:
    """BR-1 / FR-4.1: every method uses requests.get; no post/put/delete/patch.

    Drive every read method behind the mocked ``requests`` and assert the
    mutating verbs were never touched. ``profile`` uses a 200 detail body.
    """

    responses = {
        "health": FakeResponse(HEALTH_PAYLOAD),
        "providers": FakeResponse(PROVIDERS_PAYLOAD),
        "sessions": FakeResponse(SESSIONS_PAYLOAD),
        "terminals": FakeResponse(TERMINALS_PAYLOAD),
        "workflows": FakeResponse(WORKFLOWS_PAYLOAD),
        "profiles": FakeResponse(PROFILES_PAYLOAD),
        "profile": FakeResponse(PROFILE_DETAIL_PAYLOAD),
    }

    for method_name, response in responses.items():
        fake = _mock_requests(response)
        with mock.patch.object(sc, "requests", fake):
            client = ServerClient()
            method = getattr(client, method_name)
            if method_name in ("terminals", "profile"):
                method("cao-abc123" if method_name == "terminals" else "developer")
            else:
                method()

        assert fake.get.called, f"{method_name} did not call requests.get"
        for verb in MUTATING_VERBS:
            assert not getattr(
                fake, verb
            ).called, f"{method_name} called forbidden requests.{verb} (BR-1 violation)"


def test_server_client_class_exposes_no_mutating_method() -> None:
    """Static guard: the ServerClient API surface has no mutating verb method."""

    for verb in MUTATING_VERBS:
        assert not hasattr(ServerClient, verb)


def test_module_source_contains_no_mutating_verb_call() -> None:
    """BR-1 source-scan: server_client.py never writes requests.<mutating-verb>(."""

    from pathlib import Path

    source = Path(sc.__file__).read_text(encoding="utf-8")
    for verb in MUTATING_VERBS:
        assert f"requests.{verb}(" not in source, f"found requests.{verb}( in source"


# --------------------------------------------------------------------------- #
# BR-5 — unreachable / timeout → ServerUnavailable.                             #
# --------------------------------------------------------------------------- #


def test_connection_error_maps_to_server_unavailable() -> None:
    fake = mock.MagicMock(name="requests")
    fake.exceptions = requests.exceptions
    fake.get.side_effect = requests.exceptions.ConnectionError("refused")
    with mock.patch.object(sc, "requests", fake):
        with pytest.raises(ServerUnavailable):
            ServerClient().health()


def test_timeout_maps_to_server_unavailable() -> None:
    """Edge case: a slow server that times out surfaces as ServerUnavailable."""

    fake = mock.MagicMock(name="requests")
    fake.exceptions = requests.exceptions
    fake.get.side_effect = requests.exceptions.Timeout("timed out")
    with mock.patch.object(sc, "requests", fake):
        with pytest.raises(ServerUnavailable):
            ServerClient().providers()


def test_non_2xx_status_maps_to_server_unavailable() -> None:
    """A 500 (via raise_for_status → HTTPError) is a RequestException → BR-5."""

    fake = _mock_requests(FakeResponse(None, status_code=500))
    with mock.patch.object(sc, "requests", fake):
        with pytest.raises(ServerUnavailable):
            ServerClient().sessions()


def test_default_timeout_is_passed_to_every_get() -> None:
    """BR-7: every GET carries a bounded timeout kwarg."""

    fake = _mock_requests(FakeResponse(PROVIDERS_PAYLOAD))
    with mock.patch.object(sc, "requests", fake):
        ServerClient().providers()

    assert fake.get.call_args.kwargs["timeout"] == sc.DEFAULT_TIMEOUT


# --------------------------------------------------------------------------- #
# BR-6 — malformed / unexpected shape → ServerClientError (surfaced).           #
# --------------------------------------------------------------------------- #


def test_wrong_top_level_type_raises_client_error() -> None:
    """providers() expects a JSON array; an object is a surfaced BR-6 error."""

    fake = _mock_requests(FakeResponse({"unexpected": "object"}))
    with mock.patch.object(sc, "requests", fake):
        with pytest.raises(ServerClientError):
            ServerClient().providers()


def test_missing_required_key_raises_client_error() -> None:
    """A provider row missing 'binary' is a surfaced BR-6 error, not a silent skip."""

    fake = _mock_requests(FakeResponse([{"name": "kiro_cli", "installed": True}]))
    with mock.patch.object(sc, "requests", fake):
        with pytest.raises(ServerClientError):
            ServerClient().providers()


def test_non_json_body_raises_client_error() -> None:
    """A 200 with a non-JSON body → ServerClientError (BR-6), not a crash."""

    fake = _mock_requests(FakeResponse(ValueError("no json")))
    with mock.patch.object(sc, "requests", fake):
        with pytest.raises(ServerClientError):
            ServerClient().health()


# --------------------------------------------------------------------------- #
# profile(name) 404 → ProfileNotFound (renderable, non-fatal).                  #
# --------------------------------------------------------------------------- #


def test_profile_404_raises_profile_not_found() -> None:
    fake = _mock_requests(FakeResponse(None, status_code=404))
    with mock.patch.object(sc, "requests", fake):
        with pytest.raises(ProfileNotFound):
            ServerClient().profile("does-not-exist")


def test_profile_404_is_not_server_unavailable() -> None:
    """A 404 must be distinct from unreachable: the server *did* answer."""

    fake = _mock_requests(FakeResponse(None, status_code=404))
    with mock.patch.object(sc, "requests", fake):
        with pytest.raises(ProfileNotFound):
            ServerClient().profile("missing")
        # Sanity: ProfileNotFound is not a ServerUnavailable subclass.
        assert not issubclass(ProfileNotFound, ServerUnavailable)


# --------------------------------------------------------------------------- #
# Edge cases — empty list responses map to empty result lists.                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "method_name, call",
    [
        ("providers", lambda c: c.providers()),
        ("sessions", lambda c: c.sessions()),
        ("workflows", lambda c: c.workflows()),
        ("profiles", lambda c: c.profiles()),
        ("terminals", lambda c: c.terminals("cao-abc123")),
    ],
)
def test_empty_list_response_returns_empty(method_name: str, call: Any) -> None:
    fake = _mock_requests(FakeResponse([]))
    with mock.patch.object(sc, "requests", fake):
        assert call(ServerClient()) == []
