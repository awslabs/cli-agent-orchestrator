"""The zero-prompt Kimi ACP bootstrap: mint an id, send nothing, prove exit.

The properties under test are the two the native TUI depends on and
cannot verify for itself: that the minting conversation never submitted a
turn into the session the worker is about to inherit, and that the
minting process was really gone before the TUI could attach to the same
single-writer session.  Everything else here exists to prove those two
cannot be asserted without being true.
"""

from __future__ import annotations

import hashlib
import os
import stat
from typing import Any, Mapping

import pytest

from cli_agent_orchestrator.services import kimi_native_bootstrap as boot
from cli_agent_orchestrator.services import native_attachment

PINNED_VERSION_BANNER = "kimi 0.29.0"
SESSION_ID = "session_9f2c41ab"
MODEL = "kimi-k2-turbo"
EFFORT = "high"


def _options(model: str = MODEL, effort: str = EFFORT) -> list[dict[str, Any]]:
    return [
        {"id": "model", "category": "model", "currentValue": model},
        {"id": "thinking", "category": "thought_level", "currentValue": effort},
    ]


@pytest.fixture
def pinned_binary(tmp_path):
    """A real executable file plus its digest, as the pin would name it."""
    path = tmp_path / "kimi"
    path.write_bytes(b"#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return {
        "kimi_binary": os.path.realpath(str(path)),
        "binary_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "version_output": PINNED_VERSION_BANNER,
    }


def _exit_proof(**overrides: Any) -> dict[str, Any]:
    proof = {
        "pid": 4242,
        "exit_status": 0,
        "escalation": [boot.STEP_STDIN_CLOSED],
        "reaped": True,
    }
    proof.update(overrides)
    return proof


class FakeAcp:
    """Records every method that crossed the provider boundary.

    Config sets are honoured by default; ``deaf_to_config`` models the
    provider that answers a set successfully and changes nothing.
    """

    def __init__(
        self,
        *,
        session_result: Any = None,
        exit_proof: Any = None,
        request_error: Exception | None = None,
        terminate_error: Exception | None = None,
        deaf_to_config: bool = False,
    ) -> None:
        self.calls: list[tuple[str, Mapping[str, Any]]] = []
        self.terminated = 0
        self._options = _options(model="kimi-default", effort="low")
        self._session_result = (
            {"sessionId": SESSION_ID, "configOptions": self._options}
            if session_result is None
            else session_result
        )
        self._exit_proof = _exit_proof() if exit_proof is None else exit_proof
        self._request_error = request_error
        self._terminate_error = terminate_error
        self._deaf = deaf_to_config

    def request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append((method, dict(params)))
        if self._request_error is not None:
            raise self._request_error
        if method == "initialize":
            return {"protocolVersion": 1}
        if method == "session/new":
            return self._session_result
        if method == "session/set_config_option":
            if not self._deaf:
                for option in self._options:
                    if option["id"] == params["configId"]:
                        option["currentValue"] = params["value"]
            return {"configOptions": self._options}
        raise AssertionError(f"unexpected bootstrap method {method!r}")

    def terminate(self) -> Any:
        self.terminated += 1
        if self._terminate_error is not None:
            raise self._terminate_error
        return self._exit_proof


def _mint(pinned, transport, **overrides):
    kwargs = {
        **pinned,
        "working_directory": "/tmp/work",
        "model": MODEL,
        "effort": EFFORT,
        "transport": transport,
        **overrides,
    }
    return boot.mint_session(**kwargs)


# --------------------------------------------------------------------------
# The golden path
# --------------------------------------------------------------------------


def test_minting_returns_the_session_id_with_its_exit_proof(pinned_binary):
    transport = FakeAcp()

    receipt = _mint(pinned_binary, transport)

    assert receipt["schema"] == boot.BOOTSTRAP_SCHEMA
    assert receipt["native_session_id"] == SESSION_ID
    assert receipt["provider"] == "kimi"
    assert receipt["id_source"] == "acp_session_new"
    assert receipt["provider_version"] == "0.29.0"
    assert receipt["binary_sha256"] == pinned_binary["binary_sha256"]
    assert receipt["exit_proof"]["reaped"] is True
    assert receipt["exit_proof"]["schema"] == boot.EXIT_PROOF_SCHEMA
    assert transport.terminated == 1


