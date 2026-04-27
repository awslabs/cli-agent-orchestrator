"""Tests for the Kiro CLI memory-injection plugin."""

from pathlib import Path

import pytest
from cao_memory_kiro_cli.plugin import (
    MEMORY_FILENAME,
    STEERING_SUBDIR,
    KiroCliMemoryPlugin,
)

from cli_agent_orchestrator.plugins import PostCreateTerminalEvent


def _event(provider: str = "kiro_cli", terminal_id: str = "t1") -> PostCreateTerminalEvent:
    return PostCreateTerminalEvent(
        terminal_id=terminal_id,
        agent_name="developer",
        provider=provider,
        session_id="cao-test-session",
    )


def _install_metadata_and_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "cao_memory_kiro_cli.plugin.get_terminal_metadata",
        lambda terminal_id: {
            "tmux_session": "cao-test-session",
            "tmux_window": "developer-abcd",
            "id": terminal_id,
        },
    )
    monkeypatch.setattr(
        "cao_memory_kiro_cli.plugin.tmux_client.get_pane_working_directory",
        lambda session, window: str(tmp_path),
    )


@pytest.mark.asyncio
async def test_ignores_non_kiro_cli_providers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The hook must do nothing when the event is for another provider."""

    called: list[str] = []
    monkeypatch.setattr(
        "cao_memory_kiro_cli.plugin.get_terminal_metadata",
        lambda terminal_id: called.append(terminal_id) or None,
    )

    plugin = KiroCliMemoryPlugin()
    await plugin.on_post_create_terminal(_event(provider="claude_code"))

    assert called == [], "provider filter must short-circuit before any work"


@pytest.mark.asyncio
async def test_writes_steering_file_on_post_create_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On a kiro_cli terminal, the plugin writes .kiro/steering/cao-memory.md."""

    _install_metadata_and_cwd(monkeypatch, tmp_path)

    class FakeMemoryService:
        def get_memory_context_for_terminal(self, terminal_id: str) -> str:
            return "<cao-memory>\n## Context\n- stan prefers pytest\n</cao-memory>"

    monkeypatch.setattr(
        "cao_memory_kiro_cli.plugin.MemoryService",
        lambda: FakeMemoryService(),
    )

    plugin = KiroCliMemoryPlugin()
    await plugin.on_post_create_terminal(_event())

    target = tmp_path / STEERING_SUBDIR / MEMORY_FILENAME
    assert target.exists()
    content = target.read_text(encoding="utf-8")
    assert "<cao-memory>" in content
    assert "stan prefers pytest" in content


@pytest.mark.asyncio
async def test_overwrites_previous_memory_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A second invocation should replace the memory file contents cleanly."""

    target_dir = tmp_path / STEERING_SUBDIR
    target_dir.mkdir(parents=True)
    target = target_dir / MEMORY_FILENAME
    target.write_text("stale memory from a prior run\n", encoding="utf-8")

    _install_metadata_and_cwd(monkeypatch, tmp_path)

    class FakeMemoryService:
        def get_memory_context_for_terminal(self, terminal_id: str) -> str:
            return "<cao-memory>fresh</cao-memory>"

    monkeypatch.setattr(
        "cao_memory_kiro_cli.plugin.MemoryService",
        lambda: FakeMemoryService(),
    )

    plugin = KiroCliMemoryPlugin()
    await plugin.on_post_create_terminal(_event())

    content = target.read_text(encoding="utf-8")
    assert "fresh" in content
    assert "stale" not in content


@pytest.mark.asyncio
async def test_does_not_touch_agent_identity_steering_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """U7's agent-identity.md is owned by terminal_service; plugin must leave it alone."""

    steering = tmp_path / STEERING_SUBDIR
    steering.mkdir(parents=True)
    identity = steering / "agent-identity.md"
    identity.write_text("you are the developer agent\n", encoding="utf-8")

    _install_metadata_and_cwd(monkeypatch, tmp_path)

    class FakeMemoryService:
        def get_memory_context_for_terminal(self, terminal_id: str) -> str:
            return "<cao-memory>hi</cao-memory>"

    monkeypatch.setattr(
        "cao_memory_kiro_cli.plugin.MemoryService",
        lambda: FakeMemoryService(),
    )

    plugin = KiroCliMemoryPlugin()
    await plugin.on_post_create_terminal(_event())

    assert identity.read_text(encoding="utf-8") == "you are the developer agent\n"
    assert (steering / MEMORY_FILENAME).exists()


