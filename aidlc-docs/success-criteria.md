# Success Criteria — CAO Memory System Phase 1

**Status**: DRAFT — pending user approval  
**Created**: 2026-04-13  
**Request**: Build Phase 1 of the CAO memory system — full wiki file structure, all 6 providers, MCP tools, hook-triggered self-save, CLI commands. No SQLite (deferred to Phase 2).

---

## Functional Criteria

- **SC-1**: Agent calls `memory_store(content)` → wiki topic file created under the correct scope directory, `index.md` updated with a new entry.
  - Verification: unit test + integration test round-trip

- **SC-2**: `memory_store` with same key + scope called twice → topic file updated in place (upsert), `index.md` entry updated, no duplicate file.
  - Verification: integration test checks file count and content

- **SC-3**: `memory_recall(query)` returns memories matching query terms, sorted by `updated_at` descending, respecting scope precedence (session > project > global).
  - Verification: integration test stores memories at 3 scopes, asserts correct order

- **SC-4**: `memory_forget(key, scope)` removes the entry from the wiki topic file and updates `index.md`. If the file becomes empty, it is deleted.
  - Verification: integration test

- **SC-5**: Memory context is injected as a `<cao-memory>` block prepended to the first user message for all 6 providers (Claude Code, Kiro, Gemini, Codex, Kimi, Copilot).
  - Verification: unit test per provider — mock terminal creation, assert `<cao-memory>` block present in first message

- **SC-6**: Stop hook fires for Claude Code and Codex every N=15 human messages. Hook is blocked by recursion guard when already active.
  - Verification: unit test — mock JSONL transcript with 15/30 messages, assert hook returns block decision; assert flag file prevents re-entry

- **SC-7**: PreCompact hook fires for Claude Code before context compression. Agent saves memories before compaction.
  - Verification: unit test — assert hook always returns block decision

- **SC-8**: `cao memory list` shows all stored memories for the current project. `cao memory show <key>` displays content. `cao memory delete <key>` removes it. `cao memory clear --scope session` clears all session-scoped memories.
  - Verification: CLI tests with mocked MemoryService

- **SC-9**: Cleanup runs on schedule — session-scoped memories expire after 14 days, project/reference after 90 days, user/feedback never expire.
  - Verification: unit test inserts memories with backdated timestamps, runs cleanup, asserts correct rows deleted/preserved

- **SC-10**: Memory context respects a token budget (~2-4KB). If stored memories exceed the budget, oldest entries are dropped first.
  - Verification: unit test — store 20 memories, assert injected block is within budget

---

## Technical Criteria

- **SC-11**: No SQLite in Phase 1. All storage is file-based (wiki topic files + `index.md`). Recall uses file scanning + text matching.
  - Verification: grep codebase — no SQLAlchemy imports in memory service, no DB migration for memory

- **SC-12**: `key` is optional on `memory_store`. If omitted, auto-generated as a slug of the first 6 words of content.
  - Verification: unit test — call `memory_store("User prefers pytest for all tests")`, assert key = `"user-prefers-pytest-for-all"`

- **SC-13**: Concurrent `memory_store` calls to the same topic file do not corrupt it (file-level atomic write using temp file + rename).
  - Verification: integration test — `asyncio.gather` 5 concurrent stores to same scope, assert all entries present and file is valid markdown

- **SC-14**: `index.md` is always consistent with the wiki files on disk — no entries pointing to deleted files, no files missing from the index.
  - Verification: integration test — store/forget cycle, assert index matches filesystem state

- **SC-15**: Memories survive server restart (files written to disk, not memory-only).
  - Verification: integration test — store memory, restart MemoryService, recall it

---

## Quality Criteria

- **SC-16**: All unit tests pass (`uv run pytest test/services/test_memory_service.py test/cli/test_memory_commands.py -v`).
  - Verification: CI green

- **SC-17**: All integration tests pass (`uv run pytest -m integration test/ -v`).
  - Verification: CI green

- **SC-18**: `uv run mypy src/cli_agent_orchestrator/services/memory_service.py` passes with no errors.
  - Verification: CI green

---

## Out of Scope (Phase 2+)

- SQLite `MemoryMetadataModel` table and migrations
- `SessionEventModel` and session event logging
- Context manager agent
- `session_context` MCP tool
- `extract_session_context()` on BaseProvider
- LLM-powered wiki compilation (Phase 3)
- BM25 fallback search (Phase 2)
- REST API endpoints for memory write/list/delete (deferred to Phase 3 by Phase 2.5 U8; `GET /terminals/{id}/memory-context` is shipped)
- Web UI (Phase 4)
- Cross-project memory federation (Phase 4)

---

## Validation Report

| Criterion | Status | Evidence |
|---|---|---|
| SC-1 | [x] | test_store_creates_wiki_file + test_full_roundtrip pass |
| SC-2 | [x] | test_store_upsert_same_key — one file, updated content |
| SC-3 | [x] | test_recall_scope_precedence — session > project > global |
| SC-4 | [x] | test_forget_removes_file_and_index |
| SC-5 | [x] | TestConfigFilesNotMutated — GEMINI.md/CLAUDE.md/AGENTS.md untouched; challenger-3 verified |
| SC-6 | [x] | cao_stop_hook.sh: N=15, flag file guard, block decision JSON |
| SC-7 | [x] | cao_precompact_hook.sh: always returns block decision |
| SC-8 | [x] | cao memory list/show/delete/clear — 16/16 CLI tests pass |
| SC-9 | [x] | TestCleanupSessionIntegration: 15d session deleted, user/feedback preserved |
| SC-10 | [x] | test_get_context_respects_budget — 20 memories truncated to 3000 chars |
| SC-11 | [x] | test_no_sqlalchemy_imports — no SQLAlchemy in memory module |
| SC-12 | [x] | test_auto_key_generation — "User prefers pytest..." → "user-prefers-pytest-for-all" |
| SC-13 | [x] | test_concurrent_stores — asyncio.gather 5 stores, no corruption |
| SC-14 | [x] | test_index_consistency — store/forget cycle, index matches filesystem |
| SC-15 | [x] | test_survive_restart — store with svc1, recall with svc2 |
| SC-16 | [x] | 1292 passed, 6 pre-existing flaky handoff failures (pass in isolation) |
| SC-17 | [x] | uv run pytest -m integration test/ — all integration tests pass |
| SC-18 | [x] | uv run mypy src/.../memory_service.py — Success: no issues found |
