# Memory System

CAO's memory system gives agents persistent, cross-session storage. Agents store facts, decisions, and preferences during a session; CAO injects relevant memories back as context when the agent starts its next session.


## Capabilities

- **Five scopes** (`global`, `project`, `session`, `agent`, `federated`) and four type labels.
- **Store** via the `memory_store` MCP tool; **recall / forget** via MCP tools or the `cao memory` CLI.
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

## Architecture: how it works with SQLite

CAO keeps memory in **two coupled stores**, and it helps to know which does what:

- **Markdown wiki files** — the **content** store. One file per memory key, holding
  the human-readable article and its timestamped entries (see
  [Storage Layout](#storage-layout)).
- **SQLite** (`memory_metadata` table) — the **metadata / index** store and the
  **source of truth** for metadata queries. One row per wiki file. The DB lives at
  `~/.aws/cli-agent-orchestrator/.../cli-agent-orchestrator.db`.

Every `memory_store` writes **both**: it writes/updates the markdown file *and* upserts
the matching row in `memory_metadata`. Every `memory_recall` reads **both**: BM25
keyword search runs over the wiki **content**, while recency, usage, and the composite
3-factor scoring read **columns from SQLite** (`last_accessed_at`, `access_count`,
`created_at`, `updated_at`, `tags`, `token_estimate`, `related_keys`, …).

```
STORE
  agent (MCP) ─┐
               ├─▶ memory_store ─▶ MemoryService ─┬─▶ Markdown wiki file  (CONTENT)
  (no CLI      │   (resolves scope_id)            │   memory/.../wiki/{scope}/{key}.md
   store path) ┘                                  │
                                                  └─▶ SQLite: memory_metadata  (upsert)
                                                      (METADATA / INDEX — source of truth)
                                                      1 row/file: key, scope, scope_id,
                                                      tags, access_count, timestamps,
                                                      token_estimate, related_keys

RECALL
  agent (MCP) ─┐
               ├─▶ memory_recall ─▶ MemoryService ─┬─▶ BM25 keyword search ▶ wiki CONTENT
  cao memory   │                                   │
  list/show ───┘                                   └─▶ recency / usage / 3-factor
                                                       ▶ SQLite metadata columns
                                                  │
                                                  └─▶ ranked results (metadata + content)
```

SQLite is the index and source of truth for metadata queries; the wiki files hold the
content. The CLI read/maintenance commands (`list`, `show`, `lint`, `compact`, `heal`)
go through the same `MemoryService` — only **writing new memories** is MCP-exclusive.

## Scope vs. Type — the one distinction that trips people up

These are two **orthogonal** dimensions. Getting them confused is the most common
source of "where did my memory go?" questions.

- **`scope`** decides **WHERE** the memory is stored on disk (and who can read it
  back). It is the only thing that controls location.
- **`memory_type`** is just a **classification label** stamped into the file — it
  **never** changes where the memory lands.

> There is **no "user folder" and no `user` scope.** `user` is a *type*, not a scope.
> A "user memory" is simply `memory_type="user"` — by convention stored at
> `scope="global"` so it applies across every project. The same is true of
> `feedback` and `reference`: all four types can be attached to any scope.

## Memory Scopes

Scope controls where a memory is stored and who can read it back.

| Scope | Storage location | scope_id | Use when |
|---|---|---|---|
| `global` | `memory/global/wiki/global/` | none (`None`) | Cross-project facts: user preferences, coding standards |
| `project` | `memory/{project_id}/wiki/project/` | `project_id` (cwd hash / override) | Project-specific: architecture decisions, conventions |
| `session` | `memory/global/wiki/session/{session_name}/` | `session_name` | Ephemeral: notes for current session only |
| `agent` | `memory/global/wiki/agent/{agent_profile}/` | `agent_profile` | Role-specific: patterns the agent role always applies |
| `federated` | `memory/federated/wiki/federated/` | none (`None`) | Machine-wide shared tier: facts shared across all projects on this host |

`project` is the default scope. Project identity resolves via a precedence chain — a
`CAO_PROJECT_ID` / `memory.project_id` override, then the normalized git remote URL, then
`sha256(realpath(cwd))[:12]` as a fallback — so a project stays recallable across renames
and moves.

**scope_id resolution.** `global` and `federated` need no id (`scope_id=None`). Every
other scope requires a resolvable id, taken from the terminal's context — `project` from
`realpath(cwd)`, `session` from `session_name`, `agent` from `agent_profile`. If a
non-`global`/non-`federated` store can't resolve an id, it raises `ValueError` rather
than writing to the wrong place.

> **Note:** `session` and `agent` scopes are stored *under* the `global` container,
> nested by their `scope_id`. Only `project` and `federated` get their own top-level
> directory.

## Memory Types

Type is a classification label — it does not affect storage location (see
[Scope vs. Type](#scope-vs-type--the-one-distinction-that-trips-people-up) above).

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

> **Storing is MCP-only.** There is no `cao memory store` command — the CLI can
> `list`/`show`/`delete`/`clear`/`lint`/`compact`/`heal`/`export`/`import`, but *writing* a
> memory always goes through the `memory_store` MCP tool (`import` is the one
> exception: it bulk-writes bundle topics, each routed through the same store
> pipeline).

#### Storing into each scope

The only argument that changes *where* the memory lands is `scope`. `memory_type` is
carried along as a label and is orthogonal — pass `user`, `feedback`, `reference`, or
`project` with any scope. Each non-`global` scope needs a piece of the terminal's
context to resolve its `scope_id`; if it can't, the store raises `ValueError`.

```python
# global — cross-project. No scope_id needed.
memory_store(
  content="User prefers concise answers with no trailing summary",
  scope="global",
  memory_type="user",          # a TYPE label, not a scope — lands in global anyway
  key="answer-style",
)

# project (default) — scope_id = sha256(realpath(cwd))[:12], from the terminal's cwd
memory_store(
  content="Always use pytest for testing in this project",
  scope="project",
  memory_type="feedback",
  key="testing-framework",
  tags="testing,pytest",
)

# session — scope_id = session_name, from the terminal context
memory_store(
  content="For this session, reuse the existing auth middleware; do not rewrite",
  scope="session",
  memory_type="project",
  key="auth-decision",
)

# agent — scope_id = agent_profile, from the terminal context
memory_store(
  content="As the reviewer, always check for missing error handling first",
  scope="agent",
  memory_type="project",
  key="review-checklist",
)

# federated — machine-wide shared tier. No scope_id needed.
memory_store(
  content="This host's internal package index is at http://pkgs.corp.local/simple",
  scope="federated",
  memory_type="reference",
  key="internal-pypi",
)
```

**Terminal context each scope needs to resolve `scope_id`:**

| Scope | Needs from terminal context |
|---|---|
| `global` | nothing |
| `federated` | nothing |
| `project` | `cwd` → hashed to `project_id` (or `CAO_PROJECT_ID` / git remote override) |
| `session` | `session_name` |
| `agent` | `agent_profile` |

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

# Export a scope as an OKF archive bundle (directory, or .tar.gz file path)
cao memory export --scope global -o ./memory-bundle
cao memory export --scope global -o ./memory-bundle.tar.gz
cao memory export --scope session -o ./bundle --include-private   # private scopes need the flag
cao memory export --scope global -o ./bundle --include-history --redact --prune

# Import a bundle directory into a scope (global/project/federated only)
cao memory import ./memory-bundle --scope global
cao memory import ./memory-bundle --scope project --conflict merge
cao memory import ./memory-bundle --scope global --dry-run   # full pipeline, no writes
```

`cao memory heal` consumes the findings from `cao memory lint` and applies one fix per
issue type: it deletes orphan pages, resolves contradictions (keeping the newer article),
strips stale claims, and — only under `--aggressive` — zeroes poisoned access counts. It
is dry-run by default; pass `--apply` to mutate. Every applied mutation is written to the
daily audit log.

`cao memory export` writes one scope as an OKF bundle. Flags: `--scope` (required),
`-o/--output` (required — a directory, or a `.tar.gz` path for a tarball),
`--include-private` (required to export the private `session`/`agent` scopes),
`--include-history` (emit `history/<key>.md` files), `--redact` (redact secret matches
instead of skipping the topic), and `--prune` (directory output only — delete destination
topics no longer in the scope). `--format` selects the archive backend (`okf` today).
The same bundle is available over HTTP via `GET /memory/export` (see
[docs/api.md](api.md)), which never exports private scopes.

`cao memory import` reads a bundle directory back into a scope. The bundle is treated as
untrusted input: target `--scope` is required and limited to `global`/`project`/`federated`,
every topic runs through the store pipeline's validation and secret gate, and structural
markers that would spoof entry metadata are escaped. `--conflict` picks the policy when a
key already exists (`skip` (default) / `replace` / `merge`); `--dry-run` runs the full
parse/validate/secret pipeline and reports without writing.

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
├── {project_id}/                 # e.g. 14ae6bda7bac (sha256(realpath(cwd))[:12])
│   └── wiki/
│       ├── index.md              # index of this project's memories
│       └── project/
│           └── {key}.md
└── federated/                    # machine-wide shared tier
    └── wiki/
        └── federated/
            └── {key}.md
```

Note how `session` and `agent` live *inside* the `global/` container (nested by
`scope_id`), while `project` and `federated` each get their own top-level container.

Each wiki file is a markdown document with YAML-like comment header and timestamped entries:

```markdown
# testing-framework
<!-- id: abc123 | scope: project | type: feedback | tags: testing,pytest -->

## 2026-04-16T10:30:00Z
Always use pytest for testing in this project. Do not use unittest.
```

### What each scope looks like on disk

The **same key** (`review-notes`) stored at different scopes lands in different files —
scope is the only thing that moves it. The `type:` in the header is just the label you
passed as `memory_type`.

```markdown
# global  →  memory/global/wiki/global/review-notes.md
# review-notes
<!-- id: g1 | scope: global | type: user | tags: style -->

## 2026-07-01T09:00:00Z
Prefer concise reviews; lead with the highest-severity finding.
```

```markdown
# project →  memory/14ae6bda7bac/wiki/project/review-notes.md
# review-notes
<!-- id: p1 | scope: project | type: project | tags: style -->

## 2026-07-01T09:00:00Z
In this repo, flag any bare `except:` — it violates the project rules.
```

```markdown
# session →  memory/global/wiki/session/refactor-auth/review-notes.md
# review-notes
<!-- id: s1 | scope: session | type: project | tags: style -->

## 2026-07-01T09:00:00Z
For this session only: skip nits, focus on the auth boundary.
```

```markdown
# agent   →  memory/global/wiki/agent/reviewer/review-notes.md
# review-notes
<!-- id: a1 | scope: agent | type: project | tags: style -->

## 2026-07-01T09:00:00Z
As the reviewer role: always check error handling before naming.
```

```markdown
# federated → memory/federated/wiki/federated/review-notes.md
# review-notes
<!-- id: f1 | scope: federated | type: reference | tags: style -->

## 2026-07-01T09:00:00Z
House review guide for this host: link at http://pkgs.corp.local/review.
```

## Retention

Retention is keyed on **scope**, with one override for memory type:

| Scope | Retention |
|---|---|
| `global` | Never expires |
| `project` | 90 days since last update |
| `session` | 14 days |
| `agent` | Never expires |
| `federated` | Never expires |

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
