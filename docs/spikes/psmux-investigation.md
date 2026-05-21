# psmux CAO Compatibility Investigation

**Branch:** `psmux-support` (branched from upstream `main`, commit `3df3fa0`)
**Multiplexer contract read from:** `wezterm-multiplexer` branch
**psmux clone used for source verification:** `C:\dev\psmux-scratch` (shallow, v3.3.3)
**Date:** 2026-04-25

---

## 1. psmux Overview

**Canonical repo:** https://github.com/psmux/psmux

**Description:** "The native Windows tmux. Born in PowerShell, made in Rust." A terminal multiplexer that uses Windows ConPTY directly, speaks the tmux command language, reads `.tmux.conf`, and requires no WSL, Cygwin, or MSYS2.

**Activity:**
- Stars: 1.5k
- Releases: 33 (latest v3.3.3, released April 19, 2026)
- Active development: multiple releases per month in Q1 2026
- Open issues: 36

**Platform support:**
- Windows 10/11 only (primary). No Linux or macOS support stated or implied.
- Uses Windows ConPTY (`portable-pty-psmux` fork of `portable-pty` crate).
- Targets PowerShell 7+ and cmd.exe; also works with bash/WSL child processes.

**License:** MIT

**Dependency stack (from `Cargo.toml`):**
- Terminal parser: `vt100-psmux` (custom fork of `vt100` v0.16.6)
- PTY: `portable-pty-psmux` (custom fork of `portable-pty` v0.9.3)
- TUI: `ratatui` v0.30 + `crossterm` v0.29
- Windows native: `windows-sys` v0.61 (Win32 Foundation, Memory, DataExchange)

---

## 2. Compatibility Surface

The `BaseMultiplexer` contract (read from `wezterm-multiplexer:src/cli_agent_orchestrator/multiplexers/base.py`) defines 13 abstract methods plus 2 default methods. Analysis against psmux CLI follows.

### Session/window lifecycle

| CAO method | psmux CLI equivalent | Status |
|---|---|---|
| `create_session(session_name, window_name, terminal_id, working_directory, launch_spec)` | `psmux new-session -s <name> -n <window> -c <dir> -d -e CAO_TERMINAL_ID=<id>` | WORKS — `-e` env injection confirmed at `commands.rs:1952-2093` |
| `create_window(session_name, window_name, terminal_id, working_directory, launch_spec)` | `psmux new-window -n <name> -c <dir>` | WORKS — standard tmux parity |
| `session_exists(session_name)` | `psmux has-session -t <name>` or parse `list-sessions` output | WORKS — `list-session-names()` exposed via `session.rs`; `has-session` command present |
| `list_sessions()` | `psmux list-sessions` | WORKS — formats output as text; needs `-F` parsing |
| `kill_session(session_name)` | `psmux kill-session -t <name>` | WORKS — confirmed at `commands.rs:1493` |
| `kill_window(session_name, window_name)` | `psmux kill-window -t <session>:<window>` | WORKS — confirmed at `commands.rs:529` |

### Input delivery

| CAO method | psmux CLI equivalent | Status |
|---|---|---|
| `_paste_text(session, window, text)` | `psmux send-keys -l <text>` | WORKS — literal flag confirmed at `commands.rs:1305` |
| `_submit_input(session, window, enter_count)` | `psmux send-keys Enter` (repeat via `-N count`) | WORKS — ENTER → `\r` confirmed at `commands.rs:1320` |
| `send_special_key(session, window, key, literal=False)` | `psmux send-keys <KeyName>` / `send-keys -l <seq>` | WORKS — full VT key map at `commands.rs:1319-1350` |

**Finding #1 applies here** — see §3.

### Capture and logging

