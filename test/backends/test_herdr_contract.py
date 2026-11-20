"""Contract guard against herdr changing the event the inbox service depends on.

This is the regression guard for the class of bug fixed in #1378/#1379, where
herdr 0.9.0 (protocol 22) silently changed its event contract: it kept
*accepting* a broadcast ``pane.updated`` subscription but stopped *emitting*
it, so HerdrInboxService received nothing and every inbox delivery fell back to
the 30s reconciliation sweep. Nothing caught it because every other herdr test
mocks the socket, and the broken code passed all of them.

Unit tests cannot catch "herdr changed its wire contract" by construction — they
assert what *our* parser does, not what *herdr* actually sends. This test closes
that gap by asserting herdr's own published schema (``herdr api schema``) still
matches the two things the fix relies on:

1. ``pane.agent_status_changed`` is an emitted subscription-event kind carrying
   ``pane_id`` + ``agent_status``, and is a valid subscription type keyed by
   ``pane_id``. If herdr renames, removes, or reshapes it, this fails.
2. herdr's socket protocol is within the range the service has been verified
   against (``_HERDR_TESTED_PROTOCOL_MIN``..``MAX``). This is the tripwire that
   was missing at 0.9.0: a protocol bump now FAILS this test and forces whoever
   upgrades herdr to re-verify that event delivery still works and then widen
   the range deliberately — instead of the contract drifting silently.

What this catches, honestly:
- A schema change to ``pane.agent_status_changed`` (rename, removal, reshape) —
  the event is gone or different.
- A protocol bump past the verified range — the *observable signal that
  accompanied the 0.9.0 break* (protocol went 17→22). The tripwire would have
  fired at 0.9.0 and forced re-verification.

What it does NOT catch: herdr keeping the event in its schema at an unchanged
protocol but silently ceasing to *emit* it. That is a purely behavioural
regression, and catching it needs a real agent-status transition to assert
against. There is no cheap, deterministic, agent-free way to drive one —
verified against herdr 0.9.5: a command exiting in a pane does not fire it
(``agent_status`` tracks detected agents, not command exits), ``pane.report_agent``
does not emit on an agent-less pane, and ``agent.start`` spawns a real agent
process. So a behavioural end-to-end smoke requires launching a real agent CLI
and belongs in a dedicated "CAO against a live herdr" CI job, tracked as a
follow-up. This schema contract is the deterministic, agent-free guard that runs
in the normal suite.

Gated on the ``herdr`` binary being present, so a CI environment without herdr
skips cleanly.
"""

import json
import shutil
import subprocess

import pytest

from cli_agent_orchestrator.services.herdr_inbox_service import (
    _HERDR_TESTED_PROTOCOL_MAX,
    _HERDR_TESTED_PROTOCOL_MIN,
)

pytestmark = pytest.mark.skipif(
    shutil.which("herdr") is None,
    reason="herdr binary not installed; contract test needs the real schema",
)


def _herdr_schema() -> dict:
    """Return herdr's published API schema, or skip if this herdr can't produce it.

    A herdr that is present but cannot emit a usable schema — too old to have
    ``api schema``, a broken build, or malformed output — is not a contract
    regression we can assert on, so skip with a clear reason rather than fail
    with a confusing "contract changed" error.
    """
    try:
        result = subprocess.run(
            ["herdr", "api", "schema", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"herdr present but 'api schema' could not run ({exc}); cannot verify contract")
    if result.returncode != 0:
        pytest.skip(
            f"herdr present but 'api schema' failed ({result.stderr.strip()[:200]!r}); "
            "likely too old or a broken build — cannot verify contract"
        )
    try:
        schema = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        pytest.skip(f"herdr 'api schema' output is not JSON ({exc}); cannot verify contract")
    # Schema paths below are herdr-internal selectors into this document; a herdr
    # reorganization that keeps the API contract but relays it differently would
    # need these updated (the assertion messages make that clear).
    return schema


def test_agent_status_changed_is_still_an_emitted_event_with_the_expected_shape():
    """The per-pane event the fix subscribes to must still be emitted and carry
    pane_id + agent_status. herdr 0.9.x stopped emitting the broadcast
    pane.updated the OLD code relied on; this pins the replacement contract."""
    schema = _herdr_schema()
    sub_event = schema["schemas"]["subscription_event"]["$defs"]

    kinds = sub_event["SubscriptionEventKind"]["enum"]
    assert "pane.agent_status_changed" in kinds, (
        "herdr no longer lists pane.agent_status_changed as an emitted "
        f"subscription event (emitted kinds: {kinds}). The herdr inbox service "
        "subscribes to it for every delivery; if it is gone or renamed, "
        "event-driven delivery is dead and must be re-pointed at the new event."
    )

    required = set(sub_event["PaneAgentStatusChangedEvent"]["required"])
    assert {"pane_id", "agent_status"} <= required, (
        "pane.agent_status_changed payload no longer carries both pane_id and "
        f"agent_status (required fields: {sorted(required)}); the inbox service "
        "routes on pane_id and gates delivery on agent_status."
    )

    statuses = set(sub_event["AgentStatus"]["enum"])
    assert {"idle", "working", "done"} <= statuses, (
        "herdr agent-status vocabulary changed; the inbox service delivers on "
        f"idle/done and tracks working (got: {sorted(statuses)})."
    )


def test_agent_status_changed_is_a_valid_per_pane_subscription():
    """herdr rejects (and resets the connection on) an unknown subscription, so
    the per-pane shape the fix sends must stay a valid subscription type."""
    schema = _herdr_schema()
    variants = schema["schemas"]["request"]["$defs"]["Subscription"]["oneOf"]
    match = [
        v
        for v in variants
        if v.get("properties", {}).get("type", {}).get("const") == "pane.agent_status_changed"
    ]
    assert match, (
        "herdr no longer accepts a pane.agent_status_changed subscription; the "
        "inbox service's per-pane subscribe would be rejected and reset the "
        "connection on every reconnect."
    )
    assert "pane_id" in match[0].get("properties", {}), (
        "pane.agent_status_changed subscription no longer takes a pane_id; the "
        "service subscribes per managed pane."
    )


def test_protocol_is_within_the_verified_range():
    """Tripwire: a herdr protocol bump beyond the verified range fails here so
    event delivery is re-checked before trusting it — the check that was missing
    when 0.9.0 (protocol 22) silently changed the event contract."""
    protocol = _herdr_schema().get("protocol")
    assert isinstance(protocol, int), f"herdr schema has no integer protocol: {protocol!r}"
    assert _HERDR_TESTED_PROTOCOL_MIN <= protocol <= _HERDR_TESTED_PROTOCOL_MAX, (
        f"herdr socket protocol {protocol} is outside the verified range "
        f"[{_HERDR_TESTED_PROTOCOL_MIN}, {_HERDR_TESTED_PROTOCOL_MAX}]. Before "
        "widening the range in herdr_inbox_service.py, verify against the new "
        "herdr that a pane.agent_status_changed event actually arrives after an "
        "agent goes idle (0.9.0 silently stopped emitting broadcast pane.updated "
        "on exactly this kind of bump)."
    )
