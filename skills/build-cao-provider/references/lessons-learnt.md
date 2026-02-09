# Lessons Learnt from Building CAO Providers

Critical bugs encountered during Kimi CLI, Codex, Gemini CLI, and Claude Code provider development. Each lesson includes root cause, fix, and prevention strategy.

## Table of Contents

1. [CAO_TERMINAL_ID Must Be Forwarded to MCP Subprocesses](#1-cao_terminal_id-must-be-forwarded-to-mcp-subprocesses)
2. [TUI Padding Requires Sufficient Capture Lines](#2-tui-padding-requires-sufficient-capture-lines)
3. [Thinking vs Response: Use Raw ANSI Codes to Distinguish](#3-thinking-vs-response-use-raw-ansi-codes-to-distinguish)
4. [TUI Chrome Causes False Status Detection](#4-tui-chrome-causes-false-status-detection)
5. [Trust/Permission Prompts Block Initialization](#5-trustpermission-prompts-block-initialization)
6. [End-of-Line Anchoring Prevents False Prompt Matches](#6-end-of-line-anchoring-prevents-false-prompt-matches)
7. [Orphaned Processes Cause 404 Errors](#7-orphaned-processes-cause-404-errors)
8. [Ralph Autonomous Loop Catches Bugs Manual Review Misses](#8-ralph-autonomous-loop-catches-bugs-manual-review-misses)
9. [Shell Command Injection via f-string Interpolation](#9-shell-command-injection-via-f-string-interpolation)
10. [tmux Input Modes: Key Sequences and Bracketed Paste](#10-tmux-input-modes-key-sequences-and-bracketed-paste)
11. [Testing Hierarchy: Unit, Worker E2E, Supervisor E2E](#11-testing-hierarchy-unit-worker-e2e-supervisor-e2e)
12. [System Prompt Injection Is Required for Supervisor Orchestration](#12-system-prompt-injection-is-required-for-supervisor-orchestration)
13. [Ink TUI Always-Visible Idle Prompt Causes False Status Detection](#13-ink-tui-always-visible-idle-prompt-causes-false-status-detection)
14. [CLI Subprocess for Config Registration Adds Seconds Per Server](#14-cli-subprocess-for-config-registration-adds-seconds-per-server)
15. [MCP Tool Call Timeout Must Be Extended for Handoff](#15-mcp-tool-call-timeout-must-be-extended-for-handoff)

---

## 1. CAO_TERMINAL_ID Must Be Forwarded to MCP Subprocesses

**Symptom:** Worker agents created as separate tmux sessions instead of windows in the supervisor's session. User sees only the supervisor — workers are invisible.

**Root cause:** CLI tools do NOT forward parent shell env vars to MCP server subprocesses. Without `CAO_TERMINAL_ID`, cao-mcp-server doesn't know which tmux session to create windows in, so it creates entirely new sessions.

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

---

## 2. TUI Padding Requires Sufficient Capture Lines

**Symptom:** Idle detection fails on tall terminals AND inbox messages are never delivered to TUI-based providers. Two manifestations of the same root cause.

**Root cause:** Full-screen TUI apps (prompt_toolkit, Ink) fill empty space between the prompt and status bar with padding lines. The padding count depends on terminal height:

| Terminal Size | Padding Lines | Bottom 10 Lines Contains Prompt? |
|--------------|---------------|----------------------------------|
| 80x24 (E2E default) | ~10 | Yes |
| 150x46 (real user) | ~32 | **No** — prompt is at line ~14, missed entirely |

This causes failures at two levels:

### Provider level: IDLE_PROMPT_TAIL_LINES

E2E tests pass (80x24 tmux default has ~10 padding lines) but real usage fails (150x46+ terminals have 30+ padding lines). **Fix:** Set `IDLE_PROMPT_TAIL_LINES >= 50` in every TUI-based provider.

**Required test:** `test_get_status_idle_tall_terminal` — simulate a 46-row terminal with ~32 empty padding lines.

### Service level: Callers must not override provider defaults

The inbox service called `provider.get_status(tail_lines=5)`, overriding the provider's own 50-line requirement. With only 5 lines captured, the idle prompt was never found, messages stayed PENDING forever.

**Fix:** Remove `tail_lines` from service-level `get_status()` calls. Let each provider use its default (`TMUX_HISTORY_LINES=200` captured, provider slices to its `IDLE_PROMPT_TAIL_LINES` internally).

**Rules:**
- Set `IDLE_PROMPT_TAIL_LINES >= 50` for any TUI-based provider
- Never hardcode `tail_lines` in service-level code — let the provider decide
- The inbox service's fast-path log check (emoji regex on log file) is fine — only the tmux capture needs sufficient lines

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

## 8. Ralph Autonomous Loop Catches Bugs Manual Review Misses

**Symptom (Gemini CLI):** After completing all phases and passing all unit tests with 100% coverage, 3 bugs remained undetected until Ralph's autonomous verification loop found them: wrong npm package name in comments, wrong Unicode character name, stale test count in docs.

**Root cause:** Manual review suffers from anchoring bias — once you believe the implementation is "done," you stop looking for subtle issues. These are in comments, not executable code, so automated tests can't catch them.

**Prevention:** After Phase 4 (Code Quality), run Ralph with verification-focused tasks:
- Cross-check all lessons-learnt against the provider implementation
- Verify every pattern constant matches real captured terminal output
- Check documentation consistency (IDLE_PROMPT_TAIL_LINES, test counts, provider names)
- Verify no stale comments or copy-paste artifacts from reference provider

---

## 9. Shell Command Injection via f-string Interpolation

**Symptom:** Provider `initialize()` builds the CLI command using f-string interpolation: `f"kiro-cli chat --agent {self._agent_profile}"`. The `agent_profile` is user-supplied input from the API.

**Root cause:** The command string is sent via `tmux_client.send_keys()` to a shell. Shell metacharacters in `agent_profile` (`;`, `|`, `` ` ``, `$()`) are interpreted, enabling command injection.

**Fix:** Use `shlex.join()` for all command building that includes user-supplied values:

```python
command = shlex.join(["kiro-cli", "chat", "--agent", self._agent_profile])
```

**Rules:**
- EVERY provider must use `shlex.join()` or list-based command construction
- Never use f-strings for command building when any part comes from external input

---

## 10. tmux Input Modes: Key Sequences and Bracketed Paste

Two related but distinct issues with how CAO sends input to CLI tools via tmux.

### Key sequences must use non-literal send_keys

**Symptom:** Provider `exit_cli()` returns `C-d` (Ctrl+D), but the exit endpoint sends it via `send_input()` which uses `literal=True` — so the shell receives the literal text "C-d" instead of the Ctrl+D key press.

**Fix:** Add `send_special_key()` method that sends without literal mode, and route exit commands through it when they match the `C-`/`M-` prefix pattern:

```python
if exit_command.startswith(("C-", "M-")):
    terminal_service.send_special_key(terminal_id, exit_command)
else:
    terminal_service.send_input(terminal_id, exit_command)
```

### TUI hotkeys intercept literal send_keys — use bracketed paste

**Symptom:** Messages containing `!` sent to Gemini CLI toggle shell mode. The `!` character is a Gemini hotkey. After `!`, subsequent text is entered as a shell command. Status stays PROCESSING forever.

**Root cause:** `tmux send_keys(literal=True)` sends text character-by-character. TUI apps process each character through their input handler, checking for hotkeys before inserting text.

**Fix:** Use `tmux set-buffer` + `paste-buffer -p` (bracketed paste mode) for user messages. Bracketed paste wraps text in escape sequences, telling the TUI "this is pasted text" so it bypasses per-character hotkey handling.

```python
def send_keys_via_paste(self, session_name, window_name, text):
    self.server.cmd("set-buffer", "-b", "cao_paste", text)
    pane.cmd("paste-buffer", "-p", "-b", "cao_paste")
    time.sleep(0.3)
    pane.send_keys("C-m", enter=False)
```

**Rules:**
- `send_keys()` (literal mode) — only for initialization commands sent to a shell prompt
- `send_keys_via_paste()` (bracketed paste) — for all user messages sent to a running CLI TUI
- Text exit commands (`/exit`, `quit`) → `send_input()`; key sequences (`C-d`, `C-c`) → `send_special_key()`

---

## 11. Testing Hierarchy: Unit, Worker E2E, Supervisor E2E

All three levels are required. Each catches a different class of bugs that the others miss.

### Unit tests are not functional correctness

**Symptom:** Gemini CLI had 57 unit tests, 100% line coverage — all passing. But `cao launch --provider gemini_cli` failed with a 500 error because `gemini mcp add server -- command` looked correct as a string but caused yargs to fail when actually executed.

**Root cause:** Unit tests mock the tmux layer. They verify output strings but never execute commands against real binaries.

### Worker E2E tests don't cover supervisor delegation

**Symptom:** All 5 Gemini CLI E2E tests pass, but supervisor orchestration is completely broken. The tests simulate handoff by manually creating worker terminals via API — they never test a supervisor agent calling `handoff()` or `assign()` MCP tools.

**The gap:** `test_handoff.py` and `test_assign.py` test the building blocks (provider lifecycle, input/output) but not the orchestration flow (supervisor calls tool → workers spawn → results flow back). The system prompt injection bug (lesson #12) was invisible to all E2E tests because no test asks a supervisor to delegate.

**Rules:**
- Never ship a provider without running real E2E tests (handoff, assign, send_message)
- Every provider must have at least one E2E test using a supervisor profile
- The supervisor test must verify delegation actually happens (worker windows created)
- After writing provider code, run `cao launch` manually before writing tests
- 100% unit coverage does not mean the system works — it means every line was executed in isolation with mocks

---

## 12. System Prompt Injection Is Required for Supervisor Orchestration

**Symptom (Codex, then Gemini CLI):** Supervisor agent responds "I am the [CLI tool] agent" and does not know about `handoff()`, `assign()`, or `send_message()` tools. It cannot orchestrate workers.

**Root cause:** When adding a new provider, it's easy to focus on MCP server registration (enables tools) and forget the system prompt (tells the agent *how* to use them). Without the system prompt the agent has no context about its role, when to use handoff vs assign, or the multi-agent protocol.

**How each provider injects the system prompt:**

| Provider | Method | Mechanism |
|----------|--------|-----------|
| Codex | `-c developer_instructions="..."` | TOML config override |
| Claude Code | `--append-system-prompt <text>` | CLI flag |
| Kimi CLI | `--agent-file <path>` | Temp YAML + markdown file |
| Gemini CLI | `-i <text>` + `GEMINI.md` file | `-i` as first user message (primary); GEMINI.md for context (supplementary) |

**Important:** For Gemini CLI, `GEMINI.md` alone is NOT sufficient — the model treats it as weak background context. The `-i` flag sends the system prompt as the first user message, which the model strongly adopts. Always use `-i` as primary with GEMINI.md as supplementary.

**Rules:**
- Every provider MUST inject the agent profile system prompt
- If the CLI supports `-i` / `--prompt-interactive`, prefer it — stronger than instruction files
- Test that a supervisor agent describes its role correctly after launch
- This is a recurring mistake across providers — check: "Does `_build_command()` apply `profile.system_prompt`?"

---

## 13. Ink TUI Always-Visible Idle Prompt Causes False Status Detection

Gemini CLI uses React Ink for its TUI. Unlike prompt_toolkit-based TUIs (Kimi CLI, Codex) where the idle prompt disappears during processing, Ink renders the idle input box (`* Type your message`) at the bottom **at all times**. This causes three distinct failures:

### 13a. Spinner visible but idle prompt also visible → false COMPLETED

**Symptom:** Supervisor E2E test detects COMPLETED while MCP tools are still executing. Worker terminals haven't been created.

**Root cause:** `get_status()` sees idle prompt + query + response → COMPLETED, even though a Braille spinner like `⠴ Refining Delegation Parameters (esc to cancel, 50s)` is visible.

**Fix:** Add `PROCESSING_SPINNER_PATTERN` and check for it BEFORE checking for COMPLETED:

```python
PROCESSING_SPINNER_PATTERN = r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏].*\(esc to cancel"

if has_idle_prompt:
    if any(re.search(PROCESSING_SPINNER_PATTERN, line) for line in bottom_lines):
        return TerminalStatus.PROCESSING
    # ... then check for COMPLETED/IDLE
```

### 13b. Gap between text output and MCP tool call → premature COMPLETED in E2E

**Symptom:** `wait_for_status("completed")` returns immediately because the supervisor produced initial text (✦ response + idle prompt = COMPLETED), but the MCP tool call hasn't started — no spinner visible yet.

**Fix:** Use combined polling for supervisor E2E tests:

```python
def _wait_for_supervisor_done(supervisor_id, session_name, min_terminals, timeout, poll):
    while time.time() - start < timeout:
        status = get_terminal_status(supervisor_id)
        terminals = _list_terminals_in_session(session_name)
        if status == "completed" and len(terminals) >= min_terminals:
            return status, terminals
        time.sleep(poll)
```

### 13c. Idle prompt appears before -i prompt is processed → false IDLE

**Symptom:** Task messages sent after initialization are completely ignored. Supervisor stays idle forever.

**Root cause:** Ink renders the idle prompt immediately on startup, BEFORE the `-i` system prompt is processed. `initialize()` sees IDLE → returns early → test sends message → Gemini is still processing `-i` → message lost.

**Fix:** Track whether `-i` was used via `self._uses_prompt_interactive`. When `-i` is used, wait for COMPLETED (not IDLE):

```python
if self._uses_prompt_interactive:
    target_states = (TerminalStatus.COMPLETED,)
else:
    target_states = (TerminalStatus.IDLE, TerminalStatus.COMPLETED)
```

**Rules:**
- Ink-based TUIs do NOT hide the idle prompt during processing — never rely on idle prompt visibility alone
- Check for processing indicators (spinners, "Responding with") before concluding COMPLETED
- For providers with initial prompts, wait for COMPLETED specifically during initialization
- E2E supervisor tests should poll for both status AND terminal count simultaneously
- Add `test_get_status_processing_spinner_with_idle_prompt` from day one for Ink-based providers

---

## 14. CLI Subprocess for Config Registration Adds Seconds Per Server

**Symptom (Gemini CLI):** Assign/handoff ~15 seconds vs ~1 second for other providers.

**Root cause:** MCP server registration used `gemini mcp add --scope user` commands chained with `&&`. Each invocation spawns a full Node.js process (~2-3s) just to write a JSON entry to `~/.gemini/settings.json`. Cleanup had the same issue with `gemini mcp remove`.

**Fix:** Write MCP server entries directly to `~/.gemini/settings.json`:

```python
def _register_mcp_servers(self, mcp_servers: dict) -> None:
    settings_path = Path.home() / ".gemini" / "settings.json"
    settings = json.load(open(settings_path)) if settings_path.exists() else {}
    settings.setdefault("mcpServers", {})
    for name, config in mcp_servers.items():
        settings["mcpServers"][name] = {"command": ..., "args": ..., "env": ...}
    json.dump(settings, open(settings_path, "w"), indent=2)
```

**Rules:**
- If a CLI tool's `config add` command just writes to a JSON/YAML file, write directly instead of spawning the subprocess
- This applies to both registration AND cleanup paths
- Preserve existing entries — only add/remove what CAO manages
- Other providers already do this correctly: Kimi CLI/Claude Code use `--mcp-config` JSON flags, Codex uses `-c` flags (all single command)

---

## 15. MCP Tool Call Timeout Must Be Extended for Handoff

**Symptom (Codex, then Kimi CLI):** Supervisor agent calls `handoff` MCP tool. Worker terminal is created, receives the task, starts processing. After 60 seconds, the supervisor receives a `ToolError("Timeout while calling MCP tool handoff")` and gives up — even though the worker is still processing and would have completed in 90-120 seconds.

**Root cause:** CLI tools have a default MCP tool call timeout (typically 60 seconds):
- **Codex:** `tool_timeout_sec = 60` (TOML config, per MCP server)
- **Kimi CLI:** `tool_call_timeout_ms = 60000` (Python config, global)

The `handoff` MCP tool creates a worker terminal, initializes the provider (~5-30s), sends the message, waits for the agent to complete (30-300s+), extracts output, and returns. This routinely exceeds 60 seconds.

**Fix:** Override the timeout to match CAO's handoff timeout (600 seconds):

| Provider | Config mechanism | Override |
|----------|-----------------|----------|
| Codex | Per-server TOML: `-c mcp_servers.<name>.tool_timeout_sec=600.0` | Must be TOML float (600.0), not int — Codex deserializes via `Option<f64>` and silently rejects integers |
| Kimi CLI | Global config: `--config mcp.client.tool_call_timeout_ms=600000` | Integer milliseconds, set via `--config` CLI flag |
| Claude Code | No known timeout issue | N/A |
| Gemini CLI | No known timeout issue | N/A |

**Rules:**
- Check the CLI tool's MCP implementation for default tool timeout before shipping a provider
- The timeout should match `_handoff_impl()`'s default timeout (600 seconds)
- Only set the timeout when MCP servers are configured (no-op otherwise)
- Watch for type parsing quirks — Codex silently rejects integer TOML values for float fields
