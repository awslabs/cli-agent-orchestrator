"""Contract checks for memory injection documentation."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MEMORY_DOC = REPO_ROOT / "docs" / "memory.md"


def test_context_injection_discloses_process_local_restart_limit():
    context_injection = MEMORY_DOC.read_text(encoding="utf-8").split(
        "## Context Injection", maxsplit=1
    )[1]

    normalized = context_injection.lower().replace("`", "")
    assert "exactly-once tracking is in-process" in normalized
    assert "cao-server restarts" in normalized