def test_the_bootstrap_never_submits_a_turn(pinned_binary):
    transport = FakeAcp()

    _mint(pinned_binary, transport)

    methods = [method for method, _ in transport.calls]
    assert methods == [
        "initialize",
        "session/new",
        "session/set_config_option",
        "session/set_config_option",
    ]
    assert not any("prompt" in method for method in methods)


def test_the_bootstrap_client_claims_no_filesystem_or_terminal_capability(pinned_binary):
    transport = FakeAcp()

    _mint(pinned_binary, transport)

    _, params = transport.calls[0]
    assert params["clientCapabilities"] == {
        "fs": {"readTextFile": False, "writeTextFile": False},
        "terminal": False,
    }


def test_the_working_directory_and_servers_reach_session_new(pinned_binary):
    transport = FakeAcp()

    _mint(
        pinned_binary,
        transport,
        working_directory="/srv/project",
        mcp_servers=[{"name": "cao"}],
    )

    method, params = transport.calls[1]
    assert method == "session/new"
    assert params == {"cwd": "/srv/project", "mcpServers": [{"name": "cao"}]}


def test_two_mints_of_different_conversations_digest_differently(pinned_binary):
    first = _mint(pinned_binary, FakeAcp())
    second = _mint(
        pinned_binary,
        FakeAcp(session_result={"sessionId": "session_other", "configOptions": _options()}),
    )

    assert first["acp_exchange_sha256"] != second["acp_exchange_sha256"]


# --------------------------------------------------------------------------
# The route, which the resume command line cannot carry
# --------------------------------------------------------------------------


def test_the_route_is_written_into_the_session_record(pinned_binary):
    transport = FakeAcp()

    receipt = _mint(pinned_binary, transport)

    sets = {
        params["configId"]: params["value"]
        for method, params in transport.calls
        if method == "session/set_config_option"
    }
    assert sets == {"model": MODEL, "thinking": EFFORT}
    assert receipt["model"] == MODEL
    assert receipt["effort"] == EFFORT


def test_a_session_already_on_the_route_costs_no_extra_provider_calls(pinned_binary):
    transport = FakeAcp(session_result={"sessionId": SESSION_ID, "configOptions": _options()})

    receipt = _mint(pinned_binary, transport)

    assert [method for method, _ in transport.calls] == ["initialize", "session/new"]
    assert receipt["model"] == MODEL


def test_a_provider_that_accepts_the_set_but_ignores_it_is_caught(pinned_binary):
    # The dangerous case: every call succeeds and the session quietly
    # keeps the wrong model.  Only the read-back distinguishes it.
    transport = FakeAcp(deaf_to_config=True)

    with pytest.raises(boot.KimiBootstrapProtocol, match="rather than the requested"):
        _mint(pinned_binary, transport)


def test_a_wrong_route_still_terminates_the_minting_process(pinned_binary):
    transport = FakeAcp(deaf_to_config=True)

    with pytest.raises(boot.KimiBootstrapProtocol):
        _mint(pinned_binary, transport)

    assert transport.terminated == 1


def test_the_receipt_carries_no_provider_output_verbatim(pinned_binary):
    # Provider results can carry credentials; the receipt keeps a digest
    # rather than the bytes, so a durable record cannot leak them.
    transport = FakeAcp(
        session_result={"sessionId": SESSION_ID, "authToken": "REDACTED-LOOKING-SECRET"}
    )

    receipt = _mint(pinned_binary, transport)

    assert "REDACTED-LOOKING-SECRET" not in repr(receipt)


# --------------------------------------------------------------------------
# The pinned binary
# --------------------------------------------------------------------------


def test_a_non_canonical_binary_path_is_refused(pinned_binary, tmp_path):
    link = tmp_path / "link-to-kimi"
    link.symlink_to(pinned_binary["kimi_binary"])
    transport = FakeAcp()

    with pytest.raises(boot.KimiBootstrapInvalid, match="canonical absolute path"):
        _mint(pinned_binary, transport, kimi_binary=str(link))

    assert transport.calls == []


