# TSK-082 — Wire LaunchSpec end-to-end (result)

## Option chosen

**Option A — provider-supplied launch_spec.** `BaseProvider` exposes a
default `get_launch_spec(multiplexer) -> Optional[LaunchSpec]` that
returns `None`; `CodexProvider` overrides it to return a resolved
direct-spawn spec only when the active multiplexer is
`WezTermMultiplexer`. `terminal_service.create_terminal` calls the
provider for a spec before pane creation and forwards the result into
`create_session`/`create_window`.

Rationale: keeps `terminal_service` backend-agnostic — each provider
owns its own direct-spawn decision, so adding the same path for
Claude/Gemini later is a one-method override, not a new branch in the
service.

## Files changed

- `src/cli_agent_orchestrator/providers/base.py` — added
  `get_launch_spec()` default seam (returns None).
- `src/cli_agent_orchestrator/providers/codex.py` — override returns a
  cached `LaunchSpec` for `WezTermMultiplexer`, `None` otherwise.
- `src/cli_agent_orchestrator/services/terminal_service.py` —
  reordered: build provider BEFORE pane creation so the launch_spec
  decision is known at `create_session`/`create_window` time. After
  the multiplexer returns the actual window name, the provider's
  `session_name`/`window_name` are updated to match before
  `initialize()` runs.
- `test/providers/test_codex_provider_unit.py` — `+18`,
  asserts WezTerm path returns a populated LaunchSpec and tmux path
  returns None.
- `test/services/test_terminal_service.py` — `+26`, asserts the
  service calls `provider.get_launch_spec(multiplexer)` and forwards
  the spec to `create_session`.
- `test/services/test_terminal_service_full.py` — `+2`, asserts
  `get_launch_spec` is called once per `create_terminal` invocation.

## Test result

`.venv/Scripts/python.exe -m pytest test/multiplexers test/providers
test/services --no-cov --deselect test/providers/test_copilot_cli_unit.py`

→ **798 passed, 7 failed (pre-existing Windows symlink env issues —
test_q_cli_integration and test_tmux_working_directory; same baseline
as Wave C), 16 skipped, 33 deselected.**

Net delta vs Wave C: +3 passing tests, no regressions.

## Subtleties to review before commit

1. **Provider lifetime reordered.** `create_terminal` now constructs
   the provider BEFORE pane creation (was after). The error-handling
   block already calls `provider_manager.cleanup_provider(terminal_id)`
   before any pane teardown, so a failure between provider creation
   and pane creation cleans up correctly.
2. **Post-creation mutation of `provider_instance.session_name` and
   `window_name`.** Multiplexers may return a different window_name
   than requested (e.g. tmux dedup); the provider was constructed with
   the requested name and is updated afterwards. Acceptable for now,
   but the abstraction would be cleaner if BaseProvider exposed a
   single `bind(session_name, window_name)` setter, or if launch-spec
   computation moved off the provider instance entirely (classmethod
   or factory).
3. **`CodexProvider.initialize()` was not simplified.** It still has
   the `if self._launch_spec is None: self._launch_spec = build_launch_spec(...)`
   rebuild and the `direct_spawned_wezterm = isinstance(...)` skip.
   When `terminal_service` calls `get_launch_spec` first the rebuild
   becomes a no-op (cache hit), so the existing logic is correct but
   redundant on the WezTerm path. A follow-up could collapse it once
   we trust every code path goes through `terminal_service`.
4. **`launch_spec` parameter on `create_terminal`** is preserved but
   still has no caller. It now acts as an explicit override that beats
   the provider's `get_launch_spec`. Reasonable seam for future
   testing/mcp_server use; safe to leave.
