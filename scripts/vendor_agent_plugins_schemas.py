#!/usr/bin/env python3
"""Vendor the canonical Agent Plugins 1.0.0 JSON Schemas and guard the pin.

Agent Plugins §5.2 forbids a client from **retrieving a schema while loading a
plugin**. CAO therefore validates against schema bytes committed to this
repository, and this script is what keeps those bytes honest.

Two modes, mirroring ``scripts/vendor_ext_apps_skills.py``:

``python scripts/vendor_agent_plugins_schemas.py``
    Refresh: fetch each schema from its canonical URL, write it into
    ``src/cli_agent_orchestrator/schemas/agent_plugins/1.0.0/``, and rewrite
    ``PIN.json`` with the fetched bytes' sha256. **Requires network.**

``python scripts/vendor_agent_plugins_schemas.py --check``
    Drift guard: hash the on-disk vendored bytes and compare against
    ``PIN.json``. Exits non-zero on any mismatch, missing file, or unreadable
    pin. **Deliberately offline** — this is the step wired into CI on every PR,
    and an offline check is the only kind that can run in the same air-gapped
    conditions §5.2 exists to protect.

``--verify-remote``
    Optional, network-gated: additionally re-fetch each canonical URL and
    confirm upstream still serves the pinned bytes. Exits 2 (distinct from 1 =
    verified mismatch) when the network is unavailable, so a caller can treat
    "unverifiable" differently from "wrong".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT / "src" / "cli_agent_orchestrator" / "schemas" / "agent_plugins" / "1.0.0"
PIN_PATH = SCHEMA_DIR / "PIN.json"

# --- Pin -------------------------------------------------------------------
# The specification version this vendored copy targets, and the upstream
# provenance recorded alongside it. Bump these together with a refresh run.
SPEC_VERSION = "1.0.0"
SPEC_URL = "https://agent-plugins.org/specification"
SPEC_REPOSITORY = "https://github.com/agentplugins/agent-plugins-spec"
SPEC_COMMIT = "b78a4f162d92c4b09ee205a11f59a6187926d947"
SOURCE_BASE_URL = f"https://agent-plugins.org/schemas/{SPEC_VERSION}/"

# Schema files vendored, in the order they are reported.
SCHEMA_FILES: List[str] = ["plugin.schema.json", "mcp.schema.json"]

FETCH_TIMEOUT_S = 30


class VendorError(RuntimeError):
    """Raised for any non-recoverable vendoring failure."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _display(path: Path) -> str:
    """Repo-relative path for messages, falling back to absolute.

    An error message must never be the thing that raises. ``relative_to``
    throws for any path outside the repo, which happens whenever the schema
    directory is pointed elsewhere (tests, a vendored checkout).
    """
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _fetch(url: str) -> bytes:
    """Fetch ``url`` and return its raw bytes."""
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as response:  # noqa: S310
            if response.status != 200:
                raise VendorError(f"GET {url} returned HTTP {response.status}")
            body: bytes = response.read()
    except urllib.error.URLError as exc:
        raise VendorError(f"could not fetch {url}: {exc}") from exc
    except OSError as exc:
        raise VendorError(f"could not fetch {url}: {exc}") from exc
    if not body:
        raise VendorError(f"GET {url} returned an empty body")
    return body


def _render_pin(hashes: Dict[str, str]) -> str:
    """Render ``PIN.json`` deterministically from the fetched hashes."""
    pin = {
        "specification": "Agent Plugins",
        "version": SPEC_VERSION,
        "specification_url": SPEC_URL,
        "specification_repository": SPEC_REPOSITORY,
        "specification_commit": SPEC_COMMIT,
        "source_base_url": SOURCE_BASE_URL,
        "files": {
            filename: {
                "url": f"{SOURCE_BASE_URL}{filename}",
                "schema_id": f"{SOURCE_BASE_URL}{filename}",
                "sha256": hashes[filename],
            }
            for filename in SCHEMA_FILES
        },
    }
    return json.dumps(pin, indent=2) + "\n"


