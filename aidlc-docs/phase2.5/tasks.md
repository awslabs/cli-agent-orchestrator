# Tasks — CAO Memory System Phase 2.5 (Review Follow-Up)

**Generated**: 2026-04-20
**Scope**: Address reviewer feedback on PR #179 not covered by Phase 2 (U1–U10).
**Source**: https://github.com/awslabs/cli-agent-orchestrator/pull/179#issuecomment-4266032266
**Prerequisite**: Phase 2 complete and merged, 1437 tests passing.
**Review workflow**: Every unit must pass challenger + security-reviewer before next unit starts.

---

## Tier Rationale

- **Tier 1** — Correctness bugs and high-value test/design fixes that should ship before users hit them.
- **Tier 2** — Architectural improvements that are cheap to defer but compound in cost if deferred too long.
- **Tier 3** — Code-health and scope-deferral work; does not block any user path.

---

## Unit Map

| ID | Tier | Unit | Agent | Depends On | Notes |
|---|---|---|---|---|---|
| U1 | 1 | PreCompact Hook Safety Fix | Backend Developer | — | `"decision":"block"` currently cancels compaction; must not block |
| U2 | 1 | Per-Scope Injection Cap | Backend Developer | — | One-line spec bug; scope can monopolize 3KB budget |
| U3 | 1 | Regex Round-Trip Test | Code Reviewer | — | Retires "drift risk" between `_update_index` writer and reader |
| U4 | 1 | Durability + Concurrent Write Tests | Code Reviewer | — | Prove S5 (restart) and `fcntl.flock` safety claims |
| U5 | 2 | enableMemory Settings Flag | Backend Developer | — | Opt-in toggle requested by @patricka3125 |
| U6 | 2 | Stable Project Identity Resolver | Backend Developer | U1..U4 done | Replaces cwd-hash with git remote → explicit override → hash fallback; fixes rename/worktree |
| U7 | 2 | Hook Registration via BaseProvider | Backend Developer | — | Eliminate `if claude_code / elif codex / elif kiro` ladder |
| U8 | 3 | REST CRUD Endpoints Decision | Architect | — | Build the 3 missing endpoints OR formally defer in spec |
| U9 | 3 | Spec Sync Pass | Architect | U1–U8 | Update Phase 1/2 docs so shipped reality matches prose |

---

## U1 — PreCompact Hook Safety Fix (Tier 1)

**Agent**: Backend Developer
**Files**: `src/cli_agent_orchestrator/hooks/cao_precompact_hook.sh`, `src/cli_agent_orchestrator/hooks/registration.py`, tests.

**Context**: The current PreCompact hook returns `{"decision":"block","reason":"EMERGENCY SAVE before..."}`. In Claude Code hooks, `"decision":"block"` on PreCompact prevents compaction from running. The intent was to nudge the agent to call `memory_store`; the effect is to cancel compaction entirely, leaving the user stuck at context limit.

### Subtasks

- [ ] **U1.1** Change hook output to a non-blocking signal. Options, in preference order:
  1. Return `{}` (no-op) and inject the save reminder via `additionalContext` or a `systemMessage` field instead.
  2. Return `{"decision":"approve","reason":"..."}` where `approve` is a valid terminal action that doesn't cancel compaction.
  3. Remove the hook entirely and rely on Phase 2 U8 (in-process pre-compaction flush) which already exists.
- [ ] **U1.2** Verify against Claude Code hook contract (`PreCompact` spec) that chosen return shape is safe. Cite the Claude Code doc version.
- [ ] **U1.3** Update `register_hooks_claude_code()` if the hook should be removed entirely, or keep registration but swap the script body.
- [ ] **U1.4** Integration test: mock the PreCompact hook invocation, assert compaction proceeds (no `block` decision, no non-zero blocking exit code).
- [ ] **U1.5** Document in `docs/hooks.md` (or equivalent) what the hook now does and why.

### Acceptance Criteria

- AC1: PreCompact hook never returns a value that cancels compaction.
- AC2: If memory-save reminder is still desired, it arrives via a non-blocking channel.
- AC3: Test covers the "compaction proceeds" path.
- AC4: U8 (Phase 2 in-process flush) is not regressed.

