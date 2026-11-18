"""``cao_shelf.relays``: attach, start-and-attach, end, take over, reconcile, card signals.

Runs over the fake CAO and the fake Kiro Crew gateway, in process, on a fake clock.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from cao_shelf import relays as relays_mod
from cao_shelf.errors import ShelfError
from cao_shelf_support import (
    RELAY_CONDUCTOR,
    RELAY_HELPER,
    RELAY_SESSION,
    SENTINEL,
    RelayRig,
    contract_texts,
)

ACK = {"acknowledge_risk": True}


@pytest.fixture()
def rig(tmp_path: Path) -> RelayRig:
    return RelayRig(tmp_path).warm()  # type: ignore[return-value]


def only_slot(rig: RelayRig) -> Dict[str, Any]:
    assert len(rig.gw.slots) == 1, rig.gw.slots
    return next(iter(rig.gw.slots.values()))


# ------------------------------------------------------------------------------- 9.3 the risk
def test_the_risk_text_is_the_contract_text() -> None:
    import re

    doc = (Path(relays_mod.__file__).resolve().parents[2] / "api-contract.md").read_text("utf-8")
    m = re.search(r"<!-- risk:relay -->\n```text\n(.*?)\n```", doc, re.S)
    assert m is not None and m.group(1) == relays_mod.RISK
    d = re.search(r"<!-- risk:relay-detail -->\n```text\n(.*?)\n```", doc, re.S)
    assert d is not None and tuple(d.group(1).split("\n\n")) == relays_mod.RISK_DETAIL


def test_the_risk_text_names_both_backends_and_host_added_servers() -> None:
    # the short text is short, and the detail is true on kas as well as on claude
    assert len(relays_mod.RISK.split(". ")) <= 3
    kas, claude, both = relays_mod.RISK_DETAIL
    assert kas.startswith("On the kas backend") and "without asking you" in kas
    assert claude.startswith("On the claude backend") and "autoAllowBashIfSandboxed" in claude
    assert "credentials server" in both and "refuse any request" in both
    assert "credentials server" in relays_mod.RISK


def test_the_risk_detail_names_the_wait_tool_beside_the_four_cao_tools() -> None:
    # Kiro Crew's wait tool is offered to the relay chat too; it only pauses the chat
    kas, claude, both = relays_mod.RISK_DETAIL
    assert "Kiro Crew's wait tool, which only pauses the chat" in kas
    assert "(which only pauses the chat)" in claude
    assert both.endswith("other than the four CAO tools and wait.")
    for paragraph in relays_mod.RISK_DETAIL:
        assert "running" not in paragraph.lower()  # the page's bundle guard refuses the word


@pytest.mark.parametrize("ack", [None, False, "yes"])
def test_attach_and_start_and_attach_need_the_risk_accepted(rig: RelayRig, ack: Any) -> None:
    extra = {} if ack is None else {"acknowledge_risk": ack}
    for body in ({"session": RELAY_SESSION, **extra}, {"start": start_body("risky"), **extra}):
        with pytest.raises(ShelfError) as err:
            rig.relays.create(body)
        assert err.value.code == ("invalid_request" if ack == "yes" else "risk_not_acknowledged")
    assert rig.gw.calls == [] and rig.relays.list() == {"relays": []}
    assert not any(c[0] == "POST" and c[1] == "/sessions" for c in rig.cao.calls)


def test_take_over_needs_the_risk_accepted_and_leaves_the_old_relay(rig: RelayRig) -> None:
    old = rig.attach()
    calls = len(rig.gw.calls)
    with pytest.raises(ShelfError) as err:
        rig.relays.takeover(old["relay_id"], {})
    assert err.value.code == "risk_not_acknowledged"
    assert len(rig.gw.calls) == calls
    assert [r["state"] for r in rig.relays.list()["relays"]] == ["active"]


# ------------------------------------------------------------------------------- 3.1 attach
def test_attach_creates_a_titled_chat_in_normal_mode_with_the_setup_message(rig: RelayRig) -> None:
    out = rig.attach()
    assert out["state"] == "active" and out["origin"] == "attached"
    assert out["session"] == RELAY_SESSION and out["label"] == RELAY_SESSION
    assert out["conductor_id"] == RELAY_CONDUCTOR
    slot = rig.gw.slots[out["slot_key"]]
    assert slot["title"] == f"Remote control: {RELAY_SESSION}"
    assert slot["agent"] == "cao-remote-control" and slot["agent_kind"] == "template"
    assert slot["mode"] == "normal"
    setup = rig.gw.messages(out["slot_key"])
    assert len(setup) == 1
    assert f"session: {RELAY_SESSION}" in setup[0]
    assert f"conductor_terminal_id: {RELAY_CONDUCTOR}" in setup[0]
    assert f"relay_id: {out['relay_id']}" in setup[0]
    assert "handshake: pending" in setup[0]
    listed = rig.relays.list()["relays"]
    assert [(r["relay_id"], r["state"]) for r in listed] == [(out["relay_id"], "active")]


def test_attach_uses_the_given_label_in_the_title(rig: RelayRig) -> None:
    out = rig.attach(label="payments from phone")
    assert only_slot(rig)["title"] == "Remote control: payments from phone"
    assert out["label"] == "payments from phone"


def test_trust_mode_is_set_before_the_setup_message(rig: RelayRig) -> None:
    rig.attach()
    routes = [(m, r) for m, r, _ in rig.gw.calls if not r.endswith("/token")]
    assert routes == [
        ("POST", "/api/chat/slots"),
        ("POST", "/api/chat/mode"),
        ("POST", "/api/chat"),
    ]
    mode_body = next(b for m, r, b in rig.gw.calls if r == "/api/chat/mode")
    assert mode_body["mode"] == "normal"


def test_attach_makes_no_cao_write(rig: RelayRig) -> None:
    before = len(rig.cao_writes())
    rig.attach()
    assert len(rig.cao_writes()) == before


def test_session_without_a_conductor_is_refused_and_no_chat_is_made(rig: RelayRig) -> None:
    when = "2026-11-15T11:00:00.000000Z"
    rig.cao.add_session("orphans", [("bead0001", "idle", when, "deadbeef")])
    rig.warm()
    with pytest.raises(ShelfError) as err:
        rig.attach("orphans")
    assert err.value.code == "no_conductor"
    assert rig.gw.slots == {} and rig.gw.calls == []
    assert rig.relays.list()["relays"] == []


def test_unknown_session_is_not_found(rig: RelayRig) -> None:
    with pytest.raises(ShelfError) as err:
        rig.attach("no-such-session")
    assert err.value.code == "not_found"
    assert rig.gw.slots == {}


@pytest.mark.parametrize(
    "route", ["/api/chat/slots", "/api/chat/mode", "/api/chat"], ids=["slot", "mode", "setup"]
)
def test_each_failure_step_leaves_no_record_and_no_chat(rig: RelayRig, route: str) -> None:
    rig.gw.fail[("POST", route)] = 403
    with pytest.raises(ShelfError) as err:
        rig.attach()
    assert err.value.code == "gateway_refused"
    assert err.value.detail and err.value.detail["gateway_status"] == 403
    assert rig.gw.slots == {}
    assert rig.relays.list()["relays"] == []
    assert rig.make_relays().list()["relays"] == []  # nothing on disk either


def test_mode_failure_deletes_the_chat_and_refuses(rig: RelayRig) -> None:
    rig.gw.fail[("POST", "/api/chat/mode")] = 403
    with pytest.raises(ShelfError):
        rig.attach()
    assert rig.gw.count("DELETE", "/api/chat/slots/{slot}") == 1
    assert rig.gw.count("POST", "/api/chat") == 0
    assert rig.gw.slots == {}


def test_gateway_unavailable_refuses_without_a_record(tmp_path: Path) -> None:
    rig = RelayRig(tmp_path)
    rig.warm()
    rig.gw_opener.down = True
    with pytest.raises(ShelfError) as err:
        rig.attach()
    assert err.value.code == "gateway_unavailable"
    assert rig.relays.list()["relays"] == []


def test_second_attach_is_relay_exists_with_the_first_relays_id_and_label(rig: RelayRig) -> None:
    first = rig.attach(label="first")
    calls = len(rig.gw.calls)
    with pytest.raises(ShelfError) as err:
        rig.attach(label="second")
    assert err.value.code == "relay_exists"
    assert err.value.detail == {"relay_id": first["relay_id"], "label": "first"}
    assert len(rig.gw.calls) == calls  # nothing new was created


def test_take_over_ends_the_old_relay_and_attaches_a_new_chat(rig: RelayRig) -> None:
    old = rig.attach(label="laptop")
    new = rig.relays.takeover(old["relay_id"], {"acknowledge_risk": True, "label": "phone"})
    assert new["relay_id"] != old["relay_id"] and new["state"] == "active"
    assert new["slot_key"] != old["slot_key"]
    assert rig.gw.slots[new["slot_key"]]["title"] == "Remote control: phone"
    states = {r["relay_id"]: r for r in rig.relays.list()["relays"]}
    assert states[old["relay_id"]]["state"] == "ended"
    assert states[old["relay_id"]]["end_reason"] == "taken_over"
    ended_msg = rig.gw.messages(old["slot_key"])[-1]
    assert ended_msg.startswith("CAO Shelf relay ended")
    assert "reason: taken_over" in ended_msg


def test_take_over_failure_keeps_the_old_relay_active(rig: RelayRig) -> None:
    old = rig.attach()
    rig.gw.fail[("POST", "/api/chat/slots")] = 500
    with pytest.raises(ShelfError):
        rig.relays.takeover(old["relay_id"], ACK)
    states = {r["relay_id"]: r["state"] for r in rig.relays.list()["relays"]}
    assert states == {old["relay_id"]: "active"}
    assert len(rig.gw.messages(old["slot_key"])) == 1  # no relay-ended message


def test_take_over_of_an_ended_relay_is_refused(rig: RelayRig) -> None:
    old = rig.attach()
    rig.relays.end(old["relay_id"], {})
    with pytest.raises(ShelfError) as err:
        rig.relays.takeover(old["relay_id"], ACK)
    assert err.value.code == "conflict_changed"


def test_end_posts_the_relay_ended_message_and_makes_no_cao_call(rig: RelayRig) -> None:
    out = rig.attach()
    cao_calls = len(rig.cao.calls)
    res = rig.relays.end(out["relay_id"], {})
    assert res["relay"]["state"] == "ended" and res["relay"]["end_reason"] == "ended_by_owner"
    assert res["chat"] == "notified"
    assert len(rig.cao.calls) == cao_calls  # not even a read
    msg = rig.gw.messages(out["slot_key"])[-1]
    assert msg.splitlines()[0] == "CAO Shelf relay ended"
    assert f"relay_id: {out['relay_id']}" in msg
    assert "reason: ended_by_owner (you pressed End relay)" in msg
    assert out["slot_key"] in rig.gw.slots  # the transcript is kept


def test_end_with_delete_chat_deletes_it_instead(rig: RelayRig) -> None:
    out = rig.attach()
    res = rig.relays.end(out["relay_id"], {"delete_chat": True})
    assert res["chat"] == "deleted"
    assert rig.gw.slots == {}
    assert rig.gw.count("POST", "/api/chat") == 1  # only the setup message ever


def test_end_when_the_chat_is_already_gone_still_ends(rig: RelayRig) -> None:
    out = rig.attach()
    del rig.gw.slots[out["slot_key"]]
    res = rig.relays.end(out["relay_id"], {})
    assert res["relay"]["state"] == "ended" and res["chat"] == "gone"


def test_end_when_the_gateway_is_down_still_ends_and_says_so(rig: RelayRig) -> None:
    out = rig.attach()
    rig.gw_opener.down = True
    res = rig.relays.end(out["relay_id"], {})
    assert res["relay"]["state"] == "ended" and res["chat"] == "unreachable"


def test_end_of_a_relay_that_started_its_session_leaves_the_session(rig: RelayRig) -> None:
    out = rig.relays.create({"acknowledge_risk": True, "start": start_body("started-one")})
    writes = len(rig.cao_writes())
    rig.relays.end(out["relay_id"], {})
    assert len(rig.cao_writes()) == writes
    assert "cao-started-one" in rig.cao.sessions


def test_after_end_the_session_can_be_attached_again(rig: RelayRig) -> None:
    first = rig.attach()
    rig.relays.end(first["relay_id"], {})
    second = rig.attach()
    assert second["state"] == "active" and second["relay_id"] != first["relay_id"]


def test_unknown_relay_id_is_not_found_and_a_bad_one_invalid(rig: RelayRig) -> None:
    with pytest.raises(ShelfError) as err:
        rig.relays.end("rc-0000000000", {})
    assert err.value.code == "not_found"
    with pytest.raises(ShelfError) as err:
        rig.relays.end("../etc", {})
    assert err.value.code == "invalid_request"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"session": RELAY_SESSION, "start": {}},
        {"session": "bad name"},
        {"session": RELAY_SESSION, "label": ""},
        {"session": RELAY_SESSION, "label": "x" * 81},
        {"session": RELAY_SESSION, "label": "a\x07b"},
        {"session": RELAY_SESSION, "extra": 1},
    ],
)
def test_bad_create_bodies_are_invalid_request(rig: RelayRig, body: Dict[str, Any]) -> None:
    with pytest.raises(ShelfError) as err:
        rig.relays.create(body)
    assert err.value.code == "invalid_request"
    assert rig.gw.calls == []


def test_records_are_0600_and_hold_no_relayed_text(rig: RelayRig) -> None:
    out = rig.attach()
    rig.gw.add_rows(
        out["slot_key"],
        {"role": "user", "content": f"owner says {SENTINEL}"},
        {"role": "assistant", "content": f"reply {SENTINEL}"},
    )
    rig.relays.check(force=True)
    path = rig.data / "relays.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert SENTINEL not in rig.data_text()
    assert "CAO Shelf relay setup" not in rig.data_text()
    record = json.loads(path.read_text())["relays"][0]
    assert set(record) == set(relays_mod.RECORD_FIELDS)


def test_relays_survive_a_restart_and_history_being_off(tmp_path: Path) -> None:
    rig = RelayRig(tmp_path, settings={"persist_history": False})
    rig.warm()
    out = rig.attach()
    again = rig.make_relays()
    assert [r["relay_id"] for r in again.list()["relays"]] == [out["relay_id"]]
    assert again.list()["relays"][0]["state"] == "active"
    assert not (tmp_path / "ledger.json").exists()


def test_a_damaged_relays_file_is_moved_aside_and_a_new_one_starts(tmp_path: Path) -> None:
    rig = RelayRig(tmp_path)
    (rig.data / "relays.json").write_text("{not json")
    fresh = rig.make_relays()
    assert fresh.list()["relays"] == []
    assert list(rig.data.glob("relays.json.*.aside"))


def test_attach_is_refused_when_the_ops_server_points_at_another_cao(tmp_path: Path) -> None:
    rig = RelayRig(tmp_path, ops_target="http://127.0.0.1:9990")
    rig.warm()
    with pytest.raises(ShelfError) as err:
        rig.attach()
    assert err.value.code == "relay_cao_mismatch"
    assert rig.gw.calls == []


# --------------------------------------------------------------------- 3.2 start and attach
def start_body(name: str, key: str = "k" * 20) -> Dict[str, Any]:
    return {
        "idempotency_key": key,
        "name": name,
        "agent_profile": "developer",
        "provider": "mock_cli",
        "working_directory": "/work",
    }


def test_start_and_attach_sends_the_handshake_as_the_first_message(rig: RelayRig) -> None:
    out = rig.relays.create({"acknowledge_risk": True, "start": start_body("fresh")})
    assert out["origin"] == "started_by_relay" and out["session"] == "fresh"
    posts = [c for c in rig.cao.calls if c[0] == "POST" and c[1] == "/sessions"]
    assert len(posts) == 1
    body = rig.cao.bodies[rig.cao.calls.index(posts[0])]
    assert json.loads(body or b"{}")["initial_message"] == relays_mod.HANDSHAKE
    setup = rig.gw.messages(out["slot_key"])[0]
    assert "handshake: already_given" in setup
    assert f"conductor_terminal_id: {out['conductor_id']}" in setup


def test_the_same_start_twice_gives_one_session_and_one_relay(rig: RelayRig) -> None:
    first = rig.relays.create({"acknowledge_risk": True, "start": start_body("twice")})
    second = rig.relays.create({"acknowledge_risk": True, "start": start_body("twice")})
    assert second["relay_id"] == first["relay_id"]
    assert sum(1 for c in rig.cao.calls if c[0] == "POST" and c[1] == "/sessions") == 1
    assert len([r for r in rig.relays.list()["relays"] if r["session"] == "twice"]) == 1


def test_a_retry_after_a_failed_attach_starts_no_second_session(rig: RelayRig) -> None:
    rig.gw.fail[("POST", "/api/chat/slots")] = 500
    with pytest.raises(ShelfError):
        rig.relays.create({"acknowledge_risk": True, "start": start_body("retry")})
    del rig.gw.fail[("POST", "/api/chat/slots")]
    out = rig.relays.create({"acknowledge_risk": True, "start": start_body("retry")})
    assert out["state"] == "active"
    assert sum(1 for c in rig.cao.calls if c[0] == "POST" and c[1] == "/sessions") == 1
    assert len(rig.relays.list()["relays"]) == 1


def test_start_is_refused_before_starting_when_the_ops_server_points_at_another_cao(
    tmp_path: Path,
) -> None:
    rig = RelayRig(tmp_path, ops_target="http://127.0.0.1:9990")
    rig.warm()
    with pytest.raises(ShelfError) as err:
        rig.relays.create({"acknowledge_risk": True, "start": start_body("elsewhere")})
    assert err.value.code == "relay_cao_mismatch"
    assert not [c for c in rig.cao.calls if c[0] == "POST" and c[1] == "/sessions"]
    assert rig.gw.calls == [] and rig.relays.list()["relays"] == []


def test_start_and_attach_refuses_a_first_message(rig: RelayRig) -> None:
    with pytest.raises(ShelfError) as err:
        rig.relays.create(
            {"acknowledge_risk": True, "start": dict(start_body("msg"), message="hello")}
        )
    assert err.value.code == "invalid_request"
    assert not any(c[1] == "/sessions" and c[0] == "POST" for c in rig.cao.calls)


def test_backend_handshake_and_templates_equal_the_contract() -> None:
    texts = contract_texts()
    assert relays_mod.HANDSHAKE == texts["handshake"]
    assert relays_mod.SETUP_TEMPLATE == texts["setup"]
    assert relays_mod.ENDED_TEMPLATE == texts["ended"]
    section = (Path(relays_mod.__file__).parents[2] / "api-contract.md").read_text()
    for code, why in relays_mod.END_REASONS.items():
        assert f"`{code} ({why})`" in section, code


def test_unused_names_keep_the_helper_in_the_rig(rig: RelayRig) -> None:
    # the helper exists so the rig's session has one: a conductor check never picks it
    assert RELAY_HELPER in {t["id"] for t in rig.cao.session_terminals(RELAY_SESSION)}
    assert rig.attach()["conductor_id"] == RELAY_CONDUCTOR


@pytest.mark.parametrize(
    "host,port,want",
    [
        ("127.0.0.1", "9889", "http://127.0.0.1:9889"),
        ("[::1]", "9990", "http://[::1]:9990"),
        ("localhost", "9990", "http://localhost:9990"),
        ("10.0.0.1", "9889", None),
    ],
)
def test_ops_target_is_read_from_the_installed_relay_agent(
    tmp_path: Path, host: str, port: str, want: Any
) -> None:
    path = tmp_path / "app.json"
    path.write_text(json.dumps({"agents": ["agents/cao-remote-control.json"]}))
    (tmp_path / "agents").mkdir()
    env = {"CAO_API_HOST": host, "CAO_API_PORT": port}
    agent = {"name": "cao-remote-control", "mcpServers": {"cao-shelf:ops": {"env": env}}}
    (tmp_path / "agents" / "cao-remote-control.json").write_text(json.dumps(agent))
    assert relays_mod.ops_target_from_manifest(path) == want
    assert relays_mod.ops_target_from_manifest(tmp_path / "missing.json") is None


# ------------------------------------------------------------------------------ 4.1 reconcile
def card(rig: RelayRig, name: str = RELAY_SESSION) -> Dict[str, Any]:
    shelf = rig.poller.peek()
    assert shelf is not None
    return next(s for s in shelf["sessions"] if s["name"] == name)  # type: ignore[no-any-return]


def delete_in_cao(rig: RelayRig, bare: str = RELAY_SESSION) -> None:
    status, _ = rig.cao.handle("DELETE", f"/sessions/cao-{bare}")
    assert status == 200


def test_session_deleted_elsewhere_is_orphaned_within_one_poll(rig: RelayRig) -> None:
    out = rig.attach()
    delete_in_cao(rig)
    rig.warm(1)
    listed = {r["relay_id"]: r for r in rig.relays.list()["relays"]}
    assert listed[out["relay_id"]]["state"] == "orphaned"
    assert listed[out["relay_id"]]["end_reason"] == "session_gone"
    rig.relays.flush()
    msg = rig.gw.messages(out["slot_key"])[-1]
    assert msg.startswith("CAO Shelf relay ended")
    assert "reason: session_gone (the CAO session no longer exists)" in msg


def test_conductor_change_is_needs_reattach_and_the_card_says_so(rig: RelayRig) -> None:
    out = rig.attach()
    terms = rig.cao.session_terminals(RELAY_SESSION)
    terms[0]["id"] = "acc0de99"  # a new conductor, as after a CAO restart
    rig.cao.terminals["acc0de99"] = dict(rig.cao.terminals[RELAY_CONDUCTOR], id="acc0de99")
    rig.warm(2)
    rec = rig.relays.list()["relays"][0]
    assert rec["state"] == "needs_reattach" and rec["end_reason"] == "conductor_changed"
    assert card(rig)["relay"]["state"] == "needs_reattach"
    rig.relays.flush()
    assert "reason: conductor_changed" in rig.gw.messages(out["slot_key"])[-1]
    # a new relay may be attached to the new conductor
    again = rig.attach()
    assert again["conductor_id"] == "acc0de99"
    assert card(rig)["relay"]["relay_id"] == again["relay_id"]


def test_direct_activity_at_the_terminal_does_not_end_the_relay(rig: RelayRig) -> None:
    out = rig.attach()
    rig.cao.set_status(RELAY_CONDUCTOR, "processing", "2026-11-15T12:00:01.000000Z")
    rig.warm(1)
    rig.cao.set_status(RELAY_CONDUCTOR, "idle", "2026-11-15T12:00:09.000000Z")
    rig.warm(2)
    rig.relays.check(force=True)
    assert rig.relays.list()["relays"][0]["state"] == "active"
    assert len(rig.gw.messages(out["slot_key"])) == 1


def test_chat_deleted_in_kiro_crew_marks_the_relay_ended(rig: RelayRig) -> None:
    out = rig.attach()
    del rig.gw.slots[out["slot_key"]]
    rig.warm(1)  # the poll marks the chat check due
    rig.relays.check()
    rec = rig.relays.list()["relays"][0]
    assert rec["state"] == "ended" and rec["end_reason"] == "chat_gone"
    assert card(rig)["relay"] is None


def test_with_no_page_open_the_watch_pass_reads_cao_itself(rig: RelayRig) -> None:
    rig.attach()
    delete_in_cao(rig)
    rig.relays.check(force=True)  # no poller tick: the page is closed
    assert rig.relays.list()["relays"][0]["state"] == "orphaned"


def test_the_watch_pass_reads_cao_at_most_once_a_minute(rig: RelayRig) -> None:
    rig.attach()
    rig.relays.check(force=True)
    mark = len(rig.cao.calls)
    for _ in range(11):
        rig.now += 5
        rig.relays.check()
    assert len(rig.cao.calls) == mark
    rig.now += 6
    rig.relays.check()
    assert len(rig.cao.calls) > mark


def test_a_cao_outage_judges_nothing(rig: RelayRig) -> None:
    rig.attach()
    rig.cao.mode = "down"
    rig.relays.check(force=True)
    rig.warm(1)
    assert rig.relays.list()["relays"][0]["state"] == "active"


def test_relays_of_another_cao_address_are_not_reconciled(rig: RelayRig) -> None:
    rig.attach()
    rig.settings["cao_url"] = "http://127.0.0.1:9990"  # the page now reads another CAO
    rig.warm(1)
    assert rig.relays.list()["relays"][0]["state"] == "active"


def test_start_up_pass_drops_half_made_relays_and_their_chats(tmp_path: Path) -> None:
    rig = RelayRig(tmp_path)
    rig.warm()
    good = rig.attach()
    rig.gw.slots["cao-shelf-rc-00000000aa"] = {"messages": [], "title": "x"}
    doc = json.loads((rig.data / "relays.json").read_text())
    half = dict(
        doc["relays"][0],
        relay_id="rc-00000000aa",
        state="attaching",
        slot_key="cao-shelf-rc-00000000aa",
        session="other",
    )
    doc["relays"].append(half)
    (rig.data / "relays.json").write_text(json.dumps(doc))
    again = rig.make_relays()
    again.startup()
    assert [r["relay_id"] for r in again.list()["relays"]] == [good["relay_id"]]
    assert "cao-shelf-rc-00000000aa" not in rig.gw.slots
    assert good["slot_key"] in rig.gw.slots


def test_after_a_restart_active_relays_are_still_listed_and_checked(tmp_path: Path) -> None:
    rig = RelayRig(tmp_path)
    rig.warm()
    out = rig.attach()
    again = rig.make_relays()
    again.startup()
    assert again.list()["relays"][0]["state"] == "active"
    assert again.list()["relays"][0]["relay_id"] == out["relay_id"]


# ------------------------------------------------------------------------- 4.2 card signals
def owner(text: str) -> Dict[str, Any]:
    return {"role": "user", "content": text}


def app_turn(text: str) -> Dict[str, Any]:
    return {"role": "user", "content": text, "meta": {"turnActor": "app"}}


def assistant(text: str) -> Dict[str, Any]:
    return {"role": "assistant", "content": text}


def tool(title: str) -> Dict[str, Any]:
    return {"role": "tool", "content": f"✅ {title}", "meta": {"tool_call_id": "t1"}}


def permission(title: str, request_id: str = "req-1", resolved: str = "") -> Dict[str, Any]:
    cls: Dict[str, Any] = {"request_id": request_id, "tool_title": title}
    if resolved:
        cls["resolved"] = resolved
    return {"role": "permission", "content": title, "cls": json.dumps(cls)}


OPS = "mcp__cao-shelf_ops__"


def test_an_owner_turn_answered_without_an_ops_call_is_counted(rig: RelayRig) -> None:
    out = rig.attach()
    rig.gw.add_rows(
        out["slot_key"],
        assistant("Remote control is ready"),
        owner("what is 2+2?"),
        assistant("4"),
        owner("run the tests"),
        tool(OPS + "get_terminal_status"),
        tool(OPS + "send_session_message"),
        assistant("--- BEGIN TEXT ..."),
        owner("next"),
    )
    rig.relays.check(force=True)
    assert rig.relays.list()["relays"][0]["not_relayed"] == 1
    rig.warm(1)
    assert card(rig)["relay"]["not_relayed"] == 1


def test_cancel_and_stop_waiting_replies_are_not_counted(rig: RelayRig) -> None:
    rows = [
        owner("run it"),
        tool(OPS + "get_terminal_status"),
        assistant("The session is busy, so nothing was sent."),
        owner("3"),
        assistant("Cancelled. Nothing was sent."),
        owner("stop waiting"),
        assistant("anything"),
        owner("2"),
        assistant(
            "Stopped waiting. The session keeps working; its reply is not shown here, and your "
            "next message asks the session what happened, where CAO supports it."
        ),
        owner("later"),
    ]
    assert relays_mod.analyse_rows(rows).window_total == 0


def test_app_turns_and_the_open_turn_are_never_counted(rig: RelayRig) -> None:
    rows = [
        app_turn("CAO Shelf relay setup ..."),
        assistant("Remote control is ready for session x."),
        owner("hello"),
        assistant("hi, I answered myself"),  # still open: no later user row
    ]
    assert relays_mod.analyse_rows(rows).window_total == 0
    assert relays_mod.analyse_rows(rows + [owner("more")]).window_total == 1


def test_the_count_does_not_drop_when_old_rows_leave_the_window(rig: RelayRig) -> None:
    out = rig.attach()
    rig.gw.add_rows(out["slot_key"], owner("a"), assistant("self"), owner("b"))
    rig.relays.check(force=True)
    assert rig.relays.list()["relays"][0]["not_relayed"] == 1
    rig.gw.slots[out["slot_key"]]["messages"] = [owner("b")]
    rig.relays.check(force=True)
    assert rig.relays.list()["relays"][0]["not_relayed"] == 1


def test_a_pending_ops_permission_shows_the_rule_notice_on_the_card(rig: RelayRig) -> None:
    out = rig.attach()
    rig.gw.add_rows(out["slot_key"], owner("run"), permission(OPS + "get_terminal_status"))
    rig.relays.check(force=True)
    assert rig.relays.list()["relays"][0]["permission_pending"] is True
    rig.warm(1)
    assert card(rig)["relay"]["permission_pending"] is True
    # once answered, the notice goes away
    row = rig.gw.slots[out["slot_key"]]["messages"][-1]
    row["cls"] = json.dumps(
        {"request_id": "req-1", "tool_title": OPS + "x", "resolved": "approved"}
    )
    rig.relays.check(force=True)
    assert rig.relays.list()["relays"][0]["permission_pending"] is False


def test_any_pending_request_raises_the_neutral_notice_and_an_answered_one_does_not() -> None:
    """The title is not read: a row titled like an ops tool can come from another tool."""
    for title in ("Bash", OPS + "launch_session", OPS + "get_session_info", "Agent"):
        assert relays_mod.analyse_rows([owner("x"), permission(title)]).permission_pending
    done = permission(OPS + "get_terminal_status", resolved="rejected")
    assert relays_mod.analyse_rows([owner("x"), done]).permission_pending is False
    assert not hasattr(relays_mod, "ops_tool_of")


# ------------------------------------------------- 9.4 no approver: the notice only, never an approval
def test_a_pending_ops_row_raises_the_notice_and_the_backend_never_approves(rig: RelayRig) -> None:
    """Kiro Crew 0.12.2 writes no trusted identity on a Claude-backend permission row, and the title
    can be model-authored (an Agent call titled like an ops tool), so the App approves nothing."""
    from cao_shelf import gateway
    from cao_shelf.config import DEFAULTS

    assert "approve_relay_tools" not in DEFAULTS
    assert not hasattr(gateway.GatewayClient, "approve")
    out = rig.attach()
    rig.gw.add_rows(
        out["slot_key"],
        owner("go"),
        permission(OPS + "get_terminal_status", "r-ops"),
        {
            "role": "permission",
            "content": OPS + "get_session_info",
            "cls": json.dumps(
                {
                    "request_id": "r-agent",
                    "tool_title": OPS + "get_session_info",
                    "tool_kind": "think",
                }
            ),
        },
    )
    rig.relays.check(force=True)
    assert rig.gw.approved == []
    assert rig.gw.count("POST", "/api/chat/slots/{slot}/approve") == 0
    assert rig.relays.list()["relays"][0]["permission_pending"] is True


# ------------------------------------------------------------------------------ 6.2 trust mode
def test_trust_mode_rule_attach_sets_normal_and_a_refusal_deletes_the_chat(
    rig: RelayRig,
) -> None:
    out = rig.attach()
    assert rig.gw.slots[out["slot_key"]]["mode"] == "normal"
    rig.relays.end(out["relay_id"], {"delete_chat": True})
    rig.gw.fail[("POST", "/api/chat/mode")] = 403
    with pytest.raises(ShelfError) as err:
        rig.attach()
    assert err.value.code == "gateway_refused"
    assert rig.gw.slots == {}
    assert [r["state"] for r in rig.relays.list()["relays"]] == ["ended"]


# ----------------------------------------------- 9.5 an owed relay-ended message is posted until it is
def ended_messages(rig: RelayRig, slot: str) -> List[str]:
    return [m for m in rig.gw.messages(slot) if m.startswith("CAO Shelf relay ended")]


def owed(rig: RelayRig, relay_id: str) -> bool:
    doc = json.loads((rig.data / "relays.json").read_text(encoding="utf-8"))
    return bool(next(r for r in doc["relays"] if r["relay_id"] == relay_id)["ended_notice_pending"])


@pytest.mark.parametrize("outage", ["refused", "down"])
def test_end_with_kiro_crew_not_answering_keeps_the_message_owed_then_posts_it_once(
    rig: RelayRig, outage: str
) -> None:
    out = rig.attach()
    if outage == "refused":
        rig.gw.fail[("POST", "/api/chat")] = 503
    else:
        rig.gw_opener.down = True
    body = rig.relays.end(out["relay_id"], {})
    assert body["chat"] == "unreachable" and body["relay"]["state"] == "ended"
    assert owed(rig, out["relay_id"]) and ended_messages(rig, out["slot_key"]) == []
    rig.relays.check()  # still not answering: still owed
    assert owed(rig, out["relay_id"])
    rig.gw.fail.clear()
    rig.gw_opener.down = False
    rig.relays.check()
    rig.relays.check()
    assert len(ended_messages(rig, out["slot_key"])) == 1
    assert "reason: ended_by_owner" in ended_messages(rig, out["slot_key"])[0]
    assert not owed(rig, out["relay_id"])


def test_a_reconcile_message_that_could_not_be_posted_is_posted_on_a_later_pass(
    rig: RelayRig,
) -> None:
    out = rig.attach()
    rig.gw_opener.down = True
    delete_in_cao(rig)
    rig.warm(1)
    rig.relays.check()
    assert rig.relays.list()["relays"][0]["state"] == "orphaned" and owed(rig, out["relay_id"])
    rig.gw_opener.down = False
    rig.relays.check()
    assert len(ended_messages(rig, out["slot_key"])) == 1


def test_an_owed_message_survives_a_backend_restart(rig: RelayRig) -> None:
    out = rig.attach()
    rig.gw_opener.down = True
    rig.relays.end(out["relay_id"], {})
    rig.gw_opener.down = False
    again = rig.make_relays()
    again.startup()
    assert len(ended_messages(rig, out["slot_key"])) == 1


def test_take_over_reports_an_old_chat_that_could_not_be_told(
    rig: RelayRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = rig.attach()
    real = rig.gateway.post_message
    blocked = {"on": True}

    def post(slot: str, text: str) -> None:
        if blocked["on"] and slot == old["slot_key"]:
            raise ShelfError("gateway_unavailable", "down")
        real(slot, text)

    monkeypatch.setattr(rig.gateway, "post_message", post)
    new = rig.relays.takeover(old["relay_id"], ACK)
    assert new["replaced_chat"] == "unreachable" and owed(rig, old["relay_id"])
    blocked["on"] = False
    rig.relays.check()
    assert len(ended_messages(rig, old["slot_key"])) == 1
    assert "reason: taken_over" in ended_messages(rig, old["slot_key"])[0]


# ------------------------------------------------- 9.6 state changes are compare-and-set (F7)
def test_end_racing_the_watch_threads_session_gone_keeps_one_ended_by_owner_message(
    rig: RelayRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = rig.attach()

    def gone_after_an_end(session: str) -> Optional[str]:
        rig.relays.end(out["relay_id"], {})  # the owner presses End while CAO is being read
        raise ShelfError("not_found", "no such session")

    monkeypatch.setattr(rig.actions, "conductor_of_session", gone_after_an_end)
    rig.relays.check(force=True)
    rec = rig.relays.list()["relays"][0]
    assert rec["state"] == "ended" and rec["end_reason"] == "ended_by_owner"
    assert len(ended_messages(rig, out["slot_key"])) == 1


def test_take_over_racing_a_conductor_change_tells_the_old_chat_once(
    rig: RelayRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = rig.attach()
    old_rec = rig.relays._get(old["relay_id"])
    real = rig.gateway.post_message

    def post(slot: str, text: str) -> None:
        if text.startswith("CAO Shelf relay setup") and old_rec["state"] == "active":
            # the watch thread sees the conductor change while the new chat is being set up
            rig.relays._finish(old_rec, "needs_reattach", "conductor_changed")
        real(slot, text)

    monkeypatch.setattr(rig.gateway, "post_message", post)
    rig.relays.takeover(old["relay_id"], ACK)
    rig.relays.check()
    messages = ended_messages(rig, old["slot_key"])
    assert len(messages) == 1 and "reason: conductor_changed" in messages[0]
    assert old_rec["state"] == "ended" and old_rec["end_reason"] == "conductor_changed"


def test_a_relay_ended_while_its_rows_were_read_keeps_its_state(
    rig: RelayRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = rig.attach()
    rig.gw.add_rows(out["slot_key"], owner("a"), assistant("self"), owner("b"))
    real = rig.gateway.slot_rows

    def rows(slot: str) -> Any:
        got = real(slot)
        rig.relays.end(out["relay_id"], {})
        return got

    monkeypatch.setattr(rig.gateway, "slot_rows", rows)
    rig.relays.check(force=True)
    rec = rig.relays.list()["relays"][0]
    assert rec["state"] == "ended" and rec["not_relayed"] == 0


# ---------------------------------------------------- 9.7 the "not relayed" count is a count (F4, F5)
def with_mid(row: Dict[str, Any], mid: str) -> Dict[str, Any]:
    return dict(row, meta={**(row.get("meta") or {}), "mid": mid})


def test_the_count_adds_a_new_turn_after_old_rows_left_the_window(rig: RelayRig) -> None:
    """A stored 3, then the window slides past those turns, then one new unforwarded turn: 4."""
    out = rig.attach()
    slot = out["slot_key"]
    old = []
    for i in range(3):
        old += [with_mid(owner(f"q{i}"), f"m-u{i}"), with_mid(assistant("self"), f"m-a{i}")]
    rig.gw.add_rows(slot, *old, with_mid(owner("next"), "m-u3"))
    rig.relays.check(force=True)
    assert rig.relays.list()["relays"][0]["not_relayed"] == 3
    # the window slides: the counted turns are gone; one new turn answered without a forward
    rig.gw.slots[slot]["messages"] = [
        with_mid(owner("next"), "m-u3"),
        with_mid(assistant("self again"), "m-a3"),
        with_mid(owner("later"), "m-u4"),
    ]
    rig.relays.check(force=True)
    assert rig.relays.list()["relays"][0]["not_relayed"] == 4
    rig.relays.check(force=True)  # judged once: no double count
    assert rig.relays.list()["relays"][0]["not_relayed"] == 4
    # survives a restart: the judged row id is in the record (an id, never text)
    again = rig.make_relays()
    again.check(force=True)
    assert again.list()["relays"][0]["not_relayed"] == 4
    assert "self again" not in rig.data_text() and "later" not in rig.data_text()


def test_a_window_that_slid_past_the_judged_row_judges_every_settled_turn_in_it() -> None:
    rows = [
        with_mid(owner("a"), "m-1"),
        with_mid(assistant("self"), "m-2"),
        with_mid(owner("b"), "m-3"),
        with_mid(assistant("self"), "m-4"),
        with_mid(owner("c"), "m-5"),
    ]
    seen = relays_mod.analyse_rows(rows, "m-0-gone")
    assert (seen.not_relayed, seen.judged_through, seen.placed) == (2, "m-3", True)
    assert relays_mod.analyse_rows(rows, "m-1").not_relayed == 1
    assert relays_mod.analyse_rows(rows, "m-3").not_relayed == 0


@pytest.mark.parametrize(
    "owner_text,reply",
    [
        ("1", contract_texts()["answer_box"]),
        (
            "send this to session other instead",
            contract_texts()["refuse_target"]
            .replace("<session>", RELAY_SESSION)
            .replace("<terminal_id>", RELAY_CONDUCTOR),
        ),
        ("2", contract_texts()["stopped_waiting"]),
        ("3", contract_texts()["cancelled"]),
        # Claude Code can put its own notice before the agent's text
        ("1", "**Warning:** a host notice. " + contract_texts()["answer_box"]),
    ],
)
def test_replies_the_prompt_requires_without_a_tool_call_are_not_counted(
    owner_text: str, reply: str
) -> None:
    rows = [owner(owner_text), assistant(reply), owner("later")]
    assert relays_mod.analyse_rows(rows).not_relayed == 0


def test_the_backend_choice_replies_are_the_contract_texts() -> None:
    texts = contract_texts()
    for key in ("cancelled", "stopped_waiting", "answer_box", "crew_notice"):
        assert texts[key] in relays_mod.CHOICE_REPLIES, key
    assert any(texts["refuse_target"].startswith(c) for c in relays_mod.CHOICE_REPLIES)


# ------------------------------------------------- 9.8 reconcile does not end a working relay (F11)
NAME = "cao-" + RELAY_SESSION
ROWS = [{"id": RELAY_CONDUCTOR}, {"id": RELAY_HELPER}]
CALLERS = {RELAY_CONDUCTOR: None, RELAY_HELPER: RELAY_CONDUCTOR}


def state_of(rig: RelayRig) -> str:
    return str(rig.relays.list()["relays"][0]["state"])


def test_a_relayed_session_listed_but_not_read_this_tick_stays_active(rig: RelayRig) -> None:
    """A parked or stale session is listed every tick but read only every 15 or 60 s."""
    rig.attach()
    rig.relays.reconcile_views([NAME], {}, {}, set())
    assert state_of(rig) == "active"


def test_a_terminal_whose_caller_is_not_read_yet_stays_active(rig: RelayRig) -> None:
    """The bound conductor has a live caller elsewhere (so it stays the title). While the helper's
    caller is unknown, reading it as caller-less would name the helper conductor: not judged yet."""
    rig.attach()
    elsewhere = "beef0002"
    partial = {RELAY_CONDUCTOR: elsewhere}
    rig.relays.reconcile_views([NAME], {NAME: ROWS}, partial, {elsewhere, RELAY_CONDUCTOR})
    assert state_of(rig) == "active"
    known = {RELAY_CONDUCTOR: elsewhere, RELAY_HELPER: RELAY_CONDUCTOR}
    rig.relays.reconcile_views([NAME], {NAME: ROWS}, known, {elsewhere, RELAY_CONDUCTOR})
    assert state_of(rig) == "active"


def test_a_conductor_that_gains_a_caller_is_needs_reattach(rig: RelayRig) -> None:
    out = rig.attach()
    newcomer = "beef0001"
    rows = [{"id": RELAY_CONDUCTOR}, {"id": newcomer}]
    callers = {RELAY_CONDUCTOR: newcomer, newcomer: None}  # the old conductor became a helper
    rig.relays.reconcile_views([NAME], {NAME: rows}, callers, {RELAY_CONDUCTOR, newcomer})
    assert state_of(rig) == "needs_reattach"
    rig.relays.flush()
    assert "reason: conductor_changed" in rig.gw.messages(out["slot_key"])[-1]


def test_the_watch_path_marks_a_session_without_a_conductor_needs_reattach(
    rig: RelayRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.attach()
    monkeypatch.setattr(rig.actions, "conductor_of_session", lambda session: None)
    rig.relays.check(force=True)
    assert state_of(rig) == "needs_reattach"


@pytest.mark.parametrize(
    "code", ["cao_error", "cao_incompatible", "cao_unreachable", "cao_timeout"]
)
def test_the_watch_path_judges_nothing_on_an_unclear_cao_answer(
    rig: RelayRig, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    rig.attach()

    def unclear(session: str) -> Optional[str]:
        raise ShelfError(code, "unclear")

    monkeypatch.setattr(rig.actions, "conductor_of_session", unclear)
    rig.relays.check(force=True)
    assert state_of(rig) == "active"


def test_kiro_crew_down_on_the_slot_check_ends_no_relay_and_stops_reading(rig: RelayRig) -> None:
    first = rig.attach()
    second = rig.attach("payments-fix")
    rig.gw.add_rows(first["slot_key"], owner("a"), assistant("self"), owner("b"))
    rig.gw_opener.down = True
    rig.relays.check(force=True)
    listed = rig.relays.list()["relays"]
    assert [r["state"] for r in listed] == ["active", "active"]
    assert all(r["not_relayed"] == 0 for r in listed)
    # the first failed read ends the pass: the other chat is not tried until the next one
    tried = [u for u in rig.gw_opener.urls if "/api/chat/slots/" in u]
    assert len({first["slot_key"] in u or second["slot_key"] in u for u in tried}) == 1
    assert sum(1 for u in tried if first["slot_key"] in u or second["slot_key"] in u) == 1


def test_an_id_less_turn_leaving_the_window_does_not_count_a_turn_twice(rig: RelayRig) -> None:
    """A (no id) and B (id) are both counted while A has no id; once A leaves the window and C is
    added, only C is new."""
    out = rig.attach()
    slot = out["slot_key"]
    rig.gw.add_rows(
        slot,
        owner("a"),
        assistant("self"),
        with_mid(owner("b"), "m-b"),
        with_mid(assistant("self"), "m-b2"),
        with_mid(owner("next"), "m-n"),
    )
    rig.relays.check(force=True)
    assert rig.relays.list()["relays"][0]["not_relayed"] == 2
    rig.gw.slots[slot]["messages"] = [
        with_mid(owner("b"), "m-b"),
        with_mid(assistant("self"), "m-b2"),
        with_mid(owner("next"), "m-n"),
        with_mid(assistant("self again"), "m-n2"),
        with_mid(owner("later"), "m-l"),
    ]
    rig.relays.check(force=True)
    assert rig.relays.list()["relays"][0]["not_relayed"] == 3


def test_the_card_says_a_chat_is_not_told_yet_until_the_message_is_posted(rig: RelayRig) -> None:
    out = rig.attach()
    rig.gw_opener.down = True
    body = rig.relays.end(out["relay_id"], {})
    assert body["relay"]["ended_notice_pending"] is True
    rig.warm(1)
    relay = card(rig)["relay"]
    assert relay["state"] == "ended" and relay["ended_notice_pending"] is True
    rig.gw_opener.down = False
    rig.relays.check()
    rig.warm(1)
    assert card(rig)["relay"] is None


def test_after_a_take_over_the_new_relay_carries_the_old_chats_owed_message(
    rig: RelayRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = rig.attach()
    real = rig.gateway.post_message

    def post(slot: str, text: str) -> None:
        if slot == old["slot_key"]:
            raise ShelfError("gateway_unavailable", "down")
        real(slot, text)

    monkeypatch.setattr(rig.gateway, "post_message", post)
    new = rig.relays.takeover(old["relay_id"], ACK)
    rig.warm(1)
    relay = card(rig)["relay"]
    assert relay["relay_id"] == new["relay_id"] and relay["state"] == "active"
    assert relay["ended_notice_pending"] is True


def test_an_unforwarded_turn_after_an_id_less_row_is_still_counted(rig: RelayRig) -> None:
    """Socrates N6: stored 3, judged through C. The window holds E (no id, forwarded), C (counted)
    and D (new, answered without a forward). D must be counted once: 4, also after E leaves."""
    out = rig.attach()
    slot = out["slot_key"]
    rig.gw.add_rows(
        slot,
        *[r for i in range(2) for r in (with_mid(owner(f"q{i}"), f"m-q{i}"), assistant("self"))],
        with_mid(owner("c"), "m-c"),
        assistant("self"),
        with_mid(owner("open"), "m-o"),
    )
    rig.relays.check(force=True)
    assert rig.relays.list()["relays"][0]["not_relayed"] == 3
    rec = rig.relays._get(out["relay_id"])
    assert rec["judged_through"] == "m-c"
    forwarded = [tool(OPS + "get_terminal_status"), tool(OPS + "send_session_message")]
    rig.gw.slots[slot]["messages"] = [
        owner("e"),  # id-less, forwarded
        *forwarded,
        assistant("--- BEGIN TEXT ..."),
        with_mid(owner("c"), "m-c"),
        assistant("self"),
        with_mid(owner("d"), "m-d"),
        assistant("self again"),
        with_mid(owner("open"), "m-o2"),
    ]
    rig.relays.check(force=True)
    assert rig.relays.list()["relays"][0]["not_relayed"] == 4
    rig.gw.slots[slot]["messages"] = rig.gw.slots[slot]["messages"][4:]  # E leaves the window
    rig.relays.check(force=True)
    assert rig.relays.list()["relays"][0]["not_relayed"] == 4
