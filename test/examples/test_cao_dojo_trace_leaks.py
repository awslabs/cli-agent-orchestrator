"""The CAO Dojo trace leak rule, mutation-tested (#1116).

Each case takes the base trace, injects exactly one leak, and asserts that
**that rule id** fires **at that path**. Deleting a rule therefore fails its own
case. The positive controls prove the rules are not written so wide that the
trace's own vocabulary trips them.

The base trace is the published ``--live`` trace once it is recorded, and the
offline capture until then (see ``cao_dojo_trace_support``).
"""

from __future__ import annotations

import copy
import getpass
import inspect
import json
import os
import re
import socket
from typing import Any, Callable, Dict, List, Tuple

import pytest

from .cao_dojo_trace_support import PUBLISHED_TRACE, offline_trace, record_trace, trace_leaks
from .cao_dojo_trace_support import trace_scrub as scrub

Records = List[Dict[str, Any]]


def base_trace() -> bytes:
    return PUBLISHED_TRACE.read_bytes() if PUBLISHED_TRACE.is_file() else offline_trace()


def split() -> Tuple[Dict[str, Any], Records]:
    meta, records = scrub.parse(base_trace())
    return copy.deepcopy(meta), copy.deepcopy(records)


def join(meta: Dict[str, Any], records: Records) -> bytes:
    lines = [scrub.dumps({"_meta": meta})] + [scrub.dumps(r) for r in records]
    return ("\n".join(lines) + "\n").encode("utf-8")


def find(records: Records, capture: str, predicate: Callable[[Dict[str, Any]], bool]) -> int:
    for i, record in enumerate(records):
        if record.get("capture") == capture and predicate(record):
            return i
    raise AssertionError(f"no {capture} record matched")


def frame_type(t: str) -> Callable[[Dict[str, Any]], bool]:
    return lambda r: (r.get("frame") or {}).get("type") == t


def is_card(r: Dict[str, Any]) -> bool:
    return r.get("plane") == "stream"


def is_request(r: Dict[str, Any]) -> bool:
    return r.get("dir") == "request"


def interrupt_finished(r: Dict[str, Any]) -> bool:
    frame = r.get("frame") or {}
    return frame.get("type") == "RUN_FINISHED" and (frame.get("outcome") or {}).get("type") == (
        "interrupt"
    )


def fired(data: bytes, rule: str, line: int, path: str) -> bool:
    return any(
        f.rule_id == rule and f.line == line and f.json_path == path
        for f in trace_leaks.check(data)
    )


def describe(data: bytes) -> str:
    return "\n".join(str(f) for f in trace_leaks.check(data)) or "(no findings)"


@pytest.fixture
def leak_user(monkeypatch):
    """A login name the L3 guard does not skip, whatever the machine's is."""
    name = getpass.getuser()
    if len(name) < trace_leaks.L3_MIN_LENGTH or name.lower() in trace_leaks.L3_STOPLIST:
        name = "plauzyx"
        monkeypatch.setattr(trace_leaks.getpass, "getuser", lambda: name)
    return name


@pytest.fixture
def leak_host(monkeypatch):
    """A hostname the L3 floor does not skip, whatever the machine's is."""
    name = socket.gethostname()
    if len(name.split(".")[0]) < trace_leaks.L3_MIN_LENGTH:
        name = "build-host-7"
        monkeypatch.setattr(trace_leaks.socket, "gethostname", lambda: name)
    return name.split(".")[0]


# --- The mutation cases (one or more per rule) --------------------------------


L1_PREFIXES = (
    "/Users/",
    "/home/",
    "/private/",
    "/tmp/",
    "/var/",
    "/Volumes/",
    "/opt/",
    "/root/",
    "/etc/",
    "/mnt/",
    "/srv/",
    "/run/",
    "/github/",
)


@pytest.mark.parametrize("prefix", L1_PREFIXES)
def test_l1_absolute_path(prefix):
    """One case per prefix, so narrowing L1's alternation fails its own case."""
    meta, records = split()
    i = find(records, "approve", is_card)
    records[i]["frame"]["value"]["props"]["reason"] = f"{prefix}x/approved-by-dojo.txt"
    data = join(meta, records)
    assert fired(data, "L1", i + 2, "$.frame.value.props.reason"), describe(data)


