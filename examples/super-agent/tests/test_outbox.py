import json
import os
import re
import subprocess
import sys

import pytest

from conftest import BIN, base_env, run


def key(tmp_path, body="Fixed both P3s in c0de0001.\n", pr=101, kind="reply", head="c0de0001"):
    f = tmp_path / f"body-{abs(hash((body, pr, kind, head)))}.md"
    f.write_bytes(body.encode())
    r = run("outbox.py", "key", "--pr", pr, "--kind", kind, "--head", head, "--body-file", f,
            env=base_env(tmp_path))
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_key_is_sha256_and_deterministic(tmp_path):
    k = key(tmp_path)
    assert re.fullmatch(r"[0-9a-f]{64}", k)
    assert key(tmp_path) == k


def test_key_changes_with_each_input(tmp_path):
    k = key(tmp_path)
    assert key(tmp_path, body="other\n") != k
    assert key(tmp_path, pr=102) != k
    assert key(tmp_path, kind="push") != k
    assert key(tmp_path, head="c0de0002") != k


def test_key_ignores_crlf_and_trailing_newlines(tmp_path):
    assert key(tmp_path, body="a\r\nb\n\n") == key(tmp_path, body="a\nb")


def test_key_fields_cannot_collide_by_shifting(tmp_path):
    assert key(tmp_path, pr=12, kind="3reply") != key(tmp_path, pr=123, kind="reply")


def test_key_without_body_file(tmp_path):
    r = run("outbox.py", "key", "--pr", 1, "--kind", "push", "--head", "abc", env=base_env(tmp_path))
    assert r.returncode == 0 and re.fullmatch(r"[0-9a-f]{64}", r.stdout.strip())


BODY = "Fixed both P3s in c0de0001.\n"


def body_file(tmp_path, body=BODY):
    f = tmp_path / "reply.md"
    f.write_bytes(body.encode())
    return f


OWNER_A = "aaaa1111"


def reserve(tmp_path, k, body=BODY, pr=101, kind="reply", head="c0de0001", env=None, owner=OWNER_A):
    return run("outbox.py", "reserve", k, "--pr", pr, "--kind", kind, "--head", head,
               "--body-file", body_file(tmp_path, body), "--owner-terminal", owner,
               env=env or base_env(tmp_path))


def token_of(r):
    m = re.fullmatch(r"RESERVED [0-9a-f]{64} token=([0-9a-f]{32})", r.stdout.strip())
    assert m, r.stdout
    return m.group(1)


def ob(tmp_path, *args):
    return run("outbox.py", *args, env=base_env(tmp_path))


URL = "https://github.com/your-org/your-repo/pull/101#issuecomment-1"


def test_reserve_then_sent_then_used(tmp_path):
    k = key(tmp_path)
    assert ob(tmp_path, "check", k).returncode == 0
    r = reserve(tmp_path, k)
    assert r.returncode == 0 and r.stdout.startswith("RESERVED"), r.stdout
    assert (tmp_path / "state" / "outbox" / f"{k}.pending").exists()
    r = ob(tmp_path, "sent", k, "--url", URL)
    assert r.returncode == 0 and r.stdout.startswith("RECORDED"), r.stdout
    assert not (tmp_path / "state" / "outbox" / f"{k}.pending").exists()
    sent = json.loads((tmp_path / "state" / "outbox" / f"{k}.sent").read_text())
    assert sent["url"] == URL
    c = ob(tmp_path, "check", k)
    assert c.returncode == 1 and "issuecomment-1" in c.stdout
    r = reserve(tmp_path, k)
    assert r.returncode == 1 and r.stdout.startswith("USED")


def test_second_reserve_is_refused_while_pending(tmp_path):
    k = key(tmp_path)
    assert reserve(tmp_path, k).returncode == 0
    r = reserve(tmp_path, k)
    assert r.returncode == 4 and r.stdout.startswith("PENDING"), r.stdout


def test_two_claimants_at_once_only_one_wins(tmp_path):
    k = key(tmp_path)
    f = body_file(tmp_path)
    env = base_env(tmp_path)
    procs = [subprocess.Popen([sys.executable, "-I", str(BIN / "outbox.py"), "reserve", k,
                               "--pr", "101", "--kind", "reply", "--head", "c0de0001",
                               "--body-file", str(f), "--owner-terminal", OWNER_A],
                              stdout=subprocess.PIPE, text=True, env=env)
             for _ in range(6)]
    outs = [(p.wait(timeout=60), p.stdout.read()) for p in procs]
    for p in procs:
        p.stdout.close()
    winners = [o for rc, o in outs if rc == 0]
    assert len(winners) == 1 and winners[0].startswith("RESERVED"), outs
    assert all(rc == 4 and o.startswith("PENDING") for rc, o in outs if rc != 0), outs


def test_crash_after_post_blocks_until_reconciled_as_sent(tmp_path):
    k = key(tmp_path)
    assert reserve(tmp_path, k).returncode == 0
    # the publisher posted, then died before `sent`
    c = ob(tmp_path, "check", k)
    assert c.returncode == 4 and c.stdout.startswith("PENDING")
    assert reserve(tmp_path, k).returncode == 4
    # the exact read-only search found the post: record it
    assert ob(tmp_path, "sent", k, "--url", URL).returncode == 0
    assert ob(tmp_path, "check", k).returncode == 1
    assert reserve(tmp_path, k).returncode == 1


