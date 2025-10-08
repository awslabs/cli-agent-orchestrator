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
