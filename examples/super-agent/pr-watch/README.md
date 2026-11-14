# pr-watch: the zero-token PR poll loop

The shepherd's only poll. It reads GitHub with `gh`, decides in plain Python whether anything
needs a model, and wakes the shepherd only then. It never writes to GitHub.

| File | What it does |
|---|---|
| `tick_loop.sh` | The loop: poll every `INTERVAL` seconds (1800 by default), one retry on errors, an atomic `mkdir` lock so only one loop runs, `LOOP_OWNER` from `CAO_TERMINAL_ID`. Exits with `WAKE <report> [retry=<report2>] :: <reasons>` on a real event. |
| `poll.py` | The read-only poller. Fetches each watched PR and issue through `gh api graphql`, diffs it against the last snapshot in `state/`, and prints the new events as JSON. Also lists your own open PRs that aren't watched yet, and watches PRs in the reviewer's ledger for new commits and replies. |
| `wake_rules.py` | The pure wake policy. One JSON report in, `WAKE <reasons>`, `DIGEST <lines>` or `QUIET` out. No file reads; `decide(report)` returns structured reasons for tests and adapters. |
| `digest_store.py` | The durable adapter around the policy: atomic report and queue writes, the digest that rides along with the next wake, and the wake journal. |
| `summary.py` | Prints a compact table of one report, for the shepherd to read a wake. |
| `config.py`, `config.example.json` | Paths (watch list, ledger, digest, queue, journal). Without a `config.json`, everything lives next to the scripts and the ledger is `../reviews/ledger.json`. `PR_WATCH_CONFIG` points at another config file. |
| `watch.example.json` | The watch list to copy to `watch.json`. `identities.ours` names your login(s). |
| `RUNBOOK.md` | How to read one tick by hand. |

## Who is "ours"
Only the logins in `watch.json` `identities.ours` count as ours. Feedback on your own PRs wakes
the shepherd; your own comments never do. An author that isn't listed is never guessed to be
yours. The auto-merge gate reads the same block.

## The wake journal
`poll.py` appends its whole report to `state/wake-journal.json` (an atomic replacement)
**before** it saves the new snapshots and seen ids. `digest_store.py` then records the WAKE
reason in that entry, or retires it for QUIET and DIGEST ticks (their lines are already in the
digest queue). `--delivered` retires it only after the loop has printed the WAKE line. Every
start and every tick first runs `digest_store.py --replay`: the oldest undelivered WAKE is
printed as `WAKE <report> replay :: <original reason>` before any new poll. So an interruption
before the WAKE is printed can't turn into a quiet tick. A crash after the WAKE line but before
`--delivered` repeats that WAKE at most once, on the next start.

## The digest
Quiet updates (activity outside the wake scope) go into `state/digest-pending.json` and
`digest.md`. They ride along with the next wake, and wake the shepherd once by themselves on
the first poll after the oldest line is more than 24 hours old.

## Running it
Only the shepherd starts the loop, as one background job, after `pgrep -fl tick_loop.sh` prints
nothing (`shepherd-playbook.md` §1). The lock is left in place after a `kill -9`, on purpose:
check that its PID no longer runs the watcher, then remove the exact `state/tick_loop.lock/pid`
file and `rmdir` the lock directory.

Don't run `poll.py --baseline` on a live watch list: it saves snapshots without reporting
events, so feedback waiting since the last poll would be lost.

## Tests
```sh
PYTHONPATH=pr-watch python3 -m pytest -p no:cacheprovider -q pr-watch/tests
bash -n pr-watch/tick_loop.sh
```
The tests mock GitHub and the poller; they exercise the real `poll.main`, the loop, the CLI
and the digest paths without network access.
