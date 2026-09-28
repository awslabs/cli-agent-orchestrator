---
sidebar_position: 3
---

# Profiles

Agent profiles are **Markdown files with YAML frontmatter** that define an agent's identity, behavior, tool access, and provider configuration. The markdown body becomes the agent's system prompt.

## Profile Format

```markdown
---
name: developer
description: Developer Agent in a multi-agent system
role: developer
provider: claude_code
allowedTools:
  - "@builtin"
  - "fs_*"
  - "execute_bash"
  - "@cao-mcp-server"
mcpServers:
  cao-mcp-server:
    type: stdio
    command: cao-mcp-server
    args: []
---

# Developer Agent

## Role and Identity
You are the Developer Agent. Write high-quality, maintainable code.

## Core Responsibilities
- Implement software solutions based on specifications
- Write clean, efficient, and well-documented code
- Create unit tests for your implementations
```

## Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Unique identifier for the agent |
| `description` | string | Brief description of the agent's purpose |

## Optional Fields

| Field | Type | Scope | Description |
|-------|------|-------|-------------|
| `role` | string | All | `"supervisor"`, `"developer"`, `"reviewer"`, or custom role |
| `provider` | string | All | Provider to run this agent on (e.g., `"claude_code"`, `"kiro_cli"`) |
| `allowedTools` | array | All | CAO tool vocabulary allowlist; overrides role defaults |
| `skills` | array | All | Restrict injected skill catalog (fnmatch globs); omit for full catalog |
| `mcpServers` | object | All | MCP server configurations for additional tools |
| `tools` | array | All | List of allowed tools; `["*"]` for all |
| `model` | string | All | AI model to use |
| `permissionMode` | string | Claude Code | `"default"`, `"acceptEdits"`, `"plan"`, `"auto"`, `"bypassPermissions"` |
| `native_agent` | string | Claude Code | Name of a native Claude Code agent (thin-wrapper mode) |
| `codexProfile` | string | Codex | Names a `[profiles.<name>]` block in `~/.codex/config.toml` |
| `codexConfig` | object | Codex | Inline config overrides passed as `-c key=value` |
| `hermesProfile` | string | Hermes | Hermes profile wrapper command |

## Built-in Profiles

CAO ships several built-in profiles in the agent store:

```bash
# List available profiles (built-in + installed)
cao profile list
```

Common built-ins: `code_supervisor`, `developer`, `reviewer`.

## Installing Profiles

```bash
# Install a built-in profile by name
cao install code_supervisor
cao install developer
cao install reviewer

# Install from a local file
cao install ./my-custom-agent.md

# Install from a URL
cao install https://example.com/agents/custom-agent.md
```

## Profile Management Commands

```bash
# List all installed profiles
cao profile list

# Inspect a profile's frontmatter and details
cao profile show developer

# Validate schema and check for deprecations
cao profile validate developer

# List scaffolding templates
cao profile templates

# Generate a profile from a template
cao profile create -t aws/stepfunction -c config.json

# Remove an installed profile
cao profile remove my-custom-agent
```

## Profile Discovery

CAO discovers profiles by scanning directories in this order (first match wins):

1. **Local agent store** -- `~/.aws/cli-agent-orchestrator/agent-store/`
2. **Provider-specific directories** -- configured in `settings.json` under `agents.dirs`
3. **Extra custom directories** -- `agents.extra_dirs` in settings
4. **Built-in store** -- bundled with the CAO package

Configure additional scan directories:

```bash
cao config set agents.extra_dirs '["/path/to/my/agents"]'
```

## Tool Restrictions

The `role` and `allowedTools` fields control what CAO tools an agent can access. The defaults per role are:

- **supervisor** -- `@cao-mcp-server`, `fs_read`, `fs_list`
- **developer** -- `@builtin`, `fs_*`, `execute_bash`, `web_fetch`, `@cao-mcp-server`
- **reviewer** -- `@builtin`, `fs_read`, `fs_list`, `@cao-mcp-server`

`allowedTools` always overrides role defaults when set. The `--yolo` flag bypasses all restrictions.

## Example: Cross-Provider Supervisor

```markdown
---
name: orchestrator
description: Cross-provider task orchestrator
role: supervisor
provider: kiro_cli
skills:
  - "cao-supervisor-*"
allowedTools:
  - "handoff"
  - "assign"
  - "send_message"
  - "delete_terminal"
  - "memory_store"
  - "memory_recall"
  - "load_skill"
---

# Orchestrator

You are the supervisor agent. Delegate implementation to `developer` (runs on
Claude Code) and reviews to `reviewer` (runs on Codex). Collect results and
synthesize a final summary.
```