def test_crash_before_post_reconciled_as_failed_allows_a_new_attempt(tmp_path):
    k = key(tmp_path)
    tok = token_of(reserve(tmp_path, k))
    r = ob(tmp_path, "failed", k, "--reason", "search_found_no_post", "--token", tok)
    assert r.returncode == 0 and r.stdout.startswith("FAILED"), r.stdout
    assert ob(tmp_path, "check", k).returncode == 0
    assert reserve(tmp_path, k).returncode == 0
    kept = [p.name for p in (tmp_path / "state" / "outbox").iterdir() if ".failed-" in p.name]
    assert len(kept) == 1


def test_sent_without_reservation_is_refused(tmp_path):
    k = key(tmp_path)
    r = ob(tmp_path, "sent", k, "--url", URL)
    assert r.returncode == 1 and r.stdout.startswith("NOT_RESERVED") and URL in r.stdout
    assert ob(tmp_path, "check", k).returncode == 0


def test_sent_twice_is_a_duplicate(tmp_path):
    k = key(tmp_path)
    reserve(tmp_path, k)
    assert ob(tmp_path, "sent", k, "--url", "u1").returncode == 0
    r = ob(tmp_path, "sent", k, "--url", "u2")
    assert r.returncode == 1 and "DUPLICATE" in r.stdout
    assert len((tmp_path / "state" / "outbox.jsonl").read_text().splitlines()) == 1


def test_failed_without_reservation_is_refused(tmp_path):
    k = key(tmp_path)
    r = ob(tmp_path, "failed", k, "--reason", "x", "--token", "0" * 32)
    assert (r.returncode, r.stdout) == (1, f"NOT_RESERVED {k}\n")
    r = failed_gone(tmp_path, k, search_file(tmp_path), "2999-01-01T00:00:00+00:00")
    assert (r.returncode, r.stdout) == (1, f"NOT_RESERVED {k}\n")
    r = owner_gone(tmp_path, k)
    assert (r.returncode, r.stdout) == (1, f"NOT_RESERVED {k}\n")


def test_reserve_rederives_the_key_from_the_body(tmp_path):
    k = key(tmp_path)
    r = reserve(tmp_path, k, body="Fixed both P3s in c0de0001. Also P1.\n")
    assert r.returncode == 2 and r.stdout.startswith("KEY_MISMATCH")
    assert not (tmp_path / "state" / "outbox" / f"{k}.pending").exists()


@pytest.mark.parametrize("field", ["pr", "kind", "head"])
def test_reserve_rederives_the_key_from_every_field(tmp_path, field):
    k = key(tmp_path)
    over = {"pr": 102, "kind": "push", "head": "c0de0002"}
    r = reserve(tmp_path, k, **{field: over[field]})
    assert r.returncode == 2 and r.stdout.startswith("KEY_MISMATCH")


def test_sent_appends_jsonl_history(tmp_path):
    k1, k2 = key(tmp_path), key(tmp_path, body="second\n")
    reserve(tmp_path, k1)
    ob(tmp_path, "sent", k1, "--url", "u1")
    reserve(tmp_path, k2, body="second\n")
    ob(tmp_path, "sent", k2, "--url", "u2")
    rows = [json.loads(x) for x in (tmp_path / "state" / "outbox.jsonl").read_text().splitlines()]
    assert [r["key"] for r in rows] == [k1, k2] and [r["url"] for r in rows] == ["u1", "u2"]
    assert all(r["recorded_at"] for r in rows)


def test_history_row_alone_counts_as_used(tmp_path):
    k = key(tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "outbox.jsonl").write_text(json.dumps(
        {"key": k, "url": "old", "recorded_at": "2026-11-08T12:00:00+00:00", "generation": 1}) + "\n")
    assert ob(tmp_path, "check", k).returncode == 1
    assert reserve(tmp_path, k).returncode == 1


def test_malformed_key_is_an_error(tmp_path):
    for args in (("check", "not-a-key"), ("sent", "zz", "--url", "u"), ("failed", "zz", "--reason", "x", "--token", "0" * 32)):
        r = ob(tmp_path, *args)
        assert r.returncode == 2 and r.stdout.startswith("ERROR bad key")
    r = reserve(tmp_path, "zz")
    assert r.returncode == 2 and r.stdout.startswith("ERROR bad key")
    assert not (tmp_path / "state" / "outbox.jsonl").exists()


def test_sent_requires_url(tmp_path):
    assert ob(tmp_path, "sent", key(tmp_path)).returncode == 2


def test_corrupt_outbox_line_fails_closed(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "outbox.jsonl").write_text("{broken\n")
    assert ob(tmp_path, "check", key(tmp_path)).returncode == 2
    assert reserve(tmp_path, key(tmp_path)).returncode == 2


# --- reconcile search (the publisher's exact read-only step) ---

def search(tmp_path, comments, raw=None, body=BODY, login="your-login"):
    sf = tmp_path / "search.json"
    sf.write_text(raw if raw is not None else json.dumps(comments))
    return ob(tmp_path, "find-post", "--search-file", sf, "--body-file", body_file(tmp_path, body),
              "--login", login)


def c(body, login="your-login", url="https://x/c/1"):
    return {"user": {"login": login}, "body": body, "html_url": url}


