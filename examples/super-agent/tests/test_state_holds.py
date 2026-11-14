"""holds.json: hold records with their items, and _cleared rows, through automerge_gate.py
and holds.py (readers and writers)."""
import json

import pytest

from state_helpers import *  # noqa: F401,F403


HOLD_WRONG = {"sha": 7, "date": 7, "head": 7, "detail": 7, "items": "x", "recorded_at": 7}


ITEM_WRONG = {"id": 7, "sha256": 7, "real": "false", "body": 7}


def test_hold_control_is_persisted(tmp_path):
    held(tmp_path)
    out = expect(gate(tmp_path, pr(), [review(rid=1009)]), HOLD, "HOLD")
    assert "persisted hold" in out


@pytest.mark.parametrize("field", list(HOLD_WRONG) + [f"items.{f}" for f in ITEM_WRONG] + ["unknown"])
@pytest.mark.parametrize("how", ["removed", "null", "wrong_type"])
def test_hold_field_fails_closed(tmp_path, field, how):
    h = held(tmp_path)
    doc_ = json.loads(h.read_text())
    rec, name, wrong, why = doc_["101"], field, HOLD_WRONG, f"{field}:"
    if field.startswith("items."):
        rec, name, wrong = rec["items"][0], field[6:], ITEM_WRONG
        why = f"items:[0].{name}:"
    if field == "unknown":
        rec["extra"] = 1
        why = "extra:unknown_field"
    elif how == "removed":
        del rec[name]
    else:
        rec[name] = None if how == "null" else wrong[name]
    h.write_text(json.dumps(doc_))
    out = expect(gate(tmp_path, pr(), [review(body=MAINTAINER_CLEAN)]), DENY, "DENY")
    assert out.startswith(f"DENY state_corrupt:holds.json:101:{why}"), out


@pytest.mark.parametrize("bad,why", [({"pr12": {}}, "pr12:row_type:unknown"), ({"_cleared": {}}, "_cleared:_cleared:type"),
                                     ({"101": {"items": []}}, "101:sha:missing")])
def test_holds_document_shapes_fail_closed(tmp_path, bad, why):
    (tmp_path / "state").mkdir()
    write_json(tmp_path / "state" / "holds.json", bad)
    out = expect(gate(tmp_path, pr(), [review()]), DENY, "DENY")
    assert out.startswith(f"DENY state_corrupt:holds.json:{why}"), out


def test_hold_with_no_items_fails_closed(tmp_path):
    h = held(tmp_path)
    d = json.loads(h.read_text())
    d["101"]["items"] = []
    h.write_text(json.dumps(d))
    assert expect(gate(tmp_path, pr(), [review()]), DENY, "DENY") == "DENY state_corrupt:holds.json:101:items:empty"


CLEARED_WRONG = {"pr": 101, "item_id": 7, "body_sha256": 7, "reason": 7, "go_id": 7, "cleared_at": 7}


def test_cleared_control_arms(tmp_path):
    assert clear_item(tmp_path, MAINTAINER_CLEAN).stdout.startswith("CLEARED")
    expect(gate(tmp_path, pr(), [review(body=MAINTAINER_CLEAN)]), ARM, "ARM")


@pytest.mark.parametrize("field", list(CLEARED_WRONG) + ["unknown"])
@pytest.mark.parametrize("how", ["removed", "null", "wrong_type"])
def test_cleared_field_fails_closed(tmp_path, field, how):
    """A damaged clear is never "not cleared" or "cleared": the gate DENYs, holds.py REFUSES."""
    assert clear_item(tmp_path, MAINTAINER_CLEAN).stdout.startswith("CLEARED")
    h = tmp_path / "state" / "holds.json"
    d = json.loads(h.read_text())
    row, why = d["_cleared"][0], f"{field}:"
    if field == "unknown":
        row["extra"] = 1
        why = "extra:unknown_field"
    elif how == "removed":
        del row[field]
    else:
        row[field] = None if how == "null" else CLEARED_WRONG[field]
    h.write_text(json.dumps(d))
    before = h.read_text()
    out = expect(gate(tmp_path, pr(), [review(body=MAINTAINER_CLEAN)]), DENY, "DENY")
    assert out.startswith(f"DENY state_corrupt:holds.json:_cleared[0]:{why}"), out
    r = clear_item(tmp_path, MAINTAINER_CLEAN, go=False)  # the GO is already in the ledger (ids are unique)
    assert r.returncode == 1 and r.stdout.startswith(f"REFUSED state_corrupt:holds.json:_cleared[0]:{why}"), r.stdout
    assert h.read_text() == before


def test_holds_writers_refuse_in_process(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPER_AGENT_STATE_DIR", str(tmp_path / "state"))
    HD = load("holds")
    h = tmp_path / "state" / "holds.json"
    row = {"pr": "101", "item_id": "review:1", "body_sha256": "0" * 64, "reason": "r", "go_id": "w",
           "cleared_at": "2026-11-09T10:00:00+00:00"}
    for bad, why in ((dict(row, pr="x"), "pr:format"), (dict(row, reason="a\nb"), "reason:format"),
                     (dict(row, item_id="review:None"), "item_id:format"), ({**row, "x": 1}, "x:unknown_field")):
        with pytest.raises(HD.SS.RowRefused, match=rf"^row_refused:holds.json:_cleared\[0\]:{why}$"):
            HD.write_cleared(str(h), bad)
        assert not h.exists()
    HD.write_cleared(str(h), row)  # the control
    assert json.loads(h.read_text())["_cleared"] == [row]
    AG = HD.GATE
    rec = {"sha": "a" * 40, "date": "2026-11-08T09:30:00Z", "head": "a" * 40, "detail": "x",
           "items": [{"id": "issue_comment:None", "sha256": "0" * 64, "real": False, "body": ""}]}
    before = h.read_text()
    for bad, why in ((dict(rec, items=[]), "items:empty"), (dict(rec, sha="x"), "sha:format"),
                     (dict(rec, items=[dict(rec["items"][0], real=None)]), r"items:\[0\]\.real:null")):
        with pytest.raises(AG.SS.RowRefused, match=rf"^row_refused:holds.json:101:{why}$"):
            AG.write_hold(str(h), 101, ("set", bad))
        assert h.read_text() == before
    AG.write_hold(str(h), 101, ("set", rec))  # the control (an id-less GitHub item is kind:None)
    assert json.loads(h.read_text())["101"]["items"][0]["id"] == "issue_comment:None"


def test_holds_clear_refuses_to_write_into_a_corrupt_holds_file(tmp_path):
    assert clear_item(tmp_path, MAINTAINER_CLEAN).stdout.startswith("CLEARED")
    h = tmp_path / "state" / "holds.json"
    d = json.loads(h.read_text())
    d["102"] = {"sha": "x"}
    h.write_text(json.dumps(d))
    before = h.read_text()
    r = clear_item(tmp_path, MAINTAINER_CLEAN, go=False)  # the GO is already in the ledger (ids are unique)
    assert r.stdout == "REFUSED state_corrupt:holds.json:102:sha:format\n" and h.read_text() == before
