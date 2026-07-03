# Design: Open Knowledge Format (OKF) Export/Import for CAO Memory

**Issue:** #345
**Status:** Draft for maintainer review
**Scope:** Design only — no code in this document's commit.

---

## Summary

Add `cao memory export --format okf` and `cao memory import --format okf` so CAO's
wiki-based memory can be published as a plain directory of OKF v0.1 markdown files
(git/Obsidian/sync-friendly) and ingested back from such a directory. The feature is
built behind a small `MemoryArchiveBackend` ABC + registry (the project's established
extensibility pattern), so the parked CAO-native tar.gz format (branch
`docs/memory-import-export`, not on main) slots in later as a second backend without
touching the CLI or service layer.

Export is treated as a **declassification boundary**: every topic body passes through
the secret gate before leaving the CAO store, and exporting the private `session`/
`agent` scopes requires an explicit `--include-private` flag (export is strictly
per-scope). Import always requires an explicit `--target-scope` and routes every row
through `MemoryService.store()` so SQLite metadata, `index.md`, and file locking stay
consistent.

Export is deterministic and idempotent (stable filenames, stable frontmatter key order,
content-hash change detection), which makes the follow-up `cao memory sync <dir>`
command (maintainer ask, out of scope here) a thin loop over the same writer.

---

## Context

### What exists on `main` today

- **Content store:** wiki markdown files under `MEMORY_BASE_DIR`
  (`~/.aws/cli-agent-orchestrator/memory/<container>/wiki/<scope>[/<scope_id>]/<key>.md`),
  written and locked by `MemoryService` (`services/memory_service.py`).
- **Topic file shape** (produced by `store()`):

  ```markdown
  # <key>
  <!-- id: <uuid> | scope: <scope> | type: <memory_type> | tags: <csv> -->

  ## 2026-07-01T12:00:00Z
  <entry content>

  ## 2026-07-02T09:30:00Z
  <newer entry content>

  ## See Also
  - [other-key](../<scope_id>/other-key.md)
  ```

  The `<!-- id | scope | type | tags -->` comment is the metadata header;
  `## <ISO-timestamp>` sections accumulate append-style (the deferred LLM compiler
  may later rewrite the article, preserving the header and `## See Also`).
- **Index:** per-container `wiki/index.md` with entry lines of the form
  `- [key](scope/key.md) — type:X tags:Y ~Ntok updated:Z`, parsed by `_parse_index`.
- **Metadata mirror:** SQLite `MemoryMetadataModel` rows (key, scope, scope_id,
  file_path, tags, provenance, `access_count`, `related_keys`, compile timestamps).
  SQLite is derived-but-authoritative for cross-references; wiki files are the
  content source of truth.
- **Validators:** `MemoryService._sanitize_key` (lowercase slug, basename-reduced,
  traversal-safe), `get_wiki_path` (base-dir containment check), and the shared
  path validator `clients/tmux.py::_resolve_and_validate_working_directory`
  (realpath + absolute-path guard + blocked-system-dir frozenset) mandated by
  project rules for all user-supplied paths.
- **Secret gate:** `services/secret_gate.py::scan_for_secrets()` — pure, ordered
  named-regex deny-list returning the pattern NAME of the first hit (or None).
  Currently used to gate `federated` writes.