def test_find_post_found(tmp_path):
    r = search(tmp_path, [c("other"), c("Fixed both P3s in c0de0001.\r\n", url="https://x/c/9")])
    assert r.returncode == 0 and r.stdout.strip() == "FOUND https://x/c/9"


def test_find_post_ignores_other_authors(tmp_path):
    r = search(tmp_path, [c(BODY, login="maintainer-login")])
    assert r.returncode == 1 and r.stdout.strip() == "NOT_FOUND"


def test_find_post_needs_the_exact_body(tmp_path):
    assert search(tmp_path, [c("Fixed both P3s in c0de0001. Thanks")]).returncode == 1


def test_find_post_reads_slurped_and_concatenated_pages(tmp_path):
    raw = json.dumps([[c("a")], [c(BODY, url="https://x/c/2")]])
    assert search(tmp_path, None, raw=raw).stdout.strip() == "FOUND https://x/c/2"
    raw = json.dumps([c("a")]) + "\n" + json.dumps([c(BODY, url="https://x/c/3")])
    assert search(tmp_path, None, raw=raw).stdout.strip() == "FOUND https://x/c/3"


def test_find_post_many_is_an_error(tmp_path):
    r = search(tmp_path, [c(BODY, url="u1"), c(BODY, url="u2")])
    assert r.returncode == 2 and r.stdout.startswith("MANY 2")


@pytest.mark.parametrize("raw", ["", "{broken", '{"message": "Not Found"}', "[1]"])
def test_find_post_bad_search_file_is_an_error(tmp_path, raw):
    assert search(tmp_path, None, raw=raw).returncode == 2


def test_reserve_waits_for_the_outbox_lock(tmp_path):
    import fcntl
    import time
    k = key(tmp_path)
    (tmp_path / "state").mkdir(exist_ok=True)
    fd = os.open(tmp_path / "state" / "outbox.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        p = subprocess.Popen([sys.executable, "-I", str(BIN / "outbox.py"), "reserve", k, "--pr", "101",
                              "--kind", "reply", "--head", "c0de0001", "--body-file", str(body_file(tmp_path)),
                              "--owner-terminal", OWNER_A],
                             stdout=subprocess.PIPE, text=True, env=base_env(tmp_path))
        time.sleep(1.0)
        assert p.poll() is None, "reserve ran while another process held state/outbox.lock"
        assert not (tmp_path / "state" / "outbox" / f"{k}.pending").exists()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert p.wait(timeout=60) == 0 and p.stdout.read().startswith("RESERVED")
    p.stdout.close()


# --- nobody but the owner, or a proven-gone owner, can release a live reservation ---

def gone_file(tmp_path, owner=OWNER_A, code="404", name="gone.txt"):
    """What the template's exact curl writes: '%{url_effective} %{http_code}'."""
    f = tmp_path / name
    f.write_text(f"http://localhost:9889/terminals/{owner} {code}")
    return f


def search_file(tmp_path, rows=(), name="search.json"):
    f = tmp_path / name
    f.write_text(json.dumps([list(rows)]))
    return f


def owner_gone(tmp_path, k, gen=1, f=None):
    return ob(tmp_path, "owner-gone", k, "--generation", gen, "--evidence", f or gone_file(tmp_path))


def fresh_search(tmp_path, rows=(), name="search-fresh.json"):
    """The template's R4: `outbox.py now` right before the fetch, then the search file."""
    r = ob(tmp_path, "now")
    assert r.returncode == 0, r.stdout
    return search_file(tmp_path, rows, name), r.stdout.strip()


def failed_gone(tmp_path, k, sf, fetched, gen=1, body=BODY, login="your-login"):
    return ob(tmp_path, "failed", k, "--reason", "search_found_no_post", "--owner-gone", "--generation", gen,
              "--search-file", sf, "--search-fetched-at", fetched, "--body-file", body_file(tmp_path, body),
              "--login", login)


def test_d5_live_publisher_keeps_its_reservation(tmp_path):
    """A reserves and has not posted; B sees PENDING, searches (empty) -> NOT_FOUND and
    tries `failed`; C must not get RESERVED while A may still post."""
    k = key(tmp_path)
    tok_a = token_of(reserve(tmp_path, k))                                  # A
    c = ob(tmp_path, "check", k)                                            # B
    assert c.returncode == 4 and f"owner={OWNER_A}" in c.stdout, c.stdout
    fp = ob(tmp_path, "find-post", "--search-file", search_file(tmp_path), "--body-file",
            body_file(tmp_path), "--login", "your-login")
    assert (fp.returncode, fp.stdout.strip()) == (1, "NOT_FOUND")
    no_proof = ob(tmp_path, "failed", k, "--reason", "search_found_no_post")
    assert no_proof.returncode == 2                                         # neither token nor proof
    for f in (gone_file(tmp_path, code="200"), gone_file(tmp_path, owner="bbbb2222", name="other.txt"),
              gone_file(tmp_path, code="000", name="down.txt")):
        r = owner_gone(tmp_path, k, f=f)
        assert r.returncode == 1 and r.stdout.startswith(f"REFUSED {k} owner_not_proven_gone"), r.stdout
    sf, fetched = fresh_search(tmp_path)
    r = failed_gone(tmp_path, k, sf, fetched)                               # no proof recorded
    assert r.returncode == 1 and r.stdout.startswith(f"REFUSED {k} owner_not_proven_gone"), r.stdout
    r = ob(tmp_path, "failed", k, "--reason", "search_found_no_post", "--token", "f" * 32)
    assert r.returncode == 1 and "not_owner" in r.stdout
    r = reserve(tmp_path, k, owner="cccc3333")                              # C
    assert r.returncode == 4 and r.stdout.startswith("PENDING"), r.stdout
    assert ob(tmp_path, "sent", k, "--url", URL).returncode == 0           # A posts and records
    assert reserve(tmp_path, k, owner="cccc3333").returncode == 1          # C: USED, never posts
    assert tok_a


