#!/usr/bin/env python3
"""Strict schemas for every kit state file: one validator per row type, shared by every reader
and every writer.

Reading a state file validates EVERY row. Bad JSON, a row that is not an object, an unknown row
type, a missing required field, a field of the wrong type or format, an unknown field, a blank
line, a revocation of a GO that was never granted, or an owner-gone proof that is not bound to
its reservation makes the WHOLE file corrupt. The reader raises StateCorrupt, printed as

    state_corrupt:<file>:<line>:<why>

and the calling script fails closed by its own convention (DENY, REFUSED, ERROR, PENDING kept,
REFUSE). There is no "skip this row" path. <file> is the path under the state dir (else the
basename), <line> the 1-based line of a .jsonl file or the row's key in a JSON document
(holds.json: "101", "_cleared[0]"; owner.json and the generation file: 1), and <why> is
<field>:<problem>. Writers run the same validators before they write; a refused row raises
RowRefused (row_refused:<file>:<line>:<why>) and nothing is written, so the kit never writes a
row it would itself reject. Every .jsonl writer appends through append_row (one LF separator,
correct for an empty file, a final LF or a complete last row without one). GO grant ids are
unique, and each id is revoked at most once (else id:duplicate).

Out of scope: an agent deliberately forging a well-formed row (every agent is the same OS user).
In scope: accidental damage (a truncated line, a missing field, a wrong type) always fails
closed. The fix is a hand repair by the conductor after it inspects the file:

  state_schema.py check [--state-dir D]
      validates every state file the scripts read: OK <n> files (0) | one state_corrupt line
      per corrupt file (1)
  state_schema.py append-plan-row --row JSON [--ledger F]
      the conductor's only way to add a plan-approvals.jsonl row (a baseline on the user's
      words, or an approval log row): validates the row and the file, then appends under a lock.
      APPENDED <kind> (0) | ERROR state_corrupt:... / row_refused:... (2), nothing written

Files and row types (required fields; [optional]; "|null" = present, may be null):
  go.jsonl               grant  : id action repo pr|null branch|null head new_head|null
                                  body_sha256|null policy_version granted_by at expires_at
                                  revoked=false writer_role=conductor writer_terminal
                                  [relayed_by item]; pr or branch set; a *_push action has
                                  new_head and branch, any other has new_head null; granted_by
                                  "user" needs relayed_by
                         revoke : id revoked=true by at writer_role=conductor writer_terminal;
                                  id names a grant on an earlier line
  outbox.jsonl, outbox/<key>.sent
                         history: key url recorded_at generation (.sent: key = the file's key)
  outbox/<key>.pending   line 1 : reservation key pr kind head reserved_at pid owner_terminal
                                  generation token_sha256 (key = the file's key)
                         line 2+: proof owner_gone=true generation owner evidence proven_ns
                                  proven_at, bound to line 1: same owner, same generation, the
                                  exact 404 evidence for that owner, proven_at = proven_ns
  outbox/<key>.failed-*  (written, never read) the failed row: failed_at reason by evidence
  outbox/<key>.generation  one integer >= 1
  holds.json             "<pr>" : hold sha date head detail items recorded_at; items are
                                  id sha256 real body
                         _cleared: list of pr item_id body_sha256 reason go_id cleared_at
  plan-approvals.jsonl   baseline: kind workflow file sha256 verified_at verified_by evidence
                         approval: kind at plan_id workflow pr|null head base sha256{file: hex}
  heavy/active/owner.json  label token acquired_at refreshed_at pid
Lock (*.lock), temp (*.tmp), backup (mandates.json.bak-*) and .failed-* files are never read.
"""
import argparse
import fcntl
import json
import math
import os
import re
import sys
from datetime import datetime, timezone

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAO_API = "http://localhost:9889"

SHA40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
HEX32 = re.compile(r"[0-9a-f]{32}")
WID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")
TERMINAL_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
BRANCH_RE = re.compile(r"(?!-)(?!.*\.\.)[A-Za-z0-9_./-]{1,200}")
ACTION_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
PR_RE = re.compile(r"[1-9][0-9]{0,9}")
ITEM_RE = re.compile(r"(review|issue_comment|review_comment):[0-9]+")
# A held item GitHub returned without an id is saved as "<kind>:None"; it can never be cleared.
HELD_ITEM_RE = re.compile(r"(review|issue_comment|review_comment):([0-9]+|None)")
WORD_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
HEAD_RE = re.compile(r"[0-9a-f]{7,40}")
URL_RE = re.compile(r"\S{1,2048}")
LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
FILE_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}")
GENERATION_RE = re.compile(r"[1-9][0-9]{0,11}")


