# Tool Restrictions Example

This example demonstrates how CAO resolves **tool access per agent role** —
the mechanism documented in full in
[docs/tool-restrictions.md](../../docs/tool-restrictions.md). It is not
another orchestration-pattern example (see [examples/assign/](../assign/) for
`assign` vs `handoff` mechanics); it exists purely to show, with real launch
commands and real resolved tool lists, how `role` and `allowedTools` combine
to decide what an agent can and cannot do.

## Naming note

The profiles here are prefixed `tool_restrictions_` (e.g.
`tool_restrictions_developer.md`, not `developer.md`). CAO ships **built-in**
profiles named plain `developer` and `reviewer`
(`src/cli_agent_orchestrator/agent_store/`), and `cao install` stores a
profile under its filename stem — installing a file literally named
`developer.md` would silently overwrite your local copy of the built-in
`developer` profile. `examples/codex-basic/` avoids the same collision by
naming its profiles `codex_developer`/`codex_reviewer` rather than bare
`developer`/`reviewer`; this example follows that precedent.

## Pattern Overview

Four profiles, four distinct tool-access patterns:

| Profile | Pattern | `role` | `allowedTools` |
|---------|---------|--------|----------------|
| `tool_restrictions_supervisor.md` | Role default — orchestration only | `supervisor` | (unset — inherited from role) |
| `tool_restrictions_developer.md` | Role default — full access | `developer` | (unset — inherited from role) |
| `tool_restrictions_reviewer.md` | Role default — read-only | `reviewer` | (unset — inherited from role) |
| `tool_restrictions_custom_restricted.md` | Explicit override — no role | (unset) | `["fs_read", "fs_list", "execute_bash"]` |

