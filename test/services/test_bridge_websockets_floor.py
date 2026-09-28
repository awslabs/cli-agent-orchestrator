"""The declared websockets floor covers the client API the channel uses.

The bridge uses ``websockets.asyncio.client.connect(additional_headers=...)`` and the
native attach uses ``websockets.sync.client.connect(additional_headers=...)``. The
asyncio client exists from websockets 14, so the dependency floor must be 14.0 or
higher, and the installed version must provide both call shapes.
"""

import inspect
import pathlib
import re

_PYPROJECT = pathlib.Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_the_declared_floor_is_at_least_14():
    match = re.search(r'"websockets>=(\d+)(?:\.\d+)*"', _PYPROJECT.read_text())
    assert match, "pyproject.toml must declare a websockets>= floor"
    assert int(match.group(1)) >= 14


def test_the_installed_clients_accept_additional_headers():
    from websockets.asyncio.client import connect as async_connect
    from websockets.sync.client import connect as sync_connect

    assert "additional_headers" in inspect.signature(async_connect).parameters
    assert "additional_headers" in inspect.signature(sync_connect).parameters


def test_the_bridge_uses_the_asyncio_client_explicitly():
    from websockets.asyncio.client import connect

    import cli_agent_orchestrator.runtime_channel.bridge as bridge

    assert bridge.connect is connect
