"""A12: stop and clean-up, safety first.

These are the stop safety tests named in the PR description. The promise they hold in place: nothing
that is not listed, ticked and unchanged can be stopped, and there is no "all".

    test_stop_cannot_reach_a_session_that_is_not_in_the_plan
    test_stop_plan_expires_after_five_minutes
    test_stop_skips_a_session_whose_terminal_set_grew
    test_stop_skips_a_session_that_was_active_since_the_plan
    test_stop_refuses_working_sessions_without_acknowledgement
    test_stop_rejects_acknowledgement_in_a_cleanup_plan
    test_stop_request_schema_is_closed_no_all_no_wildcard
    test_stop_never_exceeds_25_targets
    test_stop_halts_on_the_first_transport_error
    test_stop_always_sends_the_canonical_cao_name
    test_no_delete_is_ever_sent_for_a_session_that_was_not_listed
"""

from __future__ import annotations

import logging
import random
from pathlib import Path
from typing import Any, Dict, Iterator, List

import cao_shelf_support as sup
import pytest
from cao_shelf.actions import PLAN_TTL_SECONDS
from cao_shelf.errors import ShelfError
from cao_shelf_support import ActionsRig, Stack, validate


@pytest.fixture
def rig(tmp_path: Path) -> ActionsRig:
    return ActionsRig(tmp_path, "quiet").warm()


@pytest.fixture
def busy(tmp_path: Path) -> ActionsRig:
    return ActionsRig(tmp_path, "busy").warm()


def plan_stale(rig: ActionsRig) -> Dict[str, Any]:
    return rig.actions.stop_plan({"scope": "stale"})


def plan_named(rig: ActionsRig, *names: str) -> Dict[str, Any]:
    return rig.actions.stop_plan({"scope": "named", "sessions": list(names)})


def item(plan: Dict[str, Any], name: str, **extra: Any) -> Dict[str, Any]:
    entry = next(s for s in plan["sessions"] if s["name"] == name)
    return {"name": name, "last_active": entry["last_active"], **extra}


def execute(rig: ActionsRig, plan: Dict[str, Any], *items: Dict[str, Any]) -> List[Dict[str, Any]]:
    return rig.actions.stop_execute({"plan_id": plan["plan_id"], "sessions": list(items)})[
        "results"
    ]


def outcomes(results: List[Dict[str, Any]]) -> Dict[str, str]:
    return {r["name"]: r["outcome"] for r in results}


def err(fn: Any, *a: Any) -> ShelfError:
    with pytest.raises(ShelfError) as exc:
        fn(*a)
    return exc.value


# ------------------------------------------------------------------------------------ the plan
def test_stale_plan_lists_only_stale_sessions_oldest_first(rig: ActionsRig) -> None:
    plan = plan_stale(rig)
    validate("actions.schema.json", plan, "StopPlan")
    assert len(plan["sessions"]) == 9 and plan["truncated"] is False
    assert all(s["tier"] == "stale" for s in plan["sessions"])
    ages = [s["last_active"] for s in plan["sessions"]]
    assert ages == sorted(ages)  # oldest idle first = smallest timestamp first
    assert plan["sessions"][0]["name"] == "ci-flakes"
    assert rig.cao.writes() == []  # a plan writes nothing to CAO
    one = plan["sessions"][0]
    assert one["terminals"][0] == {
        "id": one["terminals"][0]["id"],
        "label": "conductor",
        "role": "conductor",
        "status": "idle",
    }
    assert one["working"] is False


def test_stale_plan_lists_nothing_that_is_not_stale_now(busy: ActionsRig) -> None:
    plan = plan_stale(busy)
    names = {s["name"] for s in plan["sessions"]}
    assert "payments-fix" not in names and "docs-refresh" not in names and "migrate-db" not in names
    assert len(names) == 9


def test_stale_plan_rechecks_live_and_drops_a_session_that_woke_up(rig: ActionsRig) -> None:
    victim = rig.cao.session_terminals("ci-flakes")[0]["id"]
    rig.cao.set_status(
        victim, "processing", "2026-11-15T11:59:00.000000Z"
    )  # the shelf has not seen it yet
    plan = plan_stale(rig)
    assert "ci-flakes" not in {s["name"] for s in plan["sessions"]}


