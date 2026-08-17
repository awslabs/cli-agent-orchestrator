# Repository Agent Guidance

## Authority

- Current user direction and the live repository—especially accepted designs,
  source, tests, issue records, and Git history—are authoritative.
- Preserve unrelated work. Use dedicated worktrees for changes and do not
  rewrite or delete user state to simplify an implementation.

## Engineering threat model and proportionality

- CLI Agent Orchestrator is a trusted single-operator system running
  cooperative local agents on the operator's own machine. Agents commonly run
  unsandboxed and are expected to follow their assigned instructions. Do not
  design or review it as a hostile multi-tenant service.
- Guardrails exist to keep good-faith work out of illegal or corrupt states
  caused by stale observations, retries, duplicate effects, concurrency races,
  partial failure, ambiguous ownership, or accidental use of the wrong live
  resource. Prefer the smallest check at the real transition seam and leave an
  obvious recovery path.
- A local process could deliberately forge an opaque id, environment value,
  tmux pane id, loopback API request, or on-disk marker. That possibility alone
  is not a defect and does not justify credentials, signatures, hostile-caller
  ACLs, new human approval gates, extra leases/claims, or blocking a useful
  workflow. Add such machinery only when current user direction or a real
  external trust boundary requires it.
- Review findings must name a plausible cooperative failure sequence and its
  concrete state-integrity impact. "A malicious agent could..." is not enough.
  Treat protections aimed only at a hypothetical rogue local actor as YAGNI,
  especially when they reduce flexibility, autonomous recovery, or forward
  progress.
- Low-friction validation and observability are welcome when they prevent
  accidental drift or improve diagnosis. Defense in depth must not quietly
  become a second authority, a mandatory claim ceremony, or a fail-closed gate
  without a proportionate good-faith failure behind it.
- This posture does not waive explicit requirements for external network
  exposure, secret handling, destructive/irreversible operations, provider
  billing, or decisions the accepted design reserves to the operator. Apply
  those boundaries as written without generalizing them into distrust of every
  local worker.

## Choosing mechanism or policy

The section above decides whether a guard should exist. This one decides whether
it is code or guidance a capable supervisor follows. The failure this system
ships is over-restriction: refusals scoped wider than the transition they
protect, preconditions checked once and never released, and conclusions recorded
that were never established. A guard that makes the flow brittle has failed at
its own job, and a sound intent behind it changes nothing.

Two independent questions decide the form. **Can a good-faith mistake here be
detected and undone?** Spending the operator's money, destroying data, leaking a
secret, supplying the content of an operator-reserved decision, and leaving a
store wrong in a way nothing downstream detects are irreversible or invisible;
those get mechanism, scoped to the irreversible step itself, with the
surrounding flow left to policy. **Are the edge cases enumerated?** Recoverable
failure with uncertain edges is policy — the quadrant this system keeps getting
wrong. Recoverable failure with well-understood edges is mechanism only where
the mechanism stabilises something. Mechanism enforces its author's assumptions
on situations the author never saw and policy only advises them, so everything
outside the irreversible set defaults to policy.

- **Clean implementation is a precondition for choosing mechanism.** A guard
  that resists clean implementation is evidence the edge cases are not
  understood: demote it to policy rather than pushing the implementation through.
- **Soft first, tighten on evidence.** Add a guard for a failure that has been
  observed, not one that is imaginable, and narrow it once an actual failure is
  seen.
- **A capable supervisor blocked from managing its own session is a defect in
  the framework** until someone names the concrete corrupt transition on the
  other side. That burden falls on whoever wants the guard, in review as much as
  in repair.
- **Loosening a guard is a first-class outcome.** Narrowing a refusal, rescoping
  it, inverting its failure direction, and deleting it are repairs, not scope
  creep; the bar for deleting a branch is naming the transition it prevented and
  showing that transition cannot occur here. Say so explicitly rather than
  implementing something you believe makes the system more brittle.

## What a guard may block