---

## U2 — Per-Scope Injection Cap (Tier 1)

**Agent**: Backend Developer
**Files**: `src/cli_agent_orchestrator/services/memory_service.py` (`get_memory_context_for_terminal`, line 1062).

**Context**: Spec (verbatim): *"10 most recent memories per scope, max ~2KB total."*
Current code uses one global `budget_chars=3000` loop over `[session, project, global]` memories (lines 1108–1118). A single scope with long content can consume the whole budget, starving other scopes. The reviewer's one-line fix is `[:10]` per scope; the spec also asks for per-scope character cap.

### Subtasks

- [ ] **U2.1** In `get_memory_context_for_terminal`, enumerate memories per scope separately and cap:
  - `MAX_PER_SCOPE = 10` entries
  - `SCOPE_BUDGET_CHARS ≈ 2048 / 3 ≈ 680` per scope (or keep 2KB total and divide)
- [ ] **U2.2** Preserve scope precedence (session > project > global) — if one scope is empty, do NOT reallocate its budget to others (keeps cache-friendly behavior from U7).
- [ ] **U2.3** Add constants to `constants.py` so the caps are tunable without code change.
- [ ] **U2.4** Tests:
  - One scope with 20 long memories → truncated to 10 entries, bounded chars.
  - All three scopes populated → each gets its own slice.
  - Empty scope → other scopes don't grow.

### Acceptance Criteria

- AC1: No single scope consumes more than its allotted slice of the budget.
- AC2: `MAX_PER_SCOPE = 10` enforced for every scope with memories.
- AC3: Total injection stays within ~2KB (verify via test).
- AC4: Scope precedence ordering preserved in output.

---

## U3 — Regex Round-Trip Test (Tier 1)

**Agent**: Code Reviewer
**Files**: `test/services/test_memory_service_index_roundtrip.py` (new).

**Context**: Reviewer's "drift risk" concern. Index is written in `_update_index` / `_regenerate_scope_index` and read back in `_parse_index` with the regex:
```python
r"^- \[([^\]]+)\]\(([^)]+)\) — type:(\S+) tags:(\S*) ~\d+tok updated:(\S+)$"
```
If writer format ever changes without updating the regex (or vice versa), entries vanish silently. No test currently enforces the invariant.

### Subtasks

- [ ] **U3.1** Property-style test: write N synthetic entries via `_update_index`, read back via `_parse_index`, assert round-trip equality on `(key, memory_type, tags, updated_at)`.
- [ ] **U3.2** Edge cases:
  - Empty tags, multi-word tags, unicode in key, long key, special chars in key (within slug rules).
  - Multiple scopes written in one index file.
- [ ] **U3.3** Regression guard: if either the writer format string or the reader regex is edited, the test should fail.

### Acceptance Criteria

- AC1: Test writes through production writer, reads through production reader — no shortcuts.
- AC2: Every field written is recovered.
- AC3: Test fails if writer/reader drift.
- AC4: Runs under 1s.

---

## U4 — Durability + Concurrent Write Tests (Tier 1)

**Agent**: Code Reviewer
**Files**: `test/services/test_memory_durability.py` (new), `test/services/test_memory_concurrent.py` (new).

**Context**: Reviewer called out S5 (durability across restart) and `fcntl.flock` concurrency as untested. Code uses `fcntl.LOCK_EX` at memory_service.py:470 and :567 but no test exercises contention.

### Subtasks

- [ ] **U4.1** Durability test:
  - Store 5 memories with a `MemoryService` instance.
  - Discard and re-instantiate `MemoryService` pointing at the same base dir + SQLite DB.
  - Recall → all 5 returned, file paths resolve, content intact.
- [ ] **U4.2** Concurrent write test:
  - Spawn 2 threads (or `multiprocessing.Process`) storing to the same scope index simultaneously.
  - Assert both succeed, index.md parses cleanly afterward, both entries present.
  - Use a small barrier to force contention window.
- [ ] **U4.3** Skip on platforms where `fcntl` is unavailable (Windows) with `pytest.mark.skipif`.

