# Fork-session behaviour matrix

`fork-session-matrix.sh` exercises the Claude Code fork surface the provider's
`fork_from_session_id` depends on. It launches the real binary; it asserts nothing by
inference. Run it after a Claude Code upgrade — this is provider behaviour that a vendor
release can change without notice.

```sh
bash test/manual/fork-session-matrix.sh
column -ts $'\t' /tmp/forkmatrix/results.tsv
```

It costs a handful of one-word `haiku` turns.

## Result at Claude Code 2.1.235

15 of 16 properties hold. The one that does not is why the provider refuses a
self-directed fork.

| Case | Property | Result |
|---|---|---|
| A1 | a parent with one completed turn is forkable | pass |
| A2 | a fork of a zero-turn parent refuses | pass |
| B1 | the destination id is dictated by the caller, not parsed | pass |
| B1b | the fork inherits the parent's context | pass |
| B1c | the fork writes its own transcript | pass |
| B3 | **a fork onto its own source id leaves the parent intact** | **FAILS** |
| B4 | a fork onto a different existing id refuses | pass |
| B5 | a malformed destination id refuses | pass |
| C2 | three concurrent forks inherit and stay distinct | pass |
| D1 | a fork of a fork inherits both generations | pass |
| D1b | the intermediate fork is unchanged by its own child | pass |
| E2 | a fork from a different working directory inherits context | pass |
| G1/G2/G3 | the parent is unchanged by forking, and stays forkable | pass |
| H1 | a fork may run a different model than its parent | pass |

## B3 — the asymmetry the provider closes

Claude Code protects *other* sessions from an id collision and does not protect the
resumed session from colliding with itself:

- `--resume A --fork-session --session-id B`, where B already exists → refuses with
  `Error: Session ID B is already in use.`, exit 1, B untouched.
- `--resume A --fork-session --session-id A` → **does not refuse**. It exits 0, silently
  ignores the requested id and returns a different ambient one, and appends the turn to
  A's transcript.

Three failures in one call: the parent's priming is destroyed, the caller's chosen id is
not honoured, and the returned id is one nothing recorded. For a master whose entire value
is being correctly primed, that is the expensive direction to get wrong, and it is silent.

The provider therefore refuses `fork_from_session_id == native_session_id` in its
constructor, before the launch reaches tmux.

## Working directory

Not a constraint. Transcripts live under `~/.claude/projects/<slugified-cwd>/`, but
`--resume` resolves an id across those directories, so a forked worker keeps its own
worktree. Verified in both directions (E2).
