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

**If you don't set `role` or `allowedTools`, the agent defaults to `developer` role permissions** (`@builtin`, `fs_*`, `execute_bash`, `web_fetch`, `@cao-mcp-server`). This gives full coding access while still going through the restriction system. The launch confirmation prompt will remind you to add `role` or `allowedTools` to your profile.

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
| `developer` | `@builtin`, `fs_*`, `execute_bash`, `web_fetch`, `@cao-mcp-server` | Full access: read, write, execute, fetch, orchestrate |
| `reviewer` | `@builtin`, `fs_read`, `fs_list`, `@cao-mcp-server` | Read-only: review code, no writes, execution, or network |

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

| Tool | What it allows | Example: Claude Code | Example: Copilot CLI |
|------|---------------|---------------------|-------------------|
| `execute_bash` | Run shell commands | `Bash` | `shell` |
| `fs_read` | Read files | `Read` | `read` |
| `fs_write` | Write/edit files | `Edit`, `Write` | `write` |
| `fs_list` | Search/list files | `Glob`, `Grep` | `list`, `grep` |
| `fs_*` | All filesystem ops | All of the above | All of the above |
| `web_fetch` | Fetch URLs / search the web | `WebFetch`, `WebSearch` | (not mapped) |
| `@builtin` | Provider built-in capabilities | (internal) | (internal) |
| `@cao-mcp-server` | CAO orchestration tools | `handoff`, `assign`, `send_message`, plus Hermes prompt answers via `answer_user_prompt` | Same |
| `*` | Everything (unrestricted) | All tools | All tools |

CAO translates these to each provider's native tool names automatically. You write one vocabulary; it works across supported providers.

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

When you run `cao launch` without `--yolo` or `--auto-approve`, CAO shows a summary of the resolved tool restrictions and asks for confirmation:

```
Agent 'code_supervisor' launching on kiro_cli:
  Role:      supervisor
  Allowed:   @cao-mcp-server, fs_read, fs_list
  Directory: /home/user/my-project

  To skip this prompt next time, relaunch with --auto-approve
  To remove all restrictions, relaunch with --yolo

Proceed? [Y/n]
```

If no `role` or `allowedTools` is set in the profile, the prompt includes an additional reminder:

```
Agent 'my_agent' launching on claude_code:
  Role:      (not set — using developer defaults)
  Allowed:   @builtin, fs_*, execute_bash, web_fetch, @cao-mcp-server
  Directory: /home/user/my-project

  Note: No role or allowedTools set — defaulting to 'developer'.
  Add 'role' or 'allowedTools' to your agent profile to control tool access.
  Docs: https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/tool-restrictions.md

  To skip this prompt next time, relaunch with --auto-approve
  To remove all restrictions, relaunch with --yolo

Proceed? [Y/n]
```

### `--auto-approve` vs `--yolo`

| | `Y` at prompt | `--auto-approve` | `--yolo` |
|---|---|---|---|
| **Confirmation prompt** | Shown | Skipped | Skipped |
| **Tool restrictions** | Enforced | Enforced | Removed — `["*"]` |
| **Use case** | Interactive launch | Automated flows, scripts, agent-to-agent | Unrestricted access |

```bash
cao launch --agents my_agent                  # interactive — shows prompt
cao launch --agents my_agent --auto-approve   # automated — skips prompt, keeps restrictions
cao launch --agents my_agent --yolo           # unrestricted — skips prompt AND removes restrictions
```

The confirmation prompt is a **review gate** — it shows the resolved role and allowed tools, then lets you proceed or cancel. `--auto-approve` skips this gate while keeping all restrictions enforced — useful for CAO flows, scripted launches, and agent-to-agent workflows. `--yolo` sits at the top of the override hierarchy — it **overrides both role and allowedTools**, grants unrestricted access (`["*"]`), and skips the prompt entirely.

### How Tool Restrictions Are Enforced (Implementation Detail)

CAO defines a universal tool vocabulary (`execute_bash`, `fs_read`, `fs_write`, `fs_list`). However, not all providers understand this vocabulary natively. There are two categories:

**Providers that need translation** — Claude Code and Copilot CLI each have their own native tool names (e.g., Claude Code calls bash execution `Bash`, Copilot calls it `shell`). CAO uses an internal `TOOL_MAPPING` to translate the CAO vocabulary to provider-native names, then computes which native tools to block and passes them as CLI flags (e.g., `--disallowedTools Bash`, `--deny-tool shell`).

| CAO Tool | Claude Code | Copilot CLI |
|----------|-------------|-------------|
| `execute_bash` | `Bash` | `shell` |
| `fs_read` | `Read` | `read` |
| `fs_write` | `Edit`, `Write` | `write` |
| `fs_list` | `Glob`, `Grep` | `list`, `grep` |
| `web_fetch` | `WebFetch`, `WebSearch` | (not mapped) |

**Providers that accept CAO vocabulary directly** — Kiro CLI accepts `allowedTools` in the agent JSON at install time, using the same vocabulary as CAO. No translation needed. Kimi CLI and Codex use system prompt instructions to enforce restrictions. For all three, CAO passes the `allowedTools` list directly without translation — so no `TOOL_MAPPING` entry exists for them, and none is needed.

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

Note: `--auto-approve` is **not** in this priority chain — it only controls whether the confirmation prompt is shown, not what restrictions are applied.

Examples:

