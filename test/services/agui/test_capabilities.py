"""Unit tests for the run plane's AG-UI 1.0 capabilities declaration (#1192)."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from cli_agent_orchestrator.services.agui.capabilities import build_capabilities


def test_identity_names_cao_and_its_version():
    try:
        expected = version("cli-agent-orchestrator")
    except PackageNotFoundError:  # pragma: no cover - source tree without metadata
        expected = None
    identity = build_capabilities()["identity"]
    assert identity["name"] == "cli-agent-orchestrator"
    if expected is not None:
        assert identity["version"] == expected


def test_declares_only_what_the_run_plane_does():
    caps = build_capabilities()
    assert set(caps) == {"identity", "transport", "tools", "state", "humanInTheLoop"}
    assert caps["transport"] == {"streaming": True}
    assert caps["tools"] == {"clientProvided": False}
    assert caps["state"] == {"snapshots": True}


def test_no_value_is_null():
    """Absent means absent: an undeclared field is omitted, never null."""

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                assert value is not None
                walk(value)

    walk(build_capabilities())


def test_returns_a_fresh_object():
    a = build_capabilities()
    a["humanInTheLoop"]["approvals"] = False
    assert build_capabilities()["humanInTheLoop"]["approvals"] is True


def test_run_plane_never_reads_client_tools():
    """tools.clientProvided is declared false: the run plane must not read tools."""
    import inspect

    from cli_agent_orchestrator.services.agui import run_plane

    source = inspect.getsource(run_plane)
    assert "run_input.tools" not in source
    assert 'input_data.get("tools")' not in source
