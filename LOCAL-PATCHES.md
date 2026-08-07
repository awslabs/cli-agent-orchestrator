# Local patches: `cao launch --provider claude_code` fails

Diagnosed and fixed 2026-07-17 against CAO `main` (a614f32) + Claude Code CLI v2.1.212 on Linux / Python 3.14.

Three independent bugs stacked on top of each other. Each one hid the next, so fixing only the first
just moves the failure one stage later. All three must be applied.

---

## TL;DR — setting CAO up on a new machine

```bash
# 1. Clone + install (uv tool install is NON-editable — it copies the source)
git clone https://github.com/awslabs/cli-agent-orchestrator.git ~/cli-agent-orchestrator
cd ~/cli-agent-orchestrator
git checkout fix/fifo-pipeline-stalls        # <- the branch carrying fixes 2 & 3
uv tool install .

# 2. Fix 1 is config, not code. Must be > provider_init_timeout (60).
cao config set server.mcp_request_timeout 120

# 3. Start the server and launch
nohup cao-server > ~/.aws/cli-agent-orchestrator/logs/server-stdout.log 2>&1 &
cao launch --agents developer --provider claude_code --auto-approve
```

If you install from upstream `main` instead of the branch, bugs 2 and 3 come back.

---

## The install layout (read this first — it causes the most confusion)

CAO is a **`uv tool` install from a local git checkout**, and it is **NOT editable**:

```
~/cli-agent-orchestrator                     <- git checkout (source of truth)
~/.local/share/uv/tools/cli-agent-orchestrator/lib/python3.14/site-packages/cli_agent_orchestrator
                                             <- a COPY; this is what cao-server actually runs
```

Consequences that will bite you:

- **Editing the checkout does nothing until you reinstall.** The server runs the copy.
- **`uv tool install .` rebuilds the copy from whatever the checkout is currently on.** Reinstall while
  sitting on `main` and you silently revert all patches.
- **PyPI is the wrong place to check versions.** PyPI's 2.3.0 lags `main` by many commits.
  Use `git -C ~/cli-agent-orchestrator log main..origin/main`.

Verify the two copies agree:

```bash
diff -rq ~/.local/share/uv/tools/cli-agent-orchestrator/lib/python3.14/site-packages/cli_agent_orchestrator \
         ~/cli-agent-orchestrator/src/cli_agent_orchestrator
```

Any difference means the running server is not the code you think it is. **Check this first** if launches
break again after an update.

---

## Bug 1 — inverted timeout defaults (config fix)

**Symptom**

```
Error: Failed to connect to cao-server: HTTPConnectionPool(host='127.0.0.1', port=9889):
Read timed out. (read timeout=30)
```

**Cause**

The launch client POSTs with `timeout=mcp_request_timeout` (default **30s**, `cli/commands/launch.py:290`),
while the server legitimately blocks up to `provider_init_timeout` (default **60s**) waiting for the agent
to reach idle. Any init taking 30–60s therefore *always* times out client-side — while the server is
working correctly and would have succeeded. The defaults are simply inverted; `get_server_settings()`'s own
docstring example shows the intended shape (`mcp_request_timeout: 120` *above* `provider_init_timeout: 90`).

This error is a **red herring**: it masks the real failure. Fixing it doesn't make launch work, it makes
the actual error visible (a 500).

**Fix** — config only, no code, no server restart (the value is read client-side per invocation):

```bash
cao config set server.mcp_request_timeout 120
```

Persists to `~/.aws/cli-agent-orchestrator/settings.json`. Not versioned in git — re-apply per machine.

---

## Bug 2 — `_ever_delivered` blinded the cold-start watchdog (commit `d1b86fa`)

**Symptom**

```
ERROR - Failed to create terminal: Shell initialization timed out after 60s
WARNING - FIFO reader thread for terminal <id> did not exit within 2s; leaking a daemon thread
```

Note this fails in `wait_for_shell`, which runs **before Claude Code is ever launched** — it is waiting for
the plain *shell prompt*. Any theory about Claude's TUI is therefore irrelevant here.

**Cause**

1. tmux's initial `pipe-pane` attach often forwards nothing (the known cold-start case, harness-control#93).
2. The reader pulls some bytes, but they sit in `pending` until the coalesce window closes — when the pipe
   is cold-dead, the only flush is the `finally:` block at thread teardown.
3. `_ever_delivered` was set where bytes are **read**, not where they are **published**.
4. The cold-start check requires `not ever_delivered` → permanently blinded by bytes that never reached a
   consumer.
5. The divergence path can't cover for it: an idle shell's pane is static, so it never strikes.
6. Nothing re-arms the dead pipe → buffer stays empty → `wait_for_shell` times out.

**Fix** — move `_ever_delivered = True` to the publish site so it means "delivered to consumers".
`_last_data_at` stays on the raw read (divergence semantics unchanged).

**Evidence** — probe replicating `terminal_service`'s exact sequence: before, 0 events / 0 re-arms in 3/3
runs (matching 4/4 real launch failures); after, events delivered 3/3, watchdog self-heals.

