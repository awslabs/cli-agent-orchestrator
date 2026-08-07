# Native session claims: holding one, and giving it back

A provider-native session — a Kimi session id, a Codex conversation, a
Claude Code session — is a single-writer resource. Two processes attached
to one of them interleave their turns into a single transcript and corrupt
both histories, and nothing downstream detects it, because each
attachment's own receipts look perfectly consistent.

`services/native_attachment.py` is the only way to acquire one. This
document is about the other half: giving it back.

---

## 1. What was wrong

`native_attachment.release()` was implemented, documented, and covered by
fourteen unit tests. It had **zero production call sites**. Its only
non-test mention anywhere in `src/` was a sentence in a docstring.

So every claim the system ever took stayed in a live state forever. On the
install this was found on:

| | |
|---|---|
| attachment rows | 258 |
| `attached` | 251 |
| `ambiguous` | 7 |
| `detached` | **0** |
| rows carrying a `release_proof_json` | **0** |
| distinct `epoch` values | **1** (every row at `epoch = 2`) |

`release()` bumps the epoch and writes the proof, so a table where every
row sits at the declare→starting→attached epoch with no proof is direct
on-disk evidence that it never ran once in fourteen days.

The consequence is not cosmetic. `declare()` refuses a live owner, so
**every provider-native session on that install was permanently
unresumable** — the claim outlived the process, the terminal row, and in
most cases the tmux server.

A store that only ever acquires is not a lock. It is a leak with a
conflict check on it.

---

## 2. What decides that an owner is gone

One thing, and deliberately only one: `os.kill(pid, 0)` raising
`ProcessLookupError`.

Everything else — alive, alive under a different start marker, unreadable,
or never published at all — holds the claim exactly where it is.

### Why the start marker never decides

Ownership is `(pid, start_marker)`, never a bare pid, because pids are
recycled and a stale one can "forge a survivor — or, worse, forge a
*no*-survivor." That reasoning is right, and it does not mean the marker
should be part of the release test.

The stored marker is raw `ps -o lstart=` output: naive local wall-clock,
rendered in the writing process's timezone and locale, with BSD's
two-space padding before a single-digit day.

```
{"pid": 21063, "start_marker": "Sat Jul 25 15:08:27 2026"}
```

A rule of the form *"pid alive but the marker differs → the pid was
recycled → release"* reads a DST rollover, a `TZ` change, or a locale
change as evidence that a **live** owner is a stranger, and hands its
session to a second attacher. That is the exact failure the module calls
"worse", reached by the mechanism meant to prevent it.

`ProcessLookupError` has no such dependency. An absent pid is absent in
every timezone.

The marker is still read and still recorded — it is what tells an operator
whether a live pid is plausibly the original owner. It just never decides
anything.

### What that costs

On the install this was found on, of 251 published identities:

| observation | rows | outcome |
|---|---|---|
| pid absent | 231 | released |
| pid alive, marker matches | 19 | held — genuinely running workers |
| pid alive, marker differs | 1 | held — really a recycled pid |

The nineteen are real. They were cross-confirmed three ways: matching
`lstart`, the exact `native_session_id` in sixteen of the nineteen command
lines, and a currently-existing tmux pane whose `pane_pid` equals the
stored pid.

**All nineteen look like orphans if you ask the wrong question.** Not one
of their owning terminals still exists in the `terminals` table. A cleanup
that decided orphanhood by "is the owner still in the terminals table?"
would have released nineteen live sessions.

The recycled-pid row is the price: its true owner is dead, but automation
cannot prove that without trusting the marker. It stays claimed until a
human looks at the live process and attests that it is not the recorded
owner — see §3. Unresolved is survivable. A forged no-survivor is not.

### Signals that are *not* survivor oracles

| signal | why not |
|---|---|
| presence in `terminals` | says all 258 are orphans, including the 19 live ones |
| `managed_launch_v2_terminals.v2_lifecycle_state` | 46 rows say `live`; 19 are |
| `owner_pane_id` exists in tmux | pane ids recycle — 62 rows point at a live pane, 19 are its occupant |
| `native_tui_launch._process_field` returning `None` | means *either* "pid gone" *or* "`ps` could not be run" |

That last one matters most. A no-survivor proof must be an observation
that was actually made; "we did not look" must never be recorded as
"nothing is there".

### A claim with no published identity is never released

`declare()` journals the claim *before* the provider process exists. A row
with no `process_identity` is either a launch in flight or a crash during
one, and nothing observable tells those apart. The sweep counts them and
releases none.

---

## 3. Where claims get resolved

### Terminal teardown

`terminal_service._delete_terminal_claimed` is the funnel every deliberate
teardown passes through. The release runs after the window kill — the
first moment the owner can be observed absent — and before the row is
deleted, so it is still inside the generation claim.

`kill-window` sends SIGHUP and returns; the provider still gets to shut
itself down. Teardown therefore waits briefly, and only while the answer
is still "alive", for the process to leave the table.

It never raises. The window is already killed and the row is about to go;
aborting there would leave worse state than the claim it was resolving.

**An owner still alive when the grace expires is left attached**, not
frozen. Freezing was the first design here and it was wrong twice over.
`mark_ambiguous` is terminal for automation, so it would convert a state
the sweep resolves on its own — the provider exits a second later, or a
minute later, and the next sweep releases it — into one that permanently
requires a human. And it mislabels the evidence: "frozen" means ownership
could not be determined, and here it was determined exactly, with the
answer "still running".

Nothing is lost by leaving it. The claim is listed, the sweep reports it
every pass, and the release happens the moment the process is gone.

### The sweep