# ---------- errors ----------

class StateCorrupt(ValueError):
    """A state file the kit reads is damaged: the caller fails closed."""
    prefix = "state_corrupt"

    def __init__(self, file, line, why):
        super().__init__(f"{self.prefix}:{file}:{line}:{why}")
        self.file, self.line, self.why = file, line, why

    def __str__(self):
        return f"{self.prefix}:{self.file}:{self.line}:{self.why}"


class RowRefused(StateCorrupt):
    """A writer refused to write a row the readers would reject: nothing was written."""
    prefix = "row_refused"


def state_dir():
    return os.environ.get("SUPER_AGENT_STATE_DIR") or os.path.join(KIT, "state")


def label(path):
    """The file's name in messages: its path under the state dir, else its basename."""
    full, sd = os.path.abspath(path), os.path.abspath(state_dir())
    if full.startswith(sd + os.sep):
        return os.path.relpath(full, sd)
    return os.path.basename(full)


# ---------- field checkers: value -> None (valid) or a problem word ----------

def text(one_line=True, max_len=4000):
    """A non-empty string (not only whitespace)."""
    def check(v):
        if v is None:
            return "null"
        if not isinstance(v, str):
            return "type"
        if not v.strip():
            return "empty"
        if len(v) > max_len or (one_line and ("\n" in v or "\r" in v)):
            return "format"
        return None
    return check


def any_text(v):
    """Any string, even empty (a review body)."""
    if v is None:
        return "null"
    return None if isinstance(v, str) else "type"


def pattern(rx):
    def check(v):
        if v is None:
            return "null"
        if not isinstance(v, str):
            return "type"
        return None if rx.fullmatch(v) else "format"
    return check


def iso(v):
    """An ISO-8601 time with a zone (Z or an offset)."""
    if v is None:
        return "null"
    if not isinstance(v, str):
        return "type"
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return "format"
    return None if dt.tzinfo is not None else "no_zone"


def integer(lo):
    def check(v):
        if v is None:
            return "null"
        if type(v) is not int:  # bool is not an int here
            return "type"
        return None if v >= lo else "range"
    return check


def number(lo):
    def check(v):
        if v is None:
            return "null"
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return "type"
        return None if math.isfinite(v) and v >= lo else "range"
    return check


def const(value):
    """Exactly this value, of this type (True is not 1, "conductor" is case-sensitive)."""
    def check(v):
        if v is None:
            return "null"
        if type(v) is not type(value):
            return "type"
        return None if v == value else "value"
    return check


def boolean(v):
    if v is None:
        return "null"
    return None if type(v) is bool else "type"


def nullable(chk):
    def check(v):
        return None if v is None else chk(v)
    return check


def one_of(*checks):
    """Valid when any check passes (else the first check's problem)."""
    def check(v):
        problems = [c(v) for c in checks]
        return None if None in problems else problems[0]
    return check


def map_of(key_rx, value_chk):
    """A non-empty object whose keys match key_rx and whose values pass value_chk."""
    def check(v):
        if v is None:
            return "null"
        if not isinstance(v, dict):
            return "type"
        if not v:
            return "empty"
        for k, x in v.items():
            if not key_rx.fullmatch(k) or value_chk(x):
                return "format"
        return None
    return check


# ---------- row types ----------

class Row:
    """A row type: its required fields, its allowed optional fields, and a cross-field check."""

    def __init__(self, name, required, optional=None, check=None):
        self.name, self.required, self.optional, self.check = name, required, optional or {}, check

    def problem(self, row):
        """None when the row is valid, else '<field>:<problem>'."""
        if not isinstance(row, dict):
            return "row:not_object"
        for field in row:
            if field not in self.required and field not in self.optional:
                return f"{field}:unknown_field"
        for field, chk in self.required.items():
            if field not in row:
                return f"{field}:missing"
            why = chk(row[field])
            if why:
                return f"{field}:{why}"
        for field, chk in self.optional.items():
            if field in row:
                why = chk(row[field])
                if why:
                    return f"{field}:{why}"
        return self.check(row) if self.check else None


def _grant_check(row):
    if row["pr"] is None and row["branch"] is None:
        return "target:missing"
    if row["action"].endswith("_push"):
        if row["new_head"] is None or row["branch"] is None:
            return "new_head:missing_for_push"
    elif row["new_head"] is not None:
        return "new_head:not_a_push"
    if row["granted_by"] == "user" and "relayed_by" not in row:
        return "relayed_by:missing_for_user"
    return None


