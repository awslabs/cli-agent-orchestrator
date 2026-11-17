"""Contract checks for memory injection documentation."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MEMORY_DOC = REPO_ROOT / "docs" / "memory.md"
CONFIGURATION_DOC = REPO_ROOT / "docs" / "configuration.md"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"


def test_context_injection_discloses_process_local_restart_limit():
    context_injection = MEMORY_DOC.read_text(encoding="utf-8").split(
        "## Context Injection", maxsplit=1
    )[1]

    normalized = context_injection.lower().replace("`", "")
    assert "exactly-once tracking is in-process" in normalized
    assert "cao-server restarts" in normalized


def test_start_prompt_provider_and_kill_switch_docs_match_runtime():
    from cli_agent_orchestrator.services.terminal_service import (
        RUNTIME_SKILL_PROMPT_PROVIDERS,
    )

    expected_provider_names = {
        "claude_code": "Claude Code",
        "codex": "Codex",
        "kimi_cli": "Kimi CLI",
        "antigravity_cli": "Antigravity CLI",
        "omp": "OMP",
        "grok_cli": "Grok CLI",
        "mcode": "MiniMax Code",
    }
    assert set(expected_provider_names) == RUNTIME_SKILL_PROMPT_PROVIDERS

    memory_text = MEMORY_DOC.read_text(encoding="utf-8")
    changelog_text = (
        CHANGELOG.read_text(encoding="utf-8")
        .split("### Changed", maxsplit=1)[1]
        .split("- **Memory injection no longer", maxsplit=1)[0]
    )
    for display_name in expected_provider_names.values():
        assert display_name in memory_text
        assert display_name in changelog_text

    configuration_text = CONFIGURATION_DOC.read_text(encoding="utf-8")
    assert "first-message or provider start-prompt route" in configuration_text

    normalized_memory = " ".join(memory_text.split())
    kill_switch = normalized_memory.split(
        "The operator's local `memory.enabled` / `CAO_MEMORY_ENABLED` switch", maxsplit=1
    )[1]
    assert "first-message and provider start-prompt context injection" in kill_switch

    context_injection = normalized_memory.split("## Context Injection", maxsplit=1)[1]
    assert "workflow steps stay on the frozen first-message route" in context_injection
