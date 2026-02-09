# Lessons Learned from Building CAO Providers

Critical bugs encountered during Kimi CLI, Codex, Gemini CLI, and Claude Code provider development. Each lesson includes root cause, fix, and prevention strategy.

## Table of Contents

1. [CAO_TERMINAL_ID Must Be Forwarded to MCP Subprocesses](#1-cao_terminal_id-must-be-forwarded-to-mcp-subprocesses)
2. [IDLE_PROMPT_TAIL_LINES Must Cover Tall Terminals](#2-idle_prompt_tail_lines-must-cover-tall-terminals)
3. [Thinking vs Response: Use Raw ANSI Codes to Distinguish](#3-thinking-vs-response-use-raw-ansi-codes-to-distinguish)
4. [TUI Chrome Causes False Status Detection](#4-tui-chrome-causes-false-status-detection)
5. [Trust/Permission Prompts Block Initialization](#5-trustpermission-prompts-block-initialization)
6. [End-of-Line Anchoring Prevents False Prompt Matches](#6-end-of-line-anchoring-prevents-false-prompt-matches)
7. [Orphaned Processes Cause 404 Errors](#7-orphaned-processes-cause-404-errors)
8. [Architecture PNG Resolution Must Match Original](#8-architecture-png-resolution-must-match-original)
9. [Ralph Autonomous Loop Catches Bugs Manual Review Misses](#9-ralph-autonomous-loop-catches-bugs-manual-review-misses)
10. [Shell Command Injection via f-string Interpolation](#10-shell-command-injection-via-f-string-interpolation)
11. [Exit Key Sequences Must Use Non-Literal tmux send_keys](#11-exit-key-sequences-must-use-non-literal-tmux-send_keys)
12. [TUI Hotkeys Intercept Literal send_keys — Use Bracketed Paste](#12-tui-hotkeys-intercept-literal-send_keys--use-bracketed-paste)
13. [Unit Test Coverage Is Not Functional Correctness — Run Real E2E](#13-unit-test-coverage-is-not-functional-correctness--run-real-e2e)
14. [System Prompt Injection Is Required for Supervisor Orchestration](#14-system-prompt-injection-is-required-for-supervisor-orchestration)
15. [E2E Tests Must Cover Supervisor Delegation, Not Just Worker Tasks](#15-e2e-tests-must-cover-supervisor-delegation-not-just-worker-tasks)

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

---

## 9. Ralph Autonomous Loop Catches Bugs Manual Review Misses

**Symptom (Gemini CLI):** After completing all 6 phases (implementation, tests, code quality, documentation, final gate) and passing all 588 unit tests with 100% coverage, 3 real bugs remained undetected until Ralph's autonomous verification loop found them.

**What Ralph found in 3 loops:**

| Loop | Finding | Category |
|------|---------|----------|
| 1 | Wrong npm package name in comments (`@anthropic-ai` instead of `@google`) | Copy-paste artifact from reference provider |
| 1 | Wrong Unicode character name in comment ("six-pointed star" instead of "four-pointed star" for U+2726) | Documentation accuracy |
| 1 | Stale test count in `DEVELOPMENT.md` (number didn't match actual pytest output) | Doc-code drift |
| 2 | Verified all code comments and security patterns — confirmed clean | Validation pass |
| 3 | Cross-checked all 8 lessons-learned against the provider — no missed patterns | Exhaustive verification |

**Root cause:** Manual review suffers from anchoring bias — once you believe the implementation is "done," you stop looking for subtle issues like wrong package names in comments or incorrect Unicode names. These are precisely the types of errors that pass all automated tests (they are in comments, not executable code) but erode trust in the codebase.

**Why automated testing does not catch these:** Unit tests validate behavior, not comment accuracy. A comment saying "six-pointed star" when the actual character is a four-pointed star will never cause a test failure. Similarly, stale test counts in documentation files are outside the scope of pytest.

**Prevention:** After completing Phase 4 (Code Quality) and before Phase 6 (Documentation), run Ralph with a verification-focused `fix_plan.md`:

```markdown
## Verification Tasks
- [ ] Cross-check all lessons-learned against the provider implementation
- [ ] Verify every pattern constant matches real captured terminal output
- [ ] Check documentation consistency (IDLE_PROMPT_TAIL_LINES, test counts, provider names)
- [ ] Verify no stale comments or copy-paste artifacts from reference provider
- [ ] Validate Unicode characters match actual CLI output (code points, names)
```

**Setup:** Copy skill references to `.ralph/specs/` so Ralph has the checklists to verify against:
```bash
cp skills/build-cao-provider/references/*.md .ralph/specs/
```

**Run from a real terminal (not Claude Code):**
```bash
ralph --monitor    # tmux 3-pane: loop, live output, status dashboard
ralph --live       # headless alternative
```

**Key insight:** Ralph's value is not in writing new code — it is in systematically verifying existing code against specifications and checklists, catching the class of bugs that humans skip once they believe the work is "done."

---

## 10. Shell Command Injection via f-string Interpolation

**Symptom:** Provider `initialize()` builds the CLI command using f-string interpolation: `f"kiro-cli chat --agent {self._agent_profile}"`. The `agent_profile` is user-supplied input from the API.

**Root cause:** The command string is sent via `tmux_client.send_keys()` to a shell running in the tmux pane. Shell metacharacters in `agent_profile` (`;`, `|`, `` ` ``, `$()`) are interpreted by the shell, enabling command injection.

**Fix:** Use `shlex.join()` for all command building that includes user-supplied values:

```python
# Before (unsafe):
command = f"kiro-cli chat --agent {self._agent_profile}"

# After (safe):
command = shlex.join(["kiro-cli", "chat", "--agent", self._agent_profile])
```

**Rules:**
- EVERY provider must use `shlex.join()` or list-based command construction
- Never use f-strings for command building when any part comes from external input
- This applies even though the command goes through tmux send_keys — the target pane runs a shell

---

## 11. Exit Key Sequences Must Use Non-Literal tmux send_keys

**Symptom:** Provider `exit_cli()` returns `C-d` (Ctrl+D), but the exit endpoint sends it via `send_input()` which uses `literal=True` — so the shell receives the literal text "C-d" instead of the Ctrl+D key press.

**Root cause:** `tmux.send_keys(literal=True)` sends text character-by-character. Key sequences like `C-d`, `C-c`, `M-x` are tmux key names that must be sent with `literal=False` (or via the `send-keys` tmux command without `-l`).

**Fix:** Add `send_special_key()` method that sends without literal mode, and route exit commands through it when they match the `C-`/`M-` prefix pattern:

```python
# In tmux.py:
def send_special_key(self, session_name, window_name, key):
    pane = self._get_pane(session_name, window_name)
    pane.send_keys(key, enter=False)  # no literal flag = tmux interprets key name

# In api/main.py exit_terminal:
if exit_command.startswith(("C-", "M-")):
    terminal_service.send_special_key(terminal_id, exit_command)
else:
    terminal_service.send_input(terminal_id, exit_command)
```

**Rules:**
- Text exit commands (`/exit`, `quit`) → `send_input()` (literal text)
- Key sequence exit commands (`C-d`, `C-c`) → `send_special_key()` (tmux interprets)
- The prefix detection (`C-`, `M-`) is sufficient since no CLI tool has a text exit command starting with these

---

## 12. TUI Hotkeys Intercept Literal send_keys — Use Bracketed Paste

**Symptom:** Messages containing `!` sent to Gemini CLI toggle shell mode. The `!` character is a Gemini hotkey. After `!`, subsequent text is entered as a shell command instead of a user message. Status stays PROCESSING forever because the idle prompt changes from `* Type your message` to `! Type your shell command`.

**Root cause:** `tmux send_keys(literal=True)` sends text character-by-character to the terminal. Ink-based TUI apps (Gemini CLI) process each character through their input handler, which checks for hotkeys before inserting text. The `!` character triggers shell mode toggle before the rest of the message arrives.

**Fix:** Use `tmux set-buffer` + `paste-buffer -p` (bracketed paste mode) instead of `send_keys(literal=True)` for user messages. Bracketed paste wraps text in `\x1b[200~...\x1b[201~` escape sequences, telling the TUI "this is pasted text" so it bypasses per-character hotkey handling.

```python
# In tmux.py:
def send_keys_via_paste(self, session_name, window_name, text):
    self.server.cmd("set-buffer", "-b", "cao_paste", text)
    pane.cmd("paste-buffer", "-p", "-b", "cao_paste")
    time.sleep(0.3)
    pane.send_keys("C-m", enter=False)

# In terminal_service.py send_input():
tmux_client.send_keys_via_paste(session, window, message)  # not send_keys()
```

**Rules:**
- `send_keys()` (literal mode) — only for initialization commands sent to a shell prompt
- `send_keys_via_paste()` (bracketed paste) — for all user messages sent to a running CLI TUI
- This is a universal fix applied to all providers, not just Gemini CLI

---

## 13. Unit Test Coverage Is Not Functional Correctness — Run Real E2E

**Symptom:** Gemini CLI provider had 57 unit tests, 100% line coverage, 14 Ralph verification loops — all passing. But `cao launch --provider gemini_cli` failed with a 500 error because the provider could never start.

**Root cause:** Unit tests mocked the tmux layer. `_build_gemini_command()` tests verified the output string contained expected substrings but never executed the command against a real `gemini` binary. The `--` separator in `gemini mcp add server -- command` looked correct as a string but caused yargs to fail when executed.

**Why Ralph missed it:** Ralph ran `uv run pytest --ignore=test/e2e` — explicitly excluding E2E tests. 14 loops verified comments, imports, doc counts, and pattern consistency. Zero loops tested whether the provider actually works.

**Fix:** Ralph's `fix_plan.md` must include real E2E tests as the FIRST blocking task:

```markdown
## BLOCKING — Real E2E Tests (run FIRST)
- [ ] Start cao-server: `uv run cao-server &`
- [ ] Run E2E: `uv run pytest -m e2e test/e2e/ -v --tb=long`
- [ ] All 25 E2E tests pass
- [ ] Stop cao-server
```

**Rules:**
- Never ship a provider without running real E2E tests (handoff, assign, send_message)
- After writing provider code, run `cao launch` manually before writing any tests
- Ralph's fix_plan.md must run E2E as the first task, not cosmetic checks
- 100% unit test coverage does not mean the system works — it means every line of code was executed in isolation with mocks

---

## 14. System Prompt Injection Is Required for Supervisor Orchestration

**Symptom (Codex, then Gemini CLI):** Supervisor agent launched with the `analysis_supervisor` profile responds "I am the [CLI tool] agent" and does not know it can use `handoff()`, `assign()`, or `send_message()` tools. It cannot orchestrate worker agents because it never received the system prompt describing its role, available tools, or the multi-agent protocol.

**History:** This first hit Codex — the initial implementation registered MCP servers but did not inject the agent profile system prompt. The supervisor had the tools available but no instructions on how to use them. Fixed by adding `-c developer_instructions="..."` support. The same mistake was repeated with Gemini CLI — MCP servers are registered, but the system prompt is silently skipped because Gemini CLI reads instructions from `GEMINI.md` files and the provider avoids creating files in the user's working directory.

**Root cause:** When adding a new provider, it's easy to focus on MCP server registration (which enables the tools) and forget that the system prompt is what tells the agent *how* to use those tools. Without the system prompt the agent has no context about:
- Its role as a supervisor
- When to use handoff (blocking) vs assign (non-blocking)
- How to read inbox messages from workers
- The multi-agent communication protocol

**How each provider injects the system prompt:**

| Provider | System Prompt Method | Mechanism |
|----------|---------------------|-----------|
| Codex | `-c developer_instructions="..."` | TOML config override (added after hitting this bug) |
| Claude Code | `--append-system-prompt <text>` | CLI flag |
| Kimi CLI | `--agent-file <path>` | Temp YAML + markdown file |
| Gemini CLI | **Not implemented** | GEMINI.md files (not created by CAO) |

**Fix options for CLIs without system prompt flags (in order of preference):**
1. Create a temporary instruction file (e.g., `GEMINI.md`) in the working directory with the system prompt, and clean it up in `cleanup()` — matches Kimi CLI's temp file approach
2. Use the CLI tool's `--prompt` or `--prompt-interactive` flag if available in newer versions
3. Send the system prompt as the first user message after initialization (workaround — less reliable since it's a user message, not a system instruction)

**Rules:**
- Every provider MUST inject the agent profile system prompt — without it, the agent has tools but no instructions
- If the CLI tool doesn't support system prompt flags, use temp files (Kimi CLI pattern) rather than skipping injection entirely
- This is a recurring mistake across providers — add a check in code review: "Does _build_command() apply profile.system_prompt?"
- Test that a supervisor agent actually describes its role correctly after launch (not just that it reaches IDLE)

---

## 15. E2E Tests Must Cover Real Handoff/Assign Delegation, Not Just Worker Tasks

**Symptom:** All 5 Gemini CLI E2E tests (2 handoff, 2 assign, 1 send_message) pass, but supervisor orchestration is completely broken. The tests never test a supervisor agent actually calling `handoff()` or `assign()` MCP tools to delegate work.

**What the current E2E tests actually do:**
- `test_handoff.py` — **simulates** handoff by manually creating a `developer` terminal via API, sending it a task, and extracting output. It tests the provider's ability to receive input and produce output, but does NOT test the `handoff()` MCP tool being called by a supervisor agent.
- `test_assign.py` — same pattern: manually creates `data_analyst`/`report_generator` worker terminals and sends tasks directly. Uses `examples/assign/` worker profiles but never launches the `analysis_supervisor`.
- `test_send_message.py` — tests inbox message delivery between two terminals via API.

**The gap:** These tests verify the **building blocks** (provider lifecycle, input/output, inbox delivery) but not the **orchestration flow** (supervisor receives task → calls handoff/assign MCP tool → workers spawn → results flow back). The `analysis_supervisor` profile exists in `examples/assign/` with complete orchestration instructions, but no E2E test exercises it.

**Why this matters:** The system prompt injection bug (lesson #14) was invisible to all E2E tests. A supervisor that can't delegate still passes every test because no test asks a supervisor to delegate. The handoff/assign E2E tests only test the worker side.

**What a real handoff/assign E2E test should verify:**
1. Launch a supervisor agent with `analysis_supervisor` profile
2. Send it a task that requires delegation (e.g., "Analyze datasets A and B in parallel, then generate a report")
3. Verify the supervisor calls `assign()` or `handoff()` (check that worker windows appear in the tmux session)
4. Verify the supervisor collects results from workers
5. Verify the supervisor produces a combined final response

**Rules:**
- Every provider must have at least one E2E test using a supervisor profile (e.g., `analysis_supervisor`)
- The test must verify that delegation actually happens (worker windows created in the same tmux session)
- Worker-only E2E tests are necessary but not sufficient — they test the building blocks, not the orchestration
- Add `test_supervisor_orchestration.py` to `test/e2e/` that launches a supervisor and verifies end-to-end delegation
- The current `test_handoff.py` and `test_assign.py` should be renamed or documented to clarify they test provider lifecycle, not MCP tool delegation