```bash
# Profile has role: supervisor → restricted to @cao-mcp-server + fs_read + fs_list
cao launch --agents code_supervisor

# Same, but skip the confirmation prompt (restrictions still enforced)
cao launch --agents code_supervisor --auto-approve

# CLI flag overrides the role
cao launch --agents code_supervisor --allowed-tools execute_bash --allowed-tools fs_read

# --yolo overrides everything
cao launch --agents code_supervisor --yolo
```

## Provider Enforcement

As described in [How Tool Restrictions Are Enforced](#how-tool-restrictions-are-enforced-implementation-detail), some providers require CAO to translate `allowedTools` to native tool names (via `TOOL_MAPPING`), while others accept the CAO vocabulary directly. The table below shows how each provider enforces restrictions:

| Provider | Enforcement | How it works |
|----------|------------|-------------|
| **Claude Code** | Hard | `--disallowedTools` flags block specific tools |
| **Kiro CLI** | Hard | `allowedTools` in agent JSON at install time |
| **Copilot CLI** | Hard | `--deny-tool` flags override `--allow-all` |
| **Kimi CLI** | Soft | Security system prompt only |
| **Codex** | Soft | Security system prompt only |
| **Hermes** | Profile-defined | CAO launches default `hermes` or the optional `hermesProfile` wrapper declared by the CAO profile; restrict tools in that Hermes profile |

**Hard enforcement** = the agent physically cannot use denied tools, enforced by the provider runtime.

**Soft enforcement** = a system prompt tells the agent not to use certain tools. The agent may still attempt them. Use hard-enforcement providers for security-critical work.

### What "hard" looks like per provider

**Claude Code** — Adds `--disallowedTools` flags to the launch command:
```bash
claude --dangerously-skip-permissions --disallowedTools Bash --disallowedTools Edit --disallowedTools Write
```

`permissionMode` is a separate axis from `--disallowedTools`: `permissionMode` controls which permission tier the session runs under (unconditional bypass vs. classifier-gated tiers like `auto`), while `--disallowedTools` enforces the per-tool denylist. The two stack — a profile can set `permissionMode: auto` *and* a tool denylist, and both apply on the launch command. See [Permission Mode Override](claude-code.md#permission-mode-override) for full details.

**Kiro CLI** — Writes `allowedTools` into the agent JSON at install time:
```json
{ "allowedTools": ["@cao-mcp-server", "fs_read", "fs_list"] }
```

**Copilot CLI** — Adds `--deny-tool` flags that override `--allow-all`:
```bash
copilot --allow-all --deny-tool shell --deny-tool write
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
| Skip confirmation in scripts/automation | `--auto-approve` (restrictions still enforced) |
| No restrictions at all | `--yolo` |
| Check what's allowed before launch | Launch without `--yolo` or `--auto-approve` — the prompt shows the summary |

## Security Recommendations

1. **Use `role: supervisor` for orchestrators.** They only need MCP tools + file reading for context.
2. **Don't use `--yolo` in production.** It grants unrestricted access and skips all safety prompts.
3. **Prefer hard-enforcement providers** (Claude Code, Kiro CLI, Copilot CLI) for sensitive workloads.
4. **Review the confirmation prompt.** It shows exactly what tools are allowed and blocked before you proceed.
5. **Kimi CLI and Codex use soft enforcement** — use these only for non-critical tasks.

## Known Limitations

1. **Claude Code tool mapping is nearly complete, with MCP tools the remaining gap.** The current mapping covers `Bash` (and its `Task`/`Agent`/`Monitor`/`BashOutput`/`KillShell` execution family), `Read`, `Edit`, `Write`, `Glob`, `Grep`, and — via `web_fetch` — [`WebFetch`](https://code.claude.com/docs/en/permissions#webfetch) and `WebSearch`. The subagent tool is intentionally **not** a separate category: it is folded into `execute_bash`, because a subagent spawns with its own full toolset and can run shell, so exposing it standalone would let a profile grant subagent access without `execute_bash` and re-open that escape. Claude Code **renamed this tool from `Task` to `Agent`**, so both names are denied — current builds expose only `Agent`, so denying just `Task` would be a silent no-op. Provider MCP tools remain unmapped (see limitation #2) — they cannot be blocked via `--disallowedTools`.

2. **`@cao-mcp-server` is a pass-through marker, not enforced at the provider level.** Including `@cao-mcp-server` in `allowedTools` signals intent (this agent should have orchestration tools), but it does **not** translate to any native `--disallowedTools` flag. MCP tools (`handoff`, `assign`, `send_message`, `answer_user_prompt`) are always available to the agent regardless of `allowedTools` — providers do not currently support blocking individual MCP tools. `answer_user_prompt` is exposed by the MCP server, but its structured prompt-navigation behavior is currently implemented for Hermes workers that report `waiting_user_answer`; other providers may only receive ordinary text input until they implement equivalent prompt states. Additionally, `@cao-mcp-server` is all-or-nothing: there is no way to allow only `send_message` while blocking `assign`. Future versions may support `@cao-mcp-server:send_message` syntax for per-tool MCP control.

3. **Soft enforcement is best-effort.** Kimi CLI and Codex rely on system prompt instructions to restrict tools. The agent may ignore these restrictions. Do not rely on soft enforcement for security-critical workloads.

## Example Profiles

For complete working examples with `role` and `allowedTools`, see the [examples directory](../examples/):

- **[assign/](../examples/assign/)** — Supervisor + worker agents with role-based restrictions
- **[cross-provider/](../examples/cross-provider/)** — Mixed-provider workflows with per-agent tool restrictions
- **[codex-basic/](../examples/codex-basic/)** — Codex agents with soft enforcement
