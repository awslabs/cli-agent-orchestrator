"""Unique GO ids, and one shared append routine.

in go.jsonl a second grant with a used id (same bytes or not, revoked in between or not) and
a second revoke of one id are corrupt: state_corrupt:go.jsonl:<line>:id:duplicate. go_ledger.py
add refuses a used id, so the kit never writes one.

every .jsonl writer appends through state_schema.append_row: it validates the whole file and
the row first, then appends with exactly one LF separator whether the file is empty, ends in LF,
or ends in a complete row with no final LF. Rule for a trailing blank line (final LF + another
LF): readers reject it (row:blank_line) and so the writer refuses cleanly, the file unchanged;
the conductor drops the blank line by hand and recovery then works.
"""
import json

import pytest

from state_helpers import *  # noqa: F401,F403
from test_state_go import arm_go, shipped_arm_check


def schema_check(tmp_path):
    r = run("state_schema.py", "check", "--state-dir", tmp_path / "state", env=base_env(tmp_path))
    return r.returncode, r.stdout.strip()


def strip_final_lf(p):
    raw = p.read_bytes()
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")
    p.write_bytes(raw[:-1])


def assert_clean_jsonl(p, n_rows):
    """Exactly n_rows rows, each one LF-terminated, no blank line; the shared reader accepts it."""
    raw = p.read_bytes()
    assert raw.endswith(b"\n") and b"\n\n" not in raw and not raw.startswith(b"\n"), raw
    SS = load("state_schema")
    assert len(SS.jsonl_rows(str(p))) == n_rows


# ---------- unique grant ids ----------

def test_f8_reviewer_sequence_grant_revoke_same_grant_bytes_denies(tmp_path):
    """G -> ALLOW; R -> DENY revoked; the exact saved G bytes appended again used to ALLOW."""
    arm_go(tmp_path, "dup")
    led = tmp_path / "state" / "go.jsonl"
    grant_bytes = led.read_bytes()
    rc, out = shipped_arm_check(tmp_path, "dup")
    assert rc == 0 and out.startswith("ALLOW M6") and out.endswith("go=dup"), out
    assert ledger("revoke", "--id", "dup", "--by", "conductor", env=base_env(tmp_path)).returncode == 0
    assert shipped_arm_check(tmp_path, "dup") == (1, "DENY go_mismatch:revoked:dup")
    with open(led, "ab") as fh:
        fh.write(grant_bytes)
    assert shipped_arm_check(tmp_path, "dup") == (1, "DENY state_corrupt:go.jsonl:3:id:duplicate")
    assert schema_check(tmp_path) == (1, "state_corrupt:go.jsonl:3:id:duplicate")


@pytest.mark.parametrize("over", [{}, {"head": "b" * 40}], ids=["same_bytes", "different_bytes"])
def test_f8_duplicate_grant_without_revoke_denies(tmp_path, over):
    rows = [go_row(), go_row(**over)]
    assert recheck(tmp_path, act(doc()), *GO_ARGS, go_rows=rows) == (1, "DENY state_corrupt:go.jsonl:2:id:duplicate")


def test_f8_second_revoke_is_corrupt(tmp_path):
    rows = [go_row(), revoke_row(), revoke_row()]
    assert recheck(tmp_path, act(doc()), *GO_ARGS, go_rows=rows) == (1, "DENY state_corrupt:go.jsonl:3:id:duplicate")


def test_f8_add_with_a_used_id_is_refused(tmp_path):
    env = base_env(tmp_path)
    led = tmp_path / "state" / "go.jsonl"
    arm_go(tmp_path, "w")
    args = ["--id", "w", "--action", "arm_automerge", "--pr", "101", "--head", HEAD, "--policy-version", pv(),
            "--granted-by", "conductor GO"]
    before = led.read_bytes()
    r = ledger("add", *args, env=env)
    assert (r.returncode, r.stdout.strip()) == (2, "ERROR row_refused:go.jsonl:2:id:duplicate")
    assert led.read_bytes() == before
    assert ledger("revoke", "--id", "w", "--by", "conductor", env=env).returncode == 0
    before = led.read_bytes()
    r = ledger("add", *args, env=env)  # regranting needs a fresh id
    assert (r.returncode, r.stdout.strip()) == (2, "ERROR row_refused:go.jsonl:3:id:duplicate")
    r = ledger("revoke", "--id", "w", "--by", "conductor", env=env)
    assert (r.returncode, r.stdout.strip()) == (2, "ERROR row_refused:go.jsonl:3:id:duplicate")
    assert led.read_bytes() == before
    assert shipped_arm_check(tmp_path, "w") == (1, "DENY go_mismatch:revoked:w")


