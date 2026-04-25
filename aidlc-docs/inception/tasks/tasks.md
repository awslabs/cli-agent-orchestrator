# Tasks — CAO Memory System Phase 1

**Generated**: 2026-04-13  
**Scope**: Phase 1 — full wiki file structure, all 6 providers, MCP tools, hooks, CLI. No SQLite.  
**Design reference**: `MEMORY_SYSTEM_DESIGN.md`  
**Success criteria**: `aidlc-docs/success-criteria.md`

---

## Unit Map

| ID | Unit | Agent | Depends On | SC |
|---|---|---|---|---|
| U1 | MemoryService Core | Backend Developer | — | SC-1,2,3,4,10,11,12,13,14,15 |
| U2 | MCP Tools | Integration Specialist | U1 | SC-1,2,3,4 |
| U3 | Provider Injection | Backend Developer | U1 | SC-5,10 |
| U4 | Hook-Triggered Self-Save | Backend Developer | — | SC-6,7 |
| U5 | Cleanup Integration | Backend Developer | U1 | SC-9 |
| U6 | CLI Commands | Backend Developer | U1 | SC-8 |
| U7 | Agent Profile Updates | Backend Developer | U1 | SC-5 |
| U8 | Tests & Validation | Code Reviewer | U1–U7 | SC-16,17,18 |

---

## U1 — MemoryService Core

**Agent**: Backend Developer  
**Files**: `src/cli_agent_orchestrator/models/memory.py`, `src/cli_agent_orchestrator/services/memory_service.py`, `src/cli_agent_orchestrator/constants.py`

### Subtasks

- [ ] **U1.1** Add memory directory constants to `constants.py`
  ```
  MEMORY_BASE_DIR = Path.home() / ".aws" / "cli-agent-orchestrator" / "memory"
  # {MEMORY_BASE_DIR}/{project_hash}/wiki/{scope}/{key}.md
  # {MEMORY_BASE_DIR}/memory.db  ← Phase 2, do not create
  ```

- [ ] **U1.2** Create `Memory` Pydantic model in `models/memory.py`
  - Fields: `id`, `key`, `memory_type`, `scope`, `scope_id`, `file_path`, `tags`, `source_provider`, `source_terminal_id`, `created_at`, `updated_at`, `content`
  - No SQLAlchemy — plain Pydantic `BaseModel`
  - Validate `scope` ∈ {global, project, session, agent}
  - Validate `memory_type` ∈ {user, feedback, project, reference}

- [ ] **U1.3** Implement `resolve_scope_id(scope, terminal_context) -> Optional[str]`
  - `global` → `None`
  - `project` → SHA256[:12] of `realpath(terminal.working_directory)`
  - `session` → `terminal.session_name`
  - `agent` → `terminal.agent_profile`

- [ ] **U1.4** Implement `auto_generate_key(content: str) -> str`
  - Slug of first 6 words: lowercase, spaces→hyphens, strip punctuation
  - `"User prefers pytest for all tests"` → `"user-prefers-pytest-for-all"`
  - Truncate to 60 chars

- [ ] **U1.5** Implement wiki file path resolution
  - `get_wiki_path(scope, scope_id, key) -> Path`
  - `get_index_path(scope_id) -> Path`
  - Create directories on first use (parents=True, exist_ok=True)

- [ ] **U1.6** Implement `store(content, scope, memory_type, key, tags, terminal_context) -> Memory`
  - Resolve scope_id, generate key if not provided
  - Check if `(key, scope, scope_id)` already exists on disk (read index.md)
  - If exists: update topic file (append new timestamped entry), update index.md `updated_at`
  - If new: create topic file with header + first entry, append line to index.md
  - Atomic write: write to `.{key}.tmp` then `os.replace()` to prevent corruption
  - Return `Memory` with content loaded

- [ ] **U1.7** Wiki topic file format
  ```markdown
  # {key}
  <!-- scope: {scope} | type: {memory_type} | tags: {tags} -->

  ## {ISO8601 timestamp}
  {content}
  ```
  Each `store` appends a new `## timestamp\ncontent` section. Phase 2 LLM compilation replaces append.

- [ ] **U1.8** index.md format and maintenance
  ```markdown
  # CAO Memory Index
  <!-- Updated: {timestamp} -->

  ## global
  - [{key}](global/{key}.md) — type:{memory_type} tags:{tags} ~{est_tokens}tok updated:{timestamp}

  ## project
  - ...
  ```
  - `_update_index(scope_id, key, action)` — action = "add" | "update" | "remove"
  - Token estimate: `len(content.split()) * 1.3` (rough approximation, Phase 2 makes this precise)

