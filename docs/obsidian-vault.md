# Obsidian Vault

CAO can scan a configured Obsidian vault and maintain CAO-authored notes in
one managed folder. This guide covers the committed configuration,
maintenance commands, write safety, and current limits. It does not require
the Obsidian application to be running.

> This release supports one configured vault. Use
> [Configuration](configuration.md) for the complete `settings.json` reference.

## Configure a vault

Add a `memory.vault` object to `settings.json`. Replace every path and
identifier in this example with values for your own environment; the paths
below are placeholders.

```json
{
  "memory": {
    "vault": {
      "enabled": true,
      "max_recall_body_chars": 4096,
      "vaults": [
        {
          "id": "knowledge-vault",
          "root": "/path/to/your-vault",
          "managed_folder": "_cao",
          "exclude": ["Private/**"],
          "max_note_bytes": 262144,
          "max_notes": 20000,
          "max_frontmatter_bytes": 16384,
          "mappings": [
            {
              "folder": "_cao",
              "scope": "global",
              "index": true,
              "writable": true,
              "secret_gate": "reject"
            },
            {
              "folder": "Projects/example-project",
              "scope": "project",
              "scope_id": "example-project",
              "index": true,
              "allow_hardlinks": false
            },
            {
              "folder": "Agents/example-agent",
              "scope": "agent",
              "scope_id": "example-agent",
              "index": true
            }
          ]
        }
      ]
    }
  }
}
```

`root` must be an existing absolute vault directory. `id` uses lowercase
letters, digits, and hyphens. Mapping folders are vault-relative POSIX paths.
They may map one `global`, `project`, or `agent` scope; `project` and `agent`
mappings require `scope_id`, while `global` must not have one. A scope can be
mapped only once.

`managed_folder` is the CAO-owned folder within the vault. It must lie in
exactly one writable mapping, and that mapping is the only writable mapping.
Create the folder before enabling the configuration.

### Scan selection and bounds

`exclude` holds additional relative glob patterns. CAO also always excludes
`.obsidian/`, `.trash/`, `.git/`, and `_cao-*` paths. Exclusions prevent files
from becoming scan candidates; they do not grant CAO permission to write
elsewhere.

The validated bounds are:

| Setting | Default | Maximum |
| --- | ---: | ---: |
| `max_note_bytes` | 262144 | 1048576 |
| `max_notes` | 20000 | 100000 |
| `max_frontmatter_bytes` | 16384 | 65536 |
| `max_recall_body_chars` | 4096 | 65536 |

