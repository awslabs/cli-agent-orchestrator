# Obsidian Vault Demo

A throwaway end-to-end harness for the Obsidian vault knowledge source ([issue #644](https://github.com/awslabs/cli-agent-orchestrator/issues/644), [`docs/obsidian-vault.md`](../../docs/obsidian-vault.md)). One script builds a scratch CAO home and a generated vault, indexes it, and hands you the queries to see what CAO derived — so you can open the thing in Obsidian, edit a note, and watch the index follow, in about a minute.

It exists because the interesting behaviour of this feature is not unit-testable in a way that convinces a human: whether a quarantined note's body is really gone from recall, whether a dry run really wrote nothing, whether CAO's link graph agrees with Obsidian's own. Each of those is a two-command check here.

**Nothing touches your real vault or your real CAO home.** `CAO_HOME_DIR` is redirected to the scratch tree for the lifetime of every command, and the vault is generated from scratch — there is no path anywhere in this example that points at a vault you already own.

## What this demonstrates

- A vault becoming a recallable memory source: `cao.` frontmatter → `vault_note` rows → BM25 hits marked `source_kind=vault`.
- `[[wikilinks]]` becoming `memory_relationships` rows with `origin='vault'`, cross-checkable against Obsidian's own Backlinks pane.
- Folder-to-scope mapping, including the derived project scope id and why two `global` folders is a config error.
- The four index-time findings and the fact that **three quarantine and one only flags** — verified by going looking for the forbidden bytes in recall output, not by trusting the `status` column.
- `reconcile` being a dry run by default, made visible with a digest of every stored content hash (the dry-run and `--apply` summaries print identically, so there is otherwise nothing to see).
- Injection failing closed when the requester has no resolvable terminal identity.

## Files

- [`vault-demo.sh`](vault-demo.sh) — the whole harness. No arguments prints usage.

## Usage

Run it from a checkout of the branch under test — it invokes `uv run cao` from the repo root, so a `cao` on `PATH` from a released build (which has no `vault` command) is never used.

```bash
cd examples/obsidian-vault-demo

./vault-demo.sh up                 # build the vault, index it, print a summary
./vault-demo.sh obsidian           # how to open it in Obsidian, and what to look at
./vault-demo.sh state              # notes / findings / edges as they stand
./vault-demo.sh recall "which frontmatter fields does cao read"
./vault-demo.sh sync               # after you edit a note: dry run vs --apply
./vault-demo.sh guards             # plant 4 bad notes, show what each one does
./vault-demo.sh inject             # show that injection fails closed
./vault-demo.sh down               # delete the scratch tree
```

The scratch tree defaults to `$TMPDIR/cao-vault-demo`; set `CAO_VAULT_DEMO_DIR` to put it somewhere you'd rather keep it while you poke at it. `down` deletes it and nothing else.

## Expected numbers

After `up`:

| notes | indexed | quarantined | edges | findings |
| --- | --- | --- | --- | --- |
| 7 | 7 | 0 | 13 | 0 |

After `guards`:

| notes | indexed | quarantined | edges | findings |
| --- | --- | --- | --- | --- |
| 11 | 8 | 3 | 13 | 4 |

The clean baseline is deliberately **0 findings**, so any finding you see is one you caused. The edge count not moving across `guards` is the point of the dangling-link case: it indexes, and contributes no edge.

## The vault is the documentation

The seven seeded notes each explain one part of the feature and link to the others, so the graph you're looking at in Obsidian *is* the explanation of what you're testing:

```
Notes/Start Here.md        the tour
Notes/Frontmatter.md       the cao: block; type is a closed set
Notes/Wikilinks.md         links to edges; dangling vs cross-scope
Notes/Guards.md            what quarantines vs what only flags
Notes/Scopes.md            folder-to-scope mapping, derived project ids
Notes/Managed Folder.md    the one writable folder
CAO/Agent Notes.md         a managed: true note, project-scoped
```

Reading them in Obsidian's reading view is a reasonable substitute for reading `docs/obsidian-vault.md`, and unlike the doc, it's wrong-provable: if a note claims something the index doesn't do, `state` says so.

## Notes for reviewers

- `cao memory show <key>` is BM25 recall with `query=key` plus an exact-key filter, not a key lookup, so it returns "not found" for any note whose key words don't appear in its body. Behaves identically on `main`, so it isn't this feature's doing, but it makes `show` a misleading way to check whether a note landed. `state` and `recall` are the reliable ones.
- Findings are only reachable via SQLite; there is no `cao memory vault findings` command. `state` runs those queries for you.
- A stale `CAO_TERMINAL_ID` in your shell makes every vault recall resolve zero candidates and print nothing — indistinguishable from an empty vault. The script drops it from every child process; worth knowing if you run the commands by hand instead.
- `AKIAIOSFODNN7EXAMPLE` in the `guards` fixture is AWS's documented example key, the literal `CONTRIBUTING.md` prescribes and the only value allowlisted in `.gitleaks.toml`.
