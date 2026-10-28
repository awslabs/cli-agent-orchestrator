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

| Role | Default `allowedTools` | Default `delegateTools` (ceiling) | What the agent can do |
|------|----------------------|-----------------------------------|----------------------|
| `supervisor` | `@cao-mcp-server`, `fs_read`, `fs_list` | `*` | Orchestrate workers + read files for context; may launch workers holding any tools |
| `developer` | `@builtin`, `fs_*`, `execute_bash`, `web_fetch`, `@cao-mcp-server` | its own list | Full access: read, write, execute, fetch, orchestrate |
| `reviewer` | `@builtin`, `fs_read`, `fs_list`, `@cao-mcp-server` | its own list | Read-only: review code, no writes, execution, or network; may launch only read-only workers |
| `workflow_scout` | `@builtin`, `fs_read`, `execute_bash`, `@cao-mcp-server` | its own list | Locate existing workflow specs (`cao workflow list` / `get`). Withholds `fs_write` and `web_fetch`; `execute_bash` is still a full shell |

The third column is the role's **delegation ceiling**: the most an agent of that role may grant to the workers it creates with `assign`, `handoff` or a workflow step. Only `supervisor` widens by default; see [Cross-Provider Inheritance](#cross-provider-inheritance) for the rule and `delegateTools` for overriding it.

`workflow_scout` finds existing specs (`cao workflow list` / `cao workflow get`) so an authoring agent can extend them instead of duplicating them. The allowlist withholds the file-write category (`fs_write`) and the web-fetch category (`web_fetch`). It still grants `execute_bash`, which maps to a full shell (`Bash`, `shell`, or `run_shell_command`, depending on the provider). A permitted shell command can write files and reach the network. The profile prompt asks the scout to stay read-only; that is a convention, not a sandbox.

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

Custom roles follow the same rules as built-in roles — they're just a named `allowedTools` list. A custom role with the same name as a built-in role replaces it: the `settings.json` list is used and CAO logs a warning naming the role, so a policy you saved before a built-in of that name shipped keeps resolving as saved. The replacement is complete, in both directions: a settings entry can narrow a built-in or widen it, up to `["*"]` (unrestricted, which also switches off native tool denial). The `roles` key is written only by `cao config set` and by hand (no REST or MCP route touches `agents.roles`), so treat it as operator-trusted configuration.

A custom role's delegation ceiling defaults to its own list, with one exception: a custom role literally named `supervisor` keeps the built-in `*` ceiling, because the default table is keyed by the role name. Declare `delegateTools` on the profile when you want something else.

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
| `@builtin` | Selector naming the provider's own built-in tool set; **grants nothing by itself** on any provider. Kept in the role defaults for readability; list `fs_*`/`execute_bash` explicitly for the access you want | (no-op) | (no-op) |
| `@cao-mcp-server` | Every CAO MCP tool: `handoff`, `assign`, `send_message`, Hermes prompt answers via `answer_user_prompt`, memory (`memory_store`/`memory_recall`/`memory_forget`), workflows, `delete_terminal`, and the rest of the `cao-mcp-server` surface | Same | Same |
| `discovery` | Sibling discovery/metadata (`list_siblings`, `update_metadata`) | Same | Same |
| `*` | Everything (unrestricted) | All tools | All tools |

CAO translates these to each provider's native tool names automatically. You write one vocabulary; it works across supported providers.

#### `@cao-mcp-server` gates the whole CAO MCP tool surface, not just delegation

