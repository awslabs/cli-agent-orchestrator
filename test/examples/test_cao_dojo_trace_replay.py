"""Tier 1 of the CAO Dojo drift check: offline, no server (#1116).

Every check runs against each available trace source (``offline`` always,
``published`` once recorded):

1. Every run-plane frame parses with the pinned ``ag-ui-protocol`` SDK model
   for its ``type``.
2. **Closed shape.** The SDK models accept extras (``extra="allow"``), so each
   frame's keys are compared with the model's aliases and any extra key not in
   ``tools/known_gaps.json`` fails. An entry that no longer matches anything
   fails too, so the table cannot rot. The snapshot interior is closed against
   CAO's own producer, ``build_dashboard_snapshot``.
3. **Lifecycle.** Each run starts ``RUN_STARTED`` with ``protocolVersion
   "1.0"``, ends with exactly one ``RUN_FINISHED`` or ``RUN_ERROR``, and has
   nothing after it.
4. **Cross-plane integrity** and the per-capture outcomes
   (``record_trace.assert_outcomes``).
5. ``_meta`` and the size budget.
6. **Stream frames against CAO's own mapper**: the card is a stock
   ``CustomEvent`` in the AG-UI 1.0 framing (``metadata.cao``,
   ``subagentRunId``, an integer ``timestamp``) whose ``value`` envelope and
   ``props`` are exactly what ``to_agui_event`` and ``on_provider_waiting``
   produce.
7. **The upstream corpus controls** run through the same lifecycle check:
   the accepted stream passes and the two fatal streams fail, so the check is
   proven able to fail.

The node lane (``ag-ui-stock-client-live/verify-trace.mjs``) repeats 1-3 with
upstream's own ``enforceEvents`` and ``verifyEvents``.
"""

from __future__ import annotations

import hashlib
import json
import typing
import warnings
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import pytest

# A direct import, not importorskip: a missing [agui] extra must fail loudly.
from ag_ui.core import events as ag_events

from cli_agent_orchestrator.services.agui.base import RecordingUiEmitter
from cli_agent_orchestrator.services.agui.handoff_approval import AgentHandoffWithApproval
from cli_agent_orchestrator.services.agui_stream import GENERATIVE_UI_COMPONENTS, to_agui_event
from cli_agent_orchestrator.services.ui_state_service import build_dashboard_snapshot

from .cao_dojo_trace_support import (
    CONFORMANCE_DIR,
    KNOWN_GAPS_FILE,
    PUBLISHED_TRACE,
    SHAPE_FILE,
    offline_trace,
    record_trace,
    source_ids,
)
from .cao_dojo_trace_support import trace_scrub as scrub
from .cao_dojo_trace_support import trace_sources

SOURCES = source_ids()


def _event_models() -> Dict[str, type]:
    models: Dict[str, type] = {}
    for name in dir(ag_events):
        model = getattr(ag_events, name)
        fields = getattr(model, "model_fields", None)
        if not name.endswith("Event") or not fields or "type" not in fields:
            continue
        for arg in typing.get_args(fields["type"].annotation):
            models[getattr(arg, "value", arg)] = model
    return models


EVENT_MODELS = _event_models()
OUTCOME_MODELS = {
    "success": ag_events.RunFinishedSuccessOutcome,
    "interrupt": ag_events.RunFinishedInterruptOutcome,
}


def aliases(model: type) -> Set[str]:
    return {f.alias or name for name, f in model.model_fields.items()}  # type: ignore[attr-defined]


def known_gaps() -> Dict[str, Dict[str, List[str]]]:
    return json.loads(KNOWN_GAPS_FILE.read_text(encoding="utf-8"))