- [ ] **U1.9** Implement `recall(query, scope, memory_type, limit) -> list[Memory]`
  - Read index.md for the resolved scope_id(s)
  - Filter entries by scope (if provided) and memory_type (if provided)
  - For each candidate file: check if `query` terms appear in the file (case-insensitive)
  - Sort by `updated_at` descending
  - Return up to `limit` results with `content` populated
  - Scope precedence when no scope specified: session + project + global all returned, session first

- [ ] **U1.10** Implement `forget(key, scope, terminal_context) -> bool`
  - Find wiki file for (key, scope, scope_id)
  - If file contains only one entry: delete the file
  - If multiple entries: this is Phase 1 — delete the whole file (entries are not individually addressable yet)
  - Remove entry from index.md
  - Return True if found and deleted, False if not found

- [ ] **U1.11** Implement `get_memory_context_for_terminal(terminal_id, budget_chars=3000) -> str`
  - Resolve scope_id for project and session from terminal context
  - Load memories in precedence order: session → project → global
  - Fill up to `budget_chars`, drop oldest entries first if over budget
  - Return formatted `<cao-memory>\n...\n</cao-memory>` block
  - Return empty string if no memories exist

### Acceptance

- `store` + `recall` round-trip returns stored content (SC-1)
- Double `store` with same key does not create duplicate file (SC-2)
- `recall` respects scope precedence (SC-3)
- `forget` removes file + index entry (SC-4)
- No SQLAlchemy imports anywhere in memory service (SC-11)
- Auto-key generation slug matches expected format (SC-12)
- Concurrent stores do not corrupt file (SC-13)
- index.md stays consistent after store/forget cycle (SC-14)

---

## U2 — MCP Tools

**Agent**: Integration Specialist  
**Files**: `src/cli_agent_orchestrator/mcp_server/server.py`

### Subtasks

- [ ] **U2.1** Add `memory_store` MCP tool
  ```python
  @server.tool()
  async def memory_store(
      content: str,
      scope: str = "project",        # global | project | session | agent
      memory_type: str = "project",  # user | feedback | project | reference
      key: Optional[str] = None,     # auto-generated if omitted
      tags: Optional[str] = None,    # comma-separated
  ) -> dict:
      """Store a persistent memory. key is optional — auto-generated from content if omitted."""
  ```
  - Resolve calling terminal from MCP context (`CAO_TERMINAL_ID` env)
  - Delegate to `memory_service.store()`
  - Return `{ success, key, scope, scope_id, file_path, action }`  where action = "created" | "updated"

- [ ] **U2.2** Add `memory_recall` MCP tool
  ```python
  @server.tool()
  async def memory_recall(
      query: Optional[str] = None,
      scope: Optional[str] = None,
      memory_type: Optional[str] = None,
      limit: int = 10,
  ) -> dict:
      """Retrieve memories matching a query. Returns content from wiki files."""
  ```
  - Delegate to `memory_service.recall()`
  - Return `{ memories: [{ key, content, memory_type, scope, tags, file_path, updated_at }] }`

- [ ] **U2.3** Add `memory_forget` MCP tool
  ```python
  @server.tool()
  async def memory_forget(
      key: str,
      scope: str = "project",
  ) -> dict:
      """Remove a memory by key and scope."""
  ```
  - Delegate to `memory_service.forget()`
  - Return `{ success, deleted, key, scope }`

- [ ] **U2.4** Add MCP tool descriptions to agent profile instruction block
  - Each tool docstring should be clear enough that agents use them correctly without needing documentation

### Acceptance

- All three tools registered and callable from MCP client
- `memory_store` auto-generates key correctly
- `memory_recall` returns structured list with content populated
- `memory_forget` deletes and returns confirmation

---

## U3 — Provider Injection

**Agent**: Backend Developer  
**Files**: `src/cli_agent_orchestrator/services/terminal_service.py`, `src/cli_agent_orchestrator/providers/base.py` and all 6 provider files

### Subtasks

- [ ] **U3.1** Add `inject_memory_context(first_message: str, terminal_id: str) -> str` to `terminal_service.py`
  - Call `memory_service.get_memory_context_for_terminal(terminal_id)`
  - If non-empty: prepend `<cao-memory>\n{context}\n</cao-memory>\n\n` to `first_message`
  - Return (possibly unchanged) message
  - This is stateless — no file mutation, no backup/restore

