# GitHub Copilot CLI Provider

## Overview

The Copilot provider enables CLI Agent Orchestrator (CAO) to run **GitHub Copilot CLI** in tmux-managed sessions.

This provider targets the current Copilot CLI surface (latest versions) and does not include legacy compatibility fallbacks.

## Quick Start

### Prerequisites

1. **GitHub Copilot access** and successful `copilot login`
2. **Copilot CLI** installed (`copilot` command available)
3. **tmux** installed

```bash
# Install Copilot CLI
npm install -g @github/copilot

# Authenticate
copilot login

# Verify
copilot --version
copilot --help
```

### Using Copilot Provider with CAO

```bash
# Start CAO server
cao-server

# Install CAO agent profile into Copilot's agents directory
cao install examples/assign/data_analyst.md --provider copilot_cli

# Launch Copilot-backed terminal
cao launch --agents data_analyst --provider copilot_cli
```

Via HTTP API:

```bash
curl -X POST "http://localhost:9889/sessions?provider=copilot_cli&agent_profile=developer"
```

## Features

### Status Detection

The provider detects:

- **IDLE**: Copilot prompt visible, no pending response
- **PROCESSING**: no idle prompt yet / still running
- **WAITING_USER_ANSWER**: trust/confirmation prompt visible
- **COMPLETED**: response present and prompt returned
- **ERROR**: explicit error output detected

Trust handling is done in `initialize()`. `get_status()` is read-only.

### Message Extraction

`GET /terminals/{terminal_id}/output?mode=last` extracts the final assistant message by:

1. Finding output after last user prompt line
2. Trimming tail prompts/footer lines
3. Falling back to assistant-prefix extraction

### Agent Profile Integration

Copilot provider now follows the same split as other providers:

- `cao install --provider copilot_cli` writes `<name>.agent.md` into `~/.copilot/agents`
- provider launch passes `--agent <name>` directly
- provider does not generate runtime agent markdown files

This keeps provider logic thin and moves profile materialization to install-time.

### MCP Integration

CAO injects `cao-mcp-server` at runtime via:

- `--additional-mcp-config <json>`

Implementation:

- Add `cao-mcp-server` with `CAO_TERMINAL_ID`
- Pass the merged MCP payload inline as JSON

## Configuration

### Required Copilot CLI Flags

Provider expects these flags in your Copilot CLI:

- `--agent`
- `--additional-mcp-config`
- `--allow-all`
- `--autopilot`

When `--additional-mcp-config` is missing from your `copilot --help`, CAO skips MCP config injection.

### Command Shape

```bash
copilot --allow-all [--agent <name>] --config-dir ~/.copilot \
  --add-dir <cwd> --additional-mcp-config '{"mcpServers":{...}}' --autopilot
```

### Environment Variables

Copilot provider currently has no provider-specific environment knobs.

## Implementation Notes

- Provider file: `src/cli_agent_orchestrator/providers/copilot_cli.py`
- Exit command: `/exit`
- Paste behavior: single Enter (`paste_enter_count = 1`)

## End-to-End Testing

```bash
# Unit tests
uv run pytest test/providers/test_copilot_cli_unit.py -v
uv run pytest test/providers/test_provider_manager_unit.py -v

# Copilot E2E
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

1. **Copilot does not start**
   - Re-run `copilot login`
   - Verify `copilot --version`
   - Verify required flags in `copilot --help`

2. **Agent profile not applied**
   - Install profile: `cao install <profile>.md --provider copilot_cli`
   - Launch with `cao launch --agents <agent-name> --provider copilot_cli`

3. **MCP tools missing**
   - Ensure `cao-mcp-server` is resolvable in current environment

4. **Stuck in WAITING_USER_ANSWER**
   - Check active tmux pane for trust/confirmation prompt and answer once