def records_of(source: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    return scrub.parse(trace_sources()[source])


def skip_published_with_debt(source: str) -> None:
    """Skip the ``published`` case while the trace carries re-record debt.

    A keyless change (a new snapshot row key or card key, then ``--mock
    --write-shape``) makes the published trace stale until someone with Claude
    Code re-records it. PRs must not fail on that debt (PRs warn, the release
    path blocks), so the check runs on ``offline`` alone until the re-record.
    """
    if source != "published":
        return
    ok, message = record_trace.check_debt()
    if not ok:
        pytest.skip(f"published trace has re-record debt; the release path blocks on it: {message}")


def runs_of(records: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    runs: Dict[str, List[Dict[str, Any]]] = {}
    for record in records:
        if record.get("plane") == "run" and record.get("dir") == "out":
            runs.setdefault(record["run"], []).append(record["frame"])
    return runs


def frame_extras(frame: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Every (where, key) in ``frame`` that the SDK model does not describe."""
    model = EVENT_MODELS[frame["type"]]
    out = [(frame["type"], k) for k in sorted(set(frame) - aliases(model))]
    outcome = frame.get("outcome")
    if isinstance(outcome, dict) and outcome.get("type") in OUTCOME_MODELS:
        omodel = OUTCOME_MODELS[outcome["type"]]
        out += [(f"{frame['type']}.outcome", k) for k in sorted(set(outcome) - aliases(omodel))]
        for interrupt in outcome.get("interrupts") or []:
            extra = set(interrupt) - aliases(ag_events.Interrupt)
            out += [(f"{frame['type']}.outcome.interrupts[]", k) for k in sorted(extra)]
    return out


# --- Lifecycle (shared with the upstream controls) ------------------------------


def lifecycle_errors(frames: List[Dict[str, Any]], require_version: bool = True) -> List[str]:
    """The lifecycle rules, with upstream ``verifyEvents``' wording where it has one."""
    errors: List[str] = []
    if not frames:
        return ["empty run"]
    first = frames[0].get("type")
    if first not in ("RUN_STARTED", "RUN_ERROR"):
        errors.append(f"First event must be 'RUN_STARTED' (got {first})")
    active = False
    finished_at: Optional[int] = None
    for i, frame in enumerate(frames):
        ftype = frame.get("type")
        if finished_at is not None:
            errors.append(f"event {ftype} after the run finished")
            break
        if ftype == "RUN_STARTED":
            if active:
                errors.append("Cannot send 'RUN_STARTED' while a run is still active")
                break
            active = True
            if require_version and frame.get("protocolVersion") != "1.0":
                errors.append(f"RUN_STARTED protocolVersion {frame.get('protocolVersion')!r}")
        elif ftype in ("RUN_FINISHED", "RUN_ERROR"):
            active = False
            finished_at = i
    if finished_at is None and not errors:
        errors.append("the run never finished (no RUN_FINISHED or RUN_ERROR)")
    return errors


# --- 1. SDK models -------------------------------------------------------------


@pytest.mark.parametrize("source", SOURCES)
def test_every_run_plane_frame_parses_with_the_pinned_sdk_model(source):
    _, records = records_of(source)
    frames = [f for run in runs_of(records).values() for f in run]
    assert frames
    for frame in frames:
        assert frame["type"] in EVENT_MODELS, frame["type"]
        EVENT_MODELS[frame["type"]].model_validate(frame)


# --- 2. Closed shape -----------------------------------------------------------


@pytest.mark.parametrize("source", SOURCES)
def test_no_undescribed_key_outside_known_gaps(source):
    _, records = records_of(source)
    allowed = {
        (where, key) for gap in known_gaps().values() for where, keys in gap.items() for key in keys
    }
    for run in runs_of(records).values():
        for frame in run:
            for where, key in frame_extras(frame):
                assert (where, key) in allowed, (
                    f"undescribed key {key!r} on {where}: not described by the pinned SDK "
                    f"model and not in {KNOWN_GAPS_FILE.name}. If the run plane changed, "
                    f"re-record with `{record_trace.RECORD_COMMAND}`."
                )


def test_every_known_gap_is_still_observed():
    seen: Set[Tuple[str, str]] = set()
    for source in SOURCES:
        _, records = records_of(source)
        for run in runs_of(records).values():
            for frame in run:
                seen.update(frame_extras(frame))
    for gap_id, gap in known_gaps().items():
        for where, keys in gap.items():
            for key in keys:
                assert (where, key) in seen, (
                    f"KNOWN_GAPS {gap_id} no longer observed ({where}.{key}): remove it from "
                    f"{KNOWN_GAPS_FILE.name}"
                )


def test_frame_extras_still_reports_an_undescribed_key():
    """Control: with ``known_gaps.json`` empty, the gap tests above pass vacuously.

    This proves ``frame_extras`` still sees an extra key at the frame level and at
    the interrupt level, so an empty gap list means "no gaps", not "not looking".
    """
    snapshot = {"type": "STATE_SNAPSHOT", "snapshot": {}, "run_id": "none-run-1"}
    assert ("STATE_SNAPSHOT", "run_id") in frame_extras(snapshot)
    finished = {
        "type": "RUN_FINISHED",
        "threadId": "cao-dojo-trace",
        "runId": "approve-run-1",
        "outcome": {
            "type": "interrupt",
            "interrupts": [{"id": "id-1", "reason": "x", "undescribedKey": 1}],
        },
    }
    extras = frame_extras(finished)
    assert ("RUN_FINISHED.outcome.interrupts[]", "undescribedKey") in extras
    assert not [e for e in extras if e[1] != "undescribedKey"], extras


def _producer_snapshot() -> Dict[str, Any]:
    return build_dashboard_snapshot(
        [{"id": "s", "name": "s", "status": "detached"}],
        [
            {
                "id": "t",
                "tmux_session": "s",
                "provider": "p",
                "agent_profile": "a",
                "tmux_window": "w",
            }
        ],
        ["cao:read"],
    )


@pytest.mark.parametrize("source", SOURCES)
def test_the_snapshot_interior_is_the_producers(source):
    skip_published_with_debt(source)
    produced = _producer_snapshot()
    _, records = records_of(source)
    snapshots = [
        f["snapshot"]
        for run in runs_of(records).values()
        for f in run
        if f["type"] == "STATE_SNAPSHOT"
    ]
    assert snapshots
    for snapshot in snapshots:
        assert set(snapshot) == set(produced)
        assert set(snapshot["counts"]) == set(produced["counts"])
        for row in snapshot["sessions"]:
            assert set(row) == set(produced["sessions"][0])
        for row in snapshot["terminals"]:
            assert set(row) == set(produced["terminals"][0])


# --- 3. Lifecycle --------------------------------------------------------------


@pytest.mark.parametrize("source", SOURCES)
def test_every_run_is_lifecycle_legal(source):
    _, records = records_of(source)
    runs = runs_of(records)
    assert set(runs) == set(record_trace.RUN_IDS) - {scrub.run_id("none", 2)}
    for rid, frames in runs.items():
        assert lifecycle_errors(frames) == [], rid


# --- 4. Cross-plane integrity and outcomes -------------------------------------


@pytest.mark.parametrize("source", SOURCES)
def test_cross_plane_integrity_and_outcomes(source):
    _, records = records_of(source)
    record_trace.assert_outcomes(records)


# --- 5. _meta and size ---------------------------------------------------------


@pytest.mark.parametrize("source", SOURCES)
def test_meta(source):
    meta, _ = records_of(source)
    assert set(meta) <= scrub.META_KEYS
    assert meta["schema"] == scrub.SCHEMA
    assert meta["protocol_version"] == "1.0"
    assert meta["captures"] == list(scrub.CAPTURES)
    import importlib.metadata

    installed = importlib.metadata.version("ag-ui-protocol").split(".")[:2]
    recorded = meta["ag_ui_protocol"].split(".")[:2]
    if source == "published" and recorded != installed:
        # An SDK minor bump needs a re-record before release (trace/README.md);
        # until then a PR only warns.
        warnings.warn(
            f"published trace was recorded with ag-ui-protocol {meta['ag_ui_protocol']}, "
            f"installed is {'.'.join(installed)}: re-record before the next release"
        )
    else:
        assert recorded == installed
    assert len(meta["cao_build"]) == 40
    if source == "published":
        assert meta["mode"] == "live" and meta["provider"] == "claude_code"
        assert "shape_sha256" in meta


@pytest.mark.parametrize("source", SOURCES)
def test_size_budget(source):
    record_trace.check_budget(trace_sources()[source])


# --- 6. Stream frames against CAO's mapper -------------------------------------


def _mapper_value_keys() -> Set[str]:
    _, data = to_agui_event(
        {
            "id": "e",
            "kind": "other",
            "terminal_id": "t",
            "session_name": None,
            "timestamp": "2026-01-01T00:00:00+00:00",
            "detail": {"ui": {"component": "approval_card", "props": {}}},
        }
    )
    return set(data["value"])


def _approval_card_props_keys() -> Set[str]:
    emitter = RecordingUiEmitter()
    AgentHandoffWithApproval(emitter=emitter).on_provider_waiting("t", "mock_cli", "x")
    return set(emitter.intents[0]["props"])


CARD_ENVELOPE_KEYS = frozenset({"eventId", "terminalId", "sessionName", "traceparent"})


@pytest.mark.parametrize("source", SOURCES)
def test_stream_cards_match_cao_s_own_mapper(source):
    skip_published_with_debt(source)
    value_keys, props_keys = _mapper_value_keys(), _approval_card_props_keys()
    _, records = records_of(source)
    cards = [r["frame"] for r in records if r["plane"] == "stream"]
    assert len(cards) == 2
    for card in cards:
        ag_events.CustomEvent.model_validate(card)
        # AG-UI 1.0 framing (#1232): the record's envelope rides in metadata.cao and the
        # owning terminal in subagentRunId (ambient_run.AmbientRun._map / _envelope).
        assert set(card) == {"type", "name", "value", "timestamp", "metadata", "subagentRunId"}
        assert set(card["metadata"]) == {"cao"}
        assert {"eventId", "terminalId"} <= set(card["metadata"]["cao"]) <= CARD_ENVELOPE_KEYS
        assert isinstance(card["timestamp"], int)
        assert card["name"] == scrub.CAO_GENERATIVE_UI
        assert card["value"]["component"] in GENERATIVE_UI_COMPONENTS
        assert set(card["value"]) == value_keys
        assert set(card["value"]["props"]) == props_keys


# --- 7. Upstream corpus controls ------------------------------------------------


def _fixture(name: str) -> Dict[str, Any]:
    return json.loads((CONFORMANCE_DIR / f"{name}.json").read_text(encoding="utf-8"))


def test_the_vendored_fixtures_match_their_pin():
    pin = json.loads((CONFORMANCE_DIR / "PIN.json").read_text(encoding="utf-8"))
    assert pin["commit"] == "08eae9dc13546e91272efb2d0ef863e21acc6ee3"
    for name, entry in pin["files"].items():
        assert hashlib.sha256((CONFORMANCE_DIR / name).read_bytes()).hexdigest() == entry["sha256"]
    license_bytes = (CONFORMANCE_DIR / pin["license"]["file"]).read_bytes()
    assert hashlib.sha256(license_bytes).hexdigest() == pin["license"]["sha256"]
    assert license_bytes.startswith(b"MIT License")
    assert sorted(p.name for p in CONFORMANCE_DIR.glob("*.json") if p.name != "PIN.json") == sorted(
        pin["files"]
    )
    notice = (CONFORMANCE_DIR / "NOTICE").read_text(encoding="utf-8")
    assert pin["commit"] in notice and "MIT" in notice


# The full upstream corpus vendored for the run plane's own conformance tests.
RUN_PLANE_CORPUS = CONFORMANCE_DIR.parents[2] / "fixtures" / "agui_conformance_1_0"


def test_the_two_vendored_corpora_name_the_same_upstream_commit():
    """This corpus is a pinned subset of the run plane's; a refresh must move both."""
    pin = json.loads((CONFORMANCE_DIR / "PIN.json").read_text(encoding="utf-8"))
    upstream_md = (RUN_PLANE_CORPUS / "UPSTREAM.md").read_text(encoding="utf-8")
    assert (
        f"`{pin['commit']}`" in upstream_md
    ), f"PIN.json names {pin['commit']}, {RUN_PLANE_CORPUS.name}/UPSTREAM.md does not"
    for name in pin["files"]:
        theirs = RUN_PLANE_CORPUS / "streams" / name
        assert theirs.is_file(), f"{name} is not in {RUN_PLANE_CORPUS.name}/streams"
        assert (CONFORMANCE_DIR / name).read_bytes() == theirs.read_bytes(), name
    license_name = pin["license"]["file"]
    assert (CONFORMANCE_DIR / license_name).read_bytes() == (
        RUN_PLANE_CORPUS / "LICENSE.upstream"
    ).read_bytes()


def test_the_accepted_upstream_stream_passes_the_lifecycle_check():
    fixture = _fixture("run-finished-interrupt-outcome-accepted")
    assert fixture["expect"]["outcome"] == "completed"
    assert lifecycle_errors(fixture["stream"]) == []


@pytest.mark.parametrize("name", ["first-event-not-run-started-fatal", "nested-run-started-fatal"])
def test_the_fatal_upstream_streams_fail_the_lifecycle_check(name):
    fixture = _fixture(name)
    assert fixture["expect"]["outcome"] == "failed"
    errors = lifecycle_errors(fixture["stream"])
    assert any(fixture["expect"]["errorContains"] in e for e in errors), errors


# --- The offline tier-2 baseline, and the published-trace placeholder ----------


def test_the_offline_capture_projects_onto_the_committed_shape_file():
    """CAO's producers still emit the committed shape: drift caught at pre-push."""
    assert SHAPE_FILE.is_file(), f"{SHAPE_FILE} missing: run record_trace.py --mock --write-shape"
    committed = SHAPE_FILE.read_bytes()
    current = scrub.shape_bytes(offline_trace())
    assert current == committed, scrub.first_shape_difference(
        json.loads(committed), json.loads(current)
    )


def test_published_trace_status():
    """The published trace is committed, tier 1 reads it, and it parses as a trace.

    Staleness is not a failure here. A contributor who changes a frame on purpose
    regenerates the shape file keylessly; the trace is then stale until someone
    with Claude Code re-records it. PRs see that as a warning, and only the
    release path (``make check-agui-trace-release``) blocks on it.
    """
    assert PUBLISHED_TRACE.is_file(), f"{PUBLISHED_TRACE} is missing"
    assert "published" in SOURCES
    readme = (PUBLISHED_TRACE.parent / "README.md").read_text(encoding="utf-8")
    assert "NOT RECORDED YET" not in readme and "not recorded yet" not in readme
    ok, message = record_trace.check_debt()
    if not ok:
        assert "was recorded against shape" in message or "no longer projects" in message, message
        warnings.warn(message)
