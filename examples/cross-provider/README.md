# Cross-Provider Examples

Agent profiles that declare a `provider` key in their frontmatter, enabling
cross-provider workflows where a supervisor on one provider delegates to workers
on different providers.

## Profiles

| Profile | Provider Override | Description |
|---------|------------------|-------------|
| `data_analyst_claude_code.md` | `claude_code` | Data analyst that runs on Claude Code |
| `data_analyst_gemini_cli.md` | `gemini_cli` | Data analyst that runs on Gemini CLI |
| `data_analyst_kiro_cli.md` | `kiro_cli` | Data analyst that runs on Kiro CLI |

Each profile is identical to `examples/assign/data_analyst.md` except for the
added `provider` field in the frontmatter.

## Installation

```bash
cao install examples/cross-provider/data_analyst_claude_code.md
cao install examples/cross-provider/data_analyst_gemini_cli.md
cao install examples/cross-provider/data_analyst_kiro_cli.md
```

## Usage

Start a session on one provider and assign a worker using a cross-provider profile:

```bash
# Start a Kiro CLI supervisor session
cao launch --provider kiro_cli --agent-profile data_analyst --session-name my-session

# The supervisor can then assign tasks to workers on different providers.
# When it calls assign() with data_analyst_gemini_cli, CAO reads the profile's
# provider key and launches the worker on Gemini CLI instead of Kiro CLI.
```

## E2E Tests

See `test/e2e/test_cross_provider.py` for automated tests that verify the
cross-provider resolution works across Kiro CLI, Gemini CLI, and Claude Code.

```bash
uv run pytest -m e2e test/e2e/test_cross_provider.py -v -o "addopts="
```
