---
sidebar_position: 1
---

# Multi-Provider Support

CAO orchestrates AI coding CLI agents from multiple providers within the same session. A supervisor on one provider can delegate to workers running on entirely different providers, enabling you to leverage each tool's strengths.

## Supported Providers

| Provider | Enum Value | Authentication | Documentation |
|----------|-----------|----------------|---------------|
| **Kiro CLI** (default) | `kiro_cli` | AWS credentials | [Provider docs](https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/kiro-cli.md) |
| **Claude Code** | `claude_code` | Anthropic API key or subscription | [Provider docs](https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/claude-code.md) |
| **Codex CLI** | `codex` | OpenAI API key | [Provider docs](https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/codex-cli.md) |
| **Hermes Agent** | `hermes` | Hermes auth | [Provider docs](https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/hermes.md) |
| **Kimi CLI** | `kimi_cli` | Moonshot API key | [Provider docs](https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/kimi-cli.md) |
| **GitHub Copilot CLI** | `copilot_cli` | GitHub auth | [Provider docs](https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/copilot-cli.md) |
| **OpenCode CLI** | `opencode_cli` | Per-model API key | [Provider docs](https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/opencode-cli.md) |
| **Oh My Pi** | `omp` | OMP authenticated model account | [Provider docs](https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/omp-cli.md) |
| **Cursor CLI** | `cursor_cli` | Cursor subscription / API key | [Provider docs](https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/cursor-cli.md) |
| **Antigravity CLI** | `antigravity_cli` | Google account | [Provider docs](https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/antigravity-cli.md) |

## Specifying a Provider

### At launch time (CLI flag)

```bash
# Launch with a specific provider
cao launch --agents code_supervisor --provider claude_code

# Valid values: kiro_cli | claude_code | codex | antigravity_cli |
#              hermes | kimi_cli | copilot_cli | opencode_cli | omp | cursor_cli
```

### In an agent profile (frontmatter)

Pin a profile to a provider so it always runs on that CLI regardless of how the session was launched:

```markdown
---
name: developer
description: Developer Agent
provider: claude_code
---

You are a developer agent. Write clean, tested code.
```

### Provider inheritance

Workers inherit the supervisor's provider by default. The precedence chain:

1. Profile `provider` field (if valid)
2. Supervisor's provider (inherited)
3. `--provider` flag from `cao launch` (initial session only)
4. Default: `kiro_cli`

## Cross-Provider Orchestration Example

A Kiro CLI supervisor delegating to a Claude Code developer and a Codex reviewer:

**supervisor.md**
```markdown
---
name: cross_provider_supervisor
description: Orchestrates cross-provider work
provider: kiro_cli
role: supervisor
---

You coordinate developer and reviewer agents.
```

**developer.md**
```markdown
---
name: developer
description: Developer Agent
provider: claude_code
role: developer
---

You implement features based on specifications.
```

**reviewer.md**
```markdown
---
name: reviewer
description: Code Reviewer
provider: codex
role: reviewer
---

You review code for quality and correctness.
```

```bash
# Install profiles
cao install cross_provider_supervisor
cao install developer
cao install reviewer

# Launch -- supervisor runs on kiro_cli, workers auto-select their declared provider
cao launch --agents cross_provider_supervisor
```

When the supervisor calls `handoff("developer", "Implement login page")`, CAO reads the developer profile, sees `provider: claude_code`, and spawns the worker using Claude Code -- regardless of the supervisor's own provider.

## Provider-Specific Features

Each provider preserves its native capabilities inside CAO:

- **Claude Code** -- sub-agents, `--permission-mode`, native agent routing, MCP config injection
- **Kiro CLI** -- custom agents (`~/.kiro/agents/`), skill resources, legacy and TUI mode
- **Codex** -- named profiles (`--profile`), inline config overrides (`-c key=value`)
- **Hermes** -- profile aliases, structured approval prompts (`answer_user_prompt`)
- **Copilot CLI** -- baked-in `.agent.md` skill delivery
- **OpenCode** -- CAO-managed config root, temporary inbox polling fallback
- **Oh My Pi** -- native `.omp` configuration and skills preserved; CAO context/MCP are additive

## Status Detection

Every provider implements its own status detection patterns. CAO's StatusMonitor delegates to the provider-specific `get_status()` method, which analyzes the rolling output buffer for patterns unique to that CLI (prompts, spinners, response markers, error indicators).
