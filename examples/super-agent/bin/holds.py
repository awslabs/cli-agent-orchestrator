#!/usr/bin/env python3
"""Clear a hold-on-findings hold by reading, bound to the exact text. Offline.

Why: the keyword scan flags clean structured approvals ("APPROVE (0 P1, 0 P2, 0 P3) ...
Findings: None.") as findings, and only a newer non-merge commit clears a persisted hold, but
such a commit dismisses the approval. So a person-equivalent read, approved by the conductor,
may mark ONE item (one review or comment, one exact body) as not holding.

  holds.py body --from FILE --item KIND:ID --out F
      FILE is a paginated reviews / issue-comments / review-comments file; writes the item's
      canonical body (CRLF -> LF, trailing newlines dropped) to F and prints BODY <sha256>.
      Both the shepherd and the conductor read and hash that same file.
  holds.py clear --repo OWNER/NAME --pr-author LOGIN --base BRANCH --pr N --item KIND:ID
                 --body-file F --reason "<one line>" --go-id WID
      Appends {pr, item_id, body_sha256, reason, go_id, cleared_at} to the "_cleared" list in
      state/holds.json. --repo, --pr-author and --base are the verified target PR's (the same
      values every other pre-write check passes). Refuses (exit 1, writes nothing) unless:
        - F is canonical (written by `holds.py body`);
        - the body lists no REAL finding (automerge_gate.real_findings: a non-zero count such as
          "1 P3", a line-start item such as "1. **P3:" or "nit:", or a list item under a
          Findings/Issues heading): those stay held until a newer non-merge commit, or the
          user's decline relayed by the conductor; there is no clear path for them here. A
          prose mention ("The previous P3 is fixed.", "0 P1, 0 P2, 0 P3") is clearable;
        - mandates.json has an unrevoked, user-granted clear_hold mandate for the shepherd;
        - the full check_mandate.decide() decision (the same function every other action uses)
          prints ALLOW for holder shepherd, action clear_hold, this repo, author and base,
          with --go-id WID: not paused; the mandate's scope (repo, author, base) covers the
          target, so a conductor GO can't widen the user's standing scope; and state/go.jsonl's
          latest record for WID was written by the conductor (writer_role "conductor") and is an
          unrevoked, unexpired GO for action clear_hold, this repo, this PR, this body sha256 and
          the current policy_version (a clear binds an item and its body, not a head: the GO's
          own head is passed as the expected head);
        - on top of that, the GO names this item.
      Exit 0 CLEARED, 1 REFUSED <reason>, 2 ERROR.

automerge_gate.py honours a clear only while the item's id AND the sha256 of its current body
match: an edited body holds again. KIND is review, issue_comment or review_comment.
go.jsonl and holds.json are read (and holds.json written) through bin/state_schema.py: a corrupt
file is REFUSED state_corrupt:<file>:<line>:<why>, and nothing is written.
"""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.dirname(HERE)
ITEM_RE = re.compile(r"(review|issue_comment|review_comment):([0-9]+)")


