"""Tests for the Claude Code memory-injection plugin."""

from pathlib import Path

import pytest

from cli_agent_orchestrator.plugins import PostCreateTerminalEvent
from cli_agent_orchestrator.plugins.builtin.claude_code_memory import (
    BEGIN_MARKER,
    END_MARKER,
    ClaudeCodeMemoryPlugin,
)


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
        "cli_agent_orchestrator.plugins.builtin.claude_code_memory.get_terminal_metadata",
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
        "cli_agent_orchestrator.plugins.builtin.claude_code_memory.get_terminal_metadata",
        lambda terminal_id: {
            "tmux_session": "cao-test-session",
            "tmux_window": "developer-abcd",
            "id": terminal_id,
        },
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.plugins.builtin.claude_code_memory.tmux_client.get_pane_working_directory",
        lambda session, window: str(tmp_path),
    )

    class FakeMemoryService:
        def get_memory_context_for_terminal(self, terminal_id: str) -> str:
            return "<cao-memory>\n## Context\n- stan prefers pytest\n</cao-memory>"

    monkeypatch.setattr(
        "cli_agent_orchestrator.plugins.builtin.claude_code_memory.MemoryService",
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
        "cli_agent_orchestrator.plugins.builtin.claude_code_memory.get_terminal_metadata",
        lambda terminal_id: {
            "tmux_session": "cao-test-session",
            "tmux_window": "developer-abcd",
            "id": terminal_id,
        },
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.plugins.builtin.claude_code_memory.tmux_client.get_pane_working_directory",
        lambda session, window: str(tmp_path),
    )

    class FakeMemoryService:
        def get_memory_context_for_terminal(self, terminal_id: str) -> str:
            return "<cao-memory>NEW</cao-memory>"

    monkeypatch.setattr(
        "cli_agent_orchestrator.plugins.builtin.claude_code_memory.MemoryService",
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
        "cli_agent_orchestrator.plugins.builtin.claude_code_memory.get_terminal_metadata",
        lambda terminal_id: {
            "tmux_session": "cao-test-session",
            "tmux_window": "developer-abcd",
            "id": terminal_id,
        },
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.plugins.builtin.claude_code_memory.tmux_client.get_pane_working_directory",
        lambda session, window: str(tmp_path),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.plugins.builtin.claude_code_memory.MemoryService",
        lambda: type("F", (), {"get_memory_context_for_terminal": lambda self, t: ""})(),
    )

    plugin = ClaudeCodeMemoryPlugin()
    await plugin.on_post_create_terminal(_event())

    assert not (tmp_path / ".claude").exists()


@pytest.mark.asyncio
async def test_disabled_memory_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When memory is disabled, the real MemoryService returns "" and the
    plugin must create no file — no .claude dir, no zero-byte CLAUDE.md, no
    empty marker block. Uses the real MemoryService (only the enabled flag is
    patched) so this exercises the actual disabled short-circuit, not a stub.
    """

    monkeypatch.setattr(
        "cli_agent_orchestrator.plugins.builtin.claude_code_memory.get_terminal_metadata",
        lambda terminal_id: {
            "tmux_session": "cao-test-session",
            "tmux_window": "developer-abcd",
            "id": terminal_id,
        },
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.plugins.builtin.claude_code_memory.tmux_client.get_pane_working_directory",
        lambda session, window: str(tmp_path),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.is_memory_enabled",
        lambda: False,
    )

    plugin = ClaudeCodeMemoryPlugin()
    await plugin.on_post_create_terminal(_event())

    assert not (tmp_path / ".claude").exists()
    assert list(tmp_path.iterdir()) == [], "disabled memory must leave the cwd untouched"


@pytest.mark.asyncio
async def test_memory_fetch_failure_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Memory-service exceptions must be caught and logged."""

    monkeypatch.setattr(
        "cli_agent_orchestrator.plugins.builtin.claude_code_memory.get_terminal_metadata",
        lambda terminal_id: {
            "tmux_session": "cao-test-session",
            "tmux_window": "developer-abcd",
            "id": terminal_id,
        },
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.plugins.builtin.claude_code_memory.tmux_client.get_pane_working_directory",
        lambda session, window: str(tmp_path),
    )

    class ExplodingMemoryService:
        def get_memory_context_for_terminal(self, terminal_id: str) -> str:
            raise RuntimeError("db on fire")

    monkeypatch.setattr(
        "cli_agent_orchestrator.plugins.builtin.claude_code_memory.MemoryService",
        lambda: ExplodingMemoryService(),
    )

    plugin = ClaudeCodeMemoryPlugin()
    with caplog.at_level(
        "WARNING", logger="cli_agent_orchestrator.plugins.builtin.claude_code_memory"
    ):
        await plugin.on_post_create_terminal(_event())

    assert not (tmp_path / ".claude").exists()
    assert "memory fetch failed" in caplog.text


