# TSK-082 — Wire LaunchSpec end-to-end so Codex actually starts on WezTerm

You are working in the repo `C:\dev\aws-cao` (Windows, git-bash). The
branch `wezterm-multiplexer` is checked out. Tests run via
`.venv/Scripts/python.exe -m pytest …`.

## The bug (real correctness gap, surfaced by /simplify Wave C)

Phase 2 introduced a `LaunchSpec` plumbing through the multiplexer so
that on WezTerm, providers can spawn the CLI directly via `wezterm cli
spawn -- <argv>` (because `wezterm cli send-text` does NOT submit
reliably — Phase 1 spike 2b finding). On tmux the spec is irrelevant
(tmux just `del`s it).

**The end-to-end wiring is incomplete:**

1. `src/cli_agent_orchestrator/services/terminal_service.py:create_terminal`
   accepts a `launch_spec` parameter and forwards it to
   `get_multiplexer().create_session(...)` / `create_window(...)`, but
   **no caller ever passes one**. Confirmed:

   ```
   grep -R "create_terminal(" src/  →  api/main.py, mcp_server/server.py,
                                       services/flow_service.py,
                                       services/session_service.py
   ```

   None of them construct a `LaunchSpec`.

2. So on WezTerm, `WezTermMultiplexer._spawn` runs with `launch_spec=None`
   and spawns a **plain shell**, not codex.

3. Then `CodexProvider.initialize()` runs:

   ```python
   if self._launch_spec is None:
       self._launch_spec = build_launch_spec("codex", self._build_codex_argv())
   direct_spawned_wezterm = isinstance(get_multiplexer(), WezTermMultiplexer)
   if not direct_spawned_wezterm:
       …  warm-up + send_keys(command)  …
   ```

   On WezTerm `direct_spawned_wezterm` is True, so the provider
   **skips warm-up + `send_keys(command)`** — assuming codex was already
   spawned. But the multiplexer spawned a plain shell. **Codex never
   starts.**

The Wave A commit (`f5c95ba`) explicitly fixed the `isinstance` check
so the design intent is clear: WezTerm should direct-spawn codex via
LaunchSpec. The wiring just isn't finished.

## Files involved (read these first)

- `src/cli_agent_orchestrator/services/terminal_service.py` — has the
  unused `launch_spec` parameter on `create_terminal`
- `src/cli_agent_orchestrator/providers/codex.py` — has the
  `direct_spawned_wezterm` skip path
- `src/cli_agent_orchestrator/providers/claude_code.py` — does NOT
  currently use launch_spec; its tmux send-keys flow works on WezTerm
  via paste+Enter primitives. Whether claude should also direct-spawn
  on WezTerm is out of scope — focus on codex.
- `src/cli_agent_orchestrator/providers/manager.py` — constructs
  providers (CodexProvider call is at line 71)
- `src/cli_agent_orchestrator/multiplexers/launch.py` —
  `build_launch_spec()` resolves the codex.cmd path on Windows
- `src/cli_agent_orchestrator/multiplexers/wezterm.py` — `_spawn`
  consumes `launch_spec.argv` and `launch_spec.env`
- `src/cli_agent_orchestrator/multiplexers/tmux.py` — `del launch_spec`
  (tmux ignores it; the keyword is harmless to pass)
- `docs/PLAN-phase2.md` — the binding spec; consult for design intent
- `test/multiplexers/test_wezterm_multiplexer.py` — the
  `_spawn` / `LaunchSpec` tests
- `test/providers/test_codex_provider_unit.py` — codex initialize tests
  (mock `get_multiplexer`, `MagicMock(spec=WezTermMultiplexer)`)

## Goal

Make codex start correctly on WezTerm in production. Two acceptable
shapes — pick the one with the smaller blast radius:

**Option A (recommended): provider-supplied launch_spec.**
Add a method to BaseProvider like `get_launch_spec(multiplexer) ->
Optional[LaunchSpec]` that returns None by default. Override on
CodexProvider to return `build_launch_spec("codex",
self._build_codex_argv())` when `isinstance(multiplexer,
WezTermMultiplexer)`, else None. terminal_service calls this AFTER
deciding the multiplexer but BEFORE create_session/create_window, and
passes the result. Pros: each provider owns its own direct-spawn
decision; terminal_service stays backend-agnostic.

**Option B: codex-specific helper in terminal_service.**
A small `_compute_launch_spec(provider, multiplexer)` helper that today
returns codex's spec on WezTerm and None otherwise. Less abstraction
machinery, more concrete. Acceptable if Option A feels heavy.

Either way, after the fix:
- On WezTerm + codex: WezTerm spawns codex.cmd directly with the right
  argv/env. CodexProvider.initialize() takes the
  `direct_spawned_wezterm` branch and skips send_keys (this is the
  intended behavior — verify it).
- On WezTerm + claude: unchanged. Claude's send-keys flow continues to
  work via the paste primitives.
- On tmux + anything: unchanged. tmux discards launch_spec.

## Constraints

- **Do NOT commit, do NOT push.** Leave a clean working-tree change for
  Opus to review and commit. (This is per session protocol — see
  `CLAUDE.local.md` if it exists.)
- Conventional commit-style messages are not your job here; just
  describe the change in a final summary.
- Scope discipline: do NOT refactor unrelated code. Don't touch the
  inbox/terminal_service skill_prompt logic or anything outside the
  launch_spec wiring path.
- Update tests to reflect the new wiring. The current
  `test_codex_provider_unit.py` mocks `get_multiplexer` — make sure new
  tests cover the case where terminal_service passes a launch_spec to
  the multiplexer for codex on WezTerm, and the case where it does not
  for codex on tmux.
- Run `.venv/Scripts/python.exe -m pytest test/multiplexers
  test/providers test/services --no-cov --deselect
  test/providers/test_copilot_cli_unit.py` — the 7 pre-existing Windows
  symlink failures (test_q_cli_integration.py and
  test_tmux_working_directory.py) are environmental and unrelated;
  ignore them. Everything else must pass.

## Deliverable

Modify the source + tests in place, leave a clean working tree, and
write a short markdown summary at `spikes/TSK-082-result.md` covering:

1. Which option (A or B) you picked and one sentence on why.
2. List of files changed.
3. Pytest result line (e.g., `795 passed, 7 failed (pre-existing), 16
   skipped`).
4. Any subtleties Opus should review before committing.

That's it. Go.
