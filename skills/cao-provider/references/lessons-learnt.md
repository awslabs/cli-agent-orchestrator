# Lessons Learnt from Provider Development

Hard-won lessons from building and maintaining 7 CAO providers. Read this before implementing a new provider.

**Essential reading:** Review `docs/tool-restrictions.md` for the full tool restriction architecture (two-layer system, resolution hierarchy, per-provider enforcement, cross-provider inheritance, known limitations). That doc is the source of truth for how role, allowedTools, and enforcement work end-to-end.

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

## 11. Role → allowedTools Resolution Chain

**Problem:** New providers skip tool restriction wiring because the resolution path is non-obvious. The `role` field in agent profiles is not just a label — it drives the default `allowedTools` bundle, which in turn determines what native tools get blocked.

**Resolution chain:**
1. Explicit `allowedTools` in profile or `--allowed-tools` CLI flag (highest priority)
2. Role-based defaults from `constants.py` (`supervisor` → `["@cao-mcp-server", "fs_read", "fs_list"]`, `developer` → `["@builtin", "fs_*", "execute_bash", "@cao-mcp-server"]`, `reviewer` → `["@builtin", "fs_read", "fs_list", "@cao-mcp-server"]`)
3. Custom roles from `settings.json` (user-defined bundles)
4. Fallback: unrestricted `["*"]` (backward compatible)
5. MCP server names appended as `@server_name`

**Fix:** When building your provider's `_build_command()`, always check `self._allowed_tools` and apply restrictions. The resolution is already done by the time your provider receives the list — you just need to enforce it via CLI flags, agent JSON, or system prompt.

## 12. Child allowedTools=["*"] Must Override Parent Restrictions

**Problem:** When a supervisor (restricted to `@cao-mcp-server,fs_read,fs_list`) assigns work to a developer (unrestricted `["*"]`), the developer was inheriting the supervisor's restrictions. This happened because `_resolve_child_allowed_tools()` treated `["*"]` the same as `None` (no opinion).

**Fix:** Explicit `["*"]` means "I am unrestricted" and must NOT be overridden by parent restrictions. The resolution logic in `mcp_server/server.py` is:
- Parent unrestricted or None → use child's tools
- Child is None (no opinion) → inherit parent's restrictions
- Child is `["*"]` (explicit unrestricted) → honor it, return None (unrestricted)
- Both have explicit lists → child gets its own tools

When writing handoff/assign logic, never flatten `["*"]` to `None` before passing to `_resolve_child_allowed_tools()`.

## 13. Three Enforcement Approaches — Know Which One Your Provider Needs

**Problem:** Implementing tool restrictions the wrong way for your provider type. A provider that accepts CAO vocabulary in agent JSON doesn't need TOOL_MAPPING, and a provider with no native restriction mechanism can only use soft enforcement (system prompt).

**The three approaches:**
- **Hard via CLI flags** (Claude Code `--disallowedTools`, Copilot CLI `--deny-tool`): Add provider to `TOOL_MAPPING` in `tool_mapping.py` to translate CAO vocabulary → native tool names. `get_disallowed_tools()` computes which native tools to block.
- **Hard via agent JSON** (Kiro CLI, Q CLI): The CLI reads `allowedTools` from the agent profile at install time. No `TOOL_MAPPING` entry needed — pass CAO vocabulary directly.
- **Soft via system prompt** (Kimi CLI, Codex): No native restriction mechanism. CAO prepends `SECURITY_PROMPT` from `constants.py` to the system prompt. This is advisory only — the CLI can still use any tool.

**Limitation:** Soft enforcement is not a security boundary. If a provider doesn't support native tool blocking, document this in the provider's docs under "Known Limitations".

## 14. Shell Warm-up Before CLI Launch

**Problem:** `wait_for_shell()` detects a stable shell prompt, but shell initialization scripts (.zshrc, brew shellenv, nvm) may still be running in the background. Launching a CLI immediately causes it to silently exit or hang (e.g., Gemini CLI exits without error).

**Fix:** After `wait_for_shell()`, send an echo round-trip with a unique marker to verify the shell is fully initialized:
```python
marker = f"CAO_SHELL_READY_{self.terminal_id}"
tmux_client.send_keys(session, window, f"echo {marker}")
# Poll until marker appears in output
```
This adds ~2 seconds but prevents silent launch failures.