The first three belong to the vault entry. `max_recall_body_chars` belongs to
the enclosing `memory.vault` object. Its runtime recall behavior is documented
in [Recall and injection](#recall-and-injection).

`allow_hardlinks` defaults to `false` for each mapping. A hardlinked note is
otherwise refused during scanning. Set it to `true` only when that sharing is
intentional.

### Automatic injection

`inject: false` governs automatic injection into agent context only. It is not
a confidentiality control: indexed content remains available through explicit
recall and to anything that can read the vault or call the local API. An agent
can pull content that CAO will not push into its context.

Curator injection bounds apply to CAO-mediated vault reads for the
`memory_manager` identity. Agent-scoped mappings are returned only to a
requester positively identified as a non-curator; unknown identities, including
ordinary CLI recall without a terminal, are refused. These bounds cannot
constrain an agent that reads vault files directly or spawns another identity.
A vault mapping scoped to `agent` therefore returns nothing to plain CLI recall
without a resolvable terminal, where earlier releases returned its notes.

## Recall and injection

Vault recall uses one confined read path. Only notes currently indexed by a
successful reconciliation are eligible; quarantined, excluded, and unsupported
notes are not returned, even if older derived metadata remains. An indexed note
can still yield no result when CAO cannot safely open or decode its current
file. Run `cao memory vault reconcile --apply` after resolving a finding or
changing vault content.

Recall returns this state in its `index_freshness` field as `fresh` or `stale`.
CAO compares the current file size and modification time with the values
recorded at reconciliation.
`stale` does not withhold a note: release one has no watcher or implicit
reconciliation, so CAO serves the safely readable current content and marks it
stale. Reconcile the vault to refresh its derived metadata and relationships.
Reconciliation alone does not release a note quarantined by the secret gate;
remove or change the matching content before reconciling again.

`max_recall_body_chars` defaults to 4096 characters. A longer note body is
clipped with a visible `[Content truncated for recall]` marker, and the recall
result records that it was truncated. This is deliberately lossy; inspect the
note in the vault when the complete body matters. It is separate from the
tighter context budget used for automatic injection.

Automatic injection always applies the injection policy; a caller on that path
cannot waive it. Mappings with `inject: false` are omitted, as described above,
and the deterministic builder considers only session, project, and global
scopes, in that order. Each scope is limited independently to ten entries and
its share of the context character budget (at most 1000 characters); unused
space is not reassigned. Related entries are added only after their primary
entry and are capped at five across one context build. A recallable note can
therefore be absent from an agent prompt because its mapping is not injectable,
its scope is agent, or it lost a per-scope or related-entry budget decision.
Vault status reports injection budget effects without recording note content.

When a same-session memory curator is available, CAO may use its returned
context block. Any curator failure, busy state, timeout, unavailable provider,
or unusable response falls back to the deterministic builder so injection does
not block the worker. That fallback can produce a different selection from the
curator; it does not bypass the injection policy.

### Secret handling

Each mapping has a `secret_gate`:

- `reject` is the default. A secret-bearing note is quarantined at index time
  and reported.
- `warn` indexes the note and still reports the secret finding.

Pattern matching is deliberately conservative. A note about credential
handling can legitimately match a credential-shaped pattern; `warn` is the
operator escape hatch for that case. CAO does not silently remove or redact
the note.

For managed writes, the gate checks the authored body and CAO-generated
`cao` metadata before publishing. Preserved user frontmatter is not scanned or
rewritten by this gate; review and change that frontmatter directly when it
contains credential-shaped text.

`CAO_MEMORY_VAULT_ENABLED` can only disable a file-defined vault
configuration. Set it to a false value to turn the configured vault off for a
process. It cannot enable an absent or file-disabled vault, and no environment
variable configures a vault path, mapping, or scope.

## Maintenance commands

All vault operations are local CLI commands. There is no MCP tool for
rescanning or mutating a vault.

```text
cao memory vault status [--format table|json]
cao memory vault scan [--dry-run]
cao memory vault reconcile [--apply] [--format table|json]
cao memory vault rebuild --apply
cao memory vault migrate --scope {global|project|agent} [--scope-id ID] [--apply] [--delete-source] [--confirm-delete-source]
```

`status` reports the derived vault projection and configuration warnings. Its
`process_local_unmapped_project_writes` field is explicitly process-local; a
fresh CLI process cannot present it as a durable count.

`scan` is read-only. `--dry-run` is accepted for review workflows, but scanning
does not write files or derived rows with or without the flag.

`reconcile` plans the derived-state update by default. Pass `--apply` to write
that projection.

`rebuild` requires `--apply`. It deletes and re-derives vault-derived state;
vault access counts reset by design.

`migrate` moves one native memory scope into the managed folder. It is a
dry-run unless `--apply` is present. `--delete-source` additionally requires
both `--apply` and `--confirm-delete-source`; without both flags, the command
refuses before migration.

Migration reports lossy source fields by name when present:

- `access_count`
- `last_accessed_at`
- `last_compiled_at`
- `source_provider`
- `source_terminal_id`
- `related_keys`
- `append_only_section_history`

Typed native relationships are represented as `cao.links`. More than 64 typed
links cannot fit in the managed-note limit and are reported as the additional
lossy `cao.links` case rather than silently truncated.

## Write safety

CAO writes only notes under `managed_folder`. It never rewrites or deletes an
unmanaged vault note.

For a managed note, CAO preserves user frontmatter as text: it re-emits every
user-owned frontmatter byte unchanged except the top-level `cao` block it owns.
The writer may seed `tags` and `created` on a new managed note, but it does not
overwrite either key when the note already has a user value.

CAO stages its replacement inside the managed folder and publishes it
atomically on that filesystem. A non-empty managed note whose frontmatter
cannot be recognized is a conflict, not permission to rewrite it. Resolve the
note and run `cao memory vault reconcile --apply` before retrying.

Retention may remove CAO's native copy in a vault-backed scope, but it never
deletes the vault file. A vault-backed memory can therefore outlive the native
scope retention period until its vault note is changed or removed by its owner.

The release-one writer coordinates CAO writers, not Obsidian or a sync client.
"Obsidian open and writing" is not a guarantee in this release. Avoid a
simultaneous manual edit while a managed write is in progress, then reconcile
again if a conflict is reported.

## Known limitations

**Agent-scoped mappings are recall-only in this release.** They remain
available to explicit memory recall for the matching agent profile, but CAO's
deterministic injection builder considers only session, project, and global
scopes. Vault status reports this limitation for every agent-scoped mapping.
Adding agent scope to injection would change the existing per-scope budget
allocation, so it is intentionally deferred.

Graph projection has no requesting terminal identity. It therefore omits
agent-scoped vault notes and reports that the graph is partial in its metadata,
rather than publishing a topology that the same unknown requester cannot
recall.

CAO can retain identity across an unambiguous pure rename. A rename plus an
edit in one reconciliation window without an authored `cao.key` is reported,
not guessed. Add an authored `cao.key` when stable identity across those edits
matters.

`cao memory vault adopt` is deferred to release two. CAO does not turn an
existing unmanaged note into a managed note in this release.