def test_f8_control_two_different_ids_are_fine(tmp_path):
    arm_go(tmp_path, "w-a")
    arm_go(tmp_path, "w-b")
    assert ledger("revoke", "--id", "w-a", "--by", "conductor", env=base_env(tmp_path)).returncode == 0
    arm_go(tmp_path, "w-c")  # a fresh id after a revoke is a new GO
    for wid in ("w-b", "w-c"):
        rc, out = shipped_arm_check(tmp_path, wid)
        assert rc == 0 and out.startswith("ALLOW M6") and out.endswith(f"go={wid}"), out
    assert shipped_arm_check(tmp_path, "w-a") == (1, "DENY go_mismatch:revoked:w-a")
    assert schema_check(tmp_path) == (0, "OK 1 files")


def test_f8_check_go_append_in_process(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPER_AGENT_STATE_DIR", str(tmp_path / "state"))
    SS = load("state_schema")
    p = str(tmp_path / "state" / "go.jsonl")
    g, r = go_row(), revoke_row()
    SS.check_go_append(p, [g], r)
    SS.check_go_append(p, [g, r], go_row(id="other"))
    for rows, row, line in (([g], g, 2), ([g, r], g, 3), ([g, r], r, 3)):
        with pytest.raises(SS.RowRefused, match=rf"^row_refused:go.jsonl:{line}:id:duplicate$"):
            SS.check_go_append(p, rows, row)


# ---------- append_row in process ----------

def _plain_check(path, row):
    return None


@pytest.mark.parametrize("before,after", [
    (None, b'{"a": 1}\n'),
    (b"", b'{"a": 1}\n'),
    (b'{"z": 0}\n', b'{"z": 0}\n{"a": 1}\n'),
    (b'{"z": 0}', b'{"z": 0}\n{"a": 1}\n'),
], ids=["missing", "empty", "lf_terminated", "unterminated"])
def test_f9_append_row_separator(tmp_path, before, after):
    SS = load("state_schema")
    p = tmp_path / "x.jsonl"
    if before is not None:
        p.write_bytes(before)
    SS.append_row(str(p), {"a": 1}, _plain_check)
    assert p.read_bytes() == after


def test_f9_append_row_runs_the_check_first(tmp_path):
    SS = load("state_schema")
    p = tmp_path / "x.jsonl"
    p.write_bytes(b'{"z": 0}')

    def refuse(path, row):
        raise SS.RowRefused("x.jsonl", 2, "row:no")
    with pytest.raises(SS.RowRefused):
        SS.append_row(str(p), {"a": 1}, refuse)
    assert p.read_bytes() == b'{"z": 0}'


@pytest.mark.parametrize("damage,why", [
    (lambda b: b + b"\n", "2:row:blank_line"),                         # trailing blank line
    (lambda b: b + b"\n\n" + b, "2:row:blank_line"),                   # interior blank line
    (lambda b: b + b[:-3], "2:row:not_json"),                          # truncated last row
], ids=["trailing_blank_line", "interior_blank_line", "truncated_last_row"])
def test_f9_every_writer_refuses_damaged_files_before_writing(tmp_path, monkeypatch, damage, why):
    monkeypatch.setenv("SUPER_AGENT_STATE_DIR", str(tmp_path / "state"))
    SS = load("state_schema")
    (tmp_path / "state").mkdir()
    k = "0" * 64
    hist = dict(HIST_ROW, key=k)
    for name, row, check in (("go.jsonl", go_row(), SS.check_go_file_append),
                             ("outbox.jsonl", hist, SS.check_history_append),
                             ("plan-approvals.jsonl", BASELINE, SS.check_plan_append)):
        p = tmp_path / "state" / name
        p.write_bytes(damage(json.dumps(row).encode() + b"\n"))
        before = p.read_bytes()
        with pytest.raises(SS.StateCorrupt, match=rf"^state_corrupt:{name}:{why}$"):
            SS.append_row(str(p), dict(row, id="other") if name == "go.jsonl" else row, check)
        assert p.read_bytes() == before


def test_f9_append_checks_refuse_bad_rows_in_process(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPER_AGENT_STATE_DIR", str(tmp_path / "state"))
    SS = load("state_schema")
    (tmp_path / "state").mkdir()
    hist = tmp_path / "state" / "outbox.jsonl"
    hist.write_bytes(json.dumps(HIST_ROW).encode())
    with pytest.raises(SS.RowRefused, match=r"^row_refused:outbox.jsonl:2:url:"):
        SS.append_row(str(hist), dict(HIST_ROW, url=7), SS.check_history_append)
    plan = tmp_path / "state" / "plan-approvals.jsonl"
    plan.write_bytes(json.dumps(BASELINE).encode())
    with pytest.raises(SS.RowRefused, match=r"^row_refused:plan-approvals.jsonl:2:row_type:unknown"):
        SS.append_row(str(plan), dict(BASELINE, kind="x"), SS.check_plan_append)
    hist.write_bytes(b"[]\n")
    with pytest.raises(SS.StateCorrupt, match=r"^state_corrupt:outbox.jsonl:1:row:not_object$"):
        SS.append_row(str(hist), HIST_ROW, SS.check_history_append)
    plan.write_bytes(b"[]\n")
    with pytest.raises(SS.StateCorrupt, match=r"^state_corrupt:plan-approvals.jsonl:1:row:not_object$"):
        SS.append_row(str(plan), BASELINE, SS.check_plan_append)
    assert hist.read_bytes() == b"[]\n" and plan.read_bytes() == b"[]\n"


def test_f9_pending_append_checks_in_process(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPER_AGENT_STATE_DIR", str(tmp_path / "state"))
    SS = load("state_schema")
    k, p = reserved(tmp_path)
    before = p.read_bytes()
    bad_proof = dict(complete_proof(1_000_000_000_000_000_000), owner="bbbb2222")
    with pytest.raises(SS.RowRefused, match=r"^row_refused:outbox/[0-9a-f]{64}.pending:2:"):
        SS.append_row(str(p), bad_proof, SS.pending_append_check(k))
    bad_failed = {"failed_at": "2026-11-09T10:00:00+00:00", "reason": "a b", "by": "owner_token", "evidence": None}
    with pytest.raises(SS.RowRefused, match=r"^row_refused:outbox/[0-9a-f]{64}.pending:failed:reason:"):
        SS.append_row(str(p), bad_failed, SS.pending_append_check(k, failed=True))
    assert p.read_bytes() == before


# ---------- reader -> writer -> reader round trips, both valid endings ----------

ENDINGS = pytest.mark.parametrize("unterminated", [False, True], ids=["final_lf", "no_final_lf"])


@ENDINGS
def test_f9_go_ledger_round_trip(tmp_path, unterminated):
    """Reviewer reproduction 1 (no final LF): revoke used to glue its row to the grant -> not_json."""
    arm_go(tmp_path, "w")
    led = tmp_path / "state" / "go.jsonl"
    if unterminated:
        strip_final_lf(led)
    assert schema_check(tmp_path) == (0, "OK 1 files")
    rc, out = shipped_arm_check(tmp_path, "w")
    assert rc == 0 and out.startswith("ALLOW M6"), out
    r = ledger("revoke", "--id", "w", "--by", "conductor", env=base_env(tmp_path))
    assert (r.returncode, r.stdout.strip()) == (0, "REVOKED w")
    assert_clean_jsonl(led, 2)
    assert schema_check(tmp_path) == (0, "OK 1 files")
    assert shipped_arm_check(tmp_path, "w") == (1, "DENY go_mismatch:revoked:w")
    arm_go(tmp_path, "w2")  # and add appends cleanly too
    assert_clean_jsonl(led, 3)


@ENDINGS
def test_f9_outbox_history_round_trip(tmp_path, unterminated):
    k1 = posted(tmp_path, keep_sent=True)
    hist = tmp_path / "state" / "outbox.jsonl"
    if unterminated:
        strip_final_lf(hist)
    assert ob(tmp_path, "check", k1).returncode == 1
    k2 = key(tmp_path, body="Second reply.\n")
    token_of(reserve(tmp_path, k2, body="Second reply.\n"))
    r = ob(tmp_path, "sent", k2, "--url", "https://x/c/2")
    assert r.returncode == 0 and r.stdout.startswith("RECORDED"), r.stdout
    assert_clean_jsonl(hist, 2)
    assert schema_check(tmp_path)[0] == 0
    for k, url in ((k1, "https://x/c/1"), (k2, "https://x/c/2")):
        (tmp_path / "state" / "outbox" / f"{k}.sent").unlink()  # history-only: the jsonl alone says USED
        c = ob(tmp_path, "check", k)
        assert c.returncode == 1 and url in c.stdout, c.stdout


@ENDINGS
def test_f9_plan_ledger_round_trip(tmp_path, unterminated):
    led = tmp_path / "state" / "plan-approvals.jsonl"
    led.parent.mkdir()
    led.write_text(json.dumps(BASELINE) + "\n")
    if unterminated:
        strip_final_lf(led)
    assert schema_check(tmp_path) == (0, "OK 1 files")
    r = run("state_schema.py", "append-plan-row", "--row", json.dumps(APPROVAL), "--ledger", led,
            env=base_env(tmp_path))
    assert (r.returncode, r.stdout.strip()) == (0, "APPENDED approval"), r.stdout
    assert_clean_jsonl(led, 2)
    assert schema_check(tmp_path) == (0, "OK 1 files")


@ENDINGS
def test_f9_pending_owner_gone_round_trip_and_recovery(tmp_path, unterminated):
    """Reviewer reproduction 2 (final_lf): a pending reservation with one extra final LF, then
    owner-gone, used to leave a blank line 2, so the failure reconciliation was REFUSED."""
    k, p = reserved(tmp_path)
    if not unterminated:  # the reservation is written without a final LF; add one
        p.write_bytes(p.read_bytes() + b"\n")
    assert schema_check(tmp_path)[0] == 0
    assert ob(tmp_path, "check", k).returncode == 4
    r = owner_gone(tmp_path, k)
    assert r.returncode == 0 and r.stdout.startswith(f"OWNER_GONE {k}"), r.stdout
    assert_clean_jsonl(p, 2)
    assert schema_check(tmp_path)[0] == 0
    sf, fetched = fresh_search(tmp_path)
    r = failed_gone(tmp_path, k, sf, fetched)
    assert (r.returncode, r.stdout) == (0, f"FAILED {k} reason=search_found_no_post\n"), r.stdout
    failed = [f for f in (tmp_path / "state" / "outbox").iterdir() if ".failed-" in f.name]
    assert len(failed) == 1
    assert_clean_jsonl(failed[0], 3)  # reservation, proof, failed row
    r = reserve(tmp_path, k, owner="cccc3333")
    assert r.returncode == 0 and r.stdout.startswith("RESERVED"), r.stdout


@ENDINGS
def test_f9_owner_token_failed_round_trip(tmp_path, unterminated):
    k = key(tmp_path)
    tok = token_of(reserve(tmp_path, k))
    p = tmp_path / "state" / "outbox" / f"{k}.pending"
    if not unterminated:
        p.write_bytes(p.read_bytes() + b"\n")
    r = ob(tmp_path, "failed", k, "--reason", "post_failed", "--token", tok)
    assert r.returncode == 0 and r.stdout.startswith("FAILED"), r.stdout
    failed = [f for f in (tmp_path / "state" / "outbox").iterdir() if ".failed-" in f.name]
    assert len(failed) == 1
    assert_clean_jsonl(failed[0], 2)


def test_f9_rule_trailing_blank_line_refuses_cleanly_then_recovers_after_repair(tmp_path):
    """The chosen rule: a blank line after the final LF is rejected by the reader and the writer
    alike; owner-gone refuses before writing (PENDING kept, bytes unchanged); once the conductor
    drops the blank line the normal recovery works."""
    k, p = reserved(tmp_path)
    p.write_bytes(p.read_bytes() + b"\n\n")
    before = p.read_bytes()
    rc, out = schema_check(tmp_path)
    assert rc == 1 and out.endswith(":2:row:blank_line"), out
    r = owner_gone(tmp_path, k)
    assert r.returncode == 1 and "2:row:blank_line" in r.stdout, r.stdout
    assert p.read_bytes() == before
    assert ob(tmp_path, "check", k).returncode == 4  # still PENDING: the key stays blocked
    p.write_bytes(before[:-1])  # the hand repair: drop the blank line
    r = owner_gone(tmp_path, k)
    assert r.returncode == 0 and r.stdout.startswith("OWNER_GONE"), r.stdout
    sf, fetched = fresh_search(tmp_path)
    assert failed_gone(tmp_path, k, sf, fetched).returncode == 0
    assert reserve(tmp_path, k, owner="cccc3333").returncode == 0
