# PII / Secret Leak Response & Git-History Scrub Runbook

What to do when a secret or PII lands in the repository — on a branch, in a PR,
or on `main`. The guiding principle: **rotation/revocation is the fix; rewriting
history reduces exposure of an already-compromised secret but never substitutes
for rotating it.** Assume any secret is captured the instant it is pushed.

This runbook is referenced from [SECURITY.md](../SECURITY.md). Prevention
(the secret scanner that keeps most leaks out) is documented there and in
[`.gitleaks.toml`](../.gitleaks.toml).

## 0. Triage first — severity decides the response

| Leak type | Rotate/revoke? | Rewrite history? | Notes |
|---|---|---|---|
| Live credential (API key, token, private key, password) | **Yes — immediately, first** | Yes, after rotation | Assume compromised the moment it hit a pushed commit. |
| Internal hostnames / infra details | Case-by-case | Usually no | Evaluate exposure; often just remove going forward. |
| Personal PII (email, name) | N/A (rotation impossible) | Rarely — only on request / legal need | Remove going forward; notify the person; weigh rewrite cost. |
| Synthetic/sample data (`user@example.com`, `AKIAIOSFODNN7EXAMPLE`) | No | No | Not a leak. |

**Golden rule:** rotate/revoke first. A history rewrite on a public repo is
high-cost and never makes an exposed live secret safe again — only rotation
does.

## 1. Contain (minutes)

1. **Rotate/revoke** the leaked credential at its source (cloud console, token
   settings, key authority). Do this before anything else.
2. If a scheduled deploy or workflow could exfiltrate further, pause it.
3. Open a private security advisory / notify maintainers per
   [SECURITY.md](../SECURITY.md). **Do not** disclose the secret value in a
   public issue or PR comment.

## 2. Assess scope

```bash
# Read the value once via a hidden prompt so it never lands in shell history.
# Pipe it to git from stdin (never as an argv arg, which `ps` would expose).
read -rs -p 'Leaked value: ' SECRET; echo

# Commits + paths that contain the string. -l = list matching blobs only (path,
# no matching line — so the secret itself is never printed); -F = fixed string;
# -f - = read the pattern from stdin. Output rows are "<commit>:<path>".
# NOTE: $(git rev-list --all) expands as arguments. For repos with >100k
# commits this can hit shell ARG_MAX; if so, pipe through xargs:
#   git rev-list --all | xargs git grep -lF -f <(printf '%s' "$SECRET")
printf '%s' "$SECRET" | git grep -lF -f - $(git rev-list --all) | sort -u > /tmp/hits.txt
cut -d: -f1 /tmp/hits.txt | sort -u   # offending commits
cut -d: -f2- /tmp/hits.txt | sort -u  # offending paths
rm -f /tmp/hits.txt
unset SECRET

# For each offending commit, find which branches and tags reach it:
git branch --all --contains <commit>
git tag --contains <commit>
```

Record the affected **files, commits, branches, and tags** — you need the branch
and tag list to know the full blast radius and what must be re-cut in step 5.
(`git grep -l` deliberately omits the matching line so the secret value is never
echoed to your terminal or CI logs.)

## 3. Coordinate before rewriting

- A default-branch rewrite changes every downstream SHA and **breaks every
  existing clone, fork, and open PR**. Get maintainer sign-off first.
- Announce a short freeze window; ask contributors to pause pushes.

## 4. Execute the scrub (prefer `git filter-repo`)

```bash
# Fresh mirror clone — never rewrite your working clone.
git clone --mirror git@github.com:awslabs/cli-agent-orchestrator.git cao-scrub
cd cao-scrub

# a) Redact a string everywhere. Keep the secret out of shell history AND out of
#    process args (`ps`): read it from a hidden prompt into a mode-0600 file.
#    An idempotent cleanup, trapped on INT/TERM/EXIT, removes the plaintext even
#    if filter-repo is Ctrl-C'd; we also delete it explicitly the moment
#    filter-repo finishes. One `OLD==>NEW` per line.
repl="$(mktemp)"; chmod 600 "$repl"
cleanup() { [ -n "${repl:-}" ] && { shred -u "$repl" 2>/dev/null || rm -f "$repl"; }; repl=; }
trap cleanup INT TERM EXIT            # covers Ctrl-C, kill, and normal exit
read -rs -p 'Leaked value to redact: ' SECRET; echo
printf '%s==>REDACTED\n' "$SECRET" > "$repl"
unset SECRET                          # drop it from the shell environment
git filter-repo --replace-text "$repl"
cleanup; trap - INT TERM EXIT         # remove plaintext now; clear the trap

# b) OR purge whole files/paths from all history (no secret on disk at all):
git filter-repo --path path/to/leaky-file --invert-paths

# Verify the secret is gone from all history. -F = fixed string, -f - reads the
# pattern from stdin, -q suppresses the matching line so the secret is never
# echoed. git grep exits 0 = FOUND, 1 = absent, >1 = ERROR — distinguish all
# three so an error (e.g. a bad ref, exit 128) is never misread as "clean".
# (Same ARG_MAX note as above applies for very large repos.)
read -rs -p 'Value to verify absent: ' CHECK; echo
printf '%s' "$CHECK" | git grep -qF -f - $(git rev-list --all); rc=$?
unset CHECK
case "$rc" in
  0) echo "STILL PRESENT — do not publish" ;;
  1) echo "clean" ;;
  *) echo "ERROR running git grep (exit $rc) — verify manually before publishing" ;;
esac
```

