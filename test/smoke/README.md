# Smoke harness

Real-world tests for the WezTerm multiplexer backend. NOT run by default.

## What this exercises

- spawn / send / get-text / kill on a real WezTerm pane
- Claude trust-prompt acceptance via `send_special_key("Enter")`
- Codex direct spawn via `build_launch_spec` (resolved Windows shim)
- inbox `pipe_pane` capture at the 500 ms polling cadence

## Prerequisites

- WezTerm GUI running, `wezterm` on PATH (CLI subcommand reachable)
- `claude` on PATH (Claude CLI)
- `codex` / `codex.cmd` on PATH (Codex CLI; Windows users may need the Scoop shim)

Tests skip with a clear message when any prerequisite is missing.

## Running

```bash
pytest -m smoke
pytest test/smoke -m smoke
pytest test/smoke -m smoke -v
```

Default `pytest` invocations DO NOT run these because the project default
`addopts` excludes the `smoke` marker.

## CI

Skip in CI by default. Optional dedicated workflow: install WezTerm +
provider CLIs, then run `pytest -m smoke` on a Windows runner.
