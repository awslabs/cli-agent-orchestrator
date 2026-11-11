#!/usr/bin/env python3
"""Durable outbox: reserve an idempotence key BEFORE an outside post, so neither a retry nor a
second publisher can double-post.

  outbox.py key --pr N --kind KIND --head SHA [--body-file F]   print the sha256 key
  outbox.py check KEY        exit 0 UNUSED | 1 USED url=... |
                             4 PENDING owner=<terminal> generation=<g> (reconcile)
  outbox.py reserve KEY --pr N --kind KIND --head SHA [--body-file F] --owner-terminal TID
                             re-derive the key from the inputs (a changed body gives a different
                             key: exit 2 KEY_MISMATCH), then create state/outbox/<key>.pending
                             with os.open(O_CREAT|O_EXCL), recording the owner's CAO terminal id,
                             the sha256 of a fresh secret token and the reservation's generation
                             (state/outbox/<key>.generation: 1, 2, ... one more per reservation).
                             exit 0 RESERVED <key> token=<32 hex> | 1 USED | 4 PENDING
  outbox.py sent KEY --url URL [--generation G]
                             after the post: write the URL into <key>.pending, fsync, rename it
                             to <key>.sent, append a row to state/outbox.jsonl. exit 0 RECORDED |
                             1 NOT_RESERVED (no .pending) | 1 DUPLICATE (already .sent) |
                             1 REFUSED generation_mismatch (a reconciler names the generation
                             it proved gone; a different reservation is never resolved)
  outbox.py failed KEY --reason WORD --token T
                             the owner itself (its token), after its own post failed and an exact
                             read-only search found no such post.
  outbox.py owner-gone KEY --generation G --evidence EVIDENCE_FILE
                             reconcile step 1: the caller ran exactly
                               curl -s -o /dev/null -w '%{url_effective} %{http_code}' \
                                 http://localhost:9889/terminals/<owner> > EVIDENCE_FILE
                             and the file reads `http://localhost:9889/terminals/<owner> 404` for
                             the owner of generation G. Appends the proof, with its time, to
                             <key>.pending. exit 0 OWNER_GONE <key> generation=G at=<T> | 1 REFUSED
  outbox.py now              print the UTC time (microseconds): taken right BEFORE the search
  outbox.py failed KEY --reason WORD --owner-gone --generation G --search-file F
                   --search-fetched-at T --body-file B --login LOGIN
                             reconcile step 3, only once the owner is proven gone: a search
                             taken while the owner could still post proves nothing. Refuses
                             unless generation G is still the pending one and has an owner-gone
                             proof, the search was fetched (T, from `now`) AND its file written
                             after the latest proof, B re-derives this key, and F (the
                             `gh api --paginate --slurp .../issues/<n>/comments` output) holds no
                             comment by LOGIN with that body. Renames <key>.pending to
                             <key>.failed-<UTC> with the evidence.
                             exit 0 FAILED | 1 NOT_RESERVED | 1 REFUSED <why>
                             A live owner may still post, so nobody else may release its key.

  outbox.py find-post --search-file F --body-file F --login LOGIN
                             the exact read-only reconcile search: F holds
                             `gh api --paginate --slurp repos/{repo}/issues/<n>/comments`.
                             exit 0 FOUND <html_url> (exactly one comment by LOGIN whose body
                             equals the file) | 1 NOT_FOUND | 2 MANY or ERROR

A leftover .pending (a publisher crashed between posting and `sent`) blocks every new attempt
until someone reconciles it with `sent` (the search found the post) or `failed` (it didn't).
Every state file is read and written through bin/state_schema.py: outbox.jsonl rows and .sent
records (key, url, recorded_at, generation), the .pending reservation (line 1) and its owner-gone
proofs (later lines: owner, generation, the exact 404 evidence, proven_ns and proven_at, all bound
to that reservation), and the .generation counter. Any invalid row makes that file corrupt:
check and reserve exit 2 ERROR state_corrupt:<file>:<line>:<why> and reserve creates nothing;
a damaged .pending stays PENDING (check exit 4; sent, owner-gone and failed REFUSED). A writer
never writes a row it would reject (ERROR row_refused:..., nothing written).
Every step runs under fcntl.flock on state/outbox.lock, so check-then-create can't race with a
rename. The body is normalised (CRLF -> LF, trailing newlines dropped) so the key matches how
the read-back compares a posted body with its file. Errors (bad key, corrupt outbox) exit 2.
State dir: $SUPER_AGENT_STATE_DIR, else <kit>/state.
"""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import secrets
import sys
import time
from datetime import datetime, timezone

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY_RE = re.compile(r"[0-9a-f]{64}")
WORD_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
PENDING_EXIT = 4
TERMINAL_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


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
CAO_API = SS.CAO_API