`git filter-branch` is deprecated — avoid it. BFG Repo-Cleaner
(`bfg --replace-text` / `--delete-files`) is an acceptable alternative.

> **Note:** `git filter-repo` removes the `origin` remote by design (to stop you
> accidentally pushing a half-rewritten history). Re-add it before publishing.

## 5. Publish the rewritten history

```bash
# filter-repo dropped 'origin' — re-add it, then force-push all refs from the mirror.
git remote add origin git@github.com:awslabs/cli-agent-orchestrator.git
git push --force --mirror origin   # after maintainer sign-off
```

Expected results and gotchas:

- **`refs/pull/*` errors are expected and OK.** GitHub advertises PR refs but
  they are **read-only** — `--mirror` will try to update them and print
  `[remote rejected] refs/pull/... (deny updating a hidden ref)`. Ignore those.
  **Every _other_ ref (branches, tags) must succeed** — if a branch push is
  rejected, that's the real problem, not the PR refs.
- **Branch protection** on `main` will reject a force-push. Temporarily relax
  the protection rule (or use an admin override), push, then **re-enable it
  immediately**.
- **Delete/re-cut affected tags and releases** — they pin old SHAs and a
  release asset can still reference the pre-scrub commit.
- **Contact GitHub Support** to purge cached views of old commits and PR diffs
  and to GC unreachable objects — force-pushing alone leaves the old SHAs
  reachable via the API / PR timeline for a while.

## 6. Everyone re-syncs

Rewritten history means every existing clone, fork, and open PR now descends
from commits that no longer exist. **A plain `git pull`/merge would re-introduce
the removed history** — so it must not be used.

- **Simplest and safest: re-clone.** Tell contributors to delete their clone and
  clone fresh.
- **To keep local work**, rebase or cherry-pick it onto the rewritten branch —
  never merge:
  ```bash
  git fetch origin
  git rebase --onto origin/main <old-base> <your-feature-branch>
  ```
- **Open PRs** do not rebase themselves when closed/reopened. Each author must
  rebase their branch onto the rewritten base (as above) and force-push, or
  re-open the PR from a freshly-rebased branch. Stale branches and tags that
  still point at pre-scrub commits must be deleted or rebased too, or they
  re-expose the secret.
- **Forks are separate repositories you cannot rewrite.** Every fork keeps the
  leaked commit in its own history; a force-push to the canonical repo does not
  touch them, and you have no write access to purge someone else's fork. Ask
  fork owners to re-sync (or delete) their copy, but treat this as best-effort —
  which is another reason the leaked credential must be **rotated**, never merely
  scrubbed: you cannot guarantee removal from every fork the secret reached.

## 7. Post-incident

- Confirm the credential is fully rotated and the old one is dead.
- Add or extend a scanner rule so the specific pattern is caught in future
  (see [`.gitleaks.toml`](../.gitleaks.toml)).
- Write a short post-mortem: what leaked, how, blast radius, time-to-rotate.

## Prevention (steady state)

- **Secret scanning in CI.** gitleaks runs on every PR (commit range) and weekly
  over full history — see
  [`.github/workflows/secret-scan.yml`](../.github/workflows/secret-scan.yml)
  and [`.gitleaks.toml`](../.gitleaks.toml). Run it locally with
  `scripts/security-scan.sh gitleaks`.
- **Optional pre-commit hook.** [`.pre-commit-config.yaml`](../.pre-commit-config.yaml)
  wires gitleaks' staged-diff scan (`gitleaks git --staged`) so a secret is
  caught before it enters a commit. Opt in with `pre-commit install`. CI is the
  backstop, so the hook is a convenience, not a requirement.
- **Fixture-recording hygiene** — see
  [CONTRIBUTING.md](../CONTRIBUTING.md#recording-test-fixtures-safely):
  capture live-CLI fixtures on a synthetic/throwaway account and scrub any
  login/banner line before committing.
- **The runtime redactor** (`src/cli_agent_orchestrator/services/secret_gate.py`)
  strips credential shapes from memory writes and archive exports at runtime.