### Acceptance Criteria

- AC1: Durability test passes on clean restart.
- AC2: Concurrent test exposes any missing flock or lost-update bug (should pass with current code).
- AC3: Tests complete in under 3s combined.
- AC4: Tests documented in `test/services/README.md` if one exists.

---

## U5 — enableMemory Settings Flag (Tier 2)

**Agent**: Backend Developer
**Files**: `src/cli_agent_orchestrator/services/settings_service.py`, `services/memory_service.py`, `api/main.py`.

**Context**: @patricka3125 asked for an opt-in flag; author deferred. With memory default-on, any test run or short-lived terminal leaves wiki files behind. An explicit opt-in (or opt-out) gives users control.

### Subtasks

- [ ] **U5.1** Decide default: opt-in (`false`) for least surprise, or opt-out (`true`) to preserve current behavior. Recommend opt-out = `true` with clear docs and an opt-out toggle.
- [ ] **U5.2** Add `memory.enabled: bool` to settings schema; surface via settings_service.
- [ ] **U5.3** Short-circuit in `memory_service.store/recall/forget/consolidate` and `get_memory_context_for_terminal` when disabled.
- [ ] **U5.4** MCP tools return a clear "memory disabled" message instead of silent no-op.
- [ ] **U5.5** Tests:
  - With flag off, store→recall returns nothing, no filesystem writes.
  - With flag on, existing behavior unchanged.

### Acceptance Criteria

- AC1: Flag readable via settings_service.
- AC2: All memory entry points respect the flag.
- AC3: No filesystem or SQLite writes when disabled.
- AC4: Existing Phase 1/2 tests still pass (flag defaults preserve current behavior).

---

## U6 — Stable Project Identity Resolver (Tier 2)

**Agent**: Backend Developer
**Files**: `src/cli_agent_orchestrator/services/memory_service.py` (`resolve_scope_id`, `_get_project_dir`), `clients/database.py` (new `ProjectAliasModel`).

**Context**: @patricka3125, verbatim: *"any change made to the filesystem (i.e. folder renaming, mv project to a different directory) would immediately break project memory reference, this also makes any type of workflow involving worktrees incompatible."* SQLite's `MemoryMetadataModel.scope_id` column exists but stores the same cwd-hash — the column is not the fix; the resolver is.

### Subtasks

- [ ] **U6.1** Define identity precedence:
  1. Explicit `project_id` from settings or env.
  2. Git remote URL (first remote, normalized) — stable across rename/worktree.
  3. `realpath(cwd)` SHA256[:12] fallback (current behavior) — only when 1 and 2 unavailable.
- [ ] **U6.2** Add `ProjectAliasModel`:
  ```python
  class ProjectAliasModel(Base):
      __tablename__ = "project_aliases"
      project_id = Column(String, primary_key=True)  # canonical ID
      alias = Column(String, primary_key=True)       # cwd-hash, alt path, etc.
      kind = Column(String)                          # "git_remote" | "cwd_hash" | "manual"
      created_at = Column(DateTime, default=datetime.utcnow)
  ```
- [ ] **U6.3** On `resolve_scope_id("project", ctx)`:
  - Compute canonical_id via precedence.
  - Look up aliases; if canonical has old cwd-hash entries, migrate or union them.
  - Record new aliases opportunistically (e.g., first time a git-remote project is seen at a new path, log the alias).
- [ ] **U6.4** Storage layout: keep wiki dir keyed by canonical_id; add one-time migration from old `<hash>/` dirs to `<canonical>/` using alias table.
- [ ] **U6.5** Worktree test: same git repo at two paths (main and worktree) → same `project_id` → same memories recalled.
- [ ] **U6.6** Rename test: move a project dir, recall → memories still returned (via alias lookup).
- [ ] **U6.7** Non-git test: falls back to cwd-hash; behavior matches current.

### Acceptance Criteria

- AC1: Git repo at two different paths resolves to the same `project_id`.
- AC2: Renaming a project dir does not orphan memories (via alias or stable git remote).
- AC3: Non-git project continues to work via hash fallback.
- AC4: Migration path for existing `<hash>/` dirs documented and tested.

