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

## Flexibility is the goal; guardrails serve it

The purpose of this system is supervisor-managed flows that keep working across
the situations they actually meet. Guardrails prevent serious problems and
corrupted state. They are not the product, and a guardrail that makes the flow
brittle has failed at its own job.

- **Scope a refusal to its real blast radius.** A check that cannot answer its
  question must not gate work whose correctness never depended on that answer.
  Turning one unanswerable question into a total loss of supervision is a
  defect, not caution.
- **Unknown is not the same as unsafe.** Prefer the reading the evidence
  actually supports. A subsystem that is absent, uninitialised, or never
  deployed is usually telling you there is nothing to guard, not that everything
  might be in flight.
- **A capable supervisor blocked from managing its own session is a defect.**
  When an agent can see the right action and the framework will not let it act,
  treat that as the framework being wrong until a concrete corrupt transition on
  the other side is named.
- **Prefer adapt, reconcile, or degrade over refuse.** Refusal is correct when
  proceeding would corrupt state. When proceeding would merely be untidy, absorb
  the situation and carry on, leaving a legible trace.
- **A guardrail needs a recovery path a supervisor can actually take.** A gate
  that can only be cleared by an operator editing a store by hand is a wedge
  wearing a guardrail's clothes.
- **Push back on an over-defensive spec.** Loosening an illogical restriction,
  or deprecating a rigid mechanism in favour of a more flexible one, is a valid
  and expected outcome of implementation and review, not scope creep. Say so
  explicitly rather than implementing something you believe makes the system
  more brittle.

Examine every issue, implementation pass, and review pass through this lens
alongside the threat model above. The two are one standard: prevent the
transitions that genuinely corrupt state, and refuse to buy that prevention with
brittleness everywhere else.

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
- **A gate that only fires once is not a gate.** Ask whether a guard fires on
  every occurrence or only the first, and whether anything re-checks afterwards.

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
