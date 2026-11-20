"""A characterization matrix of the grant sidecar (#1384). It is not a fix.

Nothing here fails on ``main``: the behaviour was already pinned piecemeal across #1253,
#1273, #1283, #1285, #1290 and #1294, and this file walks it in one place:

    operation:  grant (``upsert_agent_tools``) | withdrawal (``remove_agent_tools``)
    state:      missing | written | bad-json | wrong-shape | unwritable

Each case asserts what the first call leaves in ``opencode.json`` and the sidecar, what
the warning says, and that the repair the warning (or the install flow) points at
converges on a second call. A case that stopped after the first call would pass for a
repair hint that does not work. A grant in an unwritable directory is also run with an
intact sidecar.

Not covered: a sidecar with an unknown ``version``, and a sidecar that is a directory.
The unwritable cases skip when run as root (root writes into a read-only directory), so
they only count on a non-root runner.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Dict, Iterator, List, Optional

import pytest

import cli_agent_orchestrator.utils.opencode_config as cfg_module
from cli_agent_orchestrator.utils.opencode_config import (
    GRANT_RECORD_REPAIR_HINT,
    read_config,
    remove_agent_tools,
    upsert_agent_tools,
    write_config,
)

AGENT = "developer"
KEY = "z-server*"
OTHER_ROW = {"other": ["other-tools*"]}

DAMAGED = {
    "bad-json": b'{"version": 1, "agents": {"other": ["other-tools*"]}',
    "wrong-shape": b'{"version": 1, "agents": ["other"]}',
}


@pytest.fixture()
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_file = tmp_path / "opencode_cli" / "opencode.json"
    monkeypatch.setattr(cfg_module, "OPENCODE_CONFIG_FILE", config_file)
    write_config({})
    return config_file


def _sidecar(config_file: Path) -> Path:
    return config_file.with_name(cfg_module.GRANT_RECORD_FILENAME)


def _tools() -> Optional[Dict[str, bool]]:
    return read_config().get("agent", {}).get(AGENT, {}).get("tools")


def _agents(config_file: Path) -> Dict[str, List[str]]:
    return json.loads(_sidecar(config_file).read_text(encoding="utf-8"))["agents"]


@contextlib.contextmanager
def _unwritable(config_file: Path) -> Iterator[None]:
    """Directory read-only: ``opencode.json`` exists and the sidecar cannot be created or replaced."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root writes into a read-only directory")
    config_file.parent.chmod(0o500)
    try:
        yield
    finally:
        config_file.parent.chmod(0o700)


