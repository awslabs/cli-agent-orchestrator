# Tasks — CAO Memory System Phase 2

**Generated**: 2026-04-14
**Scope**: Phase 2 — SQLite as optimization, context-manager agent, session event log, cross-provider extraction, BM25 search, cache-aware injection.
**Design reference**: `aidlc-docs/MEMORY_SYSTEM_DESIGN.md` (Phase 2 Design Notes)
**Prerequisite**: Phase 1 complete and all SC-1 through SC-18 passing.

---

## Unit Map

| ID | Unit | Agent | Depends On | Notes |
|---|---|---|---|---|
| U1 | SQLite Integration | Backend Developer | — | Replaces file-scanning in MemoryService |
| U2 | SessionEventModel + Event Logging | Backend Developer | U1 | Append-only event table |
| U3 | memory_forget + memory_consolidate MCP Tools | Integration Specialist | U1 | Expose forget to agents; add consolidate |
| U4 | extract_session_context() — 5 Providers | Backend Developer | U2 | Claude Code, Gemini, Kiro, Codex, Copilot (Kimi deferred to P3) |
| U5 | session_context MCP Tool | Integration Specialist | U2,U4 | Returns event timeline for cross-provider resumption |
| U6 | BM25 Fallback Search | Backend Developer | U1 | Hybrid recall: SQLite metadata + BM25 over wiki content |
| U7 | Cache-Aware Injection | Backend Developer | U1 | Separate static identity from dynamic memory |
| U8 | Pre-Compaction Flush | Backend Developer | U1 | Poll context_usage_percentage, trigger self-save |
| U9 | Context-Manager Agent | Backend Developer | U2,U4,U5 | Dedicated background agent for curated injection |
| U10 | Tests & Validation | Code Reviewer | U1–U9 | Unit + integration + mypy |

---

## U1 — SQLite Integration

**Agent**: Backend Developer
**Files**: `src/cli_agent_orchestrator/clients/database.py`, `src/cli_agent_orchestrator/models/memory.py`, `src/cli_agent_orchestrator/services/memory_service.py`

**Context**: Phase 1 uses file-scanning over `index.md` for recall and cleanup. Phase 2 replaces these with SQLite queries. No new capability — faster queries and WAL concurrency safety. The `MemoryMetadataModel` schema is already defined in `MEMORY_SYSTEM_DESIGN.md`.

### Subtasks

- [ ] **U1.1** Add `MemoryMetadataModel` to `clients/database.py`
  ```python
  class MemoryMetadataModel(Base):
      __tablename__ = "memory_metadata"
      id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
      key = Column(String, nullable=False)
      memory_type = Column(String, nullable=False)
      scope = Column(String, nullable=False)
      scope_id = Column(String, nullable=True)
      file_path = Column(String, nullable=False)
      tags = Column(String, nullable=False, default="")
      source_provider = Column(String, nullable=True)
      source_terminal_id = Column(String, nullable=True)
      created_at = Column(DateTime, default=datetime.utcnow)
      updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
      __table_args__ = (UniqueConstraint("key", "scope", "scope_id", name="uq_memory_key_scope"),)
  ```

- [ ] **U1.2** Add migration in `create_tables()` — `Base.metadata.create_all(engine)` already handles it; add the three indexes explicitly:
  ```sql
  CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_metadata (scope, scope_id);
  CREATE INDEX IF NOT EXISTS idx_memory_updated ON memory_metadata (updated_at);
  CREATE INDEX IF NOT EXISTS idx_memory_type ON memory_metadata (memory_type);
  ```

- [ ] **U1.3** Update `MemoryService.store()` to upsert SQLite row alongside wiki file write
  - Phase 1: upsert decision is made by checking `index.md`
  - Phase 2: replace with `SELECT` on `(key, scope, scope_id)` from `memory_metadata`
  - On create: `INSERT` row with `file_path`; on update: `UPDATE updated_at, tags, source_provider, source_terminal_id`

- [ ] **U1.4** Update `MemoryService.recall()` to query SQLite instead of scanning `index.md`
  - Replace file-scan logic with:
    ```python
    WHERE (key LIKE '%{query}%' OR tags LIKE '%{query}%')
    AND scope = ?   # if scope provided
    AND memory_type = ?  # if memory_type provided
    ORDER BY updated_at DESC LIMIT ?
    ```
  - Still read wiki file content from `row.file_path` after query

