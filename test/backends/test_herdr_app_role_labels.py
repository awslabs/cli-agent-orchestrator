"""Role labels: ``extra_labels`` carries cao.profile and cao.caller into terminal.run.

Specs: ``herdr-app-role-labels`` ("Terminals carry their profile and caller as
labels", "Extra labels cannot override reserved labels", "Existing terminals and
failures are unaffected") and the ``herdr-app-backend`` scenario "Profile and
caller labels on a new window".
"""

import inspect
import json
import logging
import re
from test.backends.fake_hc import FakeHc
from types import MappingProxyType
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.backends.base import TerminalBackend, TerminalBackendError
from cli_agent_orchestrator.backends.herdr_app_backend import (
    HerdrAppBackend,
    cao_home_label,
    space_id_for,
)
from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend
from cli_agent_orchestrator.backends.tmux_backend import TmuxBackend

APP_LOGGER = "cli_agent_orchestrator.backends.herdr_app_backend"

# The labels every CAO terminal carries today, before and after this change.
EXISTING_KEYS = {
    "herdr.space",
    "herdr.tab",
    "cao.managed",
    "cao.home",
    "cao.terminal_id",
    "cao.session",
    "cao.window",
}

# Every key the spec reserves, plus a free-form key under the `herdr.` prefix.
RESERVED_KEYS = [
    "herdr.space",
    "herdr.tab",
    "herdr.anything",
    "herdr.",
    "cao.managed",
    "cao.home",
    "cao.terminal_id",
    "cao.session",
    "cao.window",
]

CREATE_METHODS = ["create_session", "create_window"]


@pytest.fixture
def hc():
    return FakeHc()


@pytest.fixture
def backend(hc, monkeypatch):
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.mcp_resolution.cao_server_env",
        lambda: {"CAO_API": "http://x"},
    )
    return HerdrAppBackend(herdr_app_session="cao", client=hc)


def _create(backend, method, tmp_path, terminal_id="aaaa0001", **kwargs):
    """Call create_session or create_window with the same identity either way."""
    return getattr(backend, method)("cao-a", "w1", terminal_id, str(tmp_path), **kwargs)


def _labels(hc, index=-1):
    return hc.calls_to("terminal.run")[index]["labels"]


def _without_tab(labels):
    """The labels minus the random tab id, which differs on every call."""
    return {k: v for k, v in labels.items() if k != "herdr.tab"}


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


# --- Terminals carry their profile and caller as labels ---


@pytest.mark.parametrize("method", CREATE_METHODS)
def test_root_terminal_carries_profile_and_no_caller(backend, hc, tmp_path, method):
    _create(backend, method, tmp_path, extra_labels={"cao.profile": "conductor"})
    labels = _labels(hc)
    assert labels["cao.profile"] == "conductor"
    assert "cao.caller" not in labels
    assert set(labels) == EXISTING_KEYS | {"cao.profile"}


@pytest.mark.parametrize("method", CREATE_METHODS)
def test_worker_terminal_carries_profile_and_caller(backend, hc, tmp_path, method):
    _create(
        backend,
        method,
        tmp_path,
        extra_labels={"cao.profile": "reviewer", "cao.caller": "abcd1234"},
    )
    labels = _labels(hc)
    assert labels["cao.profile"] == "reviewer"
    assert labels["cao.caller"] == "abcd1234"
    assert set(labels) == EXISTING_KEYS | {"cao.profile", "cao.caller"}


