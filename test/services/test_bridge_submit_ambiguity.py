"""Production-wiring regressions: submit-ambiguous after provider acceptance.

The bridge's real ``_serve`` UDS loop must durably record
``submit-ambiguous`` evidence before returning an error whenever a
``session.admit``/``deliver_inbox`` outcome is uncertain after the provider
boundary (response loss, timeout, connection failure post-send). The state
must survive journal reopen (crash/reconcile) and any retry must be refused
without replaying the provider call.
"""

from __future__ import annotations

import json
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest

from cli_agent_orchestrator.services import managed_provider_bridge as bridge
from cli_agent_orchestrator.services import resource_registry as rr
from cli_agent_orchestrator.services.delivery_journal import (
    DeliveryJournal,
    DeliveryTransitionRefused,
)


@pytest.fixture
def short_root(monkeypatch):
    # AF_UNIX paths are length-capped; pytest's tmp_path is too deep on
    # macOS for a bridge socket, so the bridge root lives under /tmp.
    with tempfile.TemporaryDirectory(prefix="lb-amb-") as root:
        root_path = Path(root)
        subprocess.run(["git", "init", "-q"], cwd=root_path, check=True)
        (root_path / "identity.txt").write_text("submit-ambiguity\n", encoding="utf-8")
        subprocess.run(["git", "add", "identity.txt"], cwd=root_path, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=submit-ambiguity-test",
                "-c",
                "user.email=submit-ambiguity@example.test",
                "commit",
                "-qm",
                "identity",
            ],
            cwd=root_path,
            check=True,
        )
        monkeypatch.setattr(bridge, "RENDEZVOUS_ROOT", root_path / "runtime")
        rr.reset_resource_registry()
        rr.get_resource_registry(root_path / "registry.sqlite")
        try:
            yield root_path
        finally:
            rr.reset_resource_registry()


def _target(root, request):
    bridge_root = root / "bridge" / request["reservation_id"]
    target = {
        "root": bridge_root,
        "state": bridge_root / "state.json",
    }
    target.update(bridge.rendezvous_paths(request["rendezvous_identity"]))
    target["root"].mkdir(parents=True)
    return target


def _request(root, **overrides):
    request = {
        "reservation_id": str(uuid.uuid4()),
        "provider": "codex",
        "terminal_id": "a1b2c3d4",
        "generation": "generation-delivery",
        "obligation_generation": "obligation-delivery",
    }
    request.update(overrides)
    request["rendezvous_identity"] = bridge.launch_binding_identity(
        project="test-project",
        task_id=request["reservation_id"],
        terminal_id=request["terminal_id"],
        terminal_generation=request["generation"],
        working_directory=str(root.resolve()),
        actor="cafebabe",
    )
    return request