def test_stale_plan_caps_at_25_and_says_so(rig: ActionsRig) -> None:
    rig.add_stale(30)
    rig.warm(40)
    mark = len(rig.cao.calls)
    plan = plan_stale(rig)
    assert len(plan["sessions"]) == 25 and plan["truncated"] is True
    # only the listed ones were read for the plan, not all 39 stale sessions
    assert sum(1 for m, p, _ in rig.cao.calls[mark:] if p.startswith("/sessions/cao-")) <= 25


def test_named_plan_lists_any_tier_and_flags_working(busy: ActionsRig) -> None:
    plan = plan_named(busy, "payments-fix", "migrate-db")
    by = {s["name"]: s for s in plan["sessions"]}
    assert by["payments-fix"]["working"] is True and by["payments-fix"]["tier"] == "needs_you"
    assert by["migrate-db"]["working"] is False and by["migrate-db"]["tier"] == "parked"
    assert [t["role"] for t in by["payments-fix"]["terminals"]] == [
        "conductor",
        "helper",
        "helper",
        "helper",
    ]
    validate("actions.schema.json", plan, "StopPlan")


def test_named_plan_for_a_missing_session_is_not_found(rig: ActionsRig) -> None:
    assert (
        err(rig.actions.stop_plan, {"scope": "named", "sessions": ["no-such"]}).code == "not_found"
    )


def test_plan_request_shapes(rig: ActionsRig) -> None:
    bad = [
        {},
        {"scope": "all"},
        {"scope": "stale", "sessions": ["ci-flakes"]},
        {"scope": "named"},
        {"scope": "named", "sessions": []},
        {"scope": "named", "sessions": ["a", "a"]},
        {"scope": "named", "sessions": ["../x"]},
        {"scope": "named", "sessions": ["*"]},
        {"scope": "named", "sessions": "ci-flakes"},
        {"scope": "stale", "all": True},
    ]
    for body in bad:
        assert err(rig.actions.stop_plan, body).code == "invalid_request", body
    many = {"scope": "named", "sessions": [f"s{i}" for i in range(26)]}
    assert err(rig.actions.stop_plan, many).code == "too_many"
    assert rig.cao.writes() == []


# ---------------------------------------------------------------------------------- the execute
def test_stop_executes_exactly_the_ticked_sessions(rig: ActionsRig) -> None:
    plan = plan_stale(rig)
    res = execute(rig, plan, item(plan, "ci-flakes"), item(plan, "infra-audit"))
    assert outcomes(res) == {"ci-flakes": "stopped", "infra-audit": "stopped"}
    assert rig.cao.deletes() == ["cao-ci-flakes", "cao-infra-audit"]
    validate("actions.schema.json", {"results": res}, "StopExecuteResponse")
    assert "cao-payments-fix" in rig.cao.sessions and "cao-docs-refresh" in rig.cao.sessions
    assert "cao-ci-flakes" not in rig.cao.sessions


def test_stop_cannot_reach_a_session_that_is_not_in_the_plan(rig: ActionsRig) -> None:
    plan = plan_named(rig, "ci-flakes")
    other = {"name": "infra-audit", "last_active": "2026-11-06T12:00:00.000000Z"}
    res = execute(rig, plan, other, item(plan, "ci-flakes"))
    assert outcomes(res) == {"infra-audit": "skipped_not_in_plan", "ci-flakes": "stopped"}
    assert rig.cao.deletes() == ["cao-ci-flakes"]
    assert "cao-infra-audit" in rig.cao.sessions


def test_a_plan_cannot_be_used_for_another_plans_sessions(rig: ActionsRig) -> None:
    a = plan_named(rig, "ci-flakes")
    b = plan_named(rig, "infra-audit")
    res = execute(rig, a, item(b, "infra-audit"))
    assert outcomes(res) == {"infra-audit": "skipped_not_in_plan"}
    assert rig.cao.deletes() == []


def test_stop_plan_expires_after_five_minutes(rig: ActionsRig) -> None:
    plan = plan_named(rig, "ci-flakes")
    assert PLAN_TTL_SECONDS == 300
    rig.now += 299
    assert (
        err(
            rig.actions.stop_execute, {"plan_id": "nope", "sessions": [item(plan, "ci-flakes")]}
        ).code
        == "plan_expired"
    )
    rig.now += 2
    e = err(
        rig.actions.stop_execute,
        {"plan_id": plan["plan_id"], "sessions": [item(plan, "ci-flakes")]},
    )
    assert (e.code, e.http_status) == ("plan_expired", 409)
    assert rig.cao.deletes() == []


