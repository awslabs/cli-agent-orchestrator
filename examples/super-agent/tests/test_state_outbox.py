"""the outbox: history rows and .sent, the .pending reservation and its owner-gone
proofs, the .generation counter, and every outbox writer."""
import json
import time

import pytest

from state_helpers import *  # noqa: F401,F403


@pytest.mark.parametrize("text", ["0\n", "", "01\n", "-1\n", "1\n\n", " 1\n", "1.0\n", "x"])
def test_generation_file_fails_closed(tmp_path, text):
    k = key(tmp_path)
    (tmp_path / "state" / "outbox").mkdir(parents=True)
    (tmp_path / "state" / "outbox" / f"{k}.generation").write_text(text)
    r = reserve(tmp_path, k)
    assert (r.returncode, r.stdout.strip()) == (2, f"ERROR state_corrupt:outbox/{k}.generation:1:generation:format")
    assert not (tmp_path / "state" / "outbox" / f"{k}.pending").exists()


@pytest.mark.parametrize("text", ["7\n", "7"])
def test_generation_file_control(tmp_path, text):
    k = key(tmp_path)
    (tmp_path / "state" / "outbox").mkdir(parents=True)
    (tmp_path / "state" / "outbox" / f"{k}.generation").write_text(text)
    token_of(reserve(tmp_path, k))
    assert "generation=8 " in ob(tmp_path, "check", k).stdout


@pytest.mark.parametrize("bad", ["removed", None, 7, "", "A" * 64], ids=["removed", "null", "int", "empty", "upper"])
def test_f6_history_row_without_a_valid_key_refuses(tmp_path, bad):
    """history-only recovery (no .sent): a complete row gives USED; the same row without a
    valid key used to read as "never posted": UNUSED, then RESERVED. Now both refuse."""
    k = posted(tmp_path)
    assert ob(tmp_path, "check", k).returncode == 1
    hist = tmp_path / "state" / "outbox.jsonl"
    row = jsonl_get(hist, 1)
    if bad == "removed":
        del row["key"]
    else:
        row["key"] = bad
    jsonl_set(hist, 1, row)
    c = ob(tmp_path, "check", k)
    assert c.returncode == 2 and c.stdout.startswith("ERROR state_corrupt:outbox.jsonl:1:key:"), c.stdout
    r = reserve(tmp_path, k, owner="cccc3333")
    assert r.returncode == 2 and r.stdout.startswith("ERROR state_corrupt:outbox.jsonl:1:key:"), r.stdout
    assert not (tmp_path / "state" / "outbox" / f"{k}.pending").exists()
    assert (tmp_path / "state" / "outbox" / f"{k}.generation").read_text() == "1\n"


F7_PROBES = {
    "two_fields": {"owner_gone": True, "proven_ns": 1},
    "no_evidence": "drop:evidence", "no_owner": "drop:owner", "no_generation": "drop:generation",
    "no_proven_at": "drop:proven_at",
    "other_owner": {"owner": "bbbb2222", "evidence": "http://localhost:9889/terminals/bbbb2222 404"},
    "other_generation": {"generation": 2},
    "not_a_404": {"evidence": f"http://localhost:9889/terminals/{OWNER_A} 200"},
    "proven_at_mismatch": {"proven_at": "2020-01-01T00:00:00.000000+00:00"},
}


F7_WHY = {"two_fields": "generation:missing", "no_evidence": "evidence:missing", "no_owner": "owner:missing",
          "no_generation": "generation:missing", "no_proven_at": "proven_at:missing",
          "other_owner": "owner:not_the_reservation_owner",
          "other_generation": "generation:not_the_reservation_generation",
          "not_a_404": "evidence:not_a_404_for_the_owner", "proven_at_mismatch": "proven_at:not_proven_ns"}


