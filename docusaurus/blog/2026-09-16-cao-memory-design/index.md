---
title: "How CAO memory helps you and your agents keep context"
authors: [fanhongy]
tags: [deep-dive]
description: How CAO gives agents shared, scoped memory across sessions and providers without putting an LLM on the critical persistence path.
---

A coding agent can read a repository, diagnose a race condition, and propose a careful fix.
Start a new session the next day and it may ask the same questions again. The model is
capable; its execution context is temporary.

Adding memory is not just a matter of saving text. Once agents, projects, sessions, and
providers share knowledge, the design has to answer harder questions:

- Where is a fact valid, and who may write it?
- Which representation is authoritative?
- What survives a partial write?
- Which memories deserve space in a finite context window?
- Can an old workflow be replayed after memory changes?

CLI Agent Orchestrator (CAO) treats memory as a small knowledge system. Stored knowledge
has identity, scope, lifecycle, retrieval policy, provenance, and repair semantics. The
system is local-first and human-readable, but still supports ranked retrieval, typed
relationships, and cross-provider injection.

{/* truncate */}

> **Publication note:** This draft was source-checked against CAO commit `29b235cf`.
> PLACEHOLDER — replace or supplement that commit with the corresponding release version
> before publication.

This is not a usage guide. The [memory reference][memory-reference] covers commands and
configuration. This post examines the design: where authority lives, how writes fail, and
how stored knowledge becomes bounded agent context.

## Design constraints

CAO memory must be readable with ordinary tools, searchable without sending the whole
corpus to a model, isolated across scopes, resilient to partial failures, and bounded in a
prompt. The core therefore splits content from queryable state, persists before involving
an LLM, and makes scope part of identity. Embeddings, remote storage, and model-driven
organization remain optional layers over a repairable local baseline.

## Markdown content, SQLite state

CAO uses two coupled representations:

```text
                         CAO memory
                             │
                ┌────────────┴────────────┐
                │                         │
        Markdown topic files             SQLite
        --------------------             ------
        article content                  scope identity
        timestamped entries              timestamps
        human-readable header            access counters
        rendered See Also links          provenance
                                         compilation state
                                         typed relationships
```

Markdown is the content store. Each topic is a file with a stable key, a metadata header,
and timestamped entries. Updating a key appends an entry, preserving readable history.

SQLite stores state that would be costly or awkward to derive from prose on every recall:
access counts, timestamps, source provenance, token estimates, compilation state, and typed
relationships.

Neither representation is simply a cache of the other:

| Concern | Authority |
| --- | --- |
| Topic prose and timestamped entries | Markdown |
| Queryable metadata and usage state | SQLite |
| Relationship lifecycle and operator curation | SQLite |
| Human-readable index and `See Also` rendering | Generated projections |

Some SQLite rows are reconstructable projections; others record decisions. A missing
metadata row can be rebuilt from a topic. A rejected relationship cannot: the rejection is
an operator verdict that does not exist in the article body. Calling either side the sole
source of truth would erase that distinction.

The split also keeps the common paths simple. Humans and tools can read Markdown directly,
while ranking and graph queries avoid reparsing every article. The cost is an explicit
consistency model between the two stores.

## Deterministic first, intelligent second

The write path expresses the main design priority:

```text
agent observation
       │
       ▼
validate scope, identity, and write policy
       │
       ▼
acquire a per-topic lock
       │
       ▼
write an append-form Markdown topic atomically
       │
       ├──────────▶ on eligible updates, schedule LLM compilation
       │
       ▼
update the Markdown index
       │
       ▼
upsert SQLite metadata
```

CAO writes the topic to a temporary file and atomically replaces the destination while
holding a per-topic lock. Concurrent writers for the same scoped key cannot both read an
old version and silently overwrite each other. The shared Markdown index has its own lock
for the same reason: different topics can update one index concurrently.