def test_l1_windows_drive_path():
    meta, records = split()
    i = find(records, "approve", is_card)
    records[i]["frame"]["value"]["props"]["reason"] = "C:\\work\\approved-by-dojo.txt"
    data = join(meta, records)
    assert fired(data, "L1", i + 2, "$.frame.value.props.reason"), describe(data)


def test_l2_home_directory():
    meta, records = split()
    i = find(records, "approve", is_card)
    records[i]["frame"]["value"]["timestamp"] = os.path.expanduser("~") + "/x"
    data = join(meta, records)
    assert fired(data, "L2", i + 2, "$.frame.value.timestamp"), describe(data)


def test_l3_hostname(leak_host):
    meta, records = split()
    i = find(records, "approve", frame_type("STATE_SNAPSHOT"))
    records[i]["frame"]["snapshot"]["sessions"][0]["name"] = leak_host
    data = join(meta, records)
    assert fired(data, "L3", i + 2, "$.frame.snapshot.sessions[0].name"), describe(data)


def test_l3_username(leak_user):
    meta, records = split()
    i = find(records, "approve", is_card)
    records[i]["frame"]["value"]["props"]["provider"] = leak_user
    data = join(meta, records)
    assert fired(data, "L3", i + 2, "$.frame.value.props.provider"), describe(data)


def test_l4_uuid():
    meta, records = split()
    i = find(records, "approve", is_card)
    records[i]["frame"]["value"]["event_id"] = "8d2a15cf-dfbc-4078-bdfe-c095495d781a"
    data = join(meta, records)
    assert fired(data, "L4", i + 2, "$.frame.value.event_id"), describe(data)


def test_l5_terminal_id():
    meta, records = split()
    i = find(records, "approve", interrupt_finished)
    records[i]["frame"]["outcome"]["interrupts"][0]["metadata"]["terminalId"] = "e327d8c9"
    data = join(meta, records)
    path = "$.frame.outcome.interrupts[0].metadata.terminalId"
    assert fired(data, "L5", i + 2, path), describe(data)


def test_l5_scoped_rule_refuses_a_non_hex_terminal_id():
    """``worker-a`` is not hex, so only the scoped L5 (a terminal key) can refuse it."""
    meta, records = split()
    i = find(records, "approve", interrupt_finished)
    records[i]["frame"]["outcome"]["interrupts"][0]["metadata"]["terminalId"] = "worker-a"
    data = join(meta, records)
    path = "$.frame.outcome.interrupts[0].metadata.terminalId"
    assert fired(data, "L5", i + 2, path), describe(data)


@pytest.mark.parametrize(
    "value",
    [
        "8d2a15cfdfbc4078bdfec095495d781a",  # uuid4().hex, no dashes
        "0123456789abcdef",  # a 16-hex span id
        "1267fa1b50ccabfdaf6f3e1b173a105a71648d36",  # a 40-hex git sha
        "E327D8C9",  # uppercase 8-hex
        "foo_e327d8c9",  # glued to a word by an underscore
        "session_e327d8c9",  # a prefixed terminal id
        "x0123456789abcdef",  # glued to a non-hex letter
    ],
    ids=[
        "dashless-uuid",
        "16-hex",
        "40-hex",
        "uppercase-8-hex",
        "underscore-prefixed",
        "session-prefixed",
        "letter-prefixed",
    ],
)
def test_l5_global_hex_rule_catches_long_and_uppercase_hex(value):
    """A raw id under a key no typed rule knows: only the global hex rule sees it."""
    meta, records = split()
    i = find(records, "approve", is_card)
    records[i]["frame"]["value"]["props"]["reason"] = f"mock-cli:{value}"
    data = join(meta, records)
    assert fired(data, "L5", i + 2, "$.frame.value.props.reason"), describe(data)


def test_l5_global_hex_rule_covers_meta_outside_the_build_and_shape_hashes():
    meta, records = split()
    meta["provider_version"] = "2.1.318 (8d2a15cfdfbc4078bdfec095495d781a)"
    data = join(meta, records)
    assert fired(data, "L5", 1, "$._meta.provider_version"), describe(data)


def test_l4_uuid_in_the_1_0_event_id():
    meta, records = split()
    i = find(records, "approve", is_card)
    records[i]["frame"].setdefault("metadata", {}).setdefault("cao", {})
    records[i]["frame"]["metadata"]["cao"]["eventId"] = "8d2a15cf-dfbc-4078-bdfe-c095495d781a"
    data = join(meta, records)
    assert fired(data, "L4", i + 2, "$.frame.metadata.cao.eventId"), describe(data)