def _gate():
    spec = importlib.util.spec_from_file_location("automerge_gate", os.path.join(HERE, "automerge_gate.py"))
    if spec is None or spec.loader is None:
        raise ImportError("automerge_gate.py not found next to holds.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GATE = _gate()


def _check_mandate():
    spec = importlib.util.spec_from_file_location("check_mandate", os.path.join(HERE, "check_mandate.py"))
    if spec is None or spec.loader is None:
        raise ImportError("check_mandate.py not found next to holds.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CM = _check_mandate()
SS = GATE.SS  # bin/state_schema.py: the one copy every script in this process shares


def state_dir():
    return os.environ.get("SUPER_AGENT_STATE_DIR") or os.path.join(KIT, "state")


def refuse(reason):
    print(f"REFUSED {reason}")
    return 1


def error(msg):
    print(f"ERROR {msg}")
    return 2


def cmd_body(a):
    m = ITEM_RE.fullmatch(a.item)
    if not m:
        return error("bad item (want review:<id>, issue_comment:<id> or review_comment:<id>)")
    try:
        rows = GATE.load_pages(a.src)
    except (OSError, ValueError) as exc:
        return error(f"source file: {type(exc).__name__}")
    hits = [r for r in rows if isinstance(r, dict) and str(r.get("id")) == m.group(2)]
    if len(hits) != 1:
        return error(f"item {a.item} found {len(hits)} times")
    if not isinstance(hits[0].get("body"), str):  # a null body is not an empty one
        return error(f"item {a.item} has no string body")
    text = GATE.normalise_body(hits[0]["body"])
    with open(a.out, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    print(f"BODY {hashlib.sha256(text.encode('utf-8')).hexdigest()}")
    return 0


def latest_go(rows, wid):
    hits = [r for r in rows if r["id"] == wid]  # rows: SS.load_go, every row validated
    return hits[-1] if hits else None


def write_cleared(path, row):
    """Append one _cleared row to holds.json under its lock, with an atomic replace. The file is
    read with the same validator the gate uses (SS.StateCorrupt) and the result is validated
    before the write (SS.RowRefused); either way nothing is written."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        holds = GATE.read_holds(path)
        holds.setdefault(GATE.CLEARED, []).append(row)
        SS.check_holds_write(path, holds)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(holds, fh, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)


def cmd_clear(a):
    if not ITEM_RE.fullmatch(a.item):
        return error("bad item (want review:<id>, issue_comment:<id> or review_comment:<id>)")
    if not a.pr.isdigit():
        return error("bad pr")
    reason = a.reason.strip()
    if not reason or "\n" in a.reason or len(reason) > 200:
        return error("reason must be one non-empty line of at most 200 characters")
    try:
        with open(a.body_file, "rb") as fh:
            raw = fh.read()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return error(f"body file: {type(exc).__name__}")
    if GATE.normalise_body(text) != text:
        return refuse("body_not_canonical (write it with `holds.py body`)")
    sha = hashlib.sha256(raw).hexdigest()
    if GATE.real_findings(text):
        return refuse("real_findings: a severity-tagged finding stays held until a newer non-merge "
                      "commit, or the user's decline relayed by the conductor")
    try:
        with open(a.mandates) as fh:
            doc = json.load(fh)
        records = CM.load_go(a.go_file or os.path.join(state_dir(), "go.jsonl"))
    except SS.StateCorrupt as exc:  # state_corrupt:go.jsonl:<line>:<why>
        return refuse(str(exc))
    except (OSError, ValueError) as exc:
        return refuse(f"unreadable_input:{type(exc).__name__}")
    if not isinstance(doc, dict):
        return refuse("malformed_mandates")
    mandates = [m for m in doc.get("mandates") or [] if isinstance(m, dict)
                and m.get("action") == "clear_hold" and m.get("holder") == "shepherd"
                and m.get("revoked") is False and m.get("granted_by") == "user"]
    if not mandates:
        return refuse("no_clear_hold_mandate")  # not even a user one-off GO clears without M8
    go = latest_go(records, a.go_id)
    if go is None:
        return refuse(f"go_missing:{a.go_id}")
    # The full decision, the same function every other action runs (pause, scope, revocation,
    # policy_version, the GO's writer role, repo, PR, body and expiry). A clear binds an item and
    # its body, not a head, so the GO's own head stands in for the expected and remote head.
    head = go.get("head")
    extra = {"go_id": a.go_id, "pr": a.pr, "body_sha256": sha, "expect_head": head, "remote_head": head}
    word, detail = CM.decide(doc, "shepherd", "clear_hold", a.pr_author, a.base, a.repo, extra, records)
    if word != "ALLOW":
        return refuse(detail if word == "DENY" else f"{word.lower()} {detail}".strip())
    if go.get("item") != a.item:  # on top of the full decision: this exact item
        return refuse(f"go_mismatch:item:{a.go_id}")

    path = a.holds or os.path.join(state_dir(), "holds.json")
    try:
        write_cleared(path, {"pr": a.pr, "item_id": a.item, "body_sha256": sha, "reason": reason,
                             "go_id": a.go_id,
                             "cleared_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    except SS.StateCorrupt as exc:  # a corrupt holds.json, or a row the gate would reject
        return refuse(str(exc))
    except (OSError, ValueError) as exc:
        return refuse(f"holds_file:{type(exc).__name__}")
    print(f"CLEARED #{a.pr} {a.item} body={sha[:8]}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Clear a hold-on-findings hold by reading (conductor GO required).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("body")
    b.add_argument("--from", dest="src", required=True)
    b.add_argument("--item", required=True)
    b.add_argument("--out", required=True)
    c = sub.add_parser("clear")
    for f in ("--repo", "--pr-author", "--base", "--pr", "--item", "--body-file", "--reason", "--go-id"):
        c.add_argument(f, required=True)
    c.add_argument("--mandates", default=os.path.join(KIT, "mandates.json"))
    c.add_argument("--go-file")
    c.add_argument("--holds")
    a = ap.parse_args(argv)
    return cmd_body(a) if a.cmd == "body" else cmd_clear(a)


if __name__ == "__main__":
    sys.exit(main())
