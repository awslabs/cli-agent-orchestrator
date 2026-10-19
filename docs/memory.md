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

Vault-backed memory behavior, configuration, indexing, and retention limits are
documented in [Obsidian Vault Knowledge Source](obsidian-vault.md).

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
  sort_by="recency",       # optional: "recency" (default), "score", "usage", "relevance"
  include_related=False    # optional, expand cross-referenced memories
)
```

`sort_by` controls ranking: `recency` (newest first), `score` (composite 3-factor —
BM25 relevance + recency + usage), or `usage` (most accessed). When no `scope` is
given, results follow scope precedence: `session` > `project` > `global`.

`relevance` is opt-in and needs a query; omitted, empty and whitespace-only
queries follow the exact `recency` path. Other sort modes keep their existing
query handling. Hybrid relevance uses the existing widened pool; metadata and
BM25 relevance widen their single source too. Each source considers
`max(limit, min(200, max(50, 3 * limit)))` rows. Partial BM25 matches fill only
slots left by all-term matches, and all-term matches rank first.

Within each tier, rank is normalized BM25 times an age factor. Only native
project memories get `0.85 + 0.15 / (1 + days_old / 30)`; every vault row and
all other scopes are time-neutral. Future dates clamp to age zero; naive dates
mean UTC. An exact match aged 365 days has factor about 0.861, so a fresh match
with lexical strength 0.862 can beat it. These constants need rollout validation.
Ties use lexical strength and full identity, with no timestamp tie-break.
When a nonempty batch normalizes to all zeros (including batches with only
nonpositive raw BM25 scores), relevance falls back to full identity order
within each tier. Scope precedence and supersedes demotion still apply; an
age factor multiplied by zero has no effect. Explain reports this condition
as `lexical_degenerate`; ranking is unchanged.
Scope precedence still dominates when no scope is specified. Only returned
primary rows increment access; related expansion remains unchanged.

Active supersedes targets are always demoted in `score` and `relevance`, using
the existing same-scope relationship predicate. `memory.lifecycle.enabled`
does not undo this lexical-mode demotion. Explicit recall does not
exclude superseded memories. Default recall and injection are unchanged.
An older remote memory owner may reject the new sort value; upgrade both nodes.

### `memory_forget`

Remove a memory by key.

The result's `action` is authoritative. The legacy `deleted` boolean means CAO
completed the forget operation, not necessarily that a vault file was removed.
For a vault-backed memory, CAO de-indexes the note and retains its file; inspect
the returned `path` for the retained vault location.

```
memory_forget(
  key="testing-framework",
  scope="project"
)
```

### Injection eligibility sources

The in-tree `services.memory_eligibility` registry provides batch lifecycle
verdicts keyed by `(key, scope, scope_id, source_kind)`. Each enabled source is
queried once per new candidate batch; identities are memoized only within the
current injection build or curator recall. There is no cross-call cache or
automatic discovery of external sources. The built-in `lifecycle.supersedes` source is disabled by default.

Native entries, vault candidates and related targets are checked before their
caps; vault exclusions happen before body loading. The same check also covers
curator metadata, BM25, widened pools and related expansion. Candidate verdicts
are evaluated lazily in chunks, so no exclusions means one primary chunk of
10 per populated injection scope. Empty scopes require no verdict batch.
Related targets use a separate bounded batch when needed. The BM25 corpus
remains unfiltered so exclusions cannot change IDF.

Explicit operator/agent recall never excludes through this seam. Curator
recall is an injection path: it is recognized by the resolved policy or the
`memory_manager` requester profile (including distributed calls). Its requester
is the curator, not the worker it serves. Sources requiring worker identity
must fail open on `curated_recall` rather than trust task text as identity.

A source exception or invalid reason token fails open only that source's
batch. Other sources still apply. Warnings report source, purpose and count;
injection exclusion summaries report scope, count and source names, without
keys or content. **Eligibility is lifecycle, not confidentiality:** vault
binding policy and provider redaction retain their separate fail-closed gates.

### Supersedes lifecycle (opt-in)

Enable with `cao config set memory.lifecycle.enabled true` or
`CAO_MEMORY_LIFECYCLE_ENABLED=true` (environment > nested file value > default
false). Invalid nested lifecycle objects or non-boolean file values warn and
leave it disabled; read errors also leave it disabled. The built-in source
excludes targets of active same-scope `supersedes` edges from all four injection
paths. Proposals, rejected and deleted edges do not exclude. Store failures
fail open and appear in the source warnings and explain diagnostics. There is
no cross-call cache: edge changes apply on the next call.

Once lifecycle is enabled, any active `supersedes` edge removes its same-scope
target from every agent's injection, including curator recall. Such an edge
can be created through write/admin-scoped `POST /memory/relationships`, by
`cao memory relationships promote`, or in a vault note's `cao.links` (reconcile
marks links active by default when status is omitted). API authentication is
off by default. These writers therefore also control injection suppression;
the lifecycle flag adds no new authorization boundary.

Lifecycle changes do not retract provider memory blocks already persisted
in `<cwd>/.claude/CLAUDE.md`, `<cwd>/AGENTS.md` or Kiro's
`<cwd>/.kiro/steering/cao-memory.md`. Claude Code, Codex and Kiro memory
handlers refresh only their own provider's file when a terminal of that
provider is created in that cwd: `claude_code` writes `.claude/CLAUDE.md`,
`codex` writes `AGENTS.md`, and `kiro_cli` writes `.kiro/steering/cao-memory.md`.
Retrieval must return a nonempty block and the write must succeed. Empty
retrieval, fetch failures or write failures leave the previous block in place.
Kiro owns its steering file and replaces the whole file; Claude Code and Codex
replace only their delimited blocks. Kiro also validates the cwd and target
before writing; missing cwd or failed validation leaves its old file intact.
Frozen workflow replays continue using the recorded memory block without
rechecking lifecycle; another terminal creation does not invalidate that
snapshot. A fresh workflow run resolves current memory.

Explicit recall retains superseded memories. With lifecycle enabled, recency
moves them last without changing membership; usage demotes them before the
stable scope pass and slice. Score/relevance keep their existing always-on
demotion; the flag adds annotation only in those modes. A transient
`lifecycle_status="superseded"` is excluded from `model_dump` and every canonical
writer. MCP and the internal recall route include it only when set, preserving
gateway round trips. CLI show displays a status line and list appends a marker
only when labelled. Related extras are annotated but remain in expansion order.

Disabling this flag restores default injection and recency/usage behavior;
it does **not** undo score/relevance demotion. Supersedes cycles exclude both
ends, and a persistent curator can still restate guidance recalled earlier.
The diagnostic includes `lifecycle_enabled` to identify the active flag state.

### Non-accessing operator inspection (opt-in)

`cao config set memory.non_accessing_inspection true` or
`CAO_MEMORY_NON_ACCESSING_INSPECTION=true` makes CLI list/show and HTTP
`GET /memory`, `GET /memory/{key}` pass `record_access=False`. Environment
beats file; the default and read-error fallback are false. Results and related
expansion are unchanged, but these operator views no longer increase
`access_count` or `last_accessed_at`. Agent feedback lessons therefore cannot
become promotion-eligible merely from repeated operator inspection.

MCP `memory_recall` and `/internal/memory/recall` retain the default
`record_access=True` regardless of this switch. CLI clear and HTTP DELETE are
outside its scope. Disabling it restores existing operator access counting.
The service's keyword-only `record_access` parameter defaults to true; explain
continues to avoid access bookkeeping independently of this flag.

### On-demand relevance explain

`cao memory list --query Q --explain` compares default recency recall with
relevance recall through their shared pool and ordering functions. It never
increments access counts or expands related memories. Nothing is scored
passively on ordinary recall calls. `--scope`, `--type` and `--all` retain their
usual scope meaning. Explain's `--limit` defaults to 10 and accepts 1–100;
`--format` is `table` (default) or `json`. Ordinary list output is unchanged.
A bare `--query` or bare `--explain` is a usage error (exit 2). Explicit
`--limit` or `--format` without `--explain` is also a usage error, including
values equal to their defaults. Service errors report only the exception
type and an actionable hint. Set `CAO_LOG_LEVEL=DEBUG` and re-run explain to
print the exception detail on stderr; normal stdout/stderr omit that detail.
This opt-in diagnostic does not install a global CLI logging handler.

JSON schema `cao.memory.explain/1` reports pool and all-term/partial counts,
the fixed recency constants, overlap at the limit, and at most twice the limit
in candidates: the union of the relevance and default top sets. Candidate
fields are `key`, `scope`, `scope_id`, `source_kind`, `tier`, `lexical`,
`recency_factor`, `relevance`, `relevance_rank`, `recency_rank`, `recovered`,
and `superseded`, plus `injection` (`eligible`/`excluded`/`unknown`) and
`exclusion_reasons` (ordered reason tokens). Top-level `eligibility_errors`
lists sources that failed open. Top-level `lexical_degenerate` is true when
a nonempty relevance pool has only zero normalized lexical values; an empty
pool reports false. The table includes the same boolean in its footer.
Injection verdicts describe this lifecycle seam only; confidentiality and
budget gates still apply. Ranks are 1-based; relevance rank covers the entire
pool, default rank covers only the default top set. A missing rank is JSON null.
`recovered` means relevance selected a row the default top set omitted.
Scores use the exact ordering values, rounded to four decimal places.
Explain resolves supersedes state once over both pools and uses that same
snapshot for ordering, labels and the built-in injection verdict. A failed
group read adds `lifecycle.supersedes` to the errors and emits the same
content-free failed-open warning as injection. The healthy partial snapshot
is used for ordering, labels and injection verdicts without another read.
With lifecycle enabled, only rows in failed scope groups report injection
`unknown` unless another source already excludes them. Known exclusions take
precedence; healthy groups keep their actual eligible/excluded verdicts.
With lifecycle off, the failure warning uses purpose `relevance_demotion`;
injection does not consult the disabled built-in source.
A late curator ordering-snapshot failure reaches its check errors and warning
without disabling the independent related-target batch, which can read again.
Lifecycle recall similarly shares one ordering/label snapshot, including potential
related targets discovered before ordering; their bodies still load only
after the primary slice. These snapshots never survive the call.
Relevance clamps negative normalized BM25 values to zero; the existing
normalizer and frozen `score` behavior remain unchanged.
A default-only row outside the relevance pool has no relevance rank and has
zero lexical/relevance values. Overlap is intersection size divided by the
larger top-set size; it is null when both sets are empty.

Content, tags, paths, absolute timestamps, access counts and raw BM25 scores
are omitted. The operator's query is echoed. Vault reads can still produce
resolution warnings and increment the operational `recall_body_truncated`
counter. This diagnostic leaves stored access state unchanged but does not
promise zero vault observability effects.

Disable it with `cao config set memory.explain_enabled false` or
`CAO_MEMORY_EXPLAIN_ENABLED=false` (environment > file > default true).
Disabled explain exits 1 and names `memory.explain_enabled`; a disabled memory
subsystem exits 1 and names `memory.enabled`. An empty pool remains a successful
diagnostic. This switch does not gate opt-in `sort_by="relevance"`.

## CLI Commands

```bash
# List memories (shows global + current project by default)
cao memory list
cao memory list --all              # all projects
cao memory list --scope global
cao memory list --type feedback

