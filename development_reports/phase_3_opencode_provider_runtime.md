# Phase 3 — OpenCode Provider Runtime

## Summary

Implemented the `OpenCodeCliProvider` class, registered it in the provider manager, and added it to the workspace-access guard in the `launch` command. 43 unit tests were written covering all acceptance criteria at 96% line coverage.

---

## Files Created

| File | Purpose |
|------|---------|
| `src/cli_agent_orchestrator/providers/opencode_cli.py` | Full provider implementation (125 lines) |
| `test/providers/test_opencode_cli_unit.py` | 43 unit tests (96% line coverage) |

## Files Modified

| File | Change |
|------|--------|
| `src/cli_agent_orchestrator/providers/manager.py` | Import + `elif` branch for `opencode_cli` |
| `src/cli_agent_orchestrator/cli/commands/launch.py` | Added `"opencode_cli"` to `PROVIDERS_REQUIRING_WORKSPACE_ACCESS` |

---

## Acceptance Criteria Disposition

### AC-1 — `OpenCodeCliProvider` subclasses `BaseProvider` with all required methods
**Satisfied.** The class implements `initialize()`, `get_status()`, `extract_last_message_from_script()`, `get_idle_pattern_for_log()`, `exit_cli()`, and `cleanup()`. The `paste_enter_count` property returns `1`.

### AC-2 — `initialize()` sends the correct inline-env launch command and waits for IDLE/COMPLETED with 120s timeout
**Satisfied.** `_build_launch_command()` emits all seven stability env vars (`OPENCODE_CONFIG`, `OPENCODE_CONFIG_DIR`, `OPENCODE_DISABLE_AUTOUPDATE=1`, `OPENCODE_DISABLE_MOUSE=1`, `OPENCODE_DISABLE_TERMINAL_TITLE=1`, `OPENCODE_CLIENT=cao`, `TERM=xterm-256color`) followed by `opencode [--agent <name>] [--model <name>]` built with `shlex.join`. The `wait_until_status` call uses a 120-second timeout to cover first-run `npm install` cold starts. Verified output:
```
OPENCODE_CONFIG=... OPENCODE_DISABLE_AUTOUPDATE=1 ... opencode --agent developer
OPENCODE_CONFIG=... OPENCODE_DISABLE_AUTOUPDATE=1 ... opencode --agent code-reviewer --model anthropic/claude-sonnet-4-6
```

### AC-3 — `get_status()` correctly classifies all five states
**Satisfied.** Priority-order detection (WAITING_USER_ANSWER → PROCESSING → COMPLETED → IDLE → ERROR) with line-level position guard for stale `esc interrupt` alt-screen remnants.

Key design decisions:
- **Line-level position guard**: During active processing, `esc interrupt` and `ctrl+p commands` appear on the *same* footer line. A stale alt-screen snapshot puts them on separate lines. The guard checks whether any idle-footer or completion-marker line appears *after* the line containing `esc interrupt` — if so, `esc interrupt` is stale.
- **`esc_is_stale` flag**: When the position guard fires, this flag allows the IDLE branch to match even though `esc interrupt` text is still present in the buffer.
- **COMPLETED requires no trailing `▣`**: After completion, the new user input bar shows a partial `▣  Build · Big Pickle` (no duration), which would make the IDLE-post-completion state look like a new incomplete turn. The COMPLETED check confirms no `▣` token follows the last full completion marker.

### AC-4 — `extract_last_message_from_script()` correctly extracts agent text
**Satisfied.** Algorithm:
1. Strip ANSI codes.
2. Find last full `COMPLETION_MARKER_PATTERN` match (requires `·…·…Ns` duration suffix).
3. Search for last `┃  ` *before* the completion marker (unanchored — TUI lines have leading spaces).
4. Extract between `┃  ` end and completion marker start.
5. Skip user-message lines (still using `┃` indent).
6. Strip `Thinking:` preamble lines.
7. Dedent 5-space agent indent.
8. Clean control characters and trailing whitespace.

Raises `ValueError` with descriptive messages for missing completion marker or missing user message.

### AC-5 — Provider registered in `manager.py` and `launch.py`
**Satisfied.** `ProviderManager.create_provider()` routes `"opencode_cli"` to `OpenCodeCliProvider` passing `model=model`. `PROVIDERS_REQUIRING_WORKSPACE_ACCESS` includes `"opencode_cli"`.

### AC-6 — 90%+ test coverage
**Satisfied.** 96% line coverage on `opencode_cli.py` (5 uncovered lines are unreachable exception paths in string cleaning).

---

## Smoke Test

Server-based launch was not used (port 9889 reserved per user instruction). Provider dispatch was verified directly:

```python
from cli_agent_orchestrator.providers.manager import ProviderManager
pm = ProviderManager()
p = pm.create_provider('opencode_cli', 'test-terminal-1', 'cao-session', 'win-1', 'developer')
# → Provider type: OpenCodeCliProvider
# → launch command: OPENCODE_CONFIG=... opencode --agent developer
```

```python
p2 = pm.create_provider('opencode_cli', 't2', 's', 'w', 'code-reviewer', model='anthropic/claude-sonnet-4-6')
# → launch command: ... opencode --agent code-reviewer --model anthropic/claude-sonnet-4-6
```

The `cao install developer --provider opencode_cli` CLI command was also verified in a prior session:
```
✓ Agent 'developer' installed successfully
✓ opencode_cli agent: /home/bajablast69/.aws/opencode_cli/agents/developer.md
```

---

## Design Decisions and Trade-offs

- **`--model` only via launch flag, never frontmatter** (§3.1 exception): OpenCode frontmatter does not support a `model:` key that overrides the active provider; passing it via `--model` at the CLI level is the correct path. The `model` parameter flows from `ProviderManager.create_provider()` → `OpenCodeCliProvider.__init__()` → `_build_launch_command()`.
- **Unanchored `┃  ` search in `extract_last_message_from_script`**: The module-level `USER_MESSAGE_PATTERN = r"^┃\s{2}"` uses `^` anchoring suitable for full-line matching, but TUI output lines have variable leading spaces (e.g. `  ┃  say hello`). The extraction function uses `r"┃\s{2}"` without anchoring to handle this.
- **Completion-marker-first extraction order**: Finding the completion marker first and searching backwards for `┃  ` avoids the bottom input-box trap — the last `┃  ` in a completed screen is the new input-box agent/model header which appears *after* the completion marker. Restricting the `┃  ` search to `clean[:last_completion.start()]` cleanly avoids it.