- [ ] **U1.5** Update `MemoryService.forget()` to delete SQLite row alongside wiki file deletion
  - Phase 1: updates `index.md` manually
  - Phase 2: `DELETE FROM memory_metadata WHERE key=? AND scope=? AND scope_id IS ?`
  - `index.md` is now a derived view — regenerate it from SQLite after deletion (or keep in sync on both writes)

- [ ] **U1.6** Update `cleanup_service.py` cleanup query to use SQLite instead of reading file mtimes
  - Replace mtime-based check with:
    ```sql
    SELECT * FROM memory_metadata
    WHERE (scope = 'session' AND updated_at < datetime('now', '-14 days'))
       OR (memory_type IN ('project','reference') AND updated_at < datetime('now', '-90 days'))
    ```

- [ ] **U1.7** Update token estimate in `index.md` entries — Phase 1 uses `len(content.split()) * 1.3` stub; Phase 2 uses `len(content) / 4` (char-based, more consistent)
  - Store the computed estimate as a new column `token_estimate INTEGER` on `MemoryMetadataModel`
  - Recompute on every `store` call

- [ ] **U1.8** Verify `index.md` stays consistent with SQLite as the source of truth
  - On every `store`, `forget`, or cleanup: regenerate affected `index.md` section from SQLite instead of manually patching lines
  - This eliminates the Phase 1 manual line-patching risk

### Acceptance

- `recall()` uses SQLite query — no `index.md` line scanning in recall path
- `store()` inserts/updates `memory_metadata` row on every call
- `forget()` deletes SQLite row and removes wiki file
- Concurrent stores: WAL mode prevents write contention
- `index.md` always matches `memory_metadata` table (test: insert directly to DB, assert index consistent)
- Migration runs cleanly on a fresh DB and on an existing Phase 1 DB (no wiki files lost)

---

## U2 — SessionEventModel + Event Logging

**Agent**: Backend Developer
**Files**: `src/cli_agent_orchestrator/clients/database.py`, `src/cli_agent_orchestrator/services/memory_service.py`, `src/cli_agent_orchestrator/services/terminal_service.py`

**Context**: Append-only event log enables cross-provider resumption. The context-manager (U9) and `session_context` MCP tool (U5) both consume this log.

### Subtasks

- [ ] **U2.1** Add `SessionEventModel` to `clients/database.py`
  ```python
  class SessionEventModel(Base):
      __tablename__ = "session_events"
      id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
      session_name = Column(String, nullable=False, index=True)
      terminal_id = Column(String, nullable=False)
      provider = Column(String, nullable=False)
      event_type = Column(String, nullable=False)  # task_started | task_completed | handoff_returned | memory_stored | agent_launched
      summary = Column(String, nullable=False, default="")
      metadata_json = Column(String, nullable=False, default="{}")  # arbitrary event data as JSON string
      created_at = Column(DateTime, default=datetime.utcnow)
  ```

- [ ] **U2.2** Add migration for `session_events` table in `create_tables()`

- [ ] **U2.3** Add `log_event(session_name, terminal_id, provider, event_type, summary, metadata)` to `MemoryService` (or a new `EventService` if preferred)
  - Inserts a row into `session_events`
  - Called from `terminal_service.py` at key lifecycle points

- [ ] **U2.4** Wire event logging into terminal lifecycle in `terminal_service.py`
  - `agent_launched` — on terminal creation
  - `task_started` — when first user message is sent
  - `task_completed` — when COMPLETED status detected
  - `handoff_returned` — when `handoff` tool completes
  - `memory_stored` — called from `MemoryService.store()` automatically

- [ ] **U2.5** Add `get_session_timeline(session_name) -> list[SessionEventModel]` method
  - `SELECT * FROM session_events WHERE session_name=? ORDER BY created_at ASC`
  - Used by U5 (`session_context` MCP tool)

### Acceptance

- `session_events` table created in migration
- All 5 event types logged at correct lifecycle points
- `get_session_timeline()` returns ordered list for a session
- Appending events is non-blocking — failure to log must not fail the terminal operation

---

## U3 — memory_forget + memory_consolidate MCP Tools

**Agent**: Integration Specialist
**Files**: `src/cli_agent_orchestrator/mcp_server/server.py`

