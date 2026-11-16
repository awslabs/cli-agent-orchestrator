"""Shared loading for the CAO Dojo trace tests (#1116).

The tools live in ``examples/ag-ui/cao-dojo/tools`` (the generated agent-plugin
package may hold no scripts), so they are imported by path.

**Trace sources.** Tier 1 runs every check against each available source:

* ``offline``: the three captures produced in-process by CAO's own producers
  (``record_trace.capture_offline``). Always available, so no check here can
  pass by having nothing to look at.
* ``published``: the committed ``--live`` trace
  (``trace/cao-dojo-trace.ndjson``). ``test_published_trace_status`` requires
  it to exist; staleness only warns.

What an offline pass cannot prove: offline run 2 never goes through the
approval bridge, so the run-2 race check is trivially clean there; the
snapshot-interior and card-mapper checks compare CAO's producers with
themselves, so they are tautological offline and bite only on the published
trace; and the HTTP route is not exercised at all, which is tier 2's job
(``record_trace.py --mock --check``).
"""

from __future__ import annotations

import functools
import sys
from pathlib import Path
from typing import Dict, List

REPO_ROOT = Path(__file__).resolve().parents[2]
DOJO_DIR = REPO_ROOT / "examples" / "ag-ui" / "cao-dojo"
TOOLS_DIR = DOJO_DIR / "tools"
TRACE_DIR = DOJO_DIR / "trace"
PUBLISHED_TRACE = TRACE_DIR / "cao-dojo-trace.ndjson"
SHAPE_FILE = TRACE_DIR / "cao-dojo-trace.shape.json"
KNOWN_GAPS_FILE = TOOLS_DIR / "known_gaps.json"
CONFORMANCE_DIR = REPO_ROOT / "test" / "examples" / "fixtures" / "ag-ui-conformance"

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import record_trace  # noqa: E402
import trace_leaks  # noqa: E402
import trace_scrub  # noqa: E402

__all__ = [
    "CONFORMANCE_DIR",
    "KNOWN_GAPS_FILE",
    "PUBLISHED_TRACE",
    "REPO_ROOT",
    "SHAPE_FILE",
    "TOOLS_DIR",
    "TRACE_DIR",
    "offline_trace",
    "record_trace",
    "trace_leaks",
    "trace_scrub",
    "trace_sources",
]


@functools.lru_cache(maxsize=1)
def offline_trace() -> bytes:
    """The offline capture, normalised. Cached: it is deterministic."""
    meta, raw = record_trace.capture_offline()
    return trace_scrub.normalise(meta, raw)


def trace_sources() -> Dict[str, bytes]:
    sources = {"offline": offline_trace()}
    if PUBLISHED_TRACE.is_file():
        sources["published"] = PUBLISHED_TRACE.read_bytes()
    return sources


def source_ids() -> List[str]:
    return ["offline", "published"] if PUBLISHED_TRACE.is_file() else ["offline"]
