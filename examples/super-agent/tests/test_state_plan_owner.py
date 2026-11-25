"""plan-approvals.jsonl (its strict schema; the review plans it guarded are gone) and heavy/active/owner.json."""
import json
import re

import pytest

from state_helpers import *  # noqa: F401,F403


def test_shipped_plan_ledger_is_valid(tmp_path):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    second = dict(BASELINE, workflow="branch-review-script-local-20261027",
                  file="branch-review-script-local-20261027.py",
                  sha256="1" * 64)
    (state / "plan-approvals.jsonl").write_text(
        json.dumps(BASELINE) + "\n" + json.dumps(second) + "\n")
    ss = load("state_schema")
    rows = ss.load_plan_ledger(state / "plan-approvals.jsonl")
    assert len(rows) == 2 and {r["kind"] for r in rows} == {"baseline"}


OWNER_WRONG = {"label": 7, "token": 7, "acquired_at": "1", "refreshed_at": "1", "pid": "1"}


TOKEN = re.compile(r"token=([0-9a-f]{32})\b")


def acquired(tmp_path, fakebin):
    rc, out = rgate(tmp_path, fakebin, m(), "--acquire-heavy", "suite")
    assert rc == 0, out
    return TOKEN.search(out).group(1), tmp_path / "state" / "heavy" / "active" / "owner.json"


def test_owner_control(tmp_path, fakebin):
    tok, _ = acquired(tmp_path, fakebin)
    assert rgate(tmp_path, fakebin, m(), "--acquire-heavy", "b")[1].startswith("REFUSED BUSY suite")
    assert rgate(tmp_path, fakebin, m(), "--refresh-heavy", tok)[1] == "REFRESHED suite"
    assert rgate(tmp_path, fakebin, m(), "--release-heavy", tok)[1] == "RELEASED suite"


@pytest.mark.parametrize("field", list(OWNER_WRONG) + ["unknown"])
@pytest.mark.parametrize("how", ["removed", "null", "wrong_type"])
def test_owner_field_fails_closed(tmp_path, fakebin, field, how):
    tok, f = acquired(tmp_path, fakebin)
    row = json.loads(f.read_text())
    why = f"{field}:"
    if field == "unknown":
        row["extra"] = 1
        why = "extra:unknown_field"
    elif how == "removed":
        del row[field]
    else:
        row[field] = None if how == "null" else OWNER_WRONG[field]
    f.write_text(json.dumps(row))
    pre = f"REFUSED state_corrupt:heavy/active/owner.json:1:{why}"
    for args in (("--acquire-heavy", "b"), ("--refresh-heavy", tok), ("--release-heavy", tok)):
        rc, out = rgate(tmp_path, fakebin, m(), *args)
        assert rc == 1 and out.startswith(pre), (args, out)
    assert f.exists()


@pytest.mark.parametrize("text,why", [("", "document:not_json"), ('{"label": NaN}', "document:not_json"),
                                      ("[]", "row:not_object")])
def test_damaged_owner_file_fails_closed(tmp_path, fakebin, text, why):
    tok, f = acquired(tmp_path, fakebin)
    f.write_text(text)
    rc, out = rgate(tmp_path, fakebin, m(), "--release-heavy", tok)
    assert rc == 1 and out.startswith(f"REFUSED state_corrupt:heavy/active/owner.json:1:{why}"), out
    assert f.exists()


@pytest.mark.parametrize("value,why", [(float("inf"), "acquired_at:range"), (-1, "acquired_at:range"),
                                       (True, "acquired_at:type")])
def test_owner_times_must_be_finite_numbers(tmp_path, fakebin, value, why):
    tok, f = acquired(tmp_path, fakebin)
    row = json.loads(f.read_text())
    row["acquired_at"] = value
    f.write_text(json.dumps(row))  # json writes inf as Infinity: rejected as not JSON first
    rc, out = rgate(tmp_path, fakebin, m(), "--release-heavy", tok)
    want = "document:not_json" if value == float("inf") else why
    assert rc == 1 and out.startswith(f"REFUSED state_corrupt:heavy/active/owner.json:1:{want}"), out


def test_heavy_owner_writer_refuses_in_process(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPER_AGENT_STATE_DIR", str(tmp_path / "state"))
    RG = load("resource_gate")
    marker = tmp_path / "state" / "heavy" / "active"
    marker.mkdir(parents=True)
    good = {"label": "a", "token": "0" * 32, "acquired_at": 1.0, "refreshed_at": 1.0, "pid": 1}
    for bad, why in ((dict(good, label="-x"), "label:format"), (dict(good, token=None), "token:null"),
                     (dict(good, refreshed_at=float("nan")), "refreshed_at:range"), ({**good, "pid": "1"}, "pid:type")):
        with pytest.raises(RG.SS.RowRefused, match=rf"^row_refused:heavy/active/owner.json:1:{why}$"):
            RG._write_owner(str(marker), bad)
        assert not (marker / "owner.json").exists()
    RG._write_owner(str(marker), good)
    assert json.loads((marker / "owner.json").read_text()) == good


def plan_append(tmp_path, row, raw=None):
    r = run("state_schema.py", "append-plan-row", "--row", raw if raw is not None else json.dumps(row),
            env=base_env(tmp_path))
    return r.returncode, r.stdout.strip()


def test_plan_row_writer_validates_before_appending(tmp_path):
    led = tmp_path / "state" / "plan-approvals.jsonl"
    assert plan_append(tmp_path, BASELINE) == (0, "APPENDED baseline")
    assert plan_append(tmp_path, APPROVAL) == (0, "APPENDED approval")
    before = led.read_text()
    for row, why in ((dict(APPROVAL, sha256={}), "3:sha256:empty"), (dict(BASELINE, evidence=""), "3:evidence:empty"),
                     ({"kind": "note"}, "3:row_type:unknown (kind must be baseline or approval)")):
        assert plan_append(tmp_path, row) == (2, f"ERROR row_refused:plan-approvals.jsonl:{why}")
    assert plan_append(tmp_path, None, raw="{broken") == (2, "ERROR row_refused:plan-approvals.jsonl:0:row:not_json")
    assert led.read_text() == before
    led.write_text(before + "{broken\n")
    assert plan_append(tmp_path, BASELINE) == (2, "ERROR state_corrupt:plan-approvals.jsonl:3:row:not_json")
    assert led.read_text() == before + "{broken\n"