**Context**: `memory_forget` is internal-only in Phase 1. Phase 2 exposes it to agents and adds `memory_consolidate` for merging duplicate or outdated entries.

### Subtasks

- [ ] **U3.1** Expose `memory_forget` as an MCP tool (was internal-only in Phase 1)
  ```python
  @server.tool()
  async def memory_forget(
      key: str,
      scope: str = "project",
  ) -> dict:
      """Remove a stored memory by key and scope.
      Use this to delete incorrect, outdated, or superseded facts.
      Returns { success, deleted, key, scope }."""
  ```

- [ ] **U3.2** Add `memory_consolidate` MCP tool
  ```python
  @server.tool()
  async def memory_consolidate(
      keys: list[str],
      scope: str = "project",
      new_key: Optional[str] = None,
      new_content: str,
  ) -> dict:
      """Merge two or more memory entries into one.
      Provide the keys to merge and the combined content.
      Original entries are deleted; a new entry is created with new_key (or the first key if omitted).
      Returns { success, merged_from, new_key, scope }."""
  ```
  - Implementation: `store(new_key or keys[0], new_content, ...)` then `forget(k, scope)` for each original key

- [ ] **U3.3** Update agent profile memory instruction in `U7` profiles to mention `memory_forget` and `memory_consolidate`
  - "Use `memory_forget` to remove incorrect or superseded facts. Use `memory_consolidate` to merge duplicates."

### Acceptance

- `memory_forget` callable from MCP client — deletes SQLite row and wiki file
- `memory_consolidate` with 2 keys → one merged file, two originals deleted
- `memory_consolidate` with `new_key=None` → uses first key in list

---

## U4 — extract_session_context() — 5 Providers

**Agent**: Backend Developer
**Files**: `src/cli_agent_orchestrator/providers/base.py`, `src/cli_agent_orchestrator/providers/claude_code.py`, `src/cli_agent_orchestrator/providers/gemini_cli.py`, `src/cli_agent_orchestrator/providers/kiro_cli.py`, `src/cli_agent_orchestrator/providers/codex.py`, `src/cli_agent_orchestrator/providers/copilot_cli.py`

**Context**: Each provider stores session history in its own format. `extract_session_context()` returns a structured summary of what happened in this terminal's session — used by `session_context` MCP tool and the context-manager. Kimi is deferred to Phase 3 (least-structured session format).

### Subtasks

- [ ] **U4.1** Add abstract method to `BaseProvider`
  ```python
  @abstractmethod
  async def extract_session_context(self, terminal_id: str) -> dict:
      """Extract structured context from this provider's session storage.
      Returns: { provider, terminal_id, last_task, key_decisions, open_questions, files_changed }
      Returns empty dict if no session data found."""
  ```

- [ ] **U4.2** Claude Code implementation
  - Source file: `~/.claude/projects/{path}/{uuid}.jsonl`
  - Parse JSONL, extract last N human+assistant turns
  - Identify: last human message (last_task), assistant mentions of decisions/conclusions (key_decisions), any "?" in human messages (open_questions)
  - `files_changed`: scan for tool calls with `type="Write"` or `type="Edit"` in last 20 turns

- [ ] **U4.3** Gemini CLI implementation
  - Source file: `~/.gemini/sessions/{session_id}.json` (or equivalent — confirm actual path during implementation)
  - Extract last task from conversation history

- [ ] **U4.4** Kiro CLI implementation
  - Source file: `~/.kiro/sessions/` (or equivalent — confirm actual path)
  - Extract task summary from last completed response

- [ ] **U4.5** Codex CLI implementation
  - Source file: `~/.codex/history.jsonl`
  - Same JSONL parse approach as Claude Code

- [ ] **U4.6** Copilot CLI implementation
  - Source file: confirm actual path during implementation
  - Minimal implementation acceptable if session file format is underdocumented

- [ ] **U4.7** Add `extract_session_context()` call to `handoff` flow in `mcp_server/server.py`
  - After worker completes, extract context before supervisor continues
  - Append context summary to `session_events` as `task_completed` event

### Acceptance

- `extract_session_context()` returns non-empty dict for a terminal with session history
- Returns empty dict (not exception) if session file does not exist
- Claude Code implementation tested with real JSONL fixture
- Kimi stub raises `NotImplementedError` with clear message ("Kimi session extraction deferred to Phase 3")

