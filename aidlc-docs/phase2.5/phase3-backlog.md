# Phase 3 Backlog — Deferred Items from Phase 2.5

**Generated:** 2026-04-22 (U9 Spec Sync handoff)
**Purpose:** Single source of truth for non-blocking findings carried forward from Phase 2.5 audits. Nothing here blocks Phase 2.5 merge; every item is tracked for Phase 3 pickup.
**Source audits:** `aidlc-docs/phase2.5/audit.md`
**Related design doc:** `aidlc-docs/MEMORY_SYSTEM_DESIGN.md`

---

## REST API — deferred CRUD endpoints (from U8 decision)

Phase 2.5 U8 formally deferred 3 of the 4 originally contemplated REST endpoints. See `aidlc-docs/phase2.5/audit.md:1433–1485` for the full four-pillar rationale (YAGNI, double-coverage with MCP+CLI, auth surface, Phase-3 shape drivers).

| # | Endpoint | Status |
|---|---|---|
| 1 | `GET /terminals/{terminal_id}/memory-context` | **Shipped** in Phase 2.5 (loopback-only; sole invoker is Kiro AgentSpawn hook) |
| 2 | `POST /memory` (store) | Deferred to Phase 3 |
| 3 | `GET /memory` (list/search) | Deferred to Phase 3 |
| 4 | `DELETE /memory/{key}` (forget) | Deferred to Phase 3 |

**Phase 3 prerequisites before building the three deferred endpoints** (pulled from design doc §REST API Endpoints and audit):
- `MemoryService` internal API must settle (Phase 2 U1 SQLite and Phase 2.5 U6 project identity are the two most recent re-shapes).
- An auth model must be designed before any write endpoint ships. Loopback binding is insufficient for POST/DELETE.
- Phase 3 Web UI requirements should drive request/response shape, pagination semantics, filter grammar, and the auth story as a coherent design — not back-filled to match assumed needs.

---

## Deferred audit findings (9 items from U4–U7)

All INFO/LOW; zero are blockers. Each was documented when raised, concurred with by both reviewers, and parked for Phase 3.

### Tier 1 residues (U4)

| # | ID | Summary | Source (audit.md) | Remediation sketch |
|---|---|---|---|---|
| 1 | **U4 INFO-1** | Fallback-path `_update_index` flock uncovered. The existing concurrent-write test exercises only the primary path (`sqlite_ok=True`); the flock-serialized fallback (`sqlite_ok=False`) has no test. | `audit.md:510` | Add a targeted test that forces `sqlite_ok=False` and exercises two concurrent writers under a barrier. |

### Tier 2 residues (U5)

| # | ID | Summary | Source (audit.md) | Remediation sketch |
|---|---|---|---|---|
| 2 | **U5 LOW-1** | `cleanup_service.py:119,159` calls `memory_service.forget(...)` in its expired-row sweep. With the enableMemory flag off, `forget()` raises `MemoryDisabledError` which the outer `except Exception` at `:129` catches and logs at WARNING — one WARNING per expired row per sweep. Functionally safe, log-spam only. | `audit.md:806, 846, 904, 1166, 1425` | Add a 2-line `if not is_memory_enabled(): return` guard at the top of the cleanup function. |
| 3 | **U5 INFO-1** | `utils/terminal.py` `FLUSH_MESSAGE` log noise when memory disabled and a flush trigger fires. Low-priority cosmetic fix. | `audit.md:904, 1425` | Gate the log message on `is_memory_enabled()`. |

### Tier 2 residues (U6)

| # | ID | Summary | Source (audit.md) | Remediation sketch |
|---|---|---|---|---|
| 4 | **U6 INFO-1** | No committed regression test for the read-time containment guard at `memory_service.py:1473–1478` (the injection-site guard that teeth-tests during review relied on via source-mutation). | `audit.md:1169, 1426` | Add `test_inject_rejects_tampered_relative_path` — writes a malicious `index.md` with `../` in the relative_path, asserts no Memory surfaces. |
| 5 | **U6 INFO-2** | `_append_legacy_alias_dirs` reads alias values verbatim from SQLite. Defense-in-depth at read time catches traversal, but a pre-filter would stop malformed rows earlier. | `audit.md:1158, 1169, 1426` | Add `basename(alias) + whitelist regex (e.g. ^[a-f0-9]{12}$ for cwd_hash kind)` in `_append_legacy_alias_dirs` before composing the path. Defense-in-depth, not a blocker. |
| 6 | **U6 INFO-3** | `plan_project_dir_migration` ships as a read-only planner; no live apply-path exists. Operators can review the plan but cannot execute it. | `audit.md:1017, 1093, 1159, 1168, 1426` | Ship `cao memory migrate-project-ids [--apply]` CLI command that consumes `plan_project_dir_migration` and actually mutates the filesystem once aliases have been populated in a release. |

### Tier 2 residues (U7)

| # | ID | Summary | Source (audit.md) | Remediation sketch |
|---|---|---|---|---|
| 7 | **U7 INFO-1** | No direct behavioral test for `terminal_service.create_terminal` wrapping a raising `register_hooks` — the wrapping is verified structurally (`test_terminal_service_has_no_hook_ladder`), not behaviorally. Pre-U7 ladder was wrapped identically, so the invariant is preserved; but a direct test would harden against future wrapping-removal. | `audit.md:1240, 1340, 1427` | Add a unit test on `create_terminal` that installs a raising `register_hooks` and asserts the terminal is still created (behavior matches pre-U7 ladder). |
| 8 | **U7 INFO-2** | `BaseProvider.register_hooks` docstring does not explicitly state "MUST be idempotent" or "MUST NOT raise for non-fatal failures". The contract is correct in code but implicit in prose. | `audit.md:1341, 1427, 1429` | Strengthen the docstring to "must"-language so future provider authors treat it as a hard contract. |
| 9 | **U7 INFO-3** | `register_hooks_kiro` writes `hooks.json` without an explicit file-lock; concurrent `create_terminal` calls for two Kiro agents in the same directory could race. Pre-existing, not introduced by U7 (the old ladder had the same behavior). | `audit.md:1427` | Document the concurrency property (and either add an `fcntl` flock or note the single-writer assumption) at `register_hooks_kiro`. |

---

## Handoff notes

- All 9 items were explicitly documented as "non-blocking" at the time of raising; challenger and security-reviewer both concurred.
- No item touches the loopback-only auth posture or any already-closed decision.
- Phase 3 team should treat this file as the starting backlog and grow it with Phase 3's own findings.