def test_d5_owner_releases_with_its_token(tmp_path):
    k = key(tmp_path)
    tok = token_of(reserve(tmp_path, k))
    r = ob(tmp_path, "failed", k, "--reason", "post_failed", "--token", tok)
    assert r.returncode == 0 and r.stdout == f"FAILED {k} reason=post_failed\n"
    rec = [p for p in (tmp_path / "state" / "outbox").iterdir() if ".failed-" in p.name][0]
    assert '"by": "owner_token"' in rec.read_text()
    assert token_of(reserve(tmp_path, k, owner="cccc3333"))


def test_d5_gone_owner_can_be_released_with_recorded_evidence(tmp_path):
    k = key(tmp_path)
    token_of(reserve(tmp_path, k))
    r = owner_gone(tmp_path, k)
    assert r.returncode == 0 and r.stdout.startswith(f"OWNER_GONE {k} generation=1 at="), r.stdout
    sf, fetched = fresh_search(tmp_path)
    r = failed_gone(tmp_path, k, sf, fetched)
    assert r.returncode == 0 and r.stdout.startswith("FAILED"), r.stdout
    rec = [p for p in (tmp_path / "state" / "outbox").iterdir() if ".failed-" in p.name][0].read_text()
    last = json.loads(rec.strip().splitlines()[-1])
    assert last["by"] == "owner_gone" and last["evidence"]["generation"] == 1
    assert last["evidence"]["proof"]["evidence"] == f"http://localhost:9889/terminals/{OWNER_A} 404"
    assert last["evidence"]["search_fetched_at"] == fetched
    assert token_of(reserve(tmp_path, k, owner="cccc3333"))


def test_d5_token_is_unique_and_not_stored_in_clear(tmp_path):
    k1, k2 = key(tmp_path), key(tmp_path, body="other\n", pr=102)
    t1 = token_of(reserve(tmp_path, k1))
    t2 = token_of(reserve(tmp_path, k2, body="other\n", pr=102))
    assert t1 != t2
    pend = (tmp_path / "state" / "outbox" / f"{k1}.pending").read_text()
    assert t1 not in pend and json.loads(pend)["owner_terminal"] == OWNER_A


@pytest.mark.parametrize("owner", ["", "a b", "x" * 65, "../x"])
def test_d5_reserve_needs_a_valid_owner_terminal(tmp_path, owner):
    k = key(tmp_path)
    r = reserve(tmp_path, k, owner=owner)
    assert r.returncode == 2 and not (tmp_path / "state" / "outbox" / f"{k}.pending").exists()
    assert r.stdout.startswith("ERROR bad --owner-terminal"), r.stdout


def test_d5_unreadable_reservation_needs_the_token(tmp_path):
    k = key(tmp_path)
    token_of(reserve(tmp_path, k))
    (tmp_path / "state" / "outbox" / f"{k}.pending").write_text("{broken")
    want = f"REFUSED {k} state_corrupt:outbox/{k}.pending:1:row:not_json: "
    r = owner_gone(tmp_path, k)
    assert r.returncode == 1 and r.stdout.startswith(want), r.stdout
    sf, fetched = fresh_search(tmp_path)
    r = failed_gone(tmp_path, k, sf, fetched)
    assert r.returncode == 1 and r.stdout.startswith(want), r.stdout
    c = ob(tmp_path, "check", k)
    assert c.returncode == 4 and c.stdout.startswith(f"PENDING {k} state_corrupt:outbox/{k}.pending:1:"), c.stdout


def test_d5_each_reservation_records_its_own_owner(tmp_path):
    k = key(tmp_path)
    token_of(reserve(tmp_path, k, owner="dddd4444"))
    c = ob(tmp_path, "check", k)
    assert c.returncode == 4 and c.stdout.startswith(f"PENDING {k} owner=dddd4444 "), c.stdout
    r = owner_gone(tmp_path, k)                                            # aaaa1111 gone
    assert r.returncode == 1 and "owner_not_proven_gone owner=dddd4444" in r.stdout, r.stdout
    r = owner_gone(tmp_path, k, f=gone_file(tmp_path, owner="dddd4444", name="d.txt"))
    assert r.returncode == 0 and r.stdout.startswith("OWNER_GONE"), r.stdout
    sf, fetched = fresh_search(tmp_path)
    r = failed_gone(tmp_path, k, sf, fetched)
    assert r.returncode == 0 and r.stdout.startswith("FAILED"), r.stdout


# --- reconcile only after the owner is proven gone, with a fresh, later search ---

POST_URL = "https://github.com/your-org/your-repo/pull/101#issuecomment-77"


def a_post():
    return c(BODY, url=POST_URL)


