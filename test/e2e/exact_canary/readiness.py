"""Provider readiness predicates for exact-restore canaries."""

from __future__ import annotations

import re

from cli_agent_orchestrator.providers import codex as codex_provider
from cli_agent_orchestrator.utils.text import strip_terminal_escapes


def codex_composer_ready(transcript: str) -> bool:
    """Match Codex idle without mistaking its startup footer for readiness."""
    clean = strip_terminal_escapes(transcript)
    tail = "\n".join(clean.splitlines()[-25:])
    bottom_lines = clean.strip().splitlines()[-codex_provider.IDLE_PROMPT_TAIL_LINES :]
    composer_visible = any(
        re.match(
            rf"\s*{codex_provider.IDLE_PROMPT_PATTERN}",
            line,
            re.IGNORECASE,
        )
        for line in bottom_lines
    )
    footer_visible = any(
        re.search(codex_provider.TUI_FOOTER_PATTERN, line) for line in bottom_lines
    )
    progress_visible = re.search(codex_provider.TUI_PROGRESS_PATTERN, tail, re.MULTILINE)
    return bool(
        re.search(codex_provider.CODEX_WELCOME_PATTERN, clean)
        and composer_visible
        and footer_visible
        and not progress_visible
    )