- [ ] **U3.2** Hook injection into the first user message sent to each terminal
  - Identify the "first user message" send point per provider in `initialize()` or `send_input()`
  - Call `inject_memory_context()` at that point
  - The injected block is invisible to subsequent messages (one-time prepend)

- [ ] **U3.3** Claude Code — `--append-system-prompt` carries agent identity; `<cao-memory>` goes in first user message
  - Do NOT modify CLAUDE.md or any project files

- [ ] **U3.4** Kiro CLI — prepend `<cao-memory>` block to first user message
  - Do NOT modify `.kiro/steering/*.md` for memory injection

- [ ] **U3.5** Gemini CLI — prepend `<cao-memory>` block to first user message
  - Do NOT write to GEMINI.md for memory injection (avoids backup/restore fragility)

- [ ] **U3.6** Codex CLI — prepend `<cao-memory>` block to first user message
  - The `-c developer_instructions` flag carries agent identity; memory goes in user message

- [ ] **U3.7** Kimi CLI — prepend `<cao-memory>` block to first user message
  - Do NOT append to AGENTS.md (avoids 32KB budget risk)

- [ ] **U3.8** Copilot CLI — prepend `<cao-memory>` block to first user message
  - Do NOT modify `.github/copilot-instructions.md`

- [ ] **U3.9** Token budget enforcement in `get_memory_context_for_terminal()`
  - Default budget: 3000 chars (~750 tokens)
  - If memories exceed budget: include most recently updated first, truncate at budget
  - Log a warning if truncation occurs

### Acceptance

- Unit test per provider: mock terminal creation, assert `<cao-memory>` block is in first sent message
- No provider file (GEMINI.md, AGENTS.md, CLAUDE.md, steering files) is mutated (SC-5)
- Token budget respected (SC-10)

---

## U4 — Hook-Triggered Self-Save

**Agent**: Backend Developer  
**Files**: `src/cli_agent_orchestrator/hooks/cao_stop_hook.sh`, `src/cli_agent_orchestrator/hooks/cao_precompact_hook.sh`, hook registration logic

### Subtasks

- [ ] **U4.1** Create `cao_stop_hook.sh`
  ```bash
  #!/bin/bash
  # CAO Stop Hook — fires every N human messages, triggers agent self-save
  HOOK_FLAG="/tmp/cao_stop_hook_active_${CAO_TERMINAL_ID}"

  # Recursion guard
  if [ -f "$HOOK_FLAG" ]; then
    exit 0
  fi

  # Count human messages in transcript
  TRANSCRIPT=$(find ~/.claude/projects -name "*.jsonl" -newer /tmp/cao_session_start 2>/dev/null | tail -1)
  if [ -z "$TRANSCRIPT" ]; then exit 0; fi
  MSG_COUNT=$(grep -c '"type":"human"' "$TRANSCRIPT" 2>/dev/null || echo 0)

  # Fire every N=15 messages
  N=15
  if [ $((MSG_COUNT % N)) -eq 0 ] && [ "$MSG_COUNT" -gt 0 ]; then
    touch "$HOOK_FLAG"
    echo '{"decision":"block","reason":"AUTO-SAVE checkpoint. Distill key findings into 1-2 sentences each. Store decisions, preferences, and conclusions — not conversation. Organize into appropriate categories. Continue conversation after saving."}'
    # Flag cleaned up by agent after save completes
  fi
  ```

- [ ] **U4.2** Create `cao_precompact_hook.sh`
  ```bash
  #!/bin/bash
  # CAO PreCompact Hook — always fires before context compression
  echo '{"decision":"block","reason":"EMERGENCY SAVE before context compression. Save all key findings, decisions, and facts via memory_store before compaction summarizes and loses detail. This is your last chance before the context window shrinks."}'
  ```

- [ ] **U4.3** Hook registration for Claude Code
  - On terminal creation for Claude Code provider: write/merge into `.claude/settings.local.json`
  ```json
  {
    "hooks": {
      "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "~/.aws/cli-agent-orchestrator/hooks/cao_stop_hook.sh"}]}],
      "PreCompact": [{"matcher": "", "hooks": [{"type": "command", "command": "~/.aws/cli-agent-orchestrator/hooks/cao_precompact_hook.sh"}]}]
    }
  }
  ```
  - Merge with existing hooks if file already exists — do not overwrite user's own hooks

