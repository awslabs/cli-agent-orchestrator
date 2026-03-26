# Cross-Provider Examples

Agent profiles that demonstrate cross-provider workflows where a supervisor on one
provider delegates to workers on different providers via the `provider` key in
their frontmatter.

## How It Works

Each worker profile is identical to its counterpart in `examples/assign/` except for
the added `provider` field. When a supervisor calls `assign` or `handoff` with one of
these profiles, CAO reads the `provider` key and launches the worker on that provider —
regardless of which provider the supervisor is running on.

## Profiles

### Supervisor

| Profile | Description |
|---------|-------------|
| `cross_provider_supervisor.md` | Supervisor that delegates to cross-provider workers. Runs on whichever provider you launch it with. |

### Data Analysts (use with assign)

Pick the ones that match the providers you have installed. You do NOT need all of them.

| Profile | Provider Override |
|---------|------------------|
| `data_analyst_claude_code.md` | `claude_code` |
| `data_analyst_codex.md` | `codex` |
| `data_analyst_copilot_cli.md` | `copilot_cli` |
| `data_analyst_gemini_cli.md` | `gemini_cli` |
| `data_analyst_kiro_cli.md` | `kiro_cli` |

### Report Generator (use with handoff)

| Profile | Provider Override |
|---------|------------------|
| `report_generator_codex.md` | `codex` |

You can also use the base `report_generator` from `examples/assign/` if you want the
report generator to run on the same provider as the supervisor.

## Installation

Install the supervisor and whichever worker profiles match your installed providers:

```bash
# Required: supervisor
cao install examples/cross-provider/cross_provider_supervisor.md

# Pick the data analysts for providers you have installed:
cao install examples/cross-provider/data_analyst_claude_code.md
cao install examples/cross-provider/data_analyst_codex.md
# cao install examples/cross-provider/data_analyst_copilot_cli.md
# cao install examples/cross-provider/data_analyst_gemini_cli.md
# cao install examples/cross-provider/data_analyst_kiro_cli.md

# Report generator (pick one):
cao install examples/cross-provider/report_generator_codex.md
# Or use the base one from examples/assign/:
# cao install examples/assign/report_generator.md
```

## Usage

Start the supervisor on any provider — it will delegate to workers on the providers
specified in their profiles:

```bash
# Example: supervisor on Kiro CLI, workers on Claude Code + Codex
cao launch --provider kiro_cli --agent-profile cross_provider_supervisor --session-name my-session

# Example: supervisor on Claude Code
cao launch --provider claude_code --agent-profile cross_provider_supervisor --session-name my-session
```

The supervisor's system prompt lists all available worker profiles. When the user gives
a task, the supervisor picks which workers to use based on what's installed. If a worker
profile isn't installed, the assign/handoff call will fail with a clear error.

## E2E Tests

See `test/e2e/test_cross_provider.py` for automated tests that verify the
cross-provider resolution works across Kiro CLI, Gemini CLI, and Claude Code.

```bash
uv run pytest -m e2e test/e2e/test_cross_provider.py -v -o "addopts="
```