def pending_state(key):
    """(reservation, proofs) of <key>.pending: line 1 is the reservation, every later line an
    owner-gone proof bound to it (same owner, same generation, the exact 404 evidence). They
    live and die with that one reservation: `sent` and `failed` rename the file away.
    FileNotFoundError when there is none. Any invalid line raises SS.StateCorrupt: a damaged
    line might be the latest proof (or the reservation), so nothing is ever skipped."""
    return SS.load_pending(entry(key, "pending"), key)


def prove_gone(key, generation, gone_file):
    """(proof, None) when gone_file proves the owner of this pending generation gone, else (None, why)."""
    row, _ = pending_state(key)
    owner, gen = row["owner_terminal"], row["generation"]
    if generation != gen:
        return None, f"generation_mismatch pending={gen} given={generation}"
    try:
        with open(gone_file, encoding="utf-8") as fh:
            evidence = fh.read().strip()
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"owner_gone_evidence_unreadable:{type(exc).__name__}"
    if evidence != SS.gone_evidence(owner):
        return None, f"owner_not_proven_gone owner={owner} evidence={evidence[:80]!r}"
    proven_ns = time.time_ns()
    return {"owner_gone": True, "generation": gen, "owner": owner, "evidence": evidence,
            "proven_ns": proven_ns, "proven_at": iso_ns(proven_ns)}, None


iso_ns = SS.iso_ns


def parse_ns(text):
    """Integer UTC nanoseconds of an ISO time with a zone, else None."""
    try:
        dt = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return int(dt.timestamp()) * 10**9 + dt.microsecond * 1000


def owner_release(key, token, a=None):
    """(how, evidence) when this caller may release the reservation, else (None, why)."""
    row, proofs = pending_state(key)
    if token is not None:
        if not re.fullmatch(r"[0-9a-f]{32}", token) or \
                not secrets.compare_digest(hashlib.sha256(token.encode()).hexdigest(), row["token_sha256"]):
            return None, "not_owner (wrong token)"
        return "owner_token", None
    owner, gen = row["owner_terminal"], row["generation"]
    if a.generation != gen:
        return None, f"generation_mismatch pending={gen} given={a.generation}"
    if not proofs:
        return None, f"owner_not_proven_gone owner={owner} (run owner-gone first, then a fresh search)"
    proven = max(p["proven_ns"] for p in proofs)
    fetched = parse_ns(a.search_fetched_at)
    if fetched is None:
        return None, "bad --search-fetched-at (an ISO time with a zone, from `outbox.py now`)"
    if not fetched > proven:
        return None, "search_before_proof:fetched_at (search again, after the owner-gone proof)"
    try:
        written = os.stat(a.search_file).st_mtime_ns
        body = read_body(a.body_file)
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"search_or_body_unreadable:{type(exc).__name__}"
    if not written > proven:
        return None, "search_before_proof:file_written (search again, after the owner-gone proof)"
    if make_key(row["pr"], row["kind"], row["head"], body) != key:
        return None, "body_not_this_key (the --body-file is not the reserved post)"
    hits, err = search_hits(a.search_file, normalise(body), a.login)
    if err:
        return None, f"search_unreadable ({err})"
    if hits:
        return None, f"search_found_post: record it with `sent --generation {gen}`, never failed"
    return "owner_gone", {"generation": gen, "proof": [p for p in proofs if p["proven_ns"] == proven][0],
                          "search_fetched_at": a.search_fetched_at.strip()}


def state_dir():
    return os.environ.get("SUPER_AGENT_STATE_DIR") or os.path.join(KIT, "state")


def outbox_path():
    return os.path.join(state_dir(), "outbox.jsonl")