- **CLI:** `cli/commands/memory.py` — `list/show/delete/clear/lint/compact/heal`.
  **Verified:** these commands instantiate `MemoryService()` directly; they do NOT
  go through the HTTP API. A REST mirror of list/show/delete/clear exists on the
  server side (issue #286, `api/main.py`) for the web UI, but the CLI's pattern is
  direct service calls. Export/import follows the CLI's actual pattern (see D6).
- **No export/import of any kind on main.**

### What exists on the parked branch (`docs/memory-import-export`, NOT on main)

A CAO-native tar.gz bundle design: full-fidelity archive (scope, scope_id,
provenance, SQLite metadata included), with a documented tar threat model
(gzip-ratio cap, decompressed-size cap, per-file cap, traversal rejection). It was
parked, not rejected — this design reserves the `cao` format name for it and defines
the seam it will plug into.

### Maintainer (haofei) asks captured in #345

1. `cao memory sync <dir>` — keep an OKF directory continuously mirrored (Follow-ups).
2. OKF directory consumable by the Obsidian web clipper / CAO dashboard for a live
   knowledge-graph view (Follow-ups).
3. Clarity on graph ownership — who owns the knowledge graph when a mirror exists (D4).

---

## Format Mapping (CAO ⇄ OKF v0.1)

| CAO wiki construct | OKF v0.1 construct | Direction / notes |
|---|---|---|
| `<key>.md` filename (sanitized slug) | `<key>.md` filename | Both ways; key already satisfies OKF filename rules via `_sanitize_key` |
| `# <key>` H1 | `title` frontmatter (also kept as H1 in body) | Export; on import, `title` is informational — key comes from filename |
| `<!-- id: … \| scope: … \| type: … \| tags: … -->` header comment | YAML frontmatter: `type` (required), `tags` (list) | Rewritten on export; synthesized from frontmatter on import |
| `type` (CAO `MemoryType`: user/feedback/project/reference) | `type` frontmatter (required by OKF §9) | 1:1 both ways; unknown OKF types map to `reference` on import (documented coercion) |
| `tags: a,b,c` (csv) | `tags: [a, b, c]` (YAML list) | Both ways |
| Latest `## <ISO-ts>` section body | Topic body (below frontmatter) | Export: latest section only; **`timestamp` frontmatter = that latest section's ISO ts** (it pairs with the body it describes) |
| Older `## <ISO-ts>` sections | `history/<key>.md` per-topic history file | Export, only with `--include-history`; see D2 for the log.md-vs-history/ decision |
| First entry timestamp (topic creation) | `created` frontmatter (CAO-emitted extra key) | Export only; a second field so creation time isn't conflated with `timestamp`. On import, `timestamp` is preserved per D5; `created` is informational (unknown-key-tolerant importers ignore it, ours does too) |
| First ~1 sentence of latest content | `description` frontmatter (recommended) | Export only, derived; dropped on import (body is authoritative) |
| `## See Also` links | `## See Also` links, paths normalized bundle-relative | **Export only (lossy on import) in PR 1.** `store()` has no related-keys parameter and the `## See Also` block is a projection of SQLite `related_keys` (D4) — import strips the block from the body before `store()` and counts the dropped links in the report. `related_keys` ingestion is a designed Follow-up (see D5) |
| `wiki/index.md` (`- [key](path) — type:… tags:… ~Ntok updated:…`) | `index.md` (`* [Title](url) - description`, no frontmatter) | Export: regenerated in OKF line form; import: ignored (topics are authoritative) |
| `scope`, `scope_id` | — (dropped) | **Lossy on export.** OKF carries no scope; import requires explicit `--target-scope` |
| SQLite provenance (`source_provider`, `source_terminal_id`), `access_count`, `related_keys` raw cell, compile timestamps | — (dropped) | **Lossy on export.** Full fidelity is the `cao` format's job |
| `id` (uuid) | — (dropped) | New uuid minted by `store()` on import |

Reserved filenames in a bundle: `index.md`, `manifest.md` (multi-scope note, Open
Questions), anything under `history/`. Everything else `*.md` must parse as an OKF
topic (OKF §9 conformance).

---

## Architecture

### Module layout

```
src/cli_agent_orchestrator/services/memory_archive/
    __init__.py        # registry: register_backend(name, cls), get_backend(name)
    base.py            # MemoryArchiveBackend ABC + ExportReport/ImportReport dataclasses
    okf.py             # OkfArchiveBackend — first implementation
    # cao.py           # future: CAO-native tar.gz backend (parked branch)
```

Constants (new names, all in `constants.py` per Mandated rule):
`MEMORY_ARCHIVE_DEFAULT_FORMAT = "okf"`, and — reserved for the future `cao` tar
backend — `MEMORY_ARCHIVE_MAX_DECOMPRESSED_BYTES`, `MEMORY_ARCHIVE_MAX_FILE_BYTES`,
`MEMORY_ARCHIVE_MAX_GZIP_RATIO`.

### The ABC

```python
class MemoryArchiveBackend(ABC):
    """One import/export format for the CAO memory store."""

    format_name: str  # registry key, e.g. "okf"

    @abstractmethod
    def export_bundle(
        self, scope: str, scope_id: Optional[str], dest: Path,
        include_history: bool, redact: bool,
    ) -> ExportReport: ...

    @abstractmethod
    def import_bundle(
        self, src: Path, target_scope: str,
        conflict_policy: str, dry_run: bool,
    ) -> ImportReport: ...
```

`ExportReport`: counts (exported, skipped_secret, redacted, pruned, unchanged,
links_dropped), plus per-topic skip reasons carrying **pattern names only**. There is
no `skipped_private` count — export is strictly per-scope, so private scopes are
gated at the flag level (a whole-command error, D5), never skipped per topic.
`ImportReport`: counts (imported, skipped_conflict, replaced, merged, rejected,
see_also_dropped, bodies_escaped, timestamps_clamped), per-file parse/validation errors, the **resolved target scope and
scope_id** (for `--scope project`, the cwd-resolved project id — D5), and the
`dry_run` flag.

Backends receive a `MemoryService` instance (constructor injection) and use only its
public/validated surfaces: for reading, walk `_parse_index` per container index and
read each topic file via `_parse_wiki_file` — **not** `recall()`, whose `limit`
parameter caps results (default 10) and whose ranking is irrelevant here; an exporter
must enumerate every topic of the scope, which is exactly the index walk. Writing goes
through `store()`/`forget()`; validation through `_sanitize_key`/`get_wiki_path`.
Backends never write wiki files directly.

### Registry

Same shape as `backends/registry.py` (TerminalBackend) and the provider manager:
a module-level dict populated at import time via `register_backend("okf",
OkfArchiveBackend)`. `get_backend(name)` raises `ValueError` on unknown names, which
the CLI maps to a `click.ClickException` and the API maps to HTTP 400.

---

## Decisions (ADR-lite)

### D1 — Shared backend seam: `MemoryArchiveBackend` ABC + registry

- **Context.** Two archive formats are on the roadmap: OKF (this issue) and the
  parked CAO-native tar.gz (`docs/memory-import-export`). The project has a hard
  Mandated rule: extensibility goes through ABCs + registries (`BaseProvider`,
  `TerminalBackend`, `CaoPlugin`) — never if/else over type names.
- **Decision.** Define `MemoryArchiveBackend` (methods `export_bundle`,
  `import_bundle` as above) in `services/memory_archive/base.py`, registered by
  format name. OKF ships as the first backend (`okf`); the parked tar.gz format
  registers later as `cao`. One CLI switch selects the backend:
  `cao memory export|import --format {okf,cao}`.
- **Consequences.** Slightly more scaffolding in the first PR (ABC + registry +
  report dataclasses for a single implementation). In exchange: the `cao` backend
  lands as a pure addition; CLI/API/service code is format-agnostic from day one;
  the security posture (secret gate, scope rules) lives in the shared seam or is an
  explicit per-backend contract, not re-invented per format.
- **Rejected: standalone `okf_export.py` module now, unify later.** Cheaper today,
  but "unify later" means refactoring a shipped CLI surface when `cao` arrives, and
  it directly contradicts the affirmed ABC+registry rule. The migration cost is
  paid exactly once either way; paying it first keeps the rule intact.

### D2 — Bundle shape: plain directory of markdown

- **Context.** The primary consumers are git repos, Obsidian vaults, Notion
  importers, and file-sync tools — all of which want loose markdown files, not an
  archive. OKF v0.1 itself specifies files-in-a-directory.
- **Decision.** The export bundle is a **plain directory**:
  - Each topic → `<key>.md` (existing sanitized key = filename) with YAML
    frontmatter. `type` is required; `title`, `description`, `tags`, `timestamp`,
    `created` are emitted when derivable. The CAO header comment is rewritten into
    frontmatter per the mapping table.
  - The **latest** `## <ISO-ts>` section becomes the body. Older sections fold
    into **`history/<key>.md`** (one history file per topic, timestamped sections
    preserved verbatim), emitted only with `--include-history`.
    *Why `history/` over a single per-topic-appended `log.md`:* a single `log.md`
    interleaves every topic's history into one file, which breaks per-topic
    content-hash idempotency (D3 — one changed topic would rewrite the shared
    log), scales poorly, and pollutes graph views. A `history/` subdirectory is
    excluded from OKF §9 conformance as a reserved path, keeps hashes per-topic,
    and lets Obsidian users simply ignore one folder.
  - `index.md` is regenerated in OKF's line form: `* [Title](key.md) - description`,
    no frontmatter. CAO's `~Ntok`/`updated:` annotations are dropped.
  - `## See Also` links pass through **on export** with path normalization: CAO's
    `../<scope_id>/<key>.md` relative form is rewritten to bundle-relative
    `<key>.md`. Links whose target key is not in the bundle (e.g. points at a
    secret-skipped topic) are kept as plain text, not links, and counted in the
    report. (Import direction: See-Also is lossy in PR 1 — see D5.)
  - **Bundle layout by scope.** For `global`, `project`, and `federated` exports,
    all topics land in one flat directory (one scope, one scope_id — no
    collisions). For `session`/`agent` exports (only reachable with
    `--include-private`, D5), two different scope_ids can hold the same key, so
    a flat directory would collide; those scopes export into a subdirectory per
    scope_id: `<scope_id>/<key>.md`, with `index.md` links pointing into the
    subdirectories. `history/` mirrors the same nesting.
  - `scope`, `scope_id`, provenance, `access_count`, uuid are **dropped** — the
    export is deliberately lossy (documented in the mapping table); full fidelity
    is the `cao` format's job.
  - `-o out.tar.gz` is an optional convenience: the same directory writer runs
    into a temp dir which is then tar'd. No separate archive code path.
- **Consequences.** Round-tripping OKF is lossy by design (scope must be
  re-supplied on import; history collapses unless `--include-history` was used).
  The directory is directly consumable by every target tool with zero unpacking.
- **Rejected: archive-first (tar.gz as the primary artifact).** An archive is
  hostile to the actual consumers (git diff, Obsidian, sync), reintroduces the
  entire tar threat model as a *mandatory* attack surface instead of an opt-in,
  and duplicates the parked `cao` format's territory. The tarball remains
  available as a wrapper flag.

### D3 — Determinism and sync-readiness

- **Context.** Maintainer ask #1 is a future `cao memory sync <dir>` that keeps an
  OKF directory continuously up to date. Sync is only cheap if export is
  idempotent; otherwise every run rewrites every file and git/Obsidian churn is
  constant.
- **Decision.** Export is **idempotent**:
  - Stable filenames — the existing sanitized key, unchanged.
  - Stable frontmatter — fixed key order (`type`, `title`, `description`, `tags`,
    `timestamp`, `created`), deterministic YAML serialization (no dict-order
    dependence), LF line endings, single trailing newline.
  - Change detection by content hash: before writing `<key>.md`, hash the
    would-be bytes and compare against the existing file; identical → skip
    (reported as `unchanged`). No mtimes or export-run timestamps are embedded
    in file content (nothing varies run-to-run for unchanged topics).
  - `--prune`: topics present in the destination directory but no longer in the
    CAO scope are deleted (reserved paths `index.md`, `manifest.md`, `history/`
    handled by the same rule keyed on their source topic). Off by default —
    deleting files in a user-supplied directory is destructive.
  - `index.md` is regenerated from the final topic set, so it is a pure function
    of the exported topics and also stable.
- **Consequences.** Re-export to the same directory is a no-op when nothing
  changed (second run rewrites zero files — a test asserts this, D7). `sync`
  becomes "loop: export with `--prune`" plus a watch trigger — designed in
  Follow-ups, **not** in this PR.
- **Rejected: naive full rewrite every export.** Simpler writer, but makes every
  export a full git diff / full Obsidian re-index, and forecloses the thin-sync
  follow-up (sync would need its own differ anyway — the differ belongs in the
  writer once).

### D4 — Graph ownership: CAO server is the single source of truth

- **Context (maintainer ask #3).** Once an OKF mirror exists in an Obsidian vault,
  users *can* edit it there. Two writable replicas of a linked knowledge graph
  need conflict resolution, edit provenance, and merge semantics CAO does not
  have.
- **Decision.** The **CAO server owns the knowledge graph** — wiki topics, their
  metadata, and the See-Also edge set (SQLite `related_keys` is authoritative;
  the rendered `## See Also` block is a projection of it). OKF export/sync
  produces a **read-only mirror** for Obsidian/Notion/dashboards. Import exists
  for **migration and ingestion** (bringing an external OKF corpus *into* CAO, or
  moving between machines), not for round-tripping edits made in the mirror.
  Documentation and the export report both state this; export may drop a
  `manifest.md` noting "generated by CAO — edits here are not synced back".
- **Consequences.** Edits made in the mirror are overwritten by the next export
  with `--prune`/hash-rewrite. This is the documented contract, not data loss.
  Dashboards and clippers get a consistent graph because there is exactly one
  writer.
- **Rejected: bidirectional editing.** Would require, at minimum: per-topic edit
  provenance in frontmatter (who/when/base-hash), three-way merge or CRDT
  semantics for concurrent CAO-side and mirror-side edits, a conflict UI, and a
  secret-gate on the *inbound* path equivalent to a full import per file. All
  deferred; if ever built, it builds on this design's import path plus a
  base-hash field in frontmatter.

### D5 — Security model

Export and import cross the memory store's trust boundary in opposite directions;
each direction gets explicit controls. (ADR-lite: the Context for all of these is
the project's affirmed security rules — shared validator reuse, no ad-hoc path
validation, secret handling by pattern name only.)

**Export = declassification boundary.**
- Every topic body (including history sections when `--include-history`) runs
  through `secret_gate.scan_for_secrets()` before writing.
- Default policy: **reject the topic, continue the export**. The `ExportReport`
  lists the topic key and the matched **pattern NAME only** (e.g.
  `aws_access_key`) — never the matched bytes, mirroring the federated-write
  logging rule in `store()`.
- `--redact`: instead of skipping, replace each match with
  `[REDACTED:<pattern_name>]` and export the topic (counted as `redacted`).
  **This requires a new public function in `secret_gate.py`** — verified:
  `scan_for_secrets()` returns only the *name of the first* matching pattern
  (no match spans, no subsequent matches), and `_SECRET_PATTERNS` is private,
  so redaction cannot be built on the existing surface without reaching into
  privates. Add `redact_secrets(content) -> tuple[str, list[str]]` (redacted
  text, names of every pattern that fired) alongside `scan_for_secrets()`,
  keeping the module's pure/no-I/O contract; `scan_for_secrets()` is unchanged.
- **Scope privacy:** export is strictly **per-scope** (one bundle per scope,
  Open Question 1), so private-scope handling is a flag-level gate, not a
  per-topic skip: `--scope session` or `--scope agent` **errors** unless
  `--include-private` is passed. These scopes hold per-session /
  per-agent-profile working state that was never meant to leave the machine;
  global/project/federated are the shareable tiers. (Consequently there is no
  `skipped_private` counter in `ExportReport` — nothing is ever silently
  skipped for privacy; the command either runs or refuses.)
- *Rejected alternative:* export-everything-and-warn. A warning after the bytes
  are on disk (and possibly already committed/synced) is not a boundary.

**Import = untrusted input.**
- `--target-scope` is **required** — OKF carries no scope, and defaulting would
  silently misfile memories. Allowed targets: `global`, `project`, `federated`.
  **Importing into `agent` scope is banned outright**, and `session` scope is
  likewise not offered (the CLI cannot resolve a meaningful session_name, and
  bulk-writing another agent's or session's private tier is exactly the
  cross-scope contamination `store()`'s guard exists to prevent). Scope is never
  auto-assigned from bundle content.
  *Rejected alternative — infer scope from a bundle `manifest.md`:* the manifest
  is human-readable provenance written by whoever produced the bundle; trusting
  it for scope assignment turns an untrusted input file into an authorization
  input (a bundle could self-declare `federated` and bypass the operator's
  intent). Explicit `--scope` keeps the routing decision with the operator.
- **`--scope project` binds to the CLI's cwd.** Project scope_id is resolved by
  `resolve_project_id()` (env/settings override → normalized git remote →
  cwd-hash fallback) from the directory the CLI runs in — the bundle has no say.
  Because this binding is implicit and easy to get wrong (importing from the
  wrong checkout misfiles every topic), the `ImportReport` — **including
  `--dry-run` output — MUST echo the resolved project id** (and scope_id in
  general), so the operator can verify the destination before/after the write.
- **Validator reuse (Mandated rule — no reimplementation):**
  - Keys: filename stem → `MemoryService._sanitize_key`; a file whose stem does
    not round-trip the sanitizer unchanged is rejected (same rule
    `_parse_related_keys` applies), not silently renamed.
  - Destination containment: all writes go through `store()`, which resolves
    paths via `get_wiki_path` (base-dir containment + traversal check).
  - User-supplied `src` (import) and `dest` (export) paths: validated with the
    shared validator from `clients/tmux.py::_resolve_and_validate_working_directory`.
    **Verified constraint:** that function is a private method of `TmuxClient`,
    validates *directories only*, and requires the path to already exist — none
    of which fits archive targets (export dest may not exist yet; `-o
    out.tar.gz` is a file). Per the Mandated rule (reuse, don't reimplement),
    this PR **extracts it to a shared module** (e.g.
    `utils/path_validation.py::resolve_and_validate_path`) with an adapted
    contract: realpath canonicalization + absolute-path guard + the same
    blocked-system-directory frozenset, applied to the **nearest existing
    ancestor** when the target doesn't exist yet; `allow_create=True` for
    export destinations (mkdir after validation); `allow_file=True` for the
    `-o out.tar.gz` target. `TmuxClient` delegates to the extracted function
    with its stricter existing-directory settings, so tmux behavior is
    unchanged (a regression test asserts this).
  - **See-Also links in imported topics: EXPORT-ONLY in PR 1 (lossy on
    import).** Verified: `store()` has no related-keys parameter, and the
    `## See Also` block is written only by the compile pipeline as a
    projection of SQLite `related_keys` — the authoritative edge store per
    D4. Round-tripping the block through `store()` is therefore not
    implementable as a body pass-through; worse, leaving the block in the
    body would embed a stale `## See Also` inside an entry section. PR-1
    behavior: parse and **strip** any `## See Also` block from the imported
    body before calling `store()`, counting dropped links in the report
    (`see_also_dropped`). This is consistent with the design's lossy story
    (scope, provenance, and uuid are already dropped). *Designed follow-up
    (not PR 1):* a post-import metadata step that collects valid
    same-bundle keys from the stripped blocks (sanitizer round-trip +
    present-in-bundle check) and writes them into SQLite `related_keys` via
    `_upsert_metadata` after all topics are stored, letting the compile
    pipeline re-render the block natively. Deferred because it writes a
    metadata column the import path otherwise never touches and needs its
    own ordering/validation tests.
  - **Body sanitization against structure spoofing.** `store()` appends the
    imported body under a `## <ts>` heading, so an untrusted body containing
    its own `## 2026-01-01T00:00:00Z` lines or `<!-- id: … | scope: … -->`
    comments would spoof entry boundaries and metadata headers for every
    later reader (`_parse_wiki_file` takes the *last* `## <ISO-ts>` match as
    the latest entry; header rewrite regexes match the first header
    comment). **Decision: escape at import time.** Any body line matching
    the timestamp-section regex (`^## \d{4}-…Z$`) or the header-comment
    regex is prefixed with a zero-width-safe escape (rendered as
    `\## …` / commented-out marker) and the file is counted in the report
    (`bodies_escaped`); the topic still imports. Rejecting the whole file
    was considered and rejected — legitimate notes quoting CAO's own format
    (docs about CAO!) would become unimportable. Silent acceptance of the
    risk is rejected outright: this is exactly the untrusted-input boundary
    D5 exists for.
- **Tar input (future, when `cao` or `-o`-produced tarballs are accepted on
  import):** mirror the parked branch's threat model — gzip-ratio cap,
  total-decompressed-size cap, per-file size cap, member-path traversal
  rejection (no absolute paths, no `..`, no symlinks/hardlinks). Caps live in
  `constants.py`. The first PR accepts **directories only**, so this is
  documented, not implemented.
- **Conflict policies** (`--conflict`, default `skip`):
  - `skip` — existing topic with the same (key, target scope) wins; file counted
    as `skipped_conflict`.
  - `replace` — `forget()` then `store()` (new uuid, fresh single-entry article).
  - `merge` — `store()` on the existing key; its upsert semantics append the
    imported body as a new timestamped `## <ts>` section (exactly what a normal
    re-store does today).
  - `--dry-run` runs the full parse/validate/secret pipeline and produces the
    `ImportReport` without calling `store()`.
- **Writes go through `MemoryService.store()`, never raw file writes.** This
  keeps SQLite metadata, `index.md` regeneration, per-topic flock, and the
  cross-scope write guard on the one code path that already implements them.
  *Trade-off:* `store()` stamps `now()` as the entry timestamp, so the OKF
  `timestamp` frontmatter is lost. Proposed resolution, in preference order:
  1. add an optional `occurred_at: Optional[datetime]` parameter to `store()`
     (used for the `## <ts>` section heading and `created_at` when the topic is
     new) — small, honest API change; or
  2. documented post-write metadata fixup (rewrite the section heading under the
     topic lock + `_upsert_metadata` with the original timestamp) — no API
     change but re-opens the file and duplicates header logic.
  The design recommends (1); final call at implementation review.

  **Ordering rule for `occurred_at` (required for correctness, not optional).**
  `store()`'s contract is append-only: the new `## <ts>` section always lands
  last, and every reader (`_parse_wiki_file`) treats the **last** timestamp as
  `updated_at` and the last section as the current content. If a merge-import
  supplies an `occurred_at` *older* than the existing topic's latest section,
  a naive append would produce a file whose section timestamps are
  out of order and — worse — whose "latest" content is the oldest entry.
  **Decision: clamp.** When `occurred_at` < the existing topic's latest
  section timestamp (or `>` now, for clock-skewed bundles), `store()` uses
  `now()` for the `## <ts>` section heading and the original OKF timestamp is
  recorded inside the entry body as a first line
  (`_Originally recorded: <ISO-ts>_`), so provenance survives without breaking
  ordering. The future-clamp (`occurred_at` > now) applies to new topics as
  well, not just merges; only in-order, non-future timestamps are used
  verbatim. New topics and in-order merges use `occurred_at` verbatim
  otherwise. The
  `ImportReport` counts clamped entries (`timestamps_clamped`).
  *Rejected alternative — insert the section in timestamp order:* preserves a
  chronologically sorted file, but requires rewriting the file's section
  structure inside `store()`, which breaks its append contract, races with the
  deferred LLM compiler's `expected_content` token, and changes latest-entry
  semantics for every existing caller. Reordering history is the compiler's
  job, not `store()`'s.
- Import also runs `scan_for_secrets()` on inbound bodies when the target scope
  is `federated` (parity with `store()`'s existing federated gate — which will
  fire anyway since we call `store()`; the archive layer just reports it as a
  per-file rejection instead of aborting the whole import).

### D6 — Surfaces

- **Context.** The project rule says the HTTP API is the single integration seam
  for MCP; the brief asks whether the memory CLI follows that. **Verified:**
  `cli/commands/memory.py` constructs `MemoryService()` and calls it directly for
  every existing command; the HTTP mirror in `api/main.py` (issue #286) exists for
  the web UI, not the CLI. The pattern to follow for CLI export/import is
  therefore **direct MemoryService calls**, consistent with its siblings.
- **Decision.**
  - CLI:
    ```
    cao memory export --format okf --scope <s> [-o DIR|FILE.tar.gz]
                      [--include-private] [--include-history] [--redact] [--prune]
    cao memory import --format okf --scope <s> PATH
                      [--conflict skip|replace|merge] [--dry-run]
    ```
    (`--scope` on import is the required `--target-scope`; reusing the flag name
    `--scope` keeps the CLI consistent with every sibling command. Choices on
    import: `global|project|federated` only, per D5.)
  - HTTP: `GET /memory/export?format=okf&scope=<s>` returning a **tar stream**
    (`application/gzip`) built by the same directory writer into a temp dir — a
    read-only GET surface for the dashboard / clipper follow-up. Errors map to
    `HTTPException` at the boundary per project rule. **POST import API is
    deferred** (Follow-ups): accepting uploaded archives server-side pulls the
    entire tar threat model into the first PR for no current consumer.
  - Service: `MemoryService.export_memories(fmt, …)` / `import_memories(fmt, …)`
    are thin delegators — resolve the backend from the registry, inject `self`,
    call it. No format logic in `MemoryService`.
- **Consequences.** One writer serves CLI dir export, CLI tar export, and the
  HTTP stream. The CLI stays consistent with its file (direct service), the API
  surface stays consistent with #286's read-only mirror philosophy.
- **Rejected: routing the CLI through the HTTP API.** It would honor the
  "single seam" rule as written, but contradict the verified, established pattern
  of every other `cao memory` command, and make export require a running
  `cao-server` for a purely local filesystem operation.

### D7 — Testing plan

(Locked as a decision so tests ship in the same commit as the implementation,
pytest, per team rules. CI gate: `uv run black --check src/ test/` + isort must be
clean before the PR.)

| # | Test | Asserts |
|---|---|---|
| 1 | Round-trip, global + project scope | export → every non-reserved `*.md` parses YAML frontmatter with non-empty `type` (OKF §9 conformance) → import into a **fresh `MEMORY_BASE_DIR`** (tmp_path + injected base_dir) under explicit `--scope` → topics recallable via `recall`/`cao memory list` with matching content |
| 2 | Secret-gate regression | topic containing a planted `AKIA…` key: default export **skips** it; `ExportReport` carries `aws_access_key` (pattern name) and **no content bytes anywhere in report or logs**; `--redact` exports with `[REDACTED:aws_access_key]` |
| 3 | Private-scope gate | `export --scope session` / `--scope agent` without `--include-private` errors (whole command, nothing written); succeeds with the flag, topics nested per scope_id (D2 layout) |
| 4 | Idempotent re-export | second export into the same dir rewrites **zero** files (hash/mtime assertion); after deleting one CAO topic, `--prune` removes exactly that file |
| 5 | Conflict-policy matrix | existing key × {skip, replace, merge} × {dry_run on/off}: skip leaves file+SQLite untouched; replace yields fresh single-entry article; merge appends a new `## <ts>` section; dry_run mutates nothing while reporting all outcomes |
| 6 | Traversal-attack import fixture | bundle containing `../evil.md`-style names and a topic whose stem fails sanitizer round-trip: rejected with report entries; a See-Also block with a bundle-escaping link is stripped like any other (test 11); nothing written outside `MEMORY_BASE_DIR` |
| 7 | Unknown frontmatter keys tolerated | topic with extra OKF/Obsidian keys (`aliases`, `cssclass`, …) imports cleanly; unknown keys ignored, not errors |
| 8 | Missing required `type` | frontmatter without `type` → per-file rejection (report), import continues |
| 9 | Store-path integrity | imported topics have SQLite metadata rows and `index.md` entries (proof the writes went through `store()`) |
| 10 | Timestamp preservation + clamp | new-topic import: `## <ts>` heading and `created_at` reflect the OKF `timestamp` frontmatter (latest-section semantics per mapping table); merge-import with `timestamp` older than the existing latest section: heading clamps to now(), original ts appears in the entry body, `timestamps_clamped` counted (D5 ordering rule) |
| 11 | See-Also stripped on import | imported topic containing a `## See Also` block: block absent from the stored body, links counted as `see_also_dropped`, no `related_keys` written |
| 12 | Body spoofing escaped | imported body containing a fake `## <ISO-ts>` line and a fake `<!-- id: … -->` header: lines escaped per D5, `_parse_wiki_file` still reports the real entry as latest, `bodies_escaped` counted |
| 13 | Report echoes resolved scope | `import --scope project --dry-run` output contains the `resolve_project_id`-resolved project id (D5) |
| 14 | Validator extraction regression | extracted shared path validator: tmux working-directory behavior unchanged (existing-dir required, blocked dirs rejected); archive mode accepts a not-yet-existing export dest (nearest-ancestor validation) and a `-o out.tar.gz` file target |

Plus: `ValueError` from `get_backend("nope")` surfaces as CLI error / HTTP 400;
export of an empty scope produces a valid empty bundle (index.md only).

- **Consequences.** The suite locks D5's security decisions (gate, escape, clamp,
  scope echo) as regression tests, at the cost of a larger first-PR test surface
  (~14 test areas for one backend).
- **Rejected: test-after in a follow-up PR.** Violates the team's affirmed
  same-commit testing rule, and security-boundary behaviors (tests 2, 6, 12)
  are precisely the ones that must not ship unverified.

---

## Security section (summary of D5 for reviewers)

- Outbound: secret_gate scan on every body; skip-by-default, `--redact` opt-in
  (via a new `redact_secrets()` in `secret_gate.py`); pattern names only in
  reports/logs; `--scope session|agent` errors without `--include-private`.
- Inbound: explicit target scope (global/project/federated only; agent banned;
  project binds to `resolve_project_id(cwd)`, echoed in the report);
  filename-stem sanitizer round-trip; body-structure spoofing (fake `## <ts>` /
  header comments) escaped at import; `## See Also` blocks stripped
  (export-only in PR 1); all writes via `store()` (containment via
  `get_wiki_path`, flock, SQLite+index consistency, federated gate); shared
  path validator extracted from `clients/tmux.py` for `src`/`dest`; tar
  hardening documented for the future tar-input path, directories-only in PR 1.
- No new long-lived state, no credentials, no network. The HTTP surface is
  read-only GET.

---

## Follow-ups (explicitly out of scope for the first PR)

1. **`cao memory sync <dir>`** (haofei ask #1). Thin loop over the idempotent
   exporter. Proposed signature:
   ```
   cao memory sync --format okf --scope <s> DIR
                   [--interval 60s | --once] [--prune] [--redact]
   ```
   `--once` = one export pass (equivalent to `export --prune`); `--interval`
   re-runs on a timer (or, later, on memory-write events via the event bus).
   Sync is export-only per D4 — it never reads mirror edits back.
2. **Live knowledge-graph view** (haofei ask #2): the OKF directory (or the
   `GET /memory/export` stream) consumed by the Obsidian web clipper / CAO
   dashboard to render the topic + See-Also graph. Depends on sync for liveness.
3. **POST /memory/import API** — needs the tar threat-model implementation
   (caps in `constants.py`) plus an authz story for a mutating endpoint.
4. **Bidirectional editing** — rejected in D4; would require base-hash
   frontmatter, three-way merge, and inbound secret gating. Recorded here so the
   requirements are not lost.
5. **`cao` archive backend** — revive the parked `docs/memory-import-export`
   branch as the second registry entry.
6. **See-Also ingestion on import** — post-import metadata step (designed in
   D5): collect valid same-bundle keys from stripped `## See Also` blocks and
   write them into SQLite `related_keys` via `_upsert_metadata`, letting the
   compile pipeline re-render the block. Export-only is the PR-1 contract.

---

## Open questions for maintainer triage

1. **Multi-scope merged bundles.** Should one bundle ever contain topics from
   multiple scopes? **Recommendation: no — one bundle per scope**, because OKF
   has no scope field and a merged bundle cannot be re-imported without
   inventing per-file scope metadata (which would be a CAO extension, defeating
   the point of a standard format). A top-level `manifest.md` (reserved name)
   records which scope/scope_id the bundle was exported from, purely as
   human-readable provenance — import never trusts it for scope assignment
   (D5). Users who want "everything" run one export per scope into sibling
   directories.
2. **`store(occurred_at=…)` parameter vs post-write fixup** for timestamp
   preservation (D5) — design recommends the parameter; needs a maintainer nod
   since it touches `store()`'s signature.
3. Should `--include-history` history files carry frontmatter (making them OKF
   topics) or stay frontmatter-free under the reserved `history/` path
   (recommended — keeps §9 conformance checks trivial)?
