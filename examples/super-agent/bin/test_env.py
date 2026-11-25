#!/usr/bin/env python3
"""Run commands in a disposable isolated test environment."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY = os.path.join(KIT, "state", "scratch.jsonl")


def append(path, label):
    os.makedirs(os.path.dirname(REGISTRY), exist_ok=True)
    row = {"path": path, "label": label, "owner_terminal": os.environ.get("CAO_TERMINAL_ID", ""),
           "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    with open(REGISTRY, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def records():
    try:
        with open(REGISTRY, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
    except FileNotFoundError:
        return []


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--label", default="test")
    run.add_argument("cmd", nargs=argparse.REMAINDER)
    sub.add_parser("list")
    clean = sub.add_parser("clean")
    clean.add_argument("path")
    args = ap.parse_args(argv)
    if args.command == "list":
        for row in records():
            print(json.dumps(row, sort_keys=True))
        return 0
    if args.command == "clean":
        if args.path not in {row["path"] for row in records()} or not args.path.startswith("/tmp/"):
            print("ERROR unrecorded_or_unsafe_path", file=sys.stderr)
            return 2
        probe = subprocess.run(["lsof", "+D", args.path], text=True, capture_output=True)
        if probe.stdout.strip():
            print("ERROR path_in_use", file=sys.stderr)
            return 1
        subprocess.run(["chmod", "-R", "u+rwx", args.path], check=True)
        shutil.rmtree(args.path)
        print("CLEANED")
        return 0
    if not args.cmd or args.cmd[0] != "--" or len(args.cmd) == 1:
        ap.error("run needs -- <command ...>")
    scratch = tempfile.mkdtemp(prefix="cao-test-env-", dir="/tmp")
    append(scratch, args.label)
    env = {key: value for key, value in os.environ.items()
           if not (key.startswith("CAO_") or key.startswith("HERDR_") or key.startswith("TMUX"))}
    env.update({"HOME": os.path.join(scratch, "home"), "TMPDIR": os.path.join(scratch, "tmp"),
                "TMUX_TMPDIR": os.path.join(scratch, "tmux"),
                "PYTEST_ADDOPTS": f"--basetemp={os.path.join(scratch, 'pytest')}"},
               )
    env.setdefault("UV_CACHE_DIR", os.path.expanduser("~/.cache/uv"))
    env.setdefault("RUSTUP_HOME", os.path.expanduser("~/.rustup"))
    env.setdefault("CARGO_HOME", os.path.expanduser("~/.cargo"))
    for path in (env["HOME"], env["TMPDIR"], env["TMUX_TMPDIR"]):
        os.makedirs(path)
    print(scratch)
    return subprocess.run(args.cmd[1:], env=env).returncode


if __name__ == "__main__":
    sys.exit(main())
