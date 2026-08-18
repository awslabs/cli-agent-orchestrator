---
sidebar_position: 1
---

# CLI Commands

Complete reference for all CAO CLI commands.

## Global Options

```bash
cao [command] --help    # Show help for any command
cao --version           # Show version
```

## Entry Points

| Binary | Description |
|--------|-------------|
| `cao` | Main CLI |
| `cao-server` | API server (default port 9889) |
| `cao-mcp-server` | MCP server |
| `cao-ops-mcp-server` | Ops MCP server |

## Command Overview

| Command | Description |
|---------|-------------|
| `cao launch` | Launch a session with an agent profile |
| `cao session` | Manage CAO sessions (list, status, send) |
| `cao shutdown` | Shutdown sessions |
| `cao profile` | Manage agent profiles (list, show, find, validate, remove, templates, create) |
| `cao schedule` | Manage scheduled flows (add, list, remove, disable, enable, run) |
| `cao config` | Configuration management |
| `cao init` | Initialize CAO |
| `cao install` | Install agent profiles |
| `cao env` | Environment management |
| `cao mcp-server` | Start MCP server inline |
| `cao info` | Show system info |
| `cao memory` | Memory management |
| `cao skills` | Skills management |
| `cao terminal` | Terminal management |
| `cao workflow` | Workflow management |
| `cao update` | Update CAO to the latest version |
| `cao flow` | **[Deprecated]** Alias for `schedule` |

---

## cao launch

Launch a session with an agent profile. This is the primary command for starting agent work.

```bash
cao launch [MESSAGE] --agents PROFILE [OPTIONS]
```

### Options

| Option | Description |
|--------|-------------|
| `--agents TEXT` | Agent profile to launch **(required)** |
| `--session-name TEXT` | Name of the session (default: auto-generated) |
| `--headless` | Launch in detached mode (no interactive terminal) |
| `--provider TEXT` | Provider to use (default: profile provider or `kiro_cli`) |
| `--allowed-tools TEXT` | Override allowedTools (repeatable) |
| `--async` | Send message and return immediately |
| `--auto-approve` | Skip confirmation prompt |
| `--yolo` | **[DANGEROUS]** Unrestricted tool access |
| `--working-directory TEXT` | Working directory (default: current directory) |
| `--memory` | Also launch a memory_manager terminal |
| `--env KEY=VALUE` | Forward environment variable to session (repeatable) |

### Examples

```bash
# Launch with a profile and an initial message
cao launch "Fix the failing tests in src/" --agents code-reviewer

# Launch in headless (detached) mode
cao launch "Run the full test suite" --agents test-runner --headless

# Launch with a custom session name
cao launch "Deploy to staging" --agents deployer --session-name staging-deploy

# Launch with a specific provider
cao launch "Refactor the auth module" --agents refactorer --provider claude_code

# Launch with environment variables forwarded
cao launch "Build the project" --agents builder --env AWS_REGION=us-west-2 --env STAGE=beta

# Launch asynchronously (fire and forget)
cao launch "Generate weekly report" --agents reporter --async

# Launch with memory manager
cao launch "Research this codebase" --agents explorer --memory
```

---

## cao session

Manage active CAO sessions.

:::note Session names include the `cao-` prefix
`cao launch --session-name my-session` creates a session named `cao-my-session`.
The subcommands below take the full prefixed name; `cao session list` shows the
exact values.
:::

### cao session list

List all active sessions.

```bash
cao session list [--json]
```

| Option | Description |
|--------|-------------|
| `--json` | Output in JSON format |

### cao session status

Show status of a specific session.

```bash
cao session status SESSION_NAME [--terminal ID] [--workers] [--json]
```

| Option | Description |
|--------|-------------|
| `--terminal ID` | Show status of a specific terminal within the session |
| `--workers` | Include worker status |
| `--json` | Output in JSON format |

### cao session send

Send a message to a running session.

