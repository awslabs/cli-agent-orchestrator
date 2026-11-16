"""The node lane's known-gap rule matches the Python lane's: a union across traces (#1116).

``verify-trace.mjs`` fails a ``known_gaps.json`` entry only when no trace it was
given observes the gap, the same rule ``test_every_known_gap_is_still_observed``
applies. CI passes the fresh mock recording and the published trace together, so
a per-file rule would fail a PR whenever the two differ on a gap while the
published trace is stale.

Two layers:

* The accounting itself (``known-gaps.mjs``) needs only ``node``.
* The end-to-end cases run ``verify-trace.mjs`` on real trace files, which needs
  the pinned ``@ag-ui/client`` (``npm ci`` in ``ag-ui-stock-client-live``) and the
  published trace; they skip without them.

Every test here is ``must_run("agui-node")``: in CI's AC3 job, which sets
``CAO_MUST_RUN: agui-node``, any of those skips fails the session instead of
passing silently (``test/_must_run.py``).
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

HAS_AG_UI_CLIENT = (NODE_DIR / "node_modules" / "@ag-ui" / "client").is_dir()

pytestmark = pytest.mark.must_run("agui-node")

needs_node = pytest.mark.skipif(NODE is None, reason="needs node")
needs_ag_ui_client = pytest.mark.skipif(
    NODE is None or not HAS_AG_UI_CLIENT,
    reason="needs node and `npm ci` in examples/ag-ui/ag-ui-stock-client-live",
)

PROBE_GAP = {"G-probe": {"RUN_FINISHED": ["caoProbe"]}}
PROBE_WARNING = "[ag-ui][enforce] Removed unrecognised material at '/caoProbe' on RUN_FINISHED."
OTHER_WARNING = "[ag-ui][enforce] Removed unrecognised material at '/caoOther' on RUN_FINISHED."


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


@needs_node
def test_an_unlisted_warning_is_returned_next_to_an_observed_listed_gap():
    """Observing a listed gap must not hide an unlisted warning, in that trace or another."""
    out = _ledger(PROBE_GAP, [[PROBE_WARNING, OTHER_WARNING], [OTHER_WARNING]])
    assert out == {"unlisted": [[OTHER_WARNING], [OTHER_WARNING]], "unobserved": []}


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
        # The caller's environment, so a script-shim `node` (nodenv, asdf) still finds
        # its interpreter; the suite's conftest already isolates HOME.
        env={**os.environ, "CAO_DOJO_KNOWN_GAPS": str(gaps_file)},
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


def _why(result: subprocess.CompletedProcess) -> str:
    return f"rc={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"


@needs_ag_ui_client
@BOTH_ORDERS
def test_a_gap_in_only_one_of_two_traces_passes(tmp_path, published, gap_file_first):
    """A listed gap that one trace carries and the other lacks passes, in either order.

    This is both scenarios the union exists for: a keyless change adds a listed key,
    so the fresh recording has it and the stale published trace does not; or a
    keyless change removes one, so only the stale published trace still has it.
    ``verify-trace.mjs`` does not know which file is which, so at this level the
    two are the same input, and the file order is what varies.
    """
    with_gap = _with_probe(published, tmp_path / "with-gap.ndjson")
    result = _verify(tmp_path, PROBE_GAP, *_ordered(with_gap, published, gap_file_first))
    assert result.returncode == 0, _why(result)
    assert "[verify-trace] PASS" in result.stdout, _why(result)


@needs_ag_ui_client
def test_dropping_the_entry_before_the_re_record_fails_on_the_published_trace(tmp_path, published):
    """Why the entry stays until the re-record: the stale trace still carries the key."""
    stale = _with_probe(published, tmp_path / "stale-published.ndjson")
    result = _verify(tmp_path, {}, published, stale)
    assert result.returncode == 1, _why(result)
    assert PROBE_WARNING in result.stdout, _why(result)


@needs_ag_ui_client
def test_a_gap_observed_in_no_trace_fails(tmp_path, published):
    fresh = Path(shutil.copy(published, tmp_path / "fresh.ndjson"))
    result = _verify(tmp_path, PROBE_GAP, fresh, published)
    assert result.returncode == 1, _why(result)
    assert "KNOWN_GAPS G-probe not observed in any trace" in result.stdout, _why(result)