def test_plan_still_valid_just_before_expiry(rig: ActionsRig) -> None:
    plan = plan_named(rig, "ci-flakes")
    rig.now += 299
    assert outcomes(execute(rig, plan, item(plan, "ci-flakes"))) == {"ci-flakes": "stopped"}


def test_stop_skips_a_session_whose_terminal_set_grew(rig: ActionsRig) -> None:
    plan = plan_named(rig, "ci-flakes", "infra-audit")
    rig.cao.add_session("tmp", [("0c0c0c0c", "idle", "2026-11-01T00:00:00.000000Z", None)])
    rig.cao.session_terminals("ci-flakes").append(
        dict(rig.cao.session_terminals("ci-flakes")[0], id="0d0d0d0d")
    )
    res = execute(rig, plan, item(plan, "ci-flakes"), item(plan, "infra-audit"))
    assert outcomes(res) == {"ci-flakes": "skipped_changed", "infra-audit": "stopped"}
    assert rig.cao.deletes() == ["cao-infra-audit"]


def test_a_smaller_terminal_set_is_still_a_subset(busy: ActionsRig) -> None:
    plan = plan_named(busy, "api-cleanup")
    gone = busy.cao.session_terminals("api-cleanup").pop()
    assert gone
    assert outcomes(execute(busy, plan, item(plan, "api-cleanup"))) == {"api-cleanup": "stopped"}


def test_stop_skips_a_session_that_was_active_since_the_plan(rig: ActionsRig) -> None:
    plan = plan_named(rig, "ci-flakes")
    tid = rig.cao.session_terminals("ci-flakes")[0]["id"]
    rig.cao.set_status(tid, "idle", "2026-11-15T11:00:00.000000Z")  # moved, still idle
    assert outcomes(execute(rig, plan, item(plan, "ci-flakes"))) == {"ci-flakes": "skipped_changed"}
    assert rig.cao.deletes() == []


def test_stop_skips_when_the_echoed_time_is_not_the_plans(rig: ActionsRig) -> None:
    plan = plan_named(rig, "ci-flakes")
    wrong = {"name": "ci-flakes", "last_active": "2026-11-15T11:00:00.000000Z"}
    assert outcomes(execute(rig, plan, wrong)) == {"ci-flakes": "skipped_changed"}
    assert rig.cao.deletes() == []


def test_stop_refuses_working_sessions_without_acknowledgement(busy: ActionsRig) -> None:
    plan = plan_named(busy, "payments-fix")
    assert plan["sessions"][0]["working"] is True
    assert outcomes(execute(busy, plan, item(plan, "payments-fix"))) == {
        "payments-fix": "skipped_working"
    }
    again = plan_named(busy, "payments-fix")
    assert outcomes(
        execute(busy, again, item(again, "payments-fix", acknowledge_working=False))
    ) == {"payments-fix": "skipped_working"}
    assert busy.cao.deletes() == []


def test_acknowledged_working_session_is_stopped_in_a_named_plan(busy: ActionsRig) -> None:
    plan = plan_named(busy, "payments-fix")
    res = execute(busy, plan, item(plan, "payments-fix", acknowledge_working=True))
    assert outcomes(res) == {"payments-fix": "stopped"} and busy.cao.deletes() == [
        "cao-payments-fix"
    ]


def test_a_session_that_became_busy_after_the_plan_needs_acknowledgement(rig: ActionsRig) -> None:
    plan = plan_named(rig, "ci-flakes")
    tid = rig.cao.session_terminals("ci-flakes")[0]["id"]
    rig.cao.set_status(tid, "waiting_user_answer")  # status only: last_active is the same
    assert outcomes(execute(rig, plan, item(plan, "ci-flakes"))) == {"ci-flakes": "skipped_working"}
    assert rig.cao.deletes() == []


def test_stop_rejects_acknowledgement_in_a_cleanup_plan(rig: ActionsRig) -> None:
    plan = plan_stale(rig)
    e = err(
        rig.actions.stop_execute,
        {
            "plan_id": plan["plan_id"],
            "sessions": [
                item(plan, "ci-flakes"),
                item(plan, "infra-audit", acknowledge_working=True),
            ],
        },
    )
    assert e.code == "invalid_request"
    assert rig.cao.deletes() == []  # nothing, not even the unacknowledged one


