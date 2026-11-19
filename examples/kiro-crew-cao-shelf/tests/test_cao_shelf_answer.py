"""A9, A10, A11: ask, prompt and answer, start. Answers go to ``/input`` and never to the inbox."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

import cao_shelf_support as sup
import pytest
from cao_shelf import prompts
from cao_shelf.errors import ShelfError
from cao_shelf_support import ActionsRig, validate

WAITING = "b94da5c6"  # the tester in payments-fix, waiting_user_answer on the busy day


@pytest.fixture
def rig(tmp_path: Path) -> ActionsRig:
    r = ActionsRig(tmp_path, "busy").warm()
    real = r.cao._route

    def guarded(method: str, path: str, query: Dict[str, str], body: Any) -> Any:
        if (
            method == "POST"
            and path.endswith("/inbox/messages")
            and query.get("sender_id") != "cao-shelf"
        ):
            raise AssertionError("an answer reached the inbox route")
        return real(method, path, query, body)

    r.cao._route = guarded  # type: ignore[method-assign]
    return r


def err(fn: Any, *a: Any) -> ShelfError:
    with pytest.raises(ShelfError) as exc:
        fn(*a)
    return exc.value


ANSWER = {"answer": "y", "expect_status": "waiting_user_answer"}


# ------------------------------------------------------------------------------------ prompt
def test_prompt_of_a_waiting_terminal_has_tail_and_suggestions(rig: ActionsRig) -> None:
    got = rig.actions.prompt(WAITING)
    validate("actions.schema.json", got, "PromptResponse")
    assert got["status"] == "waiting_user_answer" and got["provider"] == "mock_cli"
    assert got["tail"] == ["line one", "APPROVAL_REQUIRED: push to origin?"]
    assert got["suggested"] == [{"label": "Approve", "send": "y"}, {"label": "Deny", "send": "n"}]
    assert rig.cao.count("GET", f"/terminals/{WAITING}/output") == 1
    assert rig.cao.writes() == []


def test_prompt_of_a_terminal_that_is_not_waiting_reads_no_output(rig: ActionsRig) -> None:
    got = rig.actions.prompt("1b964846")
    assert got["status"] == "processing" and got["tail"] == [] and got["suggested"] == []
    assert rig.cao.count("GET", "/terminals/1b964846/output") == 0


def test_prompt_of_an_error_terminal_has_a_tail_but_no_suggestions(rig: ActionsRig) -> None:
    rig.cao.set_status(WAITING, "error")
    rig.cao.outputs[WAITING] = "boom\nERROR: mock failure injected\n"
    got = rig.actions.prompt(WAITING)
    assert (
        got["status"] == "error" and got["tail"][-1].startswith("ERROR") and got["suggested"] == []
    )


def test_prompt_tail_is_cleaned_cut_and_capped(rig: ActionsRig) -> None:
    lines = [f"row {i}" for i in range(50)]
    text = (
        "\n".join(lines) + "\n" + "z" * 1000 + "\x1b[31mred\x1b[0m\x1b]0;title\x07 \x00ctl\r\n\n\n"
    )
    rig.cao.outputs[WAITING] = text
    got = rig.actions.prompt(WAITING)
    assert len(got["tail"]) == 20 and got["tail"][0] == "row 31"
    assert all(len(line) <= 400 for line in got["tail"])
    assert got["tail"][-1] == "z" * 400
    joined = "".join(got["tail"])
    assert (
        "\x1b" not in joined
        and "\x00" not in joined
        and "\r" not in joined
        and "\x07" not in joined
    )
    assert got["suggested"] == []  # the last line is not a recognised prompt


def test_prompt_hostile_text_stays_text(rig: ActionsRig) -> None:
    rig.cao.outputs[WAITING] = "<img src=x onerror=alert(1)>\njavascript:alert(1)\n"
    got = rig.actions.prompt(WAITING)
    assert got["tail"] == ["<img src=x onerror=alert(1)>", "javascript:alert(1)"]
    assert got["suggested"] == []


def test_prompt_tail_is_kept_ten_seconds_only(rig: ActionsRig) -> None:
    rig.actions.prompt(WAITING)
    rig.now += 9
    rig.actions.prompt(WAITING)
    assert rig.cao.count("GET", f"/terminals/{WAITING}/output") == 1
    rig.now += 2
    rig.actions.prompt(WAITING)
    assert rig.cao.count("GET", f"/terminals/{WAITING}/output") == 2


def test_prompt_errors(rig: ActionsRig) -> None:
    assert err(rig.actions.prompt, "ffffffff").code == "not_found"
    assert err(rig.actions.prompt, "../../x").code == "invalid_request"


# ---------------------------------------------------------------------------------- the shapes
@pytest.mark.parametrize(
    "tail,expected",
    [
        (["APPROVAL_REQUIRED: push to origin?"], True),
        (["x", "APPROVAL_REQUIRED: x", ""], True),
        (["APPROVAL_REQUIRED:"], False),
        (["APPROVAL_REQUIRED: "], False),
        (["approval_required: push"], False),
        (["Do you want to proceed? (y/n)"], False),
        (["1. Yes", "2. No"], False),
        (["APPROVAL_REQUIRED: a", "something else after"], False),
        ([], False),
    ],
)
def test_only_recorded_prompt_shapes_get_buttons(tail: List[str], expected: bool) -> None:
    got = prompts.suggest(tail, "mock_cli")
    assert bool(got) is expected
    if expected:
        assert [s["send"] for s in got] == ["y", "n"]
        assert all(len(s["send"]) <= 20 for s in got) and len(got) <= 4


# ------------------------------------------------------------------------------------ answer
def test_answer_goes_to_input_and_never_to_the_inbox(rig: ActionsRig) -> None:
    got = rig.actions.answer(WAITING, dict(ANSWER))
    assert got == {"delivered": True, "terminal_id": WAITING}
    validate("actions.schema.json", got, "AnswerResponse")
    posts = [c for c in rig.cao.calls if c[0] == "POST"]
    assert posts == [("POST", f"/terminals/{WAITING}/input", {"message": "y"})]
    assert not [c for c in rig.cao.calls if "inbox" in c[1] and c[0] == "POST"]
    assert rig.cao.terminals[WAITING]["status"] == "completed"  # it left waiting


def test_answer_rechecks_the_status_first(rig: ActionsRig) -> None:
    rig.cao.set_status(WAITING, "idle")
    e = err(rig.actions.answer, WAITING, dict(ANSWER))
    assert (e.code, e.http_status, e.detail) == ("not_waiting", 409, {"status": "idle"})
    assert rig.cao.writes() == []
    for status in ("processing", "completed", "error"):
        rig.cao.set_status(WAITING, status)
        assert err(rig.actions.answer, WAITING, dict(ANSWER)).code == "not_waiting"
    assert rig.cao.writes() == []


def test_answer_text_survives_url_encoding_exactly(rig: ActionsRig) -> None:
    text = "y & n=1 #frag ?q=1 é 日本\nsecond line"
    rig.actions.answer(WAITING, {"answer": text, "expect_status": "waiting_user_answer"})
    assert rig.cao.calls[-1][2] == {"message": text}


def test_answer_carries_no_sender_or_orchestration_type(rig: ActionsRig) -> None:
    rig.actions.answer(WAITING, dict(ANSWER))
    assert set(rig.cao.calls[-1][2]) == {"message"}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"answer": "y"},
        {"expect_status": "waiting_user_answer"},
        {"answer": "", "expect_status": "waiting_user_answer"},
        {"answer": "y", "expect_status": "idle"},
        {"answer": "y", "expect_status": "waiting_user_answer", "extra": 1},
        {"answer": 1, "expect_status": "waiting_user_answer"},
        {"answer": "x" * 2001, "expect_status": "waiting_user_answer"},
        {"answer": "y\x00", "expect_status": "waiting_user_answer"},
        {"answer": "日" * 2000, "expect_status": "waiting_user_answer"},  # too long once encoded
    ],
)
def test_answer_validation_sends_nothing(rig: ActionsRig, body: Dict[str, Any]) -> None:
    assert err(rig.actions.answer, WAITING, body).code == "invalid_request"
    assert rig.cao.writes() == []


def test_answer_for_bad_or_missing_terminals(rig: ActionsRig) -> None:
    assert err(rig.actions.answer, "../x", dict(ANSWER)).code == "invalid_request"
    assert err(rig.actions.answer, "ffffffff", dict(ANSWER)).code == "not_found"
    assert rig.cao.writes() == []


def test_answer_when_cao_blocks_the_input_is_cao_error_without_text(rig: ActionsRig) -> None:
    real = rig.cao._route

    def blocked(method: str, path: str, query: Dict[str, str], body: Any) -> Any:
        if method == "POST" and path.endswith("/input"):
            return 409, {"detail": sup.SENTINEL}
        return real(method, path, query, body)

    rig.cao._route = blocked  # type: ignore[method-assign]
    e = err(rig.actions.answer, WAITING, dict(ANSWER))
    assert e.code == "cao_error" and sup.SENTINEL not in json.dumps(e.envelope())


def test_answer_logs_nothing_sensitive(rig: ActionsRig, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    rig.cao.outputs[WAITING] = f"{sup.SENTINEL}\nAPPROVAL_REQUIRED: {sup.SENTINEL}\n"
    rig.actions.prompt(WAITING)
    rig.actions.answer(WAITING, {"answer": sup.SENTINEL, "expect_status": "waiting_user_answer"})
    assert sup.SENTINEL not in "\n".join(r.getMessage() for r in caplog.records)


# -------------------------------------------------------------------------------------- ask
def test_ask_posts_to_the_conductor_inbox_with_the_shelf_as_sender(rig: ActionsRig) -> None:
    got = rig.actions.ask(
        "payments-fix", {"message": "how is it going?", "conductor_id": "b2ce9908"}
    )
    validate("actions.schema.json", got, "AskResponse")
    assert got["accepted"] is True and got["terminal_id"] == "b2ce9908"
    posts = [c for c in rig.cao.calls if c[0] == "POST"]
    assert posts == [
        (
            "POST",
            "/terminals/b2ce9908/inbox/messages",
            {"sender_id": "cao-shelf", "message": "how is it going?"},
        )
    ]
    assert got["delivery"] in ("delivered", "pending")


def test_ask_rereads_the_session_before_writing(rig: ActionsRig) -> None:
    mark = len(rig.cao.calls)
    rig.actions.ask("payments-fix", {"message": "hi", "conductor_id": "b2ce9908"})
    kinds = [(m, p) for m, p, _ in rig.cao.calls[mark:]]
    assert kinds[0] == ("GET", "/sessions/cao-payments-fix") and kinds[1][0] == "POST"


def test_ask_conflict_sends_nothing(rig: ActionsRig) -> None:
    e = err(rig.actions.ask, "payments-fix", {"message": "hi", "conductor_id": "1b964846"})
    assert e.code == "conflict_changed" and rig.cao.writes() == []
    rig.cao.session_terminals("payments-fix").pop(0)  # the conductor went away
    e2 = err(rig.actions.ask, "payments-fix", {"message": "hi", "conductor_id": "b2ce9908"})
    assert e2.code == "conflict_changed" and rig.cao.writes() == []


def test_ask_unicode_and_encoding_limit(rig: ActionsRig) -> None:
    rig.actions.ask("payments-fix", {"message": "héllo 日本 & more", "conductor_id": "b2ce9908"})
    assert [c for c in rig.cao.calls if c[0] == "POST"][-1][2]["message"] == "héllo 日本 & more"
    long = {"message": "日" * 5000, "conductor_id": "b2ce9908"}
    n = len(rig.cao.writes())
    assert err(rig.actions.ask, "payments-fix", long).code == "invalid_request"
    assert len(rig.cao.writes()) == n


# ------------------------------------------------------------------------------------ start
def test_start_sends_the_profile_name_directory_key_and_message_body(rig: ActionsRig) -> None:
    got = rig.actions.start(
        {
            "idempotency_key": f"{1:032x}",
            "name": "fresh",
            "agent_profile": "developer",
            "provider": "mock_cli",
            "working_directory": "/work/example",
            "message": "hello there",
        }
    )
    validate("actions.schema.json", got, "StartResponse")
    assert got["session"] == "fresh"
    method, path, query = rig.cao.calls[-1]
    assert (method, path) == ("POST", "/sessions")
    assert query == {
        "agent_profile": "developer",
        "provider": "mock_cli",
        "session_name": "cao-fresh",
        "working_directory": "/work/example",
        "idempotency_key": f"{1:032x}",
    }
    assert json.loads(rig.cao.bodies[-1] or b"{}") == {"initial_message": "hello there"}
    assert 30.0 in rig.opener.timeouts  # start may take as long as the provider needs


def test_start_without_name_provider_or_message(rig: ActionsRig) -> None:
    got = rig.actions.start(
        {
            "idempotency_key": f"{2:032x}",
            "agent_profile": "developer",
            "working_directory": "/w",
            "provider": None,
            "message": None,
        }
    )
    assert got["session"] == "new-session" and len(got["conductor_id"]) == 8
    _, _, query = rig.cao.calls[-1]
    assert "provider" not in query and rig.cao.bodies[-1] in (None, b"")


def test_two_launches_with_two_dialog_keys_send_two_keys(rig: ActionsRig) -> None:
    """Replaces ``test_each_start_uses_a_fresh_idempotency_key``, which locked in the backend
    inventing a new key per call (so a retry double-launched). The key now comes from the dialog;
    the retry behaviour is in ``test_cao_shelf_review_fixes.py``."""
    rig.actions.start(
        {
            "idempotency_key": "a" * 20,
            "name": "one",
            "agent_profile": "d",
            "working_directory": "/w",
        }
    )
    rig.actions.start(
        {
            "idempotency_key": "b" * 20,
            "name": "two",
            "agent_profile": "d",
            "working_directory": "/w",
        }
    )
    keys = [q["idempotency_key"] for m, p, q in rig.cao.calls if p == "/sessions" and m == "POST"]
    assert keys == ["a" * 20, "b" * 20]


def test_profiles_are_cached_for_a_minute(rig: ActionsRig) -> None:
    rig.actions.profiles()
    rig.now += 59
    rig.actions.profiles()
    assert rig.cao.count("GET", "/agents/profiles") == 1
    rig.now += 2
    assert rig.actions.profiles()["profiles"][0]["name"] == "developer"
    assert rig.cao.count("GET", "/agents/profiles") == 2
