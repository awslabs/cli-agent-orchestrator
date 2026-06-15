# Codex CLI Provider

## Overview

The Codex CLI provider enables CLI Agent Orchestrator (CAO) to work with **Codex CLI** (OpenAI's coding agent) through your OpenAI API key, allowing you to orchestrate multiple Codex-based agents.

## Quick Start

### Prerequisites

1. **OpenAI API Key** or **ChatGPT Subscription**: Authentication for Codex CLI
2. **Codex CLI**: Install the CLI tool via npm
3. **tmux**: Required for terminal management

```bash
# Install Codex CLI
npm install -g @openai/codex

# Authenticate (set API key)
export OPENAI_API_KEY=your-key-here
# Or use interactive login
codex login
```

### Using Codex Provider with CAO

Create a terminal using the Codex provider:

```bash
# Start the CAO server in one terminal
cao-server

# In another terminal, launch a Codex-backed CAO session
cao launch --agents codex_developer --provider codex
```

You can also create a session via HTTP API (query parameters):

```bash
curl -X POST "http://localhost:9889/sessions?provider=codex&agent_profile=codex_developer"
```

## Features

### Status Detection

The Codex provider automatically detects terminal states:

- **IDLE**: Terminal is ready for input
- **PROCESSING**: Codex is thinking or working
- **WAITING_USER_ANSWER**: Waiting for user approval/confirmation
- **COMPLETED**: Task finished with assistant response
- **ERROR**: Error occurred during execution

The provider supports two output formats for status detection:

- **Label style**: `You ...` / `assistant: ...` (synthetic/test format)
- **Bullet style**: `› user message` / `• response` (real Codex interactive mode)

The `USER_PREFIX_PATTERN` uses `[^\S\n]` (horizontal whitespace only) to prevent matching across newline boundaries, correctly distinguishing `› ` (idle prompt) from `› text` (user input).

### Message Extraction

The provider automatically extracts the last assistant response from terminal output using a two-phase approach:

1. **Primary**: Finds the last user message (`You ...` or `› text`) and extracts everything between it and the next empty idle prompt
2. **Fallback**: Uses assistant markers (`assistant:` or `•`) when no user message is found

This works for both the label format (`assistant: response`) and Codex's native bullet format (`• response with multiple bullets`).

## Configuration

CAO's Codex provider launches `codex` with tmux-compatible flags and relies on your existing Codex CLI configuration/authentication.

- `--provider codex` selects the provider.
- `--agents <name>` specifies the agent profile. When an agent profile with a `system_prompt` is provided, it is injected into Codex as `developer_instructions` via the `-c` config override flag.
- Model/timeout/approval settings are configured in Codex CLI itself (outside of CAO).

### Agent Profile Integration

When you launch with an agent profile (e.g., `--agents code_supervisor`), CAO:

1. Loads the agent profile from the agent store (built-in or `~/.aws/cli-agent-orchestrator/agent_store/`)
2. Extracts the `system_prompt` from the profile's Markdown content
3. Passes it to Codex via `-c developer_instructions="<prompt>"`, which Codex injects as a developer role message

This enables Codex to operate with role-specific instructions (e.g., supervisor, developer, reviewer) just like other providers.

### MCP Server Integration

If the agent profile includes `mcpServers`, CAO injects each MCP server into Codex via `-c mcp_servers.<name>.<field>=<value>` config overrides. This is per-session and does not modify the user's global `~/.codex/config.toml`.

For example, the `code_supervisor` profile includes the `cao-mcp-server` which provides `handoff` and `send_message` tools. This allows the supervisor agent to delegate work to Developer and Reviewer agents through CAO's multi-agent orchestration.

CAO also sets `tool_timeout_sec=600.0` (10 minutes) for each MCP server to allow long-running operations like handoff. **Important**: The value must be a TOML float (`600.0`, not `600`) because Codex deserializes this field via `Option<f64>`. A TOML integer is silently rejected, falling back to the 60-second default.

### Memory Injection

When CAO's memory system is enabled, the built-in `codex_memory` plugin auto-injects
relevant memories into the project on terminal creation. On `post_create_terminal` for a
`codex` terminal, it writes a delimited block into `<cwd>/AGENTS.md` — the file Codex CLI
reads from the working directory as project instructions:

```markdown
<!-- cao-memory:begin -->
<cao-memory>
## Context from CAO Memory
- [project] testing-framework: Always use pytest for this project
...
</cao-memory>
<!-- cao-memory:end -->
```

Because `AGENTS.md` is a user-authored, repo-root file, the plugin owns **only** the
delimited block and replaces it in place on each run — any hand-written content around it
is preserved (the same approach as the Claude Code `CLAUDE.md` plugin, not Kiro's
whole-file ownership). The plugin is observer-only: it runs after the terminal is created,
logs-and-skips on any error, and never crashes `cao-server`. It writes nothing when memory
is disabled or there are no relevant memories. See [memory.md](memory.md) for the full
memory system.

### Launch Flags

The Codex provider automatically adds these flags for tmux compatibility:

- `--no-alt-screen`: Runs Codex in inline mode so output stays in normal scrollback, making `tmux capture-pane` reliable
- `--disable shell_snapshot`: Prevents TTY input conflicts (SIGTTIN) caused by the shell_snapshot subprocess inheriting stdin in tmux

By default, CAO also passes `--yolo` (alias for `--dangerously-bypass-approvals-and-sandbox`) because CAO agents run in non-interactive tmux sessions where approval prompts block handoff/assign flows. Profiles can opt out via `codexProfile`; see [Custom Codex Profile](#custom-codex-profile). Any unrestricted allowed-tools configuration (`allowedTools: ["*"]`, `--allowed-tools '*'`, or `cao launch --yolo`) forces `--yolo` regardless of the profile setting.

### Custom Codex Profile

The `codexProfile` field on an agent profile names a `[profiles.<name>]` block in your `~/.codex/config.toml`. When set, CAO drops `--yolo` and passes `--profile <name>` instead, letting the user's named profile govern sandbox and approval behavior. Unrestricted allowed tools (`allowedTools: ["*"]`, `--allowed-tools '*'`, or `cao launch --yolo`) override this field and always force `--yolo`.

**Important — non-interactive only**: CAO's status detector cannot interact with Codex's current boxed approval UI (`Command Approval Required / [a] Accept / [d] Decline`). Any `codexProfile` you reference MUST resolve to a non-interactive permission tier, or CAO sessions will time out waiting for input that nothing can deliver. Safe shapes:

- **Read-only / audit agents**: `approval_policy = "never"` + `sandbox_mode = "read-only"` — write/network/escape attempts fail closed and return errors to the model.
- **Write-permitted agents**: `approval_policy = "never"` + `sandbox_mode = "workspace-write"` — writes inside the workspace proceed; sandbox escapes fail closed.
- **Smart Approvals (classifier-gated)**: `approval_policy = "on-request"` + `sandbox_mode = "workspace-write"` + `approvals_reviewer = "auto_review"` — the auto-review classifier decides escalations; denials fail closed without prompting.

Avoid `approval_policy = "untrusted"` or `approval_policy = "on-request"` without `approvals_reviewer = "auto_review"` — those tiers prompt the user, which CAO cannot answer.

Example — a reviewer that runs under Codex's read-only sandbox:

```markdown
---
name: reviewer
description: Code Reviewer
provider: codex
role: reviewer
codexProfile: cao_reviewer
---

You review code for quality and correctness.
```

Matching `~/.codex/config.toml`:

```toml
[profiles.cao_reviewer]
sandbox_mode = "read-only"
approval_policy = "never"
```

## Workflows

### 1. Interactive single-agent task

```bash
cao launch --agents codex_developer --provider codex
```

In the tmux window, type your prompt at the Codex prompt.

To get the CAO terminal id (useful for API automation / MCP), run:

```bash
echo "$CAO_TERMINAL_ID"
```

### 2. Automate send/get-output via HTTP API

```bash
python3 - <<'PY'
import time

import requests

terminal_id = "<terminal-id>"

requests.post(
    f"http://localhost:9889/terminals/{terminal_id}/input",
    params={"message": "Please review this Python code for security issues"},
).raise_for_status()

# Poll status until completion
while True:
    status = requests.get(f"http://localhost:9889/terminals/{terminal_id}").json()["status"]
    if status in {"completed", "error", "waiting_user_answer"}:
        break
    time.sleep(1)

resp = requests.get(
    f"http://localhost:9889/terminals/{terminal_id}/output",
    params={"mode": "last"},
)
resp.raise_for_status()
print(resp.json()["output"])
PY
```

## Authentication

### OpenAI API Key Setup

1. **Install Codex CLI**:
   ```bash
   npm install -g @openai/codex
   ```

2. **Authenticate** (choose one):
   ```bash
   # Option 1: Set environment variable
   export OPENAI_API_KEY=your-key-here

   # Option 2: Interactive login
   codex login
   ```

3. **Verify Installation**:
   ```bash
   codex --version
   ```

## Troubleshooting

### Common Issues

1. **Authentication Failed**:
   ```bash
   # Re-authenticate
   codex logout
   codex login
   # Or set API key directly
   export OPENAI_API_KEY=your-key-here
   ```

2. **Timeout / Hanging Tasks**:
   - Confirm `codex` works in a regular shell (`codex`, then exit)
   - Attach to the tmux session and check whether Codex is waiting for input/approval
   - Verify your OpenAI API key or ChatGPT subscription and network connectivity

3. **Status Detection Problems**:
   - Check terminal history for unexpected prompts
   - Verify Codex CLI version compatibility
   - Review custom prompt patterns

## Implementation Notes

- Command building is handled by `CodexProvider._build_codex_command()` which constructs the launch command with flags and optional `developer_instructions`.
- A warm-up `echo ready` command is sent before launching Codex to prevent immediate exit in fresh tmux sessions.
- Workspace trust prompts are auto-accepted by `CodexProvider._handle_trust_prompt()` during initialization.
- Status detection uses a bottom-N-lines approach (`IDLE_PROMPT_TAIL_LINES = 5`) to check the last few lines for the idle prompt, since `--no-alt-screen` mode keeps history in scrollback.
- `ASSISTANT_PREFIX_PATTERN` matches both `assistant:` (label style) and `•` (Codex bullet style) for detecting assistant responses after user messages.
- `USER_PREFIX_PATTERN` matches both `You` (label style) and `› text` (Codex interactive prompt), using `[^\S\n]` to prevent crossing newline boundaries.
- `IDLE_PROMPT_STRICT_PATTERN` matches only empty prompt lines (`› ` or `❯ ` without trailing text) for extraction boundary detection.
- Output mode `last` uses `CodexProvider.extract_last_message_from_script()`, which extracts text between the last user message and the next idle prompt.
- Exiting a Codex terminal uses `/exit` (`POST /terminals/{terminal_id}/exit`).
- **Handoff message context**: `_handoff_impl()` prepends a `[CAO Handoff]` prefix to the task message so the worker agent knows this is a blocking handoff. Without this, Codex agents proactively try to use `send_message` to notify the supervisor, which fails because the worker doesn't have the supervisor's terminal ID. The prefix tells the agent to simply output results and finish — the orchestrator captures the response automatically.
- **TUI footer handling** (`--no-alt-screen` mode): Codex always renders a TUI footer at the bottom, even during processing. The footer format varies by version: v0.110 and earlier use `› [suggestion hint]` + `? for shortcuts` + `N% context left`; v0.111+ (PR #13202) use `› [suggestion hint]` + `model · N% left · path`. `TUI_FOOTER_PATTERN` detects both formats, and `_compute_tui_footer_cutoff()` finds the precise start of the footer area. Both `get_status()` and `extract_last_message_from_script()` use this cutoff to exclude footer lines from user-message matching — preventing false IDLE and extraction contamination.
- **TUI progress spinner**: During processing, Codex shows `• [text] (Ns • esc to interrupt)` inline. The `•` would falsely match `ASSISTANT_PREFIX_PATTERN`, and the TUI `›` hint would match idle prompt — triggering false COMPLETED. `TUI_PROGRESS_PATTERN` detects the spinner and returns PROCESSING before the COMPLETED check.

### Status Values

- `TerminalStatus.IDLE`: Ready for input
- `TerminalStatus.PROCESSING`: Working on task
- `TerminalStatus.WAITING_USER_ANSWER`: Waiting for user input
- `TerminalStatus.COMPLETED`: Task finished
- `TerminalStatus.ERROR`: Error occurred

## Best Practices

### 1. Agent Naming

Use descriptive names for Codex agents:
- `codex-frontend-dev` - Frontend development
- `codex-security-reviewer` - Security code review
- `codex-api-designer` - API design and documentation

### 2. Task Breakdown

Break complex tasks into smaller, focused prompts:
```python
# Instead of:
"Build a complete web application"

# Use:
"Design the database schema for user authentication"
"Implement the authentication API endpoints"
"Create the login form component"
"Write tests for the authentication flow"
```



## End-to-End Testing

The E2E test suite validates handoff, assign, and send_message flows against real CLI providers.

### Test Structure

```
test/e2e/
├── conftest.py                        # Shared fixtures (server health, CLI checks, helpers)
├── test_handoff.py                    # Worker lifecycle tests (handoff) — 10 tests (2 per provider)
├── test_assign.py                     # Worker lifecycle tests (assign) — 10 tests (2 per provider)
├── test_send_message.py               # Inbox delivery tests — 5 tests (1 per provider)
└── test_supervisor_orchestration.py   # Supervisor→worker delegation tests — 10 tests (2 per provider)
```

### Prerequisites

- Running CAO server: `uv run cao-server`
- Authenticated CLI tools: `codex`, `claude`, `kiro-cli`
- tmux installed
- Agent profiles installed: `analysis_supervisor`, `data_analyst`, `report_generator`
  ```bash
  cao install examples/assign/analysis_supervisor.md
  cao install examples/assign/data_analyst.md
  cao install examples/assign/report_generator.md
  ```

### Running E2E Tests

```bash
# Run all E2E tests (all providers)
uv run pytest -m e2e test/e2e/ -v

# Run for a specific provider
uv run pytest -m e2e test/e2e/ -v -k codex
uv run pytest -m e2e test/e2e/ -v -k claude_code
uv run pytest -m e2e test/e2e/ -v -k kiro_cli

# Run a specific test type
uv run pytest -m e2e test/e2e/test_handoff.py -v
uv run pytest -m e2e test/e2e/test_assign.py -v
uv run pytest -m e2e test/e2e/test_send_message.py -v
uv run pytest -m e2e test/e2e/test_supervisor_orchestration.py -v -o "addopts="
```

E2E tests are excluded from default `pytest` runs via the `-m 'not e2e'` addopts in `pyproject.toml`.

## Examples

See the `examples/` directory for a step-by-step walkthrough:
- `examples/codex-basic/` - Basic Codex usage (includes three agent profiles)
- `examples/assign/` - Assign (async parallel) workflow with data analysts and report generator

## Contributing

To contribute to the Codex provider:

1. Fork the repository
2. Create a feature branch
3. Add tests for new functionality
4. Update documentation
5. Submit a pull request

## Support

For issues and questions:
- GitHub Issues: [cli-agent-orchestrator](https://github.com/awslabs/cli-agent-orchestrator/issues)
- Documentation: [Codex CLI Provider Docs](https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/codex-cli.md)