---

## Bug 3 — FIFO stalls on Claude's alternate-screen TUI (commit `2f666e3`)

**Symptom** (only visible after bug 2 is fixed)

```
INFO  - Shell ready for <id> (buffer stable, 557 bytes)     <- bug 2 fixed, init got further
INFO  - wait_until_status [<id>]: waiting for {idle, completed}, timeout=60s
WARNING - wait_until_status [<id>]: timeout waiting for {idle, completed}
ERROR - Failed to create terminal: Claude Code initialization timed out after 60s
```

Meanwhile Claude is **completely healthy** — banner rendered, trust dialog accepted, idling at a ready prompt.

**Cause**

tmux silently stops forwarding to the FIFO after a burst of alternate-screen redraws (issue #388) — Claude's
Ink TUI is exactly that shape. The rolling buffer freezes on the pre-launch shell prompt (detects UNKNOWN)
while the pane renders a healthy agent.

The liveness watchdog **structurally cannot** recover this: once the TUI finishes painting, the pane is
STATIC, so its "pane advanced but FIFO silent" divergence test never trips — a settled frame is
indistinguishable from a genuinely idle terminal. Observed: exactly one cold-start re-arm, then the watchdog
sat silent for 66s while alive and ticking.

Detection was never at fault. On the real captured output, `get_status_from_screen` → IDLE and raw
`get_status` → COMPLETED. CAO just never saw the bytes.

**Fix** — `StatusMonitor._detect_from_live_pane()` + a hook in `get_status()`: on a cached UNKNOWN, detect
from tmux's live `capture-pane` instead of the frozen buffer. Mirrors the pre-existing PROCESSING escape
hatch in the same function. Gated on UNKNOWN, so it never forks tmux on the hot per-chunk path.

**Evidence** — real Claude in tmux with no CAO/FIFO involved: `get_status_from_screen(capture-pane)` →
COMPLETED in 4s. Tests: 146 passed.

---

## The load-bearing insight

**`capture-pane` is reliable. The FIFO / pipe-pane pipeline is not.**

`_handle_startup_prompts()` has always used `capture-pane` and has always worked — even while the FIFO was
stone dead and every FIFO-fed consumer was blind. That pipeline has four upstream issues against it (#382
blocked opens / leaked threads, #388 stalled forwarder, harness-control#93 cold start, #148 burst-then-settle)
and elaborate self-healing machinery that still didn't cover this case.

**If you need something to be correct, read `capture-pane`.** Bug 3's fix is an application of exactly this.

---

## Debugging notes

- **Server logs**: `~/.aws/cli-agent-orchestrator/logs/cao_<date>.log`.
  **Per-terminal raw output**: `logs/terminal/<terminal_id>.log` (written by LogWriter off the same event
  bus — if this file has content but StatusMonitor saw nothing, the data arrived in the teardown flush).
- **Tracebacks name site-packages, not the checkout** — proof the server runs the copy.
- `bus.publish` **silently no-ops** when `bus._loop is None` (`event_bus.py:61`). A standalone repro script
  must call `bus.set_loop()` or every publish vanishes with no error.
- The FIFO reader **spins** in `os.read`, it does not block — `O_NONBLOCK` is genuinely set (verified via
  `F_GETFL` on the live fd). A stack snapshot showing `os.read` is a spin artifact, not a wedge.
- The watchdog thread is named `fifo-pipe-watchdog`; check it exists and its state:
  `for t in /proc/$(pgrep -f bin/cao-server)/task/*; do cat $t/comm; done`
  Parked in `futex_do_wait` = healthy, ticking its 4s `Event.wait`.
- `_apply_detection` **only logs on change** — a status stuck at UNKNOWN logs nothing at all, which reads
  identically to "nothing is happening".
- Watchdog timings (`constants.py`): 3s cold-start grace, 4s check interval, 2 strikes to re-arm, max 5
  cold-start attempts. So a rescue should appear within ~7s; if it hasn't, the watchdog is blind, not slow.

## Wrong turns — don't repeat these

- **"Stale TUI regexes in `providers/claude_code.py`"** — wrong. The provider explicitly handles v2.1.212
  chrome: `NEW_TUI_BOX_PATTERN`, the `●` response glyph, the `· ✢ * ✶ ✻ ✽` spinner cycle, the
  `● high · /effort` footer. Both PyPI 2.3.0 and `main` have it. Bugs 2 and 3 both fail *before* or
  *independently of* TUI parsing.
- **"The pane is empty / the shell never starts"** — wrong. The shell renders `cao@cao:~$` fine.
- **"The reader thread is blocked in `os.read`"** — wrong. It spins; `O_NONBLOCK` is set.

## Upstreaming

Neither fix is upstream. Both are genuine bugs in `awslabs/cli-agent-orchestrator` that will affect anyone
whose pipe cold-starts or who runs a full-screen TUI provider. A merged PR is the only thing that ends the
patch-carrying — until then, every reinstall risks reverting them.