@pytest.mark.asyncio
async def test_missing_terminal_metadata_short_circuits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No metadata → no write, no crash."""

    monkeypatch.setattr(
        "cli_agent_orchestrator.plugins.builtin.claude_code_memory.get_terminal_metadata",
        lambda terminal_id: None,
    )

    # MemoryService must not be called at all when metadata lookup fails.
    def _boom(*args, **kwargs):
        raise AssertionError("MemoryService must not be constructed when metadata missing")

    monkeypatch.setattr(
        "cli_agent_orchestrator.plugins.builtin.claude_code_memory.MemoryService", _boom
    )

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
        "cli_agent_orchestrator.plugins.builtin.claude_code_memory.get_terminal_metadata",
        lambda terminal_id: {
            "tmux_session": "cao-test-session",
            "tmux_window": "developer-abcd",
            "id": terminal_id,
        },
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.plugins.builtin.claude_code_memory.tmux_client.get_pane_working_directory",
        lambda session, window: str(real_cwd),
    )

    class FakeMemoryService:
        def get_memory_context_for_terminal(self, terminal_id: str) -> str:
            return "<cao-memory>NEW</cao-memory>"

    monkeypatch.setattr(
        "cli_agent_orchestrator.plugins.builtin.claude_code_memory.MemoryService",
        lambda: FakeMemoryService(),
    )

    plugin = ClaudeCodeMemoryPlugin()
    await plugin.on_post_create_terminal(_event())

    # Assert: neither the sibling nor the symlinked target got a CLAUDE.md.
    assert not (sibling / "CLAUDE.md").exists()
    assert not (real_cwd / ".claude" / "CLAUDE.md").exists()


def test_validated_target_path_rejects_null_byte() -> None:
    """A working directory containing a null byte must be rejected.

    Parity with the Kiro plugin's guard — a null byte in a path is always a
    sign of corrupted/hostile input and must never reach the filesystem call.
    """

    plugin = ClaudeCodeMemoryPlugin()
    with pytest.raises(ValueError, match="null byte"):
        plugin._validated_target_path("/tmp/proj\x00/evil")


def test_validated_target_path_missing_dir_raises_valueerror() -> None:
    """A non-existent working dir must raise ValueError, not OSError.

    resolve(strict=True) raises FileNotFoundError (an OSError) for a missing
    cwd; the handler only catches ValueError, so the validator must convert
    it. Otherwise the error escapes the plugin's log-and-skip contract.
    """

    plugin = ClaudeCodeMemoryPlugin()
    with pytest.raises(ValueError, match="not resolvable"):
        plugin._validated_target_path("/nonexistent-cao-dir-xyz123/sub")


@pytest.mark.asyncio
async def test_missing_working_dir_does_not_escape_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ephemeral/missing cwd must be logged-and-skipped, never raised.

    Exercises the full handler: resolve(strict=True) on the missing dir would
    raise FileNotFoundError; the plugin must swallow it via its ValueError
    path rather than letting it reach PluginRegistry.dispatch.
    """

    monkeypatch.setattr(
        "cli_agent_orchestrator.plugins.builtin.claude_code_memory.get_terminal_metadata",
        lambda terminal_id: {
            "tmux_session": "cao-test-session",
            "tmux_window": "developer-abcd",
            "id": terminal_id,
        },
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.plugins.builtin.claude_code_memory.tmux_client.get_pane_working_directory",
        lambda session, window: "/nonexistent-cao-dir-xyz123/sub",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.plugins.builtin.claude_code_memory.MemoryService",
        lambda: type(
            "F",
            (),
            {"get_memory_context_for_terminal": lambda self, t: "<cao-memory>X</cao-memory>"},
        )(),
    )

    plugin = ClaudeCodeMemoryPlugin()
    # Must not raise.
    await plugin.on_post_create_terminal(_event())


def test_strip_existing_block_removes_multiple_blocks() -> None:
    """Files corrupted with several injected blocks must be fully cleaned so
    repeated runs converge to exactly one block (Copilot finding on #269)."""

    content = (
        "# Notes\n"
        f"{BEGIN_MARKER}\nold one\n{END_MARKER}\n"
        "middle text\n"
        f"{BEGIN_MARKER}\nold two\n{END_MARKER}\n"
        "tail text\n"
    )

    stripped = ClaudeCodeMemoryPlugin._strip_existing_block(content)

    assert BEGIN_MARKER not in stripped
    assert END_MARKER not in stripped
    assert "old one" not in stripped
    assert "old two" not in stripped
    assert "# Notes" in stripped
    assert "middle text" in stripped
    assert "tail text" in stripped


def test_strip_existing_block_preserves_content_around_stray_begin() -> None:
    """A stray unclosed BEGIN must not pair with a later block's END and delete
    the user content in between (Copilot finding on #269). Only the stray marker
    token is removed; surrounding text and the real block survive for re-strip."""

    content = (
        "# Notes\n"
        f"{BEGIN_MARKER}\n"  # stray, unclosed
        "important user notes\n"
        f"{BEGIN_MARKER}\nreal block\n{END_MARKER}\n"
        "tail text\n"
    )

    stripped = ClaudeCodeMemoryPlugin._strip_existing_block(content)

    assert BEGIN_MARKER not in stripped
    assert END_MARKER not in stripped
    assert "important user notes" in stripped
    assert "# Notes" in stripped
    assert "tail text" in stripped
    assert "real block" not in stripped


def test_strip_existing_block_keeps_content_when_end_missing() -> None:
    """A BEGIN with no END anywhere drops only the marker, keeping all text."""

    content = f"# Notes\n{BEGIN_MARKER}\nuser wrote this\nmore text\n"

    stripped = ClaudeCodeMemoryPlugin._strip_existing_block(content)

    assert BEGIN_MARKER not in stripped
    assert "user wrote this" in stripped
    assert "more text" in stripped


def test_write_block_is_atomic_no_tmp_left_behind(tmp_path: Path) -> None:
    """The temp file used for the atomic replace must not survive the write."""

    target = tmp_path / ".claude" / "CLAUDE.md"
    plugin = ClaudeCodeMemoryPlugin()

    plugin._write_block(target, "<cao-memory>X</cao-memory>")

    assert target.exists()
    assert "<cao-memory>X</cao-memory>" in target.read_text(encoding="utf-8")
    leftovers = [p for p in target.parent.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []
