#!/usr/bin/env bash
#
# vault-demo.sh — a throwaway end-to-end harness for the Obsidian vault
# knowledge source (issue #644).
#
# Creates a scratch CAO home and a generated vault, indexes it, and gives you
# the queries to see what CAO derived. Nothing here touches your real vault or
# your real ~/.aws/cli-agent-orchestrator: CAO_HOME_DIR is redirected to the
# scratch tree for the lifetime of every command.
#
#   ./vault-demo.sh up                 build the vault, index it, print a summary
#   ./vault-demo.sh obsidian           print how to open it in Obsidian
#   ./vault-demo.sh state              notes / findings / edges as they stand
#   ./vault-demo.sh recall "question"  BM25 retrieval, the way an agent calls it
#   ./vault-demo.sh sync               reconcile --apply, after you edit a note
#   ./vault-demo.sh guards             plant 4 bad notes, show what each one does
#   ./vault-demo.sh inject             show that injection fails closed
#   ./vault-demo.sh down               delete the scratch tree
#
# Run it from a checkout of the branch under test.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && git rev-parse --show-toplevel)"
DEMO="${CAO_VAULT_DEMO_DIR:-${TMPDIR:-/tmp}/cao-vault-demo}"
VAULT="$DEMO/DemoVault"
export CAO_HOME_DIR="$DEMO/cao-home"
DB="$CAO_HOME_DIR/db/cli-agent-orchestrator.db"

# A CAO_TERMINAL_ID inherited from the surrounding shell makes every vault
# recall resolve zero candidates and print nothing — indistinguishable from an
# empty vault. Drop it for every child process.
cao() { ( cd "$REPO" && env -u CAO_TERMINAL_ID CAO_HOME_DIR="$CAO_HOME_DIR" uv run cao "$@" ); }
py()  { ( cd "$REPO" && env -u CAO_TERMINAL_ID CAO_HOME_DIR="$CAO_HOME_DIR" uv run python "$@" ); }
q()   { sqlite3 -header -column "$DB" "$1"; }

ME="./${0##*/}"
die() { printf '%s\n' "$*" >&2; exit 1; }
have_vault() { [ -d "$VAULT" ] || die "no demo vault yet — run: $ME up"; }

# ---------------------------------------------------------------- seed content
#
# The notes are the documentation: each one explains a piece of the feature and
# links to the others, so the vault doubles as test data and as a tour. Read
# them in Obsidian and the graph you are looking at explains itself.

write_notes() {
  mkdir -p "$VAULT/Notes" "$VAULT/CAO"

  cat > "$VAULT/Notes/Start Here.md" <<'EOF'
---
tags: [demo, overview]
cao:
  key: vault-start-here
  type: reference
---

# Start Here

This vault is a disposable fixture for CAO's Obsidian vault knowledge source.
Every note in it describes one part of how the feature works, so the graph you
see in Obsidian is also the explanation of what you are testing.

Read [[Frontmatter]] to see how a note becomes a memory, [[Wikilinks]] for how
these links become graph edges, [[Scopes]] for how folders map to CAO scopes,
and [[Managed Folder]] for the one folder CAO is allowed to write to.

Nothing here is real. Delete the whole tree with `vault-demo.sh down`.
EOF

  cat > "$VAULT/Notes/Frontmatter.md" <<'EOF'
---
tags: [demo, frontmatter]
cao:
  key: vault-frontmatter
  type: reference
---

# Frontmatter

A note joins the index when its YAML frontmatter carries a `cao:` block. Only
four fields are read: `key`, `type`, `managed` and `links`. Everything else in
the frontmatter, including your own `tags`, is left alone — CAO reads the block
it owns and ignores the rest of your vault's conventions.

`type` is a closed set: `user`, `feedback`, `project`, `reference`. A value
outside it quarantines the whole note as `invalid_cao_block`, which is easy to
hit because `decision` and `note` both feel like they ought to work. See
[[Guards]] for what quarantine actually costs you.

`key` is the memory key, so it must be stable. Renaming a file is safe;
changing `key` is the edit to be careful with. Compare [[Scopes]], where
identity is the pair of key and scope rather than the key alone.
EOF

  cat > "$VAULT/Notes/Wikilinks.md" <<'EOF'
---
tags: [demo, graph]
cao:
  key: vault-wikilinks
  type: reference
---

# Wikilinks

Every `[[wikilink]]` between two indexed notes becomes a row in
`memory_relationships` with `origin='vault'`, which is what lets CAO traverse
your vault as a graph rather than a bag of documents.

A link whose target does not exist produces no edge. It is recorded as a
`link_dangling` finding at `info` severity and the note still indexes, because
linking to a note you have not written yet is ordinary Obsidian practice and
not a defect. [[Guards]] covers the findings that do stop a note.

A link that crosses a scope boundary also produces no edge, and produces no
finding either. [[Agent Notes]] is project-scoped and links to two global notes
that plainly exist, yet it appears in no edge at all. Whether the edge should
exist is a design question — scopes are isolation boundaries — but the silence
is worth noticing, because a cross-scope link and a typo look identical from
the outside.

Obsidian's own Backlinks pane is a useful independent check here: it is a
completely separate link parser, so if it and `memory_relationships` disagree
about a note's edges, one of the two has a bug — with the cross-scope case
above as the known, expected disagreement.
EOF

  cat > "$VAULT/Notes/Guards.md" <<'EOF'
---
tags: [demo, guards]
cao:
  key: vault-guards
  type: reference
---

# Guards

Indexing refuses content rather than sanitising it. Three conditions quarantine
a note outright, so its body never becomes recallable: a detected credential
(`secret_detected`), a symlink pointing outside the vault (`symlink_refused`),
and a `cao.type` outside the closed set (`invalid_cao_block`).

One condition only flags: a dangling `[[wikilink]]` records `link_dangling` at
`info` and the note indexes normally. See [[Wikilinks]] for why, and
[[Frontmatter]] for the closed type set that the third one trips over.

`vault-demo.sh guards` plants one of each so you can watch the difference.
Findings live only in the `vault_finding` table — there is no
`cao memory vault findings` command — so `vault-demo.sh state` is how you read
them.
EOF

  cat > "$VAULT/Notes/Scopes.md" <<'EOF'
---
tags: [demo, scopes]
cao:
  key: vault-scopes
  type: reference
---

# Scopes

Folders are mapped to CAO scopes in settings, not guessed from the vault. This
demo maps `Notes/` to `global` and `CAO/` to `project`.

Two mappings may not resolve to the same scope and scope id, so two `global`
folders is a configuration error rather than a merge. That constraint is why
the writable folder in [[Managed Folder]] is project-scoped here.

Project scope ids are derived, not typed by hand, so a project mapping only
resolves from the directory it was derived for. Recall from elsewhere returns
nothing at all — silence, not an error, which is worth knowing before you
conclude the index is empty. Global mappings have no such constraint, which is
why [[Start Here]] and the rest of `Notes/` are readable from anywhere.
EOF

  cat > "$VAULT/Notes/Managed Folder.md" <<'EOF'
---
tags: [demo, writes]
cao:
  key: vault-managed-folder
  type: reference
---

# Managed Folder

One folder per vault may be declared `writable`. That is the only place CAO may
create or modify notes; every other mapped folder is read-only to it, so the
notes you write by hand can never be rewritten underneath you.

Notes CAO owns carry `managed: true` in their `cao:` block — see
[[Agent Notes]] in the `CAO/` folder for the one this demo ships. The flag is
the marker for "CAO may edit this", so setting it by hand on a note you care
about opts that note into being overwritten.

Read [[Frontmatter]] for the rest of the block, and [[Scopes]] for why this
folder is project-scoped in the demo config.
EOF

  cat > "$VAULT/CAO/Agent Notes.md" <<'EOF'
---
tags: [demo, writes]
cao:
  key: vault-agent-notes
  type: project
  managed: true
---

# Agent Notes

This note lives in the writable managed folder and is marked `managed: true`,
so CAO may rewrite it. It is the counterpart to [[Managed Folder]].

Being project-scoped, it only resolves from the directory its scope id was
derived for — see [[Scopes]].
EOF
}

# Pre-configure Obsidian so the graph is readable on first open instead of a
# grey hairball: tag colour groups, arrows on, unresolved links visible.
write_obsidian_config() {
  mkdir -p "$VAULT/.obsidian"
  cat > "$VAULT/.obsidian/app.json" <<'EOF'
{
  "alwaysUpdateLinks": true,
  "newLinkFormat": "shortest",
  "useMarkdownLinks": false,
  "propertiesInDocument": "visible",
  "showInlineTitle": true,
  "promptDelete": false
}
EOF
  cat > "$VAULT/.obsidian/graph.json" <<'EOF'
{
  "showArrow": true,
  "hideUnresolved": false,
  "showTags": false,
  "showAttachments": false,
  "scale": 0.85,
  "colorGroups": [
    {"query": "tag:#graph",       "color": {"a": 1, "rgb": 5431378}},
    {"query": "tag:#frontmatter", "color": {"a": 1, "rgb": 14701138}},
    {"query": "tag:#scopes",      "color": {"a": 1, "rgb": 11621088}},
    {"query": "tag:#writes",      "color": {"a": 1, "rgb": 14725458}},
    {"query": "path:CAO/",        "color": {"a": 1, "rgb": 15687168}}
  ]
}
EOF
}

write_settings() {
  mkdir -p "$CAO_HOME_DIR"
  # Derived, not hand-written: a project mapping's scope id must be the one CAO
  # will compute for this directory, or the mapping resolves for nobody.
  local proj
  proj="$(py -c 'from pathlib import Path
from cli_agent_orchestrator.services.memory_service import resolve_project_id
print(resolve_project_id(Path.cwd()))')"

  # max_recall_body_chars is set explicitly because the default (4096) exceeds
  # the injection scope budget (1000) and warns on every single command.
  cat > "$CAO_HOME_DIR/settings.json" <<EOF
{
  "memory": {
    "vault": {
      "enabled": true,
      "max_recall_body_chars": 1000,
      "vaults": [
        {
          "id": "demo",
          "root": "$VAULT",
          "managed_folder": "CAO",
          "mappings": [
            {"folder": "Notes", "scope": "global", "index": true},
            {"folder": "CAO", "scope": "project", "scope_id": "$proj", "index": true, "writable": true}
          ]
        }
      ]
    }
  }
}
EOF
  printf 'project scope_id for %s: %s\n' "$REPO" "$proj"
}

# ------------------------------------------------------------------ subcommands

cmd_up() {
  [ -e "$DEMO" ] && die "$DEMO already exists — run '$ME down' first, or set CAO_VAULT_DEMO_DIR"
  mkdir -p "$DEMO"
  write_notes
  write_obsidian_config
  write_settings

  # On a fresh home the vault tables do not exist yet, and a vault command run
  # before init dies with a raw SQLAlchemy 'no such table: vault_note'.
  cao init >/dev/null

  echo
  echo "== reconcile --apply =="
  cao memory vault reconcile --apply

  echo
  cmd_state
  cat <<EOF

Scratch tree: $DEMO
  vault      $VAULT
  CAO home   $CAO_HOME_DIR   (your real home is untouched)

Next:
  $ME obsidian              open it in Obsidian and look at the graph
  $ME recall "which frontmatter fields does cao read"
  $ME sync                  after you edit a note, in Obsidian or otherwise
  $ME guards                see what a secret, a symlink and a bad type do
  $ME down                  delete the whole thing
EOF
}

cmd_state() {
  have_vault
  echo "== notes =="
  q "select cao_key, scope, vault_relpath, status from vault_note order by status, vault_relpath;"
  echo
  echo "== findings =="
  local f
  f="$(q "select vault_relpath, code, severity from vault_finding order by severity, vault_relpath;")"
  printf '%s\n' "${f:-(none)}"
  echo
  echo "== vault edges =="
  q "select source_key, type, target_key from memory_relationships where origin='vault' order by source_key, target_key;"
  echo
  q "select (select count(*) from vault_note) as notes,
            (select count(*) from vault_note where status='indexed') as indexed,
            (select count(*) from vault_note where status='quarantined') as quarantined,
            (select count(*) from memory_relationships where origin='vault') as edges,
            (select count(*) from vault_finding) as findings;"
}

