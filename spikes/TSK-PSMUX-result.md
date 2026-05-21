# TSK-PSMUX — psmux Compatibility Re-audit

**Status:** draft  
**Date:** 2026-04-25  
**Branch:** wezterm-multiplexer  
**Analyst:** subagent research pass (no live psmux execution)

---

## 1. Why This Re-audit

The Phase 0 decision to use WezTerm instead of psmux cited psmux's lack of tmux `-CC` control mode as the primary rationale (inherited verbatim from skiff research dated 2026-04-22).

That research is now stale:

- **2026-03-27** — psmux commit `b68962b3` (*"feat: tmux-compatible control mode (-C / -CC)"*) added full control-mode protocol: `%begin/%end/%error` response framing, 20+ async notification types (`%output`, `%window-add`, `%session-changed`, etc.), octal escaping, stable monotonic IDs (`$session`, `@window`, `%pane`), `-C` echo / `-CC` no-echo modes. Verified via 18 PowerShell integration tests. Source: `src/control.rs`, `tests/test_control_mode.ps1`.
- **2026-03-30** — commit `3ffc5704` extended control mode further: output ring buffer (per-pane 64 KB), `claim-session` psmux extension, additional format variables, and `pipe-pane` implementation.
- **2026-04-13** — commit `38d7cfa0` fixed `new-session -e` environment variable support.
- The upstream compatibility doc now explicitly marks: `Control mode | ✅ -C / -CC programmatic protocol`.

The TSK-067 Phase 0 audit enumerated CAO's 14-method API surface against tmux but **never re-validated psmux** after its control-mode features landed. This document corrects that.

**Installed psmux version on marcwin:** `3.3.1` (installed 2026-02-06 via Scoop, before control mode landed). Latest available: `v3.3.3` (2026-04-19).

---

## 2. CAO Multiplexer Surface

The 14 public methods on `BaseMultiplexer` (see `docs/multiplexer-api-surface.md` for the full table with callers and return shapes):

| # | Method | Core tmux mechanic |
|---|--------|--------------------|
| 1 | `create_session` | `new-session -d -e KEY=VAL` |
| 2 | `create_window` | `new-window -e KEY=VAL` |
| 3 | `send_keys` | `load-buffer -b <name>` + `paste-buffer -p -b <name>` + `send-keys Enter` |
| 4 | `send_keys_via_paste` | `set-buffer -b` + `paste-buffer -p -b` + `send-keys C-m` (dead code) |
| 5 | `send_special_key` | `send-keys [-l] <key>` |
| 6 | `get_history` | `capture-pane -e -p -S -{lines}` |
| 7 | `list_sessions` | iterate `server.sessions` |
| 8 | `get_session_windows` | iterate `session.windows` (dead code) |
| 9 | `kill_session` | `session.kill()` |
| 10 | `kill_window` | `window.kill()` |
| 11 | `session_exists` | `server.sessions.get(session_name=...)` |
| 12 | `get_pane_working_directory` | `display-message -p '#{pane_current_path}'` |
| 13 | `pipe_pane` | `pipe-pane -o "cat >> {file_path}"` |
| 14 | `stop_pipe_pane` | `pipe-pane` (no args = disable) |

Plus two direct-subprocess bypasses that are agent-logic (not UX):
- `send-keys -l "\x1b[B"` — literal VT escape to navigate Claude Code trust prompt (cursor-down)  
- `send-keys Enter` — companion Enter after cursor-down  

---

## 3. psmux Capability Snapshot

**Source examined:** `src/server/connection.rs`, `src/server/mod.rs`, `src/pane.rs`, `src/input.rs`, `src/format.rs` (all fetched via `gh api` at HEAD/master, 2026-04-25). Release history from `gh release list --repo psmux/psmux`.

**Control mode:** added commit `b68962b3` (2026-03-27). In v3.3.x. Not in the installed v3.3.1 which predates the commit by one day; available starting v3.3.2 (2026-04-08).

**Commands documented as supported:** 83 tmux commands; `tmux_args_reference.md` lists `pipe-pane`, `load-buffer`, `paste-buffer`, `set-buffer`, `delete-buffer`, `capture-pane`, `display-message`, `send-keys`, `new-session`, `new-window` all as fully supported with their flag sets.

