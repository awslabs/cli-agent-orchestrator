"""``cao.waiting``: the herdr-app backend publishes how many workers a terminal waits on.

Spec ``herdr-app-role-labels``: "A terminal that waits on workers carries
``cao.waiting``" and "``cao.waiting`` is reserved" (design D38).
"""

import logging
from test.backends.fake_hc import FakeHc

import pytest

from cli_agent_orchestrator.backends.base import TerminalBackend
from cli_agent_orchestrator.backends.herdr_app_backend import HerdrAppBackend
from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend
from cli_agent_orchestrator.backends.tmux_backend import TmuxBackend
from cli_agent_orchestrator.clients.hc_rpc import HcRpcError

APP_LOGGER = "cli_agent_orchestrator.backends.herdr_app_backend"
WAITING = "cao.waiting"


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


def _terminal(backend, hc, terminal_id, **extra):
    """A live CAO terminal of this server, as the core lists it."""
    labels = {**backend._managed, "cao.terminal_id": terminal_id, "cao.session": "cao-a"}
    return hc.add_terminal({**labels, **extra})


def _label_calls(hc):
    return hc.calls_to("terminal.label")


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


# --- Setting and removing the label ---


def test_a_count_is_set_as_a_decimal_string_on_the_terminal_with_that_id(backend, hc):
    caller = _terminal(backend, hc, "c1c1c1c1")
    other = _terminal(backend, hc, "d2d2d2d2")

    backend.set_waiting_counts({"c1c1c1c1": 2})

    assert caller["labels"][WAITING] == "2"
    assert WAITING not in other["labels"]
    assert _label_calls(hc) == [{"terminal": caller["address"], "labels": {WAITING: "2"}}]


def test_a_changed_count_replaces_the_value(backend, hc):
    caller = _terminal(backend, hc, "c1c1c1c1", **{WAITING: "2"})

    backend.set_waiting_counts({"c1c1c1c1": 1})

    assert caller["labels"][WAITING] == "1"


def test_a_count_of_zero_removes_the_label(backend, hc):
    caller = _terminal(backend, hc, "c1c1c1c1", **{WAITING: "1"})

    backend.set_waiting_counts({"c1c1c1c1": 0})

    assert WAITING not in caller["labels"]
    assert _label_calls(hc) == [{"terminal": caller["address"], "labels": {WAITING: ""}}]


def test_a_label_that_already_matches_makes_no_call(backend, hc):
    _terminal(backend, hc, "c1c1c1c1", **{WAITING: "2"})
    _terminal(backend, hc, "d2d2d2d2")

    backend.set_waiting_counts({"c1c1c1c1": 2, "d2d2d2d2": 0})

    assert _label_calls(hc) == []


def test_an_id_with_no_live_terminal_is_skipped(backend, hc):
    caller = _terminal(backend, hc, "c1c1c1c1")

    backend.set_waiting_counts({"gone0000": 3, "c1c1c1c1": 1})

    assert caller["labels"][WAITING] == "1"
    assert len(_label_calls(hc)) == 1


def test_a_finished_terminal_is_not_labelled(backend, hc):
    caller = _terminal(backend, hc, "c1c1c1c1")
    caller["state"] = "finished"

    backend.set_waiting_counts({"c1c1c1c1": 1})

    assert WAITING not in caller["labels"]


def test_an_empty_update_makes_no_call_at_all(backend, hc):
    _terminal(backend, hc, "c1c1c1c1", **{WAITING: "1"})

    backend.set_waiting_counts({})

    assert hc.calls_to("terminal.list") == []
    assert _label_calls(hc) == []


# --- The complete pass (the sweep) ---


def test_a_complete_pass_removes_the_label_from_a_terminal_that_waits_on_nothing(backend, hc):
    stale = _terminal(backend, hc, "c1c1c1c1", **{WAITING: "2"})
    waits = _terminal(backend, hc, "d2d2d2d2", **{WAITING: "1"})

    backend.set_waiting_counts({"d2d2d2d2": 1}, complete=True)

    assert WAITING not in stale["labels"]
    assert waits["labels"][WAITING] == "1"
    assert _label_calls(hc) == [{"terminal": stale["address"], "labels": {WAITING: ""}}]


def test_a_complete_pass_with_no_counts_clears_every_stale_label(backend, hc):
    a = _terminal(backend, hc, "c1c1c1c1", **{WAITING: "2"})
    b = _terminal(backend, hc, "d2d2d2d2", **{WAITING: "1"})

    backend.set_waiting_counts({}, complete=True)

    assert WAITING not in a["labels"]
    assert WAITING not in b["labels"]


def test_a_complete_pass_corrects_a_wrong_count_and_adds_a_missing_one(backend, hc):
    wrong = _terminal(backend, hc, "c1c1c1c1", **{WAITING: "5"})
    missing = _terminal(backend, hc, "d2d2d2d2")

    backend.set_waiting_counts({"c1c1c1c1": 1, "d2d2d2d2": 2}, complete=True)

    assert wrong["labels"][WAITING] == "1"
    assert missing["labels"][WAITING] == "2"


def test_a_partial_pass_leaves_other_labels_alone(backend, hc):
    other = _terminal(backend, hc, "d2d2d2d2", **{WAITING: "1"})

    backend.set_waiting_counts({"c1c1c1c1": 0})

    assert other["labels"][WAITING] == "1"


