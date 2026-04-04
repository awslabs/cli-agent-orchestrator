# Lessons Learned from Provider Development

Hard-won lessons from building and maintaining 7 CAO providers. Read this before implementing a new provider.

## 1. Stale Buffer Matching (Critical)

**Problem:** Matching processing patterns (spinner characters like `✽ Cooking…`) against the full tmux history buffer. Old spinner lines persist in the buffer even after the agent finishes. Since PROCESSING is typically checked before COMPLETED in `get_status()`, the terminal gets stuck returning PROCESSING forever.

**Fix:** Either:
- Check COMPLETED before PROCESSING — if both the idle prompt and response marker are present at the bottom of the buffer, the agent is done regardless of historical spinners
- Only match processing patterns against the last N lines (use `tail_lines` parameter)
- Use a latching mechanism: once you detect a response marker, don't go back to PROCESSING

**Example:** Claude Code's `PROCESSING_PATTERN = r"[✶✢✽✻✳].*…"` matched against the full 200-line buffer caused a flaky e2e test (`test_reviewer_cannot_write`) to timeout at 180s.

## 2. Double Enter After Paste

**Problem:** After tmux bracketed paste (`paste-buffer -p`), some TUIs enter multi-line mode. The first Enter adds a newline; the second Enter on the empty line triggers submission.

**Fix:** Override the `paste_enter_count` property in your provider:
```python
@property
def paste_enter_count(self) -> int:
    return 2  # Default. Override to 1 if single Enter submits.
```

## 3. New TUI Format Breaking Detection

**Problem:** CLI tools frequently update their TUI. Kiro CLI changed from `[agent] >` to `agent · model · ◔ N%` with `ask a question, or describe a task` as the idle indicator. The old regex stopped matching.

**Fix:** 
- Build detection for multiple prompt formats (old and new)
- Use fallback patterns: check the primary pattern first, then fall back to alternatives
- Consider adding a `--legacy-ui` flag if the CLI supports it
- Example from Kiro CLI:
```python
has_idle_prompt = re.search(self._idle_prompt_pattern, clean_output)
has_new_tui_idle = re.search(NEW_TUI_IDLE_PATTERN, clean_output)
if not has_idle_prompt and not has_new_tui_idle:
    return TerminalStatus.PROCESSING
```

## 4. Exception Wrapping in load_agent_profile()

**Problem:** `load_agent_profile()` was wrapping `FileNotFoundError` as `RuntimeError`. Callers like `resolve_provider()` only caught `FileNotFoundError`, so JSON-only agent profiles (AIM-installed) caused assign() to fail.

**Fix:** Re-raise `FileNotFoundError` directly, don't wrap it:
```python
except FileNotFoundError:
    raise  # Let callers handle this specifically
except Exception as e:
    raise RuntimeError(f"Failed to load profile: {e}")
```

## 5. ANSI Codes Everywhere

**Problem:** Terminal output is full of ANSI escape sequences (colors, cursor movement, formatting). Pattern matching fails if you don't strip them first.

**Fix:** Always strip ANSI codes before any pattern matching:
```python
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"
clean_output = re.sub(ANSI_CODE_PATTERN, "", output)
```

## 6. TOOL_MAPPING Is Only for Translation

**Problem:** Adding a `TOOL_MAPPING` entry for providers that accept CAO vocabulary directly (like Kiro CLI). This is unnecessary and was causing the launch prompt to show misleading `Blocked: (none)`.

**Fix:** Only add `TOOL_MAPPING` entries for providers whose native tool names differ from CAO's vocabulary:
- **Need TOOL_MAPPING:** Claude Code (`execute_bash` → `Bash`), Copilot CLI (`execute_bash` → `shell`), Gemini CLI
- **Don't need TOOL_MAPPING:** Kiro CLI, Q CLI (accept `allowedTools` in agent JSON), Kimi CLI, Codex (system prompt enforcement)

## 7. Confirmation Prompt Blocks Automation

**Problem:** The `cao launch` confirmation prompt (`Proceed? [Y/n]`) blocks automated flows, scripts, and agent-to-agent launches. `--yolo` skips it but also removes all restrictions.

**Fix:** `--auto-approve` flag skips the prompt while keeping restrictions enforced. When building a new provider, ensure your e2e tests work with the existing launch flow (they go through the API, not CLI, so this shouldn't affect them — but be aware of it).

## 8. tmux Session Cleanup in Tests

**Problem:** E2e tests that create multiple terminals (send_message needs 2, supervisor tests need 2-3) can leave stale tmux sessions. Subsequent tests may find terminals in `error` state.

**Fix:** Always clean up in a `finally` block. If tests are flaky with `status: error`, it's likely leftover sessions from previous test runs.

## 9. Provider-Specific MCP Configuration

**Problem:** Claude Code doesn't automatically forward parent shell environment variables to MCP subprocesses.

**Fix:** When building the MCP config, explicitly inject `CAO_TERMINAL_ID` into the env:
```python
env = mcp_config[server_name].get("env", {})
if "CAO_TERMINAL_ID" not in env:
    env["CAO_TERMINAL_ID"] = self.terminal_id
    mcp_config[server_name]["env"] = env
```

## 10. Nested Session Detection

**Problem:** When cao-server runs inside a Claude Code session, `CLAUDE*` env vars leak into spawned tmux panes. Claude Code detects these and refuses to start ("nested session").

**Fix:** Unset `CLAUDE*` env vars before launching (except auth-related ones like `CLAUDE_CODE_USE_BEDROCK`):
```python
unset_cmd = (
    "unset $(env | sed -n 's/^\\(CLAUDE[A-Z_]*\\)=.*/\\1/p'"
    " | grep -v -E 'CLAUDE_CODE_USE_(BEDROCK|VERTEX|FOUNDRY)'"
    ") 2>/dev/null"
)
```
