"""Exact Kimi Code 0.29.1 support, retained alongside 0.29.0 under the 0.29.2 pin.

0.29.1 was added as a *separate proven build*, not a range widening, and
stays admitted after the current pin moved to 0.29.2: already-minted
0.29.1/0.29.0 sessions must keep validating.  This suite pins the three
properties that support turns on:

1.  every version gate (contract, route, bridge) accepts 0.29.2, 0.29.1,
    and 0.29.0, and rejects everything else — an exact set, never a range;
2.  a multi-line message on any accepted session is deliverable, because
    the composer-newline table carries a separate proven entry per build
    whose keystroke plans are byte-identical; and
3.  a receipt records the *actual* installed version, so a 0.29.1 binary
    is never described by a 0.29.0 or 0.29.2 constant (or the reverse).
"""

from __future__ import annotations

import hashlib
import os
import stat

import pytest

from cli_agent_orchestrator.services import kimi_native_bootstrap as boot
from cli_agent_orchestrator.services import kimi_native_control as knc
from cli_agent_orchestrator.services import kimi_route
from cli_agent_orchestrator.services import provider_contracts as pc

CURRENT = "0.29.2"
RETAINED = "0.29.1"
OLDEST = "0.29.0"
ACCEPTED = (CURRENT, RETAINED, OLDEST)


# --------------------------------------------------------------------
# The version gates accept exactly {0.29.2, 0.29.1, 0.29.0}
# --------------------------------------------------------------------


def test_the_accepted_set_is_current_then_retained():
    assert pc.SUPPORTED_VERSIONS["kimi"] == ACCEPTED
    assert pc.PINNED_VERSIONS["kimi"] == CURRENT
    assert kimi_route.SUPPORTED_KIMI_VERSIONS == ACCEPTED


@pytest.mark.parametrize(
    "banner",
    [f"kimi {version}" for version in ACCEPTED] + list(ACCEPTED),
)
def test_check_pinned_version_accepts_all_three_builds(banner):
    pc.check_pinned_version("kimi", banner)


@pytest.mark.parametrize("bad", ["0.29.3", "0.28.9", "0.30.0", "1.0.0", "kimi", ""])
def test_check_pinned_version_rejects_everything_outside_the_set(bad):
    with pytest.raises(pc.ProviderVersionDrift):
        pc.check_pinned_version("kimi", bad)


# --------------------------------------------------------------------
# A multi-line message is deliverable on any accepted build
# --------------------------------------------------------------------


def test_the_composer_newline_table_keeps_the_separate_proven_0291_entry():
    entry = knc._PROVEN_COMPOSER_NEWLINE.get(RETAINED)
    assert entry is not None, "0.29.1 must be a separate keyed entry, never a range"
    # Byte-identical composer behaviour to 0.29.0, verified against the bundle.
    assert entry["keystroke"] == knc._PROVEN_COMPOSER_NEWLINE[OLDEST]["keystroke"]
    assert entry["normalization"] == knc.NORMALIZATION_JOIN_LF_THEN_TRIM
    assert "cba31835395ff75fa6b5bc9b81a7907c7d933e7e6a7d8ba53afac23dd0f5ab04" in entry["evidence"]


@pytest.mark.parametrize("version", ACCEPTED)
def test_a_multiline_message_is_deliverable_on_all_accepted_builds(version):
    plan = knc.plan_composer_keystrokes("line one\nline two", provider_version=version)
    assert plan["deliverable"] is True
    assert plan["undeliverable_reason"] is None
    assert plan["soft_newline_keystroke"] == "C-j"
    assert plan["encoding"] == knc.ENCODING_SOFT_NEWLINE


def test_an_unproven_neighbouring_version_is_not_silently_accepted():
    # 0.29.3 is adjacent but unread: a multi-line message on it must be
    # refused (undeliverable), not chunked, pasted, or flattened.
    plan = knc.plan_composer_keystrokes("line one\nline two", provider_version="0.29.3")
    assert plan["deliverable"] is False
    assert "0.29.3" in plan["undeliverable_reason"]


# --------------------------------------------------------------------
# Receipts record the actual installed version
# --------------------------------------------------------------------


class _FakeAcp:
    def __init__(self):
        self.calls = []
        self.terminated = 0
        self._options = [
            {"id": "model", "category": "model", "currentValue": "k"},
            {"id": "thinking", "category": "thought_level", "currentValue": "high"},
        ]

    def request(self, method, params):
        self.calls.append((method, dict(params)))
        if method == "initialize":
            return {"protocolVersion": 1}
        if method == "session/new":
            return {"sessionId": "session_9f2c41ab", "configOptions": self._options}
        if method == "session/set_config_option":
            for opt in self._options:
                if opt["id"] == params["configId"]:
                    opt["currentValue"] = params["value"]
            return {"configOptions": self._options}
        raise AssertionError(method)

    def terminate(self):
        self.terminated += 1
        return {
            "pid": 4242,
            "exit_status": 0,
            "escalation": [boot.STEP_STDIN_CLOSED],
            "reaped": True,
        }


@pytest.fixture
def pinned_binary(tmp_path):
    path = tmp_path / "kimi"
    path.write_bytes(b"#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return {
        "kimi_binary": os.path.realpath(str(path)),
        "binary_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


@pytest.mark.parametrize("version", ACCEPTED)
def test_the_bootstrap_receipt_records_the_actual_version(pinned_binary, tmp_path, version):
    transport = _FakeAcp()
    receipt = boot.mint_session(
        kimi_binary=pinned_binary["kimi_binary"],
        binary_sha256=pinned_binary["binary_sha256"],
        version_output=f"kimi {version}",
        working_directory=os.path.realpath(str(tmp_path)),
        model="k",
        effort="high",
        transport=transport,
    )
    # The receipt reflects the build that actually ran, not a pin constant.
    assert receipt["provider_version"] == version