def pending_lines(tmp_path, k):
    return (tmp_path / "state" / "outbox" / f"{k}.pending").read_text().splitlines()


def test_f3_search_before_the_owner_died_never_releases_the_key(tmp_path):
    """A reserves; B searches (NOT_FOUND); A posts and dies before `sent`; B proves A gone.
    B's old search must not release the key. A fresh search after the proof finds A's post and
    resolves the same generation as sent; C is then refused."""
    k = key(tmp_path)
    token_of(reserve(tmp_path, k))                                          # A
    chk = ob(tmp_path, "check", k)                                          # B
    assert chk.returncode == 4 and f"owner={OWNER_A} generation=1 " in chk.stdout, chk.stdout
    old_sf, old_fetched = fresh_search(tmp_path, name="search-old.json")  # B, while A is alive
    fp = ob(tmp_path, "find-post", "--search-file", old_sf, "--body-file", body_file(tmp_path), "--login", "your-login")
    assert (fp.returncode, fp.stdout.strip()) == (1, "NOT_FOUND")
    # A posts (GitHub now has the comment), then dies before `sent`.
    r = owner_gone(tmp_path, k)                                             # B: 404, recorded
    assert r.returncode == 0 and r.stdout.startswith(f"OWNER_GONE {k} generation=1 at="), r.stdout
    r = failed_gone(tmp_path, k, old_sf, old_fetched)                       # the old search: refused
    assert (r.returncode, r.stdout) == (1, f"REFUSED {k} search_before_proof:fetched_at (search again, "
                                           f"after the owner-gone proof): the owner may still post; nothing changed\n")
    r = failed_gone(tmp_path, k, old_sf, ob(tmp_path, "now").stdout.strip())  # a new time, the old file
    assert r.returncode == 1 and f"REFUSED {k} search_before_proof:file_written" in r.stdout, r.stdout
    assert ob(tmp_path, "check", k).returncode == 4
    assert reserve(tmp_path, k, owner="cccc3333").returncode == 4           # C still blocked
    new_sf, new_fetched = fresh_search(tmp_path, [c("other"), a_post()])    # B: the fresh search
    r = failed_gone(tmp_path, k, new_sf, new_fetched)                       # it found the post
    assert r.returncode == 1 and f"REFUSED {k} search_found_post" in r.stdout, r.stdout
    fp = ob(tmp_path, "find-post", "--search-file", new_sf, "--body-file", body_file(tmp_path), "--login", "your-login")
    assert (fp.returncode, fp.stdout.strip()) == (0, f"FOUND {POST_URL}")
    r = ob(tmp_path, "sent", k, "--url", POST_URL, "--generation", 1)
    assert (r.returncode, r.stdout) == (0, f"RECORDED {k}\n")
    sent = json.loads((tmp_path / "state" / "outbox" / f"{k}.sent").read_text())
    assert sent["url"] == POST_URL and sent["generation"] == 1
    r = reserve(tmp_path, k, owner="cccc3333")                              # C: the key is used
    assert r.returncode == 1 and r.stdout.startswith(f"USED {k} url={POST_URL}"), r.stdout


def test_f3_fresh_empty_search_after_the_proof_releases_that_generation(tmp_path):
    k = key(tmp_path)
    token_of(reserve(tmp_path, k))
    assert owner_gone(tmp_path, k).returncode == 0
    sf, fetched = fresh_search(tmp_path, [c("other"), c(BODY, login="maintainer-login")])
    r = failed_gone(tmp_path, k, sf, fetched)
    assert (r.returncode, r.stdout) == (0, f"FAILED {k} reason=search_found_no_post\n")
    assert ob(tmp_path, "check", k).returncode == 0


def test_f3_generation_increases_per_reservation(tmp_path):
    k = key(tmp_path)
    tok = token_of(reserve(tmp_path, k))
    assert "generation=1 " in ob(tmp_path, "check", k).stdout
    assert ob(tmp_path, "failed", k, "--reason", "post_failed", "--token", tok).returncode == 0
    token_of(reserve(tmp_path, k, owner="dddd4444"))
    assert f"owner=dddd4444 generation=2 " in ob(tmp_path, "check", k).stdout
    assert json.loads(pending_lines(tmp_path, k)[0])["generation"] == 2
    assert (tmp_path / "state" / "outbox" / f"{k}.generation").read_text() == "2\n"


def test_f3_another_generation_is_never_resolved(tmp_path):
    """B proved generation 1's owner gone; generation 1 was resolved and D reserved generation 2.
    B's generation-1 steps must not touch D's reservation."""
    k = key(tmp_path)
    tok = token_of(reserve(tmp_path, k))
    assert owner_gone(tmp_path, k).returncode == 0                          # B, generation 1
    assert ob(tmp_path, "failed", k, "--reason", "post_failed", "--token", tok).returncode == 0
    token_of(reserve(tmp_path, k, owner="dddd4444"))                        # D, generation 2
    dgone = gone_file(tmp_path, owner="dddd4444", name="d.txt")
    r = owner_gone(tmp_path, k, gen=1, f=dgone)
    assert (r.returncode, r.stdout.startswith(f"REFUSED {k} generation_mismatch pending=2 given=1")) == (1, True), r.stdout
    assert owner_gone(tmp_path, k, gen=2, f=dgone).returncode == 0          # D proven gone too
    sf, fetched = fresh_search(tmp_path)
    r = failed_gone(tmp_path, k, sf, fetched, gen=1)
    assert r.returncode == 1 and "generation_mismatch pending=2 given=1" in r.stdout, r.stdout
    r = ob(tmp_path, "sent", k, "--url", POST_URL, "--generation", 1)
    assert r.returncode == 1 and r.stdout.startswith(f"REFUSED {k} generation_mismatch"), r.stdout
    assert ob(tmp_path, "check", k).returncode == 4                         # unchanged
    assert failed_gone(tmp_path, k, sf, fetched, gen=2).returncode == 0


