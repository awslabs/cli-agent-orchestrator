#!/usr/bin/env python3
"""GO ledger: the conductor's record of each `GO <wid> ...` it grants, in state/go.jsonl.

Only the conductor writes this file: it runs `add` itself, then replies `GO <wid>`. The shepherd
and every worker only read it (check_mandate.py --go-id, holds.py clear), right before each write.
Each record carries writer_role "conductor" and writer_terminal (the writer's CAO_TERMINAL_ID,
an id only); readers refuse any record without writer_role "conductor". This is a discipline
control, not an OS boundary: every agent runs as the same user and could write the file.

  go_ledger.py add --writer-role conductor --id WID --action A --head SHA --policy-version V --granted-by WHO
                   (--pr N | --branch B | both) [--repo OWNER/NAME] [--new-head SHA]
                   [--body-file F | --body-sha256 HEX] [--item KIND:ID]
                   [--expires-in MINUTES] [--relayed-by CONDUCTOR_ID]      -> ADDED <wid> (0)
  go_ledger.py revoke --writer-role conductor --id WID --by WHO   -> REVOKED <wid> (0)
  go_ledger.py next-id (--pr N | --branch B) --action A --head SHA [--pending F]
                                                          -> <N>-<action>-<head8>-<k> (0)
  next-id is read-only (anyone may run it; the shepherd builds every WOULD id with it): <k> is
  1 + the largest k already used for that <N>-<action>-<head8> prefix by any grant or revoke in
  go.jsonl or any id string in the shepherd's pending file (default state/shepherd-pending.json,
  if it exists), so every request -- a renewal after a policy bump, restart or expiry too -- gets
  an id never used before. With no PR (a branch-only one-off) <N> is the branch with each "/"
  as ".". A corrupt ledger or unreadable pending file prints ERROR ... (2).
  Any --writer-role other than conductor, or no CAO_TERMINAL_ID, exits 2 and writes nothing.
  Every append first validates the whole ledger and the new row with bin/state_schema.py (the
  same validators check_mandate.py reads with): a corrupt ledger, a row the readers would reject,
  or a revoke of an id that was never granted prints ERROR state_corrupt:/row_refused:... (2)
  and writes nothing.

A GO binds the action, the target (repo, PR and/or branch), the content (the body sha256, and for
a push action -- any action ending in _push -- the exact commit to publish, `new_head`, plus the
branch), `head` (the remote head the action expects to find just before it runs), the
policy_version, and an expiry (`--expires-in`, 1-1440 minutes, default 120).
A **one-off** (the user's explicit OK for something no mandate covers, e.g. opening one PR) is
`--granted-by user --relayed-by <conductor terminal id>`: the conductor relays the user's words;
check_mandate.py then allows exactly that action on that target and content, nothing else.
`--body-file` hashes the raw bytes (the same as `shasum -a 256 F`); with neither body option the
GO is for an action without a body. Bad input exits 2 and writes nothing.
"""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

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
SHA_RE = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
WID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")
TERMINAL_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
BRANCH_RE = re.compile(r"(?!-)(?!.*\.\.)[A-Za-z0-9_./-]{1,200}")


def mandate_repo(doc):
    """The kit targets exactly one repository; infer it only from the authorised URL map."""
    urls = doc.get("push_urls") if isinstance(doc, dict) else None
    if not isinstance(urls, dict) or len(urls) != 1:
        raise ValueError("a single push_urls repository is required")
    repo = next(iter(urls))
    if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("invalid push_urls repository")
    return repo


def ledger():
    sd = os.environ.get("SUPER_AGENT_STATE_DIR") or os.path.join(KIT, "state")
    return os.path.join(sd, "go.jsonl")