def test_window_scenario_carries_every_existing_label_plus_role_labels(backend, hc, tmp_path):
    """herdr-app-backend, "Profile and caller labels on a new window"."""
    backend.create_session("cao-a", "sup", "abcd1234", str(tmp_path))
    assert (
        backend.create_window(
            "cao-a",
            "w2",
            "beef5678",
            str(tmp_path),
            extra_labels={"cao.profile": "reviewer", "cao.caller": "abcd1234"},
        )
        == "w2"
    )
    labels = _labels(hc)
    assert re.fullmatch(r"tb[0-9a-f]{32}", labels["herdr.tab"])
    assert _without_tab(labels) == {
        "herdr.space": space_id_for("cao-a"),
        "cao.managed": "1",
        "cao.home": cao_home_label(),
        "cao.terminal_id": "beef5678",
        "cao.session": "cao-a",
        "cao.window": "w2",
        "cao.profile": "reviewer",
        "cao.caller": "abcd1234",
    }
    # Same space as the root terminal of the session.
    assert labels["herdr.space"] == hc.calls_to("terminal.run")[0]["labels"]["herdr.space"]


@pytest.mark.parametrize("extra", ["omitted", None, {}], ids=["omitted", "none", "empty-mapping"])
@pytest.mark.parametrize("method", CREATE_METHODS)
def test_existing_labels_unchanged_without_role_labels(backend, hc, tmp_path, method, extra):
    kwargs = {} if extra == "omitted" else {"extra_labels": extra}
    _create(backend, method, tmp_path, **kwargs)
    assert set(_labels(hc)) == EXISTING_KEYS


@pytest.mark.parametrize("method", CREATE_METHODS)
def test_role_labels_arrive_in_the_same_terminal_run_call(backend, hc, tmp_path, method):
    """No second RPC sets labels, so no client sees the terminal without them."""
    _create(backend, method, tmp_path, extra_labels={"cao.profile": "developer"})
    runs = hc.calls_to("terminal.run")
    assert len(runs) == 1 and runs[0]["labels"]["cao.profile"] == "developer"
    assert not [m for m, _ in hc.calls if "label" in m]
    [stored] = hc.terminals.values()
    assert stored["labels"]["cao.profile"] == "developer"


def test_extra_labels_do_not_stick_to_the_next_terminal(backend, hc, tmp_path):
    backend.create_session(
        "cao-a", "w1", "aaaa0001", str(tmp_path), extra_labels={"cao.profile": "conductor"}
    )
    backend.create_window("cao-a", "w2", "aaaa0002", str(tmp_path))
    assert "cao.profile" not in _labels(hc)
    assert hc.calls_to("terminal.run")[0]["labels"]["cao.profile"] == "conductor"


@pytest.mark.parametrize(
    "value",
    ["café ☃ \U0001f600", "a=b c,d;e", "line1\nline2", "x" * 10_000, "  padded  "],
    ids=["unicode", "separators", "newline", "very-long", "padding"],
)
def test_label_values_reach_terminal_run_verbatim(backend, hc, tmp_path, value):
    _create(backend, "create_session", tmp_path, extra_labels={"cao.profile": value})
    assert _labels(hc)["cao.profile"] == value


@pytest.mark.parametrize(
    "key",
    [
        "herdr",
        "herdrx.space",
        "x.herdr.space",
        "cao.managed2",
        "cao.homes",
        "cao.role",
        "custom",
        # the match is literal and case-sensitive
        "HERDR.space",
        "Herdr.tab",
        "CAO.managed",
    ],
)
def test_keys_that_only_resemble_reserved_ones_are_kept(backend, hc, tmp_path, caplog, key):
    with caplog.at_level(logging.WARNING, logger=APP_LOGGER):
        _create(backend, "create_session", tmp_path, extra_labels={key: "v"})
    assert _labels(hc)[key] == "v"
    assert _warnings(caplog) == []


# --- Extra labels cannot override reserved labels ---