Every tool `cao-mcp-server` registers is denied to a bound terminal whose effective allowlist omits `@cao-mcp-server` and `*` (#671) — not only the delegation tools (`assign`, `handoff`, `assign_elastic`, the `workflow_run` family, `create_ephemeral_agent`) that carried their own check before this. A single `on_call_tool` middleware enforces this once, ahead of every tool, so a tool registered later is covered by default. Four tools stay reachable regardless of the grant because they are the callback protocol an assigned worker depends on to report back and cannot be mistaken for a capability: `send_message`, `complete_assignment`, `workflow_return`, `answer_user_prompt`. The two `discovery`-gated tools (`list_siblings`, `update_metadata`) pass this gate on `discovery` alone, per the carve-out below — they do not need `@cao-mcp-server` too. This gate covers `cao-mcp-server` only; `cao-ops-mcp-server` (the inbound control-plane server for external agents — see [Control Planes](control-planes.md)) is operator-granted and out of its scope.

#### `discovery` is a separate opt-in, not part of `@cao-mcp-server`

`discovery` gates `list_siblings`/`update_metadata` independently of `@cao-mcp-server`. A profile with `@cao-mcp-server` (handoff/assign/send_message) does **not** automatically get sibling discovery, and vice versa — none of the built-in roles (`supervisor`, `developer`, `reviewer`, `workflow_scout`) include `discovery`; add it explicitly if a profile needs peer-to-peer discovery. The same grant is what lets `send_message` name a discovered sibling as `receiver_id`: without it, an explicit receiver must be a terminal the caller created or the caller's own creator (see [Discovery Tool Coexistence](discovery-tool-coexistence.md), "How the two topologies meet").

This is deliberate (see the design discussion on [issue #432](https://github.com/awslabs/cli-agent-orchestrator/issues/432)): the supervisor/worker hierarchy `handoff`/`assign`/`send_message` are built around, and the flat peer layer `group`/`list_siblings`/`update_metadata` introduce, are two different communication topologies. A profile should be able to keep one without the other. See [Discovery Tool Coexistence](discovery-tool-coexistence.md) for the full rationale, the enforcement mechanism, and open follow-ups.

**`group` is an organizational label, not a security boundary.** On a default install with auth disabled, a worker already has local shell access, so `discovery`/`group`/session-scoping provide no isolation guarantee even when used together — see [docs/api.md](api.md) and the coexistence write-up linked above.

#### `delegateTools` — what an agent may hand down

`allowedTools` bounds what an agent can call. `delegateTools` bounds what it can **create**: the most it may grant to a worker it launches through `assign`, `handoff`, or a workflow step. A child's resolved `allowedTools` must fit inside the parent's ceiling or the launch is refused. Without the field, the role default applies (`supervisor` → `*`, every other role → its own `allowedTools`), and the ceiling always includes the agent's own list: an agent can hand down what it holds.

```yaml
---
name: build_reviewer
description: Reviews code and may run a build worker
role: reviewer                       # read-only itself
delegateTools: ["fs_read", "fs_list", "execute_bash", "@cao-mcp-server"]
---
```

This reviewer still has no shell, but it may `assign` a worker that does. Use the same vocabulary as `allowedTools`; `*` means it may launch anything. Like `allowedTools`, the list you write is the complete spec (#772): a worker holding `fs_write` would be refused here because the ceiling omits it.

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
Agent 'code_supervisor' launching on claude_code:
  Allowed:  @cao-mcp-server, fs_read, fs_list
  Blocked:  Agent, Bash, BashOutput, Edit, KillShell, Monitor, NotebookEdit, Task, WebFetch, WebSearch, Write
  Delegates: ALL TOOLS (unrestricted)
  Enforcement: native (the provider refuses blocked tools)
  Directory: /home/user/my-project

  To skip this prompt next time, relaunch with --auto-approve
  To remove all restrictions, relaunch with --yolo

Proceed? [Y/n]
```

If no `role` or `allowedTools` is set in the profile, the prompt includes an additional reminder:

```
Agent 'my_agent' launching on claude_code:
  Allowed:  @builtin, fs_*, execute_bash, web_fetch, @cao-mcp-server
  Blocked:  (none)
  Delegates: @builtin, fs_*, execute_bash, web_fetch, @cao-mcp-server
  Enforcement: native (the provider refuses blocked tools)
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

**Providers that need translation** — Claude Code, Copilot CLI, and Grok Build CLI each have their own native tool names (e.g., Claude Code and Grok call bash execution `Bash`, while Copilot calls it `shell`). CAO uses an internal `TOOL_MAPPING` to translate the CAO vocabulary to provider-native names, then computes which native tools to block and passes them as CLI flags (e.g., `--disallowedTools Bash`, `--deny-tool shell`, or `--deny Bash`).

| CAO Tool | Claude Code | Copilot CLI | Grok Build CLI | Kiro CLI (`tools` at install) |
|----------|-------------|-------------|----------------|-------------------------------|
| `execute_bash` | `Bash` | `shell` | `Bash` | `shell`, `subagent`, `use_aws` (+ alias `execute_bash`) |
| `fs_read` | `Read` | `read` | `Read`, `NotebookRead` | `read`, `knowledge` (+ alias `fs_read`) |
| `fs_write` | `Edit`, `Write` | `write` | `Edit`, `Write`, `NotebookEdit` | `write`, `code` (+ alias `fs_write`) |
| `fs_list` | `Glob`, `Grep` | `list`, `grep` | `Grep`, `Glob` | `glob`, `grep` |
| `web_fetch` | `WebFetch`, `WebSearch` | (not mapped) | `WebFetch`, `WebSearch` + disabled web search | `web_fetch`, `web_search` |
| `@builtin` | (selector) | (selector) | (selector) | `goal`, `introspect`, `todo_list` only; a bare `@builtin` would mean every built-in, shell included |

**Providers that accept CAO vocabulary directly** — Kimi CLI, MiniMax Code, and Codex use system prompt instructions to enforce restrictions, and CAO passes the `allowedTools` list directly without translation, so no `TOOL_MAPPING` entry exists for them. Kiro CLI *also* accepts the CAO vocabulary in `allowedTools`, but in Kiro that list only names the tools that run *without an approval prompt*; what the agent can use at all is `tools`, so CAO translates the resolved policy into `tools` at install time (see the table above and the Kiro entry below).

#### MCP-side enforcement for tools that launch an agent

Provider-native restrictions cannot reach CAO's own MCP tools. `TOOL_MAPPING` translates the CAO
vocabulary into native tool names, and MCP server references have none, so `get_disallowed_tools`
skips every `@`-prefixed entry. A profile without `@cao-mcp-server` therefore still reached
`assign`, `handoff` and `assign_elastic`, which launch an agent under a caller-chosen
profile. `workflow_run`, `workflow_resume` and `workflow_start` can dispatch agents
through a workflow and require the same permission.

These check the caller themselves, at the MCP boundary:

- The effective policy is the calling terminal's recorded `allowedTools`, or the profile
  resolution it stands for when nothing was recorded. It is the same list `--allowed-tools`,
  `role` defaults and `--yolo` already produce, not a second allowlist.
- The grant is `@cao-mcp-server` or `*`. A bare tool name does not grant an MCP tool, because
  bare names are provider-native vocabulary.
- The check fails closed. Running with no `CAO_TERMINAL_ID` is the supported operator context
  (`cao assign` and `cao handoff`) and is unrestricted, but once a caller presents an identity,
  anything that stops it being resolved refuses the call rather than allowing it.

`cao assign` and `cao handoff` on the command line are unaffected: the operator is not an agent
and no profile allowlist applies to them.

Installed workflow callers with an explicit restricted allowlist must include
`@cao-mcp-server` to execute or resume workflows; installed profiles do not bypass
this check. Ephemeral callers additionally remain subject to
`ephemeral.child_may_delegate`. Read-only workflow inspection is unchanged.

Holding `@cao-mcp-server` lets a caller *reach* `assign`, `handoff` and the workflow
tools. What those tools may launch is a second check, the delegation ceiling below:
the MCP layer prechecks the child's resolved tools against the caller's recorded
ceiling and returns the refusal early, and cao-server repeats the check from the
caller's stored row before any terminal exists. The server is authoritative.

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

An unrecognized `role` raises `ValueError` at install, launch, and delegation. It does not fall open to `["*"]`. Define the name under `agents.roles`, or omit `role` for developer defaults. An explicit `allowedTools` list is applied and does not raise.

Note: MCP servers a profile declares in `mcpServers` are added to the resolved list automatically at levels 4 and 5 only. At levels 2 and 3 the list you write is the list you get, so a profile that declares a server and also sets `allowedTools` has to name `@<server>` for it to be granted. That is what lets a profile configure a server without granting it, and it keeps `--allowed-tools` and `allowedTools` resolving the same list to the same policy.

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
| **Kiro CLI** | Hard (install time) | `tools` in the agent JSON is written from the resolved `allowedTools` by `cao install`; `--trust-all-tools` only suppresses prompts for the tools that remain; launch-time `--allowed-tools` and role overrides select the installed agent and do not change its `tools`. Profiles installed before this change still carry `tools: ["*"]` until reinstalled |
| **Copilot CLI** | Hard | `--deny-tool` flags override `--allow-all` |
| **OpenCode CLI** | Hard | `permission:` YAML frontmatter enforced natively at install time; launch-time `--allowed-tools` and role overrides select the installed agent and do not change its permissions |
| **Grok Build CLI** | Native (mapped families) | Restricted profiles use deny-by-default `--permission-mode dontAsk` with explicit native/MCP allows and defense-in-depth denies; native subagents are disabled |
| **Kimi CLI** | Soft | Security system prompt only |
| **MiniMax Code** | Soft | Security bootstrap prompt only |
| **OMP** | Soft | Security system prompt only |
| **Codex** | Soft | Security system prompt only |
| **Antigravity CLI** | Soft | Security system prompt only |
| **Hermes** | Profile-defined | CAO launches default `hermes` or the optional `hermesProfile` wrapper declared by the CAO profile; restrict tools in that Hermes profile |
| **Cursor CLI** | Not enforced (v2026) | `allowedTools` is currently ignored — no native flag or system-prompt path is active; see [Cursor CLI Tool Restrictions](cursor-cli.md#tool-restrictions) |

**Hard enforcement** = the agent physically cannot use denied tools, enforced by the provider runtime.

**Soft enforcement** = a system prompt tells the agent not to use certain tools. The agent may still attempt them. Use hard-enforcement providers for security-critical work.

**Ephemeral agents** have their own provider table, because not every provider runs them. A create response's `enforcement` field reports `hard` on Claude Code and `advisory` on Codex, and Kiro ephemerals are refused. See [Ephemeral agents](ephemeral-agents.md#create-surface-and-stored-files).

### What "hard" looks like per provider

**Claude Code** — Adds `--disallowedTools` flags to the launch command:
```bash
claude --dangerously-skip-permissions --disallowedTools Bash --disallowedTools Edit --disallowedTools Write
```

`permissionMode` is a separate axis from `--disallowedTools`: `permissionMode` controls which permission tier the session runs under (unconditional bypass vs. classifier-gated tiers like `auto`), while `--disallowedTools` enforces the per-tool denylist. The two stack — a profile can set `permissionMode: auto` *and* a tool denylist, and both apply on the launch command. See [Permission Mode Override](claude-code.md#permission-mode-override) for full details.

**Kiro CLI** — Writes both `allowedTools` and `tools` into the agent JSON at install time:
```json
{
  "allowedTools": ["@cao-mcp-server", "fs_read", "fs_list"],
  "tools": ["@cao-mcp-server", "fs_read", "glob", "grep", "knowledge", "read"]
}
```
In Kiro `allowedTools` only names tools that run without an approval prompt
(and CAO passes `--trust-all-tools`, so every remaining tool runs unprompted);
`tools` decides what exists for the agent, and that is where the CAO policy is
applied. An unrestricted policy writes `tools: ["*"]`; a profile that sets its
own `tools` list keeps it. Enforcement is therefore install-time: change the
profile, re-run `cao install`. Profiles installed before CAO wrote `tools`
still carry `["*"]`; `cao launch` warns when it finds one.

**Copilot CLI** — Adds `--deny-tool` flags that override `--allow-all`:
```bash
copilot --allow-all --deny-tool shell --deny-tool write
```

**Grok Build CLI** — For a restricted profile, uses deny-by-default
`--permission-mode dontAsk`, explicitly grants mapped native tools and known
configured MCP servers, adds native `--deny` rules as defense in depth, and
disables Grok-native worker routes by default:

```bash
GROK_SUBAGENTS=0 GROK_WORKFLOWS=0 GROK_GOAL=0 \
  grok --permission-mode dontAsk --no-subagents \
  --allow Read --allow Grep --allow 'MCPTool(cao-mcp-server__*)' \
  --deny Bash --deny Edit --deny Write
```

`allowedTools: ["*"]` remains the unrestricted path and uses
`--always-approve`. Grok may retain built-in read-only operations in some
permission modes; this provider limitation is not represented by CAO's
`allowedTools` vocabulary. An explicit empty allowlist sends `--deny *`, while
restricted profiles explicitly grant mapped native/MCP families and literal,
configured server names (plus `cao-mcp-server`).

Set `grokNativeWorkflows: true` in a Grok agent profile only when intentionally
allowing Grok-native workers outside CAO's orchestration accounting. It is
separate from `allowedTools`, including `allowedTools: ["*"]`.

See the [Grok Build CLI provider guide](grok-cli.md#tool-restrictions) for the
complete mapping and isolation behavior.

**Kimi CLI / MiniMax Code / Codex** — Prepends to the system or bootstrap prompt:
```
You may ONLY use these tools: @cao-mcp-server, fs_read, fs_list
Do NOT attempt to use: execute_bash, fs_write
```

## Cross-Provider Inheritance

When a supervisor delegates work via `handoff()` or `assign()`, the child agent gets its own `allowedTools` resolved from its profile, **bounded by the parent's delegation ceiling**. The rule, in one sentence:

> A child's `allowedTools` must fit inside its creator's ceiling, and the child's own ceiling is its declared ceiling intersected with the creator's.

The ceiling is read from the **creator's terminal record**, written when that terminal was created, never re-derived from a profile. So a reviewer that launches a profile declaring `role: supervisor` gets a worker whose recorded ceiling is still the reviewer's, and that worker cannot launch a developer either. Ceilings only narrow down a tree.

```
Supervisor (role: supervisor → @cao-mcp-server, fs_read, fs_list; delegates *)
  │
  ├─ assign("developer")
  │    → Developer profile: role: developer → full access, fits inside *
  │    → Claude Code launched with no --disallowedTools; its ceiling: its own list
  │
  └─ handoff("reviewer")
       → Reviewer profile: role: reviewer → read-only, fits inside *
       → Claude Code launched with --disallowedTools Bash Edit Write

Reviewer (role: reviewer → @builtin, fs_read, fs_list, @cao-mcp-server; delegates its own list)
  │
  ├─ assign("developer")
  │    → REFUSED: "Delegation refused: terminal 3f9a… (profile 'reviewer') may not
  │       launch 'developer' with tools it cannot delegate: fs_*, execute_bash, web_fetch.
  │       … Remedy: declare `delegateTools` on the parent's profile …"
  │
  └─ assign("reviewer")
       → fits; the worker's ceiling is the reviewer's list
```

What is compared: `*` is satisfied only by `*`; `fs_*` by `fs_*` or all three of `fs_read`, `fs_list`, `fs_write`; `@builtin` grants nothing on any provider and is ignored (so `supervisor`, which never lists it, can launch `reviewer`, which does). MCP references use the same selector semantics the provider resolvers use to grant servers at launch: a ceiling `@search-*` covers a child `@search-web` (case-sensitive glob, exact names always match), a whole-server grant `@server` covers `@server/tool`, and a per-tool grant never covers the whole server. A child entry that is itself a pattern (`*`, `?` or `[`) is a language, not a name: it is covered only by the byte-identical grant or by a bare `*` grant (`@*`, `@server/*`), never by matching it as text, so `@search-?` does not admit `@search-*`. Every other entry, including `discovery`, must appear by name; unknown or oddly cased names are excess. A child profile that declares `allowedTools: ["*"]` is honoured only under a `*` ceiling (issue #141, within the ceiling); under a restricted parent the launch is refused rather than silently narrowed.

The same rule covers every way a terminal is created: `assign` and `handoff` (local and `target_host`), the in-session HTTP create (`POST /sessions/{name}/terminals` now **requires** `caller_id`; a client-supplied `allowed_tools` string is checked against that caller's recorded ceiling and never trusted beyond it; an operator adding a terminal to an existing session, as the Web UI's **Add Agent** does, uses the distinct `POST /sessions/{name}/terminals:operator` sub-path, which records the profile's declared ceiling like `POST /sessions` and refuses with 400 any request that also names a caller), `cao agent run-step`, workflow steps on both tiers (YAML steps and the run-step calls a `.py` script dispatches), which inherit the ceiling of the terminal that ran `workflow_run`, `workflow_start`, `workflow_resume` or the `cao workflow` CLI from inside a terminal, and helper terminals such as the `memory_manager` sidecar, which are bounded by the primary terminal's recorded ceiling. A refused workflow step settles FAILED with the refusal text; the run finishes as any other failure. An operator shell (no `CAO_TERMINAL_ID`) has no ceiling: `cao launch`, `cao assign` and `cao handoff` from the command line are unchanged, and the profile's declared ceiling is what the new terminal records. Ephemeral agents (`docs/ephemeral-agents.md`) have had this rule since they shipped; installed profiles now follow the same comparison.

Install-time providers run the policy `cao install` wrote, not the list a create request resolved. For a create bounded by a **restricted** ceiling on Kiro CLI the installed agent's `tools`, and on OpenCode CLI the installed agent's `permission:` block, must fit inside the bounded allowlist; a wider installed file is refused before anything is allocated, with the remedy (reinstall the profile with the narrower policy, or do not override `allowed_tools`). When CAO cannot read the installed policy at all — no agent file, or (Kiro) a file with no `tools` list, or (OpenCode) a file with no dict `permission:` block or front matter that does not parse — the same bounded create is refused with the install remedy rather than skipping the comparison. Operator launches and unrestricted delegators keep the existing launch-time warning.

The terminal that starts a workflow is recorded on the run's journal row, so a run resumed after a server restart, by the operator or by another agent, re-binds to the **original** caller's ceiling and is never widened. A script-tier step whose run is journaled but has no live record on the server (the server restarted under a running script) is refused with 403 rather than launched unbounded; `cao workflow resume <run_id>` rebuilds the run from the journal.

Remote placement (`assign`/`handoff` with `target_host`): the target node has no record of the supervisor, so the supervisor's recorded ceiling travels in the request as `caller_ceiling` and the remote node bounds the worker by it. The caller first checks the target's `/health` for the `delegation_ceiling` feature and **refuses to place work on a node that lacks it**, because an older node would accept the request and launch the worker unbounded. On the receiving side, a create that carries the remote-assign callback environment (`CAO_CALLBACK_TERMINAL_ID`) **must** carry `caller_ceiling`; when that callback terminal has a row on the receiving node (single-node or loopback fleets) the asserted ceiling must also fit inside what that row records. Upgrade both nodes together, or omit `target_host`.

**The `memory_manager` sidecar needs a covering primary.** `cao launch --memory` (`POST /sessions?memory_manager=true`) launches a `memory_manager` helper beside the primary, bounded by the primary's recorded ceiling. The sidecar's profile resolves to `@cao-mcp-server, fs_read, fs_list`, so the primary's ceiling must cover all three: the built-in `supervisor`, `reviewer` and `developer` roles do; `workflow_scout` (no `fs_list`) and a profile with an explicit `allowedTools` list that omits `@cao-mcp-server` (for example `examples/tool-restrictions/tool_restrictions_custom_restricted.md`) do not. Under such a primary the sidecar is refused, the primary still launches, the response carries `memory_manager: {"state": "refused", "detail": ...}` and `cao launch` prints the refusal as a warning. Add `delegateTools` covering the three tools to the primary's profile, or launch without `--memory`.

**What the server can and cannot know.** `caller_id` and `caller_ceiling` are assertions made by a client holding the shared bearer. The server refuses their *omission* on the paths agents use and bounds them by stored rows wherever a row exists, but it cannot tell a supervisor's MCP process from any other API client presenting the same bearer: a client that substitutes another terminal's id, or sends an operator-shaped `POST /sessions` (or `POST /sessions/{name}/terminals:operator`) with no callback environment, is treated as that terminal or as the operator. Binding caller identity to an authenticated terminal (per-terminal credentials) is the follow-up, tracked publicly in issue [#965](https://github.com/awslabs/cli-agent-orchestrator/issues/965); until then, treat API access as operator access. "Bounded" also means bounded at the level the provider enforces: on the soft-enforcement providers in the table above (Codex, Antigravity CLI and the other prompt-only providers) a child whose request fits the ceiling is still only restricted by prompt, so a Codex child can hold a shell its ceiling says it may not use. The ceiling bounds what is *granted*; the provider decides what is *enforced*.

**Migration.** The ceiling is recorded in a new `delegate_tools` column on the terminals table, added automatically at server start. Terminals created before the upgrade have no recorded ceiling and are bounded by their own recorded `allowedTools` until relaunched: a supervisor launched before the upgrade cannot `assign("developer")` until you relaunch it (the refusal says so). A pre-upgrade row that recorded no allowlist either (a launch whose profile could not be resolved) has no authority to delegate at all and is refused until relaunched; the profile on disk is never consulted for an existing terminal, so editing it cannot widen one. Every shipped example keeps working, because the pattern they teach is supervisor → developer. If you wrote a restricted profile that launches wider workers, add `delegateTools` to it; the error names the tools to add.

## Quick Reference

| I want to... | Do this |
|-------------|---------|
| Restrict a supervisor to orchestration + reading | `role: supervisor` |
| Give full access to a developer | `role: developer` (or set nothing) |
| Read-only reviewer | `role: reviewer` |
| Custom tool set | `allowedTools: ["fs_read", "execute_bash"]` |
| Reusable custom preset | Define in `settings.json` `roles`, use `role: my_preset` |
| Let a restricted agent launch wider workers | `delegateTools: [...]` on that agent's profile, then relaunch it |
| Override role at launch | `--allowed-tools fs_read --allowed-tools @cao-mcp-server` |
| Skip confirmation in scripts/automation | `--auto-approve` (restrictions still enforced) |
| No restrictions at all | `--yolo` |
| Check what's allowed before launch | Launch without `--yolo` or `--auto-approve` — the prompt shows the summary |

## Security Recommendations

1. **Use `role: supervisor` for orchestrators.** They only need MCP tools + file reading for context.
2. **Don't use `--yolo` in production.** It grants unrestricted access and skips all safety prompts.
3. **Prefer hard-enforcement providers** (Claude Code, Copilot CLI, Grok Build CLI, OpenCode CLI, Kiro CLI) for sensitive workloads. Kiro CLI and OpenCode CLI enforce at install time: reinstall a profile after changing its policy.
4. **Review the confirmation prompt.** It shows exactly what tools are allowed and blocked before you proceed.
5. **Prompt-only providers (Kimi CLI, MiniMax Code, Codex, Antigravity CLI, OMP; see the table) use soft enforcement** — use these only for non-critical tasks.

## Known Limitations

1. **Claude Code tool mapping is nearly complete, with MCP tools the remaining gap.** The current mapping covers `Bash` (and its `Task`/`Agent`/`Monitor`/`BashOutput`/`KillShell` execution family), `Read`, `Edit`, `Write`, `Glob`, `Grep`, and — via `web_fetch` — [`WebFetch`](https://code.claude.com/docs/en/permissions#webfetch) and `WebSearch`. The subagent tool is intentionally **not** a separate category: it is folded into `execute_bash`, because a subagent spawns with its own full toolset and can run shell, so exposing it standalone would let a profile grant subagent access without `execute_bash` and re-open that escape. Claude Code **renamed this tool from `Task` to `Agent`**, so both names are denied — current builds expose only `Agent`, so denying just `Task` would be a silent no-op. Provider MCP tools remain unmapped (see limitation #2) — they cannot be blocked via `--disallowedTools`.

2. **`@cao-mcp-server` is server-level, not per-tool control.** Grok restricted profiles translate it to an allow rule for the configured CAO MCP server; other providers generally treat it as an intent marker. No provider currently blocks individual MCP tools: once the server is available, its `handoff`, `assign`, `send_message`, and `answer_user_prompt` tools are all exposed. **CAO enforces the server-level grant on its own side for `assign`, `handoff` and `assign_elastic`** (see [MCP-side enforcement](#mcp-side-enforcement-for-tools-that-launch-an-agent) above), so a profile without `@cao-mcp-server` cannot use them even where the provider still exposes them. `send_message` and `answer_user_prompt` are not gated this way. `answer_user_prompt` is exposed by the MCP server, but its structured prompt-navigation behavior is currently implemented for Hermes workers that report `waiting_user_answer`; other providers may only receive ordinary text input until they implement equivalent prompt states. Future versions may support `@cao-mcp-server:send_message` syntax for per-tool MCP control.

3. **Soft enforcement is best-effort.** The prompt-only providers in the table above (Kimi CLI, MiniMax Code, Codex, Antigravity CLI, OMP) rely on prompt instructions to restrict tools. The agent may ignore these restrictions. Do not rely on soft enforcement for security-critical workloads.

## Example Profiles

For complete working examples with `role` and `allowedTools`, see the [examples directory](../examples/):

- **[tool-restrictions/](../examples/tool-restrictions/)** — Supervisor/developer/reviewer role defaults plus an explicit `allowedTools` override, with a launch-by-launch breakdown of what's allowed and denied
- **[assign/](../examples/assign/)** — Supervisor + worker agents with role-based restrictions
- **[cross-provider/](../examples/cross-provider/)** — Mixed-provider workflows with per-agent tool restrictions
- **[codex-basic/](../examples/codex-basic/)** — Codex agents with soft enforcement