def test_f3_the_latest_proof_counts(tmp_path):
    k = key(tmp_path)
    token_of(reserve(tmp_path, k))
    assert owner_gone(tmp_path, k).returncode == 0
    sf, fetched = fresh_search(tmp_path)
    assert owner_gone(tmp_path, k).returncode == 0                          # a second, later proof
    r = failed_gone(tmp_path, k, sf, fetched)
    assert r.returncode == 1 and "search_before_proof:fetched_at" in r.stdout, r.stdout
    sf, fetched = fresh_search(tmp_path, name="again.json")
    assert failed_gone(tmp_path, k, sf, fetched).returncode == 0


def test_f3_body_must_be_the_reserved_post(tmp_path):
    """Another body file would make any search NOT_FOUND."""
    k = key(tmp_path)
    token_of(reserve(tmp_path, k))
    assert owner_gone(tmp_path, k).returncode == 0
    sf, fetched = fresh_search(tmp_path, [a_post()])
    r = failed_gone(tmp_path, k, sf, fetched, body="something else\n")
    assert r.returncode == 1 and f"REFUSED {k} body_not_this_key" in r.stdout, r.stdout


@pytest.mark.parametrize("raw", ["", "{broken", '{"message": "Not Found"}', "[1]"])
def test_f3_unreadable_search_never_releases(tmp_path, raw):
    k = key(tmp_path)
    token_of(reserve(tmp_path, k))
    assert owner_gone(tmp_path, k).returncode == 0
    sf, fetched = fresh_search(tmp_path)
    sf.write_text(raw)
    r = failed_gone(tmp_path, k, sf, fetched)
    assert r.returncode == 1 and f"REFUSED {k} search_unreadable" in r.stdout, r.stdout


@pytest.mark.parametrize("fetched", ["2999-01-01T00:00:00", "yesterday", ""])
def test_f3_fetch_time_needs_a_zone(tmp_path, fetched):
    k = key(tmp_path)
    token_of(reserve(tmp_path, k))
    assert owner_gone(tmp_path, k).returncode == 0
    sf, _ = fresh_search(tmp_path)
    r = failed_gone(tmp_path, k, sf, fetched)
    assert r.returncode == 1 and f"REFUSED {k} bad --search-fetched-at" in r.stdout, r.stdout


@pytest.mark.parametrize("drop", ["--generation", "--search-file", "--search-fetched-at", "--body-file", "--login"])
def test_f3_owner_gone_release_needs_every_input(tmp_path, drop):
    k = key(tmp_path)
    token_of(reserve(tmp_path, k))
    assert owner_gone(tmp_path, k).returncode == 0
    sf, fetched = fresh_search(tmp_path)
    args = {"--generation": "1", "--search-file": str(sf), "--search-fetched-at": fetched,
            "--body-file": str(body_file(tmp_path)), "--login": "your-login"}
    del args[drop]
    r = ob(tmp_path, "failed", k, "--reason", "x", "--owner-gone", *[x for kv in args.items() for x in kv])
    assert r.returncode == 2 and r.stdout.startswith("ERROR --owner-gone needs"), r.stdout
    assert ob(tmp_path, "check", k).returncode == 4


def test_f3_old_evidence_file_interface_is_gone(tmp_path):
    """The the old one-step `failed --owner-gone FILE` no longer exists."""
    k = key(tmp_path)
    token_of(reserve(tmp_path, k))
    r = ob(tmp_path, "failed", k, "--reason", "x", "--owner-gone", gone_file(tmp_path))
    assert r.returncode == 2 and ob(tmp_path, "check", k).returncode == 4


@pytest.mark.parametrize("line", ['{"owner_gone": true, "generation": 1}',
                                  '{"owner_gone": true, "generation": 1, "proven_ns": "1"}',
                                  '{"owner_gone": true, "generation": 1, "proven_ns": true}',
                                  '{"owner_gone": false, "generation": 1, "proven_ns": 1}'])
def test_f3_malformed_proof_lines_prove_nothing(tmp_path, line):
    k = key(tmp_path)
    token_of(reserve(tmp_path, k))
    with open(tmp_path / "state" / "outbox" / f"{k}.pending", "a") as fh:
        fh.write("\n" + line)
    sf, fetched = fresh_search(tmp_path)
    r = failed_gone(tmp_path, k, sf, fetched)
    assert r.returncode == 1 and r.stdout.startswith(f"REFUSED {k} state_corrupt:outbox/{k}.pending:2:"), r.stdout


