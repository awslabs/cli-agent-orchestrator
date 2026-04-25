"""SC-1 — PreCompact hook must not cancel Claude Code compaction.

Claude Code's PreCompact hook contract: returning {"decision":"block"} cancels
compaction. The previous hook body emitted exactly that string, leaving users
stuck at the context limit. This test locks the behavior down: the script must
never output a value that cancels compaction.

The save-before-compaction intent is covered non-blockingly by the Phase 2 U8
in-process flush (see test/utils/test_pre_compaction_flush.py). This test only
guards the shell-hook output shape.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

HOOK_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "cli_agent_orchestrator"
    / "hooks"
    / "cao_precompact_hook.sh"
)


def _run_hook() -> subprocess.CompletedProcess[str]:
    assert HOOK_PATH.exists(), f"Hook script missing: {HOOK_PATH}"
    # Script is shipped with +x in installed location; invoke via bash so the
    # test does not depend on file-mode state in the source tree.
    return subprocess.run(
        ["bash", str(HOOK_PATH)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


def test_precompact_hook_exits_zero() -> None:
    """Non-zero exit on a PreCompact hook would also block compaction."""
    result = _run_hook()
    assert result.returncode == 0, (
        f"PreCompact hook returned non-zero exit {result.returncode}, "
        f"which cancels compaction. stderr={result.stderr!r}"
    )


def test_precompact_hook_does_not_emit_block_decision() -> None:
    """The hook output must not contain `"decision":"block"` in any form."""
    result = _run_hook()
    stdout = result.stdout

    # Textual guard — catches both JSON and any formatting variation.
    normalized = "".join(stdout.split())
    assert '"decision":"block"' not in normalized, (
        f"PreCompact hook emitted a block decision, which cancels compaction. "
        f"stdout={stdout!r}"
    )


def test_precompact_hook_emits_parseable_json_or_empty() -> None:
    """Output must be valid JSON so Claude Code can parse it without error."""
    result = _run_hook()
    stdout = result.stdout.strip()
    if not stdout:
        return  # empty body is also a valid no-op
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as e:
        pytest.fail(f"PreCompact hook stdout is not valid JSON: {stdout!r} ({e})")

    # Defense-in-depth: if a `decision` field is ever present, it must not be "block".
    if isinstance(payload, dict) and "decision" in payload:
        assert payload["decision"] != "block", (
            f"PreCompact hook emitted decision=block: {payload!r}"
        )
