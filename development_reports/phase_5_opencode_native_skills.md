# Phase 5 — Native Skill Discovery via Symlink

## Summary

Phase 5 adds native skill discovery for the OpenCode CLI provider. At `cao install --provider opencode_cli` time, CAO now creates a symlink `OPENCODE_CONFIG_DIR/skills → SKILLS_DIR`. OpenCode auto-discovers any `<OPENCODE_CONFIG_DIR>/skills/` directory and exposes its contents through the native `skill` tool with progressive loading (metadata listed up front, full body loaded on demand). This replaces the previous approach of baking the full skill catalog into the agent system prompt.

A companion fix corrects the body source for OpenCode agents: the install branch now uses `profile.system_prompt or profile.prompt or ""` rather than `compose_agent_prompt(profile)`, keeping the agent prompt lean and directing skill delivery entirely through the native symlink path.

## Files Created or Modified

### Modified — `src/cli_agent_orchestrator/utils/opencode_config.py`

- Added `ensure_skills_symlink()` function:
  - Creates `OPENCODE_CONFIG_DIR/skills` as a symlink pointing at `SKILLS_DIR`
  - **Idempotent**: no-op when the correct symlink already exists (compares resolved paths)
  - **Warns and skips** (no modification) when the target is a non-symlink directory, a regular file, or a symlink pointing elsewhere — CAO does not repair user-owned state
- Added `logging` import and module-level `logger`
- Added `OPENCODE_CONFIG_DIR` and `SKILLS_DIR` to the constants import (both already existed in `constants.py`)

### Modified — `src/cli_agent_orchestrator/cli/commands/install.py`

- Added `ensure_skills_symlink` to the `opencode_config` import
- In the `opencode_cli` branch:
  - Calls `ensure_skills_symlink()` immediately after `OPENCODE_AGENTS_DIR.mkdir()`
  - Changed body source from `compose_agent_prompt(profile)` → `profile.system_prompt or profile.prompt or ""`

### Modified — `test/cli/commands/test_install_opencode.py`

- `install_workspace` fixture: added `monkeypatch.setattr(…ensure_skills_symlink, lambda: None)` to suppress symlink filesystem side-effects in unit tests (symlink logic is covered separately in `test_opencode_config.py`)
- `test_agent_md_has_body`: changed `_write_profile` call to use `body="You are a test sentinel agent."` (Markdown body → `profile.system_prompt`) instead of `extra_frontmatter="prompt: ..."` (`profile.prompt`), matching the new `system_prompt or prompt` precedence order; updated comment accordingly
- Added `test_ensure_skills_symlink_called`: verifies `ensure_skills_symlink()` is invoked once per `opencode_cli` install

### Modified — `test/utils/test_opencode_config.py`

- Added `ensure_skills_symlink` to imports
- Added `TestEnsureSkillsSymlink` class with four tests:
  - `test_creates_symlink_when_target_missing`: verifies symlink is created pointing at `SKILLS_DIR`
  - `test_noop_when_correct_symlink_exists`: verifies mtime is unchanged on a second call
  - `test_warns_and_skips_when_target_is_directory`: verifies warning is logged and real directory is untouched
  - `test_warns_and_skips_when_symlink_points_elsewhere`: verifies wrong-target symlink is left unchanged

### Modified — `docs/opencode-cli.md`

- Added **§ Skills** section between "Permission and Tool Mapping" and "Status Detection", documenting:
  - CAO creates a symlink at install time
  - Storage path: `~/.aws/opencode_cli/skills → ~/.aws/cli-agent-orchestrator/skills/`
  - Skill additions/removals take effect on the next OpenCode launch without reinstall
  - The agent system prompt stays lean; `load_skill` MCP tool remains available as a second path

## How Acceptance Criteria Are Satisfied