def append(row):
    """Append one row under the lock with SS.append_row, after the same validation every reader
    runs: the existing ledger must be valid (else SS.StateCorrupt) and the new row must be a valid
    grant with an id not used before, or the one revocation of an earlier grant (else
    SS.RowRefused, e.g. row_refused:go.jsonl:<n>:id:duplicate). Either way nothing is written."""
    os.makedirs(os.path.dirname(ledger()), exist_ok=True)
    with open(ledger() + ".lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        SS.append_row(ledger(), row, SS.check_go_file_append)


def pending_file():
    sd = os.environ.get("SUPER_AGENT_STATE_DIR") or os.path.join(KIT, "state")
    return os.path.join(sd, "shepherd-pending.json")


def _id_strings(value):
    """Every string (keys and values) anywhere in a JSON document."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _id_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _id_strings(v)


def next_id(target, action, head, pending=None):
    """The next unused WOULD id <target>-<action>-<head8>-<k> (read-only). Raises SS.StateCorrupt
    on a corrupt ledger and ValueError on an unreadable pending file."""
    prefix = f"{target}-{action}-{head[:8]}-"
    used = {r["id"] for r in SS.load_go(ledger())}
    path = pending if pending is not None else pending_file()
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        if pending is not None:
            raise ValueError("pending file not found") from None
        raw = None
    if raw is not None:
        try:
            used.update(_id_strings(SS.loads(raw.decode("utf-8"))))
        except (UnicodeDecodeError, ValueError):
            raise ValueError("pending file is not JSON") from None
    ks = [int(u[len(prefix):]) for u in used
          if u.startswith(prefix) and re.fullmatch(r"[1-9][0-9]{0,8}", u[len(prefix):])]
    return f"{prefix}{max(ks, default=0) + 1}"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Record conductor GO replies for check_mandate.py.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a_ = sub.add_parser("add")
    for f in ("--id", "--action", "--head", "--policy-version", "--granted-by"):
        a_.add_argument(f, required=True)
    a_.add_argument("--pr")
    a_.add_argument("--branch")
    a_.add_argument("--repo", help="default: mandates.json single push_urls repository")
    a_.add_argument("--mandates", default=os.path.join(KIT, "mandates.json"))
    a_.add_argument("--new-head", help="push actions: the exact commit to publish")
    a_.add_argument("--expires-in", type=int, default=120, help="minutes, 1-1440")
    a_.add_argument("--relayed-by", help="one-offs (--granted-by user): the conductor's id")
    a_.add_argument("--item", help="clear_hold only: the review or comment, e.g. review:123")
    g = a_.add_mutually_exclusive_group()
    g.add_argument("--body-file")
    g.add_argument("--body-sha256")
    r_ = sub.add_parser("revoke")
    r_.add_argument("--id", required=True)
    r_.add_argument("--by", required=True)
    for p in (a_, r_):
        p.add_argument("--writer-role", required=True, help="must be conductor: only it writes go.jsonl")
    n_ = sub.add_parser("next-id", help="read-only: print the next unused WOULD id")
    for f in ("--action", "--head"):
        n_.add_argument(f, required=True)
    t_ = n_.add_mutually_exclusive_group(required=True)
    t_.add_argument("--pr")
    t_.add_argument("--branch", help="a branch-only one-off with no PR")
    n_.add_argument("--pending", help="the shepherd's pending WOULDs (default state/shepherd-pending.json)")
    a = ap.parse_args(argv)
    if a.cmd == "next-id":
        if (a.pr is not None and not SS.PR_RE.fullmatch(a.pr)) \
                or (a.branch is not None and not BRANCH_RE.fullmatch(a.branch)) \
                or not SS.ACTION_RE.fullmatch(a.action) or not SHA_RE.fullmatch(a.head):
            print("ERROR bad --pr (digits) or --branch, --action or --head (40 lowercase hex)")
            return 2
        target = a.pr if a.pr is not None else a.branch.replace("/", ".")
        try:
            wid = next_id(target, a.action, a.head, a.pending)
        except (SS.StateCorrupt, ValueError) as exc:
            print(f"ERROR {exc}")
            return 2
        if not WID_RE.fullmatch(wid):
            print("ERROR the id would be too long or invalid")
            return 2
        print(wid)
        return 0
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if a.writer_role != "conductor":
        print(f"ERROR only the conductor writes go.jsonl (writer-role {a.writer_role!r} refused)")
        return 2
    writer = os.environ.get("CAO_TERMINAL_ID") or ""  # the terminal id only; no other env is read
    if not TERMINAL_RE.fullmatch(writer):
        print("ERROR CAO_TERMINAL_ID is missing or not a terminal id: run this in the conductor's terminal")
        return 2
    signed = {"writer_role": "conductor", "writer_terminal": writer}
    if not WID_RE.fullmatch(a.id):
        print("ERROR bad id")
        return 2
    if a.cmd == "revoke":
        try:
            append({"id": a.id, "revoked": True, "by": a.by, "at": at, **signed})
        except SS.StateCorrupt as exc:  # a corrupt ledger, or no such grant: nothing written
            print(f"ERROR {exc}")
            return 2
        print(f"REVOKED {a.id}")
        return 0
    if not SHA_RE.fullmatch(a.head) or not a.granted_by.strip():
        print("ERROR bad head (40 lowercase hex) or granted_by")
        return 2
    if (a.pr is None and a.branch is None) or (a.pr is not None and not a.pr.isdigit()) \
            or (a.branch is not None and not BRANCH_RE.fullmatch(a.branch)):
        print("ERROR bad target: --pr (digits) and/or --branch (a branch name)")
        return 2
    if a.repo is None:
        try:
            with open(a.mandates) as fh:
                a.repo = mandate_repo(json.load(fh))
        except (OSError, ValueError):
            print("ERROR unreadable or ambiguous mandates repository")
            return 2
    if not REPO_RE.fullmatch(a.repo):
        print("ERROR bad repo (owner/name)")
        return 2
    if a.action.endswith("_push"):
        if a.new_head is None or not SHA_RE.fullmatch(a.new_head) or a.branch is None:
            print("ERROR a push GO needs --new-head (40 lowercase hex) and --branch")
            return 2
    elif a.new_head is not None:
        print("ERROR --new-head is for push actions only")
        return 2
    if not 1 <= a.expires_in <= 1440:
        print("ERROR --expires-in must be 1-1440 minutes")
        return 2
    if a.granted_by == "user" and not (a.relayed_by or "").strip():
        print("ERROR a user one-off needs --relayed-by <conductor id>")
        return 2
    body = None
    if a.body_file:
        try:
            with open(a.body_file, "rb") as fh:
                body = hashlib.sha256(fh.read()).hexdigest()
        except OSError as exc:
            print(f"ERROR body file: {type(exc).__name__}")
            return 2
    elif a.body_sha256 is not None:
        if not HEX64.fullmatch(a.body_sha256):
            print("ERROR bad body sha256")
            return 2
        body = a.body_sha256
    now = datetime.now(timezone.utc)
    row = {"id": a.id, "action": a.action, "repo": a.repo, "pr": a.pr, "branch": a.branch,
           "head": a.head, "new_head": a.new_head, "body_sha256": body,
           "policy_version": a.policy_version, "granted_by": a.granted_by, "at": at,
           "expires_at": (now + timedelta(minutes=a.expires_in)).isoformat(timespec="seconds"),
           "revoked": False, **signed}
    if a.relayed_by is not None:
        row["relayed_by"] = a.relayed_by
    if a.item is not None:
        if not re.fullmatch(r"(review|issue_comment|review_comment):[0-9]+", a.item):
            print("ERROR bad item")
            return 2
        row["item"] = a.item
    try:
        append(row)
    except SS.StateCorrupt as exc:  # a corrupt ledger, or a row the readers would reject
        print(f"ERROR {exc}")
        return 2
    print(f"ADDED {a.id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
