#!/usr/bin/env python3
"""Turn `gh api -i <endpoint>` output into the envelope automerge_gate.py --required reads:
{"endpoint": <endpoint>, "http_status": <int | null>, "body": <JSON | null>}. Offline.

  gh api -i repos/REPO/rules/branches/main > "$D/rules.raw"      (REPO: repos[0] of workplace.json)
  python3 KIT/bin/api_envelope.py --endpoint repos/REPO/rules/branches/main \
      --raw "$D/rules.raw" --out "$D/required-rules.json"

`gh api -i` prints the status line and headers, a blank line, then the body, also for an error
status (gh then exits 1, which is expected here: run the next line regardless).

Always writes the envelope. http_status is null (so the gate WAITs required_contexts_unverified)
when the raw file is empty, has no `HTTP/<v> <code>` status line, has a body that is not JSON,
or announces another page (`Link: ...; rel="next"`): one page is not the whole inventory.
Prints ENVELOPE <status|null> and exits 0 when parsed, 2 when not.
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
STATUS = re.compile(r"HTTP/[0-9.]+ ([0-9]{3})(?: .*)?")
ENDPOINT = re.compile(r"repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+")


def parse(raw):
    """(status, body) or (None, None). Pure."""
    text = raw.replace("\r\n", "\n")
    head, sep, body = text.partition("\n\n")
    if not sep:
        return None, None
    lines = head.split("\n")
    m = STATUS.fullmatch(lines[0].strip())
    if not m:
        return None, None
    for ln in lines[1:]:
        name, _, value = ln.partition(":")
        if name.strip().lower() == "link" and re.search(r'rel="next"', value):
            return None, None
    try:
        doc = json.loads(body)
    except ValueError:
        return None, None
    return int(m.group(1)), doc


def main(argv=None):
    ap = argparse.ArgumentParser(description="gh api -i output -> required-contexts envelope.")
    ap.add_argument("--endpoint", required=True)
    ap.add_argument("--raw", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if not ENDPOINT.fullmatch(a.endpoint):
        print("ERROR bad endpoint (want repos/<owner>/<name>/...)")
        return 2
    try:
        with open(a.raw, encoding="utf-8") as fh:
            status, body = parse(fh.read())
    except (OSError, UnicodeDecodeError):
        status, body = None, None
    try:
        with SS.writer_locked_for_output(a.out):
            tmp = a.out + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"endpoint": a.endpoint, "http_status": status, "body": body}, fh)
            os.replace(tmp, a.out)
    except SS.NotWriter as exc:
        print(f"ERROR {exc}")
        return 2
    print(f"ENVELOPE {status if status is not None else 'null'}")
    return 0 if status is not None else 2


if __name__ == "__main__":
    sys.exit(main())