## 15. TERM Environment Variable Compatibility

**Problem:** Some CLIs (e.g., Kimi CLI v1.20.0+) silently exit when `TERM=tmux-256color` (the tmux default). No error message, just immediate exit — leaving the terminal stuck in PROCESSING forever.

**Fix:** If your CLI fails silently inside tmux, try overriding the TERM variable:
```python
command = f"TERM=xterm-256color {base_command}"
```
Test your provider both inside and outside tmux to catch this.

## 16. Alt-Screen vs Scrollback — A Fundamental Design Decision

**Problem:** CLIs operate in one of two modes: alt-screen (full-screen TUI, e.g., Gemini, Kimi) or scrollback (output in normal terminal buffer, e.g., Codex with `--no-alt-screen`). Detection logic differs significantly between the two.

**Key differences:**
- **Alt-screen:** Idle prompt is at a fixed position near the bottom. Use `tail_lines` to check bottom N lines (typically 50). Pattern matching anchors to bottom-of-screen, not end-of-output.
- **Scrollback:** Full history persists across interactions. Previous idle prompts remain in buffer. Must use recency heuristics (e.g., last 5 lines only for idle check) or latching flags to distinguish current state from history.

**Fix:** Decide which mode your CLI uses early. If scrollback, you'll need more defensive pattern matching and likely a `TAIL_LINES` constant calibrated to your TUI's output density.

## 17. Startup Prompt Stabilization Loop

**Problem:** Many CLIs show cascading prompts on first launch: workspace trust → permission bypass → terms acceptance → model selection. A single-check approach misses subsequent prompts.

**Fix:** Handle startup prompts in a polling loop, not one-shot:
```python
def _handle_startup_prompts(self, timeout: float = 20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        output = tmux_client.get_history(...)
        if self._detect_trust_prompt(output):
            self._dismiss_trust_prompt()
        elif self._detect_bypass_prompt(output):
            self._dismiss_bypass_prompt()
        elif self._detect_idle_prompt(output):
            return  # All prompts cleared
        time.sleep(0.5)
```
Test with a fresh install (no cached settings) to catch all prompts.

## 18. Stale Permission Prompt Detection

**Problem:** Permission prompts like `Allow this action? [y/n/t]:` stay in the buffer after the user answers. If `get_status()` sees the prompt text, it returns WAITING_USER_ANSWER even though the agent already continued.

**Fix:** Count idle prompt lines AFTER the last permission prompt. Active prompt: 0–1 idle lines after it. Stale prompt: 2+ idle lines (user answered, agent continued):
```python
last_perm_idx = max(i for i, line in enumerate(lines) if re.search(PERM_PATTERN, line))
idle_lines_after = sum(1 for line in lines[last_perm_idx:] if re.search(IDLE_PATTERN, line))
if idle_lines_after >= 2:
    # Stale — don't return WAITING_USER_ANSWER
```

## 19. Per-Directory Single-Instance Locks

**Problem:** Some CLIs (e.g., Kimi CLI v1.20.0+) enforce one process per working directory. Parallel operations (3x data analyst via assign) fail when instances try to share a directory.

**Fix:** Create a unique temp directory per provider instance:
```python
import tempfile
self._work_dir = tempfile.mkdtemp(prefix=f"cao_{provider_name}_")
command = f"cd {self._work_dir} && {base_command}"
```
Clean up the temp directory in `cleanup()`.

## 20. E2E Validation: Assign + Handoff Orchestration

**Problem:** Unit tests and basic e2e tests pass, but the provider fails in real orchestration scenarios — supervisor assigns to 3 parallel workers + handoff to 1 report agent. This is the canonical multi-agent flow that exercises assign (non-blocking), handoff (blocking), send_message (async inbox), and status detection under concurrent load.

**Fix:** Before declaring a provider ready, test with the `examples/assign/` profiles:
```bash
cao install examples/assign/data_analyst.md
cao install examples/assign/report_generator.md
cao install examples/assign/analysis_supervisor.md
cao launch --agents analysis_supervisor
```
The test flow: supervisor assigns 3x data_analyst (parallel) + handoff 1x report_generator (blocking) → analysts send_message results back → supervisor combines template + results into final report. If any step fails, the provider has a gap in status detection, message extraction, or concurrent session handling. See `test/e2e/test_assign.py` for the automated version.