Teardown can only resolve claims it is present for. The largest single
producer of lost claims is the **server exiting**, which runs no teardown
at all: there is no shutdown hook that enumerates terminals, and the tmux
backend has no boot-time adjudication. Every one of those claims outlives
both its process and the row pointing at it.

```
cao attachment sweep              # report; changes nothing
cao attachment sweep --apply      # release the provably gone
```

At boot the server runs the same pass **in report-only mode** and logs the
command when it finds something. Set `CAO_ATTACHMENT_SWEEP_ON_BOOT=apply`
to let it act.

The default is conservative for a specific reason: entering this
application's lifespan is something a *test* does, and two tests in this
repository do it without stubbing the recovery steps. A boot sweep that
mutated by default would release rows out of an operator's real database
as a side effect of running `pytest`.

### Operator freeze, then adjudication

`mark_ambiguous` freezes a claim whose ownership could not be resolved,
and the module is emphatic that automation must never undo that:
auto-releasing an ambiguous row *is* the double-attach it exists to
prevent.

It also said "a human resolves it" — and no human could. `release()`
refuses a frozen row before it looks at a proof, `mark_ambiguous` had no
inverse, and neither had an API route or a CLI command. The only valve was
editing the database by hand.

```
cao attachment show kimi_cli session_04c87e57
cao attachment adjudicate kimi_cli session_04c87e57 \
    --operator colin \
    --detail "pane and process both gone for six days" \
    --evidence ./ps-output.txt
```

A claim that is *not* frozen but that the sweep can never settle has to be
declared unresolvable first:

```
cao attachment freeze kimi_cli session_04c87e57 \
    --operator colin --detail "process table will not answer for pid 21063"
```

Two steps, on purpose. Freezing says "I cannot determine this";
adjudicating says "I have decided anyway". Collapsing them would let the
second sentence be spoken without the first ever having been true.

Exactly two dispositions are freezable, and the refusals are the
interesting part.

| disposition | freezable | why |
|---|---|---|
| unobservable | yes | nothing further will ever answer |
| alive, marker **differs** | yes, on `--pid-is-recycled` | only a human can tell a recycled pid from a timezone change |
| alive, marker **matches** | no | that is the recorded owner, running |
| gone | no | the sweep releases it; there is no judgement to make |
| **no published identity** | **no** | see below |

A claim with no published identity looks like the most stuck row on the
list and is the most dangerous to touch. `declare` writes the claim before
the provider process exists, so a launch that is *currently cold-starting*
and a launch that crashed are the same row — `starting`, no pid, the same
line in `list`. Freezing one blocks its own `mark_attached`, so the
identity is never published, the process keeps running, and the
adjudication that follows sees an empty survivor list and hands the
session away underneath it. That is the double-attach this whole document
is about, reached through the command meant to repair it.

The recycled-pid attestation is made twice, and on purpose in two places.
`freeze --pid-is-recycled` is what lets a live-but-differing owner be
classified as unresolvable at all; `adjudicate --pid-is-recycled` is what
lets the decision be made past a live pid. The second is what
`adjudicate` actually reads, because `mark_ambiguous` preserves the
*first* freeze reason — so a row its launcher had already frozen for an
unrelated reason would silently drop an attestation attached to a later
freeze, and could then never be adjudicated while the stranger lived.

It is accepted only for a survivor whose marker was genuinely **read**
and genuinely differs. An unreadable marker is `None`, which is unequal
to everything; treating that as "different" would let an attestation
about a recycled pid release an owner nobody could identify either way.
And a survivor bearing the *recorded* marker is the owner itself, which
no attestation makes releasable.

`adjudicate`'s veto is on a process actually **seen** running, not on the
survivor list alone. `observe_owner` puts a conservative placeholder in
that list for an owner it could not check, which is right for automation —
an unchecked owner is treated as alive — and wrong here: an operator who
froze a claim *because* the process table would not answer would then be
refused with "the frozen owner is still observably alive" about a process
nobody observed. Having asked for a human, it would be perverse to refuse
their answer on evidence that does not exist.

An adjudication carries no owner, no pane, no identity — it is a human's
sentence. The epoch observed at the time is what binds it to the row it
was written about, and it is required: without it, an observation taken
before a confirmation prompt can be applied after the session was
released, re-claimed, and frozen again with a live process on it. The
state would still read `ambiguous` and the stale survivor list would still
read empty.

The adjudication is stored under its own schema —
`cao-native-attachment-adjudication-v1`, not the no-survivor schema — so a
later reader can always tell a human's judgement from an observed absence.
It records who decided, a digest of what they looked at, a bounded reason,
and whatever the system could still see at that moment.

**A live owner is refused here exactly as it is everywhere else.** An
operator may resolve an unresponsive owner, never a running one. The
observation is taken server-side rather than accepted from the caller: the
one party with a motive to release a session should not also supply the
evidence that doing so is safe.

---

## 4. Known gaps

- **Paths outside the teardown funnel.** The retention sweeper, the
  session-scoped bulk delete, and the `create_terminal` failure rollback
  delete terminal rows directly. Today only the v2 launch path records a
  native session id, so their exposure is theoretical — but the sweep is
  what covers them, not the wiring.
- **The recycled-pid row** still needs a human: `cao attachment freeze
  --pid-is-recycled` then `adjudicate`. Automation will never release it,
  and that is deliberate.
- **A crashed launch that never published an identity** has no operator
  valve, because it cannot be told apart from one that is still starting.
  No such row exists on the reference install — the launch path either
  reaches `attached` or freezes itself — but if one appears, the only
  remedy is a database edit.
- **No web surface.** The dashboard shows nothing about session claims.
  `run_manifest._attachment_projection` renders most of an operator view
  and has no callers; it drops `process_identity` and `pane_id`, which are
  the two fields an adjudicating human most needs.
