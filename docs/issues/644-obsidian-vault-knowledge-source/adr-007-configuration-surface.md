# ADR-007: Configuration surface for the vault source

**Issue:** [#644](https://github.com/awslabs/cli-agent-orchestrator/issues/644)
**Status:** Proposed. **Revision 3** — `secret_gate` governs index-time behaviour as well as writes (rulings R5 and R14). Revision 2 changed the path-validation rules and the binding's scope_id handling.
**Decision owner:** the issue's Proposed Solution "configure one vault root, folder-to-scope mappings, exclusions, and a managed write folder"
**Related:** [design.md](design.md), [adr-002-vault-cardinality.md](adr-002-vault-cardinality.md), [adr-003-read-and-write-surface.md](adr-003-read-and-write-surface.md), [adr-005-index-vs-injection-policies.md](adr-005-index-vs-injection-policies.md)

---

## Context

The configuration is the security boundary. Everything the feature may read, everything it
may write, which scope each note lands in, and whether a note may reach an agent's prompt
are decided here. A weak validation story makes every other control theoretical.

CAO has one configuration file and one precedence chain, established by issue #357 in
`services/config_service.py`: `CLI flag > CAO_* env var > config file > built-in default`,
over `~/.aws/cli-agent-orchestrator/settings.json`. `CAOConfig` is a typed pydantic schema
with a section per concern; `MemoryConfig` is one of them. `settings_service.get_memory_settings()`
(line 287) layers defaults, the persisted `memory` object and `CAO_MEMORY_*` env overlays,
and `set_memory_setting()` (line 520) has a closed key whitelist.

Two existing conventions constrain the answer. First, `get_memory_settings()` does
`result.update(saved)`, so an unrecognized nested key under `memory` passes through
untouched — a nested object is addable without breaking the reader. Second,
`_validate_project_id_override` (`memory_service.py:111`) sets the house style for
user-supplied config: **reject, never sanitize**, because "silent sanitization of an
explicit user-supplied config value hides typos and buries the contract."

`config_service.NetworkConfig` and `AuthConfig` both document that their `settings.json`
values are not yet honored and only env vars are read. Vault config must not join that
category — a path only an env var can set is a path an operator cannot review in a file.

## Revision 2 changes

| Change | Driver |
| --- | --- |
| **Path validation splits by purpose.** A mapping's `folder` is validated for shape and containment but **not** against a character allowlist, because `validate_path_component`'s `_SAFE_PATH_COMPONENT_RE = \A[A-Za-z0-9._-]+\Z` (`path_validation.py:138`) rejects a folder named `CAO Design` or `Références`, which would make the feature fail at config load on real vaults. `managed_folder` **is** charset-validated, because it feeds `safe_join_under_base` on the write path | F4, directed fix D1 |
| `ScopeBinding` resolves `scope_id` through the **project-alias table**, and configuration gains guidance plus `status` warnings so identity churn cannot silently produce two replicas | F13 |
| New keys: `max_recall_body_chars` (F15), `allow_hardlinks` (T5-gap) | F15, T5-gap |
| Documentation targets corrected: `docs/settings.md` and `docs/configuration.md` **already exist** and are edited, not created | F10 correction |

## Decision

**Vault configuration lives in `~/.aws/cli-agent-orchestrator/settings.json` under
`memory.vault`, is validated by a pydantic model in `services/vault/config.py`, is read
through a new `settings_service.get_vault_config()`, and is surfaced as
`config_service.MemoryConfig.vault` so `cao config get memory.vault` works. Exactly one
env var exists, `CAO_MEMORY_VAULT_ENABLED`, and it is honored only as a disable.**

### Shape

```json
{
  "memory": {
    "enabled": true,
    "vault": {
      "enabled": false,
      "max_recall_body_chars": 4096,
      "vaults": [
        {
          "id": "primary",
          "root": "~/Vaults/Fixture",
          "managed_folder": "CAO",
          "exclude": ["Private/**", "Daily/**", "**/*.excalidraw.md"],
          "max_note_bytes": 262144,
          "max_notes": 20000,
          "max_frontmatter_bytes": 16384,
          "mappings": [
            {
              "folder": "Projects/CAO Design",
              "scope": "project",
              "scope_id": "github-com-awslabs-cli-agent-orchestrator",
              "index": true,
              "inject": false,
              "writable": true,
              "secret_gate": "reject",
              "allow_hardlinks": false
            },
            {
              "folder": "Références",
              "scope": "global",
              "index": true,
              "inject": false,
              "writable": false
            }
          ]
        }
      ]
    }
  }
}
```

The example deliberately contains a folder with a space and a folder with a non-ASCII
character. Under revision 1's rules both would have been rejected at config load.

### Validation rules, all fail-closed at load

Every rule raises with a message naming the offending key. None sanitizes and continues.

1. `enabled: true` with `vaults: []` is an error, not a silent no-op.
2. `len(vaults) > 1` is an error naming the release-one limit
   ([adr-002-vault-cardinality.md](adr-002-vault-cardinality.md)).
3. `id` matches `^[a-z0-9][a-z0-9-]{0,31}$`. It is a namespace key, never a path segment.
4. `root` goes through `resolve_and_validate_path(root, allow_create=False)`: `~`
   expands, symlinks resolve once, `BLOCKED_SYSTEM_DIRECTORIES` applies, and a
   non-directory or nonexistent root fails.
5. `root` must not be, contain, or be contained by `MEMORY_BASE_DIR` or `CAO_HOME_DIR`.
   Overlap would make the native store and the vault the same bytes — the
   two-writable-replicas outcome the issue forbids — and would let a reconcile treat
   CAO's own wiki as user notes.
6. `root` must not be the user's home directory itself, and must not resolve under
   `graph_export_root()`. The latter closes the loop where the Obsidian graph sink's
   output directory is also a read source, so a one-way projection cannot be mistaken for
   canonical content.
7. **`mappings[].folder` — shape and containment, no charset allowlist.** Each is a
   relative POSIX path. Rejected: absolute paths, any `..` segment, any NUL byte, any
   empty segment, and a trailing separator. Accepted: spaces, apostrophes, commas,
   parentheses, ampersands and non-ASCII — because real vault folders contain them.
   Containment is proven at use time, not at config time: `scan.py` and `reader.py`
   compute `os.path.realpath` and apply a single positive
   `startswith(root_real + os.sep)` guard **inline beside each filesystem sink**, the
   `_metadata_recall:2218` shape. This is the D1 correction: revision 1 specified
   `safe_join_under_base(root, *segments)` here, whose per-segment
   `\A[A-Za-z0-9._-]+\Z` check would have rejected the two mappings in the example
   above, and there is no point having a rigorous primitive that forbids the input
   domain.
8. **`managed_folder` — shape, containment, *and* charset.** In addition to rule 7, every
   segment must pass `validate_path_component`, because the write path composes targets
   with `safe_join_under_base(root, *managed_folder_segments, f"{key}.md")` and that
   primitive validates each segment against its charset. The asymmetry is deliberate and
   narrow: CAO reads folders the user named, and writes only into a folder whose name CAO
   constrains. A user who wants a managed folder called `CAO Notes` is told at config load
   to pick a name from `[A-Za-z0-9._-]`, which is a one-time cost on a folder CAO creates,
   not a restriction on their existing vault.
9. **No mapping folder may be a prefix of another.** Overlap gives a note two scopes.
10. `managed_folder` must lie inside exactly one mapping, and that mapping must have
    `writable: true`. In release one no other mapping may set `writable: true`.
11. `mappings[].scope` is restricted to `global`, `project` and `agent`. `federated` and
    `session` are rejected (settled, ruling R4) — see Security below.
12. `scope_id` is required for `project` and `agent`, must match the shipped
    `MemoryScopeId` pattern `^[a-zA-Z0-9._-]{1,128}$`, and must not consist solely of
    dots. It is forbidden for `global`, which resolves to `None` by contract.
13. Two mappings may not resolve to the same `(scope, scope_id)`.
14. `exclude` patterns are POSIX globs matched against the vault-relative path. A pattern
    containing `..` or an absolute prefix is rejected. Character content is otherwise
    unrestricted, for the same reason as rule 7.
15. `index: false` with `inject: true` is an error
    ([adr-005-index-vs-injection-policies.md](adr-005-index-vs-injection-policies.md)).
16. `secret_gate` is **per mapping**, is `"reject"` (the default) or `"warn"`, and
    **governs both the write path and index time** (rulings R5 and R14). `reject` refuses a
    secret-bearing write and quarantines a secret-bearing note at reconcile; `warn` records
    the `secret_detected` finding without refusing or quarantining. One key, one meaning,
    two enforcement points — no third value and no second key, so the two boundaries cannot
    drift apart.
    An absent `secret_gate` resolves to `reject`, so **`warn` is only ever reached by a
    deliberate operator act**: it must be written into that mapping, and it applies to that
    mapping alone. There is no global override and no way to set it for the whole vault at
    once, which is intentional — secret-handling risk is not uniform across a vault, and the
    per-mapping shape matches the granularity at which the operator already chose the scope.
    **The finding is reported in both modes.** The mode governs indexing, never disclosure.
    `reject` is the shipped default so that the safe posture is what you get without reading
    the documentation, and `warn` exists because one of the six patterns is
    `(?i)(?:password|passwd|secret|pwd)\s*[:=]\s*\S{6,}` and notes **about** credential
    handling are ordinary content in a knowledge vault.
    **On the one pairing that deserves special handling: `secret_gate: "warn"` together with
    `inject: true` is a config-load WARNING, not an error.** That combination is the
    maximal-exposure configuration in this schema — a note the gate flagged as
    credential-shaped, indexed anyway, and injected automatically into agent prompts. It is
    permitted because it is *coherent*, unlike rule 15's `inject: true` with `index: false`
    which is a contradiction and a hard error; and it is never silent, because the warning
    fires at load and `status` names it for as long as it holds. Full reasoning in rule 20.
17. `allow_hardlinks` is a bool, default `false`.
18. `max_note_bytes`, `max_notes`, `max_frontmatter_bytes` and `max_recall_body_chars`
    are positive integers with documented defaults, and are capped so a configuration
    cannot disable the denial-of-service protections or the recall body budget.
19. Always-excluded paths — `.obsidian/`, `.trash/`, `.git/`, `_cao-*` — are applied
    unconditionally and are not expressible in, or removable by, `exclude`.
20. **`secret_gate: "warn"` together with `inject: true` is a config-load WARNING, not an
    error, and is named permanently in `status`.** That pairing is the maximal-exposure
    combination in the schema: a note the gate flagged as credential-shaped, indexed anyway,
    and injected automatically into agent prompts without a human reading it.
    The reasoning for warning rather than refusing, since the choice could go either way.
    Rule 15 already makes `inject: true` with `index: false` a hard **error**, so refusing a
    combination is not unprecedented here — but that combination is **incoherent** (you
    cannot inject what is not indexed), whereas this one is **coherent**: an operator may
    legitimately want a curated `Reference/` folder that documents credential handling and is
    meant to be in every prompt. Refusing it would be CAO overriding a documented,
    deliberate, per-mapping election, which is a different act from refusing a contradiction.
    So the design permits it and makes it impossible to reach by accident: a named warning at
    config load, and a line in `cao memory vault status` for as long as it holds.
    One refinement the implementer should not miss: `status` already warns separately about
    injectable mappings and about secret findings. For a mapping carrying both, those must be
    emitted as **one combined line naming the mapping**, not two lines an operator has to
    correlate. The whole value of the warning is that the pairing is visible as a single
    fact.

### Scope-id stability guidance (F13)

`ScopeBinding` keying on a raw `scope_id` was a hole. `resolve_project_id`
(`memory_service.py:210`) falls back to `sha256(realpath(cwd))[:12]`, and
`_get_search_dirs` already compensates by including recorded `cwd_hash` alias directories
(lines 2523-2546). If a project id churned — a renamed or moved folder — a naive binding
would return `NativeBinding` and `store()` would write to the native wiki with no error:
two replicas for one logical scope, arrived at silently, which is a stated Non-Goal.

Three changes, of which only the first is a code rule and the other two are surfaces:

1. `binding.resolve()` canonicalises `scope_id` through the project-alias table
   (`get_project_id_by_alias`) **before** matching a mapping. This fixes the actual churn
   case, because the alias is recorded by `resolve_project_id` itself.
2. `status` warns on any mapping whose `scope_id` matches no known project id or alias
   (**orphaned mapping**), and on any mapped `project` `scope_id` that was resolved from a
   cwd hash rather than pinned or git-derived.
3. A native write to a `project` scope, when a vault mapping for `project` exists and the
   resolved `scope_id` is not among the mapped ones, emits a named warning and a
   `status`-visible `unmapped_project_write` counter. It is **not** a hard error: a second,
   legitimately-native project is indistinguishable from a churned id, so refusing would
   break ordinary multi-project use. Removing the silence is what satisfies the Non-Goal.

**Documented recommendation, not a rule:** a mapping with `scope: project` should pin
`memory.project_id` (or rely on a git remote), because cwd-hash identity breaks on folder
rename. This goes in `docs/obsidian-vault.md` next to the mapping example.

### Env var policy

`CAO_MEMORY_VAULT_ENABLED` is added to `config_service.ENV_REGISTRY` mapped to
`memory.vault.enabled`. Registry precedence is env over file, so the var can in principle
set `true` — but with no `vaults` entry in the file it is inert, and a `vaults` entry can
only come from the file. The asymmetry is therefore real: the env var can turn the feature
**off** where the file turns it on, and cannot turn it on where the file has configured
nothing. Documented as intended behavior.

**No path, mapping, exclusion or scope is settable by an env var.** An environment
variable is process-local, unreviewable and settable by anything that can spawn CAO; a
variable that could add a mapping could point CAO at an arbitrary directory and assign it
a scope.

### Writing configuration

`set_memory_setting()`'s closed whitelist gains no vault keys. `memory.vault` is a nested
object with cross-field invariants (rules 9, 10, 13, 15), which a single-key setter cannot
validate. Release one is file-edited, with `cao memory vault status` as the validator: it
loads the config, reports every validation error by key, and reports nothing else until
the config is valid.

### Documentation targets

All three files exist and are edited, not created — this corrects a claim in the review:

- `docs/configuration.md` — a `CAO_MEMORY_VAULT_ENABLED` row in the env-var table at
  lines 266-276.
- `docs/settings.md` — the `memory.vault` block and every key above.
- `docs/memory.md` — the `vault` relationship origin, and a pointer to the new
  `docs/obsidian-vault.md`.

## Consequences

**Positive.**

- One file, one precedence chain, the conventions #357 established.
- Every rule fails closed at load, so an invalid configuration means the feature is off,
  not partly on.
- Cross-field invariants are checked where all the fields are visible, so the properties
  the other ADRs depend on — one scope per note, one write target, no
  inject-without-index — hold before any note is read.
- **The feature now works on real vaults.** Rules 7 and 8 are the difference between a
  design that validates elegantly and one that can be configured against a directory a
  human actually made.
- Alias-aware binding plus non-silent divergence reporting close the churn hole without
  breaking multi-project use.

**Negative, and accepted.**

- **Two path-validation regimes in one config model** — charset-free for read mappings,
  charset-checked for the managed folder. A reviewer will ask why. Rule 8 states the
  reason inline, and the security section restates it: read confinement is proven by
  realpath containment at the sink, which needs no charset assumption, while
  `safe_join_under_base` needs one by construction.
- **Containment for read paths is proven at use time, not load time**, so a mapping that
  will fail confinement is only reported by `scan`/`status`, not by config load. Mitigated
  by `cao memory vault scan --dry-run` being the documented first step.
- **Hand-editing JSON is a poor authoring experience**, and a trailing-comma typo disables
  the feature. Mitigated by `status` reporting validation errors by key and by shipping a
  complete annotated example.
- **No CLI setter in release one**, so a scripted install must template the file.
- **The env var's asymmetry is surprising** until read.
- **Restricting mappable scopes will block someone.** Settled by R4.
- **Rule 5 rejects a configuration some user will try** — putting the vault inside
  `~/.aws/cli-agent-orchestrator/`. The error message must explain why, not just refuse.
- **The `unmapped_project_write` warning is a warning, not a refusal**, so a determined
  misconfiguration can still produce two stores for one logical project. The Non-Goal is
  about silence, and the silence is removed; a refusal would break legitimate cases.

## Alternatives rejected

### A. A new top-level `knowledge` or `vault` section in `CAOConfig`

Rejected. The vault is a memory content backend: gated by `memory.enabled`, served through
`MemoryService`, indexed into `memory_metadata`, subject to the memory scope model. A
sibling section would separate it from the switch that governs it and invite a future
reader to assume independence from `memory.enabled` — a real bug, since that check is the
first line of both injection entry points.

### B. A separate file, for example `vault.json`

Rejected. Issue #357's purpose was collapsing two configuration surfaces into one;
`LEGACY_CONFIG_FILE` is read once for migration then ignored. A third file also fragments
the audit story.

### C. Env-var-driven configuration, following `NetworkConfig` and `AuthConfig`

Rejected. Those document their env-only status as a limitation of an incremental
migration, not a model to copy. For paths and scope mappings it is actively unsafe: a
`CAO_MEMORY_VAULT_ROOT` would let anything that can launch CAO nominate a directory as a
knowledge source. The single enabled-flag var is included only because a disable switch is
operationally valuable and can only reduce exposure.

### D. Per-project configuration in the repository, for example `.cao/vault.json`

Rejected for release one. Attractive — the mapping is project knowledge and `scope_id` is
already a project identity — and a privilege-escalation vector: a file inside a cloned
repository would let that repository declare which of the user's vault folders CAO may
read and inject, on the first agent run after checkout. If wanted later, the safe shape is
a repository file that *requests* a mapping plus a one-time interactive approval recorded
in user-level settings.

### E. Store vault configuration in SQLite alongside the derived state

Rejected. Configuration is not derived state, and `rebuild` deletes derived state.
Configuration in a table a maintenance command clears is a footgun, and it would put the
security boundary somewhere an operator cannot review with a text editor or track in
version control.

### F. Keep `safe_join_under_base` for mapping folders and require users to rename their folders

Rejected, and named because it was revision 1's implicit position. It would preserve one
uniform primitive at the cost of telling a user to rename `Projects/CAO Design` and
`Références` before CAO will read them — i.e. requiring a vault reorganisation as a
precondition for a feature whose entire premise is "use the vault you already have". It
also does not buy real security: realpath containment at the sink is the property that
prevents escape, and a charset allowlist is a proxy for it, not a substitute.

### G. Normalise non-ASCII folder names to an ASCII form at load

Rejected. It would let `safe_join_under_base` stay in place, and it introduces a mapping
between two names for the same directory — so every finding, every `status` line and every
`vault_relpath` would have to choose one, and Unicode normalization form differences
(macOS NFD versus Linux NFC) would make the transformation platform-dependent. That is a
determinism hazard ([adr-006-identity-and-change-detection.md](adr-006-identity-and-change-detection.md))
in exchange for no security gain.

## Security and compliance implications

- **The configuration is the boundary, so it fails closed at load**, following the house
  style in `_validate_project_id_override`.
- **Read confinement does not depend on a character allowlist.** The property that
  prevents escape is `os.path.realpath` followed by a single positive
  `startswith(root + os.sep)` immediately before the filesystem sink, in the same
  function. That is strictly stronger than a charset check for the threat that matters
  (escaping the root) and it does not constrain the input domain. The charset check is
  retained exactly where the primitive that needs it is used — the write path.
- **The write path is narrower than the read path by design.** CAO reads folders the user
  named and writes only into a folder whose name CAO constrains, so the set of paths CAO
  can ever create is a strict, charset-bounded subset of the paths it can read.
- **`federated` is unmappable** because it is the machine-wide shared tier whose write
  path already runs `scan_for_secrets` and rejects credentials (`store()`, line 702).
  Mapping a cloud-synced folder onto it would make a shared-by-design tier readable from a
  directory replicated to other devices and possibly other people.
- **`session` is unmappable** because AC2 explicitly warns about short-lived scopes being
  written to a broadly shared or synced folder, and a session scope is ephemeral by
  definition.
- **`root` overlap rules prevent authority confusion.** Rule 5 keeps the native store and
  the vault distinct so neither can be reinterpreted as the other; rule 6 keeps the graph
  export root distinct so a generated projection cannot become a canonical source.
- **No path is env-settable**, so the set of directories CAO may read is fixed by a file an
  operator can review, diff and version.
- **Bounds cannot be disabled**, so the denial-of-service protections in
  [adr-004-supported-markdown-boundary.md](adr-004-supported-markdown-boundary.md) and the
  recall body budget are not configurable away.
- **Non-overlapping mappings and one-scope-one-location keep the existing authorization
  model sound**, because `scope_write_allowed` and the relationship service's same-scope
  invariant both assume a note has exactly one scope.
- **Alias-aware binding is a security fix, not an ergonomic one.** Silent divergence
  between the configured scope_id and the resolved one produces two stores for one logical
  scope, which means an operator who believes a scope is vault-backed — and therefore
  subject to the mapping's `inject` and `secret_gate` settings — could be served by a
  native store subject to neither.
- **Compliance review has a single artifact.** "What may CAO read, where may it write, and
  what may reach an agent's prompt?" is answered by one JSON object, with
  `cao memory vault status` as the machine-checkable confirmation that the object is valid
  and that only the intended mappings are injectable.