@pytest.mark.parametrize("probe", sorted(F7_PROBES))
def test_f7_incomplete_or_unbound_proof_keeps_the_reservation(tmp_path, probe):
    """after a real reservation and a valid empty search, appending only
    {"owner_gone":true,"proven_ns":1} used to give FAILED, then C RESERVED. A proof must carry the
    owner, generation, 404 evidence, proven_at and proven_ns, bound to THIS reservation."""
    k, p = reserved(tmp_path)
    sf0, fetched0 = fresh_search(tmp_path, name="s0.json")
    r = failed_gone(tmp_path, k, sf0, fetched0)                   # no proof yet: refused
    assert r.returncode == 1 and "owner_not_proven_gone" in r.stdout, r.stdout
    spec = F7_PROBES[probe]
    if isinstance(spec, str):
        proof = complete_proof(time.time_ns())
        del proof[spec.split(":")[1]]
    elif probe == "two_fields":
        proof = dict(spec)
    else:
        proof = dict(complete_proof(time.time_ns()), **spec)
    with open(p, "a") as fh:
        fh.write("\n" + json.dumps(proof))
    time.sleep(0.01)
    sf, fetched = fresh_search(tmp_path)
    r = failed_gone(tmp_path, k, sf, fetched)
    assert (r.returncode, r.stdout.startswith(f"REFUSED {k} state_corrupt:outbox/{k}.pending:2:{F7_WHY[probe]}")) \
        == (1, True), r.stdout
    assert ob(tmp_path, "check", k).returncode == 4
    assert reserve(tmp_path, k, owner="cccc3333").returncode == 4  # C stays blocked
    assert not [x for x in (tmp_path / "state" / "outbox").iterdir() if ".failed-" in x.name]


def test_f7_complete_hand_bound_proof_still_releases(tmp_path):
    """The schema accepts a complete, bound proof (the same shape owner-gone writes)."""
    k, p = reserved(tmp_path)
    with open(p, "a") as fh:
        fh.write("\n" + json.dumps(complete_proof(time.time_ns())))
    time.sleep(0.01)
    sf, fetched = fresh_search(tmp_path)
    assert failed_gone(tmp_path, k, sf, fetched).stdout == f"FAILED {k} reason=search_found_no_post\n"
    assert token_of(reserve(tmp_path, k, owner="cccc3333"))


def test_f7_recovery_from_a_cli_proof_still_works(tmp_path):
    k, _, sf, fetched = proven(tmp_path)
    assert failed_gone(tmp_path, k, sf, fetched).returncode == 0
    assert token_of(reserve(tmp_path, k, owner="cccc3333"))


@pytest.mark.parametrize("over,why", [({"kind": "a b"}, "kind:format"), ({"pr": "x"}, "pr:format"),
                                      ({"head": "ZZZZZZZ"}, "head:format"), ({"kind": ""}, "kind:format")])
def test_outbox_reserve_refuses_a_reservation_its_readers_reject(tmp_path, over, why):
    args = {"pr": 101, "kind": "reply", "head": "c0de0001", **over}
    f = tmp_path / "b.md"
    f.write_text(BODY)
    k = run("outbox.py", "key", "--pr", args["pr"], "--kind", args["kind"], "--head", args["head"],
            "--body-file", f, env=base_env(tmp_path)).stdout.strip()
    r = reserve(tmp_path, k, **args)
    assert (r.returncode, r.stdout.strip()) == (2, f"ERROR row_refused:outbox/{k}.pending:1:{why}"), r.stdout
    assert not (tmp_path / "state" / "outbox" / f"{k}.pending").exists()
    assert not (tmp_path / "state" / "outbox" / f"{k}.generation").exists()


@pytest.fixture
def outbox_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPER_AGENT_STATE_DIR", str(tmp_path / "state"))
    return load("outbox")