@pytest.mark.parametrize("gen", [None, True, "1", 0])
def test_f3_reservation_without_a_valid_generation_needs_the_token(tmp_path, gen):
    k = key(tmp_path)
    token_of(reserve(tmp_path, k))
    p = tmp_path / "state" / "outbox" / f"{k}.pending"
    row = json.loads(p.read_text())
    if gen is None:
        del row["generation"]
    else:
        row["generation"] = gen
    p.write_text(json.dumps(row))
    r = owner_gone(tmp_path, k)
    why = {None: "missing", True: "type", "1": "type", 0: "range"}[gen]
    assert r.returncode == 1 and r.stdout.startswith(
        f"REFUSED {k} state_corrupt:outbox/{k}.pending:1:generation:{why}: "), r.stdout


def test_f3_corrupt_generation_file_fails_closed(tmp_path):
    k = key(tmp_path)
    (tmp_path / "state" / "outbox").mkdir(parents=True)
    (tmp_path / "state" / "outbox" / f"{k}.generation").write_text("two\n")
    r = reserve(tmp_path, k)
    assert (r.returncode, r.stdout.strip()) == (2, f"ERROR state_corrupt:outbox/{k}.generation:1:generation:format")
    assert not (tmp_path / "state" / "outbox" / f"{k}.pending").exists()


def test_f3_now_prints_a_zoned_microsecond_time(tmp_path):
    from datetime import datetime, timezone
    out = ob(tmp_path, "now").stdout.strip()
    dt = datetime.fromisoformat(out)
    assert dt.tzinfo is not None and dt.utcoffset().total_seconds() == 0 and re.search(r"\.\d{6}\+00:00$", out)
    assert abs((datetime.now(timezone.utc) - dt).total_seconds()) < 60


def test_f3_owner_sent_needs_no_generation(tmp_path):
    k = key(tmp_path)
    token_of(reserve(tmp_path, k))
    assert ob(tmp_path, "sent", k, "--url", URL).returncode == 0
    assert json.loads((tmp_path / "state" / "outbox" / f"{k}.sent").read_text())["generation"] == 1


# --- an incomplete search row or a blank login is unreadable, never "no post" ---

def f4_release(tmp_path, rows, login="your-login"):
    """The sequence: A reserves, a valid generation-1 owner-gone proof, then a fresh search
    holding `rows`; returns (find-post, failed --owner-gone, C's reserve)."""
    k = key(tmp_path)
    token_of(reserve(tmp_path, k))                                          # A
    assert owner_gone(tmp_path, k).returncode == 0
    sf, fetched = fresh_search(tmp_path, rows)
    fp = ob(tmp_path, "find-post", "--search-file", sf, "--body-file", body_file(tmp_path), "--login", login)
    rel = failed_gone(tmp_path, k, sf, fetched, login=login)
    chk = ob(tmp_path, "check", k)
    return k, fp, rel, chk


F4_PROBES = {
    "empty_row": [{}],
    "exact_body_user_empty": [{"user": {}, "body": BODY, "html_url": POST_URL}],
    "exact_body_user_null": [{"user": None, "body": BODY, "html_url": POST_URL}],
    "exact_body_no_user": [{"body": BODY, "html_url": POST_URL}],
    "exact_body_login_null": [{"user": {"login": None}, "body": BODY, "html_url": POST_URL}],
    "exact_body_login_empty": [{"user": {"login": ""}, "body": BODY, "html_url": POST_URL}],
    "exact_body_login_blank": [{"user": {"login": "  "}, "body": BODY, "html_url": POST_URL}],
    "exact_body_login_int": [{"user": {"login": 7}, "body": BODY, "html_url": POST_URL}],
    "exact_body_user_str": [{"user": "your-login", "body": BODY, "html_url": POST_URL}],
    "body_null": [{"user": {"login": "your-login"}, "body": None, "html_url": POST_URL}],
    "body_missing": [{"user": {"login": "your-login"}, "html_url": POST_URL}],
    "body_list": [{"user": {"login": "your-login"}, "body": [BODY], "html_url": POST_URL}],
    "url_missing": [{"user": {"login": "your-login"}, "body": BODY}],
    "url_null": [{"user": {"login": "your-login"}, "body": BODY, "html_url": None}],
    "url_empty": [{"user": {"login": "your-login"}, "body": BODY, "html_url": ""}],
    "bad_row_after_good_ones": [c("other"), c(BODY, login="maintainer-login"), {}],
    "other_body_user_null": [{"user": None, "body": "other", "html_url": "https://x/c/2"}],
}


@pytest.mark.parametrize("probe", sorted(F4_PROBES))
def test_f4_incomplete_search_row_never_releases_the_key(tmp_path, probe):
    """[{}], or the exact posted body with user={} / user=null, gave NOT_FOUND -> FAILED -> C
    RESERVED. Any row that can't be judged makes the whole search an ERROR; C stays PENDING."""
    k, fp, rel, chk = f4_release(tmp_path, F4_PROBES[probe])
    assert fp.returncode == 2 and fp.stdout.startswith("ERROR search file is incomplete: "), fp.stdout
    assert rel.returncode == 1 and rel.stdout.startswith(f"REFUSED {k} search_unreadable (search file is "
                                                         f"incomplete: "), rel.stdout
    assert chk.returncode == 4 and chk.stdout.startswith(f"PENDING {k} owner={OWNER_A} generation=1 ")
    r = reserve(tmp_path, k, owner="cccc3333")                              # C
    assert r.returncode == 4 and r.stdout.startswith("PENDING"), r.stdout
    assert not [p for p in (tmp_path / "state" / "outbox").iterdir() if ".failed-" in p.name]