An update to an existing topic may schedule LLM compilation when compile mode is `llm`; a
new topic is never compiled. The compiler can merge repeated entries, preserve useful
history, and discover related topics. It runs in the background because launching a coding
agent can take tens of seconds, and persistence should not depend on that latency or
availability.

Compilation has an optimistic concurrency guard. The task receives the exact content
written by the triggering store operation. Before replacing the article, it reacquires the
topic lock and verifies that the file is still byte-for-byte equal to that value. If
anything changed, the stale result is dropped. Validation or provider failure also leaves
the original append-form topic untouched.

> **The model may improve durable state, but it is not required to establish that durable
> state exists.**

This ordering is the point. A model failure affects organization, not whether the
observation was saved.

## Partial writes are explicit

The filesystem and SQLite do not share a transaction manager. CAO does not hide that fact
behind a nominally atomic API.

The topic and Markdown index are durable before the SQLite metadata upsert. If that final
operation fails, CAO raises `MemoryPartialWriteError` with the key, scope, scope ID, file
location, and completed phases. A caller can distinguish "nothing was written" from
"content survived but metadata needs repair" and avoid blindly appending the same fact
again.

Reconciliation walks canonical topic files, validates their paths and headers, and rebuilds
missing metadata or index entries. Malformed and unsafe paths become findings rather than
repair inputs. The process never reconstructs topic content from SQLite, runs a model, or
recomputes the relationship graph.

This is not cross-store atomicity. It is a recoverable ordering: preserve readable content,
report the incomplete projection, then repair from the durable side.

## One memory contract across agents and providers

A key such as `testing-framework` is not globally unique. Its effective identity is:

```text
(key, scope, scope_id)
```

Scope semantics belong to CAO rather than to a provider CLI or model. Kiro CLI, Claude
Code, Codex, and other agents may receive memory through different integration paths, but
CAO resolves the same project, session, global, agent, and federated boundaries before the
provider sees the content. Switching CLI, model, or worker role does not create a separate
project-memory namespace. The new agent can access the same eligible memory, subject to the
same scope and caller policy.

CAO defines five scopes:

| Scope | Intended boundary | Retention |
| --- | --- | --- |
| `session` | One run's short-lived context | 14 days |
| `project` | Repository architecture and conventions | 90 days |
| `global` | Cross-project facts and user preferences | Permanent |
| `agent` | Knowledge for one agent role | Permanent |
| `federated` | Machine-wide knowledge shared across projects | Permanent |

Memories classified as `user` or `feedback` are permanent regardless of scope. Cleanup
uses the timestamp in the index and runs when the server starts; it is not a continuous
sweeper.

Scope also determines visibility, recall precedence, required identity, and write policy.
Project, session, and agent writes fail closed when CAO cannot resolve the corresponding
identity; they are not redirected into a shared bucket. The caller's scope constrains which
tiers it can write, so isolation is enforced before retrieval rather than delegated to the
model.

Project identity follows a precedence chain: an explicit override, a normalized Git remote,
then `sha256(realpath(cwd))[:12]` as a fallback. A Git-based identity survives ordinary
checkout moves, while a recorded working-directory-hash alias preserves the earlier
path-derived identity. Raw remote URLs are not persisted because they may contain
credentials.

Scope and memory type are orthogonal. Scope answers where a fact applies; type classifies
it as `project`, `user`, `feedback`, or `reference`. A user preference can be global, while
a correction can be project-specific. Classification does not change visibility.

## Retrieval allocates attention

Storage can grow without bound; prompt context cannot. CAO therefore separates automatic
injection from explicit recall.

**Automatic injection** gives a new agent a bounded starting context. It considers
session, project, and global memories in that order. Each scope contributes at most ten
entries and has an independent character cap. Unused capacity is not redistributed, so a
large global corpus cannot consume the space reserved for project or session knowledge.

The block reaches the agent through two paths: CAO writes a marker-delimited section into
the provider's project configuration and prepends a `<cao-memory>` block to the first user
message. Injection is a startup snapshot, not a live subscription.