def test_a_digest_that_does_not_match_the_pin_is_refused(pinned_binary):
    transport = FakeAcp()

    with pytest.raises(boot.KimiBootstrapInvalid, match="refusing to mint"):
        _mint(pinned_binary, transport, binary_sha256="0" * 64)

    assert transport.calls == []


def test_a_malformed_digest_is_refused_before_the_file_is_read(pinned_binary):
    with pytest.raises(boot.KimiBootstrapInvalid, match="64-character hex digest"):
        _mint(pinned_binary, FakeAcp(), binary_sha256="not-a-digest")


def test_version_drift_refuses_before_any_provider_io(pinned_binary):
    transport = FakeAcp()

    with pytest.raises(boot.KimiBootstrapInvalid, match="version drift"):
        _mint(pinned_binary, transport, version_output="kimi 0.28.0")

    assert transport.calls == []


def test_a_non_executable_binary_is_refused(pinned_binary, tmp_path):
    plain = tmp_path / "not-executable"
    plain.write_bytes(b"data")
    with pytest.raises(boot.KimiBootstrapInvalid, match="not an executable file"):
        _mint(
            pinned_binary,
            FakeAcp(),
            kimi_binary=os.path.realpath(str(plain)),
            binary_sha256=hashlib.sha256(b"data").hexdigest(),
        )


# --------------------------------------------------------------------------
# The minted id must be safely resumable
# --------------------------------------------------------------------------


def test_a_session_new_without_an_id_is_a_protocol_failure(pinned_binary):
    transport = FakeAcp(session_result={})

    with pytest.raises(boot.KimiBootstrapProtocol, match="omitted the provider session id"):
        _mint(pinned_binary, transport)


@pytest.mark.parametrize("bad_id", ["", "--session", "-S", "has space", "id;rm -rf /"])
def test_an_id_that_could_not_be_resumed_is_refused_at_mint_time(pinned_binary, bad_id):
    # The resume option's argument is optional in the installed CLI, so an
    # id that degrades on a command line opens a picker instead of failing.
    # Catching it here costs one session; catching it at launch costs an
    # attachment record naming a session nobody is running.
    transport = FakeAcp(session_result={"sessionId": bad_id})

    with pytest.raises(boot.KimiBootstrapProtocol):
        _mint(pinned_binary, transport)


def test_a_failed_exchange_still_terminates_the_minting_process(pinned_binary):
    transport = FakeAcp(session_result={})

    with pytest.raises(boot.KimiBootstrapProtocol):
        _mint(pinned_binary, transport)

    assert transport.terminated == 1


# --------------------------------------------------------------------------
# Proving the exit
# --------------------------------------------------------------------------


def test_an_unreaped_process_refuses_the_session_id(pinned_binary):
    transport = FakeAcp(exit_proof=_exit_proof(reaped=False, escalation=list(boot.EXIT_STEPS)))

    with pytest.raises(boot.KimiBootstrapNotDetached, match="was not reaped"):
        _mint(pinned_binary, transport)


def test_exit_evidence_that_is_not_a_mapping_refuses(pinned_binary):
    transport = FakeAcp(exit_proof="gone, trust me")

    with pytest.raises(boot.KimiBootstrapNotDetached, match="not exit evidence"):
        _mint(pinned_binary, transport)


def test_a_reaped_claim_without_an_exit_status_refuses(pinned_binary):
    # A wait() that returned always yields a status.  Its absence means
    # the wait never returned, whatever the flag says.
    transport = FakeAcp(exit_proof=_exit_proof(exit_status=None))

    with pytest.raises(boot.KimiBootstrapNotDetached, match="no reaped exit status"):
        _mint(pinned_binary, transport)


def test_a_reaped_claim_without_a_pid_refuses(pinned_binary):
    transport = FakeAcp(exit_proof=_exit_proof(pid=0))

    with pytest.raises(boot.KimiBootstrapNotDetached, match="no owning pid"):
        _mint(pinned_binary, transport)


def test_signalling_without_closing_stdin_first_refuses(pinned_binary):
    # Killing a provider before it has flushed its session store can mint
    # an id naming a record that was never finished being written.
    transport = FakeAcp(exit_proof=_exit_proof(escalation=[boot.STEP_SIGKILL]))

    with pytest.raises(boot.KimiBootstrapNotDetached, match="close stdin first"):
        _mint(pinned_binary, transport)


