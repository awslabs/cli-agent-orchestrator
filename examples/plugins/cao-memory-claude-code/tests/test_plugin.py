"""Tests for the Claude Code memory-injection plugin."""

from pathlib import Path

import pytest
from cao_memory_claude_code.plugin import (
    BEGIN_MARKER,
    END_MARKER,
    ClaudeCodeMemoryPlugin,
)

from cli_agent_orchestrator.plugins import PostCreateTerminalEvent


def _event(provider: str = "claude_code", terminal_id: str = "t1") -> PostCreateTerminalEvent:
    return PostCreateTerminalEvent(
        terminal_id=terminal_id,
        agent_name="developer",
        provider=provider,
        session_id="cao-test-session",
    )


@pytest.mark.asyncio
async def test_ignores_non_claude_code_providers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The hook must do nothing when the event is for another provider."""

    called: list[str] = []
    monkeypatch.setattr(
        "cao_memory_claude_code.plugin.get_terminal_metadata",
        lambda terminal_id: called.append(terminal_id) or None,
    )

    plugin = ClaudeCodeMemoryPlugin()
    await plugin.setup()
    await plugin.on_post_create_terminal(_event(provider="kiro_cli"))
    await plugin.teardown()

    assert called == [], "provider filter must short-circuit before any work"


@pytest.mark.asyncio
async def test_writes_memory_block_on_post_create_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On a claude_code terminal, the plugin should write the memory block."""

    monkeypatch.setattr(
        "cao_memory_claude_code.plugin.get_terminal_metadata",
        lambda terminal_id: {
            "tmux_session": "cao-test-session",
            "tmux_window": "developer-abcd",
            "id": terminal_id,
        },
    )
    monkeypatch.setattr(
        "cao_memory_claude_code.plugin.tmux_client.get_pane_working_directory",
        lambda session, window: str(tmp_path),
    )

    class FakeMemoryService:
        def get_memory_context_for_terminal(self, terminal_id: str) -> str:
            return "<cao-memory>\n## Context\n- stan prefers pytest\n</cao-memory>"

    monkeypatch.setattr(
        "cao_memory_claude_code.plugin.MemoryService",
        lambda: FakeMemoryService(),
    )

    plugin = ClaudeCodeMemoryPlugin()
    await plugin.setup()
    await plugin.on_post_create_terminal(_event())
    await plugin.teardown()

    target = tmp_path / ".claude" / "CLAUDE.md"
    assert target.exists()
    content = target.read_text(encoding="utf-8")
    assert BEGIN_MARKER in content
    assert END_MARKER in content
    assert "stan prefers pytest" in content