**Explicit recall** searches beyond that snapshot. It supports scope and type filters plus
three search modes: metadata, BM25, and hybrid. Hybrid recall takes metadata matches first,
then fills the remaining result limit with BM25 hits from topic bodies. Results can be
ordered by recency, usage, or a composite of lexical relevance, recency, and usage.

Successful recall increments access counts on a best-effort basis. Those counters can
influence later ranking, but a failed increment never blocks the read.

This path requires no embedding service. The trade-off is straightforward: lexical search
is local and reproducible, but may miss semantically equivalent wording.

## Memory is an execution input

Persistent memory introduces temporal nondeterminism. A workflow run today may observe a
project convention that changes before replay. Re-resolving live memory would combine old
workflow inputs with new implicit context.

For workflow runs, CAO resolves memory once and persists the manifest's stored copy before
the first terminal uses it. That copy is redacted and may be truncated to the manifest
bound. The original run and every replay receive the same stored bytes. An explicitly
frozen empty block is also meaningful: it records that the run saw no memory and prevents a
later replay from falling through to the live store.

Persist-before-use is deliberate. If persistence fails, the run proceeds without memory
rather than using context that cannot be reproduced later. An over-recorded block that was
persisted before a terminal crash is visible in the manifest; an unrecorded block used by a
terminal would not be.

Once memory can change an outcome, it must be frozen or versioned like any other execution
input.

## Relationships are governed state

CAO stores relationships as durable typed edges rather than inferring a graph on every
read. An edge records:

- type: `relates_to`, `contradiction`, or `supersedes`;
- origin: `compiler`, `wiki_lint`, `human`, `legacy_related_keys`, or
  `external_import`;
- status: `active`, `proposal`, `rejected`, `superseded`, or `deleted`;
- optional confidence, rank, and evidence attributes;
- the source memory's update time for staleness detection.

Replacement is producer-scoped. A compiler recomputation replaces only rows with the same
scope, source, origin, and type. It cannot erase a human-authored edge. Rejected and deleted
rows are also preserved through recomputation, so operator curation outranks machine
regeneration.

An edge is stale when its recorded source update precedes the current source memory.
Staleness is therefore a signal to recompute, not a reason to mutate the edge during a
read. The Markdown `See Also` section is another projection of this state, not an
independent graph writer.

Relationship promotion and instruction promotion are separate operations. The first accepts
a proposed graph edge. The second copies a reinforced lesson into an agent profile. Sharing
the word "promotion" does not give them the same lifecycle.

## Remote memory reuses the local API

By default, CAO uses local Markdown and SQLite. Distributed deployments can point the same
store, recall, forget, and context operations at a memory API. Terminal context still
resolves scope, and remote partial writes return the same typed `MemoryPartialWriteError`.
Without an API URL, the gateway calls local `MemoryService`; the network changes placement,
not semantics.

## Import and export are trust boundaries

OKF export screens topic bodies and optional history before they leave the store. Secret
matches are skipped unless redaction is requested. The CLI requires `--include-private`
for session or agent memory; the HTTP endpoint refuses those scopes.

Import treats bundles as untrusted input. The operator supplies the target scope, while CAO
validates paths and keys, rejects escapes and symlinks, neutralizes structural spoofing,
strips `See Also` projections, and routes accepted content through `MemoryService.store()`.
Session and agent imports are not offered.

OKF is deliberately lossy: it carries portable content, not scope identity, UUIDs, usage,
provenance, or relationship authority. Its deterministic output works well in Git and
Obsidian, but remains a read-only mirror rather than a synchronized replica. Two-way editing
would require versioning, merge rules, inbound secret handling, and conflict resolution.

The Markdown store itself is intentionally readable, so it is not a secrets manager.
Federated writes and exports have credential-pattern gates, but credentials still belong in
a dedicated secrets system.

## How CAO's opt-in learning loop works

CAO does not learn automatically from every conversation. The shipped loop is
supervisor-driven and opt-in: it records structured outcomes, asks a dedicated retrospector
prompt to propose lessons, stores approved lesson text in memory, and can later promote
reinforced lessons into an agent profile.