def entry(key, suffix):
    return os.path.join(state_dir(), "outbox", f"{key}.{suffix}")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_key(pr, kind, head, body):
    body = body.replace("\r\n", "\n").rstrip("\n")
    payload = json.dumps({"pr": str(pr), "kind": kind, "head": head, "body": body},
                         sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def find(key):
    """Return the recorded row for key, or None. Every outbox.jsonl row is validated first
    (SS.load_history): a row with a missing or ill-typed key might be this key's record, so any
    invalid row raises SS.StateCorrupt, never "not posted"."""
    for row in SS.load_history(outbox_path()):
        if row["key"] == key:
            return row
    return None


def sent_row(key):
    """The .sent record, else the outbox.jsonl row, else None. A damaged .sent (or history)
    raises SS.StateCorrupt: the caller refuses, it never reads "not posted"."""
    try:
        return SS.load_sent(entry(key, "sent"), key)
    except FileNotFoundError:
        pass
    return find(key)


class locked:
    def __enter__(self):
        os.makedirs(os.path.join(state_dir(), "outbox"), exist_ok=True)
        self.fd = os.open(os.path.join(state_dir(), "outbox.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)
        return False


def read_generation(key):
    """The last generation handed out for key (0 when none). Raises SS.StateCorrupt when corrupt."""
    return SS.load_generation(entry(key, "generation"))


def write_generation(key, gen):
    SS.check_generation_write(entry(key, "generation"), gen)
    tmp = entry(key, "generation.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(f"{gen}\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, entry(key, "generation"))


def write_reservation(key, rec):
    """Create <key>.pending with O_EXCL after validating the reservation (SS.RowRefused, nothing
    created). False when a reservation already exists."""
    why = SS.reservation_problem(rec, key)
    if why:
        raise SS.RowRefused(SS.label(entry(key, "pending")), 1, why)
    try:  # O_EXCL is the guard against a second reservation (or a leftover .pending)
        fd = os.open(entry(key, "pending"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as fh:
        json.dump(rec, fh)
        fh.flush()
        os.fsync(fh.fileno())
    return True


def append_proof(key, proof):
    """Append an owner-gone proof to <key>.pending, bound to its reservation (else SS.RowRefused)."""
    SS.append_row(entry(key, "pending"), proof, SS.pending_append_check(key))


def record_sent(key, rec):
    """<key>.pending -> <key>.sent holding rec, then one outbox.jsonl row (else SS.RowRefused)."""
    why = SS.sent_problem(rec, key)
    if why:
        raise SS.RowRefused(SS.label(entry(key, "sent")), 1, why)
    pending = entry(key, "pending")
    with open(pending, "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.rename(pending, entry(key, "sent"))
    SS.append_row(outbox_path(), rec, SS.check_history_append)


def record_failed(key, rec, reservation):
    """Append the failed row to <key>.pending and rename it to <key>.failed-<UTC> (else SS.RowRefused)."""
    why = SS.failed_problem(rec, reservation)
    if why:
        raise SS.RowRefused(SS.label(entry(key, "pending")), "failed", why)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    pending = entry(key, "pending")
    SS.append_row(pending, rec, SS.pending_append_check(key, failed=True))
    os.rename(pending, entry(key, f"failed-{stamp}"))


def status(key):
    """('USED', row) | ('PENDING', None) | ('UNUSED', None). Call under the lock."""
    row = sent_row(key)
    if row is not None:
        return "USED", row
    if os.path.exists(entry(key, "pending")):
        return "PENDING", None
    return "UNUSED", None


def normalise(text):
    return (text or "").replace("\r\n", "\n").rstrip("\n")


def bad_comment(c):
    """Why this search row can't take part in an absence decision, or None. A row missing
    its body, author login or URL proves nothing, so the whole search is unreadable.
    (search_hits has already refused any row that is not an object.)"""
    if not isinstance(c.get("body"), str):
        return "a row has no string body"
    user = c.get("user")
    if not isinstance(user, dict):
        return "a row has no user object"
    if not isinstance(user.get("login"), str) or not user["login"].strip():
        return "a row has no user login"
    if not isinstance(c.get("html_url"), str) or not c["html_url"].strip():
        return "a row has no html_url"
    return None


def search_hits(search_file, want, login):
    """(comments by login whose body equals want, None), or (None, error). Pure read.
    Fails closed: a blank login, or any row without a string body, a user object with a
    non-empty login and a string html_url, makes the whole search an error, never NOT_FOUND."""
    if not isinstance(login, str) or not login.strip() or re.search(r"\s", login):
        return None, "bad --login (the posting account's login, non-empty, no spaces)"
    try:
        with open(search_file, encoding="utf-8") as fh:
            text = fh.read()
        dec, pos, rows = json.JSONDecoder(), 0, []
        while True:
            while pos < len(text) and text[pos].isspace():
                pos += 1
            if pos >= len(text):
                break
            value, pos = dec.raw_decode(text, pos)
            if not isinstance(value, list):
                return None, "search file is not a list of comments"
            rows.extend(value)
        if rows and all(isinstance(x, list) for x in rows):  # --slurp pages
            rows = [x for page in rows for x in page]
        if not text.strip() or not all(isinstance(x, dict) for x in rows):
            return None, "search file is empty or malformed"
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return None, f"find-post: {type(exc).__name__}"
    for c in rows:
        why = bad_comment(c)
        if why:
            return None, f"search file is incomplete: {why}"
    hits = [c for c in rows if c["user"]["login"] == login
            and normalise(c["body"]) == want]
    return hits, None


def find_post(search_file, body_file, login):
    try:
        want = normalise(read_body(body_file))
    except (OSError, UnicodeDecodeError) as exc:
        return bad(f"find-post: {type(exc).__name__}")
    hits, err = search_hits(search_file, want, login)
    if err:
        return bad(err)
    if len(hits) > 1:
        print(f"MANY {len(hits)} " + " ".join(str(c.get("html_url")) for c in hits))
        return 2
    if hits:
        print(f"FOUND {hits[0].get('html_url')}")
        return 0
    print("NOT_FOUND")
    return 1


def bad(msg):
    print(f"ERROR {msg}")
    return 2


def read_body(path):
    if not path:
        return ""
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Durable outbox with idempotence keys.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("key", "reserve"):
        k = sub.add_parser(name)
        if name == "reserve":
            k.add_argument("key")
        k.add_argument("--pr", required=True)
        k.add_argument("--kind", required=True)
        k.add_argument("--head", required=True)
        k.add_argument("--body-file")
        if name == "reserve":
            k.add_argument("--owner-terminal", required=True)
    sub.add_parser("check").add_argument("key")
    s = sub.add_parser("sent")
    s.add_argument("key")
    s.add_argument("--url", required=True)
    s.add_argument("--generation", type=int, help="reconcile: the generation proved gone")
    og = sub.add_parser("owner-gone")
    og.add_argument("key")
    og.add_argument("--generation", type=int, required=True)
    og.add_argument("--evidence", required=True, metavar="EVIDENCE_FILE")
    sub.add_parser("now")
    f = sub.add_parser("failed")
    f.add_argument("key")
    f.add_argument("--reason", required=True)
    who = f.add_mutually_exclusive_group(required=True)
    who.add_argument("--token")
    who.add_argument("--owner-gone", action="store_true",
                     help="the owner is proven gone (owner-gone), and a later fresh search found no post")
    f.add_argument("--generation", type=int)
    f.add_argument("--search-file")
    f.add_argument("--search-fetched-at")
    f.add_argument("--body-file")
    f.add_argument("--login")
    fp = sub.add_parser("find-post")
    fp.add_argument("--search-file", required=True)
    fp.add_argument("--body-file", required=True)
    fp.add_argument("--login", required=True)
    a = ap.parse_args(argv)

    if a.cmd == "find-post":
        return find_post(a.search_file, a.body_file, a.login)

    if a.cmd == "now":
        print(iso_ns(time.time_ns()))
        return 0

    if a.cmd == "key":
        try:
            body = read_body(a.body_file)
        except (OSError, UnicodeDecodeError) as exc:
            return bad(f"body file: {type(exc).__name__}")
        print(make_key(a.pr, a.kind, a.head, body))
        return 0

    if not KEY_RE.fullmatch(a.key):
        return bad("bad key (want 64 lowercase hex)")

    try:
        if a.cmd == "check":
            with locked():
                word, row = status(a.key)
                if word == "PENDING":
                    try:
                        row, _ = pending_state(a.key)
                    except SS.StateCorrupt as exc:  # PENDING kept: the reservation still blocks
                        print(f"PENDING {a.key} {exc} (the reservation file is damaged: nobody may "
                              f"post or release this key until the conductor repairs it by hand)")
                        return PENDING_EXIT
            if word == "USED":
                print(f"USED {a.key} url={row['url']} at={row['recorded_at']}")
                return 1
            if word == "PENDING":
                print(f"PENDING {a.key} owner={row['owner_terminal']} generation={row['generation']} "
                      f"(a post may have happened: prove the owner gone, then search afresh, then "
                      f"`sent` or `failed` for this generation; or `failed` by the owner's token)")
                return PENDING_EXIT
            print(f"UNUSED {a.key}")
            return 0

        if a.cmd == "reserve":
            try:
                body = read_body(a.body_file)
            except (OSError, UnicodeDecodeError) as exc:
                return bad(f"body file: {type(exc).__name__}")
            if not TERMINAL_RE.fullmatch(a.owner_terminal):
                return bad("bad --owner-terminal (the reserving publisher's CAO terminal id)")
            if make_key(a.pr, a.kind, a.head, body) != a.key:
                print(f"KEY_MISMATCH {a.key} (the body, PR, kind or head changed since the key "
                      f"was made: do not post)")
                return 2
            with locked():
                row = sent_row(a.key)  # a corrupt .sent or history raises: ERROR, nothing reserved
                if row is not None:
                    print(f"USED {a.key} url={row['url']}")
                    return 1
                generation = read_generation(a.key) + 1  # before O_EXCL: a corrupt file leaves no .pending
                token = secrets.token_hex(16)
                rec = {"key": a.key, "pr": str(a.pr), "kind": a.kind, "head": a.head,
                       "reserved_at": now(), "pid": os.getppid(),
                       "owner_terminal": a.owner_terminal, "generation": generation,
                       "token_sha256": hashlib.sha256(token.encode()).hexdigest()}
                if not write_reservation(a.key, rec):  # validated first: a bad row creates nothing
                    print(f"PENDING {a.key} (reconcile before any new attempt)")
                    return PENDING_EXIT
                write_generation(a.key, generation)
            print(f"RESERVED {a.key} token={token}")
            return 0

        if a.cmd == "sent":
            if not a.url.strip() or re.search(r"\s", a.url):
                return bad("bad url")
            with locked():
                prior = sent_row(a.key)
                if prior is not None:
                    print(f"DUPLICATE {a.key} url={prior['url']}")
                    return 1
                pending = entry(a.key, "pending")
                if not os.path.exists(pending):
                    print(f"NOT_RESERVED {a.key} url={a.url} (nothing was reserved; tell the conductor)")
                    return 1
                try:
                    gen = pending_state(a.key)[0]["generation"]
                except SS.StateCorrupt as exc:  # PENDING kept
                    print(f"REFUSED {a.key} {exc}: nothing changed (the reservation stays PENDING)")
                    return 1
                if a.generation is not None and a.generation != gen:
                    print(f"REFUSED {a.key} generation_mismatch pending={gen} given={a.generation}: "
                          f"nothing changed")
                    return 1
                record_sent(a.key, {"key": a.key, "url": a.url, "recorded_at": now(), "generation": gen})
            print(f"RECORDED {a.key}")
            return 0

        if a.cmd == "owner-gone":
            with locked():
                pending = entry(a.key, "pending")
                if not os.path.exists(pending):
                    print(f"NOT_RESERVED {a.key}")
                    return 1
                try:
                    proof, why = prove_gone(a.key, a.generation, a.evidence)
                except SS.StateCorrupt as exc:  # PENDING kept
                    proof, why = None, str(exc)
                if proof is None:
                    print(f"REFUSED {a.key} {why}: the owner may still post; nothing changed")
                    return 1
                append_proof(a.key, proof)
            print(f"OWNER_GONE {a.key} generation={proof['generation']} at={proof['proven_at']} "
                  f"(now search afresh; an earlier search proves nothing)")
            return 0

        # failed
        if not WORD_RE.fullmatch(a.reason):
            return bad("bad reason (one word: [A-Za-z0-9_.:-]{1,64})")
        if a.owner_gone and None in (a.generation, a.search_file, a.search_fetched_at, a.body_file, a.login):
            return bad("--owner-gone needs --generation, --search-file, --search-fetched-at, "
                       "--body-file and --login")
        with locked():
            pending = entry(a.key, "pending")
            if not os.path.exists(pending):
                print(f"NOT_RESERVED {a.key}")
                return 1
            try:
                how, evidence = owner_release(a.key, a.token, a)
            except SS.StateCorrupt as exc:  # PENDING kept
                how, evidence = None, str(exc)
            if how is None:
                print(f"REFUSED {a.key} {evidence}: the owner may still post; nothing changed")
                return 1
            record_failed(a.key, {"failed_at": now(), "reason": a.reason, "by": how, "evidence": evidence},
                          pending_state(a.key)[0])
        print(f"FAILED {a.key} reason={a.reason}")
        return 0
    except ValueError as exc:  # SS.StateCorrupt / SS.RowRefused too: ERROR, nothing changed
        return bad(str(exc))

if __name__ == "__main__":
    sys.exit(main())
