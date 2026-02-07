# Lessons Learned from Building CAO Providers

Critical bugs encountered during Kimi CLI, Codex, and Claude Code provider development. Each lesson includes root cause, fix, and prevention strategy.

---

## 1. CAO_TERMINAL_ID Must Be Forwarded to MCP Subprocesses

**Symptom:** Worker agents created as separate tmux sessions instead of windows in the supervisor's session. User sees only the supervisor in `Ctrl+b, s` — workers are invisible. `tmux list-sessions` shows the workers exist as separate sessions, but they never appear as windows alongside the supervisor. Handoff/assign may appear to work in server logs, but the user cannot see or interact with worker agents.

**Root cause:** CLI tools do NOT forward parent shell env vars to MCP server subprocesses. Without `CAO_TERMINAL_ID`, cao-mcp-server doesn't know which tmux session to create windows in, so it creates entirely new sessions instead of adding windows to the supervisor's existing session.

**Fix:** In `_build_command()`, inject into every MCP server config:

```python
env = mcp_config[server_name].get("env", {})
if "CAO_TERMINAL_ID" not in env:
    env["CAO_TERMINAL_ID"] = self.terminal_id
    mcp_config[server_name]["env"] = env
```

**Rules:**
- Preserve existing env vars (merge, don't overwrite)
- Never override an existing `CAO_TERMINAL_ID` value
- This hit BOTH Codex and Kimi CLI — it's a universal requirement for any provider with MCP support

**Required tests:**
- `test_build_command_mcp_preserves_existing_env`
- `test_build_command_mcp_does_not_override_existing_terminal_id`

---

## 2. IDLE_PROMPT_TAIL_LINES Must Cover Tall Terminals

**Symptom:** All handoff/assign timeout. Server log: "initialization timed out after 60 seconds" even though CLI is fully loaded.

**Root cause:** Full-screen TUI apps (prompt_toolkit, etc.) fill empty space between the prompt and status bar with padding lines. The padding count depends on terminal height:

| Terminal Size | Padding Lines | Bottom 10 Lines Contains Prompt? |
|--------------|---------------|----------------------------------|
| 80x24 (E2E default) | ~10 | Yes |
| 150x46 (real user) | ~32 | **No** — prompt is at line ~14, missed entirely |

**Why E2E tests pass but real usage fails:** Unattached tmux sessions default to 80x24. Real attached terminals use the client's window size (commonly 150x46+).

**Fix:** Set `IDLE_PROMPT_TAIL_LINES >= 50`. Must exceed: `(max_terminal_height - prompt_line_position)`.

**Required test:** `test_get_status_idle_tall_terminal` — simulate a 46-row terminal with ~32 empty padding lines between prompt and status bar. Add this from day one, not as a bugfix.

---

## 3. Thinking vs Response: Use Raw ANSI Codes to Distinguish

**Symptom:** Extraction returns thinking output mixed into the response because both use identical prefix characters (e.g., `•` bullet).

**Fix:** Maintain parallel raw (ANSI-preserved) and clean (ANSI-stripped) line arrays. Check raw lines for thinking-specific ANSI styling:

```python
THINKING_BULLET_RAW_PATTERN = r"\x1b\[38;5;244m\s*•"  # gray color

for i in range(start, end):
    if re.search(THINKING_BULLET_RAW_PATTERN, raw_lines[i]):
        continue  # skip thinking line
    filtered_lines.append(clean_lines[i].strip())
```

**Fallback:** If ALL lines are filtered as thinking, return all content anyway (edge case safety).

---

## 4. TUI Chrome Causes False Status Detection

**Symptom (Codex):** TUI footer (`? for shortcuts` / `context left`) and progress spinners (`Working (0s)`) matched content patterns, causing false COMPLETED/IDLE.

**Prevention:** During Phase 1 (output capture), identify ALL TUI chrome:
- Status bars (time, model, context %)
- Progress spinners / working indicators
- Footer hints (keyboard shortcuts)
- Mode indicators

Add explicit pattern constants and check for TUI chrome BEFORE content matching in `get_status()`. Exclude chrome lines in `extract_last_message_from_script()`.

---

## 5. Trust/Permission Prompts Block Initialization

**Symptom (Claude Code, Codex):** CLI shows "Do you trust this workspace?" on first launch. Provider waits for IDLE, but CLI is waiting for user input. Deadlock.

**Fix:** Add `_handle_trust_prompt()` that polls during initialization. If trust prompt detected, send Enter to accept. The provider runs with `--yolo` or equivalent, and CAO's own `cao launch` already confirms workspace trust.

---

## 6. End-of-Line Anchoring Prevents False Prompt Matches

**Symptom:** `user@dir💫` matched inside user input lines like `user@dir💫 some typed text`, causing false IDLE during typing.

**Fix:** Anchor idle prompt pattern to end of line:
```python
idle_prompt_eol = IDLE_PROMPT_PATTERN + r"\s*$"
```

---

## 7. Orphaned Processes Cause 404 Errors

**Symptom:** 404 on `/terminals/{id}` requests. Database is clean but old cao-server/cao-mcp-server from previous sessions still running with stale state.

**Debug:** Always check before investigating provider issues:
```bash
pgrep -f "cao-server|cao-mcp-server" | xargs kill
```

---

## 8. Architecture PNG Resolution Must Match Original

When re-rendering `docs/assets/cao_architecture.png`, check original dimensions first and use a scale factor that meets or exceeds them:

```bash
npx -p @mermaid-js/mermaid-cli mmdc -i docs/assets/cao_architecture.mmd -o docs/assets/cao_architecture.png -s 6 -b transparent
```
