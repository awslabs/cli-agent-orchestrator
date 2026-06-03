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
| `cross_provider_supervisor.md` | Supervisor that delegates to cross-provider workers |

### Data Analysts (assign — parallel)

| Profile | Provider Override |
|---------|------------------|
| `data_analyst_claude_code.md` | `claude_code` |
| `data_analyst_gemini_cli.md` | `gemini_cli` |
| `data_analyst_kiro_cli.md` | `kiro_cli` |

### Additional Data Analysts

These are not referenced by the default supervisor profile but are available
if you want to use other providers:

| Profile | Provider Override |
|---------|------------------|
| `data_analyst_codex.md` | `codex` |
| `data_analyst_copilot_cli.md` | `copilot_cli` |

### Report Generator (handoff — sequential)

| Profile | Provider Override |
|---------|------------------|
| `report_generator_codex.md` | `codex` |

## Setup

1. Start the CAO server:
```bash
cao-server
```

2. Install the agent profiles:
```bash
# Supervisor
cao install examples/cross-provider/cross_provider_supervisor.md

# Default worker profiles (used by the supervisor)
cao install examples/cross-provider/data_analyst_claude_code.md
cao install examples/cross-provider/data_analyst_gemini_cli.md
cao install examples/cross-provider/data_analyst_kiro_cli.md
cao install examples/cross-provider/report_generator_codex.md
```

3. Launch the supervisor:
```bash
# Using Kiro CLI (workers on Claude Code + Gemini CLI + Kiro CLI + Codex)
cao launch --agent-profile cross_provider_supervisor --provider kiro_cli

# Or specify a different provider for the supervisor
cao launch --agent-profile cross_provider_supervisor --provider claude_code
cao launch --agent-profile cross_provider_supervisor --provider codex
cao launch --agent-profile cross_provider_supervisor --provider gemini_cli
cao launch --agent-profile cross_provider_supervisor --provider kimi_cli
cao launch --agent-profile cross_provider_supervisor --provider copilot_cli
```

## Usage

In the supervisor terminal, try this example task:

```
Analyze these datasets and create a comprehensive report:
- Dataset A: [1, 2, 3, 4, 5]
- Dataset B: [10, 20, 30, 40, 50]
- Dataset C: [5, 15, 25, 35, 45]

Calculate mean, median, and standard deviation for each dataset.
Generate a professional report with the analysis results.
```

## Customizing the Supervisor

The default supervisor uses `data_analyst_claude_code`, `data_analyst_gemini_cli`,
and `data_analyst_kiro_cli` for data analysis, and `report_generator_codex` for
report generation. To use different providers:

1. Install the additional worker profiles you need:

```bash
cao install examples/cross-provider/data_analyst_codex.md
cao install examples/cross-provider/data_analyst_copilot_cli.md
```

2. Copy and edit the supervisor profile to reference the profiles you want:

```bash
cp examples/cross-provider/cross_provider_supervisor.md my_supervisor.md
```

3. In `my_supervisor.md`, update the **Worker Profiles** table and the **Example**
   section to use your preferred profiles. For example, to use Codex and Copilot CLI
   instead of Gemini CLI and Kiro CLI:

```markdown
| `data_analyst_claude_code` | Claude Code |
| `data_analyst_codex` | Codex |
| `data_analyst_copilot_cli` | Copilot CLI |
```

4. Install and launch your custom supervisor:

```bash
cao install my_supervisor.md
cao launch --provider kiro_cli --agent-profile my_supervisor --session-name my-session
```

## Creating Your Own Cross-Provider Profile

To create a cross-provider version of any agent profile, add a `provider` field to
the frontmatter:

```yaml
---
name: my_agent_codex
description: My agent that runs on Codex
provider: codex
mcpServers:
  cao-mcp-server:
    type: stdio
    command: uvx
    args:
      - "--from"
      - "git+https://github.com/awslabs/cli-agent-orchestrator.git@main"
      - "cao-mcp-server"
---
```

Valid provider values: `kiro_cli`, `claude_code`, `codex`, `q_cli`, `gemini_cli`,
`kimi_cli`, `copilot_cli`.

## E2E Tests

See `test/e2e/test_cross_provider.py` for automated tests that verify the
cross-provider resolution works across Kiro CLI, Gemini CLI, and Claude Code.

```bash
uv run pytest -m e2e test/e2e/test_cross_provider.py -v -o "addopts="
```
