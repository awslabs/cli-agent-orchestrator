# COND-0324 fork-side report

- Base: `d42375743a637cc3b7eb8afa3e7bc1c5ec98dd80`
- Current head: `f54e006` (exact PR head; report commit is included)
- PR: https://github.com/colindmurray/cli-agent-orchestrator/pull/74

## Scope

Adds the read-only `GET /terminals/{terminal_id}/composer-observation`
capability required by conductor recovery. The route is advertised only for a
managed native terminal with a pinned provider/build layout, rechecks pane and
server identity under the pane-input lease, and returns only typed metadata,
digests, byte counts, and a non-secret capture reference. It never returns raw
composer text or sends input.

The extractor preserves the conductor's raw UTF-8 fingerprint contract. It
returns a positive observation only for an unwrapped, single-row payload whose
frame padding is unambiguous; wrapped or whitespace-ambiguous captures fail
closed.

## Verification

- `git diff --check`: passed
- Focused composer route/service tests: 23 passed
- Related control, input, inbox, terminal, and provider suites: 777 passed,
  1 skipped
- PR CI is running; no merge or deployment has been performed.

The final commit also normalizes Black/isort formatting in four pre-existing
Muse/native-launch files. The exact base failed the repository's Code Quality
job on those files before this feature; the formatting-only commit makes that
baseline gateable without changing behavior.

## Limitations

Composer observation is build-pinned. Current pins are Codex 0.146.0 and Kimi
Code 0.29.2 as inherited from the existing live-verified layout tables. Newer
provider builds remain unsupported until their layout is independently proven.