def test_outbox_writers_refuse_in_process(tmp_path, outbox_mod):
    """Each outbox writer validates with the readers' schema; nothing is written on refusal."""
    OB = outbox_mod
    k, p = reserved(tmp_path)
    good_res = jsonl_get(p, 1)
    before = p.read_text()
    with pytest.raises(OB.SS.RowRefused, match=r"^row_refused:outbox/[0-9a-f]+\.pending:1:pid:type$"):
        OB.write_reservation("1" * 64, dict(good_res, key="1" * 64, pid="1"))
    assert not (tmp_path / "state" / "outbox" / f"{'1' * 64}.pending").exists()
    other = complete_proof(time.time_ns())
    other["owner"] = "bbbb2222"
    with pytest.raises(OB.SS.RowRefused, match=r":2:owner:not_the_reservation_owner$"):
        OB.append_proof(k, other)
    assert p.read_text() == before
    with pytest.raises(OB.SS.RowRefused, match=r"^row_refused:outbox/[0-9a-f]+\.sent:1:url:format$"):
        OB.record_sent(k, {"key": k, "url": "a b", "recorded_at": OB.now(), "generation": 1})
    with pytest.raises(OB.SS.RowRefused, match=r"generation:null$"):
        OB.record_sent(k, {"key": k, "url": "u", "recorded_at": OB.now(), "generation": None})
    assert p.read_text() == before and not (tmp_path / "state" / "outbox.jsonl").exists()
    with pytest.raises(OB.SS.RowRefused, match=r":failed:evidence:does_not_match_by$"):
        OB.record_failed(k, {"failed_at": OB.now(), "reason": "x", "by": "owner_gone", "evidence": None}, good_res)
    bad_ev = {"generation": 2, "proof": complete_proof(time.time_ns()), "search_fetched_at": OB.now()}
    with pytest.raises(OB.SS.RowRefused, match=r":failed:evidence.generation:not_the_reservation_generation$"):
        OB.record_failed(k, {"failed_at": OB.now(), "reason": "x", "by": "owner_gone", "evidence": bad_ev}, good_res)
    bad_ev = {"generation": 1, "proof": dict(complete_proof(time.time_ns()), owner="bbbb2222"),
              "search_fetched_at": OB.now()}
    with pytest.raises(OB.SS.RowRefused, match=r":failed:evidence.proof.owner:not_the_reservation_owner$"):
        OB.record_failed(k, {"failed_at": OB.now(), "reason": "x", "by": "owner_gone", "evidence": bad_ev}, good_res)
    with pytest.raises(OB.SS.RowRefused, match=r":failed:reason:format$"):
        OB.record_failed(k, {"failed_at": OB.now(), "reason": "a b", "by": "owner_token", "evidence": None}, good_res)
    with pytest.raises(OB.SS.RowRefused, match=r":failed:by:value$"):
        OB.record_failed(k, {"failed_at": OB.now(), "reason": "x", "by": "someone", "evidence": None}, good_res)
    assert p.read_text() == before
    for gen in (0, True, "2"):
        with pytest.raises(OB.SS.RowRefused, match=r"\.generation:1:generation:range$"):
            OB.write_generation(k, gen)
    assert (tmp_path / "state" / "outbox" / f"{k}.generation").read_text() == "1\n"
    # the controls: the same writers accept valid rows
    OB.append_proof(k, complete_proof(time.time_ns()))
    OB.record_sent(k, {"key": k, "url": "u", "recorded_at": OB.now(), "generation": 1})
    assert ob(tmp_path, "check", k).returncode == 1


HIST_WRONG = {"key": 7, "url": 7, "recorded_at": 7, "generation": "1"}


def test_history_control_is_used(tmp_path):
    k = posted(tmp_path)
    c = ob(tmp_path, "check", k)
    assert c.returncode == 1 and c.stdout.startswith(f"USED {k} url=https://x/c/1"), c.stdout
    assert reserve(tmp_path, k, owner="cccc3333").returncode == 1