Guards prevent the transitions the threat model names and the irreversible set
below. Everywhere else, prefer adapt, reconcile, or degrade over refuse: a
situation the flow can absorb is absorbed, with a legible trace. Guards that are
individually correct and individually fail closed still compose into states no
actor can leave; such a state is a correctness defect of the same severity as a
corrupt write, and "every check behaved as specified" describes it rather than
defends it.

- **Scope a refusal to its real blast radius.** Name the records and actors the
  refusal covers, and confirm the gated work actually reads the value the check
  failed to produce. One unreadable input refusing every command on the
  installation, for agents party to nothing it protects, is a defect, not caution.
- **Absent and unreadable are different answers.** Where absence proves vacuity
  — no table means no rows means the writer cannot have run — admit. Where the
  surface exists and cannot be read, a live row may exist and admitting would
  invalidate something a recipient is about to act on, so hold, and hold only
  over work that depends on the unread value.
- **A refusal states what was observed.** "I could not read this record" and
  "this record shows something live" never share a typed reason, however alike
  their handling; a guard that types an unreadable surface as a live hold
  asserts a condition it never observed and sends every downstream agent chasing
  a recovery that cannot exist. Validate a refusal's remedies against the record
  it just read: a remedy that cannot apply reports as unproven rather than as a
  denial. A refusal meaning "this disposition is not implemented in this build"
  is a fact about the code rather than the campaign, and it is an override case
  rather than a hold.
- **A precondition checked once is an assumption for the rest of its window.**
  Either re-check it at use, or make the downstream failure self-clearing:
  settle the transaction as failed, release every hold it took, and leave a
  legible trace. Do not instead add an upstream refusal, which makes an
  unrelated command fail for a reason its caller did not cause and cannot clear.
- **Deliver a refusal to an actor who can act on it.** A guard that fails a
  third party to protect a transaction that party is not in is misplaced,
  however true its condition.
- **Ship the exits with the guard.** For each record it writes, enumerate the
  states reachable when composed with the other checks touching that record in
  the same flow, and complete this sentence for each: *actor A can take action
  B, after which this exact command succeeds.* Both blanks must fill. Parking,
  escalating, waiting for the operator, a manual rollback, and hand-editing a
  store are not exits. A state with no exit blocks the change.
- **A guard that writes records only what it established.** A measurement that
  failed partway is not a verdict: record the failure and which leg failed.
  Inconclusive is a durable value distinct from negative, a failed measurement
  never overwrites an established result, and a disposition is recorded because
  it is true, not because recording it clears a check.
- **An allowlist of known-good versions is not a guard, it is an expiry date.**
  It excludes every future build by default, so the system degrades by standing
  still and a vendor release silently removes a capability nobody chose to give
  up. Where a check must exist, name the build that is known broken, not the ones
  known good. Per-version knowledge is an override recording what was proven on a
  build, never a precondition: a missing record selects a conservative default and
  proceeds. **Conservative means the safe end of the range, not the null value** —
  a zero timing or an empty set chosen because nothing was proven converts an
  absent measurement into a silent failure. Where no default can be made safe,
  block that one operation with a typed reason and leave the rest of the provider
  working. Full policy: `docs/provider-version-policy.md`.
- **Treat attempted as a state.** Record intent before the external effect,
  prove effects from the receiver's own record, and give every wait a bound and
  the transition taken when it expires. A bound that expires into another wait
  is not a bound.
- **Write records through their writers.** A hand-edited ledger or campaign
  record is the silent-corruption case. Repairing a store's schema or a broken
  migration is not a hand-edit and is the expected fix.

## When a guard blocks correct work

A guard that blocks progress in a situation its author did not anticipate is a
defect in the guard. Proceed, and record the override; the unrecorded workaround
is the thing forbidden here, not the override. Waiting for a merge or for the
operator's hands is the wrong default, because it turns a tooling gap into a
campaign outage, and no ticket, approval, or reviewer is required at override
time. Open the repair through the ordinary review and deploy track in parallel:
that track is the exit a guard's design review must show, never the one blocked
work waits on.

