"""The memory plugins leave a remote terminal alone on the central server (#745).

A terminal that runs in an execution runtime has its pane, and its working
directory, on another machine. The central server dispatches
``post_create_terminal`` for it, so the plugins must neither ask the server's own
tmux for that pane nor write into the server's filesystem.
"""

import importlib

import pytest

from cli_agent_orchestrator.plugins import PostCreateTerminalEvent

PLUGINS = [
    ("claude_code_memory", "ClaudeCodeMemoryPlugin", "claude_code"),
    ("kiro_cli_memory", "KiroCliMemoryPlugin", "kiro_cli"),
    ("codex_memory", "CodexMemoryPlugin", "codex"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("module_name,class_name,provider", PLUGINS)
async def test_a_remote_terminal_is_skipped_without_touching_local_tmux(
    monkeypatch, module_name, class_name, provider
):
    module = importlib.import_module(f"cli_agent_orchestrator.plugins.builtin.{module_name}")
    monkeypatch.setattr(
        module,
        "get_terminal_metadata",
        lambda terminal_id: {
            "id": terminal_id,
            "tmux_session": "cao-beef",
            "tmux_window": "developer-beef",
            "runtime_id": "cao-runtime-0",
        },
    )

    def local_tmux():
        raise AssertionError("the server's own tmux was asked about a remote pane")

    monkeypatch.setattr(module, "get_backend", local_tmux)
    plugin = getattr(module, class_name)()
    event = PostCreateTerminalEvent(
        terminal_id="beef0001", agent_name="developer", provider=provider, session_id="cao-beef"
    )
    assert plugin._resolve_working_directory(event) is None
