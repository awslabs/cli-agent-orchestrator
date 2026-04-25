# CAO Memory System — Proposal

## Problem Statement

As users work with CAO agents across multiple sessions, agents currently have no memory of past interactions, decisions, or learnings. This means:

* Agents repeatedly ask the same questions
* Previous solutions to problems are forgotten
* User preferences and project conventions must be re-explained
* No learning accumulation across sessions
* Difficult for hybrid providers to resume the task given to one agent and its memory (short-term and long-term)

But adding memory alone isn't enough:
*"An agent can only respond well up to X instructions in its context, where X is a number that depends on model and harness choice. One of the goals of CAO is to manage each agent's context such that only X most relevant instructions are present. CAO could source those instructions from other agents or from past session history."*

Injecting all stored memories is both expensive and ineffective. The agent's effective attention is limited to the most recent X instructions, and earlier ones fade in influence. Rather than dumping everything into context, CAO should prioritize the X most relevant instructions sourced from past sessions, other agents' work, or project knowledge.

## Core Concepts

### Memory vs Session Context — The Distinction

**Session context** is the raw, complete conversation transcript — every prompt, response, and tool call in chronological order. It's the "what happened" record. Every provider stores this natively (Kiro's JSONL, Codex's SQLite, Copilot's events.jsonl). Session context is:

* Provider-locked (Kiro's JSONL only works with Kiro)
* Complete but bulky (includes everything, even irrelevant tangents)
* Not transferable (you can't feed Kiro's session to Gemini)

**Memory** is distilled knowledge extracted from one or more sessions. It's the "what matters" record — decisions, preferences, learnings worth keeping. Memory is:

* Provider-agnostic (a fact like "this repo uses pytest" works for any agent)
* Compact (only essential insights)
* Durable (outlives any single session)
* Transferable (can be injected into any provider)

**Analogy:** Session context is a complete meeting recording. Memory is the meeting notes.

The critical operation that connects them is **distillation** — turning raw session context into usable memory. This is what enables cross-provider handoff: Kiro's session context gets distilled into memories that CAO can inject into Gemini.

### Memory Layers

| Layer | What | Lifetime | Owned By |
|---|---|---|---|
| **Session context** | Raw conversation history (prompts, responses, tool calls) | Single session | Provider (native format) |
| **Working memory** | Distilled facts for the current task/session | Across terminals within a CAO session | CAO |
| **Long-term memory** | Durable knowledge across sessions | Across sessions | CAO |

### Memory Scopes

| Scope | What | Example |
|---|---|---|
| **Global** | User preferences, general patterns applicable everywhere | "User prefers pytest" |
| **Project** | Project-specific conventions, architecture decisions | "This repo uses FastAPI + SQLAlchemy" |
| **Session** | Working facts for the current multi-agent collaboration | "Kiro found 3 auth bugs" |
| **Agent** | Agent-specific learnings and context | "gemini-developer prefers short responses" |

### Storage Model

**Hybrid: Wiki files for compiled knowledge + SQLite for metadata.**

* **Wiki files** (`wiki/` + `index.md`): The primary knowledge representation. Each `memory_store` updates a topic file; `index.md` is the master map. Human-readable, provider-agnostic, injected directly into agents.
* **SQLite metadata**: Stores memory keys, scopes, tags, timestamps, and file paths. Enables fast lookup, scope resolution, and retention management. Also stores raw session events (audit trail).
* **Provider-native files** (extraction sources): Kiro's `.jsonl`, Claude's `MEMORY.md`, Copilot's `events.jsonl` — read by CAO during extraction, never modified.

```
              ┌─────────────────────┐
              │  Provider Sessions  │     ┌──────────────┐
              │ (Kiro .jsonl, etc.) │     │ Agent calls  │
              └──────────┬──────────┘     │ memory_store │
                         │ Extract &      └──────┬───────┘
                         │ Distill               │
                         ▼                       ▼
              ┌───────────────────────────────────────┐
              │       CAO Memory Store                │
              │  (wiki files + SQLite metadata)       │
              │                                       │
              │  Session scope ── working memory      │
              │  Project scope ── conventions         │
              │  Agent scope ─── agent learnings      │
              │  Global scope ── user preferences     │
              └──────────────────┬────────────────────┘
                                 │ Select(X) & Inject
              ┌──────────────────▼────────────────────┐
              │        Provider Injection             │
              │  (GEMINI.md, --append-system-prompt,  │
              │   -c instructions, steering/*.md)     │
              └───────────────────────────────────────┘
```


## Memory Operations

Memory operations are exposed as MCP tools via `cao-mcp-server`, alongside existing `handoff`, `assign`, and `send_message`.

### memory_store

Store or update a persistent memory. Writes to both wiki file (primary knowledge) and SQLite metadata (index/lookup). If a memory with the same key+scope+scope_id exists, the wiki file and metadata are updated (upsert).

```
Parameters:
  key         (str, required)  — Unique memory identifier slug (e.g. "prefer-pytest", "auth-architecture")
  content     (str, required)  — Memory content (markdown)
  memory_type (str, default="project") — Type: "user", "feedback", "project", "reference"
  scope       (str, default="project") — Scope: "global", "project", "session", "agent"
  tags        (str, optional)  — Comma-separated tags for search

Returns: { success, key, scope, scope_id, file_path, created_or_updated }
```

**What happens on store:**

* Resolve `scope_id` from the calling terminal's context
* Write/update the wiki topic file under `wiki/{scope}/` (Phase 1: simple append; Phase 2+: LLM-compiled merge into existing article)
* Update `index.md` master map if new topic file was created
* Upsert SQLite metadata (key, scope, tags, timestamps, `file_path` → wiki file)

`scope_id` auto-resolution:

* `global` → None
* `project` → hash of working directory
* `session` → CAO session name
* `agent` → agent_profile name

### memory_recall

Retrieve memories matching a query and filters. Uses **index-first navigation** (not flat DB search): queries SQLite metadata to identify matching wiki files, then returns content from those files.

```
Parameters:
  query       (str, optional)  — Search query (matches key, tags, file titles in SQLite metadata)
  scope       (str, optional)  — Filter by scope. None = returns global + project + session + agent
  memory_type (str, optional)  — Filter by type
  limit       (int, default=10) — Max results

Returns: { memories: [{ key, content, memory_type, scope, tags, file_path, updated_at, source_provider }] }
```

**Phase progression:**

* **Phase 1**: SQLite metadata query (key + tags match) → read matching wiki files → return content sorted by recency
* **Phase 2**: Hybrid — metadata query + BM25 over wiki file content for uncovered topics
* **Phase 3**: Context-manager uses `index.md` token metadata for budget-aware selection

### memory_consolidate (Phase 2)

Identify candidate memories and wiki articles for merging or removal. Uses heuristics (duplicate keys, overlapping tags, age thresholds) to surface candidates. The calling agent reviews and acts via `memory_store`/`memory_forget`.

```
Parameters:
  scope       (str, default="project") — Scope to consolidate
  dry_run     (bool, default=true) — If true, return proposed changes without applying

Returns: { proposed_actions: [{ action: "merge"|"remove", keys, reason, content_preview, file_paths }] }
```

**Note:** CAO has no LLM capability — this tool only surfaces candidates. The actual merge/remove decision is made by the calling agent.

### memory_forget (Phase 2)

Remove a specific memory by key and scope. Deletes from both SQLite metadata and the wiki topic file. Updates `index.md` if a wiki file is removed.

```
Parameters:
  key         (str, required)  — Memory key to remove
  scope       (str, default="project") — Scope of the memory to remove

Returns: { success, deleted, file_path }
```

### session_context (Phase 2)

Retrieve session event history for cross-provider resumption.

```
Parameters:
  session_id      (str, optional)  — CAO session ID. Default = current session
  last_n_events   (int, default=20) — Number of recent events to return

Returns: { events: [{ terminal_id, provider, agent_profile, event_type, content, timestamp }] }
```


## Architecture

### Dual Pipeline Design

CAO has two distinct pipelines:

* **Pipeline 1 — Memory Creation** (how knowledge enters the system): `Extract → Distill → Store`
* **Pipeline 2 — Context Delivery** (how knowledge reaches agents): `Accumulate → Rank → Select(X) → Inject`

```
┌─────────────────────────────────────────────────────────────────────┐
│                         CAO Memory Layer                            │
│                                                                     │
│  Pipeline 1: Creation                Pipeline 2: Delivery           │
│                                                                     │
│  ┌──────────┐   ┌─────────┐         ┌───────────────┐               │
│  │ Extract  │   │ Distill │         │ Context-Mgr   │ (Phase 2)     │
│  │ (provider│ → │ (agent  │         │ reads index,  │               │
│  │  sessions│   │  or CM) │         │ selects X     │               │
│  └──────────┘   └────┬────┘         └───────┬───────┘               │
│                      │                      │                       │
│                      ▼                      │ reads                 │
│               ┌─────────────┐               │                       │
│               │ Wiki Files  │ ◄─────────────┘                       │
│               │ (topic .md  │                                       │
│               │ + index.md) │         ┌───────────┐                 │
│               └──────┬──────┘         │ Provider  │                 │
│                      │                │ Injection │                 │
│               ┌──────┴──────┐         └─────┬─────┘                 │
│               │   SQLite    │               │                       │
│               │ (metadata,  │               ▼                       │
│               │  file_path, │         ┌───────────┐                 │
│               │  scope,     │         │ Agent     │                 │
│               │  retention) │         │ Context   │                 │
│               └──────┬──────┘         └───────────┘                 │
│                      │                                              │
│                  ┌───┴───┐                                          │
│                  │ MCP   │  (agents call memory_store/recall)       │
│                  │ Tools │  store → wiki + metadata                 │
│                  └───────┘  recall → metadata query → wiki read     │
└─────────────────────────────────────────────────────────────────────┘
```

### A. Extract (on terminal exit/completion)

When an agent finishes, CAO extracts key context via **lifecycle hooks** in the terminal/session service — not by ad-hoc parsing in the agent loop. This keeps extraction reliable (always fires on terminal exit) and decoupled from agent behavior.

**Lifecycle hook trigger points:**

| Event | Hook | What fires |
|---|---|---|
| Terminal exits normally | `on_terminal_complete` | Extract provider session + store session event |
| Handoff returns | `on_handoff_return` | Extract handoff output + distill working memory |
| Terminal hits limit/crashes | `on_terminal_error` | Extract whatever is available (metadata + last output) |
| Session ends | `on_session_cleanup` | Final consolidation pass |

**What to extract per source:**

| Source | What to Extract | How |
|---|---|---|
| Terminal output | Task summary, decisions, findings | Parse `extract_last_message_from_script()` |
| Agent's native memory | Facts the agent stored natively | Read provider-specific files (Claude's MEMORY.md, Gemini's GEMINI.md) |
| MCP tool calls | Memories stored via `memory_store` | Already in wiki + metadata |
| Session event log | Timeline of what happened | CAO's own SessionEvent table |

Extraction is handled by a new method on `BaseProvider`:

```python
def extract_session_context(self) -> Optional[str]:
    """Extract key context from this provider's session for cross-provider handoff."""
```

**Key benefits of CAO lifecycle hooks** (distinct from provider-native hooks):

* Extraction always fires regardless of how the terminal ends (normal exit, crash, limit)
* Hooks are independently testable — mock the terminal event, verify extraction output
* New extraction sources can be added without modifying the agent loop or provider code
* Hooks run in CAO's process, not the agent's — no risk of agent context exhaustion affecting extraction

### B. Distill (session context → working memory)

Raw session context is too large and provider-specific to inject directly. Distillation produces compact, provider-agnostic facts. Since CAO has no LLM capability, distillation must be performed by agents.

**Two tiers of distillation:**

| Tier | Who distills | When | Quality | Reliability |
|---|---|---|---|---|
| **Self-save** | The working agent itself | Periodically during work + before finishing | Highest — agent has full task context | Reliable when hook-triggered; best-effort when instruction-only |
| **Supervisor/CM** | Supervisor or context-manager agent | After agent exits or on ─handoff | High — can read agent's output and session events | Reliable — triggered by CAO lifecycle events |

**Making self-save reliable — hook-triggered saves (from MemPalace):**

Instruction-based self-save ("please call `memory_store` before finishing") is best-effort — the agent may forget or hit its context limit before saving. MemPalace solves this with **provider-native hooks** that automate the trigger while the agent does the actual classification:

| Provider | Hook mechanism | Trigger | How it works |
|---|---|---|---|
| **Claude Code** | `Stop` hook in `.claude/settings.local.json` | Every N messages (e.g., 15) | Hook counts messages in JSONL transcript → returns `{"decision": "block", "reason": "AUTO-SAVE checkpoint. Save key topics, decisions, quotes, and code from this session to your memory system. Organize into appropriate categories. Use verbatim quotes where possible. Continue conversation after saving."}` → agent saves using its own judgment → resumes |
| **Claude Code** | `PreCompact` hook | Before context compression | Hook always blocks → agent saves everything before context is summarized |
| **Codex** | `Stop` hook in `.codex/hooks.json` | Same as Claude Code | Same mechanism — Codex supports the same hook format |
| Kiro/Gemini/Kimi | No native hook API | N/A | Fall back to instruction-based + CAO-side polling of `context_usage_percentage` for pre-compaction trigger |

The key insight from MemPalace: **the hook automates the trigger, but the agent does the classification.** The hook doesn't parse or extract — it just tells the agent "save now," and the agent uses its full conversation context to decide what's worth keeping.

**Phase progression:**

* **Phase 1**: Agent self-save via `memory_store`. For Claude Code and Codex terminals, register provider-native hooks to automate the trigger. For other providers, instruction-based.
* **Phase 2**: Add supervisor/context-manager distillation. When an agent exits (especially on limit/crash), the context-manager reads the session event log and handoff output, then distills into working memory.
* **Phase 3**: Full wiki compilation. New memories compiled into existing wiki articles with cross-references (Karpathy pattern).

### C. Store (hybrid — wiki files + SQLite metadata)

Every `memory_store` call writes to **two** targets:

1. **Wiki file** (primary knowledge): Content appended/compiled into a topic file under `wiki/{scope}/`. `index.md` updated if new file created. This is what agents read.
2. **SQLite metadata** (index/lookup): Key, scope, tags, timestamps, `file_path` to the wiki file. This is what CAO queries for fast lookup, scope resolution, and retention.

```python
class MemoryMetadata:  # SQLite table — metadata only, not content
    key: str                     # slug identifier
    memory_type: str             # user | feedback | project | reference
    scope: str                   # global | project | session | agent
    scope_id: Optional[str]      # auto-resolved from terminal context
    file_path: str               # path to wiki topic file (content lives here)
    tags: List[str]
    source_provider: Optional[str]
    source_terminal_id: Optional[str]
    created_at: datetime
    updated_at: datetime
```

Key-based upsert on (key, scope, scope_id) prevents duplicates in both wiki and metadata.

### D. Inject (on terminal creation) — Context Delivery Pipeline

When a new terminal is created, CAO runs the **Context Delivery Pipeline**: Accumulate → Rank → Select(X) → Inject.

**Without context-manager (Phase 1 — rule-based):**
CAO queries SQLite metadata by scope precedence (session > project > global), applies recency weighting, reads the matching wiki files, and fills up to a hard token budget (~2-4KB).

**With context-manager (Phase 2+ — LLM-curated):**
CAO spawns a context-manager agent (same provider as supervisor) that:

1. Reads the memory index (`index.md`) — not all files, just the map
2. Reads the current task description and session history
3. Selects which memory articles are relevant
4. Fetches only those articles (incremental, budget-aware via token metadata)
5. Produces a curated context block that fits within the injection budget
6. CAO injects the curated block into the target agent

**Injected context format:**

```
## Context from CAO Memory
### Session History (if cross-provider handoff)
1. [kiro-developer] Analyzed auth module — found 3 security issues in middleware.py
### Relevant Memories
- [project] auth-issues: 3 security issues in middleware.py (L45, L89, L112)
- [user] prefer-pytest: User prefers pytest for all tests
- [feedback] no-summaries: Don't summarize at end of responses
```

### E. Context Manager

The context-manager is a dedicated agent spawned per CAO session (opt-in via `cao launch --memory`) whose sole job is context curation. It implements the Select(X) step of the delivery pipeline using LLM judgment.

**Why an LLM for selection?** No scoring algorithm can match an LLM's judgment on "what would help this agent most right now?" The context-manager can:

* Understand the task semantics (not just keyword match)
* Infer what the agent will need (anticipatory, not reactive)
* Compress and reformat for the target agent's style
* Resolve conflicting memories (pick the more recent/relevant one)

**How it works:**

```
cao launch --memory --supervisor claude
  ├── supervisor agent (claude)
  ├── context-manager agent (claude, background)  ← same provider as supervisor
  └── worker agents (kiro, gemini, codex, etc.)
```

At assign/handoff (primary — push model):

```
supervisor calls handoff("kiro-developer", task="...")
  → CAO intercepts
  → CAO sends task description to context-manager
  → context-manager reads index.md, selects relevant articles, produces curated block
  → CAO injects curated block into kiro-developer's system prompt
  → kiro-developer starts with curated context
```

**The context-manager's own X problem:** If there are 200 memory files, it can't read them all. Solution layers:

1. **Index-first** (from Karpathy): Read `index.md` (the master map, fits in one context window), then fetch only relevant articles
2. **Size metadata** (from Andrew Ng's context-hub): Index entries include token counts so the context-manager can make budget-conscious fetch decisions
3. **Scope filtering**: Only load memories matching the target agent's scope

**When NOT to use the context-manager:** Quick one-off tasks, single-agent sessions, or small projects with < 10 memory files. The `--memory` flag being opt-in handles this.


## Lifecycle Events

**CAO lifecycle events** (all providers):

| Event | Memory Action | Phase |
|---|---|---|
| Terminal created | **Inject**: query SQLite metadata (scope precedence) → read matching wiki files → inject into provider context | 1 |
| Agent calls `memory_store` | **Store**: write wiki topic file + upsert SQLite metadata + update `index.md` | 1 |
| Agent calls `memory_recall` | **Recall**: query SQLite metadata → read matching wiki files → return content | 1 |
| Terminal exits/completes | **Extract**: read provider native session → store as session event in SQLite | 1 |
| Session cleanup (14d) | **Cleanup**: delete expired wiki files + SQLite metadata per tiered retention policy | 1 |
| Handoff returns | **Distill**: supervisor/CM reads session events + output → calls `memory_store` → wiki + metadata | 2 |
| Context usage exceeds threshold | **Pre-compaction flush**: CAO polls context usage → instructs agent to self-save | 2 |
| Agent calls `memory_forget` | **Forget**: remove entry from wiki file + delete SQLite metadata + update `index.md` | 2 |
| Agent calls `memory_consolidate` | **Consolidate**: surface merge/remove candidates from wiki + metadata → agent decides | 2 |

**Provider-native hooks** (Claude Code, Codex only):

| Hook | Config file | Trigger | How it works | Phase |
|---|---|---|---|---|
| **Stop** | `.claude/settings.local.json` (Claude Code) or `.codex/hooks.json` (Codex) | Every N messages (e.g., 15) | Hook script counts human messages in JSONL transcript → returns `{"decision": "block", "reason": "AUTO-SAVE checkpoint. Save key topics, decisions, quotes, and code from this session to your memory system. Organize into appropriate categories. Use verbatim quotes where possible. Continue conversation after saving."}` → agent saves → resumes. Uses a flag file to prevent infinite recursion. | 1 |
| **PreCompact** | `.claude/settings.local.json` (Claude Code only) | Before context compression | Hook always blocks → agent saves everything before context is summarized. Last chance to capture knowledge before compaction loses detail. | 1 |

For providers without native hook support (Kiro, Gemini, Kimi, Copilot), self-save relies on instruction-based prompting + CAO-side polling of `context_usage_percentage` where available.

### Provider-Specific Integration

| Provider | Native Memory | Injection | Extraction | Hook-Triggered Save |
|---|---|---|---|---|
| **Kiro CLI** | None | `.kiro/steering/*.md` or prepend to user message | `~/.kiro/sessions/cli/*.jsonl` (structured JSONL with token counts) | No — instruction-based + CAO polls `context_usage_percentage` |
| **Claude Code** | Rich (`MEMORY.md` + topic files) | `--append-system-prompt` (identity) + first user message (dynamic memory) | Read `~/.claude/projects/<path>/memory/MEMORY.md` + topic files. **Read-only** — no bidirectional sync | Yes — Stop hook (every N msgs) + PreCompact hook |
| **Codex CLI** | Placeholder (`memories/` dir, empty) | `-c developer_instructions` | `~/.codex/history.jsonl` + `state_5.sqlite` | Yes — Stop hook (same format as Claude Code) |
| **Gemini CLI** | Partial (`/memory add` at runtime) | Append memory block to `GEMINI.md` (CAO-managed with backup/restore) | `~/.gemini/history/` (per-project dirs) | No — instruction-based |
| **Kimi CLI** | None | `AGENTS.md` content (hierarchical merge, 32KB budget) | `~/.kimi/sessions/` (per-session UUID dirs) | No — instruction-based |
| **Copilot CLI** | None | `.github/copilot-instructions.md` | `~/.copilot/session-state/{uuid}/events.jsonl` (typed event stream) | No — instruction-based |


## Provider Session Context Details

| Provider | Session Context | Persistent Memory | Native Instruction File | CAO Injection |
|---|---|---|---|---|
| **Claude Code** | `~/.claude/projects/<path-encoded>/{uuid}.jsonl` | **Yes** — `MEMORY.md` + topic files with frontmatter | `CLAUDE.md` (project + global) | `--append-system-prompt` |
| **Copilot CLI** | `~/.copilot/session-state/*/events.jsonl` + `workspace.yaml` | No | `.github/copilot-instructions.md` | `--agent` + `--config-dir` |
| **Kiro CLI** | `~/.kiro/sessions/cli/{uuid}.json` + `.jsonl` | No (GitHub issue #6988 requested) | `.kiro/steering/*.md` + agent JSON | `--agent` (profile in `~/.kiro/agents/`) |
| **Kimi CLI** | `~/.kimi/sessions/` + `user-history/` | No | `AGENTS.md` (hierarchical merge) | `--agent-file` (yaml + `system.md`) |
| **Codex CLI** | `~/.codex/state_5.sqlite` + `history.jsonl` | Placeholder (`memories/` dir exists but empty) | `AGENTS.md` + `~/.codex/rules/` | `-c developer_instructions` |
| **Gemini CLI** | `~/.gemini/history/` (per-project dirs) | Partial (`/memory add` at runtime) | `GEMINI.md` (working dir) | Write `GEMINI.md` in working dir |

**Key notes per provider:**

**Kiro CLI** — No native memory. Rich session JSONL with token counts and `context_usage_percentage`. GitHub issue #6988 confirms cross-session memory is a requested but unimplemented feature.

**Claude Code** — Richest native system: `MEMORY.md` as index + topic files with YAML frontmatter (type: user/feedback/project/reference). CAO reads this but does not write to it (read-only extraction, no bidirectional sync).

**Codex CLI** — `memories/` directory exists but is empty — feature appears to be a placeholder. Hook support is the best among non-Claude providers.

**Gemini CLI** — `/memory add` at runtime appends facts but no persistent `~/.gemini/GEMINI.md` on disk. CAO writes a temporary project-level `GEMINI.md` in working directory (backed up and restored during cleanup).

**Kimi CLI** — No native memory. Context auto-compacts at 85% capacity. `AGENTS.md` hierarchical merge (git root to CWD, 32 KiB budget) is the injection point.

**GitHub Copilot CLI** — No persistent memory. Best-structured session format (`events.jsonl` with typed event stream). Easiest provider for session extraction.


## Implementation Phases

### Phase 1 (MVP) — Memory Store + Recall + Rule-Based Injection

* **Memory creation:** Agent-driven only. Agents call `memory_store` when they learn something worth keeping.
    * `MemoryMetadataModel` database table + migration in `database.py` (metadata only — content in wiki files)
    * `Memory` pydantic model in `models/memory.py`
    * `MemoryService` in `services/memory_service.py` (store, recall, forget, resolve_scope_id, get_memory_context_for_terminal)
    * `memory_store` and `memory_recall` MCP tools in `server.py`
    * REST API endpoint `GET /terminals/{terminal_id}/memory-context` (memory injection for terminal creation). The original proposal contemplated `POST /memory`, `GET /memory`, `DELETE /memory/{key}` — those three are **deferred to Phase 3** by Phase 2.5 U8 (no current consumer; MCP + CLI already cover all classes). Rationale in `MEMORY_SYSTEM_DESIGN.md` §REST API Endpoints.
    * Memory cleanup in `cleanup_service.py` (tiered retention)
* **Hook-triggered self-save** (for providers that support it):
    * Register Stop hook for Claude Code (`.claude/settings.local.json`) and Codex (`.codex/hooks.json`) — triggers agent self-save every N messages
    * Register PreCompact hook for Claude Code — triggers emergency save before context compression
    * Other providers: instruction-based ("save key findings via `memory_store` before finishing")
* **Context delivery:** Rule-based. SQLite metadata query (scope precedence: session > project > global, recency sort) → read matching wiki files → fill up to hard token budget (~2-4KB).
    * Memory injection at terminal creation for all 6 providers
    * Memory instruction added to built-in agent profiles
* **Storage setup:**
    * Wiki directory structure: `~/.aws/cli-agent-orchestrator/memory/{project_id}/wiki/` (canonical id per Phase 2.5 U6; legacy cwd-hash directories remain readable via alias table)
    * `index.md` master map — created on first `memory_store`, updated on each new topic file
    * SQLite stores metadata for fast lookup and scope resolution

### Phase 2 — Context-Manager Agent + Cross-Provider Handoff

* **Memory creation:** Add supervisor/context-manager distillation on agent exit or limit.
    * `SessionEventModel` database table + event logging
    * `session_context` MCP tool
    * `extract_session_context()` on BaseProvider + implementations for Claude Code, Gemini, Kiro, Codex, Copilot
    * `memory_forget` and `memory_consolidate` MCP tools
    * Pre-compaction memory flush: CAO polls `context_usage_percentage`
    * Token metadata per wiki article in `index.md`
* **Context delivery:** LLM-curated via context-manager agent.
    * `cao launch --memory` flag to enable context-manager
    * Context-manager spawned as background agent (same provider as supervisor)
    * Index-first selection: context-manager reads `index.md` + token metadata, fetches only relevant articles
    * Cache-aware injection: separate static identity (system prompt) from dynamic memory

### Phase 3 — Full Wiki Compilation + Scoring

* LLM-powered wiki compilation: new memories compiled into existing articles with cross-references (Karpathy pattern)
* Wiki lint: periodic health-check for contradictions, stale claims, orphan pages
* CrewAI-style 3-factor scoring (BM25 + recency + importance) as fallback
* `extract_session_context()` for Kimi (least structured — completes all 6 providers)

### Phase 4 — Wiki Maturity + UI

* Wiki self-healing: lint passes auto-fix contradictions, merge duplicates
* Memory import/export between projects
* Web UI for memory browsing/editing
* Cross-project memory federation
* Feedback loop: agents annotate context gaps → gaps feed back into wiki improvement


## Success Criteria

### Phase 1 (MVP)

* An agent can `memory_store` a fact → wiki topic file created + SQLite metadata upserted + `index.md` updated
* A different agent (same or different provider) can `memory_recall` the same fact
* Memory context is injected at terminal creation for all 6 providers
* Hook-triggered self-save fires reliably for Claude Code (Stop + PreCompact) and Codex (Stop)
* Memories survive server restart (wiki files + SQLite metadata persisted)
* Tiered retention cleanup removes expired session-scoped memories (14d) while preserving user/feedback memories indefinitely

### Phase 2

* Cross-provider handoff: Kiro does Task A → Gemini resumes Task A with Kiro's findings injected via context-manager
* Context-manager reads `index.md` + token metadata → produces curated context block within injection budget
* `session_context` tool returns event timeline
* `extract_session_context()` works for Claude Code, Gemini, Kiro, Codex, Copilot
* `memory_forget` removes from both wiki file and SQLite metadata
* Pre-compaction flush: CAO detects high context usage → agent self-saves before compaction

### Phase 3

* LLM-powered wiki compilation produces structured, cross-referenced articles
* Wiki lint detects contradictions, stale claims, orphan pages
* BM25 + recency + importance scoring works as fallback
* `extract_session_context()` works for Kimi (completes all 6 providers)

### Phase 4

* Wiki self-healing: lint auto-fixes contradictions, merges duplicates
* Web UI for memory browsing/editing
* Cross-project memory federation
* Feedback loop: agents annotate context gaps → gaps improve wiki


## Open Questions

**Technical:**

| Question | Context | Needs answer by |
|---|---|---|
| **Wiki file concurrency** | Multiple agents calling `memory_store` simultaneously may write to the same topic file. SQLite has WAL mode, but wiki files have no locking mechanism. Do we need file-level locks or write-through-SQLite? | Phase 1 |
| **Scope resolution edge cases** | What happens when an agent operates across multiple working directories? `project` scope uses hash of working dir — but which dir if the agent traverses several? | Phase 1 |
| **Memory injection vs provider limits** | Some providers have small instruction budgets (Kimi's `AGENTS.md` has 32KB total). How does the 2-4KB memory budget interact with the provider's own system prompt? Need to measure headroom per provider. | Phase 1 |
| **Hook recursion robustness** | MemPalace uses a flag file to prevent Stop hook infinite loops. Is this robust enough? What if the flag file isn't cleaned up after a crash? | Phase 1 |
| **Memory conflicts** | Two agents store contradictory memories with the same key. Current: last-write-wins via upsert. Phase 2 `memory_consolidate` surfaces candidates; Phase 3 wiki lint auto-detects. Should CAO warn the user on conflict? | Phase 2 |
| **Context-manager provider choice** | Current design: same provider as supervisor. Could a cheaper/faster model work? Tradeoff: cost savings vs quality of context selection. | Phase 2 |

**Product:**

| Question | Context | Needs answer by |
|---|---|---|
| **Privacy** | Should agents be able to read other agents' memories? Current: yes, within same scope. May need agent-scoped read restrictions for sensitive tasks. | Phase 2 |
| **User control** | Should users be able to view, edit, and delete memories via CLI before Phase 4 web UI? A `cao memory list/show/delete` command could fill the gap. | Phase 1 |
| **Memory sharing** | Can memories be shared across projects? Current: no, project-scoped. Phase 4 includes cross-project federation. | Phase 4 |


## Research Summary

Seven external references shaped CAO's memory design.

* **Karpathy's LLM Wiki** — LLM as compiler, not retriever. Wiki with `index.md` + cross-referenced articles replaces embedding-based RAG. **Primary inspiration for CAO's wiki storage pattern, index-first navigation, and lint-based maintenance.**
* **Andrew Ng's Context Hub** — Curated documentation registry with token metadata per entry, incremental fetch (index → select → fetch), and feedback loop for gap annotation. **Contributed: token metadata in `index.md`, budget-aware selection, Phase 4 feedback loop.**
* **Anthropic Managed Agents** — Session as a durable event log external to the agent. The harness (not the agent) owns state. **Contributed: CAO-as-harness pattern, `SessionEventModel` design, push injection model.**
* **OpenClaw Memory Failures** — Six documented failure modes (context collapse, MEMORY.md bloat, lossy summaries, token burning) validate structured wiki over embeddings. **Contributed: pre-compaction flush, daily logs, anti-patterns to avoid.**
* **CrewAI Memory** — 3-factor retrieval scoring (semantic + recency + importance). **Contributed: Phase 3 fallback scoring concept (adapted to BM25 instead of embeddings), memory type taxonomy.**
* **Bedrock AgentCore Memory** — Clean separation of session memory (raw) vs semantic memory (distilled). **Validates: our session context vs memory distinction, MCP-as-API approach.**
* **MemPalace** — L0-L3 tiered injection architecture with structural pre-filtering (+34% accuracy) and hook-triggered self-save via provider-native Stop/PreCompact hooks. 96.6% LongMemEval benchmark. **Contributed: hook-triggered self-save pattern, structural pre-filtering validates wiki+index approach, L0-L3 maps to CAO's injection layers.**


## Design Recommendations

### 1. Adopt Karpathy's Wiki Pattern — Compilation over Embedding

**This is the most important design decision for CAO memory.**

The naive approach is to store memories as text blobs and use embeddings/LIKE search to retrieve them. This is OpenClaw's approach, and it fails at scale — memories accumulate without structure, retrieval returns irrelevant results, and context burns tokens on noise.

**Recommendation:** CAO should use a **wiki-style compiled memory**, not embedding-based retrieval:

* Each project gets a `wiki/` directory (under `~/.aws/cli-agent-orchestrator/memory/{project_id}/wiki/` — canonical id from `resolve_project_id(cwd)` per Phase 2.5 U6, with Phase 1/2 cwd-hash paths preserved via `ProjectAliasModel`)
* `index.md` — master map of all memory articles, fits in one context injection
* One markdown file per concept/topic (e.g., `auth-module.md`, `user-preferences.md`, `testing-conventions.md`)
* Cross-references between articles via markdown links
* Agents read `index.md` + relevant articles, not a dump of everything

**How `memory_store` changes:**

* Phase 1: Simple append to topic file (no LLM needed)
* Phase 2: LLM-powered compilation — merges new facts into existing articles, updates cross-references, flags contradictions

**Why this works for CAO specifically:**

* CAO already injects context via files (GEMINI.md, system prompts) — a wiki is just better-organized files
* The compiled wiki is provider-agnostic — same markdown works for any agent
* `index.md` as the entry point solves the "what to inject" problem — inject the index + relevant articles, not everything
* Structured articles preserve relationships between facts

**What SQLite provides:** Metadata index (key, scope, tags, timestamps, file path) for fast lookup and scope resolution. Retention management. Audit trail via raw session events.

### 2. Pre-Compaction Memory Flush (from OpenClaw)

Before an agent's context fills up (or before handoff timeout), CAO should trigger a proactive memory save. This could be a periodic check during `wait_until_terminal_status()` polling, or an instruction in the agent's system prompt. **Priority:** Phase 2.

### 3. Daily Log Files for Auditability (from OpenClaw)

In addition to the `SessionEventModel` DB table, write a daily append-only log file at `~/.aws/cli-agent-orchestrator/logs/memory/YYYY-MM-DD.md`. Each entry: timestamp, terminal_id, provider, agent_profile, event_type, summary. **Priority:** Phase 2.

### 4. Separate Identity from Memory (validated by OpenClaw)

CAO already separates identity (agent profile system_prompt) from memory (what's been learned). One refinement: the injected memory block should be clearly delineated from the system prompt:

```
<!-- System prompt (identity) ends above -->
<!-- CAO Memory (learned context) begins below -->
## Context from CAO Memory
...
```

### 5. Hybrid Storage: Wiki Files + SQLite Metadata

Wiki files (`wiki/` + `index.md`) are the primary knowledge representation — human-readable, provider-agnostic, directly injected into agents. SQLite stores metadata for fast lookup, scope resolution, and retention management.

**Why not files-only?** OpenClaw's community had to build Mem0, MemClaw, QMD, Honcho — all because file-only memory doesn't scale for multi-agent scope resolution and retention.

**Why not DB-only?** Wiki files are what agents actually read. Markdown is provider-agnostic and inspectable.

**One risk to watch:** SQLite is single-writer. With many concurrent agents calling `memory_store`, WAL mode (already default in SQLAlchemy) and retry logic are needed.

### 6. Context Budget — Don't Over-Inject

* Phase 1: Hard limit of 10 most recent memories per scope, max ~2KB total injection
* Phase 2: Smart selection — rank by relevance (tags match current task), recency, and importance
* Phase 3: CrewAI-style 3-factor scoring with tunable weights

### 6a. Prompt Cache Awareness — Inject Memory Outside System Prompt

For providers that support prompt caching, injecting dynamic memory into the system prompt causes every invocation to miss the cache.

| Provider | System prompt (static, cacheable) | Memory injection (dynamic) |
|---|---|---|
| Claude Code | `--append-system-prompt` (agent identity + instructions) | Prepend to first user message as `<cao-memory>...</cao-memory>` block |
| Gemini | `GEMINI.md` (agent instructions) | Append memory block as a clearly delineated section at the end |
| Codex | `-c developer_instructions` (agent instructions) | Separate `-c` flag or prepend to user message |
| Kiro | Agent profile (static personality) | Inject via steering file or prepend to user message |

**Phase 1 simplification:** Use instruction file/system prompt injection for all providers (accept cache misses). Phase 2: separate static identity from dynamic memory for cache-aware providers.

### 7. Agent Decides What to Remember

**Phase 1 (MVP):** Agent-driven only. Agents call `memory_store` when they learn something worth keeping. For providers with hook support (Claude Code, Codex), register Stop/PreCompact hooks to automate the save trigger.

**Phase 2:** CAO auto-extracts on terminal exit. **Phase 3:** LLM-powered auto-extraction from full session transcripts.

This is how both Claude Code and OpenClaw work — the agent decides what to remember, not the framework. It produces higher quality memories because the agent understands context.

### 8. Don't Sync Bidirectionally with Native Memory Systems (Yet)

Phase 1 — **read-only extraction**. CAO can read Claude's `MEMORY.md` for useful context, but should NOT write to it. Two-way sync introduces conflict resolution complexity. Phase 3+: Consider bidirectional sync with conflict resolution.

### 9. Memory Consolidation — Gated Triggers + Tiered Retention + Wiki Lint

**Gated trigger** to decide when consolidation runs:

| Gate | Threshold | Purpose |
|---|---|---|
| **Time** | 24 hours since last consolidation | Prevents over-consolidation during bursts |
| **Count** | 10+ new memories since last consolidation | Ensures enough material to consolidate |
| **Lock** | File-based `.consolidation.lock` with stale timeout (5 min) | Prevents concurrent consolidation from multiple agents |

All three gates must pass before consolidation triggers.

**Tiered retention strategy:**

| Memory Type | Retention |
|---|---|
| `user` | Indefinite (preferences rarely expire) |
| `feedback` | Indefinite (corrections are always relevant) |
| `project` | 90 days, then consolidate |
| `reference` | 90 days (external links go stale) |
| Session-scoped working memory | 14 days (tied to cleanup_service) |


## Case Study: Cross-Provider Handoff (Kiro → Gemini)

### Scenario

A supervisor agent needs auth module analysis done by Kiro (specialist), then a Gemini agent should continue fixing the issues Kiro found.

### With Context-Manager Agent (Phase 2 Target State)

```
cao launch --memory --supervisor claude

1. Supervisor: handoff("kiro-developer", "Analyze auth module security")

2. CAO intercepts → asks context-manager: "What context does kiro-developer need?"
   → Context-manager reads index.md → finds: project conventions, prior auth work
   → Context-manager produces curated block (800 tokens)
   → CAO injects into Kiro's system prompt

3. Kiro analyzes WITH project context already loaded
   → Stores findings via memory_store (self-save)
   → Completes task

4. Context-manager distills Kiro's session: reads handoff output + session events
   → Produces working memory: "auth-findings", "auth-recommendation"
   → Updates wiki/auth-module.md with new cross-references

5. Supervisor: handoff("gemini-developer", "Fix the auth module security issues")

6. CAO intercepts → asks context-manager: "What context does gemini-developer need?"
   → Context-manager reads index.md → finds: auth-module.md (just updated), user prefs
   → Context-manager reads auth-module.md (includes Kiro's findings)
   → Produces curated block (1200 tokens): findings + recommendation + testing prefs
   → CAO injects into GEMINI.md

7. Gemini starts with rich, curated context:
   - Knows what Kiro found AND the project's testing conventions
   - Proceeds directly to fixing with full situational awareness
```