| CAO method | psmux CLI equivalent | Status |
|---|---|---|
| `get_history(session, window, tail_lines)` | `psmux capture-pane -p -t <target>` | WORKS with caveats — see §3, Finding #3 |
| `get_pane_working_directory(session, window)` | `psmux display-message -p -t <target> '#{pane_current_path}'` | WORKS — three-layer CWD lookup (PEB walk → OSC 7 → fallback) confirmed at `format.rs:1117-1131` |
| `pipe_pane(session, window, file_path)` | `psmux pipe-pane -t <target>` | PARTIAL — `pipe-pane` command exists and forwards to server (`connection.rs:1435-1438`), but it is delegated via `send_control_to_port`. Actual file tee implementation is in the server; not fully audited. |
| `stop_pipe_pane(session, window)` | `psmux pipe-pane -O -t <target>` (tmux convention) | UNKNOWN — the `-O` off-toggle is not confirmed in the source reviewed; needs live test. |

### Default methods (implemented in BaseMultiplexer)

| Method | Notes |
|---|---|
| `send_keys()` | Default implementation on base class; calls `_paste_text` + `_submit_input` |
| `_resolve_and_validate_working_directory()` | Not backend-specific; psmux backend need not override |

---

## 3. Validate / Extend User's Findings

### Finding #1: `paste-buffer -p` does not emit bracketed-paste sequences

**Status: PARTIALLY CONFIRMED, but the framing needs correction.**

The user located the issue at approximately `connection.rs:1061`. The actual critical code is at `src/server/connection.rs:1049-1059`:

```rust
// src/server/connection.rs:1049-1059
"paste-buffer" | "pasteb" => {
    let buf_idx: Option<usize> = args.windows(2)
        .find(|w| w[0] == "-b")
        .and_then(|w| w[1].parse().ok());
    let (rtx, rrx) = mpsc::channel::<String>();
    if let Some(idx) = buf_idx {
        let _ = tx.send(CtrlReq::ShowBufferAt(rtx, idx));
    } else {
        let _ = tx.send(CtrlReq::ShowBuffer(rtx));
    }
    if let Ok(text) = rrx.recv() {
        let _ = tx.send(CtrlReq::SendText(text));  // line 1058 — plain send
    }
}
```

`SendText` routes to `send_text_to_active` (`server/mod.rs:1413`), which writes directly to `p.writer` with no bracketed-paste wrapping. Contrast with `CtrlReq::SendPaste` (`server/mod.rs:1415`) which calls `send_paste_to_active` and checks `parser.screen().bracketed_paste()` before emitting `\x1b[200~`/`\x1b[201~` (`input.rs:2549-2571`).

The `-p` flag on `paste-buffer` in tmux prints to stdout (does not mean "paste with bracketed sequences"). So the user's description is slightly imprecise. The real problem is: **`paste-buffer` always uses `SendText`, never `SendPaste`, regardless of whether the target pane has bracketed-paste mode enabled.** This causes multi-line prompt payloads to be treated as typed characters rather than paste events, which matters because Claude Code and Codex use Ink/readline which honor bracketed paste to distinguish multi-line pastes from individual keypresses.

**The one-line fix** is at `connection.rs:1058`: replace `CtrlReq::SendText(text)` with `CtrlReq::SendPaste(text)`. This routes through `send_paste_to_active`, which already handles the ConPTY bracketed-paste quirks (`input.rs:2613-2619`).

Note: `send-keys -p` already correctly routes through `SendPaste` (`connection.rs:877-878`). CAO's `_paste_text` maps to `send-keys -l`. If CAO uses `send-keys -l` for text injection and `send-keys Enter` for submission, it bypasses `paste-buffer` entirely and the bracketed-paste path is irrelevant for normal operation. The `paste-buffer` bug only matters if CAO adopts `load-buffer` + `paste-buffer` as its text delivery primitive (as the original tmux backend does in some paths).

### Finding #2: Named buffers don't exist — numeric stack cap 10

**Status: CONFIRMED.**

`app.paste_buffers` is `Vec<String>` (types.rs:392), cap 10 (enforced everywhere a buffer is inserted). There is no `HashMap<String, String>` for named buffers.

The `-b` flag on `paste-buffer` (`connection.rs:1050`) parses the name as `usize`:

