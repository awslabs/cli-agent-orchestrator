# Memory System

CAO's memory system gives agents persistent, cross-session storage. Agents store facts, decisions, and preferences during a session; CAO injects relevant memories back as context when the agent starts its next session.


## Capabilities

- **Four scopes** (`global`, `project`, `session`, `agent`) and four type labels.
- **Store / recall / forget** via MCP tools and `cao memory` CLI commands.
- **Markdown wiki storage** with a SQLite metadata index.
- **Search**: keyword (BM25), recency, or hybrid; results ranked by recency, a
  composite 3-factor score (BM25 + recency + usage), or usage.
- **Cross-references** between related memories, expandable on recall.
- **Auto-injection** into each provider's config file on terminal creation, plus a
  `<cao-memory>` block prepended to the agent's first message.
- **LLM wiki compaction** (`cao memory compact`) that rewrites topic articles.
- **Linting** (`cao memory lint`) for orphans, contradictions, stale claims, and more.
- **Self-healing** (`cao memory heal`) that turns lint findings into fixes — dry-run
  by default, `--apply` to mutate, with a full audit trail.
- **Tiered retention / cleanup**, a daily audit log, and a memory Web UI.

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

`project` is the default scope. Project identity resolves via a precedence chain — a
`CAO_PROJECT_ID` / `memory.project_id` override, then the normalized git remote URL, then
`sha256(realpath(cwd))[:12]` as a fallback — so a project stays recallable across renames
and moves.

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
  query="testing",         # optional, searches content
  scope="project",         # optional, filter by scope
  memory_type=None,        # optional, filter by type
  limit=10,                # optional, default 10, max 100
  search_mode="hybrid",    # optional: "hybrid" (default), "bm25", "metadata"
  sort_by="recency",       # optional: "recency" (default), "score", "usage"
  include_related=False    # optional, expand cross-referenced memories
)
```

`sort_by` controls ranking: `recency` (newest first), `score` (composite 3-factor —
BM25 relevance + recency + usage), or `usage` (most accessed). When no `scope` is
given, results follow scope precedence: `session` > `project` > `global`.

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

# Lint the wiki for orphans, contradictions, stale claims, etc.
cao memory lint
cao memory lint --scope project --format json

# Compact wiki topics with the LLM compiler (repair sweep)
cao memory compact --scope global
cao memory compact --key testing-framework

# Repair lint findings — dry-run by default, --apply to mutate
cao memory heal --scope project              # dry-run plan
cao memory heal --scope project --apply
cao memory heal --scope project --apply --aggressive   # also heal poison_frequency
```

`cao memory heal` consumes the findings from `cao memory lint` and applies one fix per
issue type: it deletes orphan pages, resolves contradictions (keeping the newer article),
strips stale claims, and — only under `--aggressive` — zeroes poisoned access counts. It
is dry-run by default; pass `--apply` to mutate. Every applied mutation is written to the
daily audit log.

## Context Injection

CAO injects relevant memories into a new session two ways:

1. **First-message block** — when an agent receives its first message in a session,
   CAO prepends a `<cao-memory>` block containing relevant memories.
2. **Provider config file** — built-in plugins for Claude Code, Codex, and Kiro CLI
   write the same block into each provider's per-project config file (e.g.
   `.claude/CLAUDE.md`) on terminal creation, delimited by `cao-memory` markers so
   repeated runs overwrite the same section.

The block format:

```
<cao-memory>
## Context from CAO Memory
- [session] recent-decision: Use the existing auth middleware, do not rewrite
- [project] testing-framework: Always use pytest for testing in this project
- [global] user-prefers-concise: User prefers concise responses without trailing summaries
</cao-memory>

<original user message>
```

Memories are selected in scope precedence order: `session` > `project` > `global`. Each
scope is independently capped — at most `MEMORY_MAX_PER_SCOPE` (10) entries and
`MEMORY_SCOPE_BUDGET_CHARS` (1000) characters per scope — so one scope cannot monopolize
the injection budget.

## Saving Memories

Agents call `memory_store` explicitly via MCP when they want to persist a fact; agent
profiles include guidance on when to store (see below). Hook- and plugin-driven injection
surfaces stored memories back into later sessions automatically.

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
