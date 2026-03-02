# GitHub Copilot CLI Provider

## Overview

The Copilot provider enables CLI Agent Orchestrator (CAO) to run GitHub Copilot CLI inside tmux-managed sessions for single-agent and multi-agent workflows.

## Quick Start

### Prerequisites

1. GitHub Copilot CLI installed
2. GitHub authentication completed for Copilot CLI
3. `tmux` installed

```bash
# Install GitHub Copilot CLI
npm install -g @github/copilot

# Authenticate
copilot login

# Verify installation
copilot --version
```

### Using Copilot Provider with CAO

```bash
# Start the CAO server
cao-server

# Launch a Copilot-backed session
cao launch --agents developer --provider copilot_cli
```

Via HTTP API:

```bash
curl -X POST "http://localhost:9889/sessions?provider=copilot_cli&agent_profile=developer"
```

## Custom Agents

CAO maps `--agents <name>` to Copilot agent behavior at launch time:

```bash
cao install examples/assign/analysis_supervisor.md
cao install examples/assign/data_analyst.md
cao install examples/assign/report_generator.md
```

- If `<name>` exists in CAO agent store, CAO materializes a runtime Copilot custom agent under `<config-dir>/agents/` and launches Copilot with `--agent <runtime-name>`.
- If `<name>` is not in CAO agent store, CAO passes it directly to Copilot (`--agent <name>`) so you can target an existing native Copilot custom agent.
- If installed Copilot CLI does not support `--agent`, CAO falls back to runtime prompt injection for compatibility.

CAO `mcpServers` are injected at runtime through `--additional-mcp-config`.

### Use Existing Native Copilot Agents

If you already created a native Copilot custom agent (for example `refactor-agent`):

```bash
cao launch --agents refactor-agent --provider copilot_cli
```

Equivalent raw Copilot CLI usage:

```bash
copilot --agent=refactor-agent --prompt "Refactor this code block"
```

## Features

### Status Detection

The Copilot provider detects terminal states:

- `IDLE`: Terminal is ready for input
- `PROCESSING`: Copilot is generating or executing
- `WAITING_USER_ANSWER`: Trust/confirmation prompt needs input
- `COMPLETED`: Response completed and prompt returned
- `ERROR`: Error output detected

Detection is noise-aware (footer/spinner/log churn) and uses a short stability window to avoid false positives.

### Message Extraction

`GET /terminals/{terminal_id}/output?mode=last` extracts the final meaningful assistant response by:

1. Anchoring on the last user prompt line when available
2. Falling back to assistant markers and filtered terminal tail

### Trust Prompt Handling

On startup, the provider auto-handles common trust/consent prompts (folder trust, `[y/n]`, `press enter to continue`).

## Configuration

CAO builds `copilot` flags dynamically based on `copilot --help` support checks.

Command shape:

```bash
copilot [permissive flags] [compat flags] [--agent <name>] --model <model> --config-dir <dir> \
  --add-dir <dir>... [--additional-mcp-config @<file>] [--autopilot]
```

Environment variables:

| Variable | Default | Description |
|---|---|---|
| `CAO_COPILOT_COMMAND` | unset | Full command override. |
| `CAO_COPILOT_MODEL` | `gpt-5-mini` | Model passed to `--model`. |
| `CAO_COPILOT_CONFIG_DIR` | `~/.copilot` | Copilot config directory (also contains `agents/`). |
| `CAO_COPILOT_AUTOPILOT` | `1` | Enable `--autopilot` when supported. |
| `CAO_COPILOT_ALLOW_ALL` | `1` | Enable permissive flag auto-selection. |
| `CAO_COPILOT_PERMISSIVE_FLAG` | `auto` | `auto`, `allow-all`, `yolo`, or `none`. |
| `CAO_COPILOT_NO_CUSTOM_INSTRUCTIONS` | `1` | Add `--no-custom-instructions` when supported. |
| `CAO_COPILOT_DISABLE_BUILTIN_MCPS` | `0` | Add `--disable-builtin-mcps` when supported. |
| `CAO_COPILOT_NO_AUTO_UPDATE` | `1` | Add `--no-auto-update` when supported. |
| `CAO_COPILOT_NO_ASK_USER` | `1` | Add `--no-ask-user` when supported. |
| `CAO_COPILOT_REASONING_EFFORT` | `high` | Writes `reasoning_effort` to Copilot config. |
| `CAO_COPILOT_ADDITIONAL_MCP_CONFIG` | unset | External MCP config merged into runtime config. |
| `CAO_COPILOT_CAO_MCP_COMMAND` | auto-resolved | Override command used for injected `cao-mcp-server`. |
| `CAO_COPILOT_ADD_DIRS` | current directory | Colon-separated `--add-dir` list. |

## End-to-End Testing

```bash
# Provider unit tests
uv run pytest test/providers/test_copilot_cli_unit.py -v
uv run pytest test/providers/test_provider_manager_unit.py -v

# Copilot E2E tests
uv run pytest -m e2e test/e2e/ -k copilot -v -o "addopts="
```

Maintainer-requested scenarios:

```bash
uv run pytest -m e2e test/e2e/test_assign.py::TestCopilotCliAssign::test_assign_data_analyst -v -o "addopts="
uv run pytest -m e2e test/e2e/test_assign.py::TestCopilotCliAssign::test_assign_report_generator -v -o "addopts="
uv run pytest -m e2e test/e2e/test_supervisor_orchestration.py::TestCopilotCliSupervisorOrchestration::test_supervisor_handoff -v -o "addopts="
uv run pytest -m e2e test/e2e/test_supervisor_orchestration.py::TestCopilotCliSupervisorOrchestration::test_supervisor_assign_and_handoff -v -o "addopts="
uv run pytest -m e2e test/e2e/test_supervisor_orchestration.py::TestCopilotCliSupervisorOrchestration::test_supervisor_assign_three_analysts -v -o "addopts="
```

## Troubleshooting

1. Copilot session does not start:
   - Re-run `copilot login`
   - Check `copilot --help` and `copilot --version`
   - Attach tmux and verify Copilot UI is running

2. Custom agent not found:
   - Install the profile into CAO store: `cao install <profile>.md`
   - Confirm the profile exists in `~/.aws/cli-agent-orchestrator/agent-store`
   - Launch with `cao launch --agents <agent-name> --provider copilot_cli`

3. MCP tools missing:
   - Ensure `cao-mcp-server` is resolvable on PATH
   - Or set `CAO_COPILOT_CAO_MCP_COMMAND`
   - Validate JSON at `CAO_COPILOT_ADDITIONAL_MCP_CONFIG`

## Implementation Notes

- Provider: `src/cli_agent_orchestrator/providers/copilot_cli.py`
- Runtime MCP config: `/tmp/cao_copilot_mcp_<terminal_id>.json`
- Exit command: `/exit`
