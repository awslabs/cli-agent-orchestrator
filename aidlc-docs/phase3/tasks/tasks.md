# Tasks — CAO Memory System Phase 3

**Generated**: 2026-04-14
**Scope**: Phase 3 — LLM-powered wiki compilation (Karpathy pattern), cross-references, wiki lint, 3-factor scoring, daily audit log, Kimi session extraction.
**Design reference**: `aidlc-docs/MEMORY_SYSTEM_DESIGN.md` (Phase 3 Design Notes)
**Prerequisite**: Phase 2 complete — SQLite, context-manager, BM25 search, event log all operational.

---

## Unit Map

| ID | Unit | Agent | Depends On | Notes |
|---|---|---|---|---|
| U1 | LLM Wiki Compilation | Backend Developer | — | Replaces Phase 1 append with merge-into-article |
| U2 | Cross-References | Backend Developer | U1 | `## See Also` sections, multi-hop context |
| U3 | Wiki Lint | Backend Developer | U1,U2 | Contradiction detection, stale claims, orphan pages |
| U4 | 3-Factor Scoring | Backend Developer | — | BM25 + recency + usage-frequency fallback |
| U5 | Daily Audit Log | Backend Developer | — | Append-only markdown log at `logs/memory/YYYY-MM-DD.md` |
| U6 | extract_session_context() — Kimi | Backend Developer | — | Completes all 6 providers |
| U7 | Tests & Validation | Code Reviewer | U1–U6 | Unit + integration + mypy |

---

## U1 — LLM Wiki Compilation

**Agent**: Backend Developer
**Files**: `src/cli_agent_orchestrator/services/memory_service.py`, `src/cli_agent_orchestrator/services/wiki_compiler.py` (new)

**Context**: Phase 1 and Phase 2 append raw timestamped entries to each topic file. Over time, a topic file becomes a log of disconnected observations rather than a coherent article. Phase 3 replaces the append step with an LLM-powered compilation: each new fact is merged into the existing article, updating context, resolving internal contradictions, and writing connected prose. This is the Karpathy "knowledge wiki" pattern.

### Subtasks