```rust
let buf_idx: Option<usize> = args.windows(2)
    .find(|w| w[0] == "-b")
    .and_then(|w| w[1].parse().ok());  // parses as usize, not String
```

If CAO passes `-b cao_550e8400-e29b-...`, `w[1].parse::<usize>()` returns `Err` → `None` → falls through to `ShowBuffer` (index 0). All CAO instances would share buffer slot 0 and stomp each other.

The same issue affects `load-buffer` in `server/connection.rs:1739-1742` (no `-b` flag parsing at all) and `connection.rs:1091` (delete-buffer also parses `-b` as usize).

**Minimal upstream patch shape (describe, not implement):**

1. Add a `String` name field alongside the `Vec<String>` stack: `paste_buffer_names: Vec<Option<String>>` parallel to `paste_buffers`, or replace with `Vec<(Option<String>, String)>`.
2. In `load-buffer`, `set-buffer`, `paste-buffer`, `delete-buffer`: parse `-b` as `String` first, fall back to `usize` parsing only when the value is purely numeric.
3. Lookup by name when `-b <name>` is provided; fall back to position 0 when no `-b`.
4. `CtrlReq` variants `ShowBufferAt`, `DeleteBufferAt`, `PasteBufferAt` need a name-capable variant or a unified `BufferRef { name: Option<String>, index: Option<usize> }` type.

This is a ~50-80 line change in 3-4 files. Not architecturally complex but requires upstream PR with test coverage.

**CAO workaround (no upstream change needed):** Do not use named buffers. Use `set-buffer <text>` + `paste-buffer` (no `-b` flag) immediately, with a per-target session/window lock to prevent concurrent CAO instances from racing. This is safe if CAO serializes operations per window, which it does in practice.

### Finding #3: `capture-pane` alt-screen fidelity on ConPTY — UNKNOWN

**Status: Source analysis complete. Verdict depends on live test.**

Source chain for `capture-pane -p`:
1. `connection.rs:590` → `CtrlReq::CapturePane(rtx)` 
2. `server/mod.rs` → calls `capture_active_pane_text` (variant of `capture_active_pane` in `copy_mode.rs:669+`)
3. `copy_mode.rs:655-666`: acquires `p.term.lock()` → calls `parser.screen()` → iterates `screen.cell(r, c)` for `p.last_rows × p.last_cols`

The key question is what `vt100::Parser::screen()` returns when the child process has switched to alt-screen. From `vt100-rust` source (`github.com/doy/vt100-rust`, `src/screen.rs`):

```rust
fn grid(&self) -> &Grid {
    if self.mode(MODE_ALTERNATE_SCREEN) {
        &self.alternate_grid
    } else {
        &self.grid
    }
}
```

All cell-access methods (`cell()`, `contents()`, etc.) call `self.grid()`. Therefore `parser.screen().cell(r, c)` **already returns alt-screen content** when the child process (Claude Code, Codex) has switched to alt-screen via smcup/`\x1b[?1049h`. The vt100 parser is fed the raw PTY byte stream and tracks both grids.

**This means the mechanism is sound on paper.** However, two unknowns remain for live ConPTY conditions:

1. **ConPTY passthrough fidelity:** ConPTY on Windows may modify or delay PTY bytes, causing the vt100 parser's alt-screen state to lag behind the rendered screen. The `data_version` atomic in `Pane` tracks updates but there is no explicit synchronization between the PTY reader thread and the capture call.
2. **Ink/React redraw pattern:** Ink TUIs (Claude Code's status bar, Codex's idle indicator) do incremental redraws using cursor positioning, not full-screen rewrites. If the vt100 parser's alt-screen grid only contains the last partial redraw, the idle pattern (e.g., `✻ Thinking`, `⣾ Working`) may not be present at row/column 0 where CAO's regexes look.

**Concrete 30-minute live test recipe:**

Prerequisites: psmux v3.3.3 installed (`winget install psmux` or `cargo install psmux`), `claude` on PATH, `codex.cmd` reachable.