@pytest.mark.parametrize("method", CREATE_METHODS)
@pytest.mark.parametrize("reserved", RESERVED_KEYS)
def test_reserved_key_is_ignored_and_the_real_label_stands(
    backend, hc, tmp_path, caplog, method, reserved
):
    _create(backend, method, tmp_path, terminal_id="aaaa0001")
    baseline = _labels(hc)
    with caplog.at_level(logging.WARNING, logger=APP_LOGGER):
        _create(
            backend,
            method,
            tmp_path,
            terminal_id="aaaa0002",
            extra_labels={reserved: "evil", "cao.profile": "developer"},
        )
    labels = _labels(hc)
    assert labels["cao.profile"] == "developer"
    assert "evil" not in labels.values()
    assert set(labels) == EXISTING_KEYS | {"cao.profile"}
    # every reserved label has the value the backend computed, not the attacker's
    real = {**_without_tab(baseline), "cao.terminal_id": "aaaa0002"}
    assert _without_tab(labels) == {**real, "cao.profile": "developer"}
    assert re.fullmatch(r"tb[0-9a-f]{32}", labels["herdr.tab"])
    assert [w for w in _warnings(caplog) if reserved in w], _warnings(caplog)
    assert not [w for w in _warnings(caplog) if "evil" in w], _warnings(caplog)  # value not logged


def test_reserved_session_label_scenario(backend, hc, tmp_path, caplog):
    """Spec scenario: extra_labels={"cao.session": "other", "cao.profile": "developer"}."""
    with caplog.at_level(logging.WARNING, logger=APP_LOGGER):
        backend.create_session(
            "cao-a",
            "w1",
            "aaaa0001",
            str(tmp_path),
            extra_labels={"cao.session": "other", "cao.profile": "developer"},
        )
    labels = _labels(hc)
    assert labels["cao.session"] == "cao-a"
    assert labels["cao.profile"] == "developer"
    assert any("cao.session" in w for w in _warnings(caplog))


def test_every_ignored_key_is_named_in_its_own_warning(backend, hc, tmp_path, caplog):
    extra = {"cao.home": "x", "herdr.tab": "y", "cao.terminal_id": "z", "cao.profile": "p"}
    with caplog.at_level(logging.WARNING, logger=APP_LOGGER):
        _create(backend, "create_session", tmp_path, extra_labels=extra)
    warnings = _warnings(caplog)
    ignored = ("cao.home", "herdr.tab", "cao.terminal_id")
    assert len(warnings) == 3, warnings  # one record per ignored key, not one joined record
    for message in warnings:
        assert len([key for key in ignored if key in message]) == 1, message
    for key in ignored:
        assert len([w for w in warnings if key in w]) == 1, warnings
    assert not [w for w in warnings if "cao.profile" in w], warnings
    assert _labels(hc)["cao.profile"] == "p"


def test_no_warning_when_nothing_is_ignored(backend, hc, tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger=APP_LOGGER):
        _create(
            backend,
            "create_session",
            tmp_path,
            extra_labels={"cao.profile": "developer", "cao.caller": "abcd1234"},
        )
    assert _warnings(caplog) == []


def test_extra_labels_mapping_is_not_modified(backend, hc, tmp_path):
    source = {"cao.session": "other", "cao.profile": "developer"}
    _create(backend, "create_session", tmp_path, extra_labels=source)
    assert source == {"cao.session": "other", "cao.profile": "developer"}
    # any Mapping is accepted, including a read-only one
    frozen = MappingProxyType({"cao.profile": "reviewer", "herdr.space": "evil"})
    _create(backend, "create_window", tmp_path, terminal_id="aaaa0002", extra_labels=frozen)
    assert _labels(hc)["cao.profile"] == "reviewer"
    assert _labels(hc)["herdr.space"] == space_id_for("cao-a")


# --- Existing terminals and failures are unaffected ---


@pytest.mark.parametrize("method", CREATE_METHODS)
def test_failed_terminal_run_raises_backend_error_and_leaves_no_state(
    backend, hc, tmp_path, method
):
    hc.fail_methods.add("terminal.run")
    with pytest.raises(TerminalBackendError, match="could not start terminal aaaa0001"):
        _create(
            backend,
            method,
            tmp_path,
            extra_labels={"cao.profile": "developer", "cao.caller": "abcd1234"},
        )
    assert hc.terminals == {}
    assert hc.calls_to("doc.set") == []  # the layout was never touched
    assert [m for m, _ in hc.calls if "label" in m] == []


