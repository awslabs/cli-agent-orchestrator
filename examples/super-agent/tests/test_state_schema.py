"""the shared validator itself (bin/state_schema.py): strict JSON, line reading, the field
checkers, the error strings, and the `check` CLI."""
import json

import pytest

from state_helpers import *  # noqa: F401,F403


@pytest.fixture
def ss():
    return load("state_schema")


def test_strict_json(ss):
    with pytest.raises(ValueError):
        ss.loads('{"a": 1, "a": 2}')
    for text in ("NaN", "Infinity", "-Infinity", '{"a": NaN}'):
        with pytest.raises(ValueError):
            ss.loads(text)
    assert ss.loads('{"a": 1.5}') == {"a": 1.5}


def test_jsonl_reading(tmp_path, ss):
    p = tmp_path / "x.jsonl"
    assert ss.jsonl_rows(p) == []
    p.write_bytes(b'{"a": 1}\n{"b": 2}\n')
    assert ss.jsonl_rows(p) == [(1, {"a": 1}), (2, {"b": 2})]
    p.write_bytes(b'{"a": 1}')
    assert ss.jsonl_rows(p) == [(1, {"a": 1})]
    for raw, why in ((b'{"a": 1}\n\n', "2:row:blank_line"), (b'\xff\n', "1:row:not_utf8"), (b'{"a": 1}\nx\n', "2:row:not_json")):
        p.write_bytes(raw)
        with pytest.raises(ss.StateCorrupt) as e:
            ss.jsonl_rows(p)
        assert str(e.value) == f"state_corrupt:x.jsonl:{why}"


@pytest.mark.parametrize("value,ok", [("2026-11-09T10:00:00+00:00", True), ("2026-11-09T10:00Z", True),
                                      ("2026-11-09T10:00:00", False), ("2026-11-09", False), ("x", False),
                                      (1, False), (None, False)])
def test_iso_needs_a_zone(ss, value, ok):
    assert (ss.iso(value) is None) is ok


def test_scalar_checkers(ss):
    assert ss.integer(1)(1) is None and ss.integer(1)(True) == "type" and ss.integer(1)(0) == "range"
    assert ss.integer(0)(1.0) == "type" and ss.integer(0)(None) == "null"
    assert ss.number(0)(1) is None and ss.number(0)(1.5) is None and ss.number(0)(False) == "type"
    assert ss.number(0)(float("inf")) == "range" and ss.number(0)(float("nan")) == "range" and ss.number(0)(-0.1) == "range" and ss.number(0)("1") == "type"
    assert ss.const(True)(1) == "type" and ss.const(True)(False) == "value" and ss.const(True)(True) is None
    assert ss.const("conductor")("conductor") is None and ss.const("conductor")("Conductor") == "value"
    assert ss.text()("a") is None and ss.text()(" ") == "empty" and ss.text()("a\nb") == "format"
    assert ss.text(one_line=False)("a\nb") is None and ss.text(max_len=3)("abcd") == "format"
    assert ss.text()(1) == "type" and ss.any_text("") is None and ss.any_text(1) == "type"
    assert ss.boolean(False) is None and ss.boolean(0) == "type" and ss.boolean(None) == "null"
    assert ss.nullable(ss.boolean)(None) is None and ss.nullable(ss.boolean)(1) == "type"
    assert ss.pattern(ss.SHA40)("a" * 40) is None and ss.pattern(ss.SHA40)("a" * 41) == "format"
    assert ss.map_of(ss.FILE_RE, ss.pattern(ss.HEX64))({"a.py": "0" * 64}) is None
    assert ss.map_of(ss.FILE_RE, ss.pattern(ss.HEX64))({"../a": "0" * 64}) == "format"
    assert ss.map_of(ss.FILE_RE, ss.pattern(ss.HEX64))([]) == "type"


def test_row_problem_order_and_unknown_fields(ss):
    row = {"key": "0" * 64, "url": "u", "recorded_at": "2026-11-09T10:00:00+00:00", "generation": 1}
    assert ss.HISTORY.problem(row) is None
    assert ss.HISTORY.problem([row]) == "row:not_object"
    assert ss.HISTORY.problem({**row, "z": 1}) == "z:unknown_field"
    assert ss.HISTORY.problem({k: v for k, v in row.items() if k != "url"}) == "url:missing"


def test_error_strings(ss):
    assert str(ss.StateCorrupt("go.jsonl", 3, "id:missing")) == "state_corrupt:go.jsonl:3:id:missing"
    assert str(ss.RowRefused("go.jsonl", 3, "id:missing")) == "row_refused:go.jsonl:3:id:missing"
    assert isinstance(ss.RowRefused("f", 1, "w"), ss.StateCorrupt) and isinstance(ss.StateCorrupt("f", 1, "w"), ValueError)


def test_check_cli_on_a_valid_state_dir(tmp_path):
    posted(tmp_path, keep_sent=True)
    k3 = key(tmp_path, body="third\n")
    token_of(reserve(tmp_path, k3, body="third\n"))
    assert owner_gone(tmp_path, k3).returncode == 0
    assert clear_item(tmp_path, MAINTAINER_CLEAN).stdout.startswith("CLEARED")
    expect(gate(tmp_path, pr(), [review(body="P3: x", rid=1005)]), HOLD, "HOLD")
    (tmp_path / "state" / "plan-approvals.jsonl").write_text(json.dumps(BASELINE) + "\n")
    (tmp_path / "state" / "heavy" / "active").mkdir(parents=True)
    write_json(tmp_path / "state" / "heavy" / "active" / "owner.json",
               {"label": "a", "token": "0" * 32, "acquired_at": 1.0, "refreshed_at": 1.0, "pid": 1})
    r = run("state_schema.py", "check", env=base_env(tmp_path))
    assert (r.returncode, r.stdout.strip()) == (0, "OK 9 files"), r.stdout


def test_check_cli_reports_every_corrupt_file(tmp_path):
    k = posted(tmp_path, keep_sent=True)
    (tmp_path / "state" / "outbox" / f"{k}.sent").write_text("{")
    (tmp_path / "state" / "go.jsonl").write_text("[]\n")
    (tmp_path / "state" / "holds.json").write_text('{"x": 1}')
    r = run("state_schema.py", "check", "--state-dir", tmp_path / "state", env=base_env(tmp_path))
    assert r.returncode == 1
    assert r.stdout.splitlines() == ["state_corrupt:go.jsonl:1:row:not_object",
                                     "state_corrupt:holds.json:x:row_type:unknown (want a PR number or _cleared)",
                                     f"state_corrupt:outbox/{k}.sent:1:document:not_json"]


def test_check_cli_on_a_state_dir_with_one_baseline(tmp_path):
    sd = tmp_path / "state"
    sd.mkdir()
    (sd / "plan-approvals.jsonl").write_text(json.dumps(BASELINE) + "\n")
    env = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}
    r = run("state_schema.py", "check", "--state-dir", sd, env=env)
    assert (r.returncode, r.stdout.strip()) == (0, "OK 1 files"), r.stdout