# Compare default/relevance top results without recording access
cao memory list --query "kafka streams" --explain
cao memory list --query "kafka streams" --explain --limit 10 --format json

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

# Inspect and curate typed relationships between memories
cao memory relationships list
cao memory relationships list --scope project --source-key testing-framework
cao memory relationships list --status proposal        # the review queue
cao memory relationships list --stale                 # edges older than their source
cao memory relationships list --format json
cao memory relationships inspect <relationship-id>
cao memory relationships promote <relationship-id>    # proposal -> active
cao memory relationships reject <relationship-id>     # -> rejected (survives recompute)
```

### Typed relationships

Memories are linked by typed, provenance-tagged edges (`relates_to`,
`contradiction`, `supersedes`) held in a durable store rather than inferred at
read time. Each edge records its `origin` — `compiler`, `wiki_lint`, `human`,
`legacy_related_keys`, `external_import`, or `vault` — and a `status`.

Writes are **producer-scoped**: when the compiler or linter recomputes its edges
for a source memory, it replaces only rows carrying its own origin and type, so a
human-authored edge on the same memory is never touched.

`reject` is durable curation. A rejected edge is **not** resurrected by a later
producer recompute, and is not deleted if the producer stops reporting it — the
operator's verdict outranks the producer, and each refusal is reported back to
the producer. `promote` refuses to act on a rejected or deleted edge; re-create
it instead.

`--stale` surfaces edges computed before their source memory was last edited
(the edge stores the source's `updated_at` at write time), which is the signal
that a recompute is due.

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

> **Read-only mirror:** OKF exports are generated snapshots of CAO memory.
> Editing exported files does not update the CAO store, and a later export may
> overwrite those mirror edits. Import is an explicit ingestion operation, not
> automatic reverse synchronization.

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
2. **Provider config file** — built-in event plugins for Claude Code, Codex, and Kiro CLI
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
profiles include guidance on when to store (see below). Hook- and event-plugin-driven injection
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

## Self-Learning (builds on memory)

The opt-in self-learning loop uses agent-scope memory as its lesson store:
agents report task outcomes, a retrospector agent distills them into
agent-scope `feedback` memories with `Applies when:` trigger clauses, and
recall-reinforced lessons can be promoted into agent profile files. Every
memory capability on this page — injection, recall/`access_count`, lint,
retention, the audit log — applies to those lessons unchanged. See
[Self-Learning](self-learning.md).