---

## U5 — session_context MCP Tool

**Agent**: Integration Specialist
**Files**: `src/cli_agent_orchestrator/mcp_server/server.py`

**Context**: Enables a supervisor (or any agent) to query the full event timeline for a session. Used for cross-provider resumption — "what did the previous worker actually do?"

### Subtasks

- [ ] **U5.1** Add `session_context` MCP tool
  ```python
  @server.tool()
  async def session_context(
      session_name: Optional[str] = None,  # defaults to current session
      limit: int = 20,                      # most recent N events
  ) -> dict:
      """Return the event timeline for a session.
      Includes task_started, task_completed, handoff_returned, memory_stored events.
      Use this to understand what previous agents did before taking over.
      Returns { session_name, events: [{ event_type, terminal_id, provider, summary, created_at }] }."""
  ```
  - Delegates to `memory_service.get_session_timeline(session_name)`
  - `session_name` defaults to `CAO_SESSION_NAME` env var if not provided

- [ ] **U5.2** Add `CAO_SESSION_NAME` env var injection in `clients/tmux.py`
  - Set alongside `CAO_TERMINAL_ID` when creating a tmux session
  - Value: the CAO session name (already available in session creation context)

### Acceptance

- `session_context()` returns ordered event list with no events when session is new
- `session_context()` without `session_name` uses `CAO_SESSION_NAME` from env
- Events appear in correct chronological order

---

## U6 — BM25 Fallback Search

**Agent**: Backend Developer
**Files**: `src/cli_agent_orchestrator/services/memory_service.py`

**Context**: Phase 1 recall matches only against `key` and `tags` fields in SQLite. BM25 adds full-text search over wiki file content — catches queries that don't match the key slug. BM25 is the fallback when SQLite metadata query returns fewer than `limit` results.

### Subtasks

- [ ] **U6.1** Add `rank-bm25` (or `bm25`) to dependencies (`pyproject.toml`)
  - Evaluate: `rank-bm25` is pure Python, no native deps — prefer this

- [ ] **U6.2** Build BM25 index on `recall()` call (no pre-built index in Phase 2)
  - When SQLite query returns < `limit` results: scan remaining wiki files in scope
  - Tokenize content of each wiki file, run BM25 against `query`
  - Merge BM25 results with SQLite results (deduplicate by key), re-sort by score
  - Phase 3 introduces a persistent index; Phase 2 builds on each query (acceptable at <100 files)

- [ ] **U6.3** Add `search_mode` parameter to `recall()`
  ```python
  async def recall(
      self,
      query: Optional[str] = None,
      scope: Optional[str] = None,
      memory_type: Optional[str] = None,
      limit: int = 10,
      search_mode: str = "hybrid",  # "metadata" | "bm25" | "hybrid"
      terminal_context: Optional[dict] = None,
  ) -> list[Memory]:
  ```
  - `"metadata"` — Phase 1 behavior (SQLite only)
  - `"bm25"` — BM25 only over wiki files
  - `"hybrid"` — SQLite first, BM25 to fill remaining slots up to `limit`

- [ ] **U6.4** Update `memory_recall` MCP tool to accept `search_mode` parameter (default `"hybrid"`)

### Acceptance

- `recall(query="pytest", search_mode="bm25")` finds memories where "pytest" appears in content but not in key/tags
- Hybrid mode returns merged results, sorted by relevance + recency
- BM25 search completes in < 500ms for 100 wiki files
- Fallback gracefully if `rank-bm25` not installed: log warning, return metadata-only results

---

## U7 — Cache-Aware Injection

**Agent**: Backend Developer
**Files**: `src/cli_agent_orchestrator/services/terminal_service.py`, provider files for Claude Code and Kiro CLI

**Context**: Phase 1 prepends all memory (static identity + dynamic memories) into the first user message. This defeats prompt caching for providers that support it (Claude Code, Kiro). Phase 2 separates static identity (system prompt / steering file — rarely changes) from dynamic memory (the `<cao-memory>` block — changes every session).

### Subtasks

- [ ] **U7.1** Define "static identity" vs "dynamic memory" boundary
  - Static: agent profile body (instructions, persona, tool list) — changes rarely
  - Dynamic: `<cao-memory>` block (session + project memories) — changes every session

