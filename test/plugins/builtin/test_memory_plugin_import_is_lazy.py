"""The builtin memory plugins must not touch the database at IMPORT time.

All three are discovered through ``cao.plugins`` entry points, and that discovery
runs when the MCP server imports — inside every agent process. A module-level
``from cli_agent_orchestrator.clients.database import ...`` therefore executed
that module's import-time ``_ensure_db_dir()`` -> ``DB_DIR.mkdir()`` in agents,
which fails outright wherever the data directory is unreadable (the sandboxed
``~/.aws`` case this change exists for). The import is now function-local.

Checked in a FRESH interpreter per plugin rather than with an in-process
``sys.modules`` assertion: by the time a normal test session reaches this file,
``clients.database`` has almost certainly been imported by something else, so the
in-process version would pass whether or not the fix were present — the exact
false negative that let a future refactor silently reintroduce the eager import.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

_PLUGIN_MODULES = [
    "cli_agent_orchestrator.plugins.builtin.claude_code_memory",
    "cli_agent_orchestrator.plugins.builtin.codex_memory",
    "cli_agent_orchestrator.plugins.builtin.kiro_cli_memory",
]


@pytest.mark.parametrize("module", _PLUGIN_MODULES, ids=lambda m: m.rsplit(".", 1)[-1])
def test_importing_a_memory_plugin_does_not_import_clients_database(module, tmp_path):
    probe = textwrap.dedent(f"""
        import importlib, sys
        importlib.import_module({module!r})
        leaked = "cli_agent_orchestrator.clients.database" in sys.modules
        print("LEAKED" if leaked else "CLEAN")
        """)
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
        # A writable CAO_HOME_DIR so a REGRESSION shows up as the assertion below
        # rather than as an unrelated mkdir crash on the real data dir.
        env={
            "PATH": "/usr/bin:/bin",
            "CAO_HOME_DIR": str(tmp_path),
            "PYTHONPATH": ":".join(sys.path),
        },
    )
    assert result.returncode == 0, f"probe failed: {result.stderr[-800:]}"
    assert "CLEAN" in result.stdout, (
        f"{module} imported clients.database at module scope, so importing it in an "
        f"agent process runs DB_DIR.mkdir(). Keep the import function-local.\n"
        f"stdout={result.stdout!r}"
    )


@pytest.mark.parametrize("module", _PLUGIN_MODULES, ids=lambda m: m.rsplit(".", 1)[-1])
def test_the_lazy_wrapper_delegates_to_clients_database(module, monkeypatch):
    """The wrapper's own body must be exercised, not just patched over.

    Every other plugin test monkeypatches ``<module>.get_terminal_metadata`` as a
    seam, so the wrapper's two real lines — the function-local import and the
    delegation — never execute in the suite. That left the laziness fix in an odd
    state: the import site was pinned as absent at module scope (above) but
    nothing proved the deferred import actually resolves and forwards. A typo in
    the imported name would pass every test and fail only in production, on the
    server-side path this indirection exists to serve.
    """
    import importlib

    plugin = importlib.import_module(module)
    calls = []

    def fake_get_terminal_metadata(terminal_id: str):
        calls.append(terminal_id)
        return {"session_name": "s", "agent_name": "a"}

    # Patch the SOURCE, so the wrapper's own import + call still run.
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata",
        fake_get_terminal_metadata,
    )

    assert plugin.get_terminal_metadata("term-1") == {"session_name": "s", "agent_name": "a"}
    assert calls == ["term-1"], "wrapper did not forward to clients.database"
