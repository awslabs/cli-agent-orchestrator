# Pull request review

This is the review policy of this repository. People and the
[PR Review workflow](.github/workflows/pr-review.yml) apply it; the
[operator guide](.github/pr-review/README.md) owns the procedure. Keep the
lessons here, not in prompts or agent profiles.

An AI review is never an approval. People remain accountable for requirements,
architecture, risk and merge decisions.

## General engineering rules

All reviewers, people and agents, apply these rules.

- **Fix the actual problem.** Trace the failing boundary and full data path.
  Make only necessary changes; use existing capabilities before new architecture.
- **Preserve approved architecture.** Check permission before necessity.
  Platform limits or passing tests do not authorize a different boundary.
- **Prevent overfitting.** No prompt-, entity-, source-, file- or fixture-specific
  exceptions. Cover meaningfully different cases.
- **Separate reasoning from validation.** Models handle semantic judgement.
  Code handles authorization, schemas, exact membership, bounds, state,
  idempotency, ownership and approved execution.
- **Justify limits.** Defaults, caps, timeouts and fallbacks need a contract.
  Never silently truncate or mistake a page, top-k result or sample for completeness.
- **Keep responsibilities in their layer.** Avoid duplicate guidance,
  unnecessary frameworks and executable wrappers for passive references.
- **Preserve meaning.** Resolve canonical identities before joins. Keep
  provenance, units and time zones.
- **Represent outcomes honestly.** Distinguish missing, empty, partial,
  unsupported, unauthorized, cancelled and failed. No silent source changes,
  swallowed errors or success-shaped placeholders.
- **Preserve security.** Maintain least privilege, approved networks,
  authentication and actor/session ownership. Never expose secrets or governed
  payloads.
- **Check failure boundaries.** Trace retries, idempotency, races, atomicity,
  continuation, cancellation and cleanup. Validate ownership before side effects.
  Leave no uncontrolled children, resources or partial state.
- **Keep surfaces consistent.** UI, API, events, artifacts and saved reports
  must reflect the same authoritative outcome.
- **Update coupled contracts.** Keep producers, consumers, tests, docs, prompts,
  schemas and manifests consistent; preserve supported and mixed-version behavior.
- **Test the requirement.** Use independent oracles, the reported case,
  meaningfully different cases and relevant failure boundaries.
- **Make accurate evidence claims.** Verify uncertain platform behavior from
  authoritative sources. Attribute evidence to its revision and environment;
  distinguish executed, skipped, failed, blocked and unavailable work.
  Missing evidence is not a code defect; green CI is not architectural proof.

## Repository-specific rules

Apply these rules with the general rules.

- **Review against the contract, not the CI result.** The contract is the
  linked issue, the docs and the existing interfaces. Green CI does not prove
  it: trace the changed data through the success and failure paths. Do not
  invent requirements; lint owns code style.
- **Review one head and the whole PR.** Evidence from another commit does not
  prove this head. CI on a head behind `main` tested an older merge, and a
  retarget starts no CI run. Each review covers the full PR, not only the new
  commits. If you did not check an earlier finding again at this head, say
  so; do not call it open.
- **Trace, and say what you did not check.** The review agents only read: they
  trace the code and do not run it. Record each check as checked, not checked
  or not applicable. A risk that you name is a candidate finding, not a note:
  trace its trigger, or mark the check not checked.
- **Prove each finding.** A finding needs a failing input or a code path at the
  head. Agreement does not prove it, and doubt without evidence does not
  refute it. A suggested fix is untested code: check it against the same
  inputs, or say that it is not tested.
- **Check a claim at its source.** Take a claim about a tool or a control from
  the vendor docs, the config that the tool reads, the ruleset or the run log,
  not from another document. A control binds only the people who cannot
  bypass it.

### A1. Correctness

The change does what its contract says, for each allowed input.

- **One rule for all providers.** Check each new rule, branch, constant and
  prompt line on its own. Shared code, prompts and tests have no text for one
  provider only.
