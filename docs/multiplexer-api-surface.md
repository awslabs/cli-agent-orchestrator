# Multiplexer API Surface — Phase 0 Analysis

**Purpose:** Scope the tmux coupling in CAO so Phase 1 can design a `MultiplexerBackend` abstraction and implement a WezTerm backend.

**Date:** 2026-04-24  
**Branch:** `wezterm-multiplexer`  
**Analyst:** Phase 0 / TSK-067

---

## 1. Wrapper Module Location

| Item | Value |
|------|-------|
| File | `src/cli_agent_orchestrator/clients/tmux.py` |
| Class | `TmuxClient` |
| Module singleton | `tmux_client` (bottom of file) |
| External dependency | `libtmux` (Python bindings to the tmux socket protocol) |

All callers import the singleton: `from cli_agent_orchestrator.clients.tmux import tmux_client`

---

## 2. API Surface Table

All methods on `TmuxClient` that callers depend on. The private helper `_resolve_and_validate_working_directory` is omitted (it is called only internally, but the path-validation logic is itself a tmux-ism — see section 5).

| # | Method | Signature | Semantic Purpose | Callers (file:line) | Return shape callers depend on | tmux-isms / notes |
|---|--------|-----------|-----------------|---------------------|-------------------------------|-------------------|
| 1 | `create_session` | `(session_name, window_name, terminal_id, working_directory=None) -> str` | Create detached multiplexer session with initial window; inject `CAO_TERMINAL_ID` env var | `terminal_service.py:133` | Window name string (may differ from requested name after sanitisation) | Filters provider env vars (CLAUDE*, CODEX_*) from env before passing to `server.new_session`. `detach=True`. Returns `session.windows[0].name`. |
| 2 | `create_window` | `(session_name, window_name, terminal_id, working_directory=None) -> str` | Add window to existing session | `terminal_service.py:139-141` | Window name string | Injects `CAO_TERMINAL_ID` env via `environment=`. Returns `window.name`. |
| 3 | `send_keys` | `(session_name, window_name, keys, enter_count=1) -> None` | Send text to pane via paste-buffer trick; appends 1–2 Enters | `terminal_service.py:311-314`; all providers (see §3) | None | **Critical tmux-ism:** uses `load-buffer` + `paste-buffer -p` (bracketed paste, `\x1b[200~…\x1b[201~`) so Ink TUIs don't interpret content as hotkeys. `0.3 s` sleep between paste and Enter. Also sleeps `0.5 s` between multiple Enters. Bypasses direct `send-keys` character-by-character delivery. |
| 4 | `send_keys_via_paste` | `(session_name, window_name, text) -> None` | Alternative paste path using `libtmux` pane object | No callers in current src (dead code — exists in test suite) | None | Uses `server.cmd("set-buffer")` + `pane.cmd("paste-buffer", "-p")` + `pane.send_keys("C-m", enter=False)`. Functionally identical to `send_keys` but via libtmux objects instead of raw subprocess. |
| 5 | `send_special_key` | `(session_name, window_name, key) -> None` | Send tmux key name (e.g., `"C-d"`, `"Enter"`, `"Escape"`) without carriage return | `terminal_service.py:364`; `copilot_cli.py:192,195` | None | Uses `pane.send_keys(key, enter=False)` — sends key name in tmux notation. Not bracketed paste. Used for control signals. |
| 6 | `get_history` | `(session_name, window_name, tail_lines=None) -> str` | Capture pane scrollback (with ANSI escape sequences) | `terminal_service.py:395,404,416-418`; all providers (see §3); `utils/terminal.py:50` | Multiline string with ANSI codes; callers use regex over it | Uses `capture-pane -e -p -S -{lines}`. `-e` preserves escape sequences. Joined with `\n`. Default `TMUX_HISTORY_LINES = 200` lines. |
| 7 | `list_sessions` | `() -> List[Dict[str, str]]` | Enumerate all multiplexer sessions | `session_service.py:77,90` | List of `{id, name, status}` dicts; `id` == session name | Iterates `server.sessions`; `status` is `"active"` or `"detached"`. |
| 8 | `get_session_windows` | `(session_name) -> List[Dict[str, str]]` | List windows in a session | Not called in current src (unused — no call site found) | List of `{name, index}` dicts | Iterates `session.windows`. |
| 9 | `kill_session` | `(session_name) -> bool` | Kill entire session and all windows | `session_service.py:125`; `terminal_service.py:225` | `True` if killed, `False` if not found | Calls `session.kill()`. |
| 10 | `kill_window` | `(session_name, window_name) -> bool` | Kill one window within a session | `terminal_service.py:453` | `True` if killed, `False` if not found | Calls `window.kill()`. |
| 11 | `session_exists` | `(session_name) -> bool` | Check whether a named session exists | `terminal_service.py:129,137`; `session_service.py:87,112` | `bool` | Calls `server.sessions.get(session_name=...)` and checks for `None`. |
| 12 | `get_pane_working_directory` | `(session_name, window_name) -> Optional[str]` | Read the shell's current working directory from pane | `terminal_service.py:278`; `gemini_cli.py:240-241`; `copilot_cli.py:139` | Path string or `None` | Uses `pane.cmd("display-message", "-p", "#{pane_current_path}")`. tmux tracks CWD via OSC 7 or `/proc/<pid>/cwd`. |
| 13 | `pipe_pane` | `(session_name, window_name, file_path) -> None` | Stream all pane output to a log file | `terminal_service.py:188` | None | Calls `pane.cmd("pipe-pane", "-o", f"cat >> {file_path}")`. `-o` = only new output (not history). Raw terminal bytes including ANSI/OSC sequences. This is the primary status-detection input for the inbox service (see §6). |
| 14 | `stop_pipe_pane` | `(session_name, window_name) -> None` | Stop streaming pane output to log file | `terminal_service.py:447` | None | Calls `pane.cmd("pipe-pane")` with no arguments — disables the hook. |

