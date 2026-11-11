"""Shared helpers for the brief-14 state-schema tests (test_state_*.py).

bin/state_schema.py holds one strict validator per state-file row type, used by every reader and
writer. Any invalid row makes the whole file corrupt and the caller fails closed with
state_corrupt:<file>:<line>:<why>; writers refuse (row_refused:...) and write nothing.
"""
import copy
import hashlib
import importlib.util
import json

from conftest import BIN, KIT, base_env, ledger, run, write_json  # noqa: F401
from test_automerge_gate import (ARM, DENY, MAINTAINER_CLEAN, HEAD, HOLD, clear_item, expect, gate, pr,  # noqa: F401
                                 review)
from test_check_mandate import GO_ARGS, act, doc, go_row, recheck, revoke_row  # noqa: F401
from test_outbox import (BODY, OWNER_A, failed_gone, fresh_search, key, ob, owner_gone, reserve,  # noqa: F401
                         token_of)
from test_resource_gate import fakebin, m  # noqa: F401  (fakebin is a fixture)
from test_resource_gate import gate as rgate  # noqa: F401


WID = "101-ff_fix_push-11111111"


def load(name):
    """A kit script imported in-process (the copy under SUPER_AGENT_KIT, so mutants count)."""
    spec = importlib.util.spec_from_file_location(f"kit_test_{name}", BIN / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def variants(row, wrong, nullable=(), discriminator=None, prefix=""):
    """[(id, mutated row, expected why-prefix)] for each required field: removed, null (unless the
    field is declared nullable) and of the wrong type."""
    out = []
    for field, bad in wrong.items():
        cases = [("removed", None), ("wrong_type", bad)] + ([] if field in nullable else [("null", None)])
        for how, value in cases:
            r = copy.deepcopy(row)
            if how == "removed":
                del r[field]
            else:
                r[field] = value
            why = "row_type:unknown" if field == discriminator else f"{prefix}{field}:"
            out.append((f"{field}-{how}", r, why))
    return out


def jsonl_set(path, n, row):
    """Replace line n (1-based) of a .jsonl file, keeping its other bytes."""
    lines = path.read_text().split("\n")
    lines[n - 1] = json.dumps(row)
    path.write_text("\n".join(lines))


def jsonl_get(path, n):
    return json.loads(path.read_text().split("\n")[n - 1])


def posted(tmp_path, keep_sent=False):
    """A real posting record: reserve -> sent; then (history-only recovery) drop the .sent."""
    k = key(tmp_path)
    token_of(reserve(tmp_path, k))
    assert ob(tmp_path, "sent", k, "--url", "https://x/c/1").returncode == 0
    if not keep_sent:
        (tmp_path / "state" / "outbox" / f"{k}.sent").unlink()
    return k


HIST_ROW = {"key": "0" * 64, "url": "u", "recorded_at": "2026-11-09T10:00:00+00:00", "generation": 1}


def reserved(tmp_path):
    k = key(tmp_path)
    token_of(reserve(tmp_path, k))
    return k, tmp_path / "state" / "outbox" / f"{k}.pending"


def proven(tmp_path):
    """A reservation with a real CLI-recorded owner-gone proof, then a fresh empty search."""
    k, p = reserved(tmp_path)
    assert owner_gone(tmp_path, k).returncode == 0
    sf, fetched = fresh_search(tmp_path)
    return k, p, sf, fetched


def held(tmp_path):
    expect(gate(tmp_path, pr(), [review(body=MAINTAINER_CLEAN)]), HOLD, "HOLD")
    return tmp_path / "state" / "holds.json"


def sha(b):
    return hashlib.sha256(b).hexdigest()


WF = "pr-review.py"


BASELINE = {"kind": "baseline", "workflow": WF[:-3], "file": WF, "sha256": sha(b"wf"),
            "verified_at": "2026-11-08T11:45Z", "verified_by": "user", "evidence": "test"}


APPROVAL = {"kind": "approval", "at": "2026-11-09T10:00:00+00:00", "plan_id": "plan-v2:p1",
            "workflow": WF[:-3], "pr": 101, "head": "a" * 40, "base": "main", "sha256": {WF: sha(b"wf")}}


def pv():
    return json.loads((KIT / "mandates.json").read_text())["policy_version"]


def complete_proof(ns):
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(ns // 10**9, timezone.utc).replace(microsecond=(ns // 1000) % 10**6)
    return {"owner_gone": True, "generation": 1, "owner": OWNER_A,
            "evidence": f"http://localhost:9889/terminals/{OWNER_A} 404", "proven_ns": ns,
            "proven_at": dt.isoformat(timespec="microseconds")}
