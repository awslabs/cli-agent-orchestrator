# pr-watch: the zero-token PR poll loop

The shepherd's only poll. It reads GitHub with `gh`, decides in plain Python whether anything
needs a model, and wakes the shepherd only then. It never writes to GitHub.

| File | What it does |
|---|---|
| `tick_loop.sh` | The loop: poll every `INTERVAL` seconds (1800 by default), one retry on errors, an atomic `mkdir` lock so only one loop runs, `LOOP_OWNER` from `CAO_TERMINAL_ID`. Exits with `WAKE <report> [retry=<report2>] :: <reasons>` on a real event. |
| `tick_supervisor.sh` | Keeps exactly one `tick_loop.sh` running, detached from any agent's shell. It posts each WAKE to the shepherd's CAO inbox and restarts the loop by itself. The conductor starts it; the shepherd never starts or restarts the loop. |
| `poll.py` | The read-only poller. Fetches each watched PR and issue through `gh api graphql`, diffs it against the last snapshot in `state/`, and prints the new events as JSON. Also lists your own open PRs that aren't watched yet, and watches the PRs in your review ledger (`../reviews/ledger.json`, the PRs you reviewed) for new commits and replies. |
| `wake_rules.py` | The pure wake policy. One JSON report in, `WAKE <reasons>`, `DIGEST <lines>` or `QUIET` out. No file reads; `decide(report)` returns structured reasons for tests and adapters. |
| `digest_store.py` | The durable adapter around the policy: atomic report and queue writes, the digest that rides along with the next wake, and the wake journal. |
| `summary.py` | Prints a compact table of one report, for the shepherd to read a wake. |
| `config.py`, `config.example.json` | Paths (watch list, ledger, digest, queue, journal). Without a `config.json`, everything lives next to the scripts and the ledger is `../reviews/ledger.json`. `PR_WATCH_CONFIG` points at another config file. It also reads your login from the kit's `workplace.local.json`, and checks the writer lease before any state write. |
| `watch.example.json` | The watch list to copy to `watch.json`. |
| `RUNBOOK.md` | How to read one tick by hand. |

## Who is "ours"
Only your login, `github_login` in the kit's `workplace.local.json`, counts as ours. Feedback on
your own PRs wakes the shepherd; your own comments never do. An author that isn't yours is never
guessed to be. If the login isn't set, the loop stops with `configuration_error` instead of
treating your own PRs as someone else's. The auto-merge gate reads `watch.json` `identities`,
which `bin/remap_owners.py` fills from the same login.

## The wake journal
`poll.py` appends its whole report to `state/wake-journal.json` (an atomic replacement)
**before** it saves the new snapshots and seen ids. `digest_store.py` then records the WAKE
reason in that entry, or retires it for QUIET and DIGEST ticks (their lines are already in the
digest queue). `--delivered` retires it only after the loop has printed the WAKE line. Every
start and every tick first runs `digest_store.py --replay`: the oldest undelivered WAKE is
printed as `WAKE <report> replay :: <original reason>` before any new poll. So an interruption
before the WAKE is printed can't turn into a quiet tick. A crash after the WAKE line but before
`--delivered` repeats that WAKE at most once, on the next start.

The loop has handed the WAKE over once it prints it, so the supervisor makes the delivery safe.
It writes the WAKE line to `state/tick_supervisor.pending` (fsync'd) **before** the first delivery
attempt, then delivers that file oldest line first, with a short doubling backoff, and removes each
line only after a 2xx. No new loop starts while a line is left. A TERM is taken between steps, so a
stop, a kill or a cao-server restart during the retries delays a wake but can't lose it: the next
cycle, or the next supervisor, delivers it. A crash right after a 2xx, before the line is removed,
repeats that WAKE at most once. Blank lines in the file are skipped. If the path isn't a usable
regular file (a directory, a symlink, unreadable), the supervisor logs one `STOP: pending file
unusable` line, exits 4 and leaves it as it is. On `not_writer` it stops with exit 3 and writes one
`NOT_WRITER …` line to its log and to `state/tick_supervisor.stopped`; if the inbox was down, that
line says the notice is waiting in the pending file.

## The digest
Quiet updates (activity outside the wake scope) go into `state/digest-pending.json` and
`digest.md`. They ride along with the next wake, and wake the shepherd once by themselves on
the first poll after the oldest line is more than 24 hours old.

## Running it
The conductor starts `tick_supervisor.sh`, after the shepherd reports READY and after
`pgrep -fl tick_loop.sh` prints nothing (`conductor-playbook.md` §9). The supervisor runs the loop
detached, so the loop doesn't die with an agent's background job, and a wake isn't lost when an
agent's turn ends. It writes `state/tick_supervisor.pid` and stops for good on `not_writer` (the
writer lease moved to another host). To replace the shepherd, stop the supervisor first, then the
loop. The loop's lock is left in place after a `kill -9`, on purpose: check that its PID no longer
runs the watcher, then remove the exact `state/tick_loop.lock/pid` file and `rmdir` the lock
directory.

Don't run `poll.py --baseline` on a live watch list: it saves snapshots without reporting
events, so feedback waiting since the last poll would be lost.

## Tests
```sh
PYTHONPATH=pr-watch python3 -m pytest -p no:cacheprovider -q pr-watch/tests
bash -n pr-watch/tick_loop.sh pr-watch/tick_supervisor.sh
```
The tests mock GitHub and the poller; they exercise the real `poll.main`, the loop, the CLI
and the digest paths without network access.
