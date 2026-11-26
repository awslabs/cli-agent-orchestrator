# Pull request review

This file holds the review lessons of this repository. Human reviewers and the
agents of the [PR Review workflow](.github/workflows/pr-review.yml) apply it.
The [design](docs/pr-review-design.md) explains how the workflow uses it, and
the [operator guide](.github/pr-review/README.md) explains how to run a review.
An AI review only helps people. It never approves, blocks or decides a merge.

The lessons come from the reviews that #1350 collected. From 2026-10-18 to
2026-11-23, haofeif posted 468 reviews on 287 PRs (#600 to #1459). 121 of them
requested changes, and 41 of those 121 say that all checks had passed at the
reviewed head. Each count is a lower bound. Replies from fanhongy,
gutosantos82, plauzy and tedswinyar added data, such as 1,066 AI review
reports, the CI measuring window and the bypass log. The main sources are
haofeif's [CI review][ci], [review panel][panel], [lessons][lessons],
[corrections][fixes] and [more lessons][more]. A number such as #1410 is a PR
or an issue in this repository.

[ci]: https://github.com/awslabs/cli-agent-orchestrator/issues/1350#issuecomment-6911723305
[panel]: https://github.com/awslabs/cli-agent-orchestrator/issues/1350#issuecomment-7054921441
[lessons]: https://github.com/awslabs/cli-agent-orchestrator/issues/1350#issuecomment-7099187186
[fixes]: https://github.com/awslabs/cli-agent-orchestrator/issues/1350#issuecomment-7100799631
[more]: https://github.com/awslabs/cli-agent-orchestrator/issues/1350#issuecomment-7117349697

## How to use this file

- Each line of the form `### A1. Title` is one check. Use this form only for a
  check, because the workflow reads the checks from these lines.
- The workflow reads this file from the dispatched ref, normally `main`. A copy
  in the PR is evidence only, also when the PR changes it.
- The agents only read and search. Where a rule says "run", an agent traces the
  code instead, and says that it did not run it.
- [Before you merge](#before-you-merge) is for maintainers only.

## Evidence rules

- Green CI does not prove that a PR meets its contract. Trace the changed data
  from its producers to its consumers, through the success and failure paths
  (#956, #1006, #1410, #1445, #1449). The contract is the linked issue, the
  docs and the existing interfaces. Lint owns code style.
- Evidence is for one head and one base. The Qwen run for #1410 was one commit
  after the merged head, so it did not prove that head. CI on a head that is
  behind `main` tested an older merge (#1449), and a retarget starts no CI
  run. The merge with `main` can change a result (#1461 after #1452). Do not
  say that a defect merged unless you checked the merged head (#1345).
- A note about a risk is not a check. Run or trace the trigger of each risk
  that you name, or list the risk as not checked and do not approve on it. On
  #1149, a note named a risk but did not test its trigger, a
  `[status_line] command` setting that can leave a finished Kimi Code turn
  `UNKNOWN`.
- Say what you did not check. Defects hide there: on #1449, "Not checked: a
  real LLM compile (stubs only)" held a P2.
- Check a claim about a tool or a control at its source: the vendor docs, a
  probe, the ruleset or the run log. Do not copy it from another document.
  Three corrections in #1350 came from claims that nobody checked: Trivy loads
  `.trivyignore`, not the file that `SECURITY.md` named; mypy reads
  `mypy.ini`, not `pyproject.toml`; and admins and maintainers can bypass the
  tag ruleset. A control binds only the people who cannot bypass it.
- A suggested fix is code that nobody tested. Check it against the same
  inputs, or mark it as not tested. On #1449, the suggested
  `sanitize_key(text) == text` raised `ValueError`.
- Evidence decides. Agreement does not prove a finding, and doubt without
  evidence does not refute one.

## Severity

The tiers come from #464.

| Label | Tier | Meaning |
| --- | --- | --- |
| P1 | Must Fix | Breaks security, authorization, data integrity or the approved architecture; loses or corrupts data; or presents wrong results as correct. |
| P2 | Should Fix | A defect on a supported path; a missing failure, empty or partial state; or a missing coupled test, document, prompt or schema. |
| P3 | Good To Fix | A real problem that does not need a fix before the merge, such as a weak low-risk test or a writing note. |

- An old defect counts only if this PR introduced it, newly exposed it, or
  must fix it. Set `pre_existing` for it.
- A red or missing CI result is not a finding by itself. Find the defect in the
  code (#1320).

## Risk level

Give the risk of the whole PR, with one to five concrete reasons. Start from
the changed paths, as the step 7b risk label in #1350 does:

- high: a path that `.github/CODEOWNERS` owns.
- medium: provider status detection, tool permissions, the MCP or API surface,
  a dependency manifest or lockfile, or another file in `scripts/`; or about
  800 changed lines or 40 changed files, not counting lockfiles.
- low: all other PRs.

Raise the level for a reason that the paths do not show, and say why. Never
lower it. One review can miss a defect that a second review finds (#1149,
#1462), so a maintainer can ask for a second, independent review of a
high-risk PR.

## Checks

### A1. Correctness

The change does what its issue and its docs say, for each allowed input.

- Is each new rule or branch generic, or is it for one case only? On #1410,
  the shared acceptance helper added 3 sentences for `qwen_cli` only.
- A failure must not look like a success. Code, not a model, sets the state of
  a failed, partial or skipped step. On #773 and #1200, a review fan-out
  failed, and the model still wrote "Approve with nits".
- A broad catch must not turn a stop into a partial result. A sketch in #1350
  caught every `ShimError`, so a `409` halt became a partial review.

### A2. Regressions

Behavior that the PR did not mean to change still works.

- PR CI does not run the release path. For a change to release or packaging
  code, check what a release publishes, such as the wheels and the tag trees
  (#1454).
- A release step is atomic. If it fails, `CHANGELOG.md` and the fragments stay
  as they were (#1388).

### A3. Edge cases

Empty, missing, partial, large, concurrent, cancelled and timed-out states.

- A fix for one bad input needs a test for each kind of bad input in the same
  class. After #1461, a `~name` path with a NUL, a ref with a lone surrogate
  and a missing path with a byte that is not valid UTF-8 still gave HTTP 500.

### A4. Security

Trust boundaries, secrets, permissions, injection and the supply chain.

- A security finding names the trust boundary, the attacker capability and the
  exploit path (#464).
- Tool limits depend on the provider. A soft provider in the
  [enforcement table](docs/tool-restrictions.md#provider-enforcement) limits
  tools only by prompt. `--yolo` and `allowedTools: ["*"]` remove all limits.
  Without the `@cao-mcp-server` grant, four CAO callback tools stay reachable.
  Each CLI also reads its own user config, such as MCP servers and hooks.
- A `pull_request` run uses the workflow file of the PR. Keep a secret in an
  environment that only `main` can use. A job that holds a secret never runs
  PR code, also under `pull_request_target` or `workflow_run` (#464), and a
  separate job without the secret writes to the PR. Treat a PR artifact, and a
  comment that any user can write, as untrusted data. Install a tool that
  reviews or gates a PR from the base or a pinned release, never from the PR.
- A gate fails closed. A summary job such as `ci-gate` runs with
  `if: ${{ always() }}`, fails on `failure` or `cancelled`, and accepts a skip
  only from an allowlist, because GitHub counts a skipped check as a pass. A
  required workflow has no path filter, because GitHub leaves a skipped one
  Pending. A scan fails when it cannot resolve its range, because gitleaks
  exits 0 then.
- A PR can weaken a gate through a config file (#1462).
  `test/test_codeowners_risk.py` pins the gate settings and the owned config
  files. Report a changed gate setting, or a new config that a gate tool
  reads, such as one for black, isort or pytest, unless the linked issue
  approves it.
- An ignore entry for zizmor, gitleaks, Trivy or cargo-deny needs a reason and
  a scope. A vulnerability exemption also needs a reachability statement and
  an expiry of at most 14 days (#1400). A code owner reviews it.
- A test builds a fake credential at run time. gitleaks fails on a literal one
  anywhere in the PR history.

### A5. Compatibility

Providers, Python versions, operating systems, configs, saved data and APIs.

- Before a new CLI provider merges, run
  [`examples/assign`](examples/assign/README.md) once, end to end, at the exact
  PR head, with the new provider for all five agents and the same profiles and
  task as the other providers (step 9 of the
  [provider guide](skills/cao-provider/SKILL.md)). The run shows that:
  1. `analysis_supervisor` gives three tasks to three `data_analyst` agents
     with `assign`, in parallel, without waiting between them;
  2. it then calls `report_generator` with `handoff` and waits for the
     template;
  3. each analyst returns its result with `send_message`, and the inbox shows
     a delivered message from each of the three;
  4. the final report uses the template and all three results;
  5. the supervisor does no analysis itself, as its tool calls show.

  The PR gives the commit, the CLI version, the model and the command. A worker
  test, a simulated reply or a prompt written for one provider does not prove
  the flow (#376, #1410). Without this run, A5 is `not_checked`.
- PR CI does not test each Python version that `pyproject.toml` supports.
  Check version-specific behavior: on #1213, `Path.exists()` hid a permission
  error on 3.14 only.

### A6. Test quality

The tests prove the requirement, not only the code as written.

- A new test fails on the base, or it says that it pins existing behavior. On
  #1461, a new test passed on the base, and all 95 tests in the changed test
  files still passed with `shutil.Error` removed from the code.
- A test runs the real function that the PR changes. A stub can hide the
  changed check (#1449 `wiki_compiler.compile`, #1353 `generate_changelog`).
- Mutate the changed lines. On 37 of 96 PRs, a mutant that was not equivalent
  survived the tests. Check each mutant too: on #1449, a wrong mutant made its
  "kill" prove nothing.
- A test does not depend on wall-clock time, test order or the real
  environment. Count the work, not the seconds (#1448), and use a barrier for
  a handoff between threads (#1357). Restore shared state (#1381). Keep away
  from the real `HOME`, `~/.aws` and tmux (#1231, #1245). Fix the cause of a
  flaky test, because a retry hides it (#1381).
- The Codecov comment is information, not a gate. On 11 PRs, it had no base
  upload (#1343, #1400, #1419, #1430). The coverage ratchet is the gate.

### A7. Performance

Hot paths, loops with no limit, blocking calls and extra API calls.

### A8. Operations

Logs, errors, timeouts, retries, cleanup and what an operator sees on failure.

- A nightly or scheduled job has an owner and a step that opens or updates an
  issue when it fails. GitHub tells only the person who last edited the cron
  line (#1395, #1440).

### A9. Maintainability

Docs, changelog fragments, the names of new concepts, and duplicate code.

- Check each changelog and docs claim against the code. On 24 PRs, the text
  did not agree with the code (#897, #1185, #1347, #1424). A notable change
  adds a fragment in `changelog.d/`, as `CONTRIBUTING.md` says.
- Write docs, READMEs, and the pages and blog posts in `docusaurus/` in plain
  English that a person understands on the first read: short sentences,
  common words, one term for one thing, and each term explained at its first
  use. Remove the AI style: filler, hype, jargon, strings of dashes and
  repeated bold bullet headings. Report a writing problem as a P3 note.
- A blog post is for a reader who is new to CAO. First say what the tool is,
  who uses it and what it does, and name who did the work. Write "If the
  watcher stops before it sends a notification, it reads the saved
  notification and sends it after it restarts", not "Wakes replay from a
  journal" (#1300).

## Results

- A review covers one head commit and the full PR, not only the new commits. A
  new push needs a new review.
- A failed or partial review says so, and a clean result covers only the
  checks that ran. Count the reviews that ran, not the posts: since
  2026-11-01, 19 of 33 Copilot posts said only that the review did not run,
  and all 33 had the state `COMMENTED`. In one AI review pipeline, 484 of
  1,066 reports missed at least one review angle.

## Before you merge

- An approval covers one commit. A push removes it, also "Update branch".
  Approve again at the head that merges (#1334, #1340, #1354, #1442). A review
  posted as a comment does not count.
- Do not approve from an AI report alone. Read the change, and say what you
  checked.
- A change request does not stop an admin merge: 4 PRs merged with one still
  open. A bypass is break-glass, with the reason posted before the merge. From
  2026-11-17 to 2026-11-23, 35 of 67 merges were bypasses. A status bypass
  skips CI on the latest `main`, and a review bypass skips a person.

## Add a lesson

When a review misses a defect, add the lesson to the check that should have
found it, with the PR or issue number. Keep each rule short and generic, and
merge it with a rule that already says the same thing. Put lessons here, not in
a prompt or an agent profile. `.github/CODEOWNERS` gives this file to
`@awslabs/multiq`, so a change needs a code owner's review.