def test_a_cleanup_plan_never_stops_a_session_that_woke_up(rig: ActionsRig) -> None:
    plan = plan_stale(rig)
    tid = rig.cao.session_terminals("ci-flakes")[0]["id"]
    rig.cao.set_status(tid, "processing")
    assert outcomes(execute(rig, plan, item(plan, "ci-flakes"))) == {"ci-flakes": "skipped_working"}


def test_stop_request_schema_is_closed_no_all_no_wildcard(rig: ActionsRig) -> None:
    plan = plan_stale(rig)
    good = item(plan, "ci-flakes")
    pid = plan["plan_id"]
    bad_bodies: List[Any] = [
        {"plan_id": pid, "all": True, "sessions": [good]},
        {"plan_id": pid, "sessions": [good], "everything": True},
        {"plan_id": pid, "sessions": "*"},
        {"plan_id": pid, "sessions": ["*"]},
        {"plan_id": pid, "sessions": [{"name": "*", "last_active": "x"}]},
        {"plan_id": pid, "sessions": [{"name": "ci-flakes", "last_active": "x", "all": True}]},
        {"plan_id": pid, "sessions": [{"name": "ci-flakes"}]},
        {"plan_id": pid, "sessions": [{"last_active": "x"}]},
        {"plan_id": pid, "sessions": []},
        {"plan_id": pid},
        {"sessions": [good]},
        {"plan_id": pid, "sessions": [good], "scope": "stale"},
        {"plan_id": pid, "sessions": [{**good, "acknowledge_working": "yes"}]},
        {"plan_id": pid, "sessions": [{"name": "../x", "last_active": "x"}]},
        {"plan_id": pid, "sessions": [{"name": "cao-ci-flakes/../x", "last_active": "x"}]},
        {"plan_id": 7, "sessions": [good]},
        [],
        "all",
    ]
    for body in bad_bodies:
        with pytest.raises(ShelfError) as exc:
            rig.actions.stop_execute(body)
        assert exc.value.code == "invalid_request", body
    assert rig.cao.deletes() == []


def test_stop_never_exceeds_25_targets(rig: ActionsRig) -> None:
    names = rig.add_stale(30)
    rig.warm(40)
    plan = plan_stale(rig)
    items = [item(plan, s["name"]) for s in plan["sessions"]]
    assert len(items) == 25
    assert len(execute(rig, plan, *items)) == 25 and len(rig.cao.deletes()) <= 25
    extra = {"name": names[-1], "last_active": "2026-11-01T00:00:00.000000Z"}
    too_many = {"plan_id": plan["plan_id"], "sessions": items + [extra]}
    assert err(rig.actions.stop_execute, too_many).code == "too_many"
    assert len(rig.cao.deletes()) <= 25


def test_stop_halts_on_the_first_transport_error(rig: ActionsRig) -> None:
    plan = plan_named(rig, "ci-flakes", "infra-audit", "data-backfill")
    original_open = rig.opener.open

    def open_and_fail(req: Any, timeout: Any = None) -> Any:
        if req.get_method() == "DELETE" and req.full_url.endswith("cao-infra-audit"):
            import urllib.error

            rig.cao.calls.append(("DELETE", "/sessions/cao-infra-audit", {}))
            raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))
        return original_open(req, timeout)

    rig.opener.open = open_and_fail  # type: ignore[method-assign]
    items = [item(plan, "ci-flakes"), item(plan, "infra-audit"), item(plan, "data-backfill")]
    res = execute(rig, plan, *items)
    assert [r["name"] for r in res] == [
        "ci-flakes",
        "infra-audit",
        "data-backfill",
    ]  # request order
    assert outcomes(res) == {
        "ci-flakes": "stopped",
        "infra-audit": "failed",
        "data-backfill": "failed",
    }
    assert "not attempted" in res[2]["message"]
    assert rig.cao.deletes() == ["cao-ci-flakes", "cao-infra-audit"]  # never tried the third