**Total public interface methods: 14**  
(Methods 4 and 8 are present but have no call sites in current source outside of tests.)

---

## 3. All Call Sites — Provider Layer

Providers import `tmux_client` directly and call methods on it during lifecycle operations:

| Provider file | Methods called | Usage |
|--------------|---------------|-------|
| `providers/claude_code.py:195,255,258,272,326` | `get_history`, `send_keys` | `initialize()` (snapshot + launch + poll), `get_status()` |
| `providers/claude_code.py:220` | `tmux_client.server.sessions.get(...)` | **Direct libtmux access** — trust prompt handler bypasses wrapper to get `pane.send_keys("", enter=True)` |
| `providers/codex.py:225,258,267,285` | `get_history`, `send_keys` | `initialize()`, `get_status()` |
| `providers/codex.py:235` | `tmux_client.server.sessions.get(...)` | **Direct libtmux access** — trust prompt handler (same pattern as Claude Code) |
| `providers/gemini_cli.py:240,443,447,465,495,537` | `get_pane_working_directory`, `send_keys`, `get_history` | `_build_gemini_command()` (reads CWD for GEMINI.md), `initialize()` (warmup echo + launch + poll), `get_status()` |
| `providers/copilot_cli.py:75,139,192,195,274` | `get_history`, `get_pane_working_directory`, `send_special_key`, `send_keys` | History read, CWD for `--add-dir`, key sending, launch |
| `providers/q_cli.py:58,71` | `send_keys`, `get_history` | `initialize()`, `get_status()` |
| `providers/kiro_cli.py:167,178,184,212` | `send_keys`, `get_history` | Launch + fallback command, `get_status()` |
| `providers/opencode_cli.py:144,194` | `send_keys`, `get_history` | Launch, `get_status()` |
| `providers/kimi_cli.py:344,389` | `send_keys`, `get_history` | Launch, `get_status()` |

**Most-called methods by providers:** `get_history` (every provider), `send_keys` (every provider).

