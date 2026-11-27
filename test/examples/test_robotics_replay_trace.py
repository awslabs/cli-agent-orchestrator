"""The robotics replay trace, its leak rule and its page (#1166).

The trace is a MOCK run of the hierarchical-factory example recorded through the CAO Dojo's own
scrubber and leak rule. This file holds what is specific to it:

* **Leak rule with a negative control.** The published trace passes ``trace_leaks.check``, and
  each mutation below injects one leak into a copy of it and asserts that the rule fires, at the
  path where the leak was put. A check that cannot fail on a leaky trace proves nothing, so a
  deleted rule fails its own case. The cases are the two gaps the scrubber used to have (raw
  ids inside a ``STATE_DELTA`` value and in ``metadata.cao.sender/receiver``), plus a raw
  ``threadId`` and a host name.
* **The side log** has no path, e-mail address, dotted host name or long hex id, and a mutated
  copy is refused (a single-label host name reads like a word and is not).
* **Content.** Three assigns, one handoff, every call closed, one approval resumed with
  ``approved: true``, every party a ``terminal-N`` token.
* **The page.** ``replay.html`` and ``replay-rows.js`` are static, dependency-free readers. The
  row builder is run under ``node`` on the real trace (skipped where ``node`` is absent) and
  compared with counts taken from the file here, not from the builder.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from .cao_dojo_trace_support import trace_leaks
from .cao_dojo_trace_support import trace_scrub as scrub
from .test_cao_dojo_trace_replay import frame_extras, lifecycle_errors, runs_of

ROOT = Path(__file__).resolve().parents[2] / "examples" / "ag-ui" / "robotics-replay"
TRACE = ROOT / "trace" / "robotics-mock-trace.ndjson"
LOG = ROOT / "trace" / "factory-command-log.json"
PAGE = ROOT / "replay.html"
ROWS_JS = ROOT / "replay-rows.js"
RECORDER = ROOT / "tools" / "record_robotics_trace.py"
NODE = shutil.which("node")

Records = List[Dict[str, Any]]


def load() -> Tuple[Dict[str, Any], Records]:
    meta, records = scrub.parse(TRACE.read_bytes())
    return copy.deepcopy(meta), copy.deepcopy(records)


def join(meta: Dict[str, Any], records: Records) -> bytes:
    lines = [scrub.dumps({"_meta": meta})] + [scrub.dumps(r) for r in records]
    return ("\n".join(lines) + "\n").encode("utf-8")


def stream_frames(records: Records, ftype: str) -> List[Dict[str, Any]]:
    return [r["frame"] for r in records if r["plane"] == "stream" and r["frame"]["type"] == ftype]


# --- The published trace is clean, and the rule can fail ---------------------------


def test_the_published_trace_has_no_leak_finding():
    findings = trace_leaks.check(TRACE.read_bytes())
    assert findings == [], trace_leaks.format_findings(findings)


def test_the_published_trace_is_what_the_scrubber_writes():
    """Normalising the parsed records again changes nothing: the file is the scrubber's output."""
    meta, records = load()
    assert scrub.normalise(meta, records) == TRACE.read_bytes()


def _leak_state_delta_id(records: Records) -> str:
    for r in records:
        frame = r.get("frame") or {}
        for op in frame.get("delta") or []:
            if op.get("path") == "/terminals" and op.get("value"):
                op["value"][0]["id"] = "e327d8c9a1b2"
                return f"$.frame.delta[{frame['delta'].index(op)}].value[0].id"
    raise AssertionError("no STATE_DELTA /terminals op in the trace")


def _leak_sender(records: Records) -> str:
    for r in records:
        cao = ((r.get("frame") or {}).get("metadata") or {}).get("cao") or {}
        if "sender" in cao:
            cao["sender"] = "c0ffee11deadbeef"
            return "$.frame.metadata.cao.sender"
    raise AssertionError("no sender in the trace")


def _leak_thread_id(records: Records) -> str:
    for r in records:
        frame = r.get("frame") or {}
        if "runId" in frame and r["plane"] == "run":
            frame["runId"] = "9f3a6b2c-1d4e-4f50-8a7b-2c3d4e5f6a7b"
            return "$.frame.runId"
    raise AssertionError("no run-plane runId in the trace")


def _leak_runner_host(records: Records) -> str:
    for r in records:
        frame = r.get("frame") or {}
        for op in frame.get("delta") or []:
            if op.get("path") == "/terminals" and op.get("value"):
                op["value"][0]["runner_id"] = "build-box-3.internal"
                return f"$.frame.delta[{frame['delta'].index(op)}].value[0].runner_id"
    raise AssertionError("no STATE_DELTA /terminals op in the trace")