def test_other_cao_failures_do_not_halt_the_rest(rig: ActionsRig) -> None:
    plan = plan_named(rig, "ci-flakes", "infra-audit")
    real = rig.cao._route

    def deferred(method: str, path: str, query: Dict[str, str], body: Any) -> Any:
        if method == "DELETE" and path.endswith("cao-ci-flakes"):
            return 409, {"detail": "cleanup deferred"}
        return real(method, path, query, body)

    rig.cao._route = deferred  # type: ignore[method-assign]
    res = execute(rig, plan, item(plan, "ci-flakes"), item(plan, "infra-audit"))
    assert outcomes(res) == {"ci-flakes": "failed", "infra-audit": "stopped"}
    assert all(len(r.get("message", "")) <= 300 for r in res)


def test_a_session_gone_since_the_plan_is_not_found_and_not_deleted(rig: ActionsRig) -> None:
    plan = plan_named(rig, "ci-flakes")
    del rig.cao.sessions["cao-ci-flakes"]
    rig.cao.order.remove("cao-ci-flakes")
    assert outcomes(execute(rig, plan, item(plan, "ci-flakes"))) == {"ci-flakes": "not_found"}
    assert rig.cao.deletes() == []


def test_a_plan_is_single_use_per_name(rig: ActionsRig) -> None:
    plan = plan_named(rig, "ci-flakes", "infra-audit")
    first = execute(rig, plan, item(plan, "ci-flakes"))
    assert outcomes(first) == {"ci-flakes": "stopped"}
    again = execute(rig, plan, item(plan, "ci-flakes"), item(plan, "infra-audit"))
    assert outcomes(again) == {"ci-flakes": "skipped_not_in_plan", "infra-audit": "stopped"}
    dup = plan_named(rig, "data-backfill")
    res = execute(rig, dup, item(dup, "data-backfill"), item(dup, "data-backfill"))
    assert [r["outcome"] for r in res] == ["stopped", "skipped_not_in_plan"]
    assert rig.cao.deletes().count("cao-data-backfill") == 1


def test_a_skipped_session_burns_its_plan_entry(rig: ActionsRig) -> None:
    plan = plan_named(rig, "ci-flakes")
    tid = rig.cao.session_terminals("ci-flakes")[0]["id"]
    rig.cao.set_status(tid, "idle", "2026-11-15T11:00:00.000000Z")
    assert outcomes(execute(rig, plan, item(plan, "ci-flakes"))) == {"ci-flakes": "skipped_changed"}
    retry = {"name": "ci-flakes", "last_active": "2026-11-15T11:00:00.000000Z"}
    assert outcomes(execute(rig, plan, retry)) == {"ci-flakes": "skipped_not_in_plan"}
    assert rig.cao.deletes() == []


def test_stop_always_sends_the_canonical_cao_name(rig: ActionsRig) -> None:
    plan = plan_named(rig, "ci-flakes", "infra-audit")
    execute(rig, plan, item(plan, "ci-flakes"), item(plan, "infra-audit"))
    for method, path, _ in rig.cao.writes():
        assert method == "DELETE" and path.startswith("/sessions/cao-")
    assert rig.cao.deletes() == ["cao-ci-flakes", "cao-infra-audit"]


def test_a_bare_name_that_looks_prefixed_is_a_different_session(rig: ActionsRig) -> None:
    rig.cao.add_session(
        "cao-ci-flakes", [("0e0e0e0e", "idle", "2026-11-01T00:00:00.000000Z", None)]
    )
    rig.cao.sessions.pop("cao-cao-ci-flakes")  # then it vanishes: only the real "ci-flakes" is left
    rig.cao.order.remove("cao-cao-ci-flakes")
    assert (
        err(rig.actions.stop_plan, {"scope": "named", "sessions": ["cao-ci-flakes"]}).code
        == "not_found"
    )
    assert rig.cao.writes() == [] and "cao-ci-flakes" in rig.cao.sessions