def test_a_complete_pass_never_touches_a_terminal_of_another_cao_home(backend, hc):
    mine = _terminal(backend, hc, "c1c1c1c1", **{WAITING: "2"})
    foreign = hc.add_terminal(
        {
            "cao.managed": "1",
            "cao.home": "someoneelse00000",
            "cao.terminal_id": "e3e3e3e3",
            "cao.session": "cao-a",
            WAITING: "4",
        }
    )

    backend.set_waiting_counts({"e3e3e3e3": 0}, complete=True)

    assert foreign["labels"][WAITING] == "4", "another server's terminal is not this server's"
    assert WAITING not in mine["labels"]


def test_a_terminal_that_is_not_cao_managed_is_never_touched(backend, hc):
    bystander = hc.add_terminal({WAITING: "7", "cao.terminal_id": "f4f4f4f4"})

    backend.set_waiting_counts({"f4f4f4f4": 0}, complete=True)

    assert bystander["labels"][WAITING] == "7"
    assert _label_calls(hc) == []


# --- Failures ---


def test_a_failed_write_is_logged_and_the_others_still_go_through(backend, hc, caplog):
    first = _terminal(backend, hc, "c1c1c1c1")
    second = _terminal(backend, hc, "d2d2d2d2")
    real_call = hc.call

    def flaky(method, params=None, timeout=10.0):
        if method == "terminal.label" and params["terminal"] == first["address"]:
            raise HcRpcError("unavailable", "label failed")
        return real_call(method, params, timeout)

    hc.call = flaky
    with caplog.at_level(logging.WARNING, logger=APP_LOGGER):
        backend.set_waiting_counts({"c1c1c1c1": 1, "d2d2d2d2": 2})

    assert WAITING not in first["labels"]
    assert second["labels"][WAITING] == "2"
    assert any("c1c1c1c1" in w and WAITING in w for w in _warnings(caplog)), _warnings(caplog)


def test_a_failed_listing_is_logged_and_raises_nothing(backend, hc, caplog):
    hc.fail_methods.add("terminal.list")

    with caplog.at_level(logging.WARNING, logger=APP_LOGGER):
        backend.set_waiting_counts({"c1c1c1c1": 1}, complete=True)

    assert _label_calls(hc) == []
    assert any(WAITING in w for w in _warnings(caplog)), _warnings(caplog)


# --- Bounded, quiet, clamped (review of the waiting label, round 1) ---


def test_every_rpc_of_a_label_pass_has_a_short_timeout(backend, hc):
    _terminal(backend, hc, "c1c1c1c1")
    timeouts = []
    real_call = hc.call

    def recording(method, params=None, timeout=10.0):
        timeouts.append((method, timeout))
        return real_call(method, params, timeout)

    hc.call = recording
    backend.set_waiting_counts({"c1c1c1c1": 1})

    assert [m for m, _ in timeouts] == ["terminal.list", "terminal.label"]
    assert all(t <= 3.0 for _, t in timeouts), timeouts


def test_a_failing_core_is_one_warning_per_episode_not_one_per_call(backend, hc, caplog):
    hc.fail_methods.add("terminal.list")

    with caplog.at_level(logging.DEBUG, logger=APP_LOGGER):
        for _ in range(3):
            backend.set_waiting_counts({"c1c1c1c1": 1})
        assert len(_warnings(caplog)) == 1

        hc.fail_methods.clear()  # the core answers again: the episode ends
        backend.set_waiting_counts({"c1c1c1c1": 1})
        hc.fail_methods.add("terminal.list")
        backend.set_waiting_counts({"c1c1c1c1": 1})

    assert len(_warnings(caplog)) == 2


def test_a_count_above_999_is_written_as_999(backend, hc):
    caller = _terminal(backend, hc, "c1c1c1c1")

    backend.set_waiting_counts({"c1c1c1c1": 1500})

    assert caller["labels"][WAITING] == "999"


def test_a_label_already_at_999_is_not_rewritten_for_a_larger_count(backend, hc):
    _terminal(backend, hc, "c1c1c1c1", **{WAITING: "999"})

    backend.set_waiting_counts({"c1c1c1c1": 1500})

    assert _label_calls(hc) == []


# --- The reserved key ---


@pytest.mark.parametrize("method", ["create_session", "create_window"])
def test_a_caller_cannot_set_waiting_through_extra_labels(backend, hc, tmp_path, caplog, method):
    with caplog.at_level(logging.WARNING, logger=APP_LOGGER):
        getattr(backend, method)(
            "cao-a",
            "w1",
            "aaaa0001",
            str(tmp_path),
            extra_labels={WAITING: "3", "cao.profile": "developer"},
        )

    labels = hc.calls_to("terminal.run")[-1]["labels"]
    assert WAITING not in labels
    assert labels["cao.profile"] == "developer"
    assert any(WAITING in w for w in _warnings(caplog)), _warnings(caplog)


# --- Other backends ---


@pytest.mark.parametrize("backend_class", [TmuxBackend, HerdrBackend])
def test_other_backends_inherit_a_default_that_does_nothing(backend_class):
    assert backend_class.set_waiting_counts is TerminalBackend.set_waiting_counts
    instance = object.__new__(backend_class)

    assert instance.set_waiting_counts({"c1c1c1c1": 2}, complete=True) is None
