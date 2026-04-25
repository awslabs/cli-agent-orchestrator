# TSK-083 — Replace broken `wezterm cli spawn --set-environment` with argv-wrap

You are working in `C:\dev\aws-cao` (Windows host, git-bash shell). Branch
`wezterm-multiplexer` is checked out (Draft PR #206). Tests run via
`.venv/Scripts/python.exe -m pytest …`.

## The bug

`src/cli_agent_orchestrator/multiplexers/wezterm.py:_spawn` (lines ~91–112)
emits `--set-environment KEY=VALUE` arguments to `wezterm cli spawn`. **That
flag does not exist.** WezTerm silently ignores unknown args, so every spawn
on this branch loses `CAO_TERMINAL_ID` and any `launch_spec.env`. This was an
unverified spike assumption (TSK-078). The first real WezTerm smoke run on
marcwin caught it.

Upstream is a dead end: `wezterm cli spawn --help` (verified at
wezterm.org/cli/cli/spawn.html) supports only `[PROG]…`, `--pane-id`,
`--domain-name`, `--window-id`, `--new-window`, `--cwd`, `--workspace`. No env
flag, hidden or otherwise. Issue
[wezterm/wezterm#6565](https://github.com/wezterm/wezterm/issues/6565) was
closed by @wez on 2025-02-09 ("not in scope") with no PR. There is no Lua
callback that fires for `cli spawn`, so a config-side workaround is also
unavailable. The fix has to live in CAO.

## The fix

Wrap the spawned argv with a per-platform env-injection shim. The CAO terminal
ID and any `launch_spec.env` values are set by the wrapper before it execs
into the target.

**Unix wrapper (preferred — actually exec-replaces, target is pane pid 1):**

```text
env CAO_TERMINAL_ID=<id> [K1=V1 …] -- <argv...>
```

**Windows wrapper (PowerShell — does NOT exec-replace; powershell.exe stays
in the tree as parent of the target. This is fine, see "Why this is safe"
below):**

```text
powershell.exe -NoLogo -NoProfile -Command "$env:CAO_TERMINAL_ID='<id>'; [$env:K1='V1'; …] & '<exe>' @args"
```

When `launch_spec` is `None` or has no argv, fall back to the user's default
shell so the env-wrap still runs:

- Windows: `os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")`
- Unix: `os.environ.get("SHELL", "/bin/sh")`

### Why this is safe (document this in the wezterm.py module docstring)

1. The Unix `env` wrapper exec-replaces, so on Linux/macOS the spawned
   process IS the pane's pid 1 — clean.
2. The Windows PowerShell wrapper does not exec-replace (Windows has no
   `execve`); `powershell.exe` becomes the immediate child of WezTerm and the
   target becomes a grandchild. **CAO is immune to this** because
   `WezTermMultiplexer` does not query `wezterm cli list` or read
   `process_name` anywhere — verify with `grep -R "cli list\|process_name" src/`
   (zero hits in src/ outside a `# TODO: cli list not validated` comment).
   Status detection is regex-against-`get-text` output (see
   `providers/codex.py` and `providers/claude_code.py` `get_status()`).
3. Even if a future code path does query the foreground process name,
   wezterm's `get_foreground_process_name` on Windows walks the descendant
   tree (`mux/src/localpane.rs:542` → `find_youngest()` at lines 1093–1110)
   and reports the youngest console-attached descendant. Once the target
   process starts it has a later `start_time` than the wrapper and wins.
4. PowerShell's `&` (call operator) runs the target as a child and exits
   with the child's exit code automatically — no explicit `exit` needed.

Cite issue #6565 and the `find_youngest()` reasoning in the docstring so the
next maintainer knows why this code looks the way it does.

## Quoting

The PowerShell wrapper builds a single `-Command` string. Use single-quoted
PowerShell strings (literal — no `$`-expansion); escape embedded single
quotes by doubling them:

```python
def _ps_single_quote(value: str) -> str:
    """Quote a string for a PowerShell single-quoted literal: ' → ''."""
    return "'" + value.replace("'", "''") + "'"
```

`CAO_TERMINAL_ID` is a uuid-ish value (safe), but `launch_spec.env` values
and `argv[0]` (an absolute path that may contain spaces) MUST go through this
quoter. The argv tail uses `@args` splatting — pass it as an explicit list
embedded in the `-Command` body via a single `@(...)` array and `& <exe>
@args`. Concretely:

```powershell
$args=@('arg1','arg2'); & 'C:\path\target.exe' @args
```

Each list element is `_ps_single_quote`'d. This avoids the `cmd /c "X && Y"`
parsing pitfalls entirely.

The Unix wrapper uses argv-list form (no shell parsing), so values pass
through verbatim; only the `K=V` formatting needs construction.

## Files to modify

### Source

- `src/cli_agent_orchestrator/multiplexers/wezterm.py` — rewrite `_spawn`
  to use the new wrappers; add module-level helpers `_default_shell()`,
  `_wrap_with_env()`, `_ps_single_quote()`. Add a multi-line module
  docstring (or a clear block comment above `_spawn`) explaining why the
  wrapper is necessary, citing wezterm issue #6565.

  The new `_spawn` body should be roughly:

  ```python
  def _spawn(self, working_directory, terminal_id, launch_spec):
      env_vars = {"CAO_TERMINAL_ID": terminal_id}
      if launch_spec is not None and launch_spec.env:
          env_vars.update(launch_spec.env)

      if launch_spec is not None and launch_spec.argv:
          target_argv = list(launch_spec.argv)
      else:
          target_argv = [_default_shell()]

      wrapped = _wrap_with_env(env_vars, target_argv)
      cmd = [self._bin, "cli", "spawn", "--new-window", "--cwd", working_directory, "--", *wrapped]
      result = self._run(cmd, None)
      raw = result.stdout.strip()
      if not raw.isdigit():
          raise RuntimeError(...)
      return raw
  ```

- Pick the platform inside `_wrap_with_env` via `sys.platform == "win32"`
  (consistent with `multiplexers/launch.py:default_platform()`). Do NOT
  add a new dependency; PowerShell is part of every supported Windows
  install.

### Tests

The current `test/multiplexers/test_wezterm_multiplexer.py` has tests that
assert the OLD broken `--set-environment` shape (lines ~100–186). Those tests
codify the bug. **Rewrite them.**

- `test_argv_contains_new_window_cwd_and_terminal_id` (line ~100): assert the
  argv contains `--new-window`, `--cwd`, the cwd, and a `--` separator. After
  `--`, the wrapper-specific shape applies (see below). Assert that
  `CAO_TERMINAL_ID=tid-abc` appears in the wrapper's env-set step (search
  the joined argv string for either the `env`-style `CAO_TERMINAL_ID=tid-abc`
  token OR the PowerShell-style `$env:CAO_TERMINAL_ID='tid-abc'` substring,
  depending on platform). Use `monkeypatch.setattr(sys, "platform", "linux")`
  / `"win32"` to exercise BOTH shapes — do not skip either platform on the
  other host.

- `test_launch_spec_argv_appended_after_double_dash` (line ~140): the spec's
  argv is now wrapped; assert that the FINAL invocation seen by the wrapper
  contains the spec's argv tokens. On Unix: assert the argv slice starting
  after the wrapper's `K=V` block matches the spec argv. On Windows: assert
  `_ps_single_quote('codex.cmd')` and `_ps_single_quote('--yolo')` substrings
  appear inside the `-Command` string.

- `test_launch_spec_env_adds_set_environment` (line ~162): rename to e.g.
  `test_launch_spec_env_passed_through_wrapper` and assert the env values
  show up in the wrapper's env-injection step on both platforms.

- Add new tests:
  - `test_default_shell_used_when_launch_spec_is_none` — spec is None, the
    wrapped argv is `[default_shell]`, env still injected.
  - `test_ps_single_quote_doubles_embedded_single_quote` — direct unit test
    on `_ps_single_quote("it's")` returning `"'it''s'"`.
  - `test_windows_powershell_invocation_shape` — mock platform=win32, assert
    `argv` after `--` is `["powershell.exe", "-NoLogo", "-NoProfile",
    "-Command", <body>]`.
  - `test_unix_env_invocation_shape` — mock platform=linux, assert the
    wrapper starts with `["env", "CAO_TERMINAL_ID=…", "--", …]`.

Do NOT modify `test/smoke/*` — those are manual integration tests Marc
runs on the host. Their existing assertions on the spawn flow will keep
passing because they only check end-to-end behavior (pane appears, target
reports its terminal_id), which the new wrapper preserves.

## Out of scope (do not touch)

- `multiplexers/tmux.py` — tmux ignores `launch_spec` and uses its own env
  injection (`tmux send-keys` env arg). Leave alone.
- `multiplexers/launch.py` — `build_launch_spec()` resolves the codex path;
  unrelated to env passing.
- Any provider files. The `direct_spawned_wezterm` skip in
  `providers/codex.py` is correct given that the wrapper now actually
  delivers the env vars.

## Constraints

- Default branch: `wezterm-multiplexer`. Do NOT push. Do NOT open a new
  PR (#206 tracks this branch).
- Conventional commits — but YOU don't commit. Leave a clean working
  tree for Opus to review and commit.
- Scope discipline. No unrelated refactors, no abstraction beyond what
  the wrapper requires.
- No type-only or comment-only churn outside the touched functions.

## Verify

Run before declaring done (working in repo root, git-bash):

```bash
.venv/Scripts/python.exe -m pytest test/multiplexers test/providers test/services --no-cov --deselect test/providers/test_copilot_cli_unit.py
```

Pre-existing Windows symlink failures in `test_q_cli_integration.py` and
`test_tmux_working_directory.py` are environmental and unrelated — ignore
them. Everything else (≥792 tests) must pass. Report the exact pytest
result line in your summary.

## Deliverable

Modify source + tests in place; leave a clean working tree. Write
`spikes/TSK-083-result.md` (matching TSK-082-result.md style):

1. One-paragraph summary of the change.
2. List of files changed (with `+N/-M` line counts when easily available).
3. Pytest result line.
4. Any subtleties Opus should review before committing — especially around
   PowerShell quoting edge cases and how the new tests parameterize over
   `sys.platform`.

That's it. Go.