@pytest.mark.asyncio
async def test_skips_write_when_memory_context_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Empty memory context must NOT create the steering file."""

    _install_metadata_and_cwd(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "cao_memory_kiro_cli.plugin.MemoryService",
        lambda: type("F", (), {"get_memory_context_for_terminal": lambda self, t: ""})(),
    )

    plugin = KiroCliMemoryPlugin()
    await plugin.on_post_create_terminal(_event())

    assert not (tmp_path / STEERING_SUBDIR / MEMORY_FILENAME).exists()


@pytest.mark.asyncio
async def test_memory_fetch_failure_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Memory-service exceptions must be caught and logged."""

    _install_metadata_and_cwd(monkeypatch, tmp_path)

    class ExplodingMemoryService:
        def get_memory_context_for_terminal(self, terminal_id: str) -> str:
            raise RuntimeError("db on fire")

    monkeypatch.setattr(
        "cao_memory_kiro_cli.plugin.MemoryService",
        lambda: ExplodingMemoryService(),
    )

    plugin = KiroCliMemoryPlugin()
    with caplog.at_level("WARNING", logger="cao_memory_kiro_cli.plugin"):
        await plugin.on_post_create_terminal(_event())

    assert not (tmp_path / STEERING_SUBDIR).exists()
    assert "memory fetch failed" in caplog.text


@pytest.mark.asyncio
async def test_missing_terminal_metadata_short_circuits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No metadata → no write, no crash, no MemoryService call."""

    monkeypatch.setattr(
        "cao_memory_kiro_cli.plugin.get_terminal_metadata",
        lambda terminal_id: None,
    )

    def _boom(*args, **kwargs):
        raise AssertionError("MemoryService must not be constructed when metadata missing")

    monkeypatch.setattr("cao_memory_kiro_cli.plugin.MemoryService", _boom)

    plugin = KiroCliMemoryPlugin()
    await plugin.on_post_create_terminal(_event())

    assert not (tmp_path / STEERING_SUBDIR).exists()


@pytest.mark.asyncio
async def test_path_containment_guard_rejects_symlink_escape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Escape via .kiro symlinked to a sibling dir must be refused."""

    real_cwd = tmp_path / "inside"
    sibling = tmp_path / "outside"
    real_cwd.mkdir()
    sibling.mkdir()
    (real_cwd / ".kiro").symlink_to(sibling, target_is_directory=True)

    monkeypatch.setattr(
        "cao_memory_kiro_cli.plugin.get_terminal_metadata",
        lambda terminal_id: {
            "tmux_session": "cao-test-session",
            "tmux_window": "developer-abcd",
            "id": terminal_id,
        },
    )
    monkeypatch.setattr(
        "cao_memory_kiro_cli.plugin.tmux_client.get_pane_working_directory",
        lambda session, window: str(real_cwd),
    )

    class FakeMemoryService:
        def get_memory_context_for_terminal(self, terminal_id: str) -> str:
            return "<cao-memory>NEW</cao-memory>"

    monkeypatch.setattr(
        "cao_memory_kiro_cli.plugin.MemoryService",
        lambda: FakeMemoryService(),
    )

    plugin = KiroCliMemoryPlugin()
    await plugin.on_post_create_terminal(_event())

    assert not (sibling / "steering" / MEMORY_FILENAME).exists()
    assert not (real_cwd / ".kiro" / "steering" / MEMORY_FILENAME).exists()