def test_l5_terminal_id_in_the_1_0_subagent_run_id():
    meta, records = split()
    i = find(records, "approve", interrupt_finished)
    records[i]["frame"]["outcome"]["interrupts"][0]["subagentRunId"] = "terminal:e327d8c9"
    data = join(meta, records)
    path = "$.frame.outcome.interrupts[0].subagentRunId"
    assert fired(data, "L5", i + 2, path), describe(data)


SESSION_ATTRIBUTION = "session:0123456789abcdef"  # attribution.subagent_run_id_for_session's shape


def test_l14_unscrubbed_session_attribution_on_subagent_run_id():
    """A raw ``session:<16 hex>`` under an id key fails L14 by key, whatever its form."""
    meta, records = split()
    i = find(records, "approve", interrupt_finished)
    records[i]["frame"]["outcome"]["interrupts"][0]["subagentRunId"] = SESSION_ATTRIBUTION
    data = join(meta, records)
    path = "$.frame.outcome.interrupts[0].subagentRunId"
    assert fired(data, "L14", i + 2, path), describe(data)


def test_l14_unscrubbed_session_attribution_on_parent_subagent_run_id():
    meta, records = split()
    i = find(records, "approve", is_card)
    records[i]["frame"]["parentSubagentRunId"] = SESSION_ATTRIBUTION
    data = join(meta, records)
    assert fired(data, "L14", i + 2, "$.frame.parentSubagentRunId"), describe(data)


def test_l14_untokenised_traceparent():
    meta, records = split()
    i = find(records, "approve", is_card)
    records[i]["frame"]["metadata"]["cao"]["traceparent"] = "00-trace-span-01"
    data = join(meta, records)
    assert fired(data, "L14", i + 2, "$.frame.metadata.cao.traceparent"), describe(data)


def test_l14_untokenised_interrupt_id():
    meta, records = split()
    i = find(records, "approve", interrupt_finished)
    records[i]["frame"]["outcome"]["interrupts"][0]["id"] = "approval-7"
    data = join(meta, records)
    assert fired(data, "L14", i + 2, "$.frame.outcome.interrupts[0].id"), describe(data)


def test_l6_session_name():
    meta, records = split()
    i = find(records, "approve", frame_type("STATE_SNAPSHOT"))
    records[i]["frame"]["snapshot"]["terminals"][0]["session_name"] = "cao-p2probe"
    data = join(meta, records)
    path = "$.frame.snapshot.terminals[0].session_name"
    assert fired(data, "L6", i + 2, path), describe(data)


def test_l7_profile():
    meta, records = split()
    i = find(records, "approve", frame_type("STATE_SNAPSHOT"))
    records[i]["frame"]["snapshot"]["terminals"][0]["agent_profile"] = "dojo-approver"
    data = join(meta, records)
    path = "$.frame.snapshot.terminals[0].agent_profile"
    assert fired(data, "L7", i + 2, path), describe(data)


@pytest.mark.parametrize("window", ["developer-2adf", "developer-12345"])
def test_l8_window(window):
    meta, records = split()
    i = find(records, "approve", frame_type("STATE_SNAPSHOT"))
    records[i]["frame"]["snapshot"]["terminals"][0]["window"] = window
    data = join(meta, records)
    path = "$.frame.snapshot.terminals[0].window"
    assert fired(data, "L8", i + 2, path), describe(data)


def _appended(records: Records, frame: Dict[str, Any]) -> Records:
    return records + [
        {
            "capture": "approve",
            "seq": 99,
            "plane": "run",
            "run": scrub.run_id("approve", 1),
            "dir": "out",
            "frame": frame,
        }
    ]


def test_l9_raw_frame():
    meta, records = split()
    records = _appended(records, {"type": "RAW", "event": {}})
    data = join(meta, records)
    assert fired(data, "L9", len(records) + 1, "$.frame.type"), describe(data)


def test_l9_rewrapped_custom():
    meta, records = split()
    records = _appended(records, {"type": "CUSTOM", "name": "cao.raw", "value": {}})
    data = join(meta, records)
    assert fired(data, "L9", len(records) + 1, "$.frame.name"), describe(data)