---

## U7 — Hook Registration via BaseProvider (Tier 2)

**Agent**: Backend Developer
**Files**: `src/cli_agent_orchestrator/providers/base.py`, each provider module, `services/terminal_service.py:227–232`, `hooks/registration.py`.

**Context**: @patricka3125 wants each provider to own its hook registration. Current dispatch is a ladder. Each new provider adds another `elif`.

### Subtasks

- [ ] **U7.1** Add to `BaseProvider`:
  ```python
  def register_hooks(self, working_directory: Optional[str], agent_profile: Optional[str]) -> None:
      """Default: no-op. Override to install provider-specific hooks."""
      return
  ```
- [ ] **U7.2** Move `register_hooks_claude_code` body into `ClaudeCodeProvider.register_hooks`, etc. for Kiro and Codex.
- [ ] **U7.3** `terminal_service.py` calls `provider.register_hooks(working_directory, agent_profile)` — no more `if/elif`.
- [ ] **U7.4** Keep `hooks/registration.py` functions as thin wrappers for back-compat (or delete if unused elsewhere).
- [ ] **U7.5** Tests: each provider's `register_hooks` mocked-filesystem behavior matches previous standalone function.

### Acceptance Criteria

- AC1: `terminal_service.py` has no provider-type conditionals for hooks.
- AC2: Adding a new provider requires zero changes in `terminal_service.py` for hook registration.
- AC3: Existing Phase 2 U7 (cache-aware injection) unaffected.
- AC4: All existing hook tests pass unchanged.

---

## U8 — REST CRUD Endpoints Decision (Tier 3)

**Agent**: Architect
**Files**: `src/cli_agent_orchestrator/api/main.py`, `aidlc-docs/MEMORY_SYSTEM_DESIGN.md`.

**Context**: Spec lists 4 endpoints; only `GET /terminals/{id}/memory-context` exists. MCP covers agent-facing use cases. Web UI may or may not need the rest.

**Decision (2026-04-21, architect, team-lead ratified): Path B — DEFER the 3 missing endpoints to Phase 3.** Full rationale at `aidlc-docs/phase2.5/audit.md` §"2026-04-21 — U8 Decision — DEFER to Phase 3 (architect)" — four pillars: YAGNI (no current consumer), double-coverage redundancy (MCP + CLI already cover all three consumer classes), auth surface untouched (loopback trust does not extend to writes), shape-drift avoidance (Phase 3 Web UI will drive request/response design).

### Subtasks

- [x] **U8.1** Decide: build the missing 3 (`POST /memories`, `GET /memories`, `DELETE /memories/{id}`) OR formally defer to Phase 3 in design doc. → **Deferred.**
- [ ] ~~**U8.2** If build: add endpoints, request/response models, input validation, tests. Reuse memory_service directly.~~ → **N/A (Path B selected).**
- [x] **U8.3** If defer: update `MEMORY_SYSTEM_DESIGN.md` to mark REST surface as Phase 3 and state rationale (MCP sufficient for MVP agents). → **Done** — see `MEMORY_SYSTEM_DESIGN.md` §REST API Endpoints (shipped/deferred split).

### Acceptance Criteria

- AC1: Spec and shipped code agree on what REST endpoints exist. → **Satisfied** by this entry + audit entry + design-doc delta (all three say 1 shipped + 3 deferred).
- AC2: If built, endpoints have integration tests and docs. → **Not triggered** (Path B).
- AC3: If deferred, rationale explicit in design doc. → **Satisfied** — four-pillar rationale in `MEMORY_SYSTEM_DESIGN.md` + full detail in audit entry.

---

## U9 — Spec Sync Pass (Tier 3)

**Agent**: Architect
**Files**: `aidlc-docs/MEMORY_SYSTEM_DESIGN.md`, `aidlc-docs/success-criteria.md`, `aidlc-docs/phase2/tasks/tasks.md`.

**Context**: Reviewer's "13 of 20 delivered" count implies doc-vs-code drift. After U1–U8 ship, reconcile the prose.

### Subtasks

