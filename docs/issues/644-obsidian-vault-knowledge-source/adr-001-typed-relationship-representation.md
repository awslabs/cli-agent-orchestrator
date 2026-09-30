# ADR-001: Canonical representation for typed relationships and lifecycle metadata

**Issue:** [#644](https://github.com/awslabs/cli-agent-orchestrator/issues/644)
**Status:** Proposed. **Unchanged in revision 3** — no ruling touched it. Revision 2 added the read/write charset asymmetry and the derived-key/graph-exposure coupling.
**Decision owner:** whoever approves the issue's open question "the canonical CAO-namespaced representation needs to be selected"
**Related:** [design.md](design.md), [adr-003-read-and-write-surface.md](adr-003-read-and-write-surface.md), [adr-006-identity-and-change-detection.md](adr-006-identity-and-change-detection.md)

---

## Context

A standard Obsidian wikilink `[[Other Note]]` carries a target and nothing else. It
cannot express any of the things CAO's shipped relationship store already models
(`src/cli_agent_orchestrator/services/memory_relationship_service.py`):

- edge **type** beyond untyped association — `contradiction` and `supersedes` are
  in `VALID_TYPES` at line 47 alongside `relates_to`
- **status** — `VALID_STATUSES` at line 48 is `active`, `proposal`, `rejected`,
  `superseded`, `deleted`, with `rejected` and `deleted` treated as
  operator-terminal in `CURATION_TERMINAL_STATUSES`
- **confidence** — a validated real in `[0, 1]`, or `NULL` meaning no evidence,
  which the service explicitly refuses to coerce to zero
- **provenance** — `VALID_ORIGINS` at line 55 distinguishes `compiler`,
  `wiki_lint`, `human`, `legacy_related_keys` and `external_import`
- **stable identity** independent of the note's filename

The issue's Ownership Model says vault Markdown is canonical and that "a documented
CAO-namespaced frontmatter schema or managed sidecar is canonical" for exactly this
metadata, leaving the choice open. The choice determines what a human edits, what
survives a rebuild, and what CAO is allowed to write.

Two hard constraints bound the answer. Non-goal: no unrestricted writes to
arbitrary existing vault notes. AC8: normal memory operations never modify an
unmanaged vault note. So whatever the canonical home is, CAO can only *write* it
for notes inside the managed folder.

A third constraint is human: Obsidian renders and edits YAML frontmatter natively
in its Properties view. Anything outside the note file is invisible to the person
whose vault it is unless they install something — and requiring an Obsidian plugin
is a stated non-goal.

## Decision

**A single reserved top-level YAML frontmatter key, `cao`, inside the note itself
is the canonical representation. There is no sidecar file. Edge authorship is
source-endpoint-local: an edge is canonical only in the frontmatter of its source
note.**

### The schema

```yaml
---
title: Memory Design                 # user-owned, preserved byte-for-byte
tags: [design, memory]               # user-owned, preserved; read for CAO tags
aliases:                             # user-owned, preserved; read for link resolution
  - Memory Design
cao:
  key: obsidian-memory-design        # canonical CAO key, ^[a-z0-9-]{1,60}$
  type: project                      # MemoryType: user|feedback|project|reference
  managed: true                      # CAO may rewrite this note
  created: 2026-08-01T09:12:00Z      # optional; else derived, see ADR-006
  links:
    - to: retrieval-path             # a cao.key, or "[[Note]]" resolved like any wikilink
      type: contradiction            # VALID_TYPES
      status: proposal               # VALID_STATUSES, default active
      confidence: 0.8                # optional; omitted means no evidence, never 0
      origin: human                  # VALID_ORIGINS, default human
---
```

### Rules

1. **`cao` is the only key CAO writes.** Every other top-level frontmatter key is
   user-owned: parsed, preserved in original order, re-emitted byte-for-byte on a
   managed-note rewrite. `tags` and `aliases` are *read* (tags feed
   `normalize_memory_tags`, aliases feed link resolution) and never rewritten.
2. **Values validate against the shipped closed taxonomies.** `cao.links[].type`,
   `.status` and `.origin` are checked against `VALID_TYPES`, `VALID_STATUSES` and
   `VALID_ORIGINS` from `memory_relationship_service.py` — imported, not
   re-declared, so the vocabularies cannot drift. `confidence` goes through the
   same `[0, 1]` validation. An invalid value quarantines the note with an
   `invalid_cao_block` finding; it is never coerced to a default.
3. **Edges are authored at their source endpoint.** A `contradiction` from note A
   to note B lives in A's `cao.links`. B's frontmatter is untouched. This is what
   makes the schema compatible with AC8: CAO writes an edge only when the source
   note is managed.
4. **CAO-derived edges about unmanaged notes are derived state, not canonical.**
   An edge the compiler or `wiki_lint` infers between two unmanaged notes lives
   only in `memory_relationships` with its producer's `origin`, and is rebuilt by
   the next reconcile. If the human wants it canonical they add it to the source
   note's `cao` block themselves, in Obsidian. This is a consequence of the
   non-goal, not a gap in the schema, and it is stated rather than implied.
5. **A plain wikilink in the body remains a canonical `relates_to` edge**, exactly
   as the issue's Ownership Model says, projected with the new
   `origin = "vault"`. `cao.links` is additive: it expresses what a body link
   cannot, and never suppresses one.
6. **Parsing is bounded and safe.** Frontmatter is byte-capped before parsing,
   parsed with a safe loader, and rejected outright if it uses YAML anchors or
   aliases (`&`/`*` node syntax). `cao.links` is capped at
   `MAX_EDGES_PER_MUTATION` (64, already the service's bound) so a single note
   cannot exceed one mutation's worth of edges.
7. **`cao` values are data, never instructions.** No `cao` field is ever
   interpolated into a prompt. Only closed-taxonomy values, a validated key, a
   validated timestamp and a bounded numeric reach any downstream consumer.

### Enumerator changes this requires

Adding `origin = "vault"` touches three places, all found by grep and all listed so
none is missed:

- `src/cli_agent_orchestrator/services/memory_relationship_service.py:55` —
  `VALID_ORIGINS`
- `src/cli_agent_orchestrator/clients/database.py:169` — the enumerating comment on
  `MemoryRelationshipModel.origin`
- `docs/memory.md:301` — the documented origin list

It must **not** touch `_backfill_legacy_related_keys` in `database.py`, whose source
text is hashed by a drift guard at
`test/clients/test_memory_relationships_migration.py:119`. No new `EdgeType` is
introduced, so `cao_mcp_apps/src/graph/types.ts:13` (`EdgeTypeValue`) and the web
graph tests are untouched.

## Consequences

**Positive.**

- The human edits one file. Obsidian's Properties view renders `cao` natively; no
  plugin, no hidden directory, no second file to keep in step.
- One file means one parse and one `content_sha256`, which is what makes
  deterministic rebuild tractable (ADR-006). A sidecar would need its own hash,
  its own staleness rule, and a rule for what happens when the two disagree.
- A note carries its own identity, so it survives an Obsidian rename with no CAO
  state at all — the strongest form of the ADR-006 guarantee.
- `git diff` on the vault shows the edge change next to the content change that
  motivated it. A sidecar splits that across two files.
- Reuses the shipped taxonomies by import, so the frontmatter vocabulary and the
  SQLite vocabulary cannot diverge.

**Negative, and accepted.**

- **Asymmetry.** CAO cannot record a canonical edge whose source is an unmanaged
  note. Derived edges still work and still project into the graph; they are simply
  not canonical and are re-derived on rebuild. This is the direct cost of AC8 and
  is unavoidable under any in-note representation.
- **Frontmatter is untrusted input on the query path.** Mitigated by the byte cap,
  the safe loader, the anchor/alias rejection, closed-taxonomy validation and the
  never-in-a-prompt rule — but it is a real new parsing surface and unit
  `vault-parse` is flagged security-sensitive for that reason.
- **A user who hand-edits `cao` badly quarantines their own note.** Deliberate:
  the alternative is guessing at a scope or a type, and scope is a security
  boundary. `cao memory vault status` names the note and the finding code so the
  fix is obvious.
- **Frontmatter merge is fiddly.** Preserving arbitrary user keys, comments and
  ordering across a rewrite is harder than replacing a whole file. Comments in
  particular cannot be preserved through a parse-and-re-emit cycle by
  `python-frontmatter`. Mitigation: the writer re-emits the original frontmatter
  **text region** verbatim except for the `cao` block, rather than round-tripping
  the whole mapping through YAML. A round-trip test asserts a note with comments,
  unusual key order and plugin inline fields is byte-identical after a `cao`-only
  rewrite.

## Alternatives rejected

### A. Managed sidecar file, for example `<managed>/.cao/edges/<note_uid>.yaml`

Rejected. It creates a second file that can disagree with the note it describes,
which is the two-replicas hazard the issue's own Non-Goals list. It is invisible in
Obsidian, so the human cannot curate a `proposal` edge or a confidence value
without leaving their tool — and "requiring an Obsidian plugin" is a non-goal, so
there is no sanctioned way to surface it. Keyed by path it orphans on rename; keyed
by uid it becomes unfindable by a human. It does buy one real thing — the ability
to record a canonical edge about an unmanaged note without writing to it — and that
is the one advantage this ADR gives up. The trade is accepted because such edges
are derivable and rebuildable, whereas a desynchronized sidecar is neither
detectable nor repairable.

### B. One relationship index note per mapped folder, for example `_cao-links.md`

Rejected. Every edge write in the folder contends on one file, so concurrent
Obsidian edits and CAO writes collide constantly, and a cloud-sync conflict copy of
that one file loses the whole folder's edges at once. It is a third replica of
information that already exists in the note bodies and in SQLite.

### C. Body-embedded syntax, for example a Dataview inline field
`contradicts:: [[Other]]`

Rejected. It depends on a specific community plugin's syntax, which this design
explicitly declines to interpret (ADR-004). It also puts structured metadata in the
prose that BM25 indexes, polluting ranking with taxonomy tokens.

### D. Keep typed edges only in SQLite and treat them as fully derived

Rejected as the *canonical* answer, though it remains the behavior for edges about
unmanaged notes. As a general rule it fails the Ownership Model: a human-curated
`proposal` edge or a hand-set confidence would be destroyed by the next
`cao memory vault rebuild`, since rebuild deletes and re-derives all
`origin = "vault"` rows. Human curation must live somewhere canonical, and in a
vault-first design that means in the vault.

## Security and compliance implications

- **New untrusted-parsing surface.** Vault frontmatter is authored outside CAO and
  may be synced from another machine. Controls: byte cap before parse, safe loader,
  rejection of YAML anchors and aliases (this repository has already shipped a YAML
  denial-of-service fix, so expansion bombs are a known-live class), bounded
  `cao.links` length, and quarantine-on-invalid rather than coerce-to-default.
- **Prompt injection.** A note's body is prose an agent may read, and its
  frontmatter is metadata. No `cao` value is ever interpolated into a prompt, and
  every value that reaches a consumer is either from a closed taxonomy, a validated
  slug, a validated timestamp, or a bounded number. Whether the note's *body* may
  reach an agent's context automatically is a separate policy decided in
  [adr-005-index-vs-injection-policies.md](adr-005-index-vs-injection-policies.md).
- **Confidentiality.** The schema adds no field that could carry a secret out of
  the vault. Findings and audit events carry codes, counts and relative paths only,
  matching the content-free rule the relationship service's `NFR-1.7` already
  enforces.
- **Write blast radius.** Because only the `cao` key is ever rewritten and only in a
  managed note, the worst case of a writer bug is a corrupted `cao` block in a file
  CAO created. A test plants a crafted `cao.key` containing traversal characters and
  asserts the key is rejected by the existing `_sanitize_key` charset rather than
  becoming a path. Note the asymmetry this relies on: a vault note's **path** is
  read-confined by an inline `realpath` plus `startswith` guard with no charset
  restriction (so real folder names work), while a `cao.key` that becomes a **write**
  target is `[a-z0-9-]` by construction — so the write surface stays a strict subset
  of the read surface. See
  [adr-007-configuration-surface.md](adr-007-configuration-surface.md) rules 7 and 8.
- **Derived-key readability is an incentive, not a defect.** A note without `cao.key`
  gets a path-derived key carrying a stable digest suffix
  ([adr-006-identity-and-change-detection.md](adr-006-identity-and-change-detection.md)),
  which is deliberately less pretty than an authored one. Because `Node.label` is the
  key, a path-derived key also encodes folder structure, which is why the graph
  projection may only publish keys for notes recall can already reach — see the
  graph-exposure rule in [design.md](design.md).
- **Auditability.** Because the canonical record is a text file in the user's own
  version-controlled or synced vault, an edge's history is inspectable by the human
  with tools they already have — a stronger compliance story than a row in a
  database only CAO can read.