The first three map a `role` to its built-in default via the table in
[docs/tool-restrictions.md](../../docs/tool-restrictions.md#1-role--the-simple-way).
The fourth sets `allowedTools` directly with no `role` at all, proving
`allowedTools` is a complete specification on its own.

## Setup

```bash
# Start the CAO server
cao-server

# Install the agent profiles
cao install examples/tool-restrictions/tool_restrictions_supervisor.md
cao install examples/tool-restrictions/tool_restrictions_developer.md
cao install examples/tool-restrictions/tool_restrictions_reviewer.md
cao install examples/tool-restrictions/tool_restrictions_custom_restricted.md
```

## Resolution Walkthrough

Each profile below shows: the launch command, the confirmation prompt CAO
would show (per
[Launch Confirmation Prompt](../../docs/tool-restrictions.md#launch-confirmation-prompt)),
and the resulting allowed/denied tools — both in CAO's own vocabulary and as
native Claude Code tool names (using the translation table in
[Tool Vocabulary](../../docs/tool-restrictions.md#tool-vocabulary)).
Claude Code is a **hard-enforcement** provider — see
[Provider Enforcement](../../docs/tool-restrictions.md#provider-enforcement)
— so what's "denied" below is physically blocked, not just discouraged.

### `tool_restrictions_supervisor` — orchestration only

```bash
cao launch --agents tool_restrictions_supervisor --provider claude_code
```

```
Agent 'tool_restrictions_supervisor' launching on claude_code:
  Role:      supervisor
  Allowed:   @cao-mcp-server, fs_read, fs_list
  Directory: /home/user/my-project

Proceed? [Y/n]
```

| | Allowed | Denied |
|---|---|---|
| CAO vocabulary | `@cao-mcp-server`, `fs_read`, `fs_list` | `execute_bash`, `fs_write`, `web_fetch`, `discovery` |
| Claude Code native | `Read`, `Glob`, `Grep`, plus MCP tools (`handoff`, `assign`, `send_message`) | Bash family (`Bash`, `BashOutput`, `KillShell`, `Task`, `Agent`, `Monitor`), file writes (`Edit`, `Write`, `NotebookEdit`), network (`WebFetch`, `WebSearch`) — all via `--disallowedTools` |

### `tool_restrictions_developer` — full access

```bash
cao launch --agents tool_restrictions_developer --provider claude_code
```

```
Agent 'tool_restrictions_developer' launching on claude_code:
  Role:      developer
  Allowed:   @builtin, fs_*, execute_bash, web_fetch, @cao-mcp-server
  Directory: /home/user/my-project

Proceed? [Y/n]
```

| | Allowed | Denied |
|---|---|---|
| CAO vocabulary | `@builtin`, `fs_read`, `fs_write`, `fs_list`, `execute_bash`, `web_fetch`, `@cao-mcp-server` | `discovery` (not part of any built-in role — add it explicitly if this agent needs sibling discovery) |
| Claude Code native | Everything: `Read`, `Edit`, `Write`, `Glob`, `Grep`, `Bash`, `WebFetch`, `WebSearch`, plus MCP tools | Nothing — no `--disallowedTools` flags at all |

### `tool_restrictions_reviewer` — read-only

```bash
cao launch --agents tool_restrictions_reviewer --provider claude_code
```

```
Agent 'tool_restrictions_reviewer' launching on claude_code:
  Role:      reviewer
  Allowed:   @builtin, fs_read, fs_list, @cao-mcp-server
  Directory: /home/user/my-project

Proceed? [Y/n]
```

| | Allowed | Denied |
|---|---|---|
| CAO vocabulary | `@builtin`, `fs_read`, `fs_list`, `@cao-mcp-server` | `execute_bash`, `fs_write`, `web_fetch`, `discovery` |
| Claude Code native | `Read`, `Glob`, `Grep`, plus MCP tools | Bash family (`Bash`, `BashOutput`, `KillShell`, `Task`, `Agent`, `Monitor`), file writes (`Edit`, `Write`, `NotebookEdit`), network (`WebFetch`, `WebSearch`) — all via `--disallowedTools` |

**Nuance:** `supervisor` and `reviewer` deny the exact same native Claude
Code tools — both lack `execute_bash`/`fs_write`/`web_fetch`. The only CAO-
vocabulary difference between them is `@builtin`, which the translation
table marks `(internal)` for every provider: it isn't a real
`--disallowedTools` entry, so it never shows up as a difference at the
Claude Code enforcement layer. The difference between the two roles is
about intent and MCP-tool availability (both actually keep
`@cao-mcp-server` per the built-in role table), not about a distinct
Claude Code denylist. The `Task`/`Agent`/`Monitor`/`BashOutput`/`KillShell`
grouping under "Bash family" is deliberate, not an approximation — see
[Known Limitations #1](../../docs/tool-restrictions.md#known-limitations)
for why the subagent tool is folded into `execute_bash` rather than gated
separately.

### `tool_restrictions_custom_restricted` — explicit override, no role

```bash
cao launch --agents tool_restrictions_custom_restricted --provider claude_code
```

```
Agent 'tool_restrictions_custom_restricted' launching on claude_code:
  Role:      (not set)
  Allowed:   fs_read, fs_list, execute_bash
  Directory: /home/user/my-project

Proceed? [Y/n]
```

| | Allowed | Denied |
|---|---|---|
| CAO vocabulary | `fs_read`, `fs_list`, `execute_bash` | `fs_write`, `web_fetch`, `@cao-mcp-server`, `discovery` |
| Claude Code native | `Read`, `Glob`, `Grep`, `Bash` family (`Bash`, `BashOutput`, `KillShell`, `Task`, `Agent`, `Monitor`) | File writes (`Edit`, `Write`, `NotebookEdit`), network (`WebFetch`, `WebSearch`) — and no MCP tools at all, since this profile never configures the `cao-mcp-server` server |

Note this does **not** show the "no role or allowedTools set" reminder
that [Default Behavior](../../docs/tool-restrictions.md#default-behavior)
describes — that reminder only fires when neither is set. Here
`allowedTools` is set, so it's a complete, deliberate specification, not a
fallback.

## Override Hierarchy

[How Overrides Work](../../docs/tool-restrictions.md#how-overrides-work)
ranks five levels, highest priority first: `--yolo`, the `--allowed-tools`
CLI flag, `allowedTools` in the profile, `role` in the profile, then the
`developer` fallback. This example exercises the middle two directly and
the CLI flag on top of them:

- **`role` alone** — `tool_restrictions_supervisor`, `_developer`, and
  `_reviewer` set only `role`; CAO fills in `allowedTools` from the
  built-in role table.
- **`allowedTools` alone, no `role`** — `tool_restrictions_custom_restricted`
  sets `allowedTools` directly with no `role` at all. `allowedTools` is a
  complete specification; it doesn't need a role to fall back on.
- **`allowedTools` beats `role` when both are set** — this example doesn't
  ship a fifth file for this (the scope here is four profiles), but you can
  see it by editing `tool_restrictions_custom_restricted.md` to add
  `role: developer` above its `allowedTools` line: the resolved tools stay
  exactly `fs_read, fs_list, execute_bash` — `role: developer` would
  normally add `fs_write`/`web_fetch`/`@cao-mcp-server`/`@builtin`, but the
  explicit `allowedTools` list wins and those stay denied. This is the same
  override the docs demonstrate with their own `restricted_developer`
  example — see
  [`allowedTools` — The Precise Way](../../docs/tool-restrictions.md#2-allowedtools--the-precise-way).
- **The `--allowed-tools` CLI flag beats both**, at launch time, without
  editing any file:
  ```bash
  cao launch --agents tool_restrictions_custom_restricted --allowed-tools fs_read
  ```
  This launches with **only** `fs_read` allowed — `fs_list` and
  `execute_bash` from the profile's own `allowedTools` are dropped, because
  the CLI flag is a higher-priority override than the profile.

## Cross-Agent Delegation: Children Resolve Their Own Profile

Install and launch the supervisor, then give it a small task:

```bash
cao launch --agents tool_restrictions_supervisor
```

```
Implement is_valid_email(s) and have it reviewed.
```

```
tool_restrictions_supervisor (role: supervisor → @cao-mcp-server, fs_read, fs_list)
  │
  ├─ handoff("tool_restrictions_developer")
  │    → Developer profile resolves role: developer → full access
  │    → Claude Code launched with no --disallowedTools
  │
  └─ handoff("tool_restrictions_reviewer")
       → Reviewer profile resolves role: reviewer → read-only
       → Claude Code launched with --disallowedTools Bash Edit Write WebFetch WebSearch
```

The supervisor itself can't write files or run bash — but it can still
`handoff` to `tool_restrictions_developer`, which gets full write/execute
access, because that access comes from the **developer's own profile**, not
from anything the supervisor holds or grants. The same is true in reverse:
`tool_restrictions_developer` has full access, but the
`tool_restrictions_reviewer` it hands off to next is still read-only,
because the reviewer resolves its own restrictions too. Nothing is
inherited up or down the delegation chain — every `handoff`/`assign` target
gets its tool access from its own profile, resolved fresh at launch. See
[Cross-Provider Inheritance](../../docs/tool-restrictions.md#cross-provider-inheritance)
for the same pattern in the docs.

## Usage

In the supervisor terminal:

```
Implement a Python function is_valid_email(s) that validates email format,
then have it reviewed.
```

Expected flow:
1. Supervisor calls `handoff(tool_restrictions_developer, ...)` and blocks.
2. Developer writes `is_valid_email`, returns it.
3. Supervisor calls `handoff(tool_restrictions_reviewer, ...)` with that code and blocks.
4. Reviewer reads and critiques the code (cannot edit it), returns comments.
5. Supervisor presents the implementation plus the review to you.

You can also launch any profile standalone to see its restrictions in
isolation — `tool_restrictions_custom_restricted` in particular isn't part
of the delegation chain above; it's a self-contained example of the
override pattern:

```bash
cao launch --agents tool_restrictions_custom_restricted
```

## See Also

- [docs/tool-restrictions.md](../../docs/tool-restrictions.md) — the full
  spec for `role`, `allowedTools`, `--yolo`, override priority, and
  per-provider enforcement.
- [examples/assign/](../assign/) — `assign` vs `handoff` orchestration
  mechanics (this example deliberately keeps that part simple).
- [examples/cross-provider/](../cross-provider/) — the same delegation
  pattern across multiple CLI providers.