Run one test first, on the action you are about to take rather than on the
guard's importance: can a good-faith mistake here be detected and undone? These
cannot, so they are exempt and carry no override:

- **Provider billing** — starting a metered or billed run, and the boundary
  between a free fixture run and a paid provider run.
- **Destructive or irreversible operations**, which keep their confirmations,
  including force and break variants that perform the destructive effect.
- **Secret handling and redaction.**
- **Decisions the accepted design reserves to the operator.** Do not supply the
  content of one; surface it promptly. Recording a decision the operator made
  out of band, with its source and channel, is not making that decision.
- **Transitions that leave a store wrong in a way nothing downstream detects.**

Membership follows from irreversibility, not from importance or a subsystem's
name. Inside the set the defaults invert: fail closed on every error path,
including typos, unknown identifiers, probe errors, and lookup failures, so a
guard separating a free run from a billed one refuses when it cannot identify
which it is. An error path that reaches the irreversible effect is a defect of
the same severity as a corrupt write, and the repair is to tighten rather than
soften. Failing closed here withholds the specific effect, not the enclosing
operation. Outside the set, the default under uncertainty is to proceed with a
record.

The record carries six things: the typed reason or check identifier the refusal
raised, verbatim; the record and field the guard read and what it returned,
including "could not read"; the action taken instead, as run; one sentence
naming the situation the guard's author did not anticipate; an explicit
statement that the action is none of the exempt kinds above; and the campaign,
lane, round, and timestamp. Write it into the campaign record and the round
report before or atomically with the action, so a crash mid-override still
leaves the trace. Where that store cannot be written, the round transcript
carries the record and the override proceeds: the recorder never becomes the new
block.

Repeated overrides of one guard are a defect report against that guard and the
specification for narrowing it; file the narrowing with those records as its
evidence, and treat an override that proves to have been wrong as evidence for
tightening that guard specifically. A wedged lane is not a wedged campaign: keep
every lane running that does not draw on the resource the refusal named, keep
the report path independent of whatever is stuck, and never end correct and
silent.

## Test and claim verification

A test that passes proves only that it passes. Before relying on one, establish
that it would fail if the behaviour it names regressed.

- **Mutation-test a fix, not just the code.** Revert the change and confirm at
  least one test fails. A repair whose reversion leaves the suite green is
  unpinned, whatever its diff says.
- **A fixture must model the thing, not the expectation.** A fixture that
  encodes what the caller wants to see certifies a property nothing establishes,
  and it will block a correct fix rather than catch a wrong one.
- **Ask what states the setup can construct at all.** Mutation testing asks
  whether a test would fail if the code were wrong; this asks whether the test
  ever reaches the code. A suite whose setup never enters the region stays green
  for reasons unrelated to its assertions, so the two checks catch different
  faults and neither substitutes for the other.
- **A test that proves an ingredient does not prove the wiring.** Asserting that
  a helper derives the right value says nothing about whether the call site
  consumes it.
- **Prose is a claim and decays like one.** A docstring or comment asserting an
  invariant the code does not enforce is a defect in the same class as a wrong
  fixture. Correct it when the mechanism changes, or delete it.
- **A gate that only fires once is not a gate.** Ask whether any test exercises
  the second occurrence, and what the suite would show if nothing re-checked
  after the first.

Distinguish evidence from conditions. A differential is only as good as the
equivalence of its runs, and a measurement used as evidence must be taken under
conditions the report can state.

## Implementation and review

- Keep the normal path flexible and recovery-friendly. Add idempotency,
  version/CAS checks, exact resource identity, or durable receipts when they
  prevent a demonstrated retry/race/corruption mechanism—not as rituals.
- Independent review remains adversarial about correctness, concurrency, and
  accidental state corruption, but not about malicious intent outside this
  threat model. Reproduce findings with tests or concrete examples whenever
  possible and prefer the smallest coherent repair.
