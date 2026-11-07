"""The node lane's known-gap rule matches the Python lane's: a union across traces (#1116).

``verify-trace.mjs`` fails a ``known_gaps.json`` entry only when no trace it was
given observes the gap, the same rule ``test_every_known_gap_is_still_observed``
applies. CI passes the fresh mock recording and the published trace together, so
a per-file rule would fail a PR whenever the two differ on a gap while the
published trace is stale.

Two layers:

* The accounting itself (``known-gaps.mjs``) needs only ``node``.
* The end-to-end cases run ``verify-trace.mjs`` on real trace files, which needs
  the pinned ``@ag-ui/client`` (``npm ci`` in ``ag-ui-stock-client-live``); they
  skip without it, except where ``CAO_DOJO_REQUIRE_NODE_LANE=1`` (CI's AC3 job)
  turns a missing dependency into a failure rather than a silent skip.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

from .cao_dojo_trace_support import PUBLISHED_TRACE

NODE_DIR = Path(__file__).resolve().parents[2] / "examples" / "ag-ui" / "ag-ui-stock-client-live"
KNOWN_GAPS_MJS = NODE_DIR / "known-gaps.mjs"
VERIFY_TRACE = NODE_DIR / "verify-trace.mjs"
NODE = shutil.which("node")

REQUIRED = os.environ.get("CAO_DOJO_REQUIRE_NODE_LANE") == "1"
HAS_AG_UI_CLIENT = (NODE_DIR / "node_modules" / "@ag-ui" / "client").is_dir()

needs_node = pytest.mark.skipif(NODE is None and not REQUIRED, reason="needs node")
needs_ag_ui_client = pytest.mark.skipif(
    (NODE is None or not HAS_AG_UI_CLIENT) and not REQUIRED,
    reason="needs node and `npm ci` in examples/ag-ui/ag-ui-stock-client-live",
)


def test_the_node_lane_dependencies_are_present_where_required():
    """In AC3 a missing node or @ag-ui/client fails here instead of skipping the lane."""
    if not REQUIRED:
        pytest.skip("CAO_DOJO_REQUIRE_NODE_LANE is not set")
    assert NODE is not None, "node is not on PATH"
    assert HAS_AG_UI_CLIENT, "run `npm ci` in examples/ag-ui/ag-ui-stock-client-live first"


PROBE_GAP = {"G-probe": {"RUN_FINISHED": ["caoProbe"]}}
PROBE_WARNING = "[ag-ui][enforce] Removed unrecognised material at '/caoProbe' on RUN_FINISHED."


# --- The accounting (node only) ------------------------------------------------

_LEDGER_SCRIPT = """
import { GapLedger, allowedWarnings } from %s;
const [gaps, perTrace] = JSON.parse(process.argv[1]);
const ledger = new GapLedger(allowedWarnings(gaps));
const unlisted = perTrace.map((warnings) => ledger.record(warnings));
console.log(JSON.stringify({ unlisted, unobserved: ledger.unobserved().map(([, id]) => id) }));
"""


def _ledger(gaps: Dict[str, Any], per_trace: List[List[str]]) -> Dict[str, Any]:
    script = _LEDGER_SCRIPT % json.dumps(KNOWN_GAPS_MJS.as_uri())
    result = subprocess.run(
        [str(NODE), "--input-type=module", "-e", script, json.dumps([gaps, per_trace])],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(result.stdout)


@needs_node
def test_a_gap_observed_in_one_trace_only_is_observed():
    out = _ledger(PROBE_GAP, [[PROBE_WARNING], []])
    assert out == {"unlisted": [[], []], "unobserved": []}


@needs_node
def test_a_gap_observed_in_no_trace_is_reported():
    assert _ledger(PROBE_GAP, [[], []])["unobserved"] == ["G-probe"]


@needs_node
def test_an_unlisted_warning_is_returned_for_its_own_trace():
    out = _ledger({}, [[], [PROBE_WARNING]])
    assert out == {"unlisted": [[], [PROBE_WARNING]], "unobserved": []}


# --- verify-trace.mjs end to end (node and @ag-ui/client) ------------------------


def _with_probe(source: Path, dest: Path) -> Path:
    """``source`` with ``caoProbe: true`` on every RUN_FINISHED success frame."""
    lines = source.read_text(encoding="utf-8").splitlines()
    out = [lines[0]]
    for line in lines[1:]:
        record = json.loads(line)
        frame = record.get("frame") or {}
        if (
            frame.get("type") == "RUN_FINISHED"
            and (frame.get("outcome") or {}).get("type") == "success"
        ):
            frame["caoProbe"] = True
        out.append(json.dumps(record, separators=(",", ":"), sort_keys=True))
    dest.write_text("\n".join(out) + "\n", encoding="utf-8")
    return dest


def _verify(tmp_path: Path, gaps: Dict[str, Any], *traces: Path) -> subprocess.CompletedProcess:
    gaps_file = tmp_path / "known_gaps.json"
    gaps_file.write_text(json.dumps(gaps), encoding="utf-8")
    return subprocess.run(
        [str(NODE), str(VERIFY_TRACE), *map(str, traces)],
        env={"PATH": str(Path(str(NODE)).parent), "CAO_DOJO_KNOWN_GAPS": str(gaps_file)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.fixture
def published(tmp_path: Path) -> Path:
    if not PUBLISHED_TRACE.is_file():
        pytest.skip("no published trace")
    return Path(shutil.copy(PUBLISHED_TRACE, tmp_path / "published.ndjson"))


# Both file orders: a check that kept only the first or only the last trace's
# warnings would pass one order and fail the other.
BOTH_ORDERS = pytest.mark.parametrize(
    "gap_file_first", [True, False], ids=["gap-first", "gap-last"]
)


def _ordered(gap_file: Path, other: Path, gap_file_first: bool) -> List[Path]:
    return [gap_file, other] if gap_file_first else [other, gap_file]


@needs_ag_ui_client
@BOTH_ORDERS
def test_a_gap_in_the_fresh_recording_only_passes(tmp_path, published, gap_file_first):
    """A keyless change adds a listed key; the stale published trace lacks it."""
    fresh = _with_probe(published, tmp_path / "fresh.ndjson")
    result = _verify(tmp_path, PROBE_GAP, *_ordered(fresh, published, gap_file_first))
    assert result.returncode == 0, result.stdout
    assert "[verify-trace] PASS" in result.stdout


@needs_ag_ui_client
@BOTH_ORDERS
def test_a_gap_in_the_stale_published_trace_only_passes_while_its_entry_is_kept(
    tmp_path, published, gap_file_first
):
    """The reverse: a keyless change removes the key, the published trace still has it."""
    stale = _with_probe(published, tmp_path / "stale-published.ndjson")
    result = _verify(tmp_path, PROBE_GAP, *_ordered(stale, published, gap_file_first))
    assert result.returncode == 0, result.stdout


@needs_ag_ui_client
def test_dropping_the_entry_before_the_re_record_fails_on_the_published_trace(tmp_path, published):
    """Why the entry stays until the re-record: the stale trace still carries the key."""
    stale = _with_probe(published, tmp_path / "stale-published.ndjson")
    result = _verify(tmp_path, {}, published, stale)
    assert result.returncode == 1
    assert PROBE_WARNING in result.stdout


@needs_ag_ui_client
def test_a_gap_observed_in_no_trace_fails(tmp_path, published):
    fresh = Path(shutil.copy(published, tmp_path / "fresh.ndjson"))
    result = _verify(tmp_path, PROBE_GAP, fresh, published)
    assert result.returncode == 1
    assert "KNOWN_GAPS G-probe not observed in any trace" in result.stdout
