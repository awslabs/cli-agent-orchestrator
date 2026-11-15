"""Provider memory files are opt-in and cleaned up safely by default."""

import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest

from cli_agent_orchestrator.plugins import PostCreateTerminalEvent
from cli_agent_orchestrator.plugins.builtin import (
    claude_code_memory,
    codex_memory,
    kiro_cli_memory,
)


@dataclass(frozen=True)
class ProviderCase:
    provider: str
    module: ModuleType
    plugin_type: type
    relative_target: Path


CASES = (
    ProviderCase(
        "claude_code",
        claude_code_memory,
        claude_code_memory.ClaudeCodeMemoryPlugin,
        Path(".claude/CLAUDE.md"),
    ),
    ProviderCase("codex", codex_memory, codex_memory.CodexMemoryPlugin, Path("AGENTS.md")),
    ProviderCase(
        "kiro_cli",
        kiro_cli_memory,
        kiro_cli_memory.KiroCliMemoryPlugin,
        Path(".kiro/steering/cao-memory.md"),
    ),
)


def _event(case: ProviderCase) -> PostCreateTerminalEvent:
    return PostCreateTerminalEvent(
        terminal_id="t1",
        agent_name="developer",
        provider=case.provider,
        session_id="cao-test-session",
    )


def _install_cwd(monkeypatch: pytest.MonkeyPatch, case: ProviderCase, cwd: Path) -> None:
    monkeypatch.setattr(
        case.module,
        "get_terminal_metadata",
        lambda _terminal_id: {
            "tmux_session": "cao-test-session",
            "tmux_window": "developer-abcd",
        },
    )
    monkeypatch.setattr(
        case.module,
        "get_backend",
        lambda: type(
            "FakeBackend",
            (),
            {"get_pane_working_directory": lambda self, _session, _window: str(cwd)},
        )(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.provider)
async def test_default_does_not_create_project_file_or_fetch_memory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: ProviderCase
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    _install_cwd(monkeypatch, case, tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        case.module,
        "MemoryService",
        lambda: type(
            "MemoryService",
            (),
            {
                "get_memory_context_for_terminal": lambda self, terminal_id: calls.append(
                    terminal_id
                )
                or "<cao-memory>memory</cao-memory>"
            },
        )(),
    )
    before = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tmp_path, check=True, text=True, capture_output=True
    ).stdout

    await case.plugin_type().on_post_create_terminal(_event(case))

    after = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tmp_path, check=True, text=True, capture_output=True
    ).stdout
    assert calls == []
    assert not (tmp_path / case.relative_target).exists()
    assert after == before


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.provider)
async def test_default_removes_only_cao_block_and_deletes_cao_only_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: ProviderCase
) -> None:
    target = tmp_path / case.relative_target
    target.parent.mkdir(parents=True, exist_ok=True)
    block = "<!-- cao-memory:begin -->\n<cao-memory>old</cao-memory>\n<!-- cao-memory:end -->"
    target.write_text(f"before\n{block}\nafter\n", encoding="utf-8")
    _install_cwd(monkeypatch, case, tmp_path)

    await case.plugin_type().on_post_create_terminal(_event(case))

    assert target.read_bytes() == b"before\nafter\n"
    target.write_text(block, encoding="utf-8")
    await case.plugin_type().on_post_create_terminal(_event(case))
    assert not target.exists()
    await case.plugin_type().on_post_create_terminal(_event(case))
    assert not target.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.provider)
async def test_write_then_default_cleanup_restores_user_file_byte_for_byte(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: ProviderCase
) -> None:
    target = tmp_path / case.relative_target
    target.parent.mkdir(parents=True, exist_ok=True)
    original = b"user text\n"
    target.write_bytes(original)
    _install_cwd(monkeypatch, case, tmp_path)

    plugin = case.plugin_type()
    plugin._write_block(target, "<cao-memory>new</cao-memory>")
    await plugin.on_post_create_terminal(_event(case))

    assert target.read_bytes() == original


@pytest.mark.asyncio
async def test_default_removes_recognizable_kiro_file_but_not_user_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    case = CASES[2]
    target = tmp_path / case.relative_target
    target.parent.mkdir(parents=True, exist_ok=True)
    _install_cwd(monkeypatch, case, tmp_path)
    target.write_text('<cao-memory generated="2000-01-01T00:00:00Z">old</cao-memory>\n')

    await case.plugin_type().on_post_create_terminal(_event(case))

    assert not target.exists()
    await case.plugin_type().on_post_create_terminal(_event(case))
    assert not target.exists()
    target.write_text("user steering\n", encoding="utf-8")
    await case.plugin_type().on_post_create_terminal(_event(case))
    assert target.read_text(encoding="utf-8") == "user steering\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.provider)
async def test_default_leaves_symlink_target_untouched_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: ProviderCase
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = outside / "target.md"
    destination.write_text("outside\n", encoding="utf-8")
    target = tmp_path / case.relative_target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(destination)
    _install_cwd(monkeypatch, case, tmp_path)

    plugin = case.plugin_type()
    await plugin.on_post_create_terminal(_event(case))
    first_mtime = target.lstat().st_mtime_ns
    await plugin.on_post_create_terminal(_event(case))

    assert target.is_symlink()
    assert target.lstat().st_mtime_ns == first_mtime
    assert destination.read_text(encoding="utf-8") == "outside\n"
