#!/usr/bin/env python3
"""Hand every watch.json owner held by one terminal to another, in one atomic write.

  remap_owners.py --watch PB/watch.json --from <old owner id> --to <shepherd id> --session <its session>

Every owner object (PRs and issues alike) whose "terminal" equals --from gets --to and
--session; a null owner or terminal stays null; any other owner is untouched. Also adds an
"identities" block (ours = the workplace github_login, + bot logins, mirrored from pr-watch/poll.py)
when it is missing,
which bin/automerge_gate.py --identities reads. Prints REMAPPED <n> and exits 0, or ERROR (2)
without writing anything. Back up watch.json first.
"""
import importlib.util
import argparse
import json
import os
import re
import sys

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _state_schema():
    """bin/state_schema.py, loaded once per process (every script shares its classes)."""
    path = os.path.join(KIT, "bin", "state_schema.py")
    mod = sys.modules.get("kit_state_schema")
    if mod is None or getattr(mod, "__file__", None) != path:
        spec = importlib.util.spec_from_file_location("kit_state_schema", path)
        if spec is None or spec.loader is None:
            raise ImportError("state_schema.py not found next to this script")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["kit_state_schema"] = mod
        spec.loader.exec_module(mod)
    return mod


SS = _state_schema()
ID_RE = re.compile(r"[0-9a-f]{8}")
BOTS = {"bots": ["copilot-pull-request-reviewer", "github-advanced-security", "github-actions"],
        "bot_suffixes": ["[bot]"]}


def identities():
    """The identities block: ours is the workplace github_login. Fails closed when it is unset."""
    return {"ours": [SS.workplace().load()["github_login"]], **BOTS}


def remap(doc, old, new, session):
    n = 0
    for group in ("prs", "issues"):
        items = doc.get(group) or {}
        if not isinstance(items, dict):
            raise ValueError(f"{group} is not an object")
        for item in items.values():
            owner = item.get("owner") if isinstance(item, dict) else None
            if isinstance(owner, dict) and owner.get("terminal") == old:
                owner["terminal"] = new
                owner["session"] = session
                n += 1
    if "identities" not in doc:
        doc["identities"] = identities()
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description="Remap watch.json owners.")
    ap.add_argument("--watch", required=True)
    ap.add_argument("--from", dest="old", required=True)
    ap.add_argument("--to", dest="new", required=True)
    ap.add_argument("--session", required=True)
    a = ap.parse_args(argv)
    if not ID_RE.fullmatch(a.old) or not ID_RE.fullmatch(a.new) or not a.session.strip():
        print("ERROR ids must be 8 lowercase hex and the session non-empty")
        return 2
    try:
        with open(a.watch, encoding="utf-8") as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict):
            raise ValueError("watch.json is not an object")
        n = remap(doc, a.old, a.new, a.session)
        with SS.writer_locked():
            SS.require_writer()
            tmp = a.watch + ".remap.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=1)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, a.watch)
    except (OSError, ValueError, AttributeError) as exc:
        print(f"ERROR {type(exc).__name__}: {exc}")
        return 2
    print(f"REMAPPED {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
