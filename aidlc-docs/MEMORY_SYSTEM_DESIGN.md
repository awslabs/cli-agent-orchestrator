# CAO Memory System — Technical Design

## Overview

Phase 1 delivers agent-driven memory storage and rule-based context injection: agents call `memory_store` via MCP to persist facts, and CAO injects relevant memories into each new terminal at creation time using a scope-precedence query against SQLite metadata and wiki files. Phase 2 adds a context-manager agent and cross-provider handoff distillation — this design document covers Phase 1 in full and Phase 2 lightly, so Phase 1 is not over-built.


## Data Models

### `MemoryMetadataModel` (SQLAlchemy — SQLite table)

```python
from sqlalchemy import Column, String, DateTime, UniqueConstraint
from sqlalchemy.ext.declarative import declarative_base
import uuid
from datetime import datetime

Base = declarative_base()

class MemoryMetadataModel(Base):
    __tablename__ = "memory_metadata"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    key = Column(String, nullable=False)                  # slug identifier, e.g. "prefer-pytest"
    memory_type = Column(String, nullable=False)          # user | feedback | project | reference
    scope = Column(String, nullable=False)                # global | project | session | agent
    scope_id = Column(String, nullable=True)              # auto-resolved; None for global
    file_path = Column(String, nullable=False)            # path to wiki topic file (content lives here)
    tags = Column(String, nullable=False, default="")     # comma-separated
    source_provider = Column(String, nullable=True)       # e.g. "kiro_cli", "claude_code"
    source_terminal_id = Column(String, nullable=True)    # CAO terminal_id that created this
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("key", "scope", "scope_id", name="uq_memory_key_scope"),
    )
```

**`scope_id` auto-resolution:**

| Scope | `scope_id` value |
|---|---|
| `global` | `None` |
| `project` | Canonical project id from `resolve_project_id(cwd)` — see §Project Identity. |
| `session` | CAO session name |
| `agent` | agent_profile name |

### `Memory` (Pydantic model — what agents and services work with)

```python
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class Memory(BaseModel):
    id: str
    key: str
    memory_type: str                  # user | feedback | project | reference
    scope: str                        # global | project | session | agent
    scope_id: Optional[str]
    file_path: str
    tags: str                         # comma-separated
    source_provider: Optional[str]
    source_terminal_id: Optional[str]
    created_at: datetime
    updated_at: datetime
    content: str                      # loaded from wiki file on demand
```

The `content` field is populated by reading the wiki file at `file_path`. It is not stored in SQLite.


## Project Identity