@pytest.mark.parametrize("login", ["", " ", "\t", " your-login", "your-login ", "fan hongy"])
def test_f4_blank_or_padded_login_is_refused(tmp_path, login):
    """`--login ""` with a complete matching comment gave NOT_FOUND and released the key."""
    k, fp, rel, chk = f4_release(tmp_path, [a_post()], login=login)
    assert fp.returncode == 2 and fp.stdout.startswith("ERROR bad --login"), fp.stdout
    assert rel.returncode == 1 and rel.stdout.startswith(f"REFUSED {k} search_unreadable (bad --login"), rel.stdout
    assert chk.returncode == 4
    assert reserve(tmp_path, k, owner="cccc3333").returncode == 4          # C still blocked


def test_f4_blank_login_is_refused_even_on_an_empty_search(tmp_path):
    k, fp, rel, chk = f4_release(tmp_path, [], login="")
    assert fp.returncode == 2 and rel.returncode == 1 and chk.returncode == 4, (fp.stdout, rel.stdout)


def test_f4_complete_matching_comment_is_found_and_refuses_failure(tmp_path):
    """The positive control from 11: the complete matching comment -> FOUND -> REFUSED -> C PENDING."""
    k, fp, rel, chk = f4_release(tmp_path, [c("other"), a_post()])
    assert (fp.returncode, fp.stdout.strip()) == (0, f"FOUND {POST_URL}")
    assert rel.returncode == 1 and f"REFUSED {k} search_found_post" in rel.stdout, rel.stdout
    assert chk.returncode == 4 and reserve(tmp_path, k, owner="cccc3333").returncode == 4


@pytest.mark.parametrize("rows", [[], [c("other")], [c(BODY, login="maintainer-login")],
                                  [c("other", login="github-actions[bot]"), c(BODY + "x")],
                                  [dict(c("other"), id=5, created_at="2026-11-09T00:00:00Z",
                                        user={"login": "maintainer-login", "id": 9, "type": "User"})]],
                         ids=["empty", "other_body", "other_author", "bot_and_near_body", "full_github_row"])
def test_f4_valid_empty_or_nonmatching_search_still_releases(tmp_path, rows):
    """Keep working: a valid empty list and valid non-matching comments are NOT_FOUND, and a fresh
    such search after the proof still releases that generation (C may then reserve)."""
    k, fp, rel, chk = f4_release(tmp_path, rows)
    assert (fp.returncode, fp.stdout.strip()) == (1, "NOT_FOUND"), fp.stdout
    assert (rel.returncode, rel.stdout) == (0, f"FAILED {k} reason=search_found_no_post\n"), rel.stdout
    assert chk.returncode == 0
    assert token_of(reserve(tmp_path, k, owner="cccc3333"))


@pytest.mark.parametrize("raw", ["[[]]", "[]\n[]"])
def test_f4_empty_pages_are_a_valid_empty_search(tmp_path, raw):
    assert search(tmp_path, None, raw=raw).stdout.strip() == "NOT_FOUND"


@pytest.mark.parametrize("probe", ["empty_row", "exact_body_user_null"])
def test_f4_find_post_alone_is_an_error(tmp_path, probe):
    """The owner's own step-10 search (no reservation involved) must not read NOT_FOUND either."""
    r = search(tmp_path, F4_PROBES[probe])
    assert r.returncode == 2 and r.stdout.startswith("ERROR search file is incomplete"), r.stdout


# --- sweep: damaged reservation and history lines fail closed ---

def test_sweep_malformed_line_after_a_valid_proof_refuses(tmp_path):
    """A damaged appended line might be the latest proof: never fall back to an older proof."""
    k = key(tmp_path)
    token_of(reserve(tmp_path, k))
    assert owner_gone(tmp_path, k).returncode == 0
    sf, fetched = fresh_search(tmp_path)
    for i, line in enumerate(['{"owner_gone": true, "generation": 1, "proven_ns": "9"}', "{broken", "[1]"]):
        p = tmp_path / "state" / "outbox" / f"{k}.pending"
        good = p.read_text()
        p.write_text(good + "\n" + line)
        r = failed_gone(tmp_path, k, sf, fetched)
        assert r.returncode == 1 and r.stdout.startswith(f"REFUSED {k} state_corrupt:outbox/{k}.pending:3:"), \
            (line, r.stdout)
        assert ob(tmp_path, "check", k).returncode == 4
        p.write_text(good)
    assert failed_gone(tmp_path, k, sf, fetched).returncode == 0          # the control: intact file releases


@pytest.mark.parametrize("line", ["[1]", "null", '"x"', "7"])
def test_sweep_non_object_history_row_fails_closed(tmp_path, line):
    """outbox.jsonl rows are objects; anything else might be this key's record."""
    k = key(tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "outbox.jsonl").write_text(line + "\n")
    r = ob(tmp_path, "check", k)
    assert (r.returncode, r.stdout.strip()) == (2, "ERROR state_corrupt:outbox.jsonl:1:row:not_object"), r.stdout
    r = reserve(tmp_path, k)
    assert r.returncode == 2 and not (tmp_path / "state" / "outbox" / f"{k}.pending").exists()
