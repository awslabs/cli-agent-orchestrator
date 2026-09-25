---
title: "How CAO memory helps you and your agents keep context"
authors: [fanhongy]
tags: [deep-dive]
description: How CAO gives agents one shared memory layer across sessions, models, and CLI providers.
---

A coding agent can solve a hard problem. But its session is temporary. The next agent may
repeat the same investigation.

CAO memory gives every supported agent the same memory tools. An agent can remember a fact
with one CLI provider and recall it later with another. The memory belongs to CAO, not to a
specific model or CLI.

This post explains how that shared layer works. For commands and configuration, see the
[original CAO memory reference](https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/memory.md).

{/* truncate */}

## One MCP memory layer for every agent

CAO exposes three core MCP operations:

- `memory_store` saves or updates a fact.
- `memory_recall` searches stored facts.
- `memory_forget` removes a fact.

These operations stay the same across Kiro CLI, Claude Code, Codex, and other CAO agents.
The provider may change. The model may change. The memory API and scope rules do not.

CAO resolves scope before it calls the provider. This gives every agent a consistent view
of project, session, global, agent, and federated memory. Access still follows the scope and
caller policy.

This is the main reason CAO owns the memory layer. A provider-specific memory feature would
split knowledge into separate stores. CAO keeps one store and one API.

## What the design must do

The shared layer has five goals:

1. **Unified.** Every CAO agent uses the same MCP operations and scope rules.
2. **Readable.** People can inspect memory without a special database tool.
3. **Searchable.** Agents can find a fact without reading the whole store.
4. **Scoped.** A session note does not become a global rule.
5. **Recoverable.** A partial write can be detected and repaired.

The prompt also has a hard size limit. CAO must choose a small set of useful memories. It
cannot inject the whole store.

## Local by default

CAO keeps memory on the machine by default. Markdown files hold the content under the CAO
home directory. The CAO SQLite database holds metadata and lifecycle state. BM25 search
runs in the CAO process and reads the local Markdown files.

This local design has clear benefits. Memory works without a cloud service. The files stay
under the operator's control. Reads are fast. A person can open the files and check what an
agent may recall.

An LLM is not required for the first write or for BM25 search. CAO can use an LLM later to
organize an existing topic. That step is optional.

A distributed CAO deployment can use remote memory. Setting `CAO_MEMORY_API_URL` routes the
same store, recall, forget, and context operations to a memory-owning CAO server. The MCP
contract does not change. Only the location of the store changes.

## Markdown stores content; SQLite tracks metadata

CAO uses two local stores:

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
        generated related links          provenance
                                         compilation state
                                         typed relationships
```

Markdown holds the actual memory text. Each topic has a stable key. The key identifies the
topic and becomes part of its file name.

The file header records the memory ID, scope, type, and tags. Timestamped sections record
when each observation was added. When an agent updates the same key, CAO appends a new
timestamped section to that topic. It does not create a second memory with the same key and
scope.

SQLite tracks data used for filtering, ranking, and lifecycle rules. This includes scope
IDs, timestamps, access counts, provenance, token estimates, and relationship state.
BM25 still searches the Markdown content. SQLite does not replace that content search.

The two stores have different authority:

| Concern | Authority |
| --- | --- |
| Topic text and timestamped history | Markdown |
| Search metadata and usage counters | SQLite |
| Relationship lifecycle and human decisions | SQLite |
| Human-readable index | Generated from the stores |
| `## See Also` related-topic links | Generated from relationship state |

`See Also` is a real section in a topic file. CAO writes normal Markdown links under the
`## See Also` heading. The links point to related memory topics. They are a readable view of
SQLite relationship state, not a second source of truth.

Some SQLite data can be rebuilt from Markdown. Some cannot. For example, a rejected
relationship is a human decision. That decision must remain in SQLite.

## Save first, organize later

A CAO agent calls `memory_store`. The memory service then follows a fixed write path:

```text
agent calls memory_store
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

This first path is deterministic because it uses fixed code, not model output. CAO locks
the topic, writes a known append format, and publishes it with an atomic file replacement.
The same rules run for every write.

The shared Markdown index has its own lock. This prevents two topics from losing each
other's index updates.

When creating a new topic in memory, CAO does not use an LLM. An existing topic may use an
LLM when compile mode is `llm`. The LLM can merge repeated entries and find related topics.
This work runs after the initial save.

The compiler checks for newer writes before it publishes a result. If the topic changed,
CAO drops the stale result. A slow or failed LLM never removes the saved observation.

The rule we follow is simple: save the observation first. Improve the structure later. CAO
uses an LLM for that second step only when the operator enables it.

## Partial writes are visible

The filesystem and SQLite cannot share one transaction. CAO handles that limit directly.

The topic file and Markdown index are written before SQLite metadata. If the SQLite write
fails, CAO raises `MemoryPartialWriteError`. The error lists the key, scope, file path, and
completed phases.

This tells the caller what is already safe. Retrying the same write could add a duplicate
entry.

CAO can repair the missing metadata. Reconciliation scans the topic files, validates them,
and rebuilds missing rows or index entries. It never rebuilds topic text from SQLite.

## Scope tells CAO where memory applies

We define a memory with:

```text
(key, scope, scope_id)
```

The key names the topic. The scope says where the fact applies. The scope ID names the
specific project, session, or agent when needed.

| Scope | Applies to | Retention |
| --- | --- | --- |
| `session` | One run | 14 days |
| `project` | One repository | 90 days |
| `global` | All projects | Permanent |
| `agent` | One agent profile | Permanent |
| `federated` | All projects on one machine | Permanent |

The retention periods match the expected lifetime of each scope. Session memory is temporary,
so it expires first. Project memory lasts longer because project decisions often stay useful
across many sessions. Global, agent, and federated memory are designed to cross project or
session boundaries, so they do not expire.

Memories of type `user` or `feedback` also never expire. User preferences and explicit
corrections should not silently disappear. Cleanup runs when `cao-server` starts. It is not
a continuous sweep.

Project, session, and agent scopes need an identity. CAO rejects the write if it cannot
resolve that identity. It never falls back to a wider scope.

Project identity follows this order:

1. An explicit project ID.
2. A normalized Git remote.
3. `sha256(realpath(cwd))[:12]`.

The Git-based ID survives normal checkout moves. CAO also records a path-hash alias for
older stores. It does not save raw remote URLs because they may contain credentials.

Scope and type are separate. Scope says where a fact applies. Type says whether the fact is
a project note, user preference, correction, or reference.

## Put only useful memory in the prompt

CAO has two retrieval paths.

**Automatic injection** gives an agent a small starting set. It checks session, project,
and global memory in that order. Each scope gets at most ten entries and its own character
limit. Empty space from one scope is not given to another.

Every provider receives the same `<cao-memory>` content block on the first user message.
Built-in provider plugins also write the block into the file that provider reads:

- Claude Code: `.claude/CLAUDE.md`
- Codex: `AGENTS.md`
- Kiro CLI: `.kiro/steering/cao-memory.md`

The file path is provider-specific. The memory selection and scope rules are not. Injection
is a startup snapshot, not a live update on every turn.

**Explicit recall** searches beyond that snapshot. It supports metadata, BM25, and hybrid
search. Hybrid search returns metadata matches first. BM25 fills the remaining result slots.
Results can be sorted by recency, usage, or a combined score.

A successful recall may increase `access_count`. A failed counter update never blocks the
read.

## Keep CAO workflow replays consistent

CAO workflows are a separate feature. They run repeatable, multi-step jobs. Memory matters
to workflows because a recalled fact can change the result.

Suppose a workflow reads a project rule today. The rule changes tomorrow. A replay should
not mix the old workflow inputs with the new rule.

CAO resolves memory once for a workflow run. It stores a redacted and size-limited copy in
the run manifest. The first run and every replay use those same bytes.

An empty stored block also has meaning. It says the original run saw no memory. A replay
must not fall back to the live store.

CAO saves the block before the terminal uses it. If that save fails, the run continues
without memory. This is safer than using context that cannot be reproduced.

## Keep relationships as governed data

CAO stores relationships as typed edges. It does not ask a model to rebuild the graph on
every read.

Each edge records a type, origin, status, and source update time. It may also carry
confidence, rank, and evidence.

A producer can replace only its own edges. Compiler output cannot remove a human edge.
Rejected and deleted edges survive recomputation.

CAO marks an edge stale when its source memory changes. A stale edge needs recomputation.
Reading it does not change it.

CAO has two promotion actions. Relationship promotion accepts a proposed edge. Instruction
promotion copies a lesson into an agent profile.

## How the opt-in learning loop works

CAO does not learn from every conversation. Learning is opt-in. A supervisor starts each
step of the loop.

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

The flow has five steps:

1. A supervisor records an outcome after validation. The record contains success, an
   optional score, and short friction notes. It does not contain a transcript.
2. The supervisor hands work to the retrospector. This does not happen automatically at
   session end.
3. The retrospector reads outcomes and checks existing lessons. Its prompt asks for a short,
   reusable lesson and an `Applies when:` trigger. These prompt rules do not prove that the
   lesson is correct.
4. `store_lesson` writes the lesson to the worker's `agent` scope as `feedback`. A caller
   needs the `store_lesson` capability to write into another agent's scope.
5. The worker recalls the lesson later. Recall can increase `access_count`. After three
   recalls by default, the operator can review a promotion plan.

An unpromoted agent lesson needs explicit recall. Automatic injection currently uses only
session, project, and global memory.

Promotion copies the lesson into the profile's `## Learned Patterns` block. It does not
delete the original memory. The operator should review the change like any system prompt
update.

## Costs and savings

A good memory can replace repeated work. The agent may avoid another code search, document
read, web search, or user question. This can save tool calls, tokens, and time.

Memory also has costs:

- Markdown and SQLite need reconciliation after a partial write.
- BM25 can miss facts that use different words.
- Automatic injection can become stale during a long session.
- A stored fact can be wrong.
- More injected lessons use more prompt space.

The useful question is simple: is storing and checking the conclusion cheaper than finding
it again on every run?

## Move and view memory outside CAO

CAO supports two different use cases: moving topic content and viewing relationships.

**OKF export and import** move portable topic content. Export checks content for credential
patterns. Import requires the operator to choose the target scope. OKF does not preserve
scope IDs, UUIDs, usage counts, provenance, or relationship decisions. It is a migration
format, not a full backup.

**Obsidian graph export** creates a vault for browsing. It writes one Markdown note per
node, with YAML metadata, an H1 title, and `[[wikilinks]]` for relationships. This export is
one-way. CAO does not read edits back.

![Current Obsidian export and PR #674 canonical vault architecture](./obsidian-memory-architecture.svg)

[PR #674](https://github.com/awslabs/cli-agent-orchestrator/pull/674) adds another model. A
mapped vault folder becomes the canonical Markdown source for a scope. Unmapped scopes keep
the native wiki. SQLite, BM25, and graph state become rebuildable views of the vault notes.

CAO writes only inside one managed folder. Other mapped folders are read-only sources.
Release one has no file watcher. External edits need reconciliation before indexes and
relationships are current.