@pytest.mark.parametrize(
    "mutate,rule",
    [
        (_leak_state_delta_id, "L5"),
        (_leak_sender, "L17"),
        (_leak_thread_id, "L17"),
        (_leak_runner_host, "L15"),
    ],
    ids=["state-delta-id", "metadata-sender", "run-id", "runner-host"],
)
def test_a_deliberately_leaky_trace_is_refused(mutate, rule):
    """Negative control: the same trace with one leak injected must produce a finding."""
    meta, records = load()
    path = mutate(records)
    findings = trace_leaks.check(join(meta, records))
    assert findings, f"the leak rule found nothing in a trace with a leak at {path}"
    assert any(f.rule_id == rule and f.json_path == path for f in findings), (
        rule,
        path,
        [(f.rule_id, f.json_path) for f in findings],
    )


def test_the_mutations_are_the_only_change():
    """Without a mutation the same round trip is clean, so a finding above is the mutation's."""
    meta, records = load()
    assert trace_leaks.check(join(meta, records)) == []


# --- The side log -------------------------------------------------------------------

LOG_VALUE = re.compile(r"^[A-Za-z0-9 _.,;()-]*$")
DOTTED = re.compile(r"[A-Za-z0-9-]+\.[A-Za-z][A-Za-z0-9-]*")
HEX8 = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{8,}(?![0-9A-Fa-f])")


def log_findings(log: Dict[str, Any]) -> List[str]:
    out: List[str] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, f"{path}.{k}")
        elif isinstance(value, list):
            for i, v in enumerate(value):
                walk(v, f"{path}[{i}]")
        elif isinstance(value, str):
            if not LOG_VALUE.match(value) or DOTTED.search(value) or HEX8.search(value):
                out.append(path)

    walk(log, "$")
    return out


def test_the_command_log_holds_no_path_email_dotted_host_or_id():
    assert log_findings(json.loads(LOG.read_text(encoding="utf-8"))) == []


@pytest.mark.parametrize(
    "value, rule",
    [
        ("/" + "Us" + "ers/someone/work", LOG_VALUE),  # a path: "/" is outside plain words
        ("a@b", LOG_VALUE),  # an e-mail address
        ("build-box-3.internal", DOTTED),  # a host name with a domain, nothing else odd
        ("my-laptop.local", DOTTED),
        ("e327d8c9a1b2", HEX8),  # a long hex id, plain characters only
    ],
    ids=["path", "email", "dotted-host", "dotted-host-local", "hex-id"],
)
def test_a_leaky_command_log_is_refused_by_the_rule_that_names_it(value, rule, monkeypatch):
    """Each value is refused, and only that rule refuses it: with the rule disabled it passes."""
    log = json.loads(LOG.read_text(encoding="utf-8"))
    log["commands"][0]["arguments"][0] = value
    assert log_findings(log), value
    never = re.compile(r"(?!x)x")  # matches nothing
    disabled = {"LOG_VALUE": re.compile(r".*"), "DOTTED": never, "HEX8": never}
    for name, compiled in (("LOG_VALUE", LOG_VALUE), ("DOTTED", DOTTED), ("HEX8", HEX8)):
        if compiled is rule:
            monkeypatch.setitem(globals(), name, disabled[name])
    assert log_findings(log) == [], f"{value} is refused by a rule other than the one named"


def _recorder_rule():
    """The recorder's own command-log rule, taken from its source: the three regexes and
    command_log_findings. The recorder imports mujoco at the top, so it is not imported;
    only these four statements are compiled and run."""
    import ast
    import typing

    tree = ast.parse(RECORDER.read_text(encoding="utf-8"))
    wanted = {"_LOG_VALUE", "_DOTTED", "_HEX8", "command_log_findings"}
    nodes = [
        n
        for n in tree.body
        if (
            isinstance(n, ast.Assign)
            and isinstance(n.targets[0], ast.Name)
            and n.targets[0].id in wanted
        )
        or (isinstance(n, ast.FunctionDef) and n.name in wanted)
    ]
    ns: Dict[str, Any] = {"re": re, "Any": typing.Any, "List": typing.List}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(RECORDER), "exec"), ns)
    assert wanted <= set(ns), sorted(wanted - set(ns))
    return ns


