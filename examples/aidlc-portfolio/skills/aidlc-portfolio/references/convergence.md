# Cross-Project Convergence

## Child Result Contract

Every child runner submits one result for its portfolio intent and project
using `assets/templates/child-result.yaml`. The result names:

- changed components and business capabilities;
- compatibility of every changed contract, with evidence;
- the child's disposition and note for every relevant dependency;
- concrete build, test, or review verification.

The utility validates identifiers against the registered intent, project, and
dependency catalog. A missing result is a non-decidable integration blocker.

## Impact And Status

`convergence check` traverses the dependency graph in both directions and is
cycle-safe. It evaluates runtime, contract, data, build, deployment,
operational, business, and release relationships. The report groups findings
as `satisfied`, `blocked`, `deferred`, or `unknown` and identifies affected
upstream and downstream projects.

Dependencies whose integration or release order is not complete are
non-decidable blockers. Breaking contracts, unknown catalog references,
missing assumptions, and deferred or unknown assumptions remain unresolved
until corrected or explicitly decided.

## Human Decisions

Eligible risks may receive `accepted` or `resolved` decisions. `accepted`
retains the risk as deferred; `resolved` marks the reported risk satisfied.
Each decision records the human, note, timestamp, and SHA-256 risk revision.
Any change to the underlying result or risk invalidates the old decision.

Never accept missing results or incomplete dependency ordering. Never infer a
decision from silence or edit decision YAML directly. Re-run convergence after
every result, catalog change, session update, or decision.