- [ ] **U7.2** Claude Code — move static identity to `--append-system-prompt`
  - Phase 1 already does this for agent identity; verify `<cao-memory>` is NOT in system prompt
  - Dynamic memory: stays in first user message as `<cao-memory>` block (correct Phase 1 behavior — no change needed if already separate)

- [ ] **U7.3** Kiro CLI — move static identity to `.kiro/steering/agent-identity.md`
  - Write agent profile body to the steering file once at terminal creation
  - Dynamic memory: prepended to first user message only (do not add to steering file)
  - Steering file persists across sessions; `<cao-memory>` block does not

- [ ] **U7.4** Gemini CLI — move static identity to `GEMINI.md` (acceptable: static file)
  - Dynamic memory: first user message only
  - Phase 1 injected both into first message; Phase 2 separates them

- [ ] **U7.5** Codex CLI — `-c developer_instructions` carries static identity; `<cao-memory>` stays in first user message
  - Verify Phase 1 implementation already separates these; document if not

- [ ] **U7.6** Validate cache hit rate improvement for Claude Code
  - Integration test: create two terminals with same agent profile → assert system prompt is identical (cache-able), first user message differs (dynamic memory)

### Acceptance

- System prompt / steering file: static, identical across sessions with same agent profile
- First user message: contains `<cao-memory>` block (dynamic, changes per session)
- No provider file backup/restore — static file is written once and stable
- Unit test per provider verifies separation

---

## U8 — Pre-Compaction Flush

**Agent**: Backend Developer
**Files**: `src/cli_agent_orchestrator/utils/terminal.py` (`wait_until_status()`), `src/cli_agent_orchestrator/services/terminal_service.py`

**Context**: Phase 1's PreCompact hook fires only for Claude Code. Phase 2 adds a CAO-side mechanism: during `wait_until_terminal_status()`, poll the provider for context usage percentage and trigger a self-save instruction before the context window fills.

### Subtasks

- [ ] **U8.1** Add optional `get_context_usage_percentage() -> Optional[float]` to `BaseProvider`
  - Returns `None` if provider doesn't expose this (default implementation)
  - Returns float 0.0–1.0 if available

