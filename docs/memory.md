# Memory System

CAO's memory system gives agents persistent, cross-session storage. Agents store facts, decisions, and preferences during a session; CAO injects relevant memories back as context when the agent starts its next session.

## How It Works

1. **Agent stores a memory** via `memory_store` MCP tool during a session
2. **CAO persists it** as a markdown wiki file under `~/.aws/cli-agent-orchestrator/memory/`
3. **On next session start**, CAO injects matching memories as a `<cao-memory>` context block before the agent's first message
4. **Agent recalls** with `memory_recall` when it needs to look something up explicitly

## Memory Scopes

Scope controls where a memory is stored and who can read it back.

| Scope | Storage location | Use when |
|---|---|---|
| `global` | `memory/global/wiki/global/` | Cross-project facts: user preferences, coding standards |
| `project` | `memory/{cwd_hash}/wiki/project/` | Project-specific: architecture decisions, conventions |
| `session` | `memory/global/wiki/session/` | Ephemeral: notes for current session only |
| `agent` | `memory/global/wiki/agent/` | Role-specific: patterns the agent role always applies |

`project` is the default scope. The project hash is `sha256(realpath(cwd))[:12]`.

> **Note:** `session` and `agent` scopes are stored under the global container, not in their own top-level directories. Only `project` scope gets a dedicated directory keyed by project hash.

## Memory Types

Type is a classification label — it does not affect storage location.

| Type | Use for |
|---|---|
| `project` | Architecture notes, project conventions (default) |
| `user` | User preferences, working style |
| `feedback` | Corrections, recurring mistakes to avoid |
| `reference` | Pointers to external resources, docs, links |

## MCP Tools

Agents use these tools via the `cao-mcp-server` MCP server.

### `memory_store`

Store or update a memory. If the key already exists, the new content is appended as a timestamped entry (upsert).

```
memory_store(
  content="Always use pytest for testing in this project",
  scope="project",          # optional, default: "project"
  memory_type="feedback",   # optional, default: "project"
  key="testing-framework",  # optional, auto-generated from content if omitted
  tags="testing,pytest"     # optional
)
```

### `memory_recall`

Search memories by keyword query and optional filters.

```
memory_recall(
  query="testing",     # optional, searches content
  scope="project",     # optional, filter by scope
  memory_type=None,    # optional, filter by type
  limit=10             # optional, default 10, max 100
)
```

Results are returned sorted by recency, with scope precedence: `session` > `project` > `global`.

### `memory_forget`

Remove a memory by key.

```
memory_forget(
  key="testing-framework",
  scope="project"
)
```

## CLI Commands

```bash
# List memories (shows global + current project by default)
cao memory list
cao memory list --all              # all projects
cao memory list --scope global
cao memory list --type feedback

# Show full content of a memory
cao memory show <key>
cao memory show <key> --scope global

# Delete a memory
cao memory delete <key>
cao memory delete <key> --scope project --yes

# Clear all memories for a scope
cao memory clear --scope session --yes
```

## Import / Export

Memories can be moved between machines or checkouts as a single self-describing
`.tar.gz` bundle. The bundle carries both the wiki files and their SQLite metadata
(token estimates, access counts, `related_keys`, timestamps, provenance) and is
content-hashed so import-side tampering is detectable.

```bash
# Export a scope to a deterministic bundle (.tar.gz or .tgz)
cao memory export --scope project --out project.tar.gz
cao memory export --scope global  --out global.tar.gz
cao memory export --scope project --scope-id <project_id> --out other.tar.gz

# Import a bundle — DRY-RUN by default (prints the plan, writes nothing)
cao memory import project.tar.gz
cao memory import project.tar.gz --apply                       # actually write
cao memory import project.tar.gz --on-conflict merge --apply   # skip|replace|merge
cao memory import project.tar.gz --target-project-id <id> --apply
```

`--scope` accepts `session`, `project`, `global`, or `federated` — `agent`-scope
memories are never exported or imported. Import is **dry-run by default**: without
`--apply` it prints `N new, M conflicts, K rejected` and touches nothing. `--on-conflict`
controls existing-key collisions (`skip` is the default; `replace` overwrites; `merge`
tie-breaks on updated-time, then access count, then key).

**Project identity is re-derived, never trusted.** A `project` bundle does not carry a
portable directory — its destination project_id is resolved on the *importing* machine
(from the current working directory, or pinned with `--target-project-id`), so the
memories land under the importer's own project, not the exporter's path. When a bundle's
identity came from a `cwd_hash` fallback (a local-only project with no git remote), import
**refuses to guess**: run it from inside the destination project, or pass
`--target-project-id <project_id>` explicitly. See the worked example in
[`docs/issues/memory-import-export.md`](issues/memory-import-export.md#worked-example--no-remote-project---target-project-id).

**Secret gate on import.** A bundle is untrusted input from another machine, so every
imported memory is screened with the same credential deny-list (`scan_for_secrets`) that
gates `federated` writes — for *all* target scopes, not just `federated`. A hit rejects
that row (reported by pattern name only, never the content) and rolls the whole import
back. Both commands raise `MemoryDisabledError` when memory is disabled.

## Context Injection

When an agent receives its first message in a session, CAO prepends a `<cao-memory>` block containing relevant memories (up to 3000 characters). The block format:

```
<cao-memory>
## Context from CAO Memory
- [session] recent-decision: Use the existing auth middleware, do not rewrite
- [project] testing-framework: Always use pytest for testing in this project
- [global] user-prefers-concise: User prefers concise responses without trailing summaries
</cao-memory>

<original user message>
```

Memories are selected in scope precedence order: `session` > `project` > `global`.

## Auto-Save

In Phase 1 there is no automatic save hook. Agents must call `memory_store` explicitly via MCP when they want to persist a fact. Agent profiles include guidance on when to store. Hook-driven auto-save is shipped via per-provider plugins in a subsequent PR.

## Storage Layout

```
~/.aws/cli-agent-orchestrator/memory/
├── global/
│   └── wiki/
│       ├── index.md              # index of all global/session/agent memories
│       ├── global/
│       │   └── {key}.md
│       ├── session/
│       │   └── {session_name}/
│       │       └── {key}.md
│       └── agent/
│           └── {agent_profile}/
│               └── {key}.md
└── {cwd_hash}/                   # e.g. 14ae6bda7bac
    └── wiki/
        ├── index.md              # index of this project's memories
        └── project/
            └── {key}.md
```

Each wiki file is a markdown document with YAML-like comment header and timestamped entries:

```markdown
# testing-framework
<!-- id: abc123 | scope: project | type: feedback | tags: testing,pytest -->

## 2026-04-16T10:30:00Z
Always use pytest for testing in this project. Do not use unittest.
```

## Retention

Retention is keyed on **scope**, with one override for memory type:

| Scope | Retention |
|---|---|
| `global` | Never expires |
| `project` | 90 days since last update |
| `session` | 14 days |
| `agent` | Never expires |

Memories with `memory_type` of `user` or `feedback` are operator-curated knowledge and never expire regardless of scope.

Cleanup runs automatically in the background when `cao-server` starts.

## Adding Memory Instructions to an Agent Profile

Add a `## Memory` section to the agent's system prompt:

```markdown
## Memory

When you discover something worth remembering — user preferences, project conventions,
important decisions, recurring corrections — store it immediately using the `memory_store`
CAO tool. Keep each memory to 1–2 sentences. Store decisions and conclusions, not conversation.
Use `memory_recall` to check if you already know something before asking the user.

Note: `memory_store` and `memory_recall` are CAO's cross-provider memory tools, distinct from
any provider-native memory system.
```