- [ ] **U9.1** Walk success-criteria.md, mark each SC as: shipped / partial / deferred with commit refs.
- [ ] **U9.2** Update MEMORY_SYSTEM_DESIGN.md for decisions locked in U6 (identity model) and U5 (opt-in flag).
- [ ] **U9.3** Append Phase 2.5 tasks.md completion checklist.

### Acceptance Criteria

- AC1: No spec claim contradicts shipped behavior.
- AC2: Every deferred item has an explicit "deferred to Phase N" note.
- AC3: Diff against main for design docs is reviewable in one pass.

---

## Cross-Unit Risks

- **U1 vs U8 (Phase 2)**: Phase 2 U8 pre-compaction flush is in-process and does not depend on the shell hook. If U1 removes the shell hook, U8 Phase 2 path is the primary mechanism — verify it still covers the case the shell hook was written for.
- **U6 migration**: Touching storage layout risks breaking existing users. Ship a dry-run mode first; never delete old dirs until alias table is populated.
- **U2 + U7 Phase 2**: Per-scope cap must interact cleanly with cache-aware injection (static/dynamic split). Verify scope boundaries do not leak into the static identity block.

---

## Tier 1 MVP Definition

**Target:** Address every reviewer concern that is a correctness bug or test gap.
**Ship:** U1 + U2 + U3 + U4.
**Estimate:** ~1–2 dev days.
**Exit criteria:** PR #179 review items C4 (cap), T1 (durability), T2 (concurrent), T3 (round-trip), and PreCompact safety all closed.

---

## Phase 2.5 Final Status (2026-04-22)

| Unit | Status | Audit back-ref | Notes |
|------|--------|----------------|-------|
| U1 | Shipped — both gates approved | `audit.md` §"2026-04-20 — U1 PreCompact" | Hook body `echo '{}'`; 3 new tests, 1441 passing |
| U2 | Shipped — both gates approved | `audit.md` §"2026-04-20 — U2 Per-Scope Cap" | `MEMORY_MAX_PER_SCOPE=10`, `MEMORY_SCOPE_BUDGET_CHARS=1000`; 5 tests, 1446 passing |
| U3 | Shipped — both gates approved | `audit.md` §"2026-04-20 — U3 Regex Round-Trip" | ISO-8601 Z-suffix invariant locked; 7 tests, 1453 passing |
| U4 | Shipped — both gates approved | `audit.md:510` | Durability + concurrent (primary path); fallback flock → `phase3-backlog.md` (INFO-1) |
| U5 | Shipped — both gates approved | `audit.md:806`, `:846`, `:904` | `memory.enabled` flag, guard-first + raise-vs-empty; 18 tests, 1475 passing; LOW-1/INFO-1 → phase3-backlog |
| U6 | Shipped — both gates approved | `audit.md:1020`, `:1095`, `:1099` | Module-level `resolve_project_id`, `ProjectAliasModel`, dry-run `plan_project_dir_migration`; 29 tests, 1504 passing; INFO-1/2/3 → phase3-backlog |
| U7 | Shipped — both gates approved | `audit.md:1175`, `:1244`, `:1350` | `BaseProvider.register_hooks` default no-op + per-provider overrides; 18 tests, 1522 passing; INFO-1/2/3 → phase3-backlog |
| U8 | **Deferred to Phase 3** — architect decision, team-lead ratified | `audit.md:1433–1485` | 1 shipped + 3 deferred REST endpoints; see `phase3-backlog.md` §REST API |
| U9 | Shipped (docs-only) | `audit.md` §"2026-04-22 — U9 Spec Sync" | 5 doc updates: `phase3-backlog.md` NEW; SC-9/SC-10 final-status lines; `MEMORY_SYSTEM_DESIGN.md:103` name-drift fix; Phase 1/2 sweep grep-clean; this table |

**Phase 2.5 closes with all 9 units accounted for.** 9 non-blocking items (U4 INFO-1; U5 LOW-1, INFO-1; U6 INFO-1, INFO-2, INFO-3; U7 INFO-1, INFO-2, INFO-3) handed off to `aidlc-docs/phase2.5/phase3-backlog.md`.