def test_an_out_of_order_escalation_refuses(pinned_binary):
    transport = FakeAcp(
        exit_proof=_exit_proof(
            escalation=[boot.STEP_STDIN_CLOSED, boot.STEP_SIGKILL, boot.STEP_SIGTERM]
        )
    )

    with pytest.raises(boot.KimiBootstrapNotDetached, match="prefix-respecting"):
        _mint(pinned_binary, transport)


def test_an_unknown_escalation_step_refuses(pinned_binary):
    transport = FakeAcp(exit_proof=_exit_proof(escalation=[boot.STEP_STDIN_CLOSED, "asked_nicely"]))

    with pytest.raises(boot.KimiBootstrapNotDetached, match="prefix-respecting"):
        _mint(pinned_binary, transport)


def test_the_full_escalation_ladder_is_accepted(pinned_binary):
    transport = FakeAcp(exit_proof=_exit_proof(escalation=list(boot.EXIT_STEPS), exit_status=-9))

    receipt = _mint(pinned_binary, transport)

    assert receipt["exit_proof"]["escalation"] == list(boot.EXIT_STEPS)
    assert receipt["exit_proof"]["exit_status"] == -9


def test_a_termination_that_itself_raises_refuses(pinned_binary):
    transport = FakeAcp(terminate_error=OSError("no such process"))

    with pytest.raises(boot.KimiBootstrapNotDetached, match="cannot be assumed gone"):
        _mint(pinned_binary, transport)


def test_an_unproven_exit_outranks_the_exchange_error_that_caused_it(pinned_binary):
    # Both went wrong.  The live process is the worse condition, so it is
    # the error that surfaces — with the exchange failure kept as context
    # rather than discarded.
    transport = FakeAcp(
        request_error=RuntimeError("acp handshake collapsed"),
        exit_proof=_exit_proof(reaped=False),
    )

    with pytest.raises(boot.KimiBootstrapNotDetached) as caught:
        _mint(pinned_binary, transport)

    assert isinstance(caught.value.__context__, RuntimeError)
    assert "acp handshake collapsed" in str(caught.value.__context__)


# --------------------------------------------------------------------------
# The intent a bootstrapped launch is allowed to declare
# --------------------------------------------------------------------------


def test_the_receipt_licenses_a_zero_prompt_attachment_intent(pinned_binary):
    receipt = _mint(pinned_binary, FakeAcp())

    intent = boot.bootstrap_intent(receipt)

    assert intent["acquisition_method"] == native_attachment.ACQUISITION_ACP_BOOTSTRAP
    assert intent["bootstrap_sent_no_turn"] is True
    assert intent["bootstrap_detached_before_launch"] is True
    assert intent["replays_task_bytes"] is False
    assert intent["acquisition_receipt"]["native_session_id"] == SESSION_ID


def test_the_intent_carries_an_optional_note(pinned_binary):
    receipt = _mint(pinned_binary, FakeAcp())

    intent = boot.bootstrap_intent(receipt, note="v2 native launch")

    assert intent["note"] == "v2 native launch"


def test_a_foreign_receipt_cannot_license_an_attachment():
    with pytest.raises(boot.KimiBootstrapInvalid, match="receipt"):
        boot.bootstrap_intent({"schema": "something-else", "sent_no_turn": True})


def test_a_receipt_whose_exit_proof_was_stripped_cannot_license_an_attachment(pinned_binary):
    # Receipts cross durable boundaries; the assertions are re-derived
    # from the evidence rather than trusted as flags.
    receipt = dict(_mint(pinned_binary, FakeAcp()))
    receipt["exit_proof"] = {"reaped": True}

    with pytest.raises(boot.KimiBootstrapNotDetached):
        boot.bootstrap_intent(receipt)


def test_a_receipt_that_denies_its_own_assertions_cannot_license_an_attachment(pinned_binary):
    receipt = dict(_mint(pinned_binary, FakeAcp()))
    receipt["detached_before_launch"] = False

    with pytest.raises(boot.KimiBootstrapInvalid, match="turn-free, detached"):
        boot.bootstrap_intent(receipt)