def _serve_in_thread(request, target, monkeypatch=None):
    if monkeypatch is not None:
        # The loop stays live for the assertion; suppress only its eventual
        # daemon-thread teardown, never the production registry-first claim.
        monkeypatch.setattr(bridge, "_deregister_bridge_resources", lambda *a, **k: None)
    server = threading.Thread(target=bridge._serve, args=(request, target), daemon=True)
    server.start()
    for _ in range(200):
        # A socket pathname can appear after bind but before listen, so it is
        # not a safe client-readiness signal.  Do not probe-connect: this
        # bridge fixture deliberately has one accept loop and a probe could
        # consume the request connection.  ``ready`` is atomically published
        # only after bind/chmod/listen and provider initialization.
        try:
            state = json.loads(target["state"].read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            state = None
        if state is not None and state.get("state") == "ready":
            return server
        if not server.is_alive():
            raise AssertionError("bridge exited before publishing ready state")
        time.sleep(0.01)
    raise AssertionError("bridge never published ready state")


def _call(target, request, command):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(str(target["socket"]))
        client.sendall(
            json.dumps(
                {
                    "rendezvous_identity": request["rendezvous_identity"],
                    "request": command,
                }
            ).encode()
            + b"\n"
        )
        raw = bytearray()
        while b"\n" not in raw:
            block = client.recv(65536)
            if not block:
                break
            raw.extend(block)
        return json.loads(bytes(raw).split(b"\n", 1)[0])
    finally:
        client.close()


class _ResponseLostSession:
    """Provider effect crosses the boundary, then the response is lost."""

    def __init__(self, request, marker, calls):
        self.rpc = None
        self._marker = marker
        self._calls = calls

    def initialize(self):
        return {"provider_session_id": "native"}

    def _scan_companion_events(self):
        return None

    def admit(self, command):
        self._calls.append("admit")
        self._marker.write_text("provider accepted before response loss", encoding="utf-8")
        raise bridge.SubmitUncertain("simulated response loss after provider accept")

    def deliver_inbox(self, command):
        self._calls.append("deliver")
        self._marker.write_text("provider accepted before response loss", encoding="utf-8")
        raise bridge.SubmitUncertain("simulated response loss after provider accept")

    def close(self):
        return None


class _PreBoundarySession(_ResponseLostSession):
    """A certain pre-boundary refusal: no provider I/O occurred."""

    def admit(self, command):
        self._calls.append("admit")
        raise bridge.BridgeError("admission does not match the exact bridge generation")


@pytest.fixture
def response_lost(short_root, monkeypatch):
    marker = short_root / "provider-effect"
    calls = []
    monkeypatch.setattr(
        bridge,
        "_ProviderSession",
        lambda request: _ResponseLostSession(request, marker, calls),
    )
    return marker, calls


def test_uncertain_admit_records_submit_ambiguous_via_real_serve(
    short_root, response_lost, monkeypatch
):
    marker, calls = response_lost
    request = _request(short_root)
    target = _target(short_root, request)
    delivery_id = str(uuid.uuid4())
    _serve_in_thread(request, target, monkeypatch)

    response = _call(target, request, {"op": "admit", "delivery_id": delivery_id})

    assert response["ok"] is False
    assert "submit-ambiguous" in response["error"]
    assert marker.exists(), "the provider effect crossed the boundary"

    journal = DeliveryJournal(target["root"] / "delivery-journal.db")
    record = journal.get("obligation-delivery", delivery_id)
    assert record["state"] == "submit-ambiguous"
    assert [event["to_state"] for event in record["events"]] == [
        "accepted",
        "terminal_queued",
        "submit-ambiguous",
    ]
    ambiguous_event = record["events"][-1]
    assert ambiguous_event["evidence_digest"], "ambiguity evidence must be durable"
    assert journal.is_ambiguous_preserved("obligation-delivery", delivery_id)


def test_submit_ambiguous_survives_reopen_and_retry_never_replays(
    short_root, response_lost, monkeypatch
):
    marker, calls = response_lost
    request = _request(short_root)
    target = _target(short_root, request)
    delivery_id = str(uuid.uuid4())
    _serve_in_thread(request, target, monkeypatch)

    first = _call(target, request, {"op": "admit", "delivery_id": delivery_id})
    assert first["ok"] is False

    # Crash/reconcile: a fresh journal handle over the same durable file
    # sees the preserved ambiguity.
    reopened = DeliveryJournal(target["root"] / "delivery-journal.db")
    record = reopened.get("obligation-delivery", delivery_id)
    assert record["state"] == "submit-ambiguous"

    # A retry over a fresh connection is refused and never replays the
    # provider call (no second effect, no downgrade to terminal_queued).
    retry = _call(target, request, {"op": "admit", "delivery_id": delivery_id})
    assert retry["ok"] is False
    assert calls == ["admit"], "the provider turn must not be resubmitted"
    record = reopened.get("obligation-delivery", delivery_id)
    assert record["state"] == "submit-ambiguous"
    assert [event["to_state"] for event in record["events"]] == [
        "accepted",
        "terminal_queued",
        "submit-ambiguous",
    ]


def test_pre_boundary_refusal_is_not_marked_ambiguous(short_root, monkeypatch):
    calls = []
    monkeypatch.setattr(
        bridge,
        "_ProviderSession",
        lambda request: _PreBoundarySession(request, short_root / "m", calls),
    )
    request = _request(short_root)
    target = _target(short_root, request)
    delivery_id = str(uuid.uuid4())
    _serve_in_thread(request, target, monkeypatch)

    response = _call(target, request, {"op": "admit", "delivery_id": delivery_id})

    assert response["ok"] is False
    journal = DeliveryJournal(target["root"] / "delivery-journal.db")
    record = journal.get("obligation-delivery", delivery_id)
    assert record["state"] == "terminal_queued"
    assert [event["to_state"] for event in record["events"]] == [
        "accepted",
        "terminal_queued",
    ]


def test_uncertain_deliver_records_submit_ambiguous_via_real_serve(
    short_root, response_lost, monkeypatch
):
    _, calls = response_lost
    request = _request(short_root)
    target = _target(short_root, request)
    message_id = str(uuid.uuid4())
    _serve_in_thread(request, target, monkeypatch)

    response = _call(target, request, {"op": "deliver", "message_id": message_id})

    assert response["ok"] is False
    assert calls == ["deliver"]
    journal = DeliveryJournal(target["root"] / "delivery-journal.db")
    record = journal.get("obligation-delivery", message_id)
    assert record["state"] == "submit-ambiguous"
    assert record["events"][-1]["evidence_digest"]


def test_terminal_queued_to_submit_ambiguous_transition_is_one_way(tmp_path):
    journal = DeliveryJournal(tmp_path / "journal.db")
    callback = str(uuid.uuid4())
    journal.open_intent("gen", callback, "a" * 64)
    journal.mark_terminal_queued("gen", callback)
    journal.mark_submit_ambiguous("gen", callback, evidence_digest="b" * 64)
    # submit-ambiguous is terminal: no replay, no ack, no downgrade.
    for illegal in (
        lambda: journal.mark_submitted("gen", callback),
        lambda: journal.mark_submit_acked("gen", callback),
        lambda: journal.mark_terminal_queued("gen", callback),
        lambda: journal.open_intent("gen", callback, "a" * 64),
    ):
        with pytest.raises(DeliveryTransitionRefused):
            illegal()
    assert journal.get("gen", callback)["state"] == "submit-ambiguous"
