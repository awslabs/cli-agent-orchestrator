# Pull Request Review Workflow

This workflow reviews one pull request of this repository with a panel of CAO
agents and posts one `COMMENT` review. Each seat of the panel is a step of a CAO
workflow, with its own agent and terminal. No seat is a sub-agent of another
session. A maintainer starts the review by hand and selects the agent CLI:
`kiro_cli` (the default) or `copilot_cli`.

The review is advisory. It never approves a PR and never requests changes, so it
cannot block or allow a merge.

## How it works

The GitHub Actions workflow
[`.github/workflows/pr-review.yml`](../workflows/pr-review.yml) has
three jobs:

| Job | Token permissions | What it does |
| --- | --- | --- |
| `prepare` | `contents: read`, `pull-requests: read` | Reads the PR. A closed PR fails the run. A draft PR is skipped. |
| `review` | `contents: read`, `copilot-requests: write` | Checks out the PR head as files, writes the diff, starts `cao-server` and runs the CAO workflow [`pr_review.py`](pr_review.py). Then it shows the review in the run summary. |
| `publish` | `contents: read`, `pull-requests: write` | Validates `review.json`, reads the PR again and posts the review on the reviewed head. It runs no agent. |

The CAO workflow runs the panel in three phases. The seats of one phase run at the
same time.

1. **Review.** Two reviewers, `reviewer-a` and `reviewer-b`, read the evidence and
   the head checkout. Each one reports the files that it reviewed and at most 8
   candidate findings.
2. **Vote.** The script merges exact duplicates (the same path, line and title) and
   numbers the candidates `F1` to `Fn`. Two fresh voters, `vote-1` and `vote-2`,
   check each candidate against the source. A voter does not see who proposed a
   candidate or the other vote.
3. **Adjudicate.** An adjudicator reads the cited source and decides each
   candidate: `accept`, `reject` or `owner_decision`.

With no candidates, the voters and the adjudicator do not run.

Each seat is one `step(...)` call with its own `step_id` and
`recovery="idempotent"`. The agent returns its report with CAO's
`workflow_return` tool and a JSON Schema. The script reads the reports from the
CAO run view and validates them again. A missing or invalid report never becomes
a finding or a pass.

### Verdict rules

The adjudicator decides. Votes alone never accept a finding. The script changes a
decision only in these cases:

- Both voters confirmed a P1 or P2 finding, and the adjudicator rejected it or
  lowered it to P3. The finding becomes an owner question at the higher voter
  severity.
- A P3 candidate that is not settled is rejected, because a P3 finding does not
  block a merge.
- A `duplicate_of` claim that does not point to an accepted finding of the same or
  a higher severity is ignored. The finding then stands alone.
- If the adjudicator returns no valid report, each P1 or P2 candidate becomes an
  owner question, and each P3 candidate is rejected.

### Result states

`review.json` lists each result state that applies, in this order. The first state
is the result.

| State | When |
| --- | --- |
| `changes_required` | The panel accepted a P1 or P2 finding. |
| `owner_decision_required` | A P1 or P2 candidate needs a decision from the PR owner. |
| `incomplete` | A reviewer did not account for every changed file, or a voter or the adjudicator returned no valid report. |
| `no_blocking_findings` | None of the above. Accepted P3 findings can still be listed. |

If the run cannot make a review, `review.json` is Blocked: `publishable` is
`false`, and `blocked_reason` gives the reason. For example, no reviewer returned
a valid report. The `review` job then fails at its check step, and nothing is
posted.

The posted review starts with a line that names the engine and the person who
started the run, and says that the review is not an approval. Then it gives a link
to the run, the result, each candidate with its votes and decision, the coverage
of each reviewer, and the panel. An accepted finding on a line of the diff also
gets an inline comment.

After the credential check, the `review` job adds the review to the summary page
of the workflow run. The summary starts with a line that names the PR, the
reviewed head and the engine. Then it gives the same sections as the posted
review. The run summary shows the review also when `publish` posts nothing, for
example because the PR became a draft. Each person who is signed in to GitHub
and can read the repository can see the run summary. The `publish` job adds its
result to the run summary: the ID of the posted review, or the reason why it
posted nothing.

## Set up

### 1. Add the secret of the engine

**`kiro_cli` (default).**

