# Cross-Provider Examples

Agent profiles that demonstrate cross-provider workflows where a supervisor on one
provider delegates to workers on different providers via the `provider` key in
their frontmatter.

## Profiles

| Profile | Provider Override | Pattern | Role |
|---------|------------------|---------|------|
| `cross_provider_supervisor.md` | *(none — uses launch provider)* | — | Supervisor that delegates to cross-provider workers |
| `data_analyst_claude_code.md` | `claude_code` | assign | Data analyst that runs on Claude Code |
| `data_analyst_gemini_cli.md` | `gemini_cli` | assign | Data analyst that runs on Gemini CLI |
| `data_analyst_kiro_cli.md` | `kiro_cli` | assign | Data analyst that runs on Kiro CLI |
| `report_generator_codex.md` | `codex` | handoff | Report generator that runs on Codex |

The worker profiles are identical to their counterparts in `examples/assign/` except for
the added `provider` field in the frontmatter. The supervisor profile references the
cross-provider worker names so CAO launches each worker on the correct provider.

## Installation

```bash
cao install examples/cross-provider/cross_provider_supervisor.md
cao install examples/cross-provider/data_analyst_claude_code.md
cao install examples/cross-provider/data_analyst_gemini_cli.md
cao install examples/cross-provider/data_analyst_kiro_cli.md
cao install examples/cross-provider/report_generator_codex.md
```

## Usage

Start the supervisor on any provider — it will delegate to workers on different providers:

```bash
# Start a Kiro CLI supervisor session with the cross-provider supervisor profile
cao launch --provider kiro_cli --agent-profile cross_provider_supervisor --session-name my-session

# The supervisor assigns tasks using cross-provider worker profiles.
# When it calls assign(agent_profile="data_analyst_claude_code", ...),
# CAO reads the profile's provider key and launches the worker on Claude Code
# instead of Kiro CLI.
```

You can also run the supervisor on Claude Code or Gemini CLI — the workers will
still launch on their respective providers:

```bash
cao launch --provider claude_code --agent-profile cross_provider_supervisor --session-name my-session
```

## E2E Tests

See `test/e2e/test_cross_provider.py` for automated tests that verify the
cross-provider resolution works across Kiro CLI, Gemini CLI, and Claude Code.

```bash
uv run pytest -m e2e test/e2e/test_cross_provider.py -v -o "addopts="
```