**Shipped in Phase 2.5 U6** (2026-04-21). Phase 1/2 keyed the `project` scope by `sha256(realpath(cwd))[:12]`; this broke under directory rename and resolved differently across git worktrees of the same repo (`@patricka3125`, PR #179). U6 replaces the single-source keying with a **three-tier precedence chain** plus an alias table so renames and worktrees stay continuous.

### Precedence chain (`resolve_project_id(cwd)`)

Implemented as a **module-level function** in `services/memory_service.py` — callers import it directly rather than carrying a `MemoryService` instance.

1. **Explicit override.** Settings key `memory.project_id` (nested under `memory`) or env `CAO_PROJECT_ID`. Validated through `_validate_project_id_override` — null-byte reject + whitelist regex (`^[a-zA-Z0-9_\-]+$`). Reject-style on failure (`ProjectIdentityResolutionError`); no silent sanitization.
2. **Git remote URL.** First configured remote (`git -C <cwd> config --get remote.origin.url`), argv-form subprocess with `timeout=2`, `shell=False`. Normalized and SHA256-hashed to 12 chars. Stable across rename, worktree, and branch switch.
3. **`sha256(realpath(cwd))[:12]` fallback.** Byte-identical to Phase 1/2 behavior. Used only when both 1 and 2 are unavailable (non-git directory, or git binary missing on `PATH`).

On every resolution that yields a canonical id from source 1 or 2, the **current cwd-hash is recorded as an alias** via `record_project_alias(project_id, alias, kind)` (best-effort; write failures fall to `logger.debug` and do not block resolution). Source 2 additionally registers the raw git URL itself as a `git_remote` alias (forward-compat for URL rewrites like protocol flips).

### `ProjectAliasModel` (SQLAlchemy)

```python
class ProjectAliasModel(Base):
    __tablename__ = "project_aliases"
    project_id = Column(String, primary_key=True)  # canonical id (source 1 or 2)
    alias      = Column(String, primary_key=True)  # cwd-hash, alternate path hash, or raw git URL
    kind       = Column(String, nullable=False)    # "cwd_hash" | "git_remote" | "manual"
    created_at = Column(DateTime, default=datetime.utcnow)
```

Composite primary key `(project_id, alias)` allows a project to own many aliases. Read-path queries union wiki files across the canonical id and any aliases, so memories written under Phase 1/2 cwd-hash paths continue to surface after the resolver promotes the project to a git-remote canonical id.

### Dry-run migration planner

`MemoryService.plan_project_dir_migration(canonical_id, alias)` walks the existing `<cwd_hash>/wiki/` layout on disk and **reports** the rename plan — classified as one of four actions `none | rename | merge | conflict` — without moving files or writing to `project_aliases`. Return shape is `dict` with `{dry_run: True, action, files: [...]}`. There is **no `apply_*` variant** in Phase 2.5: operators review the plan, and memories under legacy `<cwd_hash>/` directories remain readable via the alias-aware read-path. A live migration tool (`cao memory migrate-project-ids [--apply]`) is deferred to Phase 3 — see `aidlc-docs/phase2.5/phase3-backlog.md` U6 INFO-3.

### `ProjectIdentityResolutionError`

Raised when an explicit override fails validation (null byte, whitespace, character outside whitelist, empty string). The resolver does NOT raise when git or cwd-hash resolution fails individually — those fall through to the next tier. The exception is reserved for operator-supplied bad input so the mistake surfaces loudly.

Full rationale and the ten team-lead decisions that shaped U6 are recorded in `aidlc-docs/phase2.5/audit.md` §"2026-04-20 — U6 Stable Project Identity Resolver" and §"Security Review — U6".


## Storage Layout

```
~/.aws/cli-agent-orchestrator/memory/
├── {project_id}/                           # canonical id from resolve_project_id (U6)
│   └── wiki/
│       ├── index.md                    # Master map, updated on every store
│       ├── global/                     # Global-scope topic files
│       │   ├── prefer-pytest.md
│       │   └── no-summaries.md
│       ├── project/                    # Project-scoped topic files
│       │   ├── auth-issues.md
│       │   └── testing-conventions.md
│       ├── session/                    # Session-scoped topic files
│       │   └── auth-findings.md
│       └── agent/                      # Agent-scoped topic files
│           └── gemini-developer-prefs.md
└── memory.db                           # SQLite metadata
```

### Wiki Topic File Format

Each topic file is a markdown file with a standard header. On `memory_store`, new content is appended as a timestamped entry. Phase 1 uses simple append; Phase 2 LLM-powered merging compiles entries into articles.

```markdown
# prefer-pytest
<!-- scope: global | type: user | tags: testing,pytest -->

## 2026-04-10T14:23:11Z
User prefers pytest for all tests. Uses `pytest.ini` at repo root.
Do not suggest unittest or nose.

## 2026-04-12T09:05:44Z
Confirmed: user wants parametrize over class-based test suites.
```

### `index.md` Format

One line per topic file. Keeps the master map compact so it can be injected as a single context block (or read by the context-manager without fetching all articles).

```markdown
# CAO Memory Index
<!-- Updated: 2026-04-12T09:05:44Z -->

## global
- [prefer-pytest](global/prefer-pytest.md) — scope:global type:user tags:testing,pytest ~120tok
- [no-summaries](global/no-summaries.md) — scope:global type:feedback tags:style ~80tok

## project/{project_id}
- [auth-issues](project/auth-issues.md) — scope:project type:project tags:auth,security ~340tok
- [testing-conventions](project/testing-conventions.md) — scope:project type:project tags:testing ~210tok

## session/{session_name}
- [auth-findings](session/auth-findings.md) — scope:session type:project tags:auth,kiro ~180tok
```

Format per line: `- [{key}]({relative_path}) — scope:{scope} type:{memory_type} tags:{tags} ~{token_estimate}tok`


## MemoryService

```python
# src/cli_agent_orchestrator/services/memory_service.py

class MemoryService:

    async def store(
        self,
        key: str,
        content: str,
        memory_type: str = "project",
        scope: str = "project",
        tags: str = "",
        terminal_context: Optional[dict] = None,  # {"terminal_id": ..., "provider": ..., "session": ..., "cwd": ...}
    ) -> MemoryMetadataModel:
        """Store or update a memory. Upserts wiki file + SQLite metadata."""

    async def recall(
        self,
        query: Optional[str] = None,
        scope: Optional[str] = None,
        memory_type: Optional[str] = None,
        limit: int = 10,
        terminal_context: Optional[dict] = None,
    ) -> list[Memory]:
        """Recall memories matching query and filters. Returns Memory objects with content loaded."""

    async def forget(
        self,
        key: str,
        scope: str = "project",
        terminal_context: Optional[dict] = None,
    ) -> bool:
        """Remove a memory. Deletes SQLite row + removes entry from wiki file + updates index.md.
        Internal use only in Phase 1; exposed as MCP tool in Phase 2."""

    def resolve_scope_id(
        self,
        scope: str,
        terminal_context: Optional[dict],
    ) -> Optional[str]:
        """Resolve scope_id from terminal context. Returns None for global scope.

        For ``scope="project"``, delegates to the module-level ``resolve_project_id(cwd)``
        (see §Project Identity). Other scopes read session/agent fields from
        ``terminal_context`` directly."""

    def get_memory_context_for_terminal(
        self,
        terminal_id: str,
        budget_tokens: int = 800,
    ) -> str:
        """Build the memory context block to inject at terminal creation.
        Applies scope precedence, respects token budget."""
```

### `store` — Upsert Logic

1. Call `resolve_scope_id(scope, terminal_context)` to get `scope_id`.
2. Query SQLite: `SELECT * FROM memory_metadata WHERE key=? AND scope=? AND scope_id IS ?`
3. **If row exists (update):**
    - Append new timestamped entry to the existing wiki file at `row.file_path`
    - Update SQLite: `updated_at = now()`, `tags`, `source_provider`, `source_terminal_id`
4. **If row does not exist (create):**
    - Determine wiki file path: `{memory_base}/{project_id}/wiki/{scope}/{key}.md` (canonical id from `resolve_project_id(cwd)`).
    - Write wiki file with header and first entry
    - Append line to `index.md` (create `index.md` if it doesn't exist)
    - Insert new SQLite row with `file_path` pointing to the new wiki file
5. Return the upserted `MemoryMetadataModel`.

### `recall` — Query Logic

1. Build SQLite query from filters:
    - If `query` is set: `WHERE (key LIKE '%{query}%' OR tags LIKE '%{query}%')`
    - If `scope` is set: `AND scope = '{scope}'`
    - If `memory_type` is set: `AND memory_type = '{memory_type}'`
    - `ORDER BY updated_at DESC LIMIT {limit}`
2. For each matching row, read the wiki file at `row.file_path`.
3. Return `Memory` objects (SQLite fields + `content` from wiki file).

### `get_memory_context_for_terminal` — Scope Precedence + Budget

Applies scope precedence: session > project > global (agent scope loaded only when `terminal_context` includes `agent_profile`). Fills the token budget greedily in precedence order.

1. Identify `scope_ids` from the terminal's context (session name, project hash, global=None, agent_profile).
2. Query SQLite in order: session-scoped rows first, then project, then global, then agent.
3. For each row, estimate token count (approximate: `len(content) / 4`).
4. Include rows until budget is exhausted.
5. Format and return:

```
## Context from CAO Memory
- [session] auth-findings: 3 security issues in middleware.py (L45, L89, L112)
- [project] testing-conventions: Use pytest with parametrize; no unittest
- [global] prefer-pytest: User prefers pytest for all tests
- [global] no-summaries: Don't summarize at end of responses
```


## MCP Tools

Both tools are registered in `mcp_server/server.py` alongside `handoff`, `assign`, and `send_message`. The MCP server reads `CAO_TERMINAL_ID` from the calling environment to populate `terminal_context`.

### `memory_store`

```
Tool: memory_store
Description: Store or update a persistent memory fact. Content is saved to a wiki file
             and indexed in SQLite. Identical key+scope combinations are updated (upsert).

Parameters:
  key         (str, required)           — Slug identifier, e.g. "prefer-pytest", "auth-issues"
  content     (str, required)           — Memory content in markdown
  memory_type (str, default="project")  — One of: user | feedback | project | reference
  scope       (str, default="project")  — One of: global | project | session | agent
  tags        (str, optional)           — Comma-separated tags for search, e.g. "auth,security"

Returns:
  {
    "success": true,
    "key": "prefer-pytest",
    "scope": "global",
    "scope_id": null,
    "file_path": "~/.aws/cli-agent-orchestrator/memory/{hash}/wiki/global/prefer-pytest.md",
    "action": "created"   # or "updated"
  }
```

### `memory_recall`

```
Tool: memory_recall
Description: Retrieve memories matching a query and optional filters.
             Returns content from matching wiki files, sorted by recency.

Parameters:
  query       (str, optional)           — Search query matched against key and tags
  scope       (str, optional)           — Filter by scope. Omit to search all scopes.
  memory_type (str, optional)           — Filter by type. Omit to return all types.
  limit       (int, default=10)         — Maximum number of results

Returns:
  {
    "memories": [
      {
        "key": "prefer-pytest",
        "content": "User prefers pytest for all tests...",
        "memory_type": "user",
        "scope": "global",
        "scope_id": null,
        "tags": "testing,pytest",
        "file_path": "...",
        "updated_at": "2026-04-12T09:05:44Z"
      }
    ]
  }
```


## Provider Injection

Memory context is injected at terminal creation, before the agent starts processing. The injection mechanism is provider-specific.

| Provider | Injection mechanism | Where in init flow |
|---|---|---|
| Claude Code | `--append-system-prompt` (identity) + prepend to first user message as `<cao-memory>` block | Before CLI start (system prompt); after IDLE (first message) |
| Kiro CLI | Prepend to first user message, or write to `.kiro/steering/cao-memory.md` | After IDLE |
| Gemini CLI | Append CAO memory section to `GEMINI.md` in working directory (backed up, restored on cleanup) | Before CLI start |
| Codex CLI | `-c developer_instructions` flag with memory block prepended | Before CLI start |
| Kimi CLI | Append memory block to `AGENTS.md` (within 32KB budget) | Before CLI start |
| Copilot CLI | Append memory block to `.github/copilot-instructions.md` | Before CLI start |

**Phase 1 simplification:** All providers inject into the instruction file or system prompt, accepting cache misses for providers that support prompt caching. Phase 2 separates static identity from dynamic memory for cache-aware providers, with dynamic memory delivered via first user message or a dedicated dynamic file.

Memory context is retrieved by calling `MemoryService.get_memory_context_for_terminal(terminal_id, budget_tokens=800)` during terminal initialization in `terminal_service.py`.


## Hook-Triggered Self-Save (Phase 1 — Claude Code + Codex)

For providers that support it, CAO registers provider-native hooks to automate memory save triggers. The hook does not parse or extract — it interrupts the agent and instructs it to save using its own judgment.

### Stop Hook

**Trigger:** After every N human messages (default: 15).

**Mechanism:**

1. Hook script (`cao_stop_hook.sh`) is invoked by the provider after each human message.
2. Script checks if a `stop_hook_active` flag file exists (to prevent recursion). If yes, exits 0.
3. Script counts human messages in the JSONL transcript (Claude Code: `~/.claude/projects/<path>/{uuid}.jsonl`, Codex: `~/.codex/history.jsonl`).
4. If `message_count % N == 0`, creates the `stop_hook_active` flag file and returns:
   ```json
   {"decision": "block", "reason": "AUTO-SAVE checkpoint. Save key topics, decisions, quotes, and code from this session to your memory system. Organize into appropriate categories. Use verbatim quotes where possible. Continue conversation after saving."}
   ```
5. Agent receives the block signal, calls `memory_store` for relevant findings.
6. Agent completes the save, hook script removes the flag file, agent resumes.

### PreCompact Hook (Claude Code only)

**Trigger:** Immediately before context compression.

**Mechanism:** Hook always returns `{"decision": "block", "reason": "Emergency save before compaction..."}`. Agent saves all key findings before the compaction summarizes and potentially loses detail. This is the last chance to capture knowledge from a full context window.

### Config Locations

**Claude Code** — `.claude/settings.local.json`:
```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [{"type": "command", "command": "~/.aws/cli-agent-orchestrator/hooks/cao_stop_hook.sh"}]
      }
    ],
    "PreCompact": [
      {
        "matcher": "",
        "hooks": [{"type": "command", "command": "~/.aws/cli-agent-orchestrator/hooks/cao_precompact_hook.sh"}]
      }
    ]
  }
}
```

**Codex** — `.codex/hooks.json`:
```json
{
  "stop": "~/.aws/cli-agent-orchestrator/hooks/cao_stop_hook.sh"
}
```

### Other Providers

For Kiro, Gemini, Kimi, and Copilot (no native hook API), the agent profile instruction includes: *"When you discover important facts, decisions, or user preferences, store them using the `memory_store` tool. Save key findings before completing your task."*


## Cleanup Service Integration

Memory cleanup is added to the existing `cleanup_service.py` as a scheduled task (runs with the same cadence as terminal cleanup).

### Retention Policy

| Memory type | Retention |
|---|---|
| `user` | Indefinite |
| `feedback` | Indefinite |
| `project` | 90 days from `updated_at` |
| `reference` | 90 days from `updated_at` |
| Session-scoped (any type) | 14 days from `updated_at` |

### Cleanup Logic

1. Query SQLite for expired rows:
    - `scope = 'session' AND updated_at < now() - 14 days`
    - `memory_type IN ('project', 'reference') AND updated_at < now() - 90 days`
2. For each expired row:
    - Read the wiki file at `row.file_path`.
    - Remove the entries that belong to this `key` from the wiki file.
    - If the wiki file becomes empty after removal, delete the file.
    - If a wiki file was deleted, remove its line from `index.md`.
    - Delete the SQLite row.
3. Log a summary of expired entries to `~/.aws/cli-agent-orchestrator/logs/memory/YYYY-MM-DD.md` (append-only audit log).

`user` and `feedback` memories are never deleted by automated cleanup. They can only be removed via `memory_forget` (Phase 2 MCP tool) or future CLI commands.


## REST API Endpoints

Added to `api/main.py`. All endpoints delegate to `MemoryService`.

### Shipped (Phase 1/2)

```
GET    /terminals/{terminal_id}/memory-context
       Query params: budget_tokens (default 800)
       Returns: { context: "## Context from CAO Memory\n..." }
```

Used internally by terminal initialization (and the Kiro AgentSpawn hook) to fetch the pre-formatted memory block for injection.

**Auth posture.** Trusted-local; no auth at endpoint layer because the server binds to `127.0.0.1` only (see `constants.py` host binding — `SERVER_HOST = os.environ.get("CAO_API_HOST", "127.0.0.1")`) and the Kiro AgentSpawn hook is the sole invoker. Any Phase 3 REST expansion MUST revisit this — loopback trust does not extend to write endpoints.

### Deferred to Phase 3 (Phase 2.5 U8 decision, 2026-04-21)

The original design contemplated three additional endpoints:

```
POST   /memory              (store)
GET    /memory              (list/search)
DELETE /memory/{key}        (forget)
```

These are **not shipped in Phase 2.5** and are **formally deferred to Phase 3**. Full rationale in `aidlc-docs/phase2.5/audit.md` §"2026-04-21 — U8 Decision — DEFER to Phase 3 (architect)". Summary:

1. **YAGNI.** No current consumer. Agents use MCP (`memory_store` / `memory_recall` / `memory_forget` / `memory_consolidate`); humans use CLI (`cao memory list / show / delete / clear`); `web/src` has zero memory API references. Shipping endpoints without a consumer locks unreviewed schema and pagination decisions.
2. **Double-coverage redundancy.** Each missing endpoint duplicates an MCP tool and a CLI command. Three surfaces for three consumer classes is correct only when all three classes have concrete users — today REST has none.
3. **Auth surface.** The shipped endpoint is safe because it is read-only and loopback-bound. Opening POST/DELETE without a designed auth model (token? origin allow-list? process-identity?) would either force new security-review work into Phase 2.5 scope or ship insecure by default. Neither is acceptable.
4. **Phase 3 shape drivers.** Real Web UI requirements in Phase 3 — mock-ups, filter needs, pagination semantics, auth story tied to Web UI session model — will drive REST design as a coherent whole, not back-filled to match imagined needs.

When Phase 3 revisits this surface, note:
- The `MemoryService` internal API has been moving (Phase 2 U1 SQLite → Phase 2.5 U6 stable project identity); wait for it to settle before exposing via REST.
- Any write endpoint requires a designed auth model — loopback alone is insufficient for POST/DELETE.
- Consider whether REST adds value beyond a Web-UI-to-API bridge; a server-side rendered Web UI that talks to `MemoryService` in-process may avoid the need for public REST endpoints entirely.


## Database Migration

Added to `src/cli_agent_orchestrator/clients/database.py`.

### New Table

```python
class MemoryMetadataModel(Base):
    __tablename__ = "memory_metadata"
    # ... (see Data Models section above)


class ProjectAliasModel(Base):
    # Added in Phase 2.5 U6 to support rename / worktree continuity.
    # See §Project Identity above.
    __tablename__ = "project_aliases"
    project_id = Column(String, primary_key=True)
    alias      = Column(String, primary_key=True)
    kind       = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
```

### Migration

```python
def create_tables(engine):
    # Existing tables...
    Base.metadata.create_all(engine)
    # WAL mode already enabled in existing database initialization
```

Migration SQL (for reference):
```sql
CREATE TABLE IF NOT EXISTS memory_metadata (
    id TEXT PRIMARY KEY,
    key TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    scope TEXT NOT NULL,
    scope_id TEXT,
    file_path TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    source_provider TEXT,
    source_terminal_id TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (key, scope, scope_id)
);
CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_metadata (scope, scope_id);
CREATE INDEX IF NOT EXISTS idx_memory_updated ON memory_metadata (updated_at);
CREATE INDEX IF NOT EXISTS idx_memory_type ON memory_metadata (memory_type);

-- Phase 2.5 U6
CREATE TABLE IF NOT EXISTS project_aliases (
    project_id TEXT NOT NULL,
    alias      TEXT NOT NULL,
    kind       TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (project_id, alias)
);
```

WAL mode is already enabled for the database in the existing initialization; no change needed for concurrent write safety.


## Testing Strategy

### Unit Tests — `test/services/test_memory_service.py`

Mock filesystem (use `tmp_path` fixture) and SQLite (in-memory SQLite via `sqlite:///:memory:`). Do not test provider injection here — that belongs in provider unit tests.

Key test cases:

1. **Store creates wiki file and SQLite row**: call `store(key="prefer-pytest", content="...", scope="global")` → assert wiki file exists at expected path, SQLite row inserted, `index.md` updated with new entry.
2. **Store same key updates (upsert)**: call `store` twice with same `key+scope` → assert wiki file updated (content appended), SQLite `updated_at` changes, `index.md` not duplicated.
3. **Recall returns matching memories with content**: store a memory, call `recall(query="pytest")` → assert returned `Memory` has `content` populated from wiki file.
4. **Scope precedence in get_memory_context_for_terminal**: store memories at session, project, and global scope → assert session memories appear before project before global in the returned context block.
5. **Token budget respected**: store many memories → call `get_memory_context_for_terminal(budget_tokens=200)` → assert returned context does not exceed budget.
6. **Forget removes from wiki and SQLite**: call `forget(key, scope)` → assert SQLite row deleted, entry removed from wiki file, `index.md` updated.
7. **Cleanup removes expired session memories**: insert session-scoped row with `updated_at` = 15 days ago → run cleanup → assert row deleted, wiki entry removed.
8. **Cleanup preserves user and feedback memories**: insert `user` and `feedback` memories older than 90 days → run cleanup → assert both rows still exist.

### Unit Tests — `test/cli/test_memory_commands.py`

Test CLI commands (`cao memory store`, `cao memory recall`, `cao memory forget`, `cao memory list`). Mock `MemoryService` methods. Verify output formatting and argument parsing.

### Integration Tests

Mark with `@pytest.mark.integration`. Test against a real SQLite database and real filesystem (use `tmp_path`). Do not mock `MemoryService`.

Key integration test cases:

1. Full `store → recall` round-trip: store a memory, recall it, assert content matches.
2. `store → store (upsert) → recall`: two stores with same key, recall returns latest content.
3. Recall with scope filter: store memories at different scopes, assert scope filter works.
4. Cleanup integration: create expired rows, run cleanup, assert correct rows deleted and wiki files updated.
5. Concurrent stores: multiple `store` calls in parallel to the same wiki file (use `asyncio.gather`) → assert no corruption, all entries present.


## Phase 2 Design Notes (Don't Build Yet)

Phase 1 should not pre-optimize for the following Phase 2 additions:

- **`SessionEventModel` + event logging** — append-only event table (task_started, task_completed, handoff_returned, memory_stored) in `database.py`. Required by context-manager and `session_context` tool.
- **Context-manager agent** — dedicated background agent (same provider as supervisor) spawned via `cao launch --memory`. Reads `index.md` + token metadata, selects relevant articles, produces curated injection block at each handoff.
- **`memory_forget` and `memory_consolidate` MCP tools** — exposed to agents in Phase 2. `forget` is an internal-only method in Phase 1.
- **`extract_session_context()` on BaseProvider** — new abstract method for extracting structured context from provider-native session files. Implementations for Claude Code, Gemini, Kiro, Codex, Copilot.
- **`session_context` MCP tool** — returns event timeline for cross-provider resumption.
- **Wiki compilation (LLM-powered)** — Phase 2 replaces Phase 1's simple append with LLM-powered merging of entries into coherent articles with cross-references (Phase 3 for full Karpathy pattern).
- **BM25 fallback search** — Phase 2 hybrid recall: SQLite metadata query + BM25 over wiki file content for queries that don't match key/tags.
- **Token metadata in `index.md`** — approximate token counts per article entry, required for context-manager's budget-aware fetch decisions. Phase 1 includes the `~{N}tok` field in `index.md` as a stub (estimated at write time); Phase 2 uses it for selection.
- **Cache-aware injection** — separate static identity (system prompt) from dynamic memory for providers supporting prompt caching. Phase 1 accepts cache misses.
- **Pre-compaction flush** — CAO polls `context_usage_percentage` during `wait_until_terminal_status()` and instructs agent to self-save when usage exceeds threshold.


## Phase 3 Design Notes (Don't Build Yet)

Phase 3 completes the provider extraction layer and replaces Phase 1's simple append with LLM-compiled wiki articles:

- **`extract_session_context()` for Kimi** — Kimi is the least-structured provider (`~/.kimi/sessions/` + `kimi.json`). Completing this implementation finishes all 6 providers.
- **LLM-powered wiki compilation (Karpathy pattern)** — `memory_store` no longer appends a raw entry to the topic file. Instead, a compilation step merges the new fact into the existing article: updates context, adds cross-references to related topic files, flags contradictions. Phase 1's flat append is the stub this replaces.
- **Cross-references in wiki articles** — Each topic file gains a `## See Also` section linking to related articles (e.g., `auth-module.md` → `testing-conventions.md` → `user-preferences.md`). Enables multi-hop reasoning by the context-manager.
- **Wiki lint** — A periodic background pass (daily, via Flow) detects contradictions (two articles with conflicting claims), stale claims (references to removed code), and orphan pages (files not in `index.md`). Surfaces candidates for agent review.
- **3-factor scoring fallback** — When the context-manager agent is not active, Phase 3 introduces BM25 + recency + usage-frequency scoring to replace the Phase 1 recency-only sort. Weights: BM25 relevance 50%, recency decay 30%, usage count 20%. This is the fallback only — the context-manager's LLM judgment is always preferred.
- **Daily audit log** — In addition to SQLite events, write a human-readable append-only log at `~/.aws/cli-agent-orchestrator/logs/memory/YYYY-MM-DD.md` (timestamp, terminal_id, provider, event_type, summary). Eases debugging without querying the DB.


## Phase 4 Design Notes (Don't Build Yet)

Phase 4 matures the wiki and adds user-facing surfaces:

- **Wiki self-healing** — Lint passes become automated: the context-manager (or a dedicated lint agent) auto-merges duplicate articles, resolves detected contradictions by choosing the more recent claim, and removes orphan pages. No human intervention required for routine maintenance.
- **Memory import/export** — `cao memory export --project <hash> > memories.tar.gz` and `cao memory import memories.tar.gz`. Enables sharing project knowledge across team members or bootstrapping a new workspace from an existing one.
- **Web UI** — Memory browsing, search, and editing in the CAO web interface (`web/`). Surfaces: list all memories by scope/type, view/edit wiki article content, delete or merge entries, view `index.md` as a navigable graph.
- **Cross-project memory federation** — A shared global wiki separate from the per-project wikis. User-level preferences and patterns (e.g., "always use pytest", "prefer async/await over callbacks") live in the global wiki and are injected into all terminals regardless of project. Project wikis remain isolated.
- **Feedback loop** — Agents annotate gaps in the context they received: "I needed X but didn't find it in memory." These annotations are stored and fed back into the wiki improvement process — missing articles get created, under-tagged articles get better tags. Sourced from Andrew Ng's Context Hub pattern.
