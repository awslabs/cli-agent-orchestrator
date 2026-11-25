"""Last test of the run: the rig left the owner's Kiro Crew alone (the session fixture re-checks at teardown)."""

from __future__ import annotations

import subprocess

import helpers as h


def test_zz_scratch_gateway_is_on_the_scratch_port_only(stack) -> None:
    out = subprocess.run(["ss", "-ltn"], capture_output=True, text=True, check=False).stdout
    assert f"127.0.0.1:{h.KC_PORT}" in out
    # the rig never binds the owner's gateway port
    assert h.KC_PORT != 5476 and h.CAO_PORT != 9889
    snapshot = h.owner_snapshot()
    assert "--- listeners" in snapshot
