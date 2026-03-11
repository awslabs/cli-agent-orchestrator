# Agent Profile Format

Agent profiles are markdown files with YAML frontmatter that define an agent's behavior and configuration.

## Structure

```markdown
---
name: agent-name
description: Brief description of the agent
# Optional configuration fields
---

# System prompt content

The markdown content becomes the agent's system prompt.
Define the agent's role, responsibilities, and behavior here.
```

## Required Fields

- `name` (string): Unique identifier for the agent
- `description` (string): Brief description of the agent's purpose

## Optional Fields

- `provider` (string): Provider to run this agent on (e.g., `"claude_code"`, `"kiro_cli"`). See [Cross-Provider Orchestration](#cross-provider-orchestration).
- `mcpServers` (object): MCP server configurations for additional tools
- `tools` (array): List of allowed tools, use `["*"]` for all
- `allowedTools` (array): Whitelist of tools (e.g., `["@builtin", "@cao-mcp-server"]`)
- `toolAliases` (object): Map tool names to aliases
- `toolsSettings` (object): Tool-specific configuration
- `model` (string): AI model to use
- `prompt` (string): Additional prompt text

## Example

```markdown
---
name: developer
description: Developer Agent in a multi-agent system
mcpServers:
  cao-mcp-server:
    type: stdio
    command: uvx
    args:
      - "--from"
      - "git+https://github.com/awslabs/cli-agent-orchestrator.git@main"
      - "cao-mcp-server"
---

# DEVELOPER AGENT

## Role and Identity
You are the Developer Agent in a multi-agent system. Your primary responsibility is to write high-quality, maintainable code based on specifications.

## Core Responsibilities
- Implement software solutions based on provided specifications
- Write clean, efficient, and well-documented code
- Follow best practices and coding standards
- Create unit tests for your implementations

## Critical Rules
1. **ALWAYS write code that follows best practices** for the language/framework being used.
2. **ALWAYS include comprehensive comments** in your code to explain complex logic.
3. **ALWAYS consider edge cases** and handle exceptions appropriately.
```

## Cross-Provider Orchestration

Agent profiles can declare which provider they should run on via the `provider` key. This enables mixed-provider workflows where a supervisor on one provider delegates to workers on different providers.

When the supervisor calls `assign` or `handoff`, CAO reads the worker's agent profile and uses the declared `provider` if it is a valid value. If the key is missing or the value is not recognized, the worker inherits the supervisor's provider.

Valid values: `q_cli`, `kiro_cli`, `claude_code`, `codex`, `gemini_cli`.

### Example

A Kiro CLI supervisor delegating to a Claude Code developer:

```markdown
---
name: supervisor
description: Code Supervisor
provider: kiro_cli
---

You orchestrate tasks across developer and reviewer agents.
```

```markdown
---
name: developer
description: Developer Agent
provider: claude_code
---

You write code based on specifications.
```

```markdown
---
name: reviewer
description: Code Reviewer
# No provider key — inherits from supervisor (kiro_cli)
---

You review code for quality and correctness.
```

> **Note:** The `cao launch --provider` CLI flag is an explicit override and always takes precedence over the profile's `provider` key for the initial session.

## Installation

```bash
# From local file
cao install ./my-agent.md

# From URL
cao install https://example.com/agents/my-agent.md

# By name (built-in or previously installed)
cao install developer
```

## Built-in Agents

CAO includes these built-in profiles:
- `code_supervisor`: Coordinates development tasks
- `developer`: Writes code
- `reviewer`: Performs code reviews

View the [agent_store directory](https://github.com/awslabs/cli-agent-orchestrator/tree/main/src/cli_agent_orchestrator/agent_store) for examples.