def _warnings(caplog: pytest.LogCaptureFixture) -> List[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def _write_failures(caplog: pytest.LogCaptureFixture) -> List[str]:
    return [m for m in _warnings(caplog) if "Could not write the OpenCode grant record" in m]


def _damage_warnings(caplog: pytest.LogCaptureFixture) -> List[str]:
    return [m for m in _warnings(caplog) if "grant record" in m]


def _assert_repair_hint_in_every_damage_warning(
    caplog: pytest.LogCaptureFixture, sidecar: Path
) -> None:
    messages = _damage_warnings(caplog)
    assert messages, _warnings(caplog)
    for message in messages:
        assert GRANT_RECORD_REPAIR_HINT in message, message
    assert any(str(sidecar) in m for m in messages), "a warning must name the damaged file"


# ── grant ────────────────────────────────────────────────────────────────────


class TestGrant:
    def test_missing(self, config: Path, caplog):
        """First install: no sidecar is the normal state. Silent, and the row is created."""
        assert not _sidecar(config).exists()
        with caplog.at_level(logging.WARNING):
            upsert_agent_tools(AGENT, ["z-server"])
        assert _tools() == {KEY: True}
        assert _agents(config) == {AGENT: [KEY]}
        assert _warnings(caplog) == []

    def test_written(self, config: Path, caplog):
        """An intact sidecar gains this agent's row and keeps every other agent's."""
        _sidecar(config).write_text(json.dumps({"version": 1, "agents": OTHER_ROW}))
        with caplog.at_level(logging.WARNING):
            upsert_agent_tools(AGENT, ["z-server"])
        assert _tools() == {KEY: True}
        assert _agents(config) == {**OTHER_ROW, AGENT: [KEY]}
        assert _warnings(caplog) == []

    @pytest.mark.parametrize("content", DAMAGED.values(), ids=DAMAGED.keys())
    def test_damaged_then_repaired_by_hand(self, config: Path, caplog, content: bytes):
        """The grant lands, the damaged file is untouched, and the hint's repair converges."""
        sidecar = _sidecar(config)
        sidecar.write_bytes(content)
        with caplog.at_level(logging.WARNING):
            upsert_agent_tools(AGENT, ["z-server"])
        assert _tools() == {KEY: True}, "the install itself must not fail"
        assert sidecar.read_bytes() == content, "fail closed: never publish over a damaged file"
        _assert_repair_hint_in_every_damage_warning(caplog, sidecar)

        # Follow the hint: repair by hand (keeping the other agent's row), then reinstall.
        sidecar.write_text(json.dumps({"version": 1, "agents": OTHER_ROW}))
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            upsert_agent_tools(AGENT, ["z-server"])
        assert _agents(config) == {**OTHER_ROW, AGENT: [KEY]}, "recorded; other row survived"
        assert _warnings(caplog) == []

    def test_unwritable_with_an_intact_sidecar(self, config: Path, caplog):
        """An intact sidecar in a read-only directory: the new row is not recorded, the old one survives."""
        _sidecar(config).write_text(json.dumps({"version": 1, "agents": OTHER_ROW}))
        with _unwritable(config), caplog.at_level(logging.WARNING):
            upsert_agent_tools(AGENT, ["z-server"])
        assert len(_write_failures(caplog)) == 1, _warnings(caplog)
        assert _tools() == {KEY: True}
        assert _agents(config) == OTHER_ROW, "the other agent's row is untouched"

    def test_unwritable_then_writable(self, config: Path, caplog):
        """The grant lands in opencode.json unrecorded; the warning is the write warning (not the repair hint)."""
        with _unwritable(config), caplog.at_level(logging.WARNING):
            upsert_agent_tools(AGENT, ["z-server"])
        failures = _write_failures(caplog)
        assert len(failures) == 1, _warnings(caplog)
        assert GRANT_RECORD_REPAIR_HINT not in failures[0], "unwritable is not damaged"
        assert _tools() == {KEY: True}, "the grant itself lands in opencode.json"
        assert not _sidecar(config).exists(), "nothing was recorded"

        caplog.clear()
        with caplog.at_level(logging.WARNING):
            upsert_agent_tools(AGENT, ["z-server"])
        assert _tools() == {KEY: True}
        assert _agents(config) == {AGENT: [KEY]}, "the next install that can write records it"
        assert _warnings(caplog) == []


# ── withdrawal ───────────────────────────────────────────────────────────────


@pytest.fixture()
def granted(config: Path) -> Path:
    """A profile-server grant installed with a healthy sidecar: config and record agree."""
    upsert_agent_tools(AGENT, ["z-server"])
    assert _tools() == {KEY: True}
    assert _agents(config) == {AGENT: [KEY]}
    return config


def _withdrawn() -> bool:
    return AGENT not in read_config().get("agent", {})


class TestWithdrawal:
    def test_written(self, granted: Path, caplog):
        with caplog.at_level(logging.WARNING):
            remove_agent_tools(AGENT)
        assert _withdrawn(), "the entry held only CAO's grant, so it is pruned"
        assert AGENT not in _agents(granted)
        assert _warnings(caplog) == []

    def test_missing_then_reinstalled(self, granted: Path, caplog):
        """No record means no proof: the key stays, silently. A reinstall records it, then it goes."""
        _sidecar(granted).unlink()
        with caplog.at_level(logging.WARNING):
            remove_agent_tools(AGENT)
        assert _tools() == {KEY: True}, "unprovable grants are left in place, never deleted"
        assert _warnings(caplog) == []

        upsert_agent_tools(AGENT, ["z-server"])
        assert _agents(granted) == {AGENT: [KEY]}
        remove_agent_tools(AGENT)
        assert _withdrawn()
        assert AGENT not in _agents(granted)

    @pytest.mark.parametrize("content", DAMAGED.values(), ids=DAMAGED.keys())
    def test_damaged_then_repaired_by_hand(self, granted: Path, caplog, content: bytes):
        sidecar = _sidecar(granted)
        sidecar.write_bytes(content)
        with caplog.at_level(logging.WARNING):
            remove_agent_tools(AGENT)
        assert _tools() == {KEY: True}, "no proof, so the grant stays"
        assert sidecar.read_bytes() == content
        _assert_repair_hint_in_every_damage_warning(caplog, sidecar)

        # Follow the hint: restore the row by hand, then withdraw again.
        sidecar.write_text(json.dumps({"version": 1, "agents": {**OTHER_ROW, AGENT: [KEY]}}))
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            remove_agent_tools(AGENT)
        assert _withdrawn()
        assert _agents(granted) == OTHER_ROW, "only this agent's row went"
        assert _warnings(caplog) == []

    def test_unwritable_then_writable(self, granted: Path, caplog):
        """A withdrawal is recorded before it is carried out, so it is held back, not half done."""
        with _unwritable(granted), caplog.at_level(logging.WARNING):
            remove_agent_tools(AGENT)
        failures = _write_failures(caplog)
        assert len(failures) == 1, _warnings(caplog)
        assert KEY in failures[0], "the warning names the held-back key"
        assert GRANT_RECORD_REPAIR_HINT not in failures[0]
        assert _tools() == {KEY: True}, "fail closed: the grant stays"
        assert _agents(granted) == {AGENT: [KEY]}, "and the record still says it is CAO's"

        caplog.clear()
        with caplog.at_level(logging.WARNING):
            remove_agent_tools(AGENT)
        assert _withdrawn()
        assert AGENT not in _agents(granted)
        assert _warnings(caplog) == []