**Transport:** TCP (127.0.0.1 + port file at `~/.psmux/<name>.port`), not Unix domain sockets. CAO's libtmux uses Unix socket paths.

**Buffer model:** psmux uses a numeric stack (`paste_buffers: Vec<String>` with max 10 entries), not tmux's named buffers. The `-b` flag in `paste-buffer` is parsed as `usize`, and `load-buffer -b <name>` ignores the `-b` name and loads from the file path argument.

**Bracketed paste in `paste-buffer`:** `paste-buffer` dispatches to `CtrlReq::SendText` (plain text write) — NOT `CtrlReq::SendPaste`. The `-p` flag is not parsed in `paste-buffer`'s handler (`src/server/connection.rs:1049–1062`). `SendPaste` (which wraps in `\x1b[200~…\x1b[201~`) is only invoked via `send-paste` (psmux extension command) or `send-keys -p`. This means `paste-buffer -p` in psmux does not produce bracketed paste output.

**Bracketed paste in `send-keys`:** `send-keys -p` does dispatch `CtrlReq::SendPaste`, which calls `send_paste_to_active`. That function checks `parser.screen().bracketed_paste()` and conditionally wraps in `\x1b[200~…\x1b[201~`. On Windows, it writes via the PTY pipe with ConPTY caveats documented in `src/input.rs:2613–2630`.

**`pipe-pane`:** implemented in `src/server/mod.rs:2938–2985`. Spawns a child process and connects it to the pane via stdout/stdin. The `-o` (only-new-output, i.e., toggle-if-running flag) is implemented. Raw byte stream from the pane PTY goes to the child's stdin. This is functionally equivalent to tmux's `pipe-pane -o "cat >> file"`.

**`display-message -p '#{pane_current_path}'`:** implemented in `src/format.rs:1117–1135`. Uses three-layer resolution: (1) Windows PEB walk to get foreground process CWD, (2) OSC 7 path from VT parser, (3) fallback to server CWD. This is a solid implementation on Windows.

**`new-session -e` / `new-window -e`:** `new-session -e` was broken until commit `38d7cfa0` (2026-04-13), which is in v3.3.2+. The tmux args reference confirms `-e` flag support in both.

**`capture-pane -e -p -S -N`:** source confirms `-e` (escape sequences), `-p` (stdout), `-S` (start line), `-E` (end line) are all parsed. `CapturePaneStyled` handles the `-e` path. ConPTY note in control-mode docs: "Alternate screen buffer switches are consumed internally; `capture-pane` reflects primary buffer only." This is relevant because providers detect prompts via `capture-pane` — if a provider uses the alternate screen, its output is NOT in the primary buffer.

**`send-keys -l <key>`:** the `-l` (literal) flag is parsed and dispatches `CtrlReq::SendKeys(keys, literal=true)` via `src/server/connection.rs:849–877`. Literal mode is supported.

---

## 4. Compatibility Matrix

Evidence sources abbreviated:
- **conn** = `src/server/connection.rs`
- **mod** = `src/server/mod.rs`
- **input** = `src/input.rs`
- **format** = `src/format.rs`
- **ref** = `docs/tmux_args_reference.md`