def load_pin() -> Dict[str, Dict[str, str]]:
    """Return ``PIN.json``'s ``files`` mapping.

    Raises:
        VendorError: If the pin is missing, unparseable, or does not describe
            every schema file this script vendors.
    """
    if not PIN_PATH.is_file():
        raise VendorError(f"pin file missing: {_display(PIN_PATH)}")
    try:
        pin = json.loads(PIN_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise VendorError(f"pin file is not valid JSON: {exc}") from exc

    files = pin.get("files")
    if not isinstance(files, dict):
        raise VendorError("pin file has no 'files' object")
    for filename in SCHEMA_FILES:
        entry = files.get(filename)
        if not isinstance(entry, dict) or not entry.get("sha256"):
            raise VendorError(f"pin file records no sha256 for {filename}")
    return files


def _refresh() -> int:
    """Fetch every canonical schema, write it to disk, and rewrite the pin."""
    hashes: Dict[str, str] = {}
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    for filename in SCHEMA_FILES:
        url = f"{SOURCE_BASE_URL}{filename}"
        body = _fetch(url)
        (SCHEMA_DIR / filename).write_bytes(body)
        hashes[filename] = _sha256(body)
        print(f"  fetched {filename} ({len(body)} bytes, sha256 {hashes[filename][:12]}…)")

    PIN_PATH.write_text(_render_pin(hashes), encoding="utf-8")
    print(
        f"Vendored {len(SCHEMA_FILES)} Agent Plugins {SPEC_VERSION} schemas into "
        f"{_display(SCHEMA_DIR)}/ and rewrote PIN.json"
    )
    return 0


def _check_local() -> Tuple[List[str], Dict[str, str]]:
    """Hash the on-disk schemas against ``PIN.json``.

    Returns:
        A ``(problems, recorded_hashes)`` pair. ``problems`` is empty when every
        vendored file is present and matches its recorded hash.
    """
    problems: List[str] = []
    pinned = load_pin()
    recorded = {name: entry["sha256"] for name, entry in pinned.items()}

    for filename in SCHEMA_FILES:
        path = SCHEMA_DIR / filename
        if not path.is_file():
            problems.append(f"vendored schema missing: {_display(path)}")
            continue
        actual = _sha256(path.read_bytes())
        expected = recorded[filename]
        if actual != expected:
            problems.append(f"{filename}: on-disk sha256 {actual} does not match pinned {expected}")
    return problems, recorded


def _verify_remote(recorded: Dict[str, str]) -> Tuple[List[str], bool]:
    """Re-fetch each canonical URL and compare against the recorded hashes.

    Returns:
        A ``(problems, reachable)`` pair. ``reachable`` is ``False`` when the
        network (or the schema host) could not be reached at all, which callers
        report as "unverifiable" rather than "mismatched".
    """
    problems: List[str] = []
    for filename in SCHEMA_FILES:
        url = f"{SOURCE_BASE_URL}{filename}"
        try:
            body = _fetch(url)
        except VendorError as exc:
            print(f"Remote verification could not reach the pin: {exc}", file=sys.stderr)
            return problems, False
        actual = _sha256(body)
        if actual != recorded[filename]:
            problems.append(
                f"{filename}: upstream sha256 {actual} no longer matches pinned "
                f"{recorded[filename]} — upstream changed the published schema"
            )
    return problems, True


def _check(verify_remote: bool) -> int:
    try:
        problems, recorded = _check_local()
    except VendorError as exc:
        print(f"Agent Plugins schema pin is unusable: {exc}", file=sys.stderr)
        return 1

    unverifiable = False
    if verify_remote and not problems:
        remote_problems, reachable = _verify_remote(recorded)
        problems.extend(remote_problems)
        unverifiable = not reachable

    if problems:
        print("Vendored Agent Plugins schemas are OUT OF SYNC with the pin:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "\nRun: python scripts/vendor_agent_plugins_schemas.py  (then commit the result)",
            file=sys.stderr,
        )
        return 1

    if unverifiable:
        print(
            f"OK (local): {len(SCHEMA_FILES)} vendored Agent Plugins {SPEC_VERSION} "
            f"schemas match PIN.json; remote verification was skipped (network-gated)."
        )
        return 2

    scope = "local + remote" if verify_remote else "local"
    print(
        f"OK ({scope}): {len(SCHEMA_FILES)} vendored Agent Plugins {SPEC_VERSION} "
        f"schemas match PIN.json"
    )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify vendored bytes against PIN.json instead of writing (offline).",
    )
    parser.add_argument(
        "--verify-remote",
        action="store_true",
        help="With --check, additionally re-fetch upstream and compare (network-gated).",
    )
    args = parser.parse_args(argv)

    if args.check:
        return _check(verify_remote=args.verify_remote)

    if args.verify_remote:
        parser.error("--verify-remote is only meaningful with --check")

    try:
        return _refresh()
    except VendorError as exc:
        print(f"Vendoring failed: {exc}", file=sys.stderr)
        print(
            f"Refreshing requires network access to {SOURCE_BASE_URL}. Run this where "
            f"the network is available, then commit "
            f"{_display(SCHEMA_DIR)}/.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
