# Tool Restrictions

## Concept Overview

CAO controls what tools an agent can use through a two-layer system:

```
                ┌─────────────────────────────────┐
  High-level    │           role                   │  "What kind of agent is this?"
                │   supervisor, developer, ...     │  A named bundle of allowedTools
                └──────────────┬──────────────────┘
                               │ maps to
                ┌──────────────▼──────────────────┐
  Low-level     │        allowedTools              │  "What tools can this agent use?"
                │  execute_bash, fs_read, ...      │  Fine-grained tool list
                └─────────────────────────────────┘
```

- **`role`** — High-level abstraction. A named preset that maps to a default set of `allowedTools`. Think of it as "what kind of agent is this?" Built-in roles ship with CAO; users can define custom roles.
- **`allowedTools`** — Low-level control. An explicit list of tools the agent can use. Always overrides `role` when set.
- **`--yolo`** — Escape hatch. Bypasses ALL restrictions and skips confirmation prompts. The agent can do anything.

## Default Behavior

**If you don't set `role` or `allowedTools`, the agent defaults to `developer` role permissions** (`@builtin`, `fs_*`, `execute_bash`, `@cao-mcp-server`). This gives full coding access while still going through the restriction system. The launch confirmation prompt will remind you to add `role` or `allowedTools` to your profile.

## The Three Controls

### 1. `role` — The Simple Way

Set `role` in your agent profile frontmatter. CAO maps it to a sensible set of `allowedTools` automatically.

```yaml
---
name: code_supervisor
description: Orchestrates worker agents
role: supervisor
---
```

#### Built-in Roles

| Role | Default `allowedTools` | What the agent can do |
|------|----------------------|----------------------|
| `supervisor` | `@cao-mcp-server`, `fs_read`, `fs_list` | Orchestrate workers + read files for context |
| `developer` | `@builtin`, `fs_*`, `execute_bash`, `@cao-mcp-server` | Full access: read, write, execute, orchestrate |
| `reviewer` | `@builtin`, `fs_read`, `fs_list`, `@cao-mcp-server` | Read-only: review code, no writes or execution |

#### Custom Roles

Define your own roles in `~/.aws/cli-agent-orchestrator/settings.json`:

```json
{
  "roles": {
    "data_analyst": ["fs_read", "execute_bash", "@cao-mcp-server"],
    "secure_dev": ["fs_read", "fs_write", "@cao-mcp-server"]
  }
}
```

Then use them in any profile:

```yaml
---
name: my_analyst
role: data_analyst
---
```

Custom roles follow the same rules as built-in roles — they're just a named `allowedTools` list.

### 2. `allowedTools` — The Precise Way

Set `allowedTools` directly in the profile frontmatter for fine-grained control. This always overrides `role`, and **can be used without `role`**.

```yaml
---
name: restricted_developer
description: Developer with no bash access
role: developer
allowedTools: ["@builtin", "fs_*", "@cao-mcp-server"]
---
```

In this example, `role: developer` would normally include `execute_bash`, but `allowedTools` explicitly excludes it. The explicit list wins.

You can also use `allowedTools` without `role`:

```yaml
---
name: read_only_agent
description: Agent with only read and bash access
allowedTools: ["fs_read", "fs_list", "execute_bash"]
---
```

No `role` is needed — `allowedTools` is the full specification of what tools the agent can use.

#### Tool Vocabulary

| Tool | What it allows | Example: Claude Code | Example: Gemini CLI |
|------|---------------|---------------------|-------------------|
| `execute_bash` | Run shell commands | `Bash` | `run_shell_command` |
| `fs_read` | Read files | `Read` | `read_file` |
| `fs_write` | Write/edit files | `Edit`, `Write` | `write_file`, `replace` |
| `fs_list` | Search/list files | `Glob`, `Grep` | `list_directory`, `glob` |
| `fs_*` | All filesystem ops | All of the above | All of the above |
| `@builtin` | Provider built-in capabilities | (internal) | (internal) |
| `@cao-mcp-server` | CAO orchestration tools | `handoff`, `assign`, `send_message` | Same |
| `*` | Everything (unrestricted) | All tools | All tools |

CAO translates these to each provider's native tool names automatically. You write one vocabulary; it works across all 7 providers.

