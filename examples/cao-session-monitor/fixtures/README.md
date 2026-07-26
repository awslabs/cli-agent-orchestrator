# Fixtures

Four JSON files captured live against a running `cao-server` (Task 1 spike —
see `openspec/changes/cao-session-monitor/tasks.md` items 1.1/1.3). Not
fabricated: each one is real API output, frozen as ground truth for the herdr
plugin's parsing code (Tasks 4/5). `tests/test_fixture_shapes.py` pins every
fixture against the actual `cli_agent_orchestrator` function or Pydantic model
it mirrors, so drift between a fixture and the real source fails loudly here.

## Files

- `state_snapshot.json` — bare `DashboardSnapshot` dict (sessions, terminals,
  counts, scopes), as returned by `build_dashboard_snapshot()`. Not wrapped in
  the AG-UI `state_snapshot_frame` envelope.
- `state_delta.json` — bare RFC-6902 ops array, as returned by
  `diff_snapshot()`. Not wrapped in the AG-UI `state_delta_frame` envelope.
- `flows.json` — array of `Flow`-model-shaped rows (`GET /flows`).
- `workflows.json` — array of `WorkflowIndexRow`-shaped rows (`GET /workflows`).

## Spike findings (Task 1 success criteria)

### 1. Window-name field path

Each terminal's window name lives at `terminals[*].window` in
`state_snapshot.json`. It is projected from the raw backend's `tmux_window`
field via:

```python
"window": t.get("tmux_window", t.get("name")),
```

— `src/cli_agent_orchestrator/services/ui_state_service.py` (~line 90).

Observed format: `f"{agent_profile}-{uuid4().hex[:4]}"` (e.g. `developer-987d`),
matching `generate_window_name()` in `src/cli_agent_orchestrator/utils/terminal.py`.
This is the herdr tab label CAO writes for each terminal (`herdr_backend.py`
lines 321, 396). Tasks 4-6 correlate on this field directly — no fallback
key needed.

### 2. Workspace-label format

The workspace label — what later tasks match a herdr workspace against — is
the bare `session_name` (e.g. `cao-isolated-test`), **not** additionally
prefixed with `cao-` on top of that. Confirmed against
`src/cli_agent_orchestrator/backends/herdr_backend.py`:

- line 294: `args = ["workspace", "create", "--label", session_name]`
- line 321: `self._run_herdr(["tab", "rename", root_tab_id, window_name], check=False)`
- line 396: `args = ["tab", "create", "--workspace", workspace_id, "--label", window_name]`

`session_name` and `window_name` are passed through verbatim in every case —
herdr_backend.py never adds its own `cao-` prefix.

Note that `session_name` itself already carries CAO's own `cao-` prefix, from
`constants.SESSION_PREFIX = "cao-"` (applied in `terminal_service.py` before
the backend ever sees the name). So a workspace label like
`cao-isolated-test` is one prefix, not two — don't strip or match a second
`cao-` on top of it.

This is also why the plan's design explicitly rejects filtering by label
prefix: `openspec/changes/cao-session-monitor/design.md` D2 chooses to scope
by herdr session instead of workspace-label prefix, and
`specs/cao-session-monitor/spec.md` (line 29) formalizes it as a hard
requirement: "SHALL NOT filter by any `cao-` label prefix."