def test_l10_file_mod_delta():
    """The real trace never exercises this path, so it is proven only here."""
    meta, records = split()
    records = _appended(
        records,
        {
            "type": "STATE_DELTA",
            "delta": [{"op": "add", "path": "/last_file_mod", "value": {"path": "/Users/x/f.py"}}],
            "metadata": {"path": "/Users/x/f.py"},
        },
    )
    data = join(meta, records)
    line = len(records) + 1
    assert fired(data, "L10", line, "$.frame.delta[0].path"), describe(data)
    assert fired(data, "L10", line, "$.frame.metadata"), describe(data)


def test_l11_prompt_body():
    meta, records = split()
    i = find(records, "approve", interrupt_finished)
    records[i]["frame"]["outcome"]["interrupts"][0][
        "message"
    ] = "Allow Write to approved-by-dojo.txt?"
    data = join(meta, records)
    path = "$.frame.outcome.interrupts[0].message"
    assert fired(data, "L11", i + 2, path), describe(data)


def test_l12_request_carries_messages():
    meta, records = split()
    i = find(records, "approve", is_request)
    records[i]["request"]["messages"] = [{"role": "user", "content": "hi"}]
    data = join(meta, records)
    assert fired(data, "L12", i + 2, "$.request.messages"), describe(data)


def test_l13_unknown_record_key():
    meta, records = split()
    i = find(records, "approve", is_card)
    records[i]["note"] = "x"
    data = join(meta, records)
    assert fired(data, "L13", i + 2, "$.note"), describe(data)


# --- The L3 guard --------------------------------------------------------------


@pytest.mark.parametrize("login", ["ci", "approve"])
def test_l3_guard_skips_short_and_stoplisted_logins(monkeypatch, login):
    monkeypatch.setattr(trace_leaks.getpass, "getuser", lambda: login)
    assert [f for f in trace_leaks.check(base_trace()) if f.rule_id == "L3"] == []


def test_l3_guard_still_fires_on_a_real_login(monkeypatch):
    monkeypatch.setattr(trace_leaks.getpass, "getuser", lambda: "plauzyx")
    meta, records = split()
    i = find(records, "approve", is_card)
    records[i]["frame"]["value"]["props"]["provider"] = "plauzyx"
    data = join(meta, records)
    assert fired(data, "L3", i + 2, "$.frame.value.props.provider"), describe(data)


@pytest.mark.parametrize("host", ["card", "card.local"])
def test_l3_guard_skips_a_hostname_inside_trace_vocabulary(monkeypatch, host):
    """A host named like part of a word (`approval_card`) is not a leak."""
    monkeypatch.setattr(trace_leaks.socket, "gethostname", lambda: host)
    assert [f for f in trace_leaks.check(base_trace()) if f.rule_id == "L3"] == []


def test_l3_guard_still_fires_on_a_whole_word_hostname(monkeypatch):
    monkeypatch.setattr(trace_leaks.socket, "gethostname", lambda: "card.local")
    meta, records = split()
    i = find(records, "approve", frame_type("STATE_SNAPSHOT"))
    records[i]["frame"]["snapshot"]["sessions"][0]["name"] = "card"
    data = join(meta, records)
    assert fired(data, "L3", i + 2, "$.frame.snapshot.sessions[0].name"), describe(data)


# --- Every rule id has a case ---------------------------------------------------


def test_scrubber_id_keys_are_all_l14_checked():
    assert scrub.ID_KEYS <= trace_leaks.L14_KEYS


# Every rule id the checker can report, read from its source, so a new rule needs
# its own cases without anyone remembering to widen a range here.
RULE_IDS = sorted(
    set(re.findall(r'add\(\s*"(L\d+)"', inspect.getsource(trace_leaks))),
    key=lambda r: int(r[1:]),
)


def test_the_rule_ids_are_read_from_the_checker():
    assert RULE_IDS[0] == "L1" and len(RULE_IDS) >= 14, RULE_IDS


def test_every_rule_id_has_a_mutation_case():
    names = {name for name in globals() if name.startswith("test_l")}
    for rule in RULE_IDS:
        prefix = f"test_{rule.lower()}_"
        assert any(name.startswith(prefix) for name in names), f"{rule} has no case"


# --- Near misses: one per rule, at the same site as its mutation case ---------
#
# Each value sits just outside its rule, so a rule widened to over-match fails
# its own case here rather than only the aggregate clean-trace controls.


def _near_l1(meta: Dict[str, Any], records: Records) -> None:
    i = find(records, "approve", is_card)
    records[i]["frame"]["value"]["props"]["reason"] = "work/Users/x/approved-by-dojo.txt"