```bash
cao session send SESSION_NAME MESSAGE [--terminal ID] [--async] [--timeout N]
```

| Option | Description |
|--------|-------------|
| `--terminal ID` | Target a specific terminal within the session |
| `--async` | Send and return immediately without waiting for response |
| `--timeout N` | Timeout in seconds to wait for response |

### Examples

```bash
# List all sessions
cao session list

# List sessions in JSON format
cao session list --json

# Check status of a session
cao session status cao-my-session

# Check status including workers
cao session status cao-my-session --workers

# Send a follow-up message to a session
cao session send cao-my-session "Now run the integration tests"

# Send a message to a specific terminal
cao session send cao-my-session "Check the logs" --terminal a1b2c3d4

# Send asynchronously
cao session send cao-my-session "Generate the report" --async

# Send with a timeout
cao session send cao-my-session "Run benchmarks" --timeout 300
```

---

## cao shutdown

Shutdown one or all sessions.

```bash
cao shutdown --all
cao shutdown --session NAME
```

| Option | Description |
|--------|-------------|
| `--all` | Shutdown all active sessions |
| `--session NAME` | Shutdown a specific session by name |

### Examples

```bash
# Shutdown all sessions
cao shutdown --all

# Shutdown a specific session
cao shutdown --session cao-staging-deploy
```

---

## cao profile

Manage agent profiles. Profiles define agent behavior, tools, and configuration.

### cao profile list

List all installed profiles.

```bash
cao profile list
```

### cao profile show

Display the full contents of a profile.

```bash
cao profile show NAME_OR_PATH
```

### cao profile validate

Validate a profile for correctness.

```bash
cao profile validate NAME_OR_PATH
```

### cao profile remove

Remove an installed profile.

```bash
cao profile remove NAME [-y]
```

| Option | Description |
|--------|-------------|
| `-y` | Skip confirmation prompt |

### cao profile find

Search profiles by keyword.

```bash
cao profile find QUERY [--limit N] [--json]
```

| Option | Description |
|--------|-------------|
| `--limit N` | Maximum results to return (default: 10) |
| `--json` | Output in JSON format |

### cao profile templates

List available profile templates.

```bash
cao profile templates
```

### cao profile create

Create a new profile from a template.

```bash
cao profile create --template NAME --config FILE [--output-dir DIR]
```

| Option | Description |
|--------|-------------|
| `--template NAME` | Template to base the profile on, namespaced e.g. `aws/sqs-monitor` **(required)**. Run `cao profile templates` to list. |
| `--config FILE` | Configuration file for profile creation **(required)** |
| `--output-dir DIR` | Directory to write the profile to |

### Examples

```bash
# List all installed profiles
cao profile list

# Show a profile's configuration
cao profile show code-reviewer

# Validate a profile file
cao profile validate ./my-profile.md

# List available templates
cao profile templates

# Create a new profile from a template
cao profile create --template aws/sqs-monitor --config ./my-config.json

# Create a profile with a custom output directory
cao profile create --template aws/stepfunction --config ./config.json --output-dir ./agents

# Remove a profile (with confirmation)
cao profile remove old-profile

# Remove a profile without confirmation
cao profile remove old-profile -y
```

---

## cao schedule

Manage scheduled flows. Flows are automated sequences of agent operations that can run on a schedule.

### cao schedule add

Add a new scheduled flow from a file.

```bash
cao schedule add FILE_PATH
```

### cao schedule list

List all scheduled flows.

```bash
cao schedule list
```

### cao schedule remove

Remove a scheduled flow.

```bash
cao schedule remove NAME
```

### cao schedule disable

Disable a scheduled flow (keeps configuration but stops execution).

```bash
cao schedule disable NAME
```

### cao schedule enable

Re-enable a disabled scheduled flow.

```bash
cao schedule enable NAME
```

### cao schedule run

Manually trigger a scheduled flow.

```bash
cao schedule run NAME
```

### Examples

