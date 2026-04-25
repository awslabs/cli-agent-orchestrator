# Success Criteria — CAO Memory System Phase 2.5

**Generated:** 2026-04-20
**Status:** AWAITING USER APPROVAL
**Trigger:** Review follow-up for PR #179 items not covered by Phase 2 (U1–U10).
**Design refs:**
- `aidlc-docs/MEMORY_SYSTEM_DESIGN.md` (technical design)
- `aidlc-docs/MEMORY_SYSTEM_PROPOSAL.md` (problem framing, wiki pattern, scope model)
- `aidlc-docs/phase2.5/tasks.md` (unit breakdown)

---

## Tier 1 — Correctness (MVP)

**SC-1 (U1, PreCompact safety):** The PreCompact hook does not cancel Claude Code compaction.
*Verified by:* Integration test that invokes the hook and asserts compaction is not blocked; no output shape contains `"decision":"block"` on the PreCompact path.
*Reference:* `MEMORY_SYSTEM_DESIGN.md` §Hook-Triggered Self-Save warns the hook "always returns" block — SC inverts this. Existing Phase 2 U8 (`pre-compaction flush`) covers the save intent non-blockingly.

**SC-2 (U2, per-scope injection cap):** `get_memory_context_for_terminal()` never returns output where a single scope exceeds its allotted share, and per-scope entry count is ≤ 10.
*Verified by:* Unit test stores 20 long memories in one scope → asserts ≤ 10 entries and ≤ per-scope char budget in output. Second test populates all three scopes → asserts each gets its own slice in precedence order.
*Reference:* Proposal §Design Rec 6: *"10 most recent memories per scope, max ~2KB total."*

**SC-3 (U3, regex round-trip):** Index writer and reader stay in sync — every field written is recovered.
*Verified by:* Property-style test writes N synthetic entries via production writer, reads back via production reader, asserts equality on (key, memory_type, tags, updated_at) for all entries including unicode, multi-word tags, empty tags. Test fails if either format drifts.
*Reference:* Design `index.md` format §Storage Layout; reviewer "drift risk" quote.

**SC-4 (U4a, durability across restart):** Memories survive `MemoryService` reinstantiation.
*Verified by:* Store 5 memories → discard service → new service at same base_dir/DB → recall returns all 5 with content intact.
*Reference:* Design §Success Criteria Phase 1: *"Memories survive server restart."*

**SC-5 (U4b, concurrent write safety):** Two concurrent writers to the same scope index produce a parseable index with both entries present.
*Verified by:* Multi-process/thread test with a barrier → both writes succeed, `_parse_index()` returns both, index.md not corrupted. Skipped on platforms without `fcntl`.
*Reference:* `memory_service.py:470, 567` flock usage; Proposal §Open Questions "Wiki file concurrency."

---

## Tier 2 — Architecture

**SC-6 (U5, enableMemory flag):** Memory subsystem can be disabled via settings with no filesystem or SQLite side effects when off.
*Verified by:* Flag=false → `memory_store/recall/forget/consolidate` and `get_memory_context_for_terminal` are no-ops; no wiki files created, no SQLite rows inserted. Flag=true → existing Phase 1/2 behavior unchanged (all prior tests pass).
*Reference:* @patricka3125 review comment requesting opt-in.

**SC-7 (U6, stable project identity):** Project memories survive directory rename and resolve identically across git worktrees.
*Verified by:*
- (a) Same git repo at two different paths (main + worktree) → `resolve_scope_id("project", ctx)` returns the same id → same memories recalled.
- (b) Move a project dir → memories remain recallable (via alias table or stable git remote).
- (c) Non-git directory → falls back to cwd-hash, matches current Phase 1/2 behavior.
*Reference:* Proposal §Open Questions "Scope resolution edge cases"; @patricka3125 quote: *"any change made to the filesystem ... would immediately break project memory reference, this also makes any type of workflow involving worktrees incompatible."*

**SC-8 (U7, hook registration on BaseProvider):** `terminal_service.py` has no provider-type conditionals for hook registration.
*Verified by:* Grep for `if provider == ProviderType.*:` in the hook-registration section of `terminal_service.py` returns zero hits; each provider's `register_hooks()` method exercises the same filesystem effects as the prior standalone function (validated by existing hook tests).
*Reference:* @patricka3125 review comment.

---

## Tier 3 — Scope Cleanup

**SC-9 (U8, REST endpoints):** Spec and shipped code agree on REST surface.
*Verified by:* Path (b) selected — `MEMORY_SYSTEM_DESIGN.md` §REST API Endpoints marks the three write endpoints as deferred with rationale. Surface is now **1 shipped + 3 deferred to Phase 3** (not 4 shipped). See `aidlc-docs/phase2.5/phase3-backlog.md` §REST API for the endpoint table and Phase 3 prerequisites.
*Final status:* **Shipped (Path B — deferral).** Audit back-ref: `audit.md:1433–1485` (U8 Decision — DEFER to Phase 3). Design doc delta: `MEMORY_SYSTEM_DESIGN.md:439–476`.
*Reference:* Design §REST API Endpoints lists 4; only 1 ships.

**SC-10 (U9, spec sync):** No claim in design/proposal docs contradicts shipped behavior.
*Verified by:* Walk of `success-criteria.md` (root) and `MEMORY_SYSTEM_DESIGN.md` against main — every SC marked shipped / partial / deferred with commit refs; every deferred item has an explicit Phase-N label.
*Final status:* **Shipped (2026-04-22).** U9 updates landed: SC-9 revised to "1 shipped + 3 deferred"; §Project Identity paragraph in design doc aligned with U6 shipped resolver (`resolve_project_id`, `_record_alias_safe`, `plan_project_dir_migration`); `plan_project_identity_migration` name-drift at `MEMORY_SYSTEM_DESIGN.md:103` corrected; `phase3-backlog.md` created with 9 deferred INFO items from U4–U7.
*Reference:* Reviewer "13 of 20 delivered" framing.

---

## Cross-Cutting Quality Criteria (apply to every unit)

**SC-Q1 — No regressions:** All 1437 existing tests still pass after the unit lands.
**SC-Q2 — mypy clean:** Zero new mypy errors.
**SC-Q3 — Challenger approved:** Per `feedback_review_workflow.md`, challenger agent must approve before next unit starts.
**SC-Q4 — Security-reviewer approved:** Security-reviewer must approve before next unit starts.
**SC-Q5 — AIDLC audit:** Every unit's decisions, debate, and verdict appended to `aidlc-docs/phase2.5/audit.md`.

---

## Explicit Non-Goals (out of scope for Phase 2.5)

- LLM-powered wiki compilation (Phase 3 per design doc §Phase 3 Design Notes).
- Cross-project federation (Phase 4).
- Web UI for memory (Phase 4).
- Any Kimi `extract_session_context()` work (deferred to Phase 3).
- Embedding-based retrieval — design doc §Design Rec 1 explicitly rejects this in favor of the Karpathy wiki pattern.

---

## MVP Definition

**Ship Tier 1 (SC-1..SC-5).** Exit criteria:
- All Tier 1 SCs PASS.
- Reviewer's PR #179 items closed: C4 (cap), T1 (durability), T2 (concurrent), T3 (round-trip), plus the PreCompact block bug.
- Tier 2 and Tier 3 may follow in separate PRs.

---

## Approval

- [ ] User has reviewed and approved these criteria.
- [ ] User has approved the tier ordering (T1 → T2 → T3).
- [ ] User has approved the MVP scope (T1 only).

**Until all three boxes are checked, no unit implementation begins.**