def _near_l2(meta: Dict[str, Any], records: Records) -> None:
    i = find(records, "approve", is_card)
    records[i]["frame"]["value"]["timestamp"] = os.path.expanduser("~").lstrip("/") + "/x"


def _near_l3(meta: Dict[str, Any], records: Records) -> None:
    i = find(records, "approve", is_card)
    records[i]["frame"]["value"]["props"]["provider"] = "plauzyx_build"


def _near_l4(meta: Dict[str, Any], records: Records) -> None:
    i = find(records, "approve", is_card)
    records[i]["frame"]["value"]["props"]["reason"] = "mock-cli:8d2a15cf-dfbc-4078-bdfe-c095495d781"


def _near_l5(meta: Dict[str, Any], records: Records) -> None:
    i = find(records, "approve", is_card)
    records[i]["frame"]["value"]["props"]["reason"] = "mock-cli:e327d8c"
    j = find(records, "approve", interrupt_finished)
    records[j]["frame"]["outcome"]["interrupts"][0]["metadata"]["terminalId"] = "terminal-3"


def _near_l6(meta: Dict[str, Any], records: Records) -> None:
    i = find(records, "approve", frame_type("STATE_SNAPSHOT"))
    records[i]["frame"]["snapshot"]["terminals"][0]["session_name"] = "session-2"


def _near_l7(meta: Dict[str, Any], records: Records) -> None:
    i = find(records, "approve", frame_type("STATE_SNAPSHOT"))
    records[i]["frame"]["snapshot"]["terminals"][0]["agent_profile"] = "profile-1"


def _near_l8(meta: Dict[str, Any], records: Records) -> None:
    i = find(records, "approve", frame_type("STATE_SNAPSHOT"))
    records[i]["frame"]["snapshot"]["terminals"][0]["window"] = "window-1234"


def _near_l9(meta: Dict[str, Any], records: Records) -> None:
    records[:] = _appended(
        records, {"type": "CUSTOM", "name": scrub.CAO_GENERATIVE_UI, "value": {}}
    )


def _near_l10(meta: Dict[str, Any], records: Records) -> None:
    records[:] = _appended(
        records,
        {"type": "STATE_DELTA", "delta": [{"op": "add", "path": "/terminals", "value": []}]},
    )


def _near_l11(meta: Dict[str, Any], records: Records) -> None:
    i = find(records, "approve", interrupt_finished)
    records[i]["frame"]["outcome"]["interrupts"][0]["message"] = ""


def _near_l12(meta: Dict[str, Any], records: Records) -> None:
    i = find(records, "approve", is_request)
    records[i]["request"]["resume"] = [
        {"interruptId": "id-1", "status": "resolved", "payload": {"approved": True}}
    ]


def _near_l13(meta: Dict[str, Any], records: Records) -> None:
    records[:] = _appended(records, {"type": "STATE_DELTA", "delta": []})


def _near_l14(meta: Dict[str, Any], records: Records) -> None:
    i = find(records, "approve", is_card)
    records[i]["frame"]["value"]["event_id"] = "id-7"


NEAR_MISSES: Dict[str, Callable[[Dict[str, Any], Records], None]] = {
    name[len("_near_") :].upper(): fn
    for name, fn in globals().items()
    if name.startswith("_near_l")
}


@pytest.mark.parametrize("rule", sorted(NEAR_MISSES, key=lambda r: int(r[1:])))
def test_a_near_miss_does_not_fire_its_rule(rule, monkeypatch):
    """Pins each rule's edge from the clean side, at the site its mutation case uses."""
    monkeypatch.setattr(trace_leaks.getpass, "getuser", lambda: "plauzyx")
    meta, records = split()
    NEAR_MISSES[rule](meta, records)
    data = join(meta, records)
    assert [str(f) for f in trace_leaks.check(data) if f.rule_id == rule] == []


def test_every_rule_id_has_a_near_miss():
    assert set(NEAR_MISSES) == set(RULE_IDS)


# --- Positive controls ---------------------------------------------------------


def test_the_base_trace_is_clean():
    assert trace_leaks.check(base_trace()) == [], describe(base_trace())


def test_a_legitimate_state_delta_is_clean():
    meta, records = split()
    records = _appended(
        records,
        {
            "type": "STATE_DELTA",
            "delta": [{"op": "replace", "path": "/counts/terminals", "value": 1}],
        },
    )
    data = join(meta, records)
    assert trace_leaks.check(data) == [], describe(data)


