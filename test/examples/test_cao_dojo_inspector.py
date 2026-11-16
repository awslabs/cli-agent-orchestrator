"""The CAO Dojo raw event inspector shows every recorded event, in order (#1165).

Two layers, like the Dojo's other node lanes:

* **Static guard** (always runs): ``inspector.html`` exists, has no dependency,
  builds its DOM with ``textContent`` only, and has no write path (no ``POST``,
  ``innerHTML``, ``eval`` or ``EventSource``). It is a reader of a recorded
  trace, not a client of ``/agui/v1``.
* **Browser run**: ``tools/inspector-e2e.mjs`` opens the page on the published
  trace and compares every row with the ``.ndjson``, computed from the file and
  not from the page. It needs ``node`` and Playwright (``npm ci`` plus
  ``npx playwright install chromium`` in ``examples/ag-ui/cao-dojo/tools``).
  It skips where either is absent. No CI job runs it yet, so it carries no
  ``must_run`` marker; the marker returns with the CI step.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

DOJO = Path(__file__).resolve().parents[2] / "examples" / "ag-ui" / "cao-dojo"
PAGE = DOJO / "inspector.html"
E2E = DOJO / "tools" / "inspector-e2e.mjs"
TRACE = DOJO / "trace" / "cao-dojo-trace.ndjson"
NODE = shutil.which("node")
HAS_PLAYWRIGHT = (DOJO / "tools" / "node_modules" / "@playwright" / "test").is_dir()


def _playwright_cache() -> Path:
    """Where Chromium was installed, found without ``$HOME``.

    The suite's conftest points ``HOME`` at a temp directory, so Playwright would
    look for its browser there and not find it.
    """
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    sub = "Library/Caches/ms-playwright" if sys.platform == "darwin" else ".cache/ms-playwright"
    return home / sub


def _has_chromium() -> bool:
    """A Chromium build is installed where Playwright will look (or is pointed to)."""
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    root = Path(override) if override else _playwright_cache()
    return root.is_dir() and any(root.glob("chromium*"))


def _records() -> List[Dict[str, Any]]:
    out = []
    for line in TRACE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            if "_meta" not in rec:
                out.append(rec)
    return out


def test_the_page_exists_and_the_trace_has_the_records_it_must_show() -> None:
    assert PAGE.is_file(), "examples/ag-ui/cao-dojo/inspector.html is missing"
    records = _records()
    # The counts come from the file, so re-recording the trace does not turn this red.
    assert records, "the published trace has no records"
    # Frames carry a timestamp only on some records (the CUSTOM cards today); the page must
    # show the others as absent, not invent one.
    stamped = [r for r in records if isinstance(r.get("frame", {}).get("timestamp"), int)]
    assert len(stamped) <= len(records)


def test_the_page_is_a_read_only_zero_dependency_reader() -> None:
    # The header comment names what the page does not do; check the code only.
    src = re.sub(r"<!--.*?-->", "", PAGE.read_text(encoding="utf-8"), flags=re.S)
    for forbidden in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
        "EventSource",
        "WebSocket",
        '"POST"',
        "'POST'",
        '"PUT"',
        '"PATCH"',
        "sendBeacon",
        '"DELETE"',
        "<script src",
        '<link rel="stylesheet"',
        "@import",
        "/agui/v1",
    ):
        assert forbidden not in src, f"inspector.html must not contain {forbidden!r}"
    assert "textContent" in src


def test_the_trace_path_is_resolved_and_held_to_this_origin() -> None:
    """The page's claim that it never fetches an arbitrary URL rests on these checks.

    The behaviour is exercised in the browser by ``inspector-e2e.mjs`` (hostile paths such
    as ``\\\\evil.example/x`` and ``ht<TAB>tp://evil.example/x`` must produce no request
    to another origin); this guards the code that does it from being edited away.
    """
    src = PAGE.read_text(encoding="utf-8")
    body = src[src.index("function safeRelative") : src.index("function loadFromPath")]
    assert "new URL(raw, location.href)" in body
    assert "url.origin !== location.origin" in body
    assert "url.pathname.indexOf(dir.pathname) !== 0" in body
    assert "\\\\" in body and "\\u0020" in body, "backslash and whitespace/control refusal"


@pytest.mark.skipif(
    NODE is None or not HAS_PLAYWRIGHT or not _has_chromium(),
    reason="needs node, `npm ci` in examples/ag-ui/cao-dojo/tools and an installed Chromium",
)
def test_every_recorded_event_is_shown_in_order_with_type_and_timestamp(tmp_path: Path) -> None:
    env = dict(os.environ, E2E_SHOT_DIR=os.environ.get("E2E_SHOT_DIR", str(tmp_path)))
    env.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_playwright_cache()))
    assert NODE is not None
    proc = subprocess.run(
        [NODE, str(E2E)],
        cwd=str(E2E.parent),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    match = re.search(r"inspector-e2e: PASS rows=(\d+) with-timestamp=(\d+) ", out)
    assert match, out
    records = _records()
    stamped = [r for r in records if isinstance(r.get("frame", {}).get("timestamp"), int)]
    assert (int(match.group(1)), int(match.group(2))) == (len(records), len(stamped)), out
    assert "refused=" in out, out