def test_the_recorder_and_this_file_use_the_same_command_log_rule():
    """Drift guard: the recorder's three patterns are this file's, and its own function
    refuses and passes the same values this file's rule does."""
    ns = _recorder_rule()
    assert ns["_LOG_VALUE"].pattern == LOG_VALUE.pattern
    assert ns["_DOTTED"].pattern == DOTTED.pattern
    assert ns["_HEX8"].pattern == HEX8.pattern
    log = json.loads(LOG.read_text(encoding="utf-8"))
    assert ns["command_log_findings"](log) == []
    for value in ("/" + "Us" + "ers/someone/work", "a@b", "build-box-3.internal", "e327d8c9a1b2"):
        leaky = copy.deepcopy(log)
        leaky["commands"][0]["arguments"][0] = value
        assert ns["command_log_findings"](leaky) == ["$.commands[0].arguments[0]"], value
        assert log_findings(leaky) == ["$.commands[0].arguments[0]"], value
    for value in ("buildhost", "motion_not_approved"):
        plain = copy.deepcopy(log)
        plain["commands"][0]["arguments"][0] = value
        assert ns["command_log_findings"](plain) == [], value


@pytest.mark.parametrize("value", ["buildhost", "done", "motion_not_approved", "form-1"])
def test_plain_words_pass_including_a_single_label_host_name(value):
    """A single-label host name cannot be told from a word, so it is not refused (documented)."""
    log = json.loads(LOG.read_text(encoding="utf-8"))
    log["commands"][0]["arguments"][0] = value
    assert log_findings(log) == []


# --- What the trace records -----------------------------------------------------------


def test_meta_names_a_mock_recording_of_a_clean_build():
    meta, _ = load()
    assert meta["mode"] == "mock" and meta["provider"] == "mock_cli"
    assert meta["captures"] == ["robotics-mock"]
    assert meta["cao_build_dirty"] is False
    assert re.fullmatch(r"[0-9a-f]{40}", meta["cao_build"])


def test_three_assigns_and_one_handoff_each_opened_and_closed():
    _, records = load()
    starts = stream_frames(records, "TOOL_CALL_START")
    ends = stream_frames(records, "TOOL_CALL_END")
    assert [f["toolCallName"] for f in starts] == ["assign", "assign", "assign", "handoff"]
    assert sorted(f["toolCallId"] for f in ends) == sorted(f["toolCallId"] for f in starts)
    assert {f["metadata"]["cao"]["closedBy"] for f in ends} == {"completion"}


def test_the_approval_is_raised_then_resumed_with_approved_true():
    _, records = load()
    cards = [
        f["value"]["props"]
        for f in stream_frames(records, "CUSTOM")
        if f["value"]["component"] == "approval_card"
    ]
    # Raised once, then resolved twice: first "expired", then "approve".
    assert len(cards) == 3, cards
    assert cards[0].get("options") == ["approve", "deny"] and "resolved" not in cards[0]
    assert [(c.get("resolved"), c.get("outcome")) for c in cards[1:]] == [
        (True, "expired"),
        (True, "approve"),
    ]
    assert len({c["interrupt_id"] for c in cards}) == 1
    resumes = [r for r in records if r["dir"] == "request" and r["request"]["resume"]]
    assert len(resumes) == 1 and resumes[0]["request"]["resume"][0]["payload"] == {"approved": True}
    # The order on the wire: card raised, run resumed, then both resolutions.
    card_seqs = [
        r["seq"]
        for r in records
        if r["plane"] == "stream"
        and r["frame"]["type"] == "CUSTOM"
        and r["frame"]["value"]["component"] == "approval_card"
    ]
    assert card_seqs[0] < resumes[0]["seq"] < card_seqs[1] < card_seqs[2]


def test_every_party_is_a_terminal_token():
    _, records = load()
    for frame in stream_frames(records, "TOOL_CALL_START"):
        cao = frame["metadata"]["cao"]
        assert re.fullmatch(r"terminal-\d+", cao["sender"]), cao
        assert re.fullmatch(r"terminal-\d+", cao["receiver"]), cao


def test_every_run_is_lifecycle_legal_and_sdk_described():
    _, records = load()
    runs = runs_of(records)
    assert set(runs) == {"robotics-run-1", "robotics-run-2"}
    for name, frames in runs.items():
        assert lifecycle_errors(frames) == [], name
        for frame in frames:
            assert frame_extras(frame) == [], (name, frame["type"])


def test_the_factory_ended_with_every_team_done():
    log = json.loads(LOG.read_text(encoding="utf-8"))
    assert log["final_team_status"] == {"west": "done", "middle": "done", "east": "done"}
    assert log["label"].startswith("MOCK agents")