- [ ] **U4.4** Hook registration for Codex
  - On terminal creation for Codex provider: write/merge into `.codex/hooks.json`
  ```json
  { "stop": "~/.aws/cli-agent-orchestrator/hooks/cao_stop_hook.sh" }
  ```

- [ ] **U4.5** Hook script installation on `cao-server` startup
  - Copy hook scripts to `~/.aws/cli-agent-orchestrator/hooks/` and `chmod +x`
  - Idempotent — safe to run multiple times

- [ ] **U4.6** Flag file cleanup
  - After the agent completes its save response, the stop hook flag file should be removed
  - Investigate if Claude Code / Codex provide a post-response hook, or use a cleanup cron on startup

### Acceptance

- Unit test: mock JSONL transcript with 15 messages → hook returns block decision (SC-6)
- Unit test: flag file present → hook exits 0 (recursion guard) (SC-6)
- Unit test: PreCompact hook always returns block decision (SC-7)
- Hook scripts are executable and installed on server startup

---

## U5 — Cleanup Integration

**Agent**: Backend Developer  
**Files**: `src/cli_agent_orchestrator/services/cleanup_service.py`

### Subtasks

- [ ] **U5.1** Add `cleanup_expired_memories()` method to `cleanup_service.py`

- [ ] **U5.2** Implement tiered retention policy
  ```python
  RETENTION_POLICY = {
      "user":      None,    # indefinite
      "feedback":  None,    # indefinite
      "project":   90,      # days
      "reference": 90,      # days
      # session-scoped memories (any type): 14 days
  }
  SESSION_SCOPE_RETENTION_DAYS = 14
  ```

- [ ] **U5.3** Expiry check logic
  - Read `updated_at` from the wiki file header comment or last entry timestamp
  - Check `scope` — if session-scoped, apply 14-day retention regardless of type
  - If expired: call `memory_service.forget(key, scope)` to remove file + update index.md

- [ ] **U5.4** Wire into existing cleanup schedule
  - `cleanup_expired_memories()` called from the existing periodic cleanup task in `api/main.py`
  - Default run interval: daily (matches existing cleanup frequency)

- [ ] **U5.5** Cleanup is idempotent — safe to run multiple times without double-deleting

### Acceptance

- Unit test: session-scoped memory with `updated_at` = 15 days ago → deleted (SC-9)
- Unit test: `user` memory 200 days old → preserved (SC-9)
- Unit test: `project` memory 91 days old → deleted (SC-9)
- Cleanup does not crash if memory files have already been manually deleted

---

## U6 — CLI Commands

**Agent**: Backend Developer  
**Files**: `src/cli_agent_orchestrator/cli/commands/memory.py`, `src/cli_agent_orchestrator/cli/main.py`

### Subtasks

- [ ] **U6.1** Create `cli/commands/memory.py` with a `memory` Click group

- [ ] **U6.2** `cao memory list [--scope <scope>] [--type <type>]`
  - List all stored memories for current project
  - Output: table with key, scope, type, tags, updated_at
  - Default: shows project + global scopes

- [ ] **U6.3** `cao memory show <key> [--scope <scope>]`
  - Display full content of a memory
  - Output: rendered markdown

- [ ] **U6.4** `cao memory delete <key> [--scope <scope>]`
  - Remove memory by key
  - Confirm prompt: "Delete memory '{key}'? [y/N]"
  - Calls `memory_service.forget()`

- [ ] **U6.5** `cao memory clear --scope <scope>`
  - Remove all memories for a given scope (requires `--scope`)
  - Confirm prompt: "Clear all {scope}-scoped memories? [y/N]"
  - Safety: refuse to run without explicit `--scope` flag

- [ ] **U6.6** Register `memory` group in `cli/main.py`

### Acceptance

- `cao memory list` shows stored memories (SC-8)
- `cao memory show <key>` displays content (SC-8)
- `cao memory delete <key>` removes memory with confirmation (SC-8)
- `cao memory clear --scope session` clears session memories with confirmation (SC-8)
- `cao memory clear` without `--scope` prints error and exits (safety guard)

---

## U7 — Agent Profile Updates

**Agent**: Backend Developer  
**Files**: `src/cli_agent_orchestrator/agent_store/code_supervisor.md`, `src/cli_agent_orchestrator/agent_store/developer.md`, `src/cli_agent_orchestrator/agent_store/reviewer.md`

### Subtasks