### 3. `--yolo` — The Escape Hatch

```bash
cao launch --agents code_supervisor --yolo
```

`--yolo` does two things:
1. Sets `allowedTools: ["*"]` — the agent can use ALL tools
2. Skips the confirmation prompt — launches immediately after showing a warning

**Use `--yolo` when you want zero restrictions.** This overrides everything — role, allowedTools, CLI flags. The agent can execute any command: `aws`, `rm -rf`, `curl`, read credentials, anything.

A warning is still displayed so you know what's happening:

```
[WARNING] --yolo mode enabled
  Agent 'code_supervisor' launching UNRESTRICTED on claude_code.
  Agent can execute ANY command (aws, rm, curl, read credentials).
  Directory: /home/user/my-project
```

## Launch Confirmation Prompt

When you run `cao launch` without `--yolo`, CAO shows a summary of the resolved tool restrictions and asks for confirmation:

```
Agent 'code_supervisor' launching on kiro_cli:
  Allowed:  @cao-mcp-server, fs_read, fs_list
  Blocked:  Bash, Edit, Write
  Directory: /home/user/my-project

  To grant all permissions, re-run with --yolo.

Proceed? [Y/n]
```

If no `role` or `allowedTools` is set in the profile, the prompt includes an additional reminder:

```
Agent 'my_agent' launching on claude_code:
  Allowed:  @builtin, fs_*, execute_bash, @cao-mcp-server
  Blocked:  (none)
  Directory: /home/user/my-project

  Note: No role or allowedTools set — defaulting to 'developer'.
  Add 'role' or 'allowedTools' to your agent profile to control tool access.
  Docs: https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/tool-restrictions.md

  To grant all permissions, re-run with --yolo.

Proceed? [Y/n]
```

### Confirmation vs `--yolo`

Answering **Y** to the confirmation prompt is **not** the same as `--yolo`:

| | Confirmation → Y | `--yolo` |
|---|---|---|
| **Tool restrictions** | Still enforced — agent is limited to the tools shown in "Allowed" | Removed — agent gets `["*"]` (all tools) |
| **Blocked tools** | Still blocked — agent cannot use tools shown in "Blocked" | Nothing is blocked |
| **What it means** | "I've reviewed these restrictions and want to proceed" | "Give the agent unrestricted access, no questions asked" |

The confirmation prompt is a **review gate** — it shows you exactly what the agent can and cannot do, then lets you proceed or cancel. The restrictions listed in the summary are still enforced after you confirm. `--yolo` both **removes all restrictions** and **skips the review gate entirely**.

## How Overrides Work

When multiple controls are set, the highest priority wins:

```
Priority (highest to lowest):

  1. --yolo                    → ["*"] (unrestricted, no prompts)
  2. --allowed-tools CLI flag  → explicit list at launch time
  3. allowedTools in profile   → explicit list in frontmatter
  4. role in profile           → maps to built-in/custom role defaults
  5. (nothing set)             → developer defaults
```

Examples:

```bash
# Profile has role: supervisor → restricted to @cao-mcp-server + fs_read + fs_list
cao launch --agents code_supervisor

# CLI flag overrides the role
cao launch --agents code_supervisor --allowed-tools execute_bash --allowed-tools fs_read

# --yolo overrides everything
cao launch --agents code_supervisor --yolo
```

## Provider Enforcement

CAO translates `allowedTools` to each provider's native restriction mechanism:

| Provider | Enforcement | How it works |
|----------|------------|-------------|
| **Claude Code** | Hard | `--disallowedTools` flags block specific tools |
| **Kiro CLI** | Hard | `allowedTools` in agent JSON at install time |
| **Q CLI** | Hard | `allowedTools` in agent JSON at install time |
| **Copilot CLI** | Hard | `--deny-tool` flags override `--allow-all` |
| **Gemini CLI** | Hard | Policy Engine TOML deny rules in `~/.gemini/policies/` |
| **Kimi CLI** | Soft | Security system prompt only |
| **Codex** | Soft | Security system prompt only |

**Hard enforcement** = the agent physically cannot use denied tools, enforced by the provider runtime.

**Soft enforcement** = a system prompt tells the agent not to use certain tools. The agent may still attempt them. Use hard-enforcement providers for security-critical work.

