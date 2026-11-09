"""``subagentRunId`` attribution: terminal ids, hashed session-incarnation ids.

Conditions accepted with the hashed session id (#1193): deterministic per
incarnation (same id across reconnects and replays), a fixed hash length of at
least 12 hex characters with no collision across the sessions of a run, and the
same value used as ``parentSubagentRunId`` by that session's terminals.
"""

from __future__ import annotations

import re
from test._support import agui_ambient_records as R
from typing import Any, Dict, List

import pytest

from cli_agent_orchestrator.services.agui import ambient_run
from cli_agent_orchestrator.services.agui.attribution import (
    subagent_run_id_for_session,
    subagent_run_id_for_terminal,
)

SESSION_ID_RE = re.compile(r"^session:[0-9a-f]{16}$")


def test_terminal_id_is_prefixed_terminal_id() -> None:
    assert subagent_run_id_for_terminal("aaaa1111") == "terminal:aaaa1111"


@pytest.mark.parametrize("fn", [subagent_run_id_for_terminal, subagent_run_id_for_session])
def test_empty_input_is_refused(fn: Any) -> None:
    with pytest.raises(ValueError):
        fn("")


def test_session_id_is_deterministic_fixed_length_and_hides_the_incarnation() -> None:
    inc = "3f2c9a1e-0000-4000-8000-1234567890ab"
    first, second = subagent_run_id_for_session(inc), subagent_run_id_for_session(inc)
    assert first == second
    assert SESSION_ID_RE.match(first)  # fixed: 16 hex chars, at least the 12 required
    assert inc not in first and inc.split("-")[0] not in first


def test_session_ids_do_not_collide_across_many_incarnations() -> None:
    ids = {subagent_run_id_for_session(f"incarnation-{n}") for n in range(20000)}
    assert len(ids) == 20000


def _run(incarnations: Dict[str, str]) -> ambient_run.AmbientRun:
    return ambient_run.AmbientRun(
        thread_id="t",
        run_id="r",
        incarnation_of=lambda name: incarnations.get(name),
        state_baseline_known=True,
    )


def _events(run: ambient_run.AmbientRun, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = [e for _, e in run.start()]
    for record in records:
        out.extend(e for _, e in run.feed(record))
    return [e.model_dump(by_alias=True, exclude_none=True) for e in out]


def test_sessions_of_one_run_get_distinct_ids_and_their_terminals_point_at_them() -> None:
    names = [f"cao-s{n}" for n in range(50)]
    records: List[Dict[str, Any]] = []
    for n, name in enumerate(names):
        records.append(dict(R.SESSION_LAUNCH, id=f"s-{n}", session_name=name))
        records.append(
            dict(R.TERMINAL_A_LAUNCH, id=f"t-{n}", session_name=name, terminal_id=f"term{n:04d}")
        )
    events = _events(_run({name: f"inc-{name}" for name in names}), records)

    session_ids = {
        e["name"]: e["subagentRunId"]
        for e in events
        if e["type"] == "SUBAGENT_STARTED" and e["subagentRunId"].startswith("session:")
    }
    assert len(set(session_ids.values())) == len(names)  # no collision inside the run
    assert all(SESSION_ID_RE.match(v) for v in session_ids.values())
    for n, name in enumerate(names):
        terminal = next(
            e
            for e in events
            if e["type"] == "SUBAGENT_STARTED" and e["subagentRunId"] == f"terminal:term{n:04d}"
        )
        assert terminal["parentSubagentRunId"] == session_ids[name]
        assert session_ids[name] == subagent_run_id_for_session(f"inc-{name}")


def test_same_incarnation_same_id_across_reconnects_and_replays() -> None:
    history = [R.SESSION_LAUNCH, R.TERMINAL_A_LAUNCH, R.HANDOFF]
    incarnations = {R.SESSION_LAUNCH["session_name"]: "inc-stable"}
    a = _events(_run(incarnations), history)
    b = _events(_run(incarnations), history[1:])  # a reconnect whose replay starts later
    sid = lambda evs: next(
        e["subagentRunId"] for e in evs if e["type"] == "SUBAGENT_STARTED"
    )  # noqa: E731
    assert sid(a) == sid(b) == subagent_run_id_for_session("inc-stable")


def test_relaunch_after_kill_is_deterministic_across_reconnects() -> None:
    relaunch = dict(R.SESSION_LAUNCH, id="evt-14", timestamp="2026-11-04T10:00:13.000000+00:00")
    history = [R.SESSION_LAUNCH, R.SESSION_KILL, relaunch]
    incarnations = {R.SESSION_LAUNCH["session_name"]: "inc-current"}
    started = lambda evs: [
        e["subagentRunId"] for e in evs if e["type"] == "SUBAGENT_STARTED"
    ]  # noqa: E731
    first, second = started(_events(_run(incarnations), history)), started(
        _events(_run(incarnations), history)
    )
    assert first == second
    assert len(set(first)) == 2


def test_stamped_session_id_on_the_record_wins() -> None:
    stamped = subagent_run_id_for_session("inc-at-log-time")
    launch = dict(
        R.SESSION_LAUNCH, detail={**R.SESSION_LAUNCH["detail"], "session_subagent_run_id": stamped}
    )
    events = _events(_run({R.SESSION_LAUNCH["session_name"]: "inc-current-pointer"}), [launch])
    assert next(e for e in events if e["type"] == "SUBAGENT_STARTED")["subagentRunId"] == stamped


def test_without_a_label_lookup_a_known_terminal_keeps_the_generic_name() -> None:
    """``terminal_known`` but no ``terminal_label`` (an embedder such as a trace
    recorder): the terminal is still announced, under the generic name."""
    run = ambient_run.AmbientRun(
        thread_id="t",
        run_id="r",
        incarnation_of=lambda name: "inc-1",
        terminal_known=lambda terminal_id: True,
    )
    ui = dict(R.GENERATIVE_UI, id="u-1", terminal_id="deadbeef")
    events = _events(run, [R.SESSION_LAUNCH, ui])
    started = {e["subagentRunId"]: e for e in events if e["type"] == "SUBAGENT_STARTED"}
    assert started["terminal:deadbeef"]["name"] == "terminal"


def test_without_a_terminal_lookup_an_unseen_terminal_stays_on_its_session() -> None:
    """No ``terminal_known`` callback: a terminal the run has not seen is never announced.

    The event falls back to its session invocation, so no phantom subagent appears
    when the caller cannot ask CAO which terminals exist.
    """
    run = _run({R.SESSION_LAUNCH["session_name"]: "inc-1"})
    ui = dict(R.GENERATIVE_UI, id="u-1", terminal_id="deadbeef")
    events = _events(run, [R.SESSION_LAUNCH, ui])

    started = [e["subagentRunId"] for e in events if e["type"] == "SUBAGENT_STARTED"]
    assert started == [subagent_run_id_for_session("inc-1")]
    (custom,) = [e for e in events if e["type"] == "CUSTOM" and e["name"] == "cao.generative_ui"]
    assert custom["subagentRunId"] == subagent_run_id_for_session("inc-1")
