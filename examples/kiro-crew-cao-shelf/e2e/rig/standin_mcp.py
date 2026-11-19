#!/usr/bin/env python3
"""A stand-in for a user-level MCP server or plugin tool, for the e2e rig only (stdlib only, stdio only).

    python e2e/rig/standin_mcp.py --marker <scratch>/work-c/rogue/standin.log

It plays the part of a server the owner configured for all of their Claude Code sessions (a Slack, mail or
ticket plugin, say): the relay chat never asked for it, but Claude Code mounts it beside the App's server
unless something keeps it out. Its one tool, ``mark``, appends a line to ``--marker`` and answers
``STANDIN-RAN``, so a journey can tell whether a call to it ran. It opens no socket and touches nothing
else; ``--marker`` must be inside ``$SHELF_SCRATCH``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

TOOL = {
    "name": "mark",
    "description": "Stand-in for a plugin tool the relay chat never asked for. Appends a line to a log.",
    "inputSchema": {"type": "object", "properties": {"note": {"type": "string"}}},
}


def answer(req: dict[str, Any], marker: Path) -> Optional[dict[str, Any]]:
    """The JSON-RPC response to one request, or None for a notification."""
    method = req.get("method")
    rid = req.get("id")
    if rid is None:
        return None
    if method == "initialize":
        params = req.get("params") or {}
        result: dict[str, Any] = {
            "protocolVersion": params.get("protocolVersion") or "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "standin", "version": "0.0.1"},
        }
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        note = str(((req.get("params") or {}).get("arguments") or {}).get("note", ""))[:200]
        with open(marker, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} mark {note}\n")
        result = {"content": [{"type": "text", "text": "STANDIN-RAN"}]}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "not found"}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def check_marker(path: str) -> Path:
    scratch = os.environ.get("SHELF_SCRATCH", "")
    marker = Path(path).resolve()
    if not scratch or not str(marker).startswith(str(Path(scratch).resolve()) + os.sep):
        raise SystemExit("standin_mcp: --marker must be inside $SHELF_SCRATCH")
    marker.parent.mkdir(parents=True, exist_ok=True)
    return marker


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--marker", required=True)
    marker = check_marker(ap.parse_args().marker)
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        if not isinstance(req, dict):
            continue
        resp = answer(req, marker)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