@pytest.mark.asyncio
async def test_replaces_existing_memory_block_on_rerun(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A second invocation should replace the prior block, not append."""

    target_dir = tmp_path / ".claude"
    target_dir.mkdir()
    target = target_dir / "CLAUDE.md"
    target.write_text(
        "# Project Notes\n\nHand-written content.\n"
        f"{BEGIN_MARKER}\n<cao-memory>OLD</cao-memory>\n{END_MARKER}\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "cao_memory_claude_code.plugin.get_terminal_metadata",
        lambda terminal_id: {
            "tmux_session": "cao-test-session",
            "tmux_window": "developer-abcd",
            "id": terminal_id,
        },
    )
    monkeypatch.setattr(
        "cao_memory_claude_code.plugin.tmux_client.get_pane_working_directory",
        lambda session, window: str(tmp_path),
    )

    class FakeMemoryService:
        def get_memory_context_for_terminal(self, terminal_id: str) -> str:
            return "<cao-memory>NEW</cao-memory>"

    monkeypatch.setattr(
        "cao_memory_claude_code.plugin.MemoryService",
        lambda: FakeMemoryService(),
    )

    plugin = ClaudeCodeMemoryPlugin()
    await plugin.on_post_create_terminal(_event())

    content = target.read_text(encoding="utf-8")
    assert "<cao-memory>NEW</cao-memory>" in content
    assert "<cao-memory>OLD</cao-memory>" not in content
    assert "Hand-written content." in content, "prior user content must be preserved"
    assert content.count(BEGIN_MARKER) == 1
    assert content.count(END_MARKER) == 1


@pytest.mark.asyncio
async def test_skips_write_when_memory_context_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Empty memory context must NOT create or modify CLAUDE.md."""

    monkeypatch.setattr(
        "cao_memory_claude_code.plugin.get_terminal_metadata",
        lambda terminal_id: {
            "tmux_session": "cao-test-session",
            "tmux_window": "developer-abcd",
            "id": terminal_id,
        },
    )
    monkeypatch.setattr(
        "cao_memory_claude_code.plugin.tmux_client.get_pane_working_directory",
        lambda session, window: str(tmp_path),
    )
    monkeypatch.setattr(
        "cao_memory_claude_code.plugin.MemoryService",
        lambda: type("F", (), {"get_memory_context_for_terminal": lambda self, t: ""})(),
    )

    plugin = ClaudeCodeMemoryPlugin()
    await plugin.on_post_create_terminal(_event())

    assert not (tmp_path / ".claude").exists()


@pytest.mark.asyncio
async def test_memory_fetch_failure_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Memory-service exceptions must be caught and logged."""

    monkeypatch.setattr(
        "cao_memory_claude_code.plugin.get_terminal_metadata",
        lambda terminal_id: {
            "tmux_session": "cao-test-session",
            "tmux_window": "developer-abcd",
            "id": terminal_id,
        },
    )
    monkeypatch.setattr(
        "cao_memory_claude_code.plugin.tmux_client.get_pane_working_directory",
        lambda session, window: str(tmp_path),
    )

    class ExplodingMemoryService:
        def get_memory_context_for_terminal(self, terminal_id: str) -> str:
            raise RuntimeError("db on fire")

    monkeypatch.setattr(
        "cao_memory_claude_code.plugin.MemoryService",
        lambda: ExplodingMemoryService(),
    )

    plugin = ClaudeCodeMemoryPlugin()
    with caplog.at_level("WARNING", logger="cao_memory_claude_code.plugin"):
        await plugin.on_post_create_terminal(_event())

    assert not (tmp_path / ".claude").exists()
    assert "memory fetch failed" in caplog.text


@pytest.mark.asyncio
async def test_missing_terminal_metadata_short_circuits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No metadata → no write, no crash."""

    monkeypatch.setattr(
        "cao_memory_claude_code.plugin.get_terminal_metadata",
        lambda terminal_id: None,
    )

    # MemoryService must not be called at all when metadata lookup fails.
    def _boom(*args, **kwargs):
        raise AssertionError("MemoryService must not be constructed when metadata missing")

    monkeypatch.setattr("cao_memory_claude_code.plugin.MemoryService", _boom)

    plugin = ClaudeCodeMemoryPlugin()
    await plugin.on_post_create_terminal(_event())

    assert not (tmp_path / ".claude").exists()


@pytest.mark.asyncio
async def test_path_containment_guard_rejects_escape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A working directory whose resolved CLAUDE.md path escapes must be refused.

    We simulate escape by pointing the CWD at a symlink whose real path is
    inside tmp_path, but building a .claude/CLAUDE.md that resolves to a
    sibling dir.
    """

    # Arrange: two sibling dirs. `cwd_link` is the advertised cwd; inside it
    # we plant a symlinked ".claude" that points at the sibling. That makes
    # `<cwd>/.claude/CLAUDE.md` resolve outside of `<cwd>`.
    real_cwd = tmp_path / "inside"
    sibling = tmp_path / "outside"
    real_cwd.mkdir()
    sibling.mkdir()
    (real_cwd / ".claude").symlink_to(sibling, target_is_directory=True)

    monkeypatch.setattr(
        "cao_memory_claude_code.plugin.get_terminal_metadata",
        lambda terminal_id: {
            "tmux_session": "cao-test-session",
            "tmux_window": "developer-abcd",
            "id": terminal_id,
        },
    )
    monkeypatch.setattr(
        "cao_memory_claude_code.plugin.tmux_client.get_pane_working_directory",
        lambda session, window: str(real_cwd),
    )

    class FakeMemoryService:
        def get_memory_context_for_terminal(self, terminal_id: str) -> str:
            return "<cao-memory>NEW</cao-memory>"

    monkeypatch.setattr(
        "cao_memory_claude_code.plugin.MemoryService",
        lambda: FakeMemoryService(),
    )

    plugin = ClaudeCodeMemoryPlugin()
    await plugin.on_post_create_terminal(_event())

    # Assert: neither the sibling nor the symlinked target got a CLAUDE.md.
    assert not (sibling / "CLAUDE.md").exists()
    assert not (real_cwd / ".claude" / "CLAUDE.md").exists()