def test_a_state_delta_is_held_to_the_snapshot_rules():
    """A `/terminals` patch cannot carry what a snapshot may not."""
    meta, records = split()
    records = _appended(
        records,
        {
            "type": "STATE_DELTA",
            "delta": [{"op": "replace", "path": "/terminals", "value": [{"id": "e327d8c9"}]}],
        },
    )
    data = join(meta, records)
    assert fired(data, "L5", len(records) + 1, "$.frame.delta[0].value[0].id"), describe(data)


@pytest.mark.parametrize(
    "pointer, value, rule, path",
    [
        ("/terminals", [{"id": "worker-1"}], "L5", "$.frame.delta[0].value[0].id"),
        ("/sessions", [{"name": "my-project"}], "L6", "$.frame.delta[0].value[0].name"),
    ],
    ids=["terminals-L5", "sessions-L6"],
)
def test_a_state_delta_value_only_the_scoped_rules_refuse(pointer, value, rule, path):
    """Non-hex values, so only the STATE_DELTA-to-snapshot mapping can make these fire."""
    meta, records = split()
    records = _appended(
        records, {"type": "STATE_DELTA", "delta": [{"op": "add", "path": pointer, "value": value}]}
    )
    data = join(meta, records)
    assert fired(data, rule, len(records) + 1, path), describe(data)


@pytest.mark.parametrize("token", ["window-100", "window-1234"])
def test_a_scrubber_window_token_is_not_an_l8_finding(token):
    meta, records = split()
    i = find(records, "approve", frame_type("STATE_SNAPSHOT"))
    records[i]["frame"]["snapshot"]["terminals"][0]["window"] = token
    data = join(meta, records)
    assert [f for f in trace_leaks.check(data) if f.rule_id == "L8"] == [], describe(data)


def test_the_recorders_fixed_constants_are_clean():
    """Catches a global rule written too wide, such as a `cao-` check on threadId."""
    clock = [scrub.CLOCK_EPOCH.isoformat(timespec="microseconds")]
    meta = {"schema": record_trace.SCHEMA, "captures": list(record_trace.CAPTURES)}
    records: Records = []
    for rid in record_trace.RUN_IDS:
        capture = rid.split("-run-")[0]
        records.append(
            {
                "capture": capture,
                "seq": len(records) + 1,
                "plane": "run",
                "run": rid,
                "dir": "out",
                "frame": {
                    "type": "RUN_STARTED",
                    "threadId": record_trace.THREAD_ID,
                    "runId": rid,
                    "protocolVersion": "1.0",
                },
            }
        )
    records.append(
        {
            "capture": "approve",
            "seq": 1,
            "plane": "stream",
            "dir": "out",
            "frame": {
                "type": "CUSTOM",
                "name": scrub.CAO_GENERATIVE_UI,
                "value": {
                    "event_id": "id-1",
                    "terminal_id": "terminal-1",
                    "session_name": "session-1",
                    "timestamp": clock[0],
                    "traceparent": "id-2",
                    "component": "approval_card",
                    "props": {"interrupt_id": "id-3", "terminal_id": "terminal-1", "message": ""},
                },
            },
        }
    )
    records.append(
        {
            "capture": "approve",
            "seq": 2,
            "plane": "run",
            "run": scrub.run_id("approve", 1),
            "dir": "out",
            "frame": {
                "type": "STATE_SNAPSHOT",
                "snapshot": {
                    "sessions": [{"id": "session-1", "name": "session-1", "status": "detached"}],
                    "terminals": [
                        {
                            "id": "terminal-1",
                            "session_name": "session-1",
                            "agent_profile": "profile-1",
                            "window": "window-1",
                            "last_active": clock[0],
                        }
                    ],
                },
            },
        }
    )
    data = join(meta, records)
    assert trace_leaks.check(data) == [], describe(data)


def test_findings_name_the_fix():
    finding = trace_leaks.Finding("L5", 6, "$.frame.x", "e327d8c9")
    text = trace_leaks.format_findings([finding])
    assert "L5 at line 6 $.frame.x" in text
    assert "trace_scrub.py" in text and "re-record" in text


def test_the_published_trace_bytes_are_canonical():
    """A hand edit that reorders keys or adds whitespace is not a valid trace."""
    data = base_trace()
    meta, records = scrub.parse(data)
    assert join(meta, records) == data
    assert json.loads(data.decode().split("\n")[0])["_meta"]["schema"] == scrub.SCHEMA