HIST_CASES = variants(HIST_ROW, HIST_WRONG) + [
    ("key-upper", dict(HIST_ROW, key="A" * 64), "key:format"),
    ("url-space", dict(HIST_ROW, url="a b"), "url:format"),
    ("generation-bool", dict(HIST_ROW, generation=True), "generation:type"),
    ("generation-zero", dict(HIST_ROW, generation=0), "generation:range"),
    ("unknown_field", dict(HIST_ROW, x=1), "x:unknown_field"),
]


@pytest.mark.parametrize("case", HIST_CASES, ids=[c[0] for c in HIST_CASES])
def test_history_row_field_fails_closed(tmp_path, case):
    """Every outbox.jsonl row (this key's or not) is validated: check and reserve both ERROR."""
    k = posted(tmp_path)
    _, row, why = case
    hist = tmp_path / "state" / "outbox.jsonl"
    if row.get("key") == "0" * 64:  # the case does not touch the key: keep this key's own record
        row = dict(row, key=jsonl_get(hist, 1)["key"])
    jsonl_set(hist, 1, row)
    c = ob(tmp_path, "check", k)
    assert c.returncode == 2 and c.stdout.startswith(f"ERROR state_corrupt:outbox.jsonl:1:{why}"), c.stdout
    r = reserve(tmp_path, k, owner="cccc3333")
    assert r.returncode == 2 and r.stdout.startswith("ERROR state_corrupt:outbox.jsonl:1:"), r.stdout
    assert not (tmp_path / "state" / "outbox" / f"{k}.pending").exists()


@pytest.mark.parametrize("case", HIST_CASES, ids=[c[0] for c in HIST_CASES])
def test_sent_record_field_fails_closed(tmp_path, case):
    k = posted(tmp_path, keep_sent=True)
    _, row, why = case
    sent = tmp_path / "state" / "outbox" / f"{k}.sent"
    if row.get("key") == "0" * 64:
        row = dict(row, key=k)
    sent.write_text(json.dumps(row))
    c = ob(tmp_path, "check", k)
    assert c.returncode == 2 and c.stdout.startswith(f"ERROR state_corrupt:outbox/{k}.sent:1:{why}"), c.stdout
    assert reserve(tmp_path, k, owner="cccc3333").returncode == 2
    assert not (tmp_path / "state" / "outbox" / f"{k}.pending").exists()


@pytest.mark.parametrize("text,why", [("{broken", "document:not_json"), ("", "document:not_json"),
                                      ("[]", "row:not_object")])
def test_damaged_sent_file_fails_closed(tmp_path, text, why):
    k = posted(tmp_path, keep_sent=True)
    (tmp_path / "state" / "outbox" / f"{k}.sent").write_text(text)
    assert ob(tmp_path, "check", k).stdout.strip() == f"ERROR state_corrupt:outbox/{k}.sent:1:{why}"


def test_sent_record_for_another_key_fails_closed(tmp_path):
    k = posted(tmp_path, keep_sent=True)
    sent = tmp_path / "state" / "outbox" / f"{k}.sent"
    sent.write_text(json.dumps(dict(json.loads(sent.read_text()), key="0" * 64)))
    assert ob(tmp_path, "check", k).stdout.strip() == f"ERROR state_corrupt:outbox/{k}.sent:1:key:not_this_file"


RES_WRONG = {"key": 7, "pr": 101, "kind": 7, "head": 7, "reserved_at": 7, "pid": "1",
             "owner_terminal": 7, "generation": "1", "token_sha256": 7}


def test_reservation_control(tmp_path):
    k, _ = reserved(tmp_path)
    c = ob(tmp_path, "check", k)
    assert c.returncode == 4 and c.stdout.startswith(f"PENDING {k} owner={OWNER_A} generation=1 "), c.stdout
    assert owner_gone(tmp_path, k).stdout.startswith(f"OWNER_GONE {k} generation=1 ")