cmd_obsidian() {
  have_vault
  cat <<EOF
Obsidian has no URL scheme for registering a folder it has never seen, so this
is a two-click manual step rather than something the script can do for you:

  Obsidian -> File -> Open folder as vault -> $VAULT

Or use the vault switcher (bottom-left) -> "Open folder as vault". Your existing
vaults are untouched; this is added alongside them, and 'down' only deletes the
folder, so remove it from the switcher yourself afterwards if you care.

Once it is open:
  - Graph view (left ribbon) — the notes are colour-grouped by tag and the
    demo's links are already the graph's structure.
  - Backlinks pane on any note — an independent link parser. Compare it with
    the 'vault edges' table from '$ME state'; a disagreement is a real bug,
    except for CAO/Agent Notes.md, whose links cross a scope boundary and so
    appear in Obsidian but in no CAO edge. That one is expected.
  - After 'guards', the dangling link shows up as an unresolved (hollow) node,
    which is Obsidian's view of the link_dangling finding.
  - Edit any note, save, then run '$ME sync' and '$ME state' to watch CAO pick
    up exactly what changed.
EOF
  command -v open >/dev/null && open -R "$VAULT" 2>/dev/null || true
}

cmd_recall() {
  have_vault
  [ $# -ge 1 ] || die "usage: $ME recall \"a question\""
  # There is no --query on `cao memory list`; BM25 retrieval lives at the
  # service/MCP/API layer only, so this is the call an agent actually makes.
  CAO_DEMO_QUERY="$*" py -c '
import asyncio, logging, os
logging.disable(logging.WARNING)
from cli_agent_orchestrator.services.memory_service import MemoryService
hits = asyncio.run(MemoryService().recall(query=os.environ["CAO_DEMO_QUERY"]))
if not hits:
    print("(no hits — wrong cwd for a project mapping, or a stale CAO_TERMINAL_ID)")
for m in hits:
    kind = getattr(m, "source_kind", "native")
    print(f"{m.key}  [{m.scope}/{m.memory_type}]  {kind}")
    for line in (m.content or "").strip().splitlines()[:2]:
        print("    " + line)
' 2>&1 | grep -viE 'candidate resolution'
}

cmd_sync() {
  have_vault
  # The dry-run and --apply summaries are byte-identical, so the only way to see
  # that one of them wrote nothing is to fingerprint the stored hashes either
  # side of it. That is the whole point of this subcommand.
  local before during after
  before="$(index_digest)"
  echo "== reconcile (dry run — this is the default) =="
  cao memory vault reconcile
  during="$(index_digest)"
  echo
  echo "== reconcile --apply =="
  cao memory vault reconcile --apply
  after="$(index_digest)"
  echo
  printf 'index digest before      %s\n' "$before"
  printf 'index digest after dry   %s   %s\n' "$during" \
    "$([ "$before" = "$during" ] && echo '<- unchanged, as it should be' || echo '<- CHANGED: a dry run wrote to the index')"
  printf 'index digest after apply %s   %s\n' "$after" \
    "$([ "$during" = "$after" ] && echo '<- unchanged: nothing on disk differed from the index' || echo '<- changed, your edit landed')"
}

# A fingerprint of every stored content hash, so a write is visible without
# having to eyeball per-note shas.
index_digest() {
  sqlite3 "$DB" "select cao_key||':'||content_sha256||':'||status from vault_note order by cao_key;" \
    | shasum | cut -c1-12
}

cmd_guards() {
  have_vault
  echo "Planting four notes, each defective in a different way."
  printf -- '---\ncao:\n  key: demo-secret\n  type: reference\n---\n\nAWS_SECRET_ACCESS_KEY=AKIAIOSFODNN7EXAMPLE\n' \
    > "$VAULT/Notes/Has Secret.md"
  printf -- '---\ncao:\n  key: demo-dangling\n  type: reference\n---\n\nSee [[No Such Note]] for details.\n' \
    > "$VAULT/Notes/Has Dangling Link.md"
  printf -- '---\ncao:\n  key: demo-bad-type\n  type: decision\n---\n\n`decision` is not in the closed set.\n' \
    > "$VAULT/Notes/Has Bad Type.md"
  mkdir -p "$DEMO/outside"
  echo "this file is outside the vault and must never be read" > "$DEMO/outside/Target.md"
  ln -sf "$DEMO/outside/Target.md" "$VAULT/Notes/Escapes Vault.md"

  echo
  cao memory vault reconcile --apply
  echo
  cmd_state

  # Don't take quarantine on trust: go looking for the content and show that it
  # is not there. A status column saying "quarantined" and a corpus that still
  # contains the body would look identical up to this point.
  echo
  echo "== is the quarantined content actually gone from recall? =="
  # Query for the forbidden text, then search every returned body for it. A hit
  # count alone would prove nothing: these queries share vocabulary with the
  # legitimate notes, so recall returns rows either way. What matters is whether
  # the quarantined bytes are among them.
  py -c '
import asyncio, logging
logging.disable(logging.WARNING)
from cli_agent_orchestrator.services.memory_service import MemoryService
svc = MemoryService()

def probe(label, query, needle=None, want_key=None):
    hits = asyncio.run(svc.recall(query=query))
    bodies = "\n".join(m.content or "" for m in hits)
    keys = {m.key for m in hits}
    if needle is not None:
        leaked = needle in bodies
        verdict = "LEAKED - quarantine is not holding" if leaked else "absent, as intended"
    else:
        present = want_key in keys
        verdict = "present, as intended" if present else "MISSING - flagged note was dropped"
    print(f"  {label:<32} {len(hits):>2} hit(s)  {verdict}")

# Deliberately a query that DOES match the legitimate notes: a zero-hit query
# cannot leak anything, so it would prove nothing about the gate.
probe("bytes of the credential", "a detected credential in the body",
      needle="AKIAIOSFODNN7EXAMPLE")
probe("body of the symlink target", "outside the vault never be read",
      needle="must never be read")
probe("the merely-flagged note", "No Such Note for details", want_key="demo-dangling")
' 2>&1 | grep -viE 'candidate resolution'

  cat <<'EOF'

Three of the four are quarantined, so their content never becomes recallable:
  secret_detected     a credential in the body
  symlink_refused     a symlink whose target is outside the vault
  invalid_cao_block   cao.type outside {user, feedback, project, reference}

The fourth is only flagged:
  link_dangling       info severity, note still indexed, contributes no edge

That asymmetry is the intended design — an unwritten link target is normal
Obsidian practice — but it is the one worth confirming by hand, because
"flagged" and "quarantined" look identical until you check whether the content
comes back from recall.
EOF
}

cmd_inject() {
  have_vault
  echo "Injected context with no terminal identity, and with an unknown one:"
  py -c '
import logging; logging.disable(logging.WARNING)
from cli_agent_orchestrator.services.memory_service import MemoryService
svc = MemoryService()
print("  no terminal_id  ->", repr(svc.get_memory_context({"cwd": "."})))
print("  bogus terminal  ->", repr(svc.get_memory_context({"cwd": ".", "terminal_id": "nope"})))
' 2>&1 | grep -viE 'candidate resolution'
  cat <<'EOF'

Both empty. Injection resolves the requester from the caller's context and will
not fall back to the ambient CAO_TERMINAL_ID, so an unidentifiable caller gets
no vault content at all rather than someone else's. Seeing the populated arm
needs a real CAO terminal; this subcommand only pins the fail-closed one.
EOF
}

cmd_down() {
  [ -e "$DEMO" ] || die "nothing at $DEMO"
  case "$DEMO" in
    */cao-vault-demo|*/cao-vault-demo/*) : ;;
    *) [ -f "$CAO_HOME_DIR/settings.json" ] || die "refusing to delete $DEMO: does not look like a demo tree" ;;
  esac
  rm -rf "$DEMO"
  echo "removed $DEMO"
  echo "(if you opened it in Obsidian, remove it from the vault switcher too)"
}

case "${1:-}" in
  up)       shift; cmd_up "$@" ;;
  state)    shift; cmd_state "$@" ;;
  obsidian) shift; cmd_obsidian "$@" ;;
  recall)   shift; cmd_recall "$@" ;;
  sync)     shift; cmd_sync "$@" ;;
  guards)   shift; cmd_guards "$@" ;;
  inject)   shift; cmd_inject "$@" ;;
  down)     shift; cmd_down "$@" ;;
  # Print the header comment block and stop at the first line that isn't one,
  # so the usage text can't drift out of sync with the script's length.
  *) awk 'NR==1 {next} /^#/ {sub(/^# ?/, ""); print; next} {exit}' "${BASH_SOURCE[0]}"; exit 1 ;;
esac