```powershell
# Step 1: Start a detached psmux session
psmux new-session -s cao-test -d -n claude-pane -c $HOME

# Step 2: Launch Claude Code inside it
psmux send-keys -t cao-test:claude-pane "claude --no-auto-updater" Enter
Start-Sleep -Seconds 10  # let it reach idle/trust prompt

# Step 3: Capture pane and inspect
psmux capture-pane -p -t cao-test:claude-pane > C:\Temp\capture-claude.txt
Get-Content C:\Temp\capture-claude.txt | Select-String -Pattern "✻|Thinking|Working|Continue|Trust|permit" -CaseSensitive:$false

# Step 4: Compare with direct WezTerm get-text baseline (if WezTerm is running same session)
# Expected: the idle indicator line should appear in capture output

# Step 5: Repeat with Codex
psmux new-window -t cao-test -n codex-pane -c $HOME
psmux send-keys -t cao-test:codex-pane "codex.cmd -c hooks=[] --yolo --no-alt-screen --disable shell_snapshot" Enter
Start-Sleep -Seconds 15
psmux capture-pane -p -t cao-test:codex-pane > C:\Temp\capture-codex.txt
Get-Content C:\Temp\capture-codex.txt | Select-String -Pattern "idle|waiting|ready|\$" -CaseSensitive:$false

# Step 6: Send a benign prompt and check capture during processing
psmux send-keys -l -t cao-test:claude-pane "echo hello"
psmux send-keys -t cao-test:claude-pane Enter
Start-Sleep -Seconds 3
psmux capture-pane -p -t cao-test:claude-pane > C:\Temp\capture-claude-busy.txt
Get-Content C:\Temp\capture-claude-busy.txt | Select-String -Pattern "✻|Thinking|Working" -CaseSensitive:$false

# Success criteria:
# - Idle pattern present in capture-claude.txt (before prompt)
# - Busy pattern present in capture-claude-busy.txt (after prompt, before response)
# - Output is not garbled with ANSI escape sequences (--no-escapes default in capture-pane)

# Step 7: Cleanup
psmux kill-session -t cao-test
```

**What to look for vs. tmux/WezTerm baseline:**
- tmux `capture-pane -p`: well-known to show alt-screen content correctly for Ink TUIs.
- WezTerm `wezterm cli get-text`: shows rendered surface, reliable for Ink TUIs.
- psmux `capture-pane -p`: should match tmux if vt100 parser is fed the full ConPTY byte stream. A follow-up agent can run this recipe in ~15 minutes on a Windows machine with psmux installed.

---

## 4. Verdict and Next Steps

**Verdict: NEEDS-WORKAROUND**

psmux is architecturally compatible with CAO's `BaseMultiplexer` contract. All session lifecycle, input delivery, special-key, and pane-capture operations have tmux-equivalent CLI commands. The project is actively maintained (33 releases, last commit April 2026), MIT-licensed, and explicitly targets the Windows ConPTY environment that is CAO's primary pain point.

Two bugs block a clean implementation:

1. **`paste-buffer` → `SendText` instead of `SendPaste`** (confirmed, single-line upstream fix). Workaround: CAO should use `send-keys -l <text>` for `_paste_text` (as the WezTerm backend does), which already routes through the correct path and does not touch `paste-buffer` at all. This workaround costs nothing.

2. **Named buffer collision** (confirmed). Workaround: serialize buffer operations per window (no concurrent `set-buffer`+`paste-buffer` across panes without a lock). CAO already operates sequentially per window in practice. The workaround is safe for MVP.

