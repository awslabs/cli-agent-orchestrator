#!/usr/bin/env python3
"""Are these workflow scripts byte-identical to their trusted baseline? Pure and offline.

  plan_hash_check.py [--ledger state/plan-approvals.jsonl] FILE [FILE ...]

For each FILE: sha256 of its bytes (the same as `shasum -a 256 FILE`), compared with the LATEST
`"kind": "baseline"` record whose "file" is FILE's basename. Prints one line per file and exits
0 only when every file matches:
  MATCH <name> <sha8>
  REFUSE missing_baseline <name>            no trusted record: the conductor asks the user
  REFUSE changed <name> got=<8> want=<8>    the script changed: the conductor asks the user
  REFUSE unreadable <name>
A corrupt ledger prints REFUSE corrupt_ledger state_corrupt:<file>:<line>:<why> and exits 1:
every row is validated by bin/state_schema.py (kind baseline or approval, every required field),
and any invalid row corrupts the whole ledger. The conductor appends rows only with
`state_schema.py append-plan-row`, which runs the same validator first.

Only baseline records are trusted, never approval log rows, so approving a plan can never turn
a new hash into a baseline. Baselines come from verified evidence (seeded at handover) or the
user's explicit words; the conductor never records one by itself.
"""
import argparse
import hashlib
import importlib.util
import os
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


def default_ledger():
    return SS.default_plan_ledger()


def baselines(path):
    """{file: sha256} from the latest baseline record per file. Every row is validated first
    (SS.load_plan_ledger: a baseline or an approval row, nothing else): any invalid row might be
    a later baseline, so it raises SS.StateCorrupt and nothing is trusted."""
    out = {}
    for row in SS.load_plan_ledger(path):
        if row["kind"] != "baseline":
            continue  # approval rows are a log; they never become a baseline
        out[row["file"]] = row["sha256"]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Compare workflow scripts with trusted baselines.")
    ap.add_argument("--ledger", default=None)
    ap.add_argument("files", nargs="+")
    a = ap.parse_args(argv)
    try:
        trusted = baselines(a.ledger or default_ledger())
    except SS.StateCorrupt as exc:
        print(f"REFUSE corrupt_ledger {exc}")
        return 1
    except OSError as exc:
        print(f"REFUSE corrupt_ledger unreadable:{type(exc).__name__}")
        return 1
    ok = True
    for f in a.files:
        name = os.path.basename(f)
        try:
            with open(f, "rb") as fh:
                got = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            print(f"REFUSE unreadable {name}")
            ok = False
            continue
        want = trusted.get(name)
        if want is None:
            print(f"REFUSE missing_baseline {name}")
            ok = False
        elif got != want:
            print(f"REFUSE changed {name}")
            ok = False
        else:
            print(f"MATCH {name} {got[:8]}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