SIGNED = {"writer_role": const("conductor"), "writer_terminal": pattern(TERMINAL_RE)}
GO_GRANT = Row("go_grant", {
    "id": pattern(WID_RE), "action": pattern(ACTION_RE), "repo": pattern(REPO_RE),
    "pr": nullable(pattern(PR_RE)), "branch": nullable(pattern(BRANCH_RE)), "head": pattern(SHA40),
    "new_head": nullable(pattern(SHA40)), "body_sha256": nullable(pattern(HEX64)),
    "policy_version": text(), "granted_by": text(), "at": iso, "expires_at": iso,
    "revoked": const(False), **SIGNED,
}, optional={"relayed_by": text(), "item": pattern(ITEM_RE)}, check=_grant_check)
GO_REVOKE = Row("go_revoke", {"id": pattern(WID_RE), "revoked": const(True), "by": text(), "at": iso,
                              **SIGNED})

HISTORY = Row("outbox_history", {"key": pattern(HEX64), "url": pattern(URL_RE), "recorded_at": iso,
                                 "generation": integer(1)})
RESERVATION = Row("outbox_reservation", {
    "key": pattern(HEX64), "pr": pattern(PR_RE), "kind": pattern(WORD_RE), "head": pattern(HEAD_RE),
    "reserved_at": iso, "pid": integer(0), "owner_terminal": pattern(TERMINAL_RE),
    "generation": integer(1), "token_sha256": pattern(HEX64)})
PROOF = Row("outbox_owner_gone_proof", {
    "owner_gone": const(True), "generation": integer(1), "owner": pattern(TERMINAL_RE),
    "evidence": text(), "proven_ns": integer(0), "proven_at": iso})


def _evidence_check(v):
    if v is None:
        return None
    if not isinstance(v, dict):
        return "type"
    why = Row("evidence", {"generation": integer(1), "proof": PROOF.problem,
                           "search_fetched_at": iso}).problem(v)
    return why and why.replace(":", ".", 1)


def _failed_check(row):
    if (row["by"] == "owner_token") != (row["evidence"] is None):
        return "evidence:does_not_match_by"
    return None


FAILED = Row("outbox_failed", {"failed_at": iso, "reason": pattern(WORD_RE),
                               "by": one_of(const("owner_token"), const("owner_gone")),
                               "evidence": _evidence_check}, check=_failed_check)


def _items_check(v):
    if v is None:
        return "null"
    if not isinstance(v, list):
        return "type"
    if not v:
        return "empty"
    for i, item in enumerate(v):
        why = HELD_ITEM.problem(item)
        if why:
            return f"[{i}].{why}"
    return None


HELD_ITEM = Row("held_item", {"id": pattern(HELD_ITEM_RE), "sha256": pattern(HEX64), "real": boolean,
                              "body": any_text})
HOLD = Row("hold", {"sha": pattern(SHA40), "date": iso, "head": pattern(SHA40),
                    "detail": text(one_line=False), "items": _items_check, "recorded_at": iso})
CLEARED = Row("cleared", {"pr": pattern(PR_RE), "item_id": pattern(ITEM_RE), "body_sha256": pattern(HEX64),
                          "reason": text(max_len=200), "go_id": pattern(WID_RE), "cleared_at": iso})

BASELINE = Row("plan_baseline", {"kind": const("baseline"), "workflow": text(), "file": pattern(FILE_RE),
                                 "sha256": pattern(HEX64), "verified_at": iso, "verified_by": text(),
                                 "evidence": text(one_line=False)})
APPROVAL = Row("plan_approval", {"kind": const("approval"), "at": iso, "plan_id": text(), "workflow": text(),
                                 "pr": nullable(one_of(integer(1), pattern(PR_RE))), "head": pattern(SHA40),
                                 "base": text(), "sha256": map_of(FILE_RE, pattern(HEX64))})

HEAVY_OWNER = Row("heavy_owner", {"label": pattern(LABEL_RE), "token": pattern(HEX32),
                                  "acquired_at": number(0), "refreshed_at": number(0), "pid": integer(0)})


# ---------- strict JSON and line reading ----------