- **No pass without work.** A check that did no work fails, such as a scan
  with no commit range, an empty input or a skipped job. Code, not a model,
  sets a failed, partial or skipped state, and a failed fan-out never passes.
- **Catch narrowly.** A broad catch must not turn a stop, such as a `409`
  halt, into a partial result.

### A2. Regressions

Behavior that the PR did not mean to change still works.

- **Release path.** PR CI does not run the release path. For a release or
  packaging change, check what a release publishes, such as the wheels and the
  tag trees, and the state that earlier releases left. A release step is
  atomic: if it fails, `CHANGELOG.md` and the fragments stay as they were.

### A3. Edge cases

Empty, missing, partial, large, concurrent, cancelled and timed-out states.

- **Each kind of bad input.** A fix for one bad input lists each kind of input
  in its class and tests each one, such as a NUL byte, a lone surrogate and
  bytes that are not valid UTF-8.

### A4. Security

Trust boundaries, secrets, permissions, injection and the supply chain.

- **Exploit path.** A security finding names the trust boundary, the attacker
  capability and the exploit path.
- **Provider tool limits.** Check the
  [provider enforcement](docs/tool-restrictions.md#provider-enforcement)
  table: a soft provider limits tools only by prompt. `--yolo` and
  `allowedTools: ["*"]` remove every limit. Without the `@cao-mcp-server`
  grant, four CAO callback tools stay reachable. Each CLI also reads its own
  user config, such as MCP servers and hooks.
- **Workflow secrets.** A `pull_request` run uses the workflow file of the PR.
  Keep a secret in an environment that only `main` can use. A job that holds a
  secret never runs PR code, and a separate job without the secret writes to
  the PR. PR artifacts, `workflow_run` data and comments are untrusted.
  Install a tool that reviews or gates a PR from the base or a pinned release.
- **Gates fail closed.** A summary gate runs with `if: always()`, fails on
  `failure` or `cancelled`, and accepts a skip only from an allowlist, because
  GitHub counts a skipped check as a pass. A required workflow has no path
  filter and also runs on `merge_group`.
- **Gate config.** A config file in any folder can weaken a gate, for Ruff,
  mypy, black, isort, pytest, Trivy or gitleaks. `test/test_codeowners_risk.py`
  pins the gate settings and their owners. Report a new or changed gate
  setting, or a gate or trust-boundary file with no code owner, unless the
  linked issue approves it.
- **Ignore entries.** A scanner ignore entry has a reason and a scope. A
  vulnerability exemption also has a reachability statement and an expiry of
  at most 14 days, and a code owner reviews it.
- **Test credentials.** A test builds a fake credential at run time. gitleaks
  fails on a literal one anywhere in the PR history.

### A5. Compatibility

Providers, Python versions, operating systems, configs, saved data and APIs.

- **New provider.** The PR records one end-to-end run of
  [`examples/assign`](examples/assign/README.md) at its head, with the new
  provider for all five agents (step 9 of the
  [provider guide](skills/cao-provider/SKILL.md)). The record gives the
  commit, the CLI version, the model and the command, and shows that:
  1. the supervisor assigns three tasks to three analysts in parallel;
  2. it then waits on a `handoff` to `report_generator`;
  3. the inbox shows a delivered `send_message` reply from each analyst;
  4. the final report uses the template and all three results;
  5. the tool calls of the supervisor show that it did no analysis itself.

  Worker tests, a simulated reply or a prompt for one provider do not prove
  the flow. Without the record, A5 is not checked.
- **Python versions.** PR CI does not run every Python version that
  `pyproject.toml` supports. Check version-specific behavior.

### A6. Test quality

The tests prove the requirement, not only the code as written.

- **Fails on the base.** A new test fails on the base, or says that it pins
  existing behavior.
- **Real code.** A test runs the real function that the PR changes. A stub of
  that function proves nothing.
- **Changed lines.** For each changed line, name a test that fails if the line
  is wrong. Check each mutant too: a kill proves nothing if the mutant breaks
  something else.
- **Deterministic.** Count the work, not the seconds, and assert the property,
  not a count of event-loop ticks. Use a barrier for a handoff between
  threads. Restore shared state, and keep away from the real `HOME`, `~/.aws`,
  tmux and `CAO_*` settings. Fix a flaky test at its cause, not with a retry.
- **Coverage.** The Codecov comment is information; the coverage ratchet is
  the gate.

### A7. Performance

Hot paths, loops with no limit, blocking calls and extra API calls.

- **CI cost.** A PR workflow cancels its superseded runs, but not on `main`.
  One push runs each test suite once.

### A8. Operations

Logs, errors, timeouts, retries, cleanup and what an operator sees on failure.

- **Scheduled jobs.** A scheduled job has an owner and a step that opens or
  updates an issue when it fails. GitHub tells only the person who last edited
  the cron line.
- **Timeouts.** Each job and step has a timeout that fits the largest input. A
  run that cannot start or finish ends as failed or neutral; it never hangs or
  passes.

### A9. Maintainability

Docs, changelog text, the names of new concepts, and duplicate code.

- **Claims match the code.** Check each claim in the docs, the changelog, code
  comments and the PR text against the head code.
- **Plain English.** Docs, READMEs, and the pages and blog posts in
  `docusaurus/` use plain English that a person understands on the first read:
  short sentences, common words, one term for one thing, and each term
  explained at its first use. No AI style, such as filler, hype, jargon,
  strings of dashes or repeated bold headings. A blog post first says what CAO
  is, who uses it and what the change does, and names who did the work.
  Report serious AI style as P2, for example AI style that runs through a
  section or hides what the text means. Report another writing problem as P3.

## Severity

| Label | Tier | Meaning | Blocks merge |
| --- | --- | --- | --- |
| P1 | Must Fix | Breaks security, authorization, data integrity or the approved architecture; loses or corrupts data; or presents wrong results as correct. | Yes |
| P2 | Should Fix | A defect on a supported path; a missing failure, empty or partial state; or a missing coupled test, document, prompt or schema. | Yes |
| P3 | Good To Fix | A real problem that does not need a fix before the merge, such as a weak low-risk test or a minor writing problem. | No |

Severity follows the control and the impact, not the audience size or known
exploits. An old defect counts only if this PR introduces it, newly exposes it
or must fix it; set `pre_existing` for it. A red or missing CI result is not a
finding by itself: find the defect in the code. A fix is not a severity
downgrade.

## Risk level

Give the risk of the whole PR, with one to five concrete reasons. Start from
the changed paths:

- high: a path that `.github/CODEOWNERS` owns.
- medium: provider status detection, tool permissions, the MCP or API surface,
  a dependency manifest or lockfile, or another file in `scripts/`; or about
  800 changed lines or 40 changed files, not counting lockfiles.
- low: all other PRs.

Raise the level for a reason that the paths do not show, and say why. Never
lower it. A maintainer can ask for a second, independent review of a
high-risk PR.

## Approval and merge

- **The workflow never approves a PR.** After its comment, it approves the
  waiting CI runs of the PR's commits and assigns the maintainer who started
  it. If the review accepts a P1 or P2, it requests changes. Only a dismissal
  clears that request: by a later complete review of the PR head that accepts
  no P1 or P2, or by a maintainer.
- **A maintainer approves** at the head that merges. A push removes an
  approval, also "Update branch", and a review posted as a comment does not
  count. Do not approve from an AI report alone: read the change and say what
  you checked. An AI post that says that the review did not run, for example
  because of a quota or a timeout, is not a review.
- **A bypass is break-glass.** A change request does not stop an admin merge.
  Post the reason before a bypass merge. A status bypass skips CI on the
  latest `main`; a review bypass skips a person.

## Add a lesson

When a review misses a defect, add the lesson to the check that should have
found it. Keep each rule short and generic, merge it with a rule that says the
same thing, and give the source in the commit message. Only a check uses a
`### A1. Title` heading, because the workflow reads the checks from these
lines. The workflow reads this file from the dispatched ref; a copy in the PR
is evidence only. `.github/CODEOWNERS` gives this file to `@awslabs/multiq`.