1. Create an API key at [app.kiro.dev](https://app.kiro.dev). Kiro API keys need a
   Kiro Pro, Pro+, Pro Max or Power subscription. If an administrator manages the
   subscription, the administrator must first enable API keys. See
   [API key authentication](https://kiro.dev/docs/getting-started/authentication/).
2. Add the key as the repository secret `KIRO_API_KEY`:

   ```bash
   gh secret set KIRO_API_KEY --repo OWNER/REPO
   ```

Kiro documents API keys for its non-interactive mode. CAO runs Kiro in its
terminal UI, which also reads `KIRO_API_KEY` at startup. The governance rules of a
Kiro administrator also apply to API key sessions, for example rules for MCP
servers and models. Each seat needs the `cao-mcp-server` MCP server. The account
must offer the models of the panel. See [Models](#models).

**`copilot_cli`.** Select one:

- **The job token.** This needs no secret. The `review` job grants
  `copilot-requests: write`, so the job token pays for the Copilot requests. The
  organization policy **Allow use of Copilot CLI billed to the organization** must
  be on. See
  [Using Copilot CLI in GitHub Actions with `GITHUB_TOKEN`](https://docs.github.com/en/copilot/how-tos/copilot-cli/use-copilot-cli-in-actions).
- **A personal access token.** Create a fine-grained token with the "Copilot
  Requests" permission, and add it as the secret `COPILOT_GITHUB_TOKEN`. If this
  secret is set, the workflow uses it instead of the job token. The requests are
  then billed to the Copilot seat of the token owner.

Each engine gets only its own credential. A `kiro_cli` run never gets the job
token, and a `copilot_cli` run never gets `KIRO_API_KEY`.

### 2. Limit the secret to the default branch (optional)

A user with write access can dispatch this workflow from any branch, and so can
change the workflow file of that run. To stop this, store the secret as an
environment secret, not a repository secret, in a GitHub environment with a
deployment branch policy for the default branch only. Then add
`environment: <name>` to the `review` job.

### 3. Put the workflow on the default branch

GitHub runs a `workflow_dispatch` workflow only when the workflow file is on the
default branch.

## Run a review

In the **Actions** tab, select **PR Review**, then **Run workflow**. Or use the
GitHub CLI:

```bash
gh workflow run pr-review.yml --repo OWNER/REPO -f pr=123
gh workflow run pr-review.yml --repo OWNER/REPO -f pr=123 -f engine=copilot_cli
```

| Input | Default | Meaning |
| --- | --- | --- |
| `pr` | (required) | Number of the PR to review. |
| `engine` | `kiro_cli` | `kiro_cli` or `copilot_cli`. |
| `model_a` | `KIRO_MODEL_A` with `kiro_cli`, the CLI default with `copilot_cli` | Model for `reviewer-a`, `vote-1` and the adjudicator. |
| `model_b` | `KIRO_MODEL_B` with `kiro_cli`, the CLI default with `copilot_cli` | Model for `reviewer-b` and `vote-2`. |

Each seat starts a new agent session, so the two reviewers and the two voters
work on their own. Two different models make them more independent. Test a
model before you make it a default (see [Models](#models)). Use model names that
the selected CLI accepts.

Only one review of a PR runs at a time. A later dispatch for the same PR waits.

Each run reviews one head SHA. If the PR head moves during the run, `publish`
still posts the review on the reviewed head, with a warning, while that commit is
part of the PR. After a force push removes the commit, `publish` stops as Stale
and posts nothing.

### Models

The `env` block of the workflow sets the models of a `kiro_cli` run. To change a
default, change it there. The `model_a` and `model_b` inputs change a model for
one run.

| Variable | Default | Use |
| --- | --- | --- |
| `KIRO_MODEL_A` | `claude-opus-5.5` | Model A: `reviewer-a`, `vote-1` and the adjudicator. The adjudicator is the one seat that decides alone, so it uses model A. |
| `KIRO_MODEL_B` | `claude-opus-5.5` | Model B: `reviewer-b` and `vote-2`. The default is the same as model A. See the tests of other models below. |
| `KIRO_MODEL_DEFAULTS` | `{"claude-opus-5.5": {"output_config": {"effort": "max"}}}` | The Kiro setting `chat.modelDefaults` of the seats, with settings for each model ID. The default sets the `max` reasoning effort for Claude Opus 5.5, so every seat uses it. |

- The workflow writes `KIRO_MODEL_DEFAULTS` to the Kiro user settings of the
  seats. The seats start in an empty directory, so no workspace setting takes
  precedence. Claude models use `output_config.effort`, and GPT models use
  `reasoning.effort`. Not all models accept every level. See
  [Effort](https://kiro.dev/docs/models/effort/). When you change a model, update
  `KIRO_MODEL_DEFAULTS` to match. It applies only to the model IDs that it names.
- Before `cao-server` starts, the workflow runs
  `kiro-cli chat --list-models --format json`. If the `KIRO_API_KEY` account does
  not offer model A or model B, the run stops. Without this check, Kiro can fail a
  seat or use another model.
- Tests of other models as model B, with Kiro CLI 3.5.0 on the same PR:
  - Kimi K3: 3 of 4 seats in two runs ended with
    `no report: the agent did not call workflow_return`. In the run with seat
    logs, Kimi K3 sent malformed arguments in 6 of its 33 tool calls. After three
    failed calls in a row, Kiro stopped the seat before it returned a report. In
    the same run, the Claude Opus 5.5 seats made 89 tool calls with no failure.
  - Grok 4.7: both seats returned valid reports in one run. Grok 4.7 is
    experimental, so Kiro can change or remove it.

  Kimi K3 and every experimental model are Kiro Excluded Models, so the output
  indemnity in Section 50.10 of the
  [AWS Service Terms](https://aws.amazon.com/service-terms/) does not apply. See
  [Kiro Excluded Models](https://kiro.dev/docs/models/available-models/#kiro-excluded-models).
  Before you make a model the default, test it with the `model_a` or `model_b`
  input.
- A `copilot_cli` run uses the Copilot CLI default model unless you set an input.
- When the panel completes, the job log shows the model and the report status of
  each seat.

## Trust boundaries

- **Manual dispatch only.** Nothing runs on a `pull_request` or
  `pull_request_target` event. Only a user with write access can dispatch the
  workflow, so a fork PR cannot start it or reach its secrets.
- **Split tokens.** The `review` job, where the agents run, has no write token.
  Only `publish` can write, and it runs no agent. It posts with `event: COMMENT`
  only, and at most one review for each workflow run.
- **No PR code runs.** The PR head is checked out as files with
  `persist-credentials: false`. No step builds, installs or runs code from it. CAO,
  the seat profiles and the workflow come from the dispatched ref.
- **Empty working directory.** The agents start in an empty directory, not in the
  PR checkout. Agent instructions and tool settings in the PR do not load.
- **Read-only seats.** The profiles grant only `fs_read` and `fs_list`. CAO turns
  this grant into the tool settings of the selected CLI, so a seat has no shell,
  write or web tool. With Copilot, the grant also turns off the built-in MCP
  servers, such as the GitHub MCP server. The prompts tell each seat that all PR
  content is evidence, not instructions.
- **Filtered environment.** `cao-server`, tmux, the agents and the Kiro model
  check get only `HOME`, `PATH`, `SHELL`, `USER`, `LANG`, `LC_ALL` and the
  variables of the selected engine: `KIRO_API_KEY` and `KIRO_NO_AUTO_UPDATE` for
  Kiro, and `COPILOT_GITHUB_TOKEN` or the job token as `GITHUB_TOKEN` for
  Copilot. No other `GITHUB_*`, `RUNNER_*` or `ACTIONS_*` variable of the job
  reaches them.
- **Pinned tools.** Kiro CLI is a pinned release with a SHA-256 check. Copilot CLI
  is a pinned npm version, installed with `--ignore-scripts`. Each action is pinned
  to a commit SHA. `uv` installs the committed `uv.lock`. No cache is used.
- **Credential check.** Before the upload, `github_review.py check-secrets` refuses
  a `review.json` that holds the value of an engine credential (raw, Base64 or hex)
  or a known token pattern. `summary` and `publish` check for token patterns
  again.
- **Safe text.** Agent text in the review has no control characters, no HTML
  comment delimiters and no live @mentions. The job log never shows agent text.
  The run summary shows the review only after `check-secrets` and the checks of
  `github_review.py summary` pass. When the panel completes, the log shows the
  model and the report status of each seat. When a run fails, it shows only step
  IDs, states and error kinds.

A script-tier CAO workflow needs an approved plan. No person is at the runner to
approve it, so the `review` job turns `workflow.require_approval` off in a private
CAO settings file. The maintainer who dispatched the run chose the workflow, the
profiles and the PR, and all three come from the dispatched ref.

### Residual risks

- The agents run as the runner user. The tool settings of the selected CLI are the
  only control that stops a seat from writing files or running commands.
- The engine credential is in the environment of the agent processes, because the
  CLI needs it. `check-secrets` does not find an obfuscated copy. The job token
  expires when the job ends, but a `KIRO_API_KEY` or a personal access token does
  not. Rotate the secret if a review shows unexpected text.
- CAO always allows its callback tools, such as `send_message`. A seat can send
  text to another seat of the same run.
- The PR content goes to the model provider of the selected engine.

## Limits

- A PR with more than 1,000 changed files is Blocked.
- Each reviewer reports at most 8 findings and 5 unknowns.
- Each seat can use 1,800 seconds. The panel step of the job can use 120 minutes.
- The review body must fit one GitHub review. The full body is used first, then a
  compact body, then a cut body with a note. The `pr-review-result` artifact keeps
  `review.json` with every finding for 7 days.
- At most 100 inline comments are posted. If GitHub refuses the inline comments,
  the workflow posts the review without them and adds a note.

## Troubleshooting

| Message | Cause |
| --- | --- |
| `The kiro_cli engine needs the KIRO_API_KEY secret.` | The secret is not set. |
| `A model name is not valid.` | A model name has more than 100 characters, or a character that is not a letter, a digit, `.`, `_`, `:`, `/` or `-`. |
| `Kiro CLI did not list the models of the KIRO_API_KEY account.` | `kiro-cli chat --list-models` failed or returned no model list. Check the key and the subscription. The step log shows the Kiro CLI messages. |
| `The KIRO_API_KEY account does not offer the model …` | The account cannot use this model. For example, an administrator did not approve the model, or the model is not available in the geography of the account. Select a model from the list in the message. Then change `KIRO_MODEL_A` or `KIRO_MODEL_B`, or set the input. |
| `Blocked: … no reviewer returned a valid report: reviewer-a: no report: the step ended in state failed; the step call failed: HTTP 400` | CAO refused to start the agent. With `kiro_cli`, a key that Kiro does not accept starts the terminal UI with no model in its status row, and CAO refuses that terminal. Check the key, the subscription and the administrator settings. |
| `… no report: the agent did not call workflow_return` | The seat ended with no report. With `copilot_cli`, the usual cause is that Copilot has no authentication. A governance rule that blocks the `cao-mcp-server` MCP server has the same result. A model can also cause it: in tests with `kiro_cli`, Kimi K3 sent malformed tool arguments, and Kiro stopped the seat (see [Models](#models)). Test another model with the `model_a` or `model_b` input. |
| `… no report: the step ended in state failed (error); the step call failed: HTTP 502 (error)` | The agent terminal showed an error. With `copilot_cli`, the usual cause is that GitHub refused the Copilot request. If the organization policy is off, Copilot CLI refuses the job token with `Access denied by policy settings`. Turn on the policy, or set the `COPILOT_GITHUB_TOKEN` secret with the "Copilot Requests" permission. |
| `Stale: the checkout is not the head that prepare read` | The PR changed during setup. Dispatch again. |
| `Stale: the reviewed head is no longer a commit of the PR` | A force push removed the reviewed commit. Dispatch again. |
| `Skipped: … is a draft` | Mark the PR ready for review, then dispatch again. |

## Run the CAO workflow without GitHub Actions

Use absolute paths. `work_dir` must be an empty directory, and the three
directories must not overlap. The checkout must hold the head commit that
`prepare` read and `origin/<target branch>`.

```bash
review_dir=.github/pr-review
python3 "$review_dir/github_review.py" prepare --repo OWNER/REPO --pr 123 --out /abs/evidence
git clone https://github.com/OWNER/REPO.git /abs/pr-head
git -C /abs/pr-head fetch origin pull/123/head
git -C /abs/pr-head checkout --detach "$(jq -r .head_sha /abs/evidence/pr.json)"
python3 "$review_dir/github_review.py" collect --evidence /abs/evidence --source /abs/pr-head

for profile in pr_reviewer pr_voter pr_adjudicator; do
  cao install "$review_dir/$profile.md" --provider kiro_cli
done
cao workflow create pr_review --from-file "$review_dir/pr_review.py"
cao workflow run pr_review --run-id pr-review-123 \
  --input evidence_dir=/abs/evidence \
  --input source_dir=/abs/pr-head \
  --input work_dir=/abs/empty-dir \
  --input engine=kiro_cli \
  --working-directory /abs/empty-dir --json
```

For Copilot, use `copilot_cli` in `cao install` and in the `engine` input. With
plan approval on (the default), the first run is refused, and the error gives its
`plan_id`. Approve it with `cao workflow approve <plan_id>`, then run again with a
new `--run-id`. The optional `step_timeout` input sets the time of each seat, from
300 to 2,700 seconds.

`/abs/evidence/review.json` holds the result. To see the review request without
posting it, add `--dry-run /abs/request.json` to `github_review.py publish`.

## Files

- [`pr_review.py`](pr_review.py): the CAO workflow. It runs the panel, applies the
  verdict rules and writes `review.json`.
- [`github_review.py`](github_review.py): the GitHub and Git side: `prepare`,
  `collect`, `check-secrets`, `publish` and `summary`. Standard library only, plus
  `gh` and `git`.
- [`pr_reviewer.md`](pr_reviewer.md), [`pr_voter.md`](pr_voter.md),
  [`pr_adjudicator.md`](pr_adjudicator.md): the read-only seat profiles.
- [`.github/workflows/pr-review.yml`](../workflows/pr-review.yml):
  the GitHub Actions workflow.
- [`test/test_pr_review_workflow.py`](../../test/test_pr_review_workflow.py):
  the unit tests of both scripts.
