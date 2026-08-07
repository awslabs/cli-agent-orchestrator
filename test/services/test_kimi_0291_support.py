"""Exact Kimi Code version support under the open policy (cond-0331: 0.34.0).

Each accepted build was added as a *separate proven build*, never a range
widening, and stays admitted as the current pin moves forward: already-minted
sessions under older builds must keep validating.  Under the cond-0331 open
policy, Kimi additionally accepts any non-empty semver-shaped version at the
launch identity boundary, while the exact ``SUPPORTED_VERSIONS`` tuple still
gates feature-specific authority.  This suite pins the three properties that
support turns on:

1.  every version gate (contract, route, bridge) accepts 0.34.0, 0.33.0,
    0.32.0, 0.31.0, 0.30.0, 0.29.2, 0.29.1, and 0.29.0 for proven-build
    authority, and rejects unparseable banners; semver-shaped versions outside
    the proven set are accepted at launch but inherit no advanced authority;
2.  a multi-line message on any accepted session is deliverable, because
    the composer-newline table carries a separate proven entry per build
    whose keystroke plans are byte-identical; and
3.  a receipt records the *actual* installed version, so a 0.29.1 binary
    is never described by a 0.29.0, 0.30.0, 0.31.0, 0.32.0, 0.33.0, or
    0.34.0 constant (or the reverse).

This exact ACCEPTED tuple is the cross-repo parity contract for proven
builds: it must equal the conductor's capability.accepted_versions("kimi_cli"),
and each side's suite pins it so a future drift fails its own run.
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

CURRENT = "0.34.0"
PIN_0330 = "0.33.0"
PIN_0320 = "0.32.0"
PIN_0310 = "0.31.0"
PIN_0300 = "0.30.0"
PIN_0292 = "0.29.2"
RETAINED = "0.29.1"
OLDEST = "0.29.0"
ACCEPTED = (CURRENT, PIN_0330, PIN_0320, PIN_0310, PIN_0300, PIN_0292, RETAINED, OLDEST)


# --------------------------------------------------------------------
# The version gates accept exactly the proven set for feature authority
# --------------------------------------------------------------------


def test_the_accepted_set_is_current_then_retained():
    assert pc.SUPPORTED_VERSIONS["kimi"] == ACCEPTED
    assert pc.PINNED_VERSIONS["kimi"] == CURRENT
    assert kimi_route.SUPPORTED_KIMI_VERSIONS == ACCEPTED


@pytest.mark.parametrize(
    "banner",
    [f"kimi {version}" for version in ACCEPTED] + list(ACCEPTED),
)
def test_check_pinned_version_accepts_every_accepted_build(banner):
    pc.check_pinned_version("kimi", banner)


@pytest.mark.parametrize("banner", ["0.35.0", "kimi 0.35.0", "9.9.9"])
def test_check_pinned_version_accepts_future_semver_at_launch_boundary(banner):
    # Open enforcement: a future semver-shaped version must not be refused
    # at the launch identity boundary before task bytes.
    pc.check_pinned_version("kimi", banner)


@pytest.mark.parametrize(
    "bad",
    ["kimi", ""],
)
def test_check_pinned_version_rejects_unparseable_banners(bad):
    # Unknown/unparseable versions still fail closed at the boundary.
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


def test_the_composer_newline_table_keeps_the_separate_proven_0300_entry():
    entry = knc._PROVEN_COMPOSER_NEWLINE.get(PIN_0300)
    assert entry is not None, "0.30.0 must be a separate keyed entry, never a range"
    # The same composer facts as the 0.29.x line, read from the installed bundle.
    assert entry["keystroke"] == knc._PROVEN_COMPOSER_NEWLINE[OLDEST]["keystroke"]
    assert entry["normalization"] == knc.NORMALIZATION_JOIN_LF_THEN_TRIM
    assert "49ad0553cff0b5f60f83ba85df56bb5ccdbcb908158c80d9363d0e5a529ea51c" in entry["evidence"]


def test_the_composer_newline_table_keeps_the_separate_proven_0310_entry():
    entry = knc._PROVEN_COMPOSER_NEWLINE.get(PIN_0310)
    assert entry is not None, "0.31.0 must be a separate keyed entry, never a range"
    # cond-0310: the installed 0.31.0 bundle declares the same composer facts as
    # the 0.29.x/0.30.0 line (newLine ['shift+enter','ctrl+j'], submit 'enter',
    # expandPasteMarkers(lines.join("\n")).trim(), PASTE_ENTER_SUPPRESS_WINDOW_MS
    # = 120) — read directly from dist/main.mjs, never copied without comparison.
    assert entry["keystroke"] == knc._PROVEN_COMPOSER_NEWLINE[OLDEST]["keystroke"]
    assert entry["normalization"] == knc.NORMALIZATION_JOIN_LF_THEN_TRIM
    assert "689fc2a123dfc3145dab26a8e6a86c71a5dc8552b13fe0449679e065ce96774e" in entry["evidence"]


def test_the_composer_newline_table_keeps_the_separate_proven_0320_entry():
    entry = knc._PROVEN_COMPOSER_NEWLINE.get(PIN_0320)
    assert entry is not None, "0.32.0 must be a separate keyed entry, never a range"
    # cond-0315: the installed 0.32.0 bundle declares the same composer facts as
    # the 0.29.x/0.30.0/0.31.0 line — each snippet verified byte-identical
    # against the npm-published 0.31.0 bundle, and the installed digest matches
    # the npm-published 0.32.0 digest.
    assert entry["keystroke"] == knc._PROVEN_COMPOSER_NEWLINE[OLDEST]["keystroke"]
    assert entry["normalization"] == knc.NORMALIZATION_JOIN_LF_THEN_TRIM
    assert "b02ebfe77dda7d9f38cf61c5a923567eb7ff4f3bc914dff24b02b5fd22b4ff79" in entry["evidence"]


def test_the_composer_newline_table_keeps_the_separate_proven_0330_entry():
    entry = knc._PROVEN_COMPOSER_NEWLINE.get(PIN_0330)
    assert entry is not None, "0.33.0 must be a separate keyed entry, never a range"
    # cond-0315: the installed 0.33.0 bundle declares the same composer facts as
    # the 0.29.x/0.30.0/0.31.0/0.32.0 line — each snippet verified
    # byte-identical against the npm-published 0.32.0 bundle, and the installed
    # digest matches the npm-published 0.33.0 digest.
    assert entry["keystroke"] == knc._PROVEN_COMPOSER_NEWLINE[OLDEST]["keystroke"]
    assert entry["normalization"] == knc.NORMALIZATION_JOIN_LF_THEN_TRIM
    assert "0e77b9c64e67a4eecb96aae011750668aab11bd781564fe3e4855513812247b2" in entry["evidence"]


def test_the_composer_newline_table_has_a_separate_proven_0340_entry():
    entry = knc._PROVEN_COMPOSER_NEWLINE.get(CURRENT)
    assert entry is not None, "0.34.0 must be a separate keyed entry, never a range"
    # cond-0331: 0.34.0 was admitted by the compatibility check; its composer
    # facts are byte-identical to the attested 0.33.0 line.
    assert entry["keystroke"] == knc._PROVEN_COMPOSER_NEWLINE[OLDEST]["keystroke"]
    assert entry["normalization"] == knc.NORMALIZATION_JOIN_LF_THEN_TRIM
    assert "d3e781774e7a95f71e9d813e2cda95486d15db73712b3e821dd4a357b0511d8c" in entry["evidence"]


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


def test_a_future_semver_has_no_proven_composer_authority():
    # Open enforcement accepts 0.35.0 at launch, but it does not inherit the
    # composer control authority of the proven builds.
    plan = knc.plan_composer_keystrokes("line one\nline two", provider_version="0.35.0")
    assert plan["deliverable"] is False
    assert "0.35.0" in plan["undeliverable_reason"]


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