| # | Method | Verdict | Evidence / Gap |
|---|--------|---------|----------------|
| 1 | `create_session` | **GO with minor adaptation** | `new-session -e` works in v3.3.2+ (commit `38d7cfa0`); `detach=True` equivalent exists. CAO uses libtmux objects — would need subprocess/TCP rewrite; not a psmux gap, a CAO integration gap. |
| 2 | `create_window` | **GO with minor adaptation** | `new-window -e` supported per ref. Same integration-layer note as #1. |
| 3 | `send_keys` (bracketed paste path) | **NO-GO** | CAO's critical path: `load-buffer -b <uuid>` + `paste-buffer -p -b <uuid>`. In psmux: (a) named buffers don't exist — `-b` on `paste-buffer` parses as `usize`, so `cao_8f3a...` silently fails to index; (b) even if buffer lookup worked, `paste-buffer` dispatches `SendText` not `SendPaste` — the `-p` bracketed-paste flag is NOT parsed (conn:1049–1062). The entire bracketed-paste injection pipeline is broken. The workaround exists: replace with `send-keys -p <text>` (which does dispatch `SendPaste`), but that requires restructuring CAO's send path to not use the buffer intermediate. |
| 4 | `send_keys_via_paste` | **NO-GO** (dead code — same gap as #3) | Same named-buffer + `-p` flag issues. Not a priority since no current callers. |
| 5 | `send_special_key` | **GO** | `send-keys -l <vt_sequence>` supported and dispatches literal bytes (conn:849, literal=true path). `send-keys Enter` supported. The `\x1b[B` cursor-down injection for Claude trust prompt would work. |
| 6 | `get_history` | **UNKNOWN** | `capture-pane -e -p -S -{N}` is supported syntactically (conn:590–635). ConPTY limitation: psmux's `capture-pane` only captures the **primary buffer**, not the alternate screen. Ink-based TUIs (Claude Code, Codex) run in the alternate screen — their output will NOT appear in `capture-pane` output. This is the same behavior as tmux (tmux also only captures primary buffer by default), but it's worth confirming with a live test whether provider idle patterns (`❯`, `⏺`) appear after an alt-screen TUI returns to the primary buffer on psmux/ConPTY. If they don't, all provider status detection breaks. |
| 7 | `list_sessions` | **GO with minor adaptation** | `list-sessions -F '...'` supported. CAO would need to parse TCP/text output rather than iterate libtmux objects. Not a psmux gap. |
| 8 | `get_session_windows` | **GO with minor adaptation** | Dead code; `list-windows` supported. Same integration note. |
| 9 | `kill_session` | **GO with minor adaptation** | `kill-session` supported. Integration layer rewrite needed, not a psmux gap. |
| 10 | `kill_window` | **GO with minor adaptation** | `kill-window` supported. Same. |
| 11 | `session_exists` | **GO with minor adaptation** | `has-session -t <name>` supported. Same. |
| 12 | `get_pane_working_directory` | **GO** | `display-message -p '#{pane_current_path}'` implemented with 3-layer resolution including Windows PEB walk (format:1117–1135). Solid Windows implementation. |
| 13 | `pipe_pane` | **GO** | `pipe-pane -o "cat >> {file}"` is implemented (mod:2938–2985). The `-o` flag (toggle-if-running) is handled. A child process is spawned with pane PTY output piped to its stdin. Functionally equivalent to tmux. |
| 14 | `stop_pipe_pane` | **GO** | `pipe-pane` with no args closes the existing pipe (mod:2944–2950, `cmd.is_empty()` branch). |
| — | Direct bypass: `send-keys -l "\x1b[B"` | **GO** | `-l` literal flag supported (conn:849, `literal=true`). |
| — | Direct bypass: `send-keys Enter` | **GO** | Named keys work (conn:849). |

**Summary count:** 2 NO-GO (both #3 and #4, but #4 is dead code), 1 UNKNOWN (#6), 9 GO/GO-minor-adaptation, 1 GO (#12, #13, #14, #5).

---

## 5. Verdict

**psmux is mostly viable but has one specific functional gap and one untested risk.**

### NO-GO: Bracketed paste via `paste-buffer -p` (method #3, the most-called method)

This is the only true functional gap, and it is the load-bearing one. Every provider calls `send_keys` on every message delivery. The current tmux implementation uses:

```
load-buffer -b cao_<uuid> -   (reads from stdin pipe)
paste-buffer -p -b cao_<uuid> -t session:window
send-keys -t session:window Enter
```

In psmux, this pipeline fails at two points:
1. **Named buffers do not exist.** psmux uses a numeric stack. `paste-buffer -b cao_8f3a...` parses `cao_8f3a...` as `usize`, returns `Err`, falls back to buffer index 0. If another operation put content in buffer 0, the wrong content gets pasted.
2. **`paste-buffer -p` does not produce bracketed paste.** The handler dispatches `CtrlReq::SendText` regardless of the `-p` flag (`conn:1049–1062`). SendText writes raw bytes without `\x1b[200~…\x1b[201~` wrappers. Ink TUIs will receive the text without the "this is pasted input" signal, meaning hotkey interception is not bypassed.

**However, the gap is closable without touching CAO's production code.** The workaround is to replace the `load-buffer` + `paste-buffer -p` pipeline with a single `send-keys -p <text>` call. `send-keys -p` dispatches `SendPaste`, which does check `bracketed_paste()` and wraps correctly (`input.rs:2575–2683`). This would require adapting the TmuxMultiplexer's `_paste_text` method (or adding a `PsmuxMultiplexer` class), but it is a 5–10 line change — not an architectural issue.

**Alternatively:** marlocarlo could fix `paste-buffer -p` to dispatch `SendPaste` (a 1-line change in `conn:1061` — change `CtrlReq::SendText(text)` to `CtrlReq::SendPaste(text)`). This would make psmux's `paste-buffer -p` behaviorally identical to tmux's.

### UNKNOWN: `capture-pane` ANSI fidelity for alt-screen TUI providers

psmux's `capture-pane` captures the **primary buffer only**. All Ink-based TUIs (Claude Code, Codex) run in the alternate screen. After they exit back to the primary shell, their output is in the primary buffer — but the prompt patterns CAO detects (`❯`, `⏺`, `✦`) are emitted **into the alternate screen** while the TUI is active. Whether those bytes are visible to `capture-pane` on psmux/ConPTY is untested and may differ from tmux's behavior.

This is actually the same behavior tmux has (tmux `capture-pane` also only sees the primary buffer by default). CAO's existing tmux implementation works because after the TUI exits alt-screen and returns to the primary shell, the pattern appears. If psmux behaves identically, this is a GO. If ConPTY's alt-screen handling differs (discards vs. saves the primary buffer on switch), this could break all provider status detection.

---

## 6. Specific Gaps (for marlocarlo if asked)

1. **`paste-buffer -p` does not dispatch bracketed paste.** In `src/server/connection.rs` around line 1061, the handler unconditionally dispatches `CtrlReq::SendText`. For `-p` to behave like tmux (wrap in `\x1b[200~…\x1b[201~` when the target has bracketed-paste mode enabled), it should dispatch `CtrlReq::SendPaste` when the `-p` flag is present.

2. **Named buffers are not supported.** psmux uses a numeric stack (`Vec<String>`), so `load-buffer -b my_name_here -` and `paste-buffer -b my_name_here` don't round-trip. tmux named buffers (`-b <name>`) are referenced by string; psmux parses `-b` as `usize` and silently fails for non-numeric names. For safe concurrent use from external programs (multiple callers loading different buffers simultaneously), named buffers are needed.

3. **`new-session -e` was broken until v3.3.2.** Fixed in commit `38d7cfa0` (2026-04-13). Installed v3.3.1 on marcwin predates this fix. Not a psmux design gap — just a version currency issue.

---

## 7. What Live Testing Would Resolve the UNKNOWN

### UNKNOWN #6: `capture-pane` alt-screen fidelity

**Test plan (5–10 minutes, live psmux):**

1. Start a psmux session: `psmux new-session -d -s test-cap -x 220 -y 50`
2. Launch Claude Code (Ink TUI) in the session: `psmux send-keys -t test-cap 'claude' Enter`
3. Wait 5 seconds for TUI to fully render (it will be in the alternate screen)
4. While TUI is running (alt screen active), run: `psmux capture-pane -t test-cap -p -e` — verify whether the Ink UI content (`❯` prompt, `⏺` tokens, etc.) appears
5. Then type `/exit` to close Claude Code, wait 2 seconds, re-run `capture-pane` — verify the shell prompt appears
6. Compare against same test with tmux to see if output format differs
7. Run CAO's actual idle pattern against captured output: `python3 -c "import re; text=open('cap.txt').read(); print(re.search(r'[>❯][\s\xa0]', text))"`

**Expected outcomes:**
- If step 4 shows the Ink TUI content → UNKNOWN resolves to GO (psmux exposes alt-screen content, or ConPTY merges the buffers)
- If step 4 shows an empty/stale primary buffer → step 5 will determine if the post-exit primary buffer shows the prompt
- If step 5 fails (no shell prompt visible) → NO-GO; ConPTY is eating the primary buffer on alt-screen switch (rare but possible)

---

## 8. Implications for the WezTerm Decision

**The WezTerm path is still justified on independent merit**, separate from any psmux comparison. Specifically:

1. **Direct-spawn fast path for Codex.** The WezTerm `wezterm cli spawn` approach allows CAO to spawn providers with explicit argv (via `LaunchSpec`), resolving the Windows Codex `.cmd` shim issue documented in spike 2b. psmux spawns the default shell and you type into it — there is no equivalent of `wezterm cli spawn -- <exact-argv>`.

2. **Alt-screen TUI rendering.** WezTerm's `wezterm cli get-text` captures the **rendered terminal surface** (what the user sees), not the primary buffer. This means Ink TUI content is always visible in `get_history()`, regardless of alt-screen state. This is why spike 4 showed Claude trust text matching in plain `get-text` output. psmux's `capture-pane` is subject to the alt-screen UNKNOWN above.

3. **No dependency on psmux being installed.** WezTerm is already the user's interactive terminal. CAO's WezTerm path has zero additional setup on marcwin. A psmux backend would require ensuring psmux v3.3.2+ is installed and prefer it over the system tmux shim.

4. **The bracketed-paste gap in psmux is a workaround, not a blocker**, but it does mean a psmux backend would need its own `_paste_text` implementation (using `send-keys -p` instead of the buffer pipeline). The WezTerm backend already has a working implementation (`wezterm cli send-text --pane-id <id> -- <text>` for bracketed paste by default).

The honest summary: **psmux could serve as a third CAO backend with ~2 days of work** (TCP transport layer, `send-keys -p` paste path, `capture-pane` ANSI validation), and marlocarlo could close the `paste-buffer -p` gap upstream in minutes. But the WezTerm backend is already implemented and tested on marcwin. The original rationale ("psmux can't do control mode") is dead; the remaining reason to prefer WezTerm is the alt-screen fidelity advantage and the direct-spawn fast path — both of which are real, not sunk-cost rationalizations.

---

## 9. Recommended Next Actions

| Priority | Action | Effort |
|----------|--------|--------|
| **Now** | Update PRJ-042 description: replace "psmux lacks -CC" with the real distinction (alt-screen capture fidelity + direct-spawn fast path) | 10 min |
| **Now** | Run the live `capture-pane` alt-screen test (section 7) to resolve UNKNOWN #6 | 30 min |
| **Short-term** | Upgrade marcwin psmux from 3.3.1 → 3.3.3 (`scoop update psmux`) so `-e` env injection and control mode are available for any future spike | 5 min |
| **Optional** | File upstream psmux issue: "`paste-buffer -p` should dispatch bracketed paste, not plain text". Cite the 1-line fix (`conn.rs:1061`). This would close the only true gap for a future psmux backend. | 1 hr (issue + PR) |
| **Later** | If a psmux backend is desired: implement `PsmuxMultiplexer` using TCP transport, `send-keys -p` for paste, `capture-pane` for history. Add to `_BackendName` enum. | 2–3 days |

---

## 10. BASE Corrections

The following items reference the stale "psmux lacks -CC control mode" finding and should be updated:

### PRJ-042 description

**Current rationale (approximate):** "psmux was ruled out because it lacks tmux's -CC control mode, which CAO would need for programmatic session control."

**Correction:** psmux added full -CC control mode in commit `b68962b3` (2026-03-27), before PRJ-042 Phase 0 began. The actual distinctions favoring WezTerm over psmux are: (1) `wezterm cli get-text` captures the rendered terminal surface including alt-screen content — psmux's `capture-pane` captures primary buffer only, which may miss Ink TUI content while the TUI is active; (2) `wezterm cli spawn -- <argv>` allows direct process spawn with exact argv, bypassing shell shims — psmux has no equivalent.

### spikes/SUMMARY.md

`SUMMARY.md` does not mention psmux and contains no stale claims. No correction needed.

### docs/multiplexer-api-surface.md

The document correctly scopes itself to "tmux vs WezTerm" and does not make claims about psmux. No correction needed.

### Skiff design doc (`skiff/docs/specs/2026-04-22-skiff-design.md`, line ~139)

The skiff repo does not exist on marcwin (`ls /c/dev/` confirms no skiff directory). If the correction is needed, it should be applied on the machine where skiff is checked out. Suggested note to add near line 139:

> **2026-04-25 correction:** The claim that "psmux lacks tmux's -CC control mode" was accurate as of the 2026-04-22 research date but became stale the same week: psmux commit b68962b3 (2026-03-27) added full -CC control mode. The skiff recommendation to use WezTerm remains valid on independent grounds (alt-screen capture fidelity, direct-spawn fast path for Codex) but the stated psmux gap no longer exists.
