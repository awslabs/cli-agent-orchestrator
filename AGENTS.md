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

## Implementation and review

- Keep the normal path flexible and recovery-friendly. Add idempotency,
  version/CAS checks, exact resource identity, or durable receipts when they
  prevent a demonstrated retry/race/corruption mechanism—not as rituals.
- Independent review remains adversarial about correctness, concurrency, and
  accidental state corruption, but not about malicious intent outside this
  threat model. Reproduce findings with tests or concrete examples whenever
  possible and prefer the smallest coherent repair.