def test_failed_terminal_run_with_a_reserved_key_still_raises_backend_error(backend, hc, tmp_path):
    hc.fail_methods.add("terminal.run")
    with pytest.raises(TerminalBackendError):
        _create(backend, "create_session", tmp_path, extra_labels={"cao.session": "other"})
    assert hc.terminals == {}


def test_a_retry_after_a_failed_run_carries_only_its_own_labels(backend, hc, tmp_path):
    hc.fail_methods.add("terminal.run")
    with pytest.raises(TerminalBackendError):
        _create(backend, "create_session", tmp_path, extra_labels={"cao.profile": "first"})
    hc.fail_methods.clear()
    _create(backend, "create_session", tmp_path)
    assert set(_labels(hc)) == EXISTING_KEYS


# --- The other backends accept extra_labels and ignore it ---

LABELS = {"cao.profile": "developer", "cao.caller": "t1", "cao.session": "other"}


def _tmux_calls(spawn_mode, method, extra):
    client = MagicMock()
    for name in ("create_session", "create_window", "create_pane"):
        getattr(client, name).return_value = f"{name}-result"
    kwargs = {} if extra is None else {"extra_labels": extra}
    backend = TmuxBackend(client=client, spawn_mode=spawn_mode)
    if method == "create_session":
        result = backend.create_session("cao-a", "w", "t1", "/tmp", **kwargs)
    else:
        result = backend.create_window("cao-a", "w2", "t2", "/tmp", **kwargs)
    return result, client.mock_calls


@pytest.mark.parametrize(
    "spawn_mode, method",
    [("window", "create_session"), ("window", "create_window"), ("pane", "create_window")],
)
def test_tmux_backend_ignores_extra_labels(spawn_mode, method):
    plain_result, plain_calls = _tmux_calls(spawn_mode, method, None)
    result, calls = _tmux_calls(spawn_mode, method, LABELS)
    assert calls == plain_calls and len(calls) == 1
    assert result == plain_result
    assert "extra_labels" not in str(calls)


def _herdr_tui_calls(method, extra):
    """Run one create call against the herdr TUI backend; return (result, argv list)."""
    workspace = {"label": "cao-a", "workspace_id": "w1"}
    pane = {"pane_id": "w1-1", "workspace_id": "w1", "tab_id": "tab-0"}

    def completed(payload=""):
        proc = MagicMock()
        proc.stdout = payload
        proc.returncode = 0
        proc.stderr = ""
        return proc

    def respond(cmd, *args, **kwargs):
        if "list" in cmd:
            return completed(json.dumps({"result": {"workspaces": [workspace]}}))
        if "create" in cmd:
            return completed(json.dumps({"result": {"workspace_id": "w1", "root_pane": pane}}))
        return completed()

    kwargs = {} if extra is None else {"extra_labels": extra}
    with patch("cli_agent_orchestrator.backends.herdr_backend.os.path.exists", return_value=True):
        backend = HerdrBackend(send_delay_ms=0)
    with patch("subprocess.run", side_effect=respond) as run:
        if method == "create_session":
            result = backend.create_session("cao-a", "w", "t1", "/tmp", **kwargs)
        else:
            result = backend.create_window("cao-a", "w2", "t2", "/tmp", **kwargs)
    return result, [c[0][0] for c in run.call_args_list]


@pytest.mark.parametrize("method", CREATE_METHODS)
def test_herdr_tui_backend_ignores_extra_labels(method):
    plain_result, plain_argv = _herdr_tui_calls(method, None)
    result, argv = _herdr_tui_calls(method, LABELS)
    assert argv == plain_argv and argv
    assert result == plain_result
    assert not [a for cmd in argv for a in cmd if "cao.profile" in a or "developer" in a]


# --- The contract ---


@pytest.mark.parametrize("cls", [TerminalBackend, TmuxBackend, HerdrBackend, HerdrAppBackend])
@pytest.mark.parametrize("method", CREATE_METHODS)
def test_every_backend_declares_keyword_only_extra_labels(cls, method):
    param = inspect.signature(getattr(cls, method)).parameters["extra_labels"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is None
