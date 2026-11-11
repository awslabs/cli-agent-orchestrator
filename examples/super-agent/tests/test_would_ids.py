"""every WOULD gets a fresh id <N>-<action>-<head8>-<k> from the read-only
`go_ledger.py next-id`, so a GO renewal after a policy bump works while grant ids stay unique."""
import json

from conftest import base_env, ledger, run, write_json
from test_check_mandate import REPO, doc

HEAD = "a" * 40
PREFIX = "101-arm_automerge-aaaaaaaa-"


def mandates(version):
    d = doc(policy_version=version)
    d["mandates"].append({"id": "M6", "holder": "shepherd", "action": "arm_automerge",
                          "scope": {"repo": REPO, "pr_author": "your-login", "base": "main"},
                          "conditions": {}, "granted_by": "user", "granted_at": "2026-11-09",
                          "source": "memory:test", "revoked": False})
    return d


def next_id(tmp_path, *extra, pr="101", action="arm_automerge", head=HEAD):
    r = run("go_ledger.py", "next-id", "--pr", pr, "--action", action, "--head", head, *extra,
            env=base_env(tmp_path))
    return r.returncode, r.stdout.strip()


def grant(tmp_path, wid, version):
    return ledger("add", "--id", wid, "--action", "arm_automerge", "--pr", "101", "--head", HEAD,
                  "--policy-version", version, "--granted-by", "conductor GO", env=base_env(tmp_path))


def precheck(tmp_path, wid, version):
    path = write_json(tmp_path / "mandates.json", mandates(version))
    r = run("check_mandate.py", "--mandates", path, "--holder", "shepherd", "--action", "arm_automerge",
            "--pr-author", "your-login", "--base", "main", "--go-id", wid, "--pr", "101",
            "--expect-head", HEAD, "--remote-head", HEAD, "--expect-policy-version", version,
            env=base_env(tmp_path))
    return r.returncode, r.stdout.strip()


def go_file(tmp_path):
    return tmp_path / "state" / "go.jsonl"


def test_first_id_is_k1_and_reads_nothing_into_being(tmp_path):
    assert next_id(tmp_path) == (0, PREFIX + "1")
    assert not (tmp_path / "state").exists()  # read-only: no state dir, ledger or lock created


def test_renewal_after_a_policy_bump_allows(tmp_path):
    """The reviewer's sequence: grant, policy bump, a new WOULD via next-id, a new grant -> ALLOW."""
    rc, w1 = next_id(tmp_path)
    assert (rc, w1) == (0, PREFIX + "1")
    assert grant(tmp_path, w1, "v1").returncode == 0
    assert precheck(tmp_path, w1, "v1") == (0, f"ALLOW M6 go={w1}")
    rc, out = precheck(tmp_path, w1, "v2")  # the policy bump voids the old GO
    assert (rc, out) == (1, f"DENY go_mismatch:policy_version:{w1}")
    rc, w2 = next_id(tmp_path)
    assert (rc, w2) == (0, PREFIX + "2")
    r = grant(tmp_path, w2, "v2")
    assert (r.returncode, r.stdout) == (0, f"ADDED {w2}\n"), r.stdout
    assert precheck(tmp_path, w2, "v2") == (0, f"ALLOW M6 go={w2}")
    assert next_id(tmp_path) == (0, PREFIX + "3")


def test_the_old_id_is_still_refused_as_a_duplicate(tmp_path):
    for wid in (PREFIX + "1", "101-arm_automerge-aaaaaaaa"):  # a new-format id and a legacy one
        assert grant(tmp_path, wid, "v1").returncode == 0
        before = go_file(tmp_path).read_bytes()
        r = grant(tmp_path, wid, "v2")
        n = before.count(b"\n") + 1
        assert (r.returncode, r.stdout) == (2, f"ERROR row_refused:go.jsonl:{n}:id:duplicate\n"), r.stdout
        assert go_file(tmp_path).read_bytes() == before
    assert next_id(tmp_path) == (0, PREFIX + "2")  # the legacy id has no <k>: it does not count