def test_no_delete_is_ever_sent_for_a_session_that_was_not_listed(tmp_path: Path) -> None:
    """A counting fake CAO: whatever the request holds, DELETEs are a subset of plan and request."""
    rng = random.Random(7)
    (tmp_path / "x").mkdir(exist_ok=True)
    pool = [r["name"][4:] for r in sup.load(sup.FIXTURES / "cao" / "quiet" / "sessions.json")]
    hostile = [
        "*",
        "all",
        "../x",
        "cao-",
        "",
        "a b",
        "ci-flakes/../infra-audit",
        "CI-FLAKES",
        "ci-flakes ",
        "ci-flakes\n",
    ]
    for round_ in range(25):
        rig = ActionsRig(tmp_path / f"r{round_}", "quiet").warm()
        plan = plan_named(rig, *rng.sample(pool, 3)) if rng.random() < 0.5 else plan_stale(rig)
        listed = {s["name"] for s in plan["sessions"]}
        asked = []
        for name in rng.sample(pool, 5) + rng.sample(hostile, 3):
            asked.append(
                {"name": name, "last_active": rng.choice(["x", "2026-11-15T10:00:00.000000Z"])}
            )
        for s in plan["sessions"]:
            if rng.random() < 0.5:
                asked.append({"name": s["name"], "last_active": s["last_active"]})
        before = len(rig.cao.deletes())
        try:
            rig.actions.stop_execute({"plan_id": plan["plan_id"], "sessions": asked})
        except ShelfError:
            pass
        sent = set(rig.cao.deletes()[before:])
        requested = {f"cao-{a['name']}" for a in asked}
        assert sent <= {f"cao-{n}" for n in listed} & requested


def test_stop_audit_line_has_names_and_counts_only(
    rig: ActionsRig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    plan = plan_named(rig, "ci-flakes")
    execute(rig, plan, item(plan, "ci-flakes"))
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("stop ")]
    assert lines == ["stop session=ci-flakes terminals=1 outcome=stopped"]


def test_plan_store_is_bounded(rig: ActionsRig) -> None:
    for _ in range(40):
        plan_named(rig, "ci-flakes")
    assert len(rig.actions._plans) <= 20


# ------------------------------------------------------------------------------- over HTTP
@pytest.fixture
def stack(tmp_path: Path) -> Iterator[Stack]:
    s = Stack(tmp_path / "data", "quiet")
    try:
        s.warm()
        yield s
    finally:
        s.close()


def test_http_plan_then_execute_and_the_closed_schema(stack: Stack) -> None:
    r = stack.post("/api/stop/plan", {"scope": "stale"})
    assert r.status == 200
    validate("actions.schema.json", r.json, "StopPlan")
    plan = r.json
    first = plan["sessions"][0]
    body = {
        "plan_id": plan["plan_id"],
        "sessions": [{"name": first["name"], "last_active": first["last_active"]}],
    }
    ok = stack.post("/api/stop/execute", body)
    assert ok.status == 200
    validate("actions.schema.json", ok.json, "StopExecuteResponse")
    assert ok.json["results"] == [{"name": first["name"], "outcome": "stopped"}]
    assert stack.cao.deletes() == [f"cao-{first['name']}"]
    for extra in ({"all": True}, {"scope": "stale"}, {"everything": 1}):
        refused = stack.post("/api/stop/execute", {**body, **extra})
        assert refused.status == 400 and refused.json["error"]["code"] == "invalid_request"
    assert len(stack.cao.deletes()) == 1


def test_http_expired_plan_is_409(stack: Stack) -> None:
    plan = stack.post("/api/stop/plan", {"scope": "stale"}).json
    stack.now += 301
    first = plan["sessions"][0]
    r = stack.post(
        "/api/stop/execute",
        {
            "plan_id": plan["plan_id"],
            "sessions": [{"name": first["name"], "last_active": first["last_active"]}],
        },
    )
    assert r.status == 409 and r.json["error"]["code"] == "plan_expired"
    validate("errors.schema.json", r.json)
    assert stack.cao.deletes() == []


def test_http_too_many(stack: Stack) -> None:
    r = stack.post("/api/stop/plan", {"scope": "named", "sessions": [f"s{i}" for i in range(26)]})
    assert r.status == 400 and r.json["error"]["code"] == "too_many"
    r2 = stack.post(
        "/api/stop/execute",
        {"plan_id": "x", "sessions": [{"name": f"s{i}", "last_active": "x"} for i in range(26)]},
    )
    assert r2.status == 400 and r2.json["error"]["code"] == "too_many"


def test_http_stop_audit_never_logs_bodies(stack: Stack, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    plan = stack.post("/api/stop/plan", {"scope": "named", "sessions": ["ci-flakes"]}).json
    stack.post(
        "/api/stop/execute",
        {
            "plan_id": plan["plan_id"],
            "sessions": [{"name": "ci-flakes", "last_active": plan["sessions"][0]["last_active"]}],
        },
    )
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "stop session=ci-flakes terminals=1 outcome=stopped" in text
    assert plan["plan_id"] not in text
