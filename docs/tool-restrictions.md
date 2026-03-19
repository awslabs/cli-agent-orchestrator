# Tool Restrictions (allowedTools)

CAO enforces tool restrictions through `allowedTools` — a unified vocabulary that gets translated to each provider's native restriction mechanism. This ensures agents only have access to the tools their role requires, regardless of which CLI provider runs them.

## How It Works

```
Agent profile markdown (or --allowed-tools CLI flag)
  └→ CAO vocabulary: execute_bash, fs_read, fs_write, fs_*, @cao-mcp-server, @builtin
     └→ Translated per provider:
          Kiro CLI / Q CLI:   allowedTools in agent JSON config (install time)
          Claude Code:        --disallowedTools CLI flags
          Copilot CLI:        --deny-tool flags (overrides --allow-all)
          Gemini CLI:         Policy Engine TOML deny rules (~/.gemini/policies/)
          Kimi CLI:           Security system prompt (soft enforcement)
          Codex:              Security system prompt (soft enforcement)
```

## Provider Behavior Summary

The table below shows what happens for each provider when a `supervisor` role (restricted) and `developer` role (unrestricted) agent is launched, with and without `--yolo`:

| Provider | Enforcement | `supervisor` (no `--yolo`) | `developer` (no `--yolo`) | Any role + `--yolo` |
|----------|------------|--------------------------|--------------------------|---------------------|
| **Kiro CLI** | Hard | `allowedTools: ["@cao-mcp-server"]` in agent JSON — no bash, no file write | `allowedTools: ["@builtin", "fs_*", "execute_bash", "@cao-mcp-server"]` in agent JSON | `allowedTools: ["*"]` — unrestricted |
| **Q CLI** | Hard | Same as Kiro CLI (agent JSON) | Same as Kiro CLI (agent JSON) | `allowedTools: ["*"]` — unrestricted |
| **Claude Code** | Hard | `--disallowedTools Bash Edit Write Glob Grep` blocks shell + file tools | No `--disallowedTools` flags — all tools available | No `--disallowedTools` — unrestricted |
| **Copilot CLI** | Hard | `--deny-tool shell --deny-tool write ...` blocks shell + file write | No `--deny-tool` flags — all tools available | No `--deny-tool` — unrestricted |
| **Gemini CLI** | Hard | TOML deny rules: `run_shell_command`, `write_file`, `replace`, etc. excluded from model memory | No TOML deny rules — all tools available | No policy file written — unrestricted |
| **Kimi CLI** | Soft | Security system prompt: "You may ONLY use: @cao-mcp-server" | No restriction prompt — full access | No restriction prompt — unrestricted |
| **Codex** | Soft | Security system prompt: "You may ONLY use: @cao-mcp-server" | No restriction prompt — full access | No restriction prompt — unrestricted |

**Key**: Hard enforcement = agent physically cannot execute denied tools. Soft enforcement = prompt-based, agent may still attempt restricted actions.

## CAO Tool Vocabulary

| CAO Tool | Description | Example Native Mapping |
|----------|-------------|----------------------|
| `execute_bash` | Shell/terminal command execution | Claude: `Bash`, Gemini: `run_shell_command`, Copilot: `shell` |
| `fs_read` | Read files | Claude: `Read`, Gemini: `read_file` |
| `fs_write` | Write/edit files | Claude: `Edit, Write`, Gemini: `write_file, replace` |
| `fs_list` | List/search files (glob, grep) | Claude: `Glob, Grep`, Gemini: `list_directory, glob` |
| `fs_*` | All filesystem operations (read + write + list) | Expands to all fs_ tools |
| `@builtin` | Provider's built-in non-tool capabilities | Not mapped (provider-internal) |
| `@cao-mcp-server` | CAO MCP server tools (assign, handoff, send_message) | Not restricted (always allowed) |

## Roles and Defaults

When a profile doesn't explicitly set `allowedTools`, defaults are based on the `role` field:

| Role | Default `allowedTools` | Use Case |
|------|----------------------|----------|
| `supervisor` | `["@cao-mcp-server"]` | Orchestration only — no code execution, no file access |
| `developer` | `["@builtin", "fs_*", "execute_bash", "@cao-mcp-server"]` | Full access for coding and testing |
| `reviewer` | `["@builtin", "fs_read", "fs_list", "@cao-mcp-server"]` | Read-only code review |
| *(unset)* | Same as `developer` | Backward compatible |

### Setting Role in Profile

```yaml
---
name: code_supervisor
description: Code Supervisor
role: supervisor
---
```

The `role` field determines default tool access. You can override with explicit `allowedTools`:

```yaml
---
name: restricted_developer
description: Developer with no bash
role: developer
allowedTools: ["@builtin", "fs_*", "@cao-mcp-server"]
---
```

## Resolution Order

Tool permissions are resolved in this priority order (highest wins):

| Priority | Source | Example |
|----------|--------|---------|
| 1 (highest) | `--yolo` flag | Sets `["*"]` — unrestricted, skips confirmation |
| 2 | `--allowed-tools` CLI flag | `--allowed-tools @cao-mcp-server --allowed-tools fs_read` |
| 3 | Profile `allowedTools` | `allowedTools: ["@builtin", "fs_read"]` in frontmatter |
| 4 | Role defaults | Based on `role: supervisor` → `["@cao-mcp-server"]` |
| 5 (lowest) | Developer defaults | Fallback if nothing else is set |

## The `--yolo` Flag

`--yolo` is the escape hatch — it sets `allowedTools: ["*"]` (unrestricted) AND skips the confirmation prompt.