---

## 4. Direct tmux Invocations Bypassing the Wrapper

These are `subprocess.run(["tmux", ...])` calls that do NOT go through `TmuxClient`:

| Location | Command | Reason |
|----------|---------|--------|
| `clients/tmux.py:221-224` | `["tmux", "load-buffer", "-b", buf_name, "-"]` | Inside `send_keys()` — the wrapper itself. Part of the bracketed-paste trick that libtmux has no high-level API for. |
| `clients/tmux.py:226-228` | `["tmux", "paste-buffer", "-p", "-b", buf_name, "-t", target]` | Same — inside the wrapper. |
| `clients/tmux.py:240-242` | `["tmux", "send-keys", "-t", target, "Enter"]` | Same — inside the wrapper; sends Enter key(s) after paste. |
| `clients/tmux.py:249-251` | `["tmux", "delete-buffer", "-b", buf_name]` | Same — buffer cleanup inside wrapper. |
| `providers/claude_code.py:210` | `["tmux", "send-keys", "-t", target, "-l", "\x1b[B"]` | **True bypass:** sends raw Down-arrow escape directly to bypass the selection menu in Claude Code's trust/bypass prompt. `-l` (literal) mode only exists as a raw tmux flag — no libtmux equivalent. |
| `providers/claude_code.py:212` | `["tmux", "send-keys", "-t", target, "Enter"]` | **True bypass:** companion Enter after the Down-arrow above. |
| `cli/commands/info.py:27` | `["tmux", "display-message", "-p", "#S"]` | Reads the current session name inside an already-attached tmux session (user's interactive session). Used only for CLI UX, not agent orchestration. |
| `cli/commands/launch.py:187` | `["tmux", "attach-session", "-t", session_name]` | Attaches the user's terminal to the created session. UX-only, not agent orchestration. |
| `api/main.py:674` | `["tmux", "-u", "attach-session", "-t", ...]` | Attaches inside a PTY for the WebSocket terminal viewer endpoint. UX-only. |

**Summary of true bypasses (affecting agent I/O):** Lines `claude_code.py:210` and `:212` are the only agent-logic bypass — they send a raw escape sequence (`\x1b[B`, VT100 cursor-down) and Enter to navigate Claude Code's interactive selection UI. This cannot be expressed as a paste-buffer operation.

---

## 5. Supervisor / MCP-Layer Calls

The MCP server (`mcp_server/server.py`) does **not** call `tmux_client` directly. It operates entirely through the HTTP API:

- `handoff()` → `_handoff_impl()` → `_create_terminal()` → `POST /sessions` or `POST /sessions/{name}/terminals` → `terminal_service.create_terminal()` → tmux_client
- `assign()` → `_assign_impl()` → same path
- `send_message()` → `_send_to_inbox()` → inbox DB → `inbox_service.check_and_send_pending_messages()` → `terminal_service.send_input()` → `tmux_client.send_keys()`

The inbox service (`services/inbox_service.py`) reads the log file written by `pipe_pane` using `subprocess.run(["tail", "-n", ...])` — not a tmux call, but depends on the file that `pipe_pane` creates.

Status polling inside `handoff()` uses `wait_until_terminal_status()` which calls `GET /terminals/{id}` → `terminal_service.get_terminal()` → `provider.get_status()` → `tmux_client.get_history()`.

---

## 6. `pipe_pane` Deep-Dive

This is the highest-risk component for the WezTerm port.

### What it does

`pipe_pane` is tmux's mechanism to stream a copy of all bytes written to a pane to an external process. CAO uses: `pipe-pane -o "cat >> {file_path}"`.

- `-o` = only output directed to the pane (not history replay)
- Raw stream including ANSI/OSC escape sequences, carriage returns, overwrite sequences from TUI re-renders
- Written to `~/.aws/cli-agent-orchestrator/logs/terminal/{terminal_id}.log`

### Who starts it

`terminal_service.create_terminal()` calls `tmux_client.pipe_pane(...)` after provider initialization (line 188). `stop_pipe_pane()` is called in `delete_terminal()`.

### Who reads it

`services/inbox_service.py:_get_log_tail()` reads the last N lines via `tail -n {lines}` subprocess. This is the *fast-path* idle check before doing a more expensive `tmux capture-pane`.

### The two-phase detection pipeline

```
pipe_pane writes raw output → {terminal_id}.log
    ↓
watchdog FileSystemEventHandler triggers on modification
    ↓
_get_log_tail() reads last 100 lines via tail(1)
    ↓
_has_idle_pattern(): provider.get_idle_pattern_for_log() regex against tail
    ↓ (if pattern found)
check_and_send_pending_messages(): provider.get_status() for full check
    ↓ (if IDLE or COMPLETED)
terminal_service.send_input() → tmux_client.send_keys()
```

### Regex patterns consuming the log (per provider)

| Provider | `get_idle_pattern_for_log()` return value | Notes |
|----------|------------------------------------------|-------|
| ClaudeCode | `r"[>❯][\s\xa0]"` | Matches both old `>` and new `❯` prompt glyphs |
| Gemini | `r"\*.*Type your message"` | Asterisk + placeholder text |
| Kiro | (check `kiro_cli.py`) | TBD — not read in this analysis pass |
| Q CLI | (check `q_cli.py`) | TBD |
| Codex | (check `codex.py`) | TBD |
| Copilot | (check `copilot_cli.py`) | TBD |
| OpenCode | (check `opencode_cli.py`) | TBD |

### WezTerm risk

WezTerm has no equivalent of `pipe-pane`. The closest analogues are:

1. `wezterm cli get-pane-output --pane-id N` — dumps scrollback, not a live stream. Polling only.
2. User-defined event hooks in `wezterm.lua` (`wezterm.on("update-status", ...)`) — not per-pane I/O stream.
3. No documented API for redirecting byte-level pane output to a file.

This means the entire `pipe_pane` → log-file → watchdog → fast-idle-check pipeline must be redesigned for WezTerm. The most viable replacement is periodic polling of `get_history()` (already exists) with a debounced check against the idle pattern — eliminating the watchdog and the log file entirely.

**TBD-spike:** Does replacing `pipe_pane` with a polling loop introduce unacceptable latency for inbox message delivery? The current inbox polling interval is `INBOX_POLLING_INTERVAL = 5` seconds, so a polling approach may be within tolerance.

---

## 7. Open Questions / TBD-Spike Verifies

Items the Phase 1 spike must validate before any implementation is committed:

### High risk

**TBD-spike-1 (bracketed paste):** Does WezTerm's `wezterm cli send-text --no-paste` or equivalent deliver text that bypasses Ink TUI hotkey interception the same way tmux's `paste-buffer -p` does? The `\x1b[200~…\x1b[201~` bracketed-paste protocol is what makes CAO's `send_keys()` safe for interactive TUI apps. WezTerm must either support bracketed-paste injection directly, or the Phase 1 design must find another bypass mechanism.

**TBD-spike-2 (pipe_pane replacement):** Confirm that a polling-based replacement (`get_history()` polled on a timer) provides acceptable latency for inbox message delivery. Measure worst-case delivery lag at the default 5-second polling interval. Decide whether the watchdog-on-log-file pattern can be dropped entirely or needs an alternative event source.

**TBD-spike-3 (raw escape sequences):** Claude Code's trust-prompt handler sends `\x1b[B` (cursor-down) via `tmux send-keys -l` (literal mode). WezTerm's `wezterm cli send-text` does not have a `-l` flag — confirm whether it can inject raw VT sequences and whether the receiving TUI (Ink/React) reacts identically.

### Medium risk

**TBD-spike-4 (env injection on session create):** tmux allows `environment={"CAO_TERMINAL_ID": terminal_id}` on `new_session` and `new_window`. WezTerm's `wezterm cli spawn` supports `--set-environment KEY=VALUE`. Confirm this propagates to the child shell and to subprocesses (MCP servers spawned by the CLI).

**TBD-spike-5 (pane CWD):** `#{pane_current_path}` works because tmux reads CWD from the kernel (`/proc/<pid>/cwd` on Linux, `PROC_PIDVNODEPATHINFO` on macOS). WezTerm exposes pane CWD via `wezterm cli list --format json` → `cwd` field. Confirm the JSON API is stable and that it works on Windows (where there is no `/proc`).

**TBD-spike-6 (multi-window sessions):** tmux sessions map N windows to one session name. WezTerm uses tabs within a window (or multiple panes within a tab). The naming model is different — confirm that the `session_name:window_name` addressing scheme can be faithfully mapped to WezTerm concepts (e.g., `window_id:pane_id` or `window_id:tab_index`).

**TBD-spike-7 (capture-pane ANSI fidelity):** `capture-pane -e` preserves ANSI SGR sequences but strips many OSC and DCS sequences. `wezterm cli get-pane-output` has its own rendering pipeline. Confirm that provider regex patterns (which are tuned against tmux's `capture-pane -e` output) still match against WezTerm's output format. In particular: does WezTerm normalise or strip the exact Unicode characters providers look for (`❯`, `⏺`, `✦`, `▀`, `▄`, etc.)?

### Low risk

**TBD-spike-8 (detach mode):** tmux `new-session -d` creates a session without attaching. WezTerm always shows a GUI window. For headless server use, a WezTerm `--no-attach` or background spawn mode must be confirmed. (WezTerm does support `wezterm start --no-gui` in some modes — verify.)

---

## 8. Estimated Backend Method Count

The WezTerm `MultiplexerBackend` will need to implement **11 methods** to replace the `TmuxClient` API surface.

Methods 4 (`send_keys_via_paste`) and 8 (`get_session_windows`) have no current callers outside of tests and can be deferred to Phase 2 or dropped. The private `_resolve_and_validate_working_directory` logic is backend-agnostic and should move to the abstract base class.

The 11 required methods map directly to entries 1–3, 5–7, 9–14 in the API surface table (§2).

**Confidence:** High for the count; medium for the complexity estimate. The bracketed-paste replacement (method 3 / `send_keys`) and the `pipe_pane` redesign (method 13 / `pipe_pane`) are likely to require the most non-trivial work.

---

## Appendix: Files Referenced

- `src/cli_agent_orchestrator/clients/tmux.py` — TmuxClient class
- `src/cli_agent_orchestrator/services/terminal_service.py` — primary tmux_client consumer
- `src/cli_agent_orchestrator/services/session_service.py` — session-level tmux_client consumer
- `src/cli_agent_orchestrator/services/inbox_service.py` — pipe_pane log consumer
- `src/cli_agent_orchestrator/mcp_server/server.py` — MCP tools (handoff, assign, send_message)
- `src/cli_agent_orchestrator/providers/base.py` — BaseProvider ABC
- `src/cli_agent_orchestrator/providers/claude_code.py` — direct tmux bypass + libtmux direct access
- `src/cli_agent_orchestrator/providers/codex.py` — libtmux direct access
- `src/cli_agent_orchestrator/providers/gemini_cli.py` — get_pane_working_directory usage
- `src/cli_agent_orchestrator/providers/copilot_cli.py` — send_special_key usage
- `src/cli_agent_orchestrator/utils/terminal.py` — wait_for_shell calls get_history directly
- `src/cli_agent_orchestrator/constants.py` — TMUX_HISTORY_LINES, TERMINAL_LOG_DIR, INBOX_POLLING_INTERVAL
- `src/cli_agent_orchestrator/cli/commands/info.py` — tmux display-message bypass (UX)
- `src/cli_agent_orchestrator/cli/commands/launch.py` — tmux attach-session bypass (UX)
- `src/cli_agent_orchestrator/api/main.py` — tmux attach in WebSocket PTY (UX)