# --- The page and the recorder ---------------------------------------------------------

FORBIDDEN = (
    "innerHTML",
    "outerHTML",
    "eval(",
    "new Function",
    "EventSource",
    "XMLHttpRequest",
    "WebSocket",
    "document.write",
    'method: "POST"',
    "method:'POST'",
    "sendBeacon",
)


@pytest.mark.parametrize("path", [PAGE, ROWS_JS], ids=["replay.html", "replay-rows.js"])
def test_the_page_is_a_read_only_dependency_free_reader(path):
    text = path.read_text(encoding="utf-8")
    for token in FORBIDDEN:
        assert token not in text, f"{path.name} contains {token}"
    code = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    assert not re.search(r"""(?:src|href)\s*=\s*["'](?:https?:)?//""", code), "an external resource"
    assert "fetch(" in text if path is PAGE else True


def test_the_page_reads_only_the_two_files_beside_it():
    text = PAGE.read_text(encoding="utf-8")
    assert 'var TRACE = "trace/robotics-mock-trace.ndjson"' in text
    assert 'var LOG = "trace/factory-command-log.json"' in text
    assert TRACE.is_file() and LOG.is_file()


def test_the_recorder_uses_the_dojo_scrubber_and_leak_rule_unmodified():
    """It must call them, and must not patch, wrap or replace either."""
    text = RECORDER.read_text(encoding="utf-8")
    assert "ts.normalise(" in text and "leak_check(" in text
    for patch in ("ts.classify =", "scrub.classify =", "_walk =", "def normalise", "def classify"):
        assert patch not in text, patch


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_row_builder_agrees_with_counts_taken_from_the_file():
    script = (
        "const R=require(process.argv[1]),fs=require('fs');"
        "const t=R.parseNdjson(fs.readFileSync(process.argv[2],'utf8'));"
        "const l=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));"
        "console.log(JSON.stringify(R.buildRows(t.records,l.commands)));"
    )
    out = subprocess.run(
        [NODE, "-e", script, str(ROWS_JS), str(TRACE), str(LOG)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    rows = json.loads(out)
    _, records = load()
    log = json.loads(LOG.read_text(encoding="utf-8"))

    kinds = [r["kind"] for r in rows]
    n_starts = len(stream_frames(records, "TOOL_CALL_START"))
    n_ends = len(stream_frames(records, "TOOL_CALL_END"))
    n_agents = len(stream_frames(records, "SUBAGENT_STARTED")) + len(
        stream_frames(records, "SUBAGENT_FINISHED")
    )
    n_cards = sum(
        1 for f in stream_frames(records, "CUSTOM") if f["value"]["component"] == "approval_card"
    )
    n_resumes = sum(len(r["request"]["resume"]) for r in records if r["dir"] == "request")
    assert kinds.count("call") == n_starts + n_ends == 8
    assert kinds.count("agent") == n_agents
    assert kinds.count("approval") == n_cards + n_resumes
    assert kinds.count("factory") == len(log["commands"])
    # Stream rows keep file order; factory rows carry no seq.
    seqs = [r["seq"] for r in rows if r["seq"] is not None]
    assert seqs == sorted(seqs)
    # A factory command follows the tool call whose ordinal it names.
    for i, row in enumerate(rows):
        if row["kind"] == "factory" and row["after"] > 0:
            earlier = [
                r for r in rows[:i] if r["kind"] == "call" and r.get("ordinal") == row["after"]
            ]
            assert earlier, row


# ---------------------------------------------------------------------------
# The recorder must not write a trace with a lifecycle it opened but did not close
# (local review, 2026-11-23). The stream listener appends on a daemon thread, so
# the closing records can arrive after the scenario's last step.
# ---------------------------------------------------------------------------


def _lifecycle_module() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "robotics_replay_lifecycle", ROOT / "tools" / "replay_lifecycle.py"
    )
    assert spec and spec.loader, "tools/replay_lifecycle.py is missing"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stream_frames() -> List[Dict[str, Any]]:
    out = []
    for line in TRACE.read_text(encoding="utf-8").splitlines()[1:]:
        rec = json.loads(line)
        if rec.get("plane") == "stream":
            out.append(rec["frame"])
    return out


def test_the_committed_trace_has_no_unclosed_lifecycle() -> None:
    assert _lifecycle_module().unclosed(_stream_frames()) == []


@pytest.mark.parametrize("drop", ["TOOL_CALL_END", "SUBAGENT_FINISHED"])
def test_a_missing_closing_record_is_reported(drop: str) -> None:
    frames = _stream_frames()
    i = max(n for n, f in enumerate(frames) if f.get("type") == drop)
    del frames[i]
    left = _lifecycle_module().unclosed(frames)
    assert len(left) == 1 and drop in left[0]


def test_the_wait_returns_once_a_held_closing_record_is_appended() -> None:
    """A listener whose closing record is still in flight: the wait polls until the
    daemon-thread append lands, and only then returns."""
    import threading

    lc = _lifecycle_module()
    frames = _stream_frames()
    held = frames.pop(max(n for n, f in enumerate(frames) if f.get("type") == "TOOL_CALL_END"))
    live: List[Dict[str, Any]] = list(frames)
    seen_unclosed = threading.Event()

    def _deliver() -> None:
        assert seen_unclosed.wait(5)
        live.append(held)

    def _frames() -> List[Dict[str, Any]]:
        snapshot = list(live)
        if lc.unclosed(snapshot):
            seen_unclosed.set()
        return snapshot

    worker = threading.Thread(target=_deliver, daemon=True)
    worker.start()
    lc.wait_until_closed(_frames, 5.0, what="closing records")
    worker.join(5)
    assert lc.unclosed(list(live)) == []


def test_the_wait_fails_loudly_when_a_closing_record_never_arrives() -> None:
    lc = _lifecycle_module()
    frames = _stream_frames()
    del frames[max(n for n, f in enumerate(frames) if f.get("type") == "TOOL_CALL_END")]
    import threading

    outcome: Dict[str, Any] = {}

    def _run() -> None:
        try:
            lc.wait_until_closed(lambda: list(frames), 0.3, what="closing records")
            outcome["returned"] = True
        except TimeoutError as exc:
            outcome["error"] = str(exc)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(5)  # the bound is 0.3 s; a wait that ignores it fails here, not hangs
    assert not worker.is_alive(), "wait_until_closed ignored its bound"
    assert "TOOL_CALL_END" in outcome.get("error", ""), outcome


def test_kinds_limits_the_check_to_tool_calls_before_the_teardown() -> None:
    """Before the session is deleted every tool call is closed but the subagent runs
    are not: SUBAGENT_FINISHED only arrives after the delete. A tool-call-only check
    must therefore pass, and its wait must return at once, or every real recording
    would wait out its whole bound before the teardown."""
    import time

    lc = _lifecycle_module()
    frames = _stream_frames()
    before_teardown = [f for f in frames if f.get("type") != "SUBAGENT_FINISHED"]
    assert lc.unclosed(before_teardown), "fixture: subagent runs are still open here"
    assert lc.unclosed(before_teardown, ("TOOL_CALL_START",)) == []
    started = time.monotonic()
    lc.wait_until_closed(
        lambda: list(before_teardown), 5.0, what="closing records", kinds=("TOOL_CALL_START",)
    )
    assert time.monotonic() - started < 1.0


def test_the_recorder_waits_for_and_checks_closed_lifecycles_before_writing() -> None:
    """Read from source: the recorder imports mujoco, so it is not imported here."""
    src = RECORDER.read_text(encoding="utf-8")
    scenario = src[src.index("def scenario(") : src.index("\ndef ", src.index("def scenario("))]
    assert "let the stream deliver the closing frames" not in scenario, "a fixed sleep is no wait"
    calls = re.findall(r"_wait_closed\(d, ([^,]+(?:,\))?), ", scenario)
    assert calls == ['("TOOL_CALL_START",)', "tuple(lifecycle.PAIRS)"], (
        "the wait before the teardown must cover tool calls only (SUBAGENT_FINISHED "
        "arrives after the delete), and the wait after it every pair",
        calls,
    )
    assert scenario.index("_wait_closed(d, (") < scenario.index("requests.delete(")
    assert scenario.index("requests.delete(") < scenario.index("_wait_closed(d, tuple(")
    helper = src[src.index("def _wait_closed(") :]
    assert "lifecycle.wait_until_closed(" in helper[: helper.index("\ndef ")]
    main = src[src.index("def main(") :]
    start = main.index("left = lifecycle.unclosed(frames)")
    assert start < main.index("a.out.write_bytes("), "the check must run before the write"
    lines = [ln.strip() for ln in main[start:].splitlines()[:4]]
    assert (
        lines[1] == "if left:" and "return 2" in lines[2:4]
    ), "main must refuse (return 2) a trace with an open lifecycle before writing it"