- [ ] **U7.1** Add memory instruction section to each built-in agent profile

  Append to the profile body:
  ```markdown
  ## Memory

  When you discover something worth remembering — user preferences, project conventions,
  important decisions, recurring corrections — store it immediately using `memory_store`.
  Keep each memory to 1–2 sentences. Store decisions and conclusions, not conversation.
  Use `memory_recall` to check if you already know something before asking the user.
  ```

- [ ] **U7.2** Ensure the instruction does not conflict with provider-specific memory instructions (e.g., Claude Code's own MEMORY.md guidance)
  - The `memory_store` tool is CAO's cross-provider system; Claude Code's native MEMORY.md is separate
  - Instruction should make clear this is the CAO tool, not the native system

### Acceptance

- All 3 built-in profiles contain memory instruction section
- Instruction is provider-agnostic (no provider-specific references)

---

## U8 — Tests & Validation

**Agent**: Code Reviewer  
**Files**: `test/services/test_memory_service.py`, `test/cli/test_memory_commands.py`, `test/providers/test_memory_injection.py`

### Subtasks

- [ ] **U8.1** `test/services/test_memory_service.py` — unit tests (mock filesystem with `tmp_path`)
  - `test_store_creates_wiki_file` — store → file exists, content correct
  - `test_store_updates_index_md` — store → index.md contains entry
  - `test_store_upsert_same_key` — double store → one file, updated content (SC-2)
  - `test_auto_key_generation` — no key → slug from first 6 words (SC-12)
  - `test_recall_scope_precedence` — session > project > global ordering (SC-3)
  - `test_recall_query_matching` — query filters to relevant files
  - `test_forget_removes_file_and_index` — forget → file deleted, index updated (SC-4)
  - `test_forget_nonexistent_key_returns_false` — graceful no-op
  - `test_get_context_respects_budget` — 20 memories → block under budget limit (SC-10)
  - `test_cleanup_session_14_days` — session memory 15d old → deleted (SC-9)
  - `test_cleanup_preserves_user_feedback` — user/feedback any age → preserved (SC-9)
  - `test_survive_restart` — write file, recreate MemoryService, recall succeeds (SC-15)

- [ ] **U8.2** `test/services/test_memory_service.py` — integration tests (`@pytest.mark.integration`, real `tmp_path`)
  - `test_full_roundtrip` — store → recall, content matches (SC-1)
  - `test_concurrent_stores` — `asyncio.gather` 5 stores to same scope → no corruption (SC-13)
  - `test_index_consistency` — store/forget cycle → index matches filesystem (SC-14)
  - `test_no_sqlalchemy_imports` — assert no SQLAlchemy in memory module (SC-11)

- [ ] **U8.3** `test/cli/test_memory_commands.py` — CLI command tests (mock MemoryService)
  - `test_memory_list` — runs, formats output
  - `test_memory_show` — finds key, displays content
  - `test_memory_delete_with_confirmation` — prompts, deletes
  - `test_memory_clear_requires_scope` — errors without `--scope`

- [ ] **U8.4** `test/providers/test_memory_injection.py` — provider injection tests
  - `test_claude_code_injection` — first message contains `<cao-memory>` block
  - `test_kiro_injection` — same
  - `test_gemini_injection` — same, GEMINI.md not modified
  - `test_no_injection_when_empty` — no memories → no `<cao-memory>` block

- [ ] **U8.5** Run full test suite and fix any failures
  - `uv run pytest test/ --ignore=test/e2e -v` (SC-16)
  - `uv run pytest -m integration test/ -v` (SC-17)
  - `uv run mypy src/cli_agent_orchestrator/services/memory_service.py` (SC-18)

### Acceptance

- All tests listed above pass
- No regressions in existing test suite
- mypy clean on memory_service.py

---

## Build & Test Checklist

Run in order after all units complete:

```bash
# Format
uv run black src/cli_agent_orchestrator/services/memory_service.py \
             src/cli_agent_orchestrator/models/memory.py \
             src/cli_agent_orchestrator/mcp_server/server.py \
             src/cli_agent_orchestrator/cli/commands/memory.py

# Types
uv run mypy src/cli_agent_orchestrator/services/memory_service.py

# Unit tests
uv run pytest test/services/test_memory_service.py test/cli/test_memory_commands.py test/providers/test_memory_injection.py -v

# Integration tests
uv run pytest -m integration test/ -v

# Full suite (no regressions)
uv run pytest test/ --ignore=test/e2e -v
```

All must pass before declaring Phase 1 complete.