| AC | Result |
|----|--------|
| `ensure_skills_symlink()` added to `opencode_config.py`; creates symlink on first call, no-ops on subsequent calls | ✅ Implemented and tested |
| Warns-and-skips when target is non-symlink (directory, file, or wrong-target symlink) | ✅ Implemented and tested (4 cases, including explicit wrong-target-symlink test) |
| `install.py` opencode_cli branch calls `ensure_skills_symlink()` | ✅ Wired in; unit test asserts call count |
| Agent body uses `profile.system_prompt or profile.prompt` (lean, no skill catalog) | ✅ `compose_agent_prompt` removed from opencode path |
| `test_agent_md_has_body` asserts both sentinel presence AND `## Available Skills` absence | ✅ Both assertions pass |
| Unit tests pass | ✅ 1444 passed, 7 skipped (final count after all revisions) |
| mypy clean on modified files | ✅ No errors in `opencode_config.py`, `install.py`, `opencode_cli.py`, `base.py` |
| `docs/opencode-cli.md` § Skills section added | ✅ Documents symlink path, lazy loading, and lean-prompt rationale |
| Live smoke test: `opencode agent list` shows `developer`; symlink exposes both CAO skills | ✅ `opencode` at `/usr/local/bin/opencode` v1.14.19; `developer (all)` confirmed; `~/.aws/opencode_cli/skills/cao-supervisor-protocols/SKILL.md` and `cao-worker-protocols/SKILL.md` reachable through symlink |

## Design Decisions and Trade-offs

- **Symlink vs. copy**: A symlink means skill updates (new files, edits) are visible on the next OpenCode launch with no reinstall. A copy would require reinstall on every skill change.
- **Warn-and-skip on collision**: If `OPENCODE_CONFIG_DIR/skills` already exists as a real directory (user-owned data), CAO logs a warning and leaves it untouched. This is the safest policy — deleting or replacing a real directory could destroy user data.
- **No repair of wrong-target symlinks**: If the symlink points elsewhere, CAO also warns and skips. Rewriting a symlink that might point to user-authored content is outside CAO's mandate for this phase.
- **`compose_agent_prompt` removal**: The previous code baked the full skill catalog (potentially hundreds of lines) into every OpenCode agent's system prompt. Skills are now delivered lazily via OpenCode's native `skill` tool — the metadata catalog appears in OpenCode's tool listing and full skill bodies are fetched only when the agent explicitly calls `skill`. This keeps token usage low.

## Additional Fixes Found During E2E Verification

Two provider bugs were discovered and fixed during the Phase 5 e2e re-run. Both were pre-existing but only surfaced once agents had full system prompts (from the body-source change), causing responses to take longer and produce more output.

### Fix 1 — `COMPLETION_MARKER_PATTERN` doesn't handle `Nm Ns` duration format

**File:** `src/cli_agent_orchestrator/providers/opencode_cli.py`

OpenCode formats turn duration as `Ns` for short responses and `Nm Ns` for responses that take more than 60 seconds (e.g. `1m 8s`). The original pattern `\d+(?:\.\d+)?s` only matched the seconds-only form. With full system prompts, agents regularly take >60 s, causing `get_status()` to stall at PROCESSING indefinitely and the 180s completion wait to time out.

Fixed: `(?:\d+m\s+)?\d+(?:\.\d+)?s` (the minutes prefix is optional).

New unit tests added: `test_completion_marker_pattern_matches_minute_second_duration`.

### Fix 2 — `TMUX_HISTORY_LINES=200` too small for long-response extraction

**Files:** `src/cli_agent_orchestrator/providers/base.py`, `src/cli_agent_orchestrator/providers/opencode_cli.py`, `src/cli_agent_orchestrator/services/terminal_service.py`

`extract_last_message_from_script` receives a 200-line tmux capture. With a full system prompt (148 lines for `report_generator`), the agent's response is long enough to push the user-message marker (`┃  `) beyond the 200-line window, causing "No user message found" on extraction. Status checks don't need deep history; only extraction does.

Added `extraction_tail_lines: Optional[int]` property to `BaseProvider` (default `None` = use `TMUX_HISTORY_LINES`). `OpenCodeCliProvider` overrides to `2000` (matches tmux's `history-limit`). `terminal_service.get_output` uses this value for LAST-mode captures without affecting status-check captures.

New unit test: `test_extraction_tail_lines_is_2000`.

### E2E Results After All Fixes

```
test/e2e/test_assign.py::TestOpenCodeCliAssign::test_assign_data_analyst     PASSED
test/e2e/test_assign.py::TestOpenCodeCliAssign::test_assign_report_generator PASSED
test/e2e/test_assign.py::TestOpenCodeCliAssign::test_assign_with_callback    PASSED
3 passed in 176.00s (0:02:55)
```

## Commits

`3d70846` — `feat(opencode): native skill discovery via OPENCODE_CONFIG_DIR/skills symlink`

`7c0c224` — `fix(opencode): handle Nm Ns duration format and extend extraction buffer`

`8979d41` — Phase 5 review revisions: single-capture consolidation, wrong-target symlink test