```text
validated work
     │
     ▼
report_outcome ──▶ workflow_outcomes (SQLite)
                          │
                          ▼
supervisor hands off to the retrospector prompt
                          │
               list_outcomes + memory_recall
                          │
                          ▼
store_lesson ──▶ worker's agent-scope feedback memory
                          │
                   explicit recall
                          │
                          ▼
optional reviewed promotion ──▶ profile ## Learned Patterns
```

After a meaningful unit of work is validated, a supervisor can record success or failure,
an optional score, and short friction notes. The record is a compact signal, not a
transcript. Nothing triggers retrospection automatically at session end; the supervisor
hands off at a useful boundary such as a completed package, feature, or review cycle.

The built-in retrospector is a constrained agent prompt. It tells the model to read the
recorded outcomes, recall existing lessons to avoid duplicates, and keep only patterns that
are actionable, supported by an outcome, and likely to recur. It also requires the lesson
format to end with `Applies when: <trigger>`. These are prompt-level quality rules, not a
claim that CAO can deterministically prove a lesson is correct. Returning "no lessons" is
valid.

The `store_lesson` service does enforce the storage boundary. It writes to the named
worker's `agent` scope with type `feedback`; ordinary `memory_store(scope="agent")` would
instead resolve to the retrospector's profile. Writing into another profile requires the
server-checked `store_lesson` capability, so an ordinary worker cannot insert permanent
feedback into another agent's memory.

An unpromoted agent-scope lesson is available through explicit recall when the caller's
agent identity matches. It is not part of the deterministic automatic injection builder,
which currently considers session, project, and global scopes. Each successful recall can
increase `access_count`; promotion requires three recalls by default. The operator then
reviews a dry-run plan before copying eligible text into the profile's `## Learned
Patterns` block, where it becomes part of every future session for that profile.

This two-stage design separates advisory memory from standing instructions. Promotion is
separately gated, never deletes the backing memory, and should be reviewed like a
system-prompt change. Recall count is only a reinforcement heuristic; workflows with real
quality metrics should use those metrics before applying promotion.

## Costs, savings, and trade-offs

A compact, scoped conclusion can replace repeated code searches, document reads, web
research, and user explanations. On recurring work that can save tool calls, input tokens,
and latency. The comparison is the cost of storing and curating a conclusion versus
rediscovering it on every run.

The saving is not automatic. Stale knowledge wastes time, and injected lessons consume
context. Other trade-offs remain:

- Markdown plus SQLite requires reconciliation after partial writes.
- Local BM25 is predictable but can miss semantic paraphrases.
- Automatic injection is a snapshot; long sessions may need live recall.
- Provenance, linting, and curation manage errors but do not prove facts.
- Plaintext requires credential discipline.
- Portable OKF content does not restore all internal state.

## Take memory into Obsidian

CAO already exports a memory graph as an Obsidian-openable vault. The sink creates one
Markdown note per node, with YAML metadata, an H1 label, and `[[wikilinks]]` for outgoing
relationships. This is a one-way snapshot for browsing: CAO does not read edits back. The
separate OKF archive remains the path for portable topic content that can be imported.

![Current Obsidian export and PR #674 canonical vault architecture](./obsidian-memory-architecture.svg)

[PR #674](https://github.com/awslabs/cli-agent-orchestrator/pull/674) goes one step further.
It keeps the `MemoryService` API but adds a scope binding: an unmapped scope continues to
use the native wiki, while a mapped global, project, or agent scope uses a configured vault
folder as its canonical Markdown source. SQLite metadata, BM25 search state, and graph
relationships become rebuildable projections of those notes.

The branch limits writes to one managed folder; other mapped folders are read sources.
Release one has no watcher, so external edits require reconciliation before indexes and
relationships refresh. Unlike today's export sink, an explicitly mapped vault is the
content authority while agents continue using the same scoped memory API.

[memory-reference]: https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/memory.md