def test_next_id_skips_gaps_and_counts_revoked_ids(tmp_path):
    for k in (1, 3):
        assert grant(tmp_path, f"{PREFIX}{k}", "v1").returncode == 0
    assert next_id(tmp_path) == (0, PREFIX + "4")  # never refills the gap at 2
    assert ledger("revoke", "--id", PREFIX + "3", "--by", "c", env=base_env(tmp_path)).returncode == 0
    assert next_id(tmp_path) == (0, PREFIX + "4")  # a revoked id stays used
    assert grant(tmp_path, PREFIX + "7", "v1").returncode == 0
    assert next_id(tmp_path) == (0, PREFIX + "8")


def test_next_id_is_per_prefix(tmp_path):
    assert grant(tmp_path, PREFIX + "5", "v1").returncode == 0
    assert next_id(tmp_path, head="b" * 40) == (0, "101-arm_automerge-bbbbbbbb-1")
    assert next_id(tmp_path, pr="1260") == (0, "1260-arm_automerge-aaaaaaaa-1")
    assert next_id(tmp_path, action="update_branch") == (0, "101-update_branch-aaaaaaaa-1")
    # look-alike ids never count: a longer prefix, a zero or leading-zero k, a non-number
    for wid in ("11259-arm_automerge-aaaaaaaa-9", PREFIX + "0", PREFIX + "09", PREFIX + "x9",
                PREFIX + "9-1", "101-arm_automerge-aaaaaaaab-9"):
        assert grant(tmp_path, wid, "v1").returncode == 0, wid
    assert next_id(tmp_path) == (0, PREFIX + "6")


def test_next_id_counts_pending_woulds(tmp_path):
    (tmp_path / "state").mkdir()
    pend = tmp_path / "state" / "shepherd-pending.json"
    pend.write_text(json.dumps({PREFIX + "9": {"sent": True}, "list": [{"wid": PREFIX + "6"}]}))
    assert next_id(tmp_path) == (0, PREFIX + "10")  # a key counts
    pend.write_text(json.dumps({"list": [{"wid": PREFIX + "6"}]}))
    assert next_id(tmp_path) == (0, PREFIX + "7")  # a value inside a list counts
    pend.write_text(json.dumps({"woulds": {"a": PREFIX + "2"}}))
    assert next_id(tmp_path) == (0, PREFIX + "3")
    other = tmp_path / "other.json"
    other.write_text(json.dumps([PREFIX + "11"]))
    assert next_id(tmp_path, "--pending", other) == (0, PREFIX + "12")
    pend.write_text("{not json")
    assert next_id(tmp_path) == (2, "ERROR pending file is not JSON")
    assert next_id(tmp_path, "--pending", tmp_path / "missing.json") == (2, "ERROR pending file not found")


def test_next_id_fails_closed_on_a_corrupt_ledger(tmp_path):
    assert grant(tmp_path, PREFIX + "1", "v1").returncode == 0
    with open(go_file(tmp_path), "a") as fh:
        fh.write("{truncated\n")
    before = go_file(tmp_path).read_bytes()
    rc, out = next_id(tmp_path)
    assert rc == 2 and out.startswith("ERROR state_corrupt:go.jsonl:2:"), out
    assert go_file(tmp_path).read_bytes() == before


def test_next_id_bad_input(tmp_path):
    for kw in ({"pr": "0"}, {"pr": "12a"}, {"pr": "١٢"}, {"action": "Arm"}, {"head": "A" * 40},
               {"head": "a" * 39}):
        rc, out = next_id(tmp_path, **kw)
        assert rc == 2 and out.startswith("ERROR bad --pr"), (kw, out)


def test_next_id_branch_only_one_off(tmp_path):
    r = run("go_ledger.py", "next-id", "--branch", "fix/x-1", "--action", "open_pr", "--head", HEAD,
            env=base_env(tmp_path))
    assert (r.returncode, r.stdout) == (0, "fix.x-1-open_pr-aaaaaaaa-1\n")
    r = run("go_ledger.py", "next-id", "--branch", "a..b", "--action", "open_pr", "--head", HEAD,
            env=base_env(tmp_path))
    assert r.returncode == 2 and r.stdout.startswith("ERROR bad"), r.stdout
    r = run("go_ledger.py", "next-id", "--branch", "/x", "--action", "open_pr", "--head", HEAD,
            env=base_env(tmp_path))
    assert (r.returncode, r.stdout) == (2, "ERROR the id would be too long or invalid\n")