```bash
# With --yolo: agent can execute ANY command (aws, rm, curl, etc.)
cao launch --agents code_supervisor --yolo

# Without --yolo: shows confirmation prompt with tool summary
cao launch --agents code_supervisor
# Output:
#   Agent 'code_supervisor' launching on kiro_cli:
#     Allowed:  @cao-mcp-server
#     Blocked:  run_shell_command, write_file, replace, ...
#     Directory: /home/user/project
#
#   Do you trust all the actions in this folder? [Y/n]
```

**When `--yolo` is set, ALL provider restrictions are bypassed** — no policy files, no `--disallowedTools`, no deny rules. This is the current behavior and is by design.

## Provider Enforcement Details

### Hard Enforcement (5/7 providers)

These providers block tools at the runtime level — the agent physically cannot execute denied tools.

#### Kiro CLI / Q CLI

Tool restrictions are baked into the agent JSON at **install time**:

```bash
cao install code_supervisor --provider kiro_cli
# Writes allowedTools: ["@cao-mcp-server"] to ~/.kiro/agents/code_supervisor.json
```

Runtime `allowed_tools` API parameters are stored in the database for auditing/inheritance but don't change Kiro/Q CLI's behavior. To update restrictions, reinstall the profile.

#### Claude Code

Restrictions via `--disallowedTools` flags alongside `--dangerously-skip-permissions`:

```bash
claude --dangerously-skip-permissions --disallowedTools Bash --disallowedTools Edit --disallowedTools Write ...
```

Applied at runtime per-session. The `--dangerously-skip-permissions` flag auto-approves allowed tools while `--disallowedTools` blocks specific ones.

#### Copilot CLI

Restrictions via `--deny-tool` flags that override `--allow-all`:

```bash
copilot --allow-all --deny-tool shell --deny-tool write ...
```

`--deny-tool` takes precedence over `--allow-all`, providing hard enforcement.

#### Gemini CLI

Restrictions via **Policy Engine TOML deny rules** in `~/.gemini/policies/`:

```toml
# Auto-generated by CAO — tool restriction policy
# Terminal: abc12345

[[rule]]
toolName = "run_shell_command"
decision = "deny"
priority = 900
deny_message = "Blocked by CAO policy (terminal abc12345)"

[[rule]]
toolName = "write_file"
decision = "deny"
priority = 900
deny_message = "Blocked by CAO policy (terminal abc12345)"
```

Policy Engine deny rules completely **exclude tools from the model's memory** — the model doesn't even see them as options. This works even in `--yolo` mode (unlike the deprecated `excludeTools` in settings.json).

Each terminal gets its own policy file (`cao-{terminal_id}.toml`), so concurrent sessions don't conflict. Files are cleaned up when sessions end.

### Soft Enforcement (2/7 providers)

These providers lack native tool restriction mechanisms. CAO injects a security system prompt.

#### Kimi CLI / Codex

A security prompt is prepended to the agent's instructions:

```
## SECURITY CONSTRAINTS — TOOL RESTRICTIONS
You are operating under restricted tool access. You may ONLY use these tools: @cao-mcp-server
Do NOT attempt to use: execute_bash, fs_read, fs_write, fs_list
If a task requires a restricted tool, explain that you cannot perform it due to policy restrictions.
```

This is best-effort — the model may still attempt restricted actions. For security-critical workloads, use a hard-enforcement provider.

## Cross-Provider Inheritance

When a supervisor delegates via `handoff()` or `assign()`, the child terminal inherits allowed tools:

1. Read parent's `allowed_tools` from database
2. Load child profile's `allowedTools` / role defaults
3. Pass the child's resolved `allowed_tools` to the new terminal

```
Example: Kiro supervisor (allowedTools=@cao-mcp-server)
  → assign("developer")
  → Developer profile: allowedTools=[execute_bash, fs_*, @cao-mcp-server]
  → Child on Claude Code gets: [execute_bash, fs_*, @cao-mcp-server]
  → Translated: --disallowedTools (none blocked — developer has full access)
```

## CLI Usage Examples

```bash
# Use profile/role defaults
cao launch --agents code_supervisor
# supervisor role → allowed: @cao-mcp-server only

# Override with specific tools
cao launch --agents developer --allowed-tools @cao-mcp-server --allowed-tools fs_read
# Developer restricted to orchestration + file reading

# Unrestricted (DANGEROUS)
cao launch --agents code_supervisor --yolo
# All tools allowed, no confirmation prompt

# Cross-provider with tool restrictions
cao launch --agents code_supervisor --provider gemini_cli
# supervisor role → Policy Engine deny rules block shell/write tools
```

## API Usage

The `allowed_tools` parameter can be passed when creating sessions via the API:

```bash
# Restricted session
curl -X POST "http://localhost:9889/sessions?provider=claude_code&agent_profile=developer&allowed_tools=@cao-mcp-server,fs_read"

# Unrestricted session
curl -X POST "http://localhost:9889/sessions?provider=claude_code&agent_profile=developer&allowed_tools=*"
```

The `allowed_tools` value is stored in the database and returned by `GET /terminals/{id}`.

## Security Recommendations

1. **Use the most restrictive role possible.** Supervisors should use `role: supervisor` — they only need MCP tools to orchestrate.
2. **Don't use `--yolo` in production.** It grants unrestricted access and skips all safety prompts.
3. **Review tool summaries.** The confirmation prompt shows exactly what tools are allowed and blocked — read it before confirming.
4. **Prefer hard-enforcement providers** (Kiro CLI, Claude Code, Copilot CLI, Gemini CLI, Q CLI) for sensitive workloads.
5. **Kimi CLI and Codex use soft enforcement** — the agent may still attempt restricted actions. Use these only for non-critical tasks or combine with other safeguards.

For vulnerability reporting and additional security details, see [SECURITY.md](../SECURITY.md).