### What "hard" looks like per provider

**Claude Code** — Adds `--disallowedTools` flags to the launch command:
```bash
claude --dangerously-skip-permissions --disallowedTools Bash --disallowedTools Edit --disallowedTools Write
```

**Kiro CLI / Q CLI** — Writes `allowedTools` into the agent JSON at install time:
```json
{ "allowedTools": ["@cao-mcp-server", "fs_read", "fs_list"] }
```

**Copilot CLI** — Adds `--deny-tool` flags that override `--allow-all`:
```bash
copilot --allow-all --deny-tool shell --deny-tool write
```

**Gemini CLI** — Writes per-session TOML deny rules to `~/.gemini/policies/`:
```toml
[[rule]]
toolName = "run_shell_command"
decision = "deny"
priority = 900
```

**Kimi CLI / Codex** — Prepends to the system prompt:
```
You may ONLY use these tools: @cao-mcp-server, fs_read, fs_list
Do NOT attempt to use: execute_bash, fs_write
```

## Cross-Provider Inheritance

When a supervisor delegates work via `handoff()` or `assign()`, the child agent gets its own `allowedTools` resolved from its profile — not inherited from the parent.

```
Supervisor (role: supervisor → @cao-mcp-server, fs_read, fs_list)
  │
  ├─ assign("developer")
  │    → Developer profile: role: developer → full access
  │    → Claude Code launched with no --disallowedTools
  │
  └─ handoff("reviewer")
       → Reviewer profile: role: reviewer → read-only
       → Claude Code launched with --disallowedTools Bash Edit Write
```

Each agent is restricted based on its own profile, not its parent's permissions.

## Quick Reference

| I want to... | Do this |
|-------------|---------|
| Restrict a supervisor to orchestration + reading | `role: supervisor` |
| Give full access to a developer | `role: developer` (or set nothing) |
| Read-only reviewer | `role: reviewer` |
| Custom tool set | `allowedTools: ["fs_read", "execute_bash"]` |
| Reusable custom preset | Define in `settings.json` `roles`, use `role: my_preset` |
| Override role at launch | `--allowed-tools fs_read --allowed-tools @cao-mcp-server` |
| No restrictions at all | `--yolo` or don't set role/allowedTools |
| Check what's allowed before launch | Launch without `--yolo` — the confirmation prompt shows the summary |

## Security Recommendations

1. **Use `role: supervisor` for orchestrators.** They only need MCP tools + file reading for context.
2. **Don't use `--yolo` in production.** It grants unrestricted access and skips all safety prompts.
3. **Prefer hard-enforcement providers** (Claude Code, Kiro CLI, Q CLI, Copilot CLI, Gemini CLI) for sensitive workloads.
4. **Review the confirmation prompt.** It shows exactly what tools are allowed and blocked before you proceed.
5. **Kimi CLI and Codex use soft enforcement** — use these only for non-critical tasks.

## Known Limitations

1. **Claude Code tool mapping is incomplete.** The current mapping covers `Bash`, `Read`, `Edit`, `Write`, `Glob`, and `Grep`. Claude Code also has `WebFetch`, `Agent` (subagent), and MCP tools that are not yet mapped to CAO vocabulary. These tools remain unrestricted even when `allowedTools` is set. Future versions will add `web_fetch` and `subagent` to the CAO vocabulary.

2. **MCP tool control is coarse-grained.** `@cao-mcp-server` allows all tools from that server (`handoff`, `assign`, `send_message`, `list_terminals`, etc.). There is no way to allow only `send_message` while blocking `assign`. Future versions may support `@cao-mcp-server:send_message` syntax for per-tool MCP control.

3. **Soft enforcement is best-effort.** Kimi CLI and Codex rely on system prompt instructions to restrict tools. The agent may ignore these restrictions. Do not rely on soft enforcement for security-critical workloads.

## Example Profiles

For complete working examples with `role` and `allowedTools`, see the [examples directory](../examples/):

- **[assign/](../examples/assign/)** — Supervisor + worker agents with role-based restrictions
- **[cross-provider/](../examples/cross-provider/)** — Mixed-provider workflows with per-agent tool restrictions
- **[codex-basic/](../examples/codex-basic/)** — Codex agents with soft enforcement