@pytest.mark.parametrize("field", list(RES_WRONG) + ["unknown"])
@pytest.mark.parametrize("how", ["removed", "null", "wrong_type"])
def test_reservation_field_fails_closed(tmp_path, field, how):
    """A damaged reservation stays PENDING: check exit 4, owner-gone / failed / sent REFUSED."""
    k, p = reserved(tmp_path)
    row = jsonl_get(p, 1)
    if field == "unknown":
        row["extra"] = {"removed": None, "null": None, "wrong_type": 1}[how]
        why = "extra:unknown_field"
    else:
        if how == "removed":
            del row[field]
        else:
            row[field] = None if how == "null" else RES_WRONG[field]
        why = f"{field}:"
    jsonl_set(p, 1, row)
    pre = f"state_corrupt:outbox/{k}.pending:1:{why}"
    c = ob(tmp_path, "check", k)
    assert c.returncode == 4 and c.stdout.startswith(f"PENDING {k} {pre}"), c.stdout
    r = owner_gone(tmp_path, k)
    assert r.returncode == 1 and r.stdout.startswith(f"REFUSED {k} {pre}"), r.stdout
    r = ob(tmp_path, "failed", k, "--reason", "post_failed", "--token", "f" * 32)
    assert r.returncode == 1 and r.stdout.startswith(f"REFUSED {k} {pre}"), r.stdout
    r = ob(tmp_path, "sent", k, "--url", "https://x/c/1")
    assert r.returncode == 1 and r.stdout.startswith(f"REFUSED {k} {pre}"), r.stdout
    assert p.exists() and reserve(tmp_path, k, owner="cccc3333").returncode == 4


def test_reservation_for_another_key_fails_closed(tmp_path):
    k, p = reserved(tmp_path)
    jsonl_set(p, 1, dict(jsonl_get(p, 1), key="0" * 64))
    assert ob(tmp_path, "check", k).stdout.startswith(f"PENDING {k} state_corrupt:outbox/{k}.pending:1:key:not_this_file")


@pytest.mark.parametrize("text,why", [("", "1:reservation:missing"), ("{broken", "1:row:not_json"),
                                      ("\n\n", "1:row:blank_line")])
def test_empty_or_damaged_reservation_stays_pending(tmp_path, text, why):
    k, p = reserved(tmp_path)
    p.write_text(text)
    c = ob(tmp_path, "check", k)
    assert c.returncode == 4 and c.stdout.startswith(f"PENDING {k} state_corrupt:outbox/{k}.pending:{why}"), c.stdout


PROOF_WRONG = {"owner_gone": "true", "generation": "1", "owner": 7, "evidence": 7, "proven_ns": "1",
               "proven_at": 7}


def test_proof_control_releases(tmp_path):
    k, _, sf, fetched = proven(tmp_path)
    assert failed_gone(tmp_path, k, sf, fetched).stdout == f"FAILED {k} reason=search_found_no_post\n"


@pytest.mark.parametrize("field", list(PROOF_WRONG) + ["unknown"])
@pytest.mark.parametrize("how", ["removed", "null", "wrong_type"])
def test_proof_field_fails_closed(tmp_path, field, how):
    k, p, sf, fetched = proven(tmp_path)
    row = jsonl_get(p, 2)
    if field == "unknown":
        row["extra"] = 1
        why = "extra:unknown_field"
    else:
        if how == "removed":
            del row[field]
        else:
            row[field] = None if how == "null" else PROOF_WRONG[field]
        why = f"{field}:"
    jsonl_set(p, 2, row)
    r = failed_gone(tmp_path, k, sf, fetched)
    assert r.returncode == 1 and r.stdout.startswith(f"REFUSED {k} state_corrupt:outbox/{k}.pending:2:{why}"), r.stdout
    assert ob(tmp_path, "check", k).returncode == 4 and reserve(tmp_path, k, owner="cccc3333").returncode == 4
