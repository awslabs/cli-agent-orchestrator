# Harness Lifecycle

The portfolio utility owns AI-DLC harness staging and projection. Do not copy
`.claude/` or `aidlc/` directly.

## Stage

`harness stage` reads one Claude distribution, hashes every file under
`.claude/` and `aidlc/`, records the Git revision when available, applies the
portfolio Opus model overlay, and writes `harness/claude/manifest.json`.

The default overlay maps `opus[1m]` to
`global.anthropic.claude-opus-5[1m]`. The source distribution is never
modified. Repeating the command with identical source bytes and overlay is a
no-op.

## Sync

`harness sync` verifies the staged files before projecting them into registered
worktrees. It replaces `.claude/` and `aidlc/` transactionally, writes a
worktree receipt under `.aidlc-portfolio/`, and adds a marked block to the
repository-local Git exclude file.

Sync refuses any worktree where `.claude/`, `aidlc/`, or `.aidlc-portfolio/`
contains tracked files. Project-owned configuration must be reconciled
explicitly rather than overwritten.

## Verify

`harness verify` compares:

1. Current source revision and hashes against the staged manifest.
2. Current staged files and model configuration against the manifest.
3. Every selected worktree and receipt against the staged revision.
4. Repository-local exclusion state.

Any source upgrade, staged mutation, local worktree mutation, stale receipt,
missing file, model drift, or missing exclusion fails verification and names
the affected worktree.