```bash
# Add a new scheduled flow
cao schedule add ./flows/nightly-tests.md

# List all scheduled flows
cao schedule list

# Manually run a flow
cao schedule run nightly-tests

# Disable a flow temporarily
cao schedule disable nightly-tests

# Re-enable it
cao schedule enable nightly-tests

# Remove a flow entirely
cao schedule remove nightly-tests
```

---

## cao config

Configuration management for CAO settings.

```bash
cao config
```

---

## cao init

Initialize CAO in the current directory or environment.

```bash
cao init
```

---

## cao install

Install an agent profile. `AGENT_SOURCE` is required and may be a built-in
profile name, a local `.md` path, or a URL on an allowlisted host
(`github.com`, `raw.githubusercontent.com` by default).

```bash
cao install AGENT_SOURCE [--provider PROVIDER] [--env KEY=VALUE]
```

| Option | Description |
|--------|-------------|
| `--provider PROVIDER` | Provider to install for. Precedence: this flag, then the profile's `provider:` frontmatter, then `kiro_cli` |
| `--env KEY=VALUE` | Substitute a value into `${VAR}` placeholders in the profile (repeatable) |

```bash
# Install a built-in profile
cao install code_supervisor

# Install from a local file
cao install ./my-agent.md

# Install for a specific provider
cao install developer --provider claude_code
```

---

## cao update

Update CAO to the latest version. Automatically detects how CAO was installed (git, PyPI registry, or local) and runs the appropriate upgrade command.

```bash
cao update
```

### Behavior by install method

| Install source | What `cao update` does |
|----------------|------------------------|
| PyPI (`uv tool install cli-agent-orchestrator`) | Runs `uv tool upgrade cli-agent-orchestrator` |
| PyPI with version pin (`==2.3.0`, `<3.0`) | Runs `uv tool install cli-agent-orchestrator@latest --upgrade` (unpins) |
| Git (`uv tool install git+...`) | Runs `uv tool install <git-source> --upgrade --reinstall` |
| Local directory / editable | Prints guidance (cannot auto-update a local install) |

After updating, restart any running `cao-server` to pick up the new version.

### Examples

```bash
# Update CAO
cao update

# If installed from git, it will fetch the latest commits:
# $ uv tool install git+https://github.com/awslabs/cli-agent-orchestrator.git@main --upgrade --reinstall
```

---

## cao env

Manage environment settings for CAO sessions.

```bash
cao env
```

---

## cao mcp-server

Start an MCP server inline (within the current process).

```bash
cao mcp-server
```

---

## cao info

Display information about the current CAO session (database path, active session context). For version info, use `cao --version`.

```bash
cao info
```

---

## cao memory

Memory management for agent sessions.

```bash
cao memory
```

---

## cao skills

Skills management for agent capabilities.

```bash
cao skills
```

---

## cao terminal

Terminal management for multi-terminal sessions.

```bash
cao terminal
```

---

## cao workflow

Workflow management for complex multi-step operations.

```bash
cao workflow
```

---

## Providers

CAO supports multiple agent providers. Specify a provider with the `--provider` flag on `cao launch`.

| Provider | Description |
|----------|-------------|
| `kiro_cli` | Kiro CLI (default) |
| `claude_code` | Claude Code |
| `codex` | OpenAI Codex |
| `kimi_cli` | Kimi CLI |
| `copilot_cli` | GitHub Copilot CLI |
| `opencode_cli` | OpenCode CLI |
| `omp` | Oh My Pi |
| `hermes` | Hermes |
| `cursor_cli` | Cursor CLI |
| `antigravity_cli` | Antigravity CLI |

### Example

```bash
# Launch with Claude Code as the provider
cao launch "Analyze this codebase" --agents analyst --provider claude_code

# Launch with Codex
cao launch "Write unit tests" --agents test-writer --provider codex
```

---

## Deprecated Commands

| Command | Replacement |
|---------|-------------|
| `cao flow` | Use `cao schedule` instead |