def _no_duplicates(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate field {k}")
        out[k] = v
    return out


def _no_constant(name):
    raise ValueError(f"{name} is not JSON")


def loads(text_):
    """json.loads that refuses duplicate fields and NaN/Infinity."""
    return json.loads(text_, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)


def jsonl_rows(path):
    """[(line_no, row)] of a .jsonl file; [] when it does not exist. A blank, undecodable or
    non-JSON line raises StateCorrupt: it might be any row."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return []
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()  # the final newline
    out = []
    for n, line in enumerate(lines, 1):
        try:
            s = line.decode("utf-8")
        except UnicodeDecodeError:
            raise StateCorrupt(label(path), n, "row:not_utf8") from None
        if not s.strip():
            raise StateCorrupt(label(path), n, "row:blank_line")
        try:
            out.append((n, loads(s)))
        except ValueError:
            raise StateCorrupt(label(path), n, "row:not_json") from None
    return out


def append_row(path, row, check):
    """The one append routine every .jsonl writer uses. Call it under the file's existing
    lock. check(path, row) validates the whole current file and the new row (raising
    StateCorrupt, or RowRefused for the row); on any error nothing is written. Then exactly one
    row is appended with exactly one LF between it and the last row, whichever valid ending the
    file had: empty (or missing), ending in LF, or a complete last row with no final LF. The new
    row always ends in LF.

    Rule for a trailing blank line (a final LF followed by another LF): the reader rejects it as
    row:blank_line, so check() raises and the writer refuses cleanly before writing; the file is
    left byte-for-byte as it was, for the conductor to hand-repair (drop the blank line) and
    rerun, after which recovery works as normal. Readers accept both valid endings."""
    check(path, row)
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        raw = b""
    sep = b"\n" if raw and not raw.endswith(b"\n") else b""
    with open(path, "ab") as fh:
        fh.write(sep + json.dumps(row, sort_keys=True).encode("utf-8") + b"\n")
        fh.flush()
        os.fsync(fh.fileno())


def json_doc(path):
    """One JSON document. FileNotFoundError propagates; anything unparsable is StateCorrupt."""
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        return loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise StateCorrupt(label(path), 1, "document:not_json") from None


# ---------- go.jsonl ----------

def go_row_problem(row, granted, revoked_ids):
    """Why this go.jsonl row is invalid after the grant ids in `granted` and the revoked ids in
    `revoked_ids`, or None. Grant ids are unique: a second grant with a used id -- the same
    bytes or not, revoked in between or not -- is corrupt, and so is a second revoke of one id,
    so a stale grant can never reappear after its revocation."""
    if not isinstance(row, dict):
        return "row:not_object"
    revoked = row.get("revoked")
    if revoked is False:
        why = GO_GRANT.problem(row)
        if why is None and row["id"] in granted:
            return "id:duplicate"
        return why
    if revoked is True:
        why = GO_REVOKE.problem(row)
        if why is None and row["id"] not in granted:
            return "id:no_such_grant"
        if why is None and row["id"] in revoked_ids:
            return "id:duplicate"
        return why
    return "row_type:unknown (revoked must be true or false)"


def _go_ids(rows):
    """(granted ids, revoked ids) of validated go.jsonl rows."""
    granted = {r["id"] for r in rows if r["revoked"] is False}
    revoked_ids = {r["id"] for r in rows if r["revoked"] is True}
    return granted, revoked_ids


def load_go(path):
    """Every row of go.jsonl, validated; [] when the file does not exist."""
    rows = []
    for n, row in jsonl_rows(path):
        why = go_row_problem(row, *_go_ids(rows))
        if why:
            raise StateCorrupt(label(path), n, why)
        rows.append(row)
    return rows


def check_go_append(path, rows, row):
    """Raise RowRefused unless `row` may follow the validated `rows` of go.jsonl."""
    why = go_row_problem(row, *_go_ids(rows))
    if why:
        raise RowRefused(label(path), len(rows) + 1, why)


def check_go_file_append(path, row):
    """append_row check for go.jsonl: the whole ledger, then the new row after it."""
    check_go_append(path, load_go(path), row)


# ---------- outbox ----------

def iso_ns(ns):
    """ISO UTC time of integer nanoseconds, truncated (never rounded up) to microseconds."""
    dt = datetime.fromtimestamp(ns // 10**9, timezone.utc).replace(microsecond=(ns // 1000) % 10**6)
    return dt.isoformat(timespec="microseconds")


def gone_evidence(owner):
    """The only text that proves a terminal gone: what the template's exact curl writes."""
    return f"{CAO_API}/terminals/{owner} 404"


def history_problem(row):
    return HISTORY.problem(row)


def load_history(path):
    """Every row of outbox.jsonl, validated; [] when the file does not exist."""
    rows = []
    for n, row in jsonl_rows(path):
        why = HISTORY.problem(row)
        if why:
            raise StateCorrupt(label(path), n, why)
        rows.append(row)
    return rows


def check_history_append(path, row):
    """append_row check for outbox.jsonl: the whole history, then the new row."""
    rows = load_history(path)
    why = HISTORY.problem(row)
    if why:
        raise RowRefused(label(path), len(rows) + 1, why)


def sent_problem(row, key):
    why = HISTORY.problem(row)
    if why is None and row["key"] != key:
        return "key:not_this_file"
    return why


def load_sent(path, key):
    """The record in <key>.sent, validated. FileNotFoundError propagates."""
    row = json_doc(path)
    why = sent_problem(row, key)
    if why:
        raise StateCorrupt(label(path), 1, why)
    return row


def reservation_problem(row, key):
    why = RESERVATION.problem(row)
    if why is None and row["key"] != key:
        return "key:not_this_file"
    return why


def proof_problem(proof, reservation):
    """Why this owner-gone proof does not prove THIS reservation's owner gone, or None."""
    why = PROOF.problem(proof)
    if why:
        return why
    if proof["owner"] != reservation["owner_terminal"]:
        return "owner:not_the_reservation_owner"
    if proof["generation"] != reservation["generation"]:
        return "generation:not_the_reservation_generation"
    if proof["evidence"] != gone_evidence(proof["owner"]):
        return "evidence:not_a_404_for_the_owner"
    if proof["proven_at"] != iso_ns(proof["proven_ns"]):
        return "proven_at:not_proven_ns"
    return None


def load_pending(path, key):
    """(reservation, [proofs]) of <key>.pending, every line validated and every proof bound to
    the reservation. FileNotFoundError propagates; an empty file is corrupt."""
    rows = jsonl_rows(path)
    if not rows:
        raise StateCorrupt(label(path), 1, "reservation:missing")
    n, reservation = rows[0]
    why = reservation_problem(reservation, key)
    if why:
        raise StateCorrupt(label(path), n, why)
    proofs = []
    for n, proof in rows[1:]:
        why = proof_problem(proof, reservation)
        if why:
            raise StateCorrupt(label(path), n, why)
        proofs.append(proof)
    return reservation, proofs


def pending_append_check(key, failed=False):
    """append_row check for <key>.pending: the reservation and every proof, then the new row --
    an owner-gone proof bound to that reservation, or (failed=True) the final failed row."""
    def check(path, row):
        reservation, proofs = load_pending(path, key)
        if failed:
            why, line = failed_problem(row, reservation), "failed"
        else:
            why, line = proof_problem(row, reservation), len(proofs) + 2
        if why:
            raise RowRefused(label(path), line, why)
    return check


def failed_problem(row, reservation):
    why = FAILED.problem(row)
    if why is None and row["evidence"] is not None:
        ev = row["evidence"]
        if ev["generation"] != reservation["generation"]:
            return "evidence.generation:not_the_reservation_generation"
        why = proof_problem(ev["proof"], reservation)
        return why and f"evidence.proof.{why}"
    return why


def parse_generation(path, text_):
    """The integer in <key>.generation (one number >= 1, then an optional newline)."""
    body = text_[:-1] if text_.endswith("\n") else text_
    if not GENERATION_RE.fullmatch(body):
        raise StateCorrupt(label(path), 1, "generation:format")
    return int(body)


def check_generation_write(path, gen):
    if type(gen) is not int or gen < 1:
        raise RowRefused(label(path), 1, "generation:range")


def load_generation(path):
    """The last generation handed out (0 when the file does not exist)."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return 0
    try:
        return parse_generation(path, raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise StateCorrupt(label(path), 1, "generation:not_utf8") from None


# ---------- holds.json ----------

def holds_problem(doc):
    """(row key, why) for the first invalid part of a holds.json document, else None."""
    if not isinstance(doc, dict):
        return 1, "document:not_object"
    for k, v in doc.items():
        if k == "_cleared":
            if not isinstance(v, list):
                return "_cleared", "_cleared:type"
            for i, row in enumerate(v):
                why = CLEARED.problem(row)
                if why:
                    return f"_cleared[{i}]", why
        elif PR_RE.fullmatch(k):
            why = HOLD.problem(v)
            if why:
                return k, why
        else:
            return k, "row_type:unknown (want a PR number or _cleared)"
    return None


def load_holds(path):
    """The validated holds.json document; {} when it does not exist."""
    try:
        doc = json_doc(path)
    except FileNotFoundError:
        return {}
    bad = holds_problem(doc)
    if bad:
        raise StateCorrupt(label(path), *bad)
    return doc


def check_holds_write(path, doc):
    bad = holds_problem(doc)
    if bad:
        raise RowRefused(label(path), *bad)


# ---------- plan-approvals.jsonl ----------

def plan_row_problem(row):
    if not isinstance(row, dict):
        return "row:not_object"
    kind = row.get("kind")
    if kind == "baseline" and type(kind) is str:
        return BASELINE.problem(row)
    if kind == "approval" and type(kind) is str:
        return APPROVAL.problem(row)
    return "row_type:unknown (kind must be baseline or approval)"


def load_plan_ledger(path):
    rows = []
    for n, row in jsonl_rows(path):
        why = plan_row_problem(row)
        if why:
            raise StateCorrupt(label(path), n, why)
        rows.append(row)
    return rows


def default_plan_ledger():
    return os.path.join(state_dir(), "plan-approvals.jsonl")


def append_plan_row(path, row):
    """Validate the file and the row, then append it under an exclusive lock."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(str(path) + ".lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        append_row(path, row, check_plan_append)


def check_plan_append(path, row):
    """append_row check for plan-approvals.jsonl: the whole ledger, then the new row."""
    rows = load_plan_ledger(path)
    why = plan_row_problem(row)
    if why:
        raise RowRefused(label(path), len(rows) + 1, why)


# ---------- heavy/active/owner.json ----------

def load_owner(path):
    """The validated owner.json. FileNotFoundError propagates (a marker without its file)."""
    row = json_doc(path)
    why = HEAVY_OWNER.problem(row)
    if why:
        raise StateCorrupt(label(path), 1, why)
    return row


def check_owner_write(path, row):
    why = HEAVY_OWNER.problem(row)
    if why:
        raise RowRefused(label(path), 1, why)


# ---------- the whole state dir ----------

def check_all(sd):
    """[(file, None | StateCorrupt)] for every state file the scripts read."""
    os.environ["SUPER_AGENT_STATE_DIR"] = sd  # labels relative to this dir
    out = []

    def run(name, fn):
        try:
            fn()
            out.append((name, None))
        except StateCorrupt as exc:
            out.append((name, exc))
        except OSError as exc:
            out.append((name, StateCorrupt(name, 0, f"file:{type(exc).__name__}")))

    for name, fn in (("go.jsonl", load_go), ("outbox.jsonl", load_history),
                     ("plan-approvals.jsonl", load_plan_ledger), ("holds.json", load_holds)):
        p = os.path.join(sd, name)
        if os.path.exists(p):
            run(name, lambda p=p, fn=fn: fn(p))
    ob = os.path.join(sd, "outbox")
    for f in sorted(os.listdir(ob)) if os.path.isdir(ob) else []:
        p, key, _, suffix = os.path.join(ob, f), *f.partition(".")
        if suffix == "pending":
            run(f"outbox/{f}", lambda p=p, key=key: load_pending(p, key))
        elif suffix == "sent":
            run(f"outbox/{f}", lambda p=p, key=key: load_sent(p, key))
        elif suffix == "generation":
            run(f"outbox/{f}", lambda p=p: load_generation(p))
    owner = os.path.join(sd, "heavy", "active", "owner.json")
    if os.path.exists(owner):
        run("heavy/active/owner.json", lambda: load_owner(owner))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Validate the kit's state files (fail closed).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--state-dir")
    p = sub.add_parser("append-plan-row")
    p.add_argument("--row", required=True, help="one JSON object: a baseline or approval row")
    p.add_argument("--ledger")
    a = ap.parse_args(argv)
    if a.cmd == "check":
        results = check_all(os.path.abspath(a.state_dir or state_dir()))
        bad = [exc for _, exc in results if exc is not None]
        for exc in bad:
            print(exc)
        if bad:
            return 1
        print(f"OK {len(results)} files")
        return 0
    path = a.ledger or default_plan_ledger()
    try:
        row = loads(a.row)
    except ValueError:
        print(f"ERROR {RowRefused(label(path), 0, 'row:not_json')}")
        return 2
    try:
        append_plan_row(path, row)
    except StateCorrupt as exc:
        print(f"ERROR {exc}")
        return 2
    except OSError as exc:
        print(f"ERROR ledger: {type(exc).__name__}")
        return 2
    print(f"APPENDED {row['kind']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