3. **`capture-pane` alt-screen fidelity** (unconfirmed, 30-minute live test settles it). If the vt100 parser is correctly fed the PTY byte stream (likely, given psmux's own TUI renders correctly from it), this is a non-issue. If not, CAO would need a polling loop with retry logic similar to the WezTerm poller.

**Ordered next steps (if pursuing psmux backend):**

1. Run the §3 Finding #3 live test recipe. If `capture-pane` sees the Ink idle pattern → proceed. If not → characterize the lag and decide whether a retry loop is acceptable.
2. Implement `PsmuxMultiplexer(BaseMultiplexer)` using `send-keys -l` for paste (not `paste-buffer`). Model closely on `TmuxMultiplexer`; the command surface is nearly identical.
3. Wire `create_session` to pass `CAO_TERMINAL_ID` via `-e CAO_TERMINAL_ID=<id>`.
4. File upstream PR for the `paste-buffer` → `SendPaste` fix (one line, clearly correct, easy for maintainer to accept).
5. File upstream issue for named buffer support. Do not block CAO implementation on this.
6. Add `get_pane_working_directory` using `display-message -p '#{pane_current_path}'`.
7. Audit `pipe_pane` / `stop_pipe_pane` in a live test (the server-side implementation was not fully audited in this spike).

**Honest effort-vs-value assessment:**

WezTerm already works on Windows. The concrete benefit psmux adds:

- **Windows-headless operation:** psmux runs as a background server with no GUI window. WezTerm requires a visible GUI window to expose its CLI. A `cao-server` on a Windows headless machine (CI, cloud VM, Windows Server) cannot use WezTerm. psmux fills this gap.
- **Tmux muscle memory for existing tmux users migrating from Linux:** psmux reads `.tmux.conf` and accepts tmux aliases. Users who already script against tmux get psmux for free on Windows.
- **Smaller footprint:** psmux is a single ~5MB binary. WezTerm is ~100MB. For containers or slim Windows environments this matters.
- **FOSS-only:** psmux is MIT. WezTerm is MIT+Apache. Both are fine, but psmux has no GUI dependency that could pull in additional licensing questions.

The value case is real but narrow: headless Windows and psmux-first users. If neither is a priority, WezTerm is the better-tested path.

---

## 5. Open Questions

1. **`pipe_pane` server-side implementation**: The `pipe-pane` handler in `connection.rs:1435-1438` forwards to the server via `send_control_to_port`. The actual file-tee logic is in `server/mod.rs` but was not fully traced. It is unknown whether it supports the `-O` (off) toggle and whether the output file path survives session reattach. Needs live test.

2. **`capture-pane` fidelity under ConPTY** (per §3 Finding #3): Settled by the 30-minute live test. Cannot determine from source alone whether ConPTY byte delivery to the vt100 parser is synchronous enough for CAO's polling loop.

3. **`new-session` from inside a running psmux session**: Issue #200 workaround is implemented in psmux (`commands.rs:1937`), but it spawns a new server process. Whether this is transparent to a CAO `PsmuxMultiplexer` that connects via the control socket needs verification.

4. **Target addressing (`-t session:window`) under the psmux IPC model**: tmux's `-t` addressing is well-understood. psmux forwards most targeted commands via `send_control_to_port`. The exact routing for multi-session scenarios was not traced end-to-end.

5. **Linux/macOS availability**: psmux is Windows-only. A `PsmuxMultiplexer` backend would not provide cross-platform parity; it would be a Windows-specific backend alongside tmux (Linux/macOS) and WezTerm (Windows GUI). This is the correct framing for the PR, not a "universal" backend.

---

*Sources:*
- https://github.com/psmux/psmux
- `C:\dev\psmux-scratch\src\server\connection.rs` (lines 1049-1059, 590-628, 849-884, 1739-1742)
- `C:\dev\psmux-scratch\src\copy_mode.rs` (lines 636-666)
- `C:\dev\psmux-scratch\src\commands.rs` (lines 1300-1395, 1451-1479)
- `C:\dev\psmux-scratch\src\input.rs` (lines 2549-2619)
- `C:\dev\psmux-scratch\src\types.rs` (line 392)
- `C:\dev\psmux-scratch\src\format.rs` (lines 1117-1131)
- https://github.com/doy/vt100-rust (Screen::grid() alt-screen selection)
- `git show wezterm-multiplexer:src/cli_agent_orchestrator/multiplexers/base.py`
