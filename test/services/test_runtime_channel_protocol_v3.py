"""Protocol v3 and the small bridge hygiene items on #802.

- v3: INPUT now carries ``frozen_memory`` and the attach stream carries an
  epoch; a v2 bridge would drop the field and ignore the epoch, so the version
  must bump and be refused at hello rather than silently mis-served.
- The bridge imports the websockets asyncio client explicitly.
- No non-inclusive fd names remain.
- The package docstring describes the transport that now exists.
"""

import inspect
from pathlib import Path

import cli_agent_orchestrator.runtime_channel as runtime_channel
from cli_agent_orchestrator.runtime_channel import bridge as bridge_mod
from cli_agent_orchestrator.runtime_channel.protocol import PROTOCOL_VERSION

_BRIDGE_SRC = Path(bridge_mod.__file__).read_text()


def test_protocol_version_is_3():
    assert PROTOCOL_VERSION == 3


def test_bridge_imports_the_asyncio_client_connect():
    from websockets.asyncio.client import ClientConnection, connect

    assert bridge_mod.connect is connect
    assert bridge_mod.ClientConnection is ClientConnection
    # The run loop connects through the explicit name, not top-level websockets.
    assert "websockets.connect(" not in _BRIDGE_SRC


def test_bridge_uses_inclusive_fd_names():
    assert "master_fd" not in _BRIDGE_SRC
    assert "slave_fd" not in _BRIDGE_SRC
    assert "parent_fd" in _BRIDGE_SRC and "child_fd" in _BRIDGE_SRC


def test_remote_attach_test_uses_inclusive_fd_names():
    src = (
        Path(inspect.getfile(runtime_channel)).parents[3] / "test" / "cli" / "test_remote_attach.py"
    )
    text = src.read_text()
    assert "slave" not in text
    assert "master" not in text


def test_package_docstring_describes_the_transport_modules():
    doc = runtime_channel.__doc__ or ""
    assert "no transport wiring yet" not in doc
    for module in ("api", "bridge", "registry"):
        assert module in doc, f"docstring should name the {module} module"