- [ ] **U1.1** Create `services/wiki_compiler.py`
  - Single public method: `async def compile(topic_file_path: Path, new_entry: str) -> str`
  - Reads current wiki file content
  - Calls the active LLM provider (via CAO's own supervisor terminal or a dedicated compile agent) with a compilation prompt
  - Returns updated article content (does not write file — caller writes)

- [ ] **U1.2** Compilation prompt template
  ```
  You are maintaining a knowledge wiki. The existing article is below.
  A new observation has been added. Merge the new observation into the article:
  - Update existing sections if the new fact changes or refines them
  - Add new sections if the fact covers new ground
  - Remove or strikethrough claims that are directly contradicted
  - Keep the article concise — prefer 200–400 words per topic file
  - Do not add bullet-point logs — write connected prose or structured sections
  - Preserve the markdown header and frontmatter comment

  EXISTING ARTICLE:
  {existing_content}

  NEW OBSERVATION:
  {new_entry}

  Return the updated article only, no commentary.
  ```

- [ ] **U1.3** Update `MemoryService.store()` to call `wiki_compiler.compile()` instead of appending
  - If compilation succeeds: write compiled article to wiki file (atomic temp+rename)
  - If compilation fails or times out (> 15s): fall back to Phase 1 append behavior, log warning
  - This fallback ensures `store()` is never blocked by LLM compilation failure

- [ ] **U1.4** Add `compile_mode` config to settings (`settings_service.py`)
  - `"llm"` — Phase 3 compilation (default when Phase 3 is active)
  - `"append"` — Phase 1/2 behavior (fallback, user-configurable)
  - `cao settings set memory.compile_mode append` to revert

- [ ] **U1.5** Add `last_compiled_at` field to `MemoryMetadataModel` in `database.py`
  - Records when LLM compilation last ran for this entry
  - `NULL` = entry has never been compiled (was created in Phase 1/2 with append)

- [ ] **U1.6** Backfill compilation for existing Phase 1/2 topic files
  - `cao memory compile --scope project` — runs compilation for all uncompiled entries in scope
  - Background task: process one file per minute to avoid LLM rate limits
  - Idempotent — safe to run multiple times

### Acceptance

- `store("prefer-pytest", "User confirmed pytest-asyncio for async tests")` → existing article updated, not appended
- Compiled article is coherent prose, not timestamped log
- Fallback to append if compilation times out or errors
- `compile_mode=append` bypasses LLM entirely (regression-free rollback)
- `cao memory compile --scope project` backfills 10 entries in test fixture

---

## U2 — Cross-References

**Agent**: Backend Developer
**Files**: `src/cli_agent_orchestrator/services/wiki_compiler.py`, `src/cli_agent_orchestrator/services/memory_service.py`

**Context**: Related topic files should reference each other so the context-manager can follow links for multi-hop reasoning. For example, `auth-issues.md` links to `testing-conventions.md` because auth bugs should be tested according to the project's conventions.

### Subtasks

- [ ] **U2.1** Add `## See Also` section generation to `wiki_compiler.py`
  - After compiling an article, run a second LLM pass to identify related topics
  - Prompt: "Given the topic files listed in index.md, which 1–3 are most relevant to this article? List their keys."
  - Append or update `## See Also` section at the bottom of the article:
    ```markdown
    ## See Also
    - [testing-conventions](../project/testing-conventions.md)
    - [user-preferences](../global/user-preferences.md)
    ```

- [ ] **U2.2** Store cross-reference data in SQLite
  - Add `related_keys` column (`TEXT`, comma-separated) to `MemoryMetadataModel`
  - Populated during compilation; used by context-manager to fetch related articles

- [ ] **U2.3** Update `get_memory_context_for_terminal()` to follow one level of cross-references
  - After loading primary memories for a terminal, follow `related_keys` for each result
  - Add related articles to the context block if budget allows, labeled `[related]`
  - Prevent cycles: track visited keys

- [ ] **U2.4** Update `recall()` to optionally return related articles
  - Add `include_related: bool = False` parameter
  - When `True`: for each result, also return articles linked in `## See Also`

### Acceptance

- Compiled article for `auth-issues.md` includes `## See Also` with at least one related article
- `related_keys` column populated in SQLite after compilation
- `get_memory_context_for_terminal()` with cross-references stays within token budget (related articles fill remaining budget, not over it)
- No infinite loops in cross-reference traversal

---

## U3 — Wiki Lint

**Agent**: Backend Developer
**Files**: `src/cli_agent_orchestrator/services/wiki_lint.py` (new), `src/cli_agent_orchestrator/services/flow_service.py`

**Context**: As the wiki grows, inconsistencies accumulate. Lint detects three classes of problems: contradictions (two articles assert conflicting facts), stale claims (references to code that no longer exists), and orphan pages (wiki files on disk not listed in `index.md`). Problems are surfaced for agent review, not auto-resolved (auto-resolution is Phase 4).

### Subtasks

- [ ] **U3.1** Create `services/wiki_lint.py`
  - `async def run_lint(project_hash: str) -> list[LintIssue]`
  - `LintIssue` dataclass: `{ issue_type, key, related_key, description, severity }`
  - `issue_type` ∈ {`contradiction`, `stale_claim`, `orphan_page`}

- [ ] **U3.2** Contradiction detection
  - For each pair of topic files sharing a tag: run LLM comparison
  - Prompt: "Do these two articles contradict each other? If so, state the contradiction in one sentence. If not, say NO."
  - Only compare files sharing a tag (reduces pairs from O(n²) to manageable)
  - Store detected contradictions as `LintIssue(issue_type="contradiction")`

- [ ] **U3.3** Stale claim detection
  - Scan wiki articles for file paths, function names, and class names mentioned in code blocks or inline code
  - Check if referenced path exists on disk (for file references)
  - Check if referenced symbol appears in any tracked source file (via grep)
  - Flag missing references as `LintIssue(issue_type="stale_claim")`

- [ ] **U3.4** Orphan page detection
  - Walk all `.md` files under `{memory_base}/{project_hash}/wiki/`
  - Cross-reference against `index.md` entries and `memory_metadata` rows
  - Files missing from both → `LintIssue(issue_type="orphan_page", severity="warning")`

- [ ] **U3.5** Wire lint into daily Flow schedule
  - Add `memory_lint` step to the daily Flow in `flow_service.py`
  - Default: runs daily at 03:00 local time
  - Write results to `~/.aws/cli-agent-orchestrator/logs/lint/YYYY-MM-DD.md`

- [ ] **U3.6** Add `cao memory lint [--scope <scope>]` CLI command
  - Runs lint on demand and prints results as a table
  - Output columns: severity, issue_type, key, description
  - Exit code 1 if any ERROR-severity issues found (allows CI integration)

- [ ] **U3.7** Expose lint results to agents via `memory_recall`
  - Add `memory_type="lint_issue"` entries for each unresolved issue
  - Context-manager can see outstanding lint issues and prioritize them for agent review

### Acceptance

- Contradiction lint: store two articles with conflicting claims → lint identifies contradiction
- Orphan page lint: create wiki file without adding to `index.md` → lint flags it
- Stale claim lint: article references deleted file path → lint flags it
- `cao memory lint` exits 0 with no issues on a clean wiki
- Daily Flow schedule runs lint and writes to log
- Lint does not crash on empty wiki

---

## U4 — 3-Factor Scoring

**Agent**: Backend Developer
**Files**: `src/cli_agent_orchestrator/services/memory_service.py`

**Context**: Phase 2 adds BM25 search. Phase 3 introduces a composite scoring function for the fallback (when the context-manager is not active): BM25 relevance (50%) + recency decay (30%) + usage-frequency (20%). This replaces the Phase 1/2 simple `ORDER BY updated_at DESC`.

### Subtasks

- [ ] **U4.1** Add `access_count INTEGER DEFAULT 0` column to `MemoryMetadataModel`
  - Increment on every `recall()` hit for that row
  - Used as the "usage-frequency" factor

- [ ] **U4.2** Add `last_accessed_at DATETIME` column to `MemoryMetadataModel`
  - Updated on every `recall()` hit
  - Separate from `updated_at` (which tracks write time)

- [ ] **U4.3** Implement composite scoring function
  ```python
  def score_memory(bm25_score: float, updated_at: datetime, access_count: int) -> float:
      # Recency decay: full score if updated today, halves every 30 days
      days_old = (datetime.utcnow() - updated_at).days
      recency = 1.0 / (1.0 + days_old / 30.0)
      # Usage frequency: log-scale to prevent runaway accumulation
      usage = math.log1p(access_count) / math.log1p(100)  # normalize to ~0–1 at 100 accesses
      # Weighted sum
      return 0.50 * bm25_score + 0.30 * recency + 0.20 * usage
  ```

- [ ] **U4.4** Apply composite scoring in `recall()` when `search_mode="hybrid"` (Phase 2+)
  - Replace `ORDER BY updated_at DESC` with composite score sort
  - Pure metadata queries (no BM25 term): set `bm25_score=0.0`, sort by recency + usage only

- [ ] **U4.5** Add `sort_by` parameter to `memory_recall` MCP tool
  - `"score"` — composite 3-factor (Phase 3 default)
  - `"recency"` — Phase 1/2 behavior
  - `"usage"` — most accessed first

- [ ] **U4.6** Increment `access_count` and update `last_accessed_at` on each `recall()` hit
  - Batch update: one `UPDATE` call for all returned rows after recall completes
  - Non-blocking: use background task if update latency is noticeable

### Acceptance

- `access_count` increments on each `recall()` hit
- Composite score puts recently-accessed + recently-updated entries above stale, unaccessed entries
- `sort_by="recency"` reproduces Phase 1/2 ordering (regression test)
- Score function unit test: verify weights sum to 1.0, output bounded 0.0–1.0

---

## U5 — Daily Audit Log

**Agent**: Backend Developer
**Files**: `src/cli_agent_orchestrator/services/cleanup_service.py` (or `memory_service.py`)

**Context**: The event log in SQLite (`session_events`) is queryable but not human-readable without tooling. Phase 3 adds a parallel append-only markdown log for debugging without DB access. Eases post-incident review and manual auditing.

### Subtasks

- [ ] **U5.1** Add `write_audit_log_entry(event_type, terminal_id, provider, summary)` function
  - Target file: `~/.aws/cli-agent-orchestrator/logs/memory/YYYY-MM-DD.md`
  - Create file on first write for the day; append on subsequent writes
  - Entry format:
    ```markdown
    - 14:23:11Z [kiro_cli/term-abc123] memory_stored: user-prefers-pytest (global) — "User confirmed pytest over unittest"
    - 14:25:03Z [claude_code/term-xyz789] task_completed: Refactored auth middleware
    ```

- [ ] **U5.2** Call `write_audit_log_entry()` from `MemoryService.store()` and `MemoryService.forget()`
  - Non-blocking: write errors must not fail the memory operation
  - Use `asyncio.create_task()` for the write, do not await

- [ ] **U5.3** Call `write_audit_log_entry()` from event logging in `terminal_service.py` (U2 of Phase 2)
  - All 5 event types should appear in the audit log

- [ ] **U5.4** Add log rotation — keep 30 days of log files, delete older ones during daily cleanup
  - Runs in `cleanup_service.py` alongside memory expiry cleanup

- [ ] **U5.5** Add `cao memory logs [--date YYYY-MM-DD]` CLI command
  - Default: show today's log
  - `--date`: show a specific day
  - Output: raw markdown (no formatting needed — it's already readable)

### Acceptance

- `store()` writes to audit log file for today
- Log file exists at `logs/memory/YYYY-MM-DD.md` after first memory operation
- Log rotation deletes files older than 30 days
- `cao memory logs` outputs today's entries
- Audit log write failure does not cause `store()` to fail or raise

---

## U6 — extract_session_context() — Kimi

**Agent**: Backend Developer
**Files**: `src/cli_agent_orchestrator/providers/kimi_cli.py`

**Context**: Kimi CLI is the least-structured provider — its session storage format is non-standard and may require reverse engineering. Phase 2 ships a `NotImplementedError` stub. Phase 3 completes the implementation to cover all 6 providers.

### Subtasks

- [ ] **U6.1** Research Kimi CLI session file location and format
  - Check `~/.kimi/` and `~/.config/kimi/` for session files
  - Determine if sessions are stored as JSONL, JSON, plaintext, or SQLite
  - Document findings inline in the implementation

- [ ] **U6.2** Implement `extract_session_context()` for `KimiCliProvider`
  - Parse session file to extract: last human message, last assistant response summary, any file paths mentioned
  - Return `{ provider: "kimi_cli", terminal_id, last_task, key_decisions, open_questions, files_changed }`
  - If session file format is unusable: return minimal dict with `last_task` only (from tmux history as fallback)

- [ ] **U6.3** Remove `NotImplementedError` stub from `kimi_cli.py`
  - Replace with real implementation from U6.2
  - If tmux-history-only fallback is used, add a comment explaining the limitation

- [ ] **U6.4** Add Kimi to provider injection tests in `test/providers/test_session_context.py`
  - Add fixture for Kimi session file (even if minimal)
  - Assert `extract_session_context()` returns non-empty dict

### Acceptance

- `extract_session_context()` returns at minimum `{ provider: "kimi_cli", last_task: "..." }`
- No `NotImplementedError` raised for Kimi
- Returns empty dict (not exception) if Kimi session file does not exist
- All 6 providers now have working implementations

---

## U7 — Tests & Validation

**Agent**: Code Reviewer
**Files**: `test/services/test_wiki_compiler.py`, `test/services/test_wiki_lint.py`, `test/services/test_scoring.py`, `test/providers/test_kimi_session.py`

### Subtasks

- [ ] **U7.1** `test/services/test_wiki_compiler.py`
  - `test_compile_merges_new_fact` — existing article + new entry → compiled result contains both facts in prose (not timestamped append)
  - `test_compile_fallback_on_timeout` — mock LLM timeout → file falls back to append, no exception
  - `test_compile_mode_append_bypasses_llm` — `compile_mode=append` → LLM not called
  - `test_see_also_added_to_article` — compilation adds `## See Also` section with related key

- [ ] **U7.2** `test/services/test_wiki_lint.py`
  - `test_contradiction_detected` — two articles with conflicting claims → lint returns contradiction issue
  - `test_orphan_page_detected` — wiki file not in index → lint returns orphan issue
  - `test_stale_claim_detected` — article references deleted file path → lint returns stale_claim
  - `test_lint_clean_wiki` — no issues in clean wiki → empty list returned
  - `test_lint_cli_exit_code` — ERROR-severity issue → CLI exits with code 1

- [ ] **U7.3** `test/services/test_scoring.py`
  - `test_score_weights_sum_to_one` — verify BM25(0.5) + recency(0.3) + usage(0.2) = 1.0
  - `test_recent_entry_scores_higher` — same BM25 score, different recency → recent scores higher
  - `test_frequently_accessed_scores_higher` — same BM25 + recency, different access count → higher access = higher score
  - `test_access_count_increments_on_recall` — recall → DB row has access_count = 1
  - `test_sort_by_recency_reproduces_phase1_order` — `sort_by="recency"` → identical to Phase 1/2 ORDER BY updated_at

- [ ] **U7.4** `test/providers/test_kimi_session.py`
  - `test_kimi_extract_returns_dict` — real or mocked session file → dict with `last_task` key
  - `test_kimi_extract_missing_file` — no session file → empty dict, no exception

- [ ] **U7.5** `test/services/test_audit_log.py`
  - `test_store_writes_audit_log` — store memory → log file contains entry for today
  - `test_audit_log_write_failure_does_not_fail_store` — mock log write raising IOError → store succeeds
  - `test_log_rotation_deletes_old_files` — create 35-day-old log file → cleanup deletes it

- [ ] **U7.6** Run full suite, confirm no Phase 1 or Phase 2 regressions
  - `uv run pytest test/ --ignore=test/e2e -v`
  - `uv run mypy src/cli_agent_orchestrator/services/memory_service.py src/cli_agent_orchestrator/services/wiki_compiler.py src/cli_agent_orchestrator/services/wiki_lint.py`

### Acceptance

- All new tests pass
- No regressions in Phase 1 or Phase 2 test suite
- mypy clean on all new files
- Compilation latency benchmark: 95th percentile < 5s per `store()` call in integration test

---

## Build & Test Checklist

Run in order after all units complete:

```bash
# Format
uv run black src/cli_agent_orchestrator/services/wiki_compiler.py \
             src/cli_agent_orchestrator/services/wiki_lint.py \
             src/cli_agent_orchestrator/services/memory_service.py \
             src/cli_agent_orchestrator/providers/kimi_cli.py

# Types
uv run mypy src/cli_agent_orchestrator/services/wiki_compiler.py \
            src/cli_agent_orchestrator/services/wiki_lint.py \
            src/cli_agent_orchestrator/services/memory_service.py

# Unit tests (Phase 3 only)
uv run pytest test/services/test_wiki_compiler.py \
              test/services/test_wiki_lint.py \
              test/services/test_scoring.py \
              test/services/test_audit_log.py \
              test/providers/test_kimi_session.py -v

# Integration tests
uv run pytest -m integration test/ -v

# Full suite (no regressions across all phases)
uv run pytest test/ --ignore=test/e2e -v
```

All must pass before declaring Phase 3 complete.