- [ ] **U8.2** Claude Code implementation of `get_context_usage_percentage()`
  - Parse JSONL transcript for `context_usage_percentage` field (present in Claude Code's stop events)
  - Return latest value found

- [ ] **U8.3** Add pre-compaction flush logic in `wait_until_status()` in `utils/terminal.py`
  - On each poll: call `provider.get_context_usage_percentage()`
  - If > 0.85 (85% full): send a self-save instruction via `terminal_service.send_input()`
    - Message: `"IMPORTANT: Context window is nearly full. Use memory_store to save all key findings, decisions, and preferences before continuing."`
  - Guard: only trigger once per terminal session (flag to prevent repeated triggers)

- [ ] **U8.4** Add `flush_threshold` config to settings (`settings_service.py`)
  - Default: 0.85
  - User-configurable: `cao settings set memory.flush_threshold 0.90`

### Acceptance

- Unit test: `get_context_usage_percentage()` returns correct value from Claude Code JSONL fixture
- Integration test: mock provider returns 0.87 → `wait_until_status()` sends flush instruction
- Flush triggers at most once per terminal session
- Non-Claude Code providers return `None` gracefully (no flush triggered)

---

## U9 — Context-Manager Agent

**Agent**: Backend Developer
**Files**: `src/cli_agent_orchestrator/services/terminal_service.py`, `src/cli_agent_orchestrator/agent_store/memory_manager.md` (new profile)

**Context**: The context-manager is a dedicated background agent (runs on the same provider as the supervisor). At each handoff, it reads `index.md` + token estimates, queries the session timeline, and produces a curated injection block — replacing the mechanical scope-precedence selection of Phase 1.

### Subtasks

- [ ] **U9.1** Create `agent_store/memory_manager.md` agent profile
  - Provider: configurable (defaults to supervisor's provider)
  - Instructions: read `index.md`, read `session_context`, select the most relevant memories for the incoming terminal's task, format as `<cao-memory>` block within budget

- [ ] **U9.2** Add `cao launch --memory` flag to start the context-manager terminal
  - Creates a dedicated terminal for the memory-manager agent
  - Registered in the session alongside supervisor and workers

- [ ] **U9.3** Add `get_curated_memory_context(terminal_id, task_description) -> str` to `MemoryService`
  - If context-manager terminal is active: `send_message` to context-manager with task description, await response, return the curated `<cao-memory>` block
  - If context-manager is not active: fall back to `get_memory_context_for_terminal()` (Phase 1 behavior)
  - Timeout: 10 seconds — if context-manager doesn't respond, fall back immediately

- [ ] **U9.4** Wire `get_curated_memory_context()` into terminal initialization in `terminal_service.py`
  - Replace direct call to `get_memory_context_for_terminal()` with `get_curated_memory_context()`

- [ ] **U9.5** Add heartbeat check — context-manager must be IDLE before querying
  - If PROCESSING (busy with another request): fall back to Phase 1 mechanical injection

### Acceptance

- `cao launch --memory` starts the context-manager terminal
- Supervisor terminal creation calls `get_curated_memory_context()` — context-manager responds with curated block
- Fallback to Phase 1 behavior when context-manager is absent or busy
- Context-manager does not inject its own memories (it has no `<cao-memory>` block in its system prompt)

---

## U10 — Tests & Validation

**Agent**: Code Reviewer
**Files**: `test/services/test_memory_service_phase2.py`, `test/providers/test_session_context.py`, `test/services/test_event_logging.py`

### Subtasks

- [ ] **U10.1** `test/services/test_memory_service_phase2.py` — SQLite-backed unit tests
  - `test_store_inserts_sqlite_row` — store → `memory_metadata` row exists
  - `test_recall_uses_sqlite_query` — mock SQLite, assert query called (not file scan)
  - `test_forget_deletes_sqlite_row` — forget → row deleted
  - `test_token_estimate_stored` — store → `token_estimate` column populated
  - `test_bm25_finds_content_match` — store memory, query by word not in key/tags → recall finds it
  - `test_hybrid_recall_merges_results` — metadata match + BM25 match → both returned, deduped
  - `test_consolidate_merges_two_entries` — consolidate → one file, two originals gone

- [ ] **U10.2** `test/services/test_event_logging.py`
  - `test_agent_launched_event` — terminal creation → `agent_launched` event in DB
  - `test_handoff_returned_event` — handoff complete → `handoff_returned` event in DB
  - `test_get_session_timeline_ordered` — insert 3 events → timeline returns in created_at order

- [ ] **U10.3** `test/providers/test_session_context.py`
  - `test_claude_code_extract_context` — JSONL fixture → structured context dict
  - `test_extract_returns_empty_for_missing_file` — no session file → empty dict, no exception
  - `test_kimi_raises_not_implemented` — Kimi provider → `NotImplementedError`

- [ ] **U10.4** `test/services/test_cache_aware_injection.py`
  - `test_static_identity_in_system_prompt` — Claude Code terminal → system prompt matches agent profile
  - `test_dynamic_memory_in_first_user_message` — Claude Code terminal → first user message has `<cao-memory>`, system prompt does not

- [ ] **U10.5** Run full suite and confirm no Phase 1 regressions
  - `uv run pytest test/ --ignore=test/e2e -v`
  - `uv run mypy src/cli_agent_orchestrator/services/memory_service.py src/cli_agent_orchestrator/clients/database.py`

### Acceptance

- All new tests pass
- No regressions in Phase 1 test suite
- mypy clean on memory_service.py and database.py
- BM25 search completes in < 500ms on 100-file benchmark

---

## Build & Test Checklist

Run in order after all units complete:

```bash
# Format
uv run black src/cli_agent_orchestrator/services/memory_service.py \
             src/cli_agent_orchestrator/clients/database.py \
             src/cli_agent_orchestrator/mcp_server/server.py \
             src/cli_agent_orchestrator/providers/base.py

# Types
uv run mypy src/cli_agent_orchestrator/services/memory_service.py \
            src/cli_agent_orchestrator/clients/database.py

# Unit tests (Phase 2 only)
uv run pytest test/services/test_memory_service_phase2.py \
              test/services/test_event_logging.py \
              test/providers/test_session_context.py \
              test/services/test_cache_aware_injection.py -v

# Integration tests
uv run pytest -m integration test/ -v

# Full suite (no regressions)
uv run pytest test/ --ignore=test/e2e -v
```

All must pass before declaring Phase 2 complete.
