# ADR-004: The supported Markdown and Obsidian boundary

**Issue:** [#644](https://github.com/awslabs/cli-agent-orchestrator/issues/644)
**Status:** Proposed. **Revision 3** — adds the index-time secret row (rulings R5 and R14). Revision 2 added four rows (hardlink, root-escape, non-UTF-8, derived-key collision) and answered the charset question explicitly.
**Decision owner:** the issue's open question "the supported boundary for aliases, embeds, heading links, attachments, duplicate titles, and plugin metadata must be documented"
**Related:** [design.md](design.md), [adr-001-typed-relationship-representation.md](adr-001-typed-relationship-representation.md), [adr-006-identity-and-change-detection.md](adr-006-identity-and-change-detection.md)

---

## Context

Obsidian's Markdown dialect and its plugin ecosystem are far larger than any
first-release parser should attempt. AC6 sets the bar: "Resolvable wikilinks appear
as graph relationships, with malformed or ambiguous links reported rather than
silently misresolved." The operative word is *reported*. A first release is allowed
to not support a construct; it is not allowed to guess.

The concrete constructs the issue names — aliases, embeds, heading links,
attachments, duplicate titles, plugin metadata — each fail differently:

- an **alias** is a legitimate resolution input that must never become identity
- an **embed** (`![[Note]]`) asks whose bytes the indexed content is
- a **heading link** (`[[Note#Heading]]`) targets a sub-note anchor CAO has no
  model for
- an **attachment** is not prose and must not be indexed or become an edge
- **duplicate titles** are the ordinary Obsidian condition that makes bare
  basename links genuinely ambiguous
- **plugin metadata** (Dataview inline fields, Templater syntax, Kanban boards,
  Excalidraw payloads) is syntax CAO must preserve without interpreting

## Decision

**Every construct is classified into exactly one of three buckets — supported,
degraded-with-a-finding, or refused-with-a-finding — and every non-supported
construct emits a typed finding code that surfaces in
`cao memory vault status --format json`. Nothing is dropped without a code.**

The classification is data, not scattered conditionals: it lives in
`vault/findings.py` as the single table below, so the parser, the link resolver, the
status view and the documentation cannot drift.

### The boundary table

| Construct | Classification | Behavior | Finding code |
| --- | --- | --- | --- |
| `[[Note]]` | Supported | `relates_to` edge, `origin = "vault"` | — |
| `[[Note\|Display]]` | Supported | Same edge; display text discarded | — |
| `[[folder/Note]]` | Supported | Path-qualified match, preferred over basename match | — |
| `[[Note#Heading]]` | Degraded | Note-level edge; heading recorded in `attributes_json` as `{"fragment": "Heading"}`; not resolved to a block | `heading_fragment_ignored` (info) |
| `[[Note#^blockid]]` | Refused | No edge | `block_reference_unsupported` (info) |
| `![[Note]]` embed | Degraded | `relates_to` edge with `{"embed": true}`; embedded body **not** inlined into indexed content | `embed_not_inlined` (info) |
| `![[image.png]]` and any non-`.md` embed | Refused | No edge, not indexed | `attachment_ignored` (info) |
| `aliases:` frontmatter | Supported, resolution only | Read as link-resolution input; **never** identity, never a CAO key | — |
| Alias matching two notes in one scope | Refused | No edge | `alias_ambiguous` (warn) |
| Duplicate note basenames | Supported for identity, refused for bare links | `cao.key` (or the path-derived fallback) disambiguates identity; a bare `[[Basename]]` with more than one candidate produces no edge; a path-qualified link still resolves | `link_ambiguous` (warn) |
| Duplicate `cao.key` in one scope | Refused, both notes | Both quarantined, neither indexed | `key_collision` (error) |
| Link to a note in no mapped folder or an excluded path | Refused | No edge | `link_excluded` (info) |
| Link to a nonexistent note | Refused | No edge | `link_dangling` (info) |
| Inline Markdown link whose destination is a relative path ending `.md` | Supported | Treated as a wikilink to that relative path | — |
| Markdown link to `http(s)://` | Refused | No edge, no fetch, ever | — |
| Malformed YAML frontmatter | Refused | Note quarantined; body not indexed | `frontmatter_malformed` (error) |
| Frontmatter using YAML anchors or aliases | Refused | Note quarantined before expansion | `frontmatter_unsafe` (error) |
| Frontmatter over the byte cap | Refused | Note quarantined | `frontmatter_too_large` (error) |
| Invalid value in the `cao` block | Refused | Note quarantined; never coerced to a default | `invalid_cao_block` (error) |
| `cao.key` failing `^[a-z0-9-]{1,60}$` | Refused | Note quarantined | `key_invalid` (error) |
| Note over `max_note_bytes` | Refused | Not indexed | `note_too_large` (warn) |
| Dataview inline fields `field:: value` | Not interpreted | Left in the body; indexed as prose; preserved byte-for-byte on a managed rewrite | — |
| Templater, Kanban and similar in-body plugin syntax | Not interpreted | Same as above | — |
| `*.excalidraw.md`, or frontmatter carrying `excalidraw-plugin` | Refused | Not indexed | `plugin_format_excluded` (info) |
| `.canvas` files | Refused | Not a `.md` file; never a candidate | — |
| `.obsidian/`, `.trash/`, `.git/`, `_cao-*` | Refused | Never scanned; not configurable | — |
| Any symlinked path component below the root | Refused | Not indexed | `symlink_refused` (error) |
| Sync-conflict filenames (`*.sync-conflict-*`, `* conflicted copy *`, `*.icloud`, `.~lock.*`) | Refused | Never a candidate | `sync_artifact_skipped` (info) |
| Two paths differing only by case, on a case-insensitive filesystem | Refused, both | Both quarantined | `path_case_collision` (error) |
| A candidate whose `st_nlink > 1` (hardlink) | Refused unless the mapping sets `allow_hardlinks` | Not indexed | `hardlink_refused` (warn) |
| A `memory_metadata.file_path` that does not resolve under the vault root | Refused at the sink | Row skipped, recall continues | `path_escapes_root` (warn) |
| Note whose bytes are not valid UTF-8 | Refused | Quarantined, never lossily decoded | `note_not_utf8` (error) |
| Note body matching a `secret_gate` pattern, at reconcile | Refused under `secret_gate: "reject"` (the **default**); reported-only under `"warn"` | `reject` quarantines; `warn` indexes and records the finding. **The finding is reported in BOTH modes** — the mode changes whether the note is indexed, never whether the operator is told. The finding carries the matched **pattern name** only, never the matched bytes | `secret_detected` (error under `reject`, warn under `warn`) |
| Two distinct paths producing the same derived key | Refused, both | Both quarantined; `detail` flags `derived` to distinguish it from an authored duplicate | `key_collision` (error) |
| Note whose size or mtime changes across the stability check | Deferred | Skipped this run; the prior row is left intact | `unstable_skipped` (warn) |

### Rules that generalize the table

1. **Refuse rather than guess.** Every ambiguous case yields no edge, not a chosen
   edge. `link_ambiguous` and `alias_ambiguous` are the two places a first release
   would be most tempted to pick the first match; both are refusals.
2. **Identity is never inferred from a display name.** Aliases and heading text
   affect resolution and nothing else. This is what keeps
   [ADR-006](adr-006-identity-and-change-detection.md)'s identity story stable when
   a user renames a display alias.
3. **Not-interpreted is not the same as not-preserved.** Plugin body syntax is
   indexed as prose and re-emitted byte-for-byte by the managed-note writer. A
   round-trip test asserts a note containing Dataview inline fields, comments and
   unusual frontmatter key order is byte-identical after a `cao`-block-only rewrite.
4. **Findings have severities and severities have meanings.** `info` is expected
   and normal (a dangling link in a working vault is not a defect). `warn` means a
   note is partly unusable. `error` means the note is quarantined. `status` reports
   counts per code so a user can see "412 info, 3 warn, 0 error" and act
   proportionally.
5. **A refused note is quarantined, not silently omitted.** `vault_note.status =
   'quarantined'` keeps a row so the note is visible in `status` and so a later
   reconcile can clear it. An omitted note would be indistinguishable from a note
   that does not exist. **A quarantined note is never projected into the graph**,
   because `Node.label` is the key and a path-derived key encodes folder structure, so
   publishing its node would expose the structure and title of a note that is
   deliberately not recallable. See the graph-exposure rule in [design.md](design.md).
6. **No network, ever.** An `http(s)://` link is never fetched and never becomes an
   edge. Stated explicitly because a link resolver is exactly the component someone
   would later be tempted to make "helpful".
7. **There is no charset-based rejection of a note path, so there is no
   `path_charset_refused` row.** Revision 1 implied one by specifying
   `safe_join_under_base` for note paths, whose per-segment `\A[A-Za-z0-9._-]+\Z`
   check would have refused a folder named `CAO Design` or `Références` — that is,
   most real vaults — and rule 5 would then have obliged a finding code for the
   refusal. Revision 2 removes the cause rather than adding the code: note paths are
   confined by `os.path.realpath` plus a single positive `startswith(root + os.sep)`
   guard inline beside each filesystem sink, which needs no assumption about
   characters. The one surviving charset rule applies to `managed_folder` only and is
   a **config-load error**, not a per-note finding, because it concerns a folder CAO
   creates rather than a note the user wrote. The surviving per-note *path* refusals
   are `symlink_refused`, `hardlink_refused`, `path_escapes_root` and
   `path_case_collision`, all rowed above.
8. **A refusal that only a hostile or corrupted input can trigger still gets a row.**
   `path_escapes_root` cannot arise from a well-formed vault — it needs a poisoned or
   stale `memory_metadata` row — and it is rowed anyway, because the alternative is a
   guard whose firing is invisible. Its teeth test is named in
   [test-strategy.md](test-strategy.md).
9. **`secret_detected` is the one refusal driven by a heuristic rather than a parse
   result, and that changes what the row has to promise.** Every other refusal in this table
   is decidable: a symlink either is one or is not, YAML either parses or does not.
   `scan_for_secrets` is an ordered deny-list of six regexes, one of which is
   `(?i)(?:password|passwd|secret|pwd)\s*[:=]\s*\S{6,}` — so a runbook quoting
   `password: hunter2` as an example, or a design note about credential handling, matches.
   **In a knowledge vault that is ordinary content, not a mistake, and it is the concrete
   false-positive class that justifies the escape hatch** rather than making it a
   weakening. Three consequences follow.

   First, the severity is **mapping-dependent** rather than fixed, which no other row in
   this table is: `error` and quarantine under `secret_gate: "reject"` (the shipped
   default), `warn` and indexed under `"warn"`. `findings.py` must therefore model this
   one code's severity as a function of configuration rather than a constant.

   Second, **the finding is reported in both modes** (ruling R14). The mode governs
   indexing, never disclosure. A `warn` mapping that silently indexed a secret-bearing note
   would be the worst of both designs — the exposure of `warn` with the blindness the
   default was chosen to avoid.

   Third, the finding must name the **pattern**, never the match, so
   `cao memory vault status` output stays safe to paste into an issue — the same
   content-free rule `scan_for_secrets`'s own docstring imposes on its callers.

   **Rejected alternatives** (ruling R14), recorded because this row's shape is a
   confidentiality decision rather than a parsing one:

   - **Always quarantine, no override.** Simplest rule and the strongest guarantee, and it
     leaves a vault that documents credential handling partly unrecallable with no
     supported remedy short of editing the user's own notes — which the non-goals forbid
     CAO from doing and which is an unreasonable thing to ask of a vault owner.
   - **`warn` by default, `reject` opt-in.** Inverts the reason AC10 was flagged in the
     first place. The insecure posture must not be what you get without reading the
     documentation.
   - **Per-pattern confidence policy** — always quarantine on the high-confidence patterns
     (`aws_access_key`, `pem_private_key`) and warn-only on the loose `password:` regex.
     The most precise answer and **the natural future refinement**, explicitly deferred
     rather than dismissed. It requires classifying all six patterns by confidence and then
     maintaining that classification, which makes the classification itself a
     security-relevant review surface — a cost worth paying later, with evidence about
     which patterns actually misfire, and not worth guessing at now.

## Consequences

**Positive.**

- AC6 is satisfied by construction, and satisfiable by a test: for each row in the
  table there is a fixture note and an assertion on the finding code.
- The boundary is publishable. The table goes into user documentation verbatim, so
  a user can predict what CAO will do with their vault without reading code.
- Because the table is data in one module, adding support for a construct later is
  a row change plus a resolver arm, and the documentation regenerates from the same
  source.
- Quarantine-with-a-code turns "CAO ignored my note" from a support question into a
  one-command answer.

**Negative, and accepted.**

- **Degraded heading links may surprise.** A user who wrote `[[Design#Read path]]`
  meaning a specific section gets a note-level edge. The fragment is preserved in
  edge attributes so a later release can resolve it, and the `info` finding makes
  the degradation visible.
- **Embeds are not content.** A user who composes a note primarily out of `![[…]]`
  transclusions will find its indexed body nearly empty. This is deliberate:
  inlining raises an ownership question (whose bytes are these?), an unbounded
  transitive-expansion risk, and a duplication problem for BM25, where the same text
  would be scored twice. Documented, with a `status` count so it is measurable.
- **Ambiguity produces silence in the graph.** Two notes named `Design.md` mean
  every bare `[[Design]]` in that scope yields no edge at all. The finding names the
  candidates, and the remedy — add `cao.key`, or use a path-qualified link — is in
  the finding's help text. Choosing one candidate would be worse: it would be wrong
  half the time and invisible always.
- **The table is long.** That is the cost of the criterion. A short table would mean
  undocumented behavior somewhere.

## Alternatives rejected

### A. Best-effort resolution — pick the first match, ignore what does not parse

Rejected. It is a direct violation of AC6's "reported rather than silently
misresolved". Its failure mode is the worst available: an edge that looks correct,
in a graph a human trusts, pointing at the wrong note. Silent misresolution is
undetectable without comparing the graph to the vault by hand.

### B. Strict mode only — refuse any note containing an unsupported construct

Rejected. It fails AC3 in practice. Real vaults contain embeds, attachments and
plugin syntax in most notes, so a strict parser would quarantine most of a real
vault and make existing knowledge unrecallable — the exact problem the issue exists
to fix. The three-bucket classification exists so that a note with one unsupported
construct still contributes its prose.

### C. Delegate parsing to an Obsidian-compatible library or an Obsidian plugin

Rejected. Requiring an Obsidian plugin is a stated non-goal. A third-party
Obsidian-dialect library would be a new dependency on the untrusted-input path with
its own parsing-bounds and denial-of-service posture, when the constructs actually
needed are a bounded frontmatter parse (`python-frontmatter`, already a direct
dependency) and a wikilink regex. It would also hand the boundary definition to
someone else's release cadence, when the boundary is precisely what AC6 requires
CAO to document.

### D. Interpret Dataview inline fields as structured metadata

Rejected. It is the single most requested-looking option and the most expensive:
`field:: value` has no closed vocabulary, so interpreting it means accepting
arbitrary keys from untrusted input into CAO's metadata model, and it puts taxonomy
tokens into the body text BM25 ranks. Preserving them as prose costs nothing and
loses nothing that `cao.links` does not already express.

## Security and compliance implications

- **The boundary is a denial-of-service boundary.** `frontmatter_too_large`,
  `frontmatter_unsafe`, `note_too_large` and the `cao.links` cap exist so a single
  crafted or corrupted note cannot exhaust memory or CPU during a reconcile. YAML
  anchor and alias rejection specifically addresses expansion bombs; this
  repository has already shipped one YAML denial-of-service fix, so the class is
  live rather than theoretical.
- **Refusing symlinked components is a confinement control.** A note that is a
  symlink to a file outside the vault would otherwise let a mapped folder read
  arbitrary filesystem content while appearing to be a vault note. Reuses
  `memory_reconciliation._first_symlink_component`.
- **Excluding always-excluded paths is not configurable on purpose.**
  `.obsidian/` holds Obsidian's own configuration and plugin data; `.trash/` holds
  notes the user deleted. A mis-edited configuration must not be able to pull either
  into the corpus.
- **`path_case_collision` prevents a cross-platform confidentiality bug.** On a
  case-insensitive filesystem, `Private.md` and `private.md` are one file; on a
  case-sensitive one they are two. Silently case-folding relative paths would merge
  two distinct notes on Linux, potentially unifying material with different
  sensitivity. Both are quarantined instead.
- **`link_excluded` closes an exclusion bypass.** Without it, an excluded note could
  still appear in the graph as an edge target, leaking its existence and its name to
  anyone who can read the graph. Refusing the edge keeps the exclusion total.
- **No network access from the link resolver** removes an exfiltration path: a
  crafted note containing a link to an attacker-controlled URL cannot cause CAO to
  make a request that signals the note was read.
- **Findings are content-free.** A finding carries a code, a severity, the
  vault-relative path and bounded counts — never note bytes, never a matched
  secret-gate pattern's content. This matches the content-free reporting rule the
  relationship service already enforces, so `cao memory vault status` output is safe
  to paste into an issue.
