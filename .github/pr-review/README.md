# Pull Request Review Workflow

This workflow reviews one pull request of this repository with CAO agents. By
default, one agent reviews the PR. A maintainer can choose a panel of five seats
instead. Each seat is a step of a CAO workflow, with its own agent and terminal.
No seat is a sub-agent of another session. A maintainer starts the review by
hand and selects the agent CLI: `kiro_cli` (the default) or `copilot_cli`. The
workflow posts one comment on the PR, updates that comment on later runs, and
adds a neutral check to the reviewed head commit.

The review never approves a PR, and its check is always neutral. After the
comment, the workflow requests changes when the review accepts a P1 or P2
finding, approves the waiting CI runs of the PR's commits, and requests a
review from the maintainer team. See [Actions on the PR](#actions-on-the-pr). A
maintainer still approves and merges.

The agents apply the review policy in [`REVIEW.md`](../../REVIEW.md). The
[design](../../docs/pr-review-design.md) explains the decisions, and how the
review meets #1350 and #464.

## How it works

The GitHub Actions workflow
[`.github/workflows/pr-review.yml`](../workflows/pr-review.yml) has
three jobs:

| Job | Token permissions | What it does |
| --- | --- | --- |
| `prepare` | `contents: read`, `pull-requests: read` | Reads the PR. A closed PR fails the run. A draft PR is skipped. |
| `review` | `contents: read`, `copilot-requests: write` | Runs in the `ai-review` environment, which holds the engine secret. Checks out the PR head as files, writes the diff, starts `cao-server` and runs the CAO workflow [`pr_review.py`](pr_review.py). Then it stops the agents, checks the result for credentials and shows the review in the run summary. |
| `publish` | `contents: read`, `pull-requests: write`, `checks: write`, `actions: write` | Validates `review.json` and reads the PR again. Then it posts or updates the review comment, adds a neutral check run to the reviewed head, and does the [actions on the PR](#actions-on-the-pr). If the review did not finish or was not posted, it adds a neutral check run that says so. It skips with no check run in two cases: the PR is now a draft, or a newer run posted the comment. It runs no agent. |

The `reviewers` input selects the seats.

- **`single`** (the default) runs one reviewer, `reviewer-a`. The script accepts
  each finding of the reviewer at its proposed severity. No vote and no
  adjudication run, and the review says that no second agent checked the
  findings.
- **`panel`** runs three phases. The seats of one phase run at the same time.
  1. **Review.** Two reviewers, `reviewer-a` and `reviewer-b`, read the evidence
     and the head checkout.
  2. **Vote.** The script merges exact duplicates (the same path, line and
     title) and numbers the candidates `F1` to `Fn`. Two fresh voters, `vote-1`
     and `vote-2`, check each candidate against the source. A voter does not see
     who proposed a candidate or the other vote.
  3. **Adjudicate.** An adjudicator reads the cited source and decides each
     candidate: `accept`, `reject` or `owner_decision`.

  With no candidates, the voters and the adjudicator do not run.

Each reviewer reports the files that it reviewed, the status of each check in
`REVIEW.md`, the overall risk with one to five reasons, and at most 8 candidate
findings. Each finding gives its check, path, line, failure, impact, evidence,
confidence and suggested fix. Before the vote, the script drops each finding
whose path is not a file in the head commit, or whose line is not a line of
that file. The script reads the commit with Git, not the files of the checkout,
because the checkout also holds `.git` and can hold files that are not in the
commit. It does not follow a symbolic link, because GitHub shows the link
itself at that path. An empty file has no lines. The review lists each dropped
finding. If Git cannot read the head commit, the review is Blocked.

### The review policy

The `review` job gives the driver the absolute path of `REVIEW.md` from the
dispatched ref, as the `policy` input. Each agent reads that file first and
applies it. A `REVIEW.md` in the PR checkout is evidence only, also when the PR
changes it. Each line of the form `### A1. Title` in the policy is one check. Each
reviewer reports each check as `checked`, `not_checked` or `not_applicable`. The
review names the policy file and the start of its SHA-256, so you can see which
policy ran. The prompts keep only the safety rules.

Each seat is one `step(...)` call with its own `step_id` and
`recovery="idempotent"`. The agent returns its report with CAO's
`workflow_return` tool and a JSON Schema. The script reads the reports from the
CAO run view and validates them again. A missing or invalid report never becomes
a finding or a pass. If a step call returns before CAO ends the step, the script
waits up to 150 seconds for the step to end before it reads the report.

### Verdict rules

These rules apply in panel mode. In single mode, each finding is accepted at its
proposed severity.

The adjudicator decides. Votes alone never accept a finding. The script changes a
decision only in these cases:

- Both voters confirmed a P1 or P2 finding, and the adjudicator rejected it or
  lowered it to P3. The finding becomes an owner question at the higher voter
  severity.
- An owner question that the adjudicator opens keeps at least the proposed
  severity, so the P3 rule below cannot reject an open P1 or P2 question.
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
| `changes_required` | The review accepted a P1 or P2 finding. |
| `owner_decision_required` | A P1 or P2 candidate needs a decision from the PR owner. |
| `incomplete` | A reviewer did not account for every changed file, read no changed file in full, or marked no check `checked`. Or no reviewer checked a check of `REVIEW.md`, the script dropped a finding with a bad location, or a voter or the adjudicator returned no valid report. |
| `no_blocking_findings` | None of the above. Accepted P3 findings can still be listed. |

If the run cannot make a review, `review.json` is Blocked: `publishable` is
`false`, and `blocked_reason` gives the reason. For example, no reviewer returned
a valid report. The `review` job then fails at its check step, and nothing is
posted.

The review is one comment on the PR. The comment starts with a line that names
the engine, the reviewers and the person who started the run, and says that the
review is not an approval. Then it gives a link to the run, the result, the risk
level with its reasons, and each finding with its check, location, impact,
evidence and suggested fix. In panel mode, each candidate also shows its votes
and its decision. Then come the dropped findings, the status of each check of
`REVIEW.md`, the coverage of each reviewer, and the agents with their models.
The checks section names the policy file and the start of its SHA-256. A hidden
marker at the end holds only validated values, such as the run ID and the
result. A later run finds the comment by this marker and by its author, and
updates it in place. The workflow posts no inline comments.

Then `publish` adds the check run `PR review (advisory)` to the reviewed head.
Its conclusion is always `neutral`. Its title gives the result. Its summary gives
the counts, the risk level, the number of checks that no reviewer checked, and
links to the comment and the run. The check run holds no agent text.

After the credential check, the `review` job adds the review to the summary page
of the workflow run. The summary starts with a line that names the PR, the
reviewed head and the engine. Then it gives the same sections as the review
comment. The run summary shows the review also when `publish` posts nothing, for
example because the PR became a draft. Each person who is signed in to GitHub
and can read the repository can see the run summary. The `publish` job adds its
result to the run summary: the IDs of the comment and the check run and one line
for each action on the PR, or the reason why it posted nothing.

### Actions on the PR

After the check run, `publish` does three actions. It never approves a PR.

1. **Change request.** If the review accepts a P1 or P2 finding, `publish`
   requests changes at the reviewed head, with a link to the review comment. It
   does not request changes again at a head where an earlier run did. A later
   run dismisses these change requests only when its review is complete, accepts
   no P1 or P2, and the head did not move during the review. Otherwise they stay
   until a maintainer dismisses them. `publish` changes only the reviews that
   this workflow wrote.
2. **CI runs.** A CI run of a PR from a fork waits until a maintainer approves
   it. `publish` approves the waiting `pull_request` runs of each commit of the
   PR, oldest first, when the run comes from the head repository of the PR. If
   the PR head moved during the review, it approves no run, because no review
   read the new commits.
3. **Review request.** `publish` requests a review from the maintainer team,
   `REVIEW_TEAM` in the workflow. GitHub cannot assign a team to a PR.

Each action writes one line to the job log and the run summary. If an action
fails, `publish` still does the next action, and the job fails at the end. A
failed action does not remove the comment or the check run.

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
must offer the models of the seats. See [Models](#models).

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

### 2. Limit the secret to the default branch

The `review` job runs in the GitHub environment `ai-review`, with
`deployment: false`. So the job gets the secrets and the branch rule of the
environment, and GitHub records no deployment. If the environment does not
exist, the first run creates it with no rules and no secrets, and the job uses
the repository secrets.

A user with write access can dispatch this workflow from any branch, and so can
change the workflow file of that run. A changed workflow file can read each
repository secret. After the workflow is on the default branch, do these steps:

1. In **Settings > Environments > `ai-review`**, set **Deployment branches and
   tags** to **Selected branches and tags**. Add the default branch only.
2. Add the secret of the engine to the environment, then delete the repository
   secret of the same name:

   ```bash
   gh secret set KIRO_API_KEY --env ai-review --repo OWNER/REPO
   gh secret delete KIRO_API_KEY --repo OWNER/REPO
   ```

   Do the same for `COPILOT_GITHUB_TOKEN` if you use it.

Then only a run from the default branch can start the `review` job. Do not add a
custom deployment protection rule to `ai-review`: with `deployment: false`, such
a rule fails the job.

### 3. Put the workflow on the default branch

GitHub runs a `workflow_dispatch` workflow only when the workflow file is on the
default branch.

## Run a review

In the **Actions** tab, select **PR Review**, then **Run workflow**. Or use the
GitHub CLI:

```bash
gh workflow run pr-review.yml --repo OWNER/REPO -f pr=123
gh workflow run pr-review.yml --repo OWNER/REPO -f pr=123 -f reviewers=panel
gh workflow run pr-review.yml --repo OWNER/REPO -f pr=123 -f engine=copilot_cli
```

| Input | Default | Meaning |
| --- | --- | --- |
| `pr` | (required) | Number of the PR to review. |
| `engine` | `kiro_cli` | `kiro_cli` or `copilot_cli`. |
| `reviewers` | `single` | `single`: one reviewer. `panel`: two reviewers, two voters and an adjudicator. |
| `model_a` | `KIRO_MODEL_A` with `kiro_cli`, the CLI default with `copilot_cli` | Model for `reviewer-a`, and in panel mode also for `vote-1` and the adjudicator. |
| `model_b` | `KIRO_MODEL_B` with `kiro_cli`, the CLI default with `copilot_cli` | Panel mode only. Model for `reviewer-b` and `vote-2`. |

In panel mode, each seat starts a new agent session, so the two reviewers and
the two voters work on their own. Two different models make them more
independent. Test a model before you make it a default (see [Models](#models)).
Use model names that the selected CLI accepts.

Only one review of a PR runs at a time. A later dispatch for the same PR cancels
the earlier run, and a cancelled run posts nothing. If an earlier run still
reaches `publish` after a later run posted, it sees the newer run ID in the
comment marker and posts nothing.

Each run reviews one head SHA. If the PR head moves during the run, `publish`
still posts the review for the reviewed head, with a warning, while that commit
is part of the PR. It then approves no CI run and dismisses no change request.
After a force push removes the commit, `publish` stops as
Stale. It posts no comment, and adds the neutral check run
`Review not published (advisory)` to the reviewed head.

### Models

The `env` block of the workflow sets the models of a `kiro_cli` run. To change a
default, change it there. The `model_a` and `model_b` inputs change a model for
one run.

| Variable | Default | Use |
| --- | --- | --- |
| `KIRO_MODEL_A` | `claude-opus-5.5` | Model A: `reviewer-a`, and in panel mode also `vote-1` and the adjudicator. The adjudicator is the one seat that decides alone, so it uses model A. |
| `KIRO_MODEL_B` | `claude-opus-5.5` | Model B, panel mode only: `reviewer-b` and `vote-2`. The default is the same as model A. See the tests of other models below. |
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
- When the CAO run completes, the job log shows the model and the report status
  of each seat.

## Trust boundaries

- **Manual dispatch only.** Nothing runs on a `pull_request` or
  `pull_request_target` event. Only a user with write access can dispatch the
  workflow, so a fork PR cannot start it or reach its secrets.
- **Split tokens.** The `review` job, where the agents run, cannot write to the
  repository. Its job token has only `contents: read` and
  `copilot-requests: write`. With `copilot_cli` and no `COPILOT_GITHUB_TOKEN`
  secret, the agents get the job token, and they can use it to make Copilot
  requests. Only `publish` can write to the repository, and it runs no agent. It
  writes one comment on the PR, which later runs update in place, neutral check
  runs and the [actions on the PR](#actions-on-the-pr). It never approves a PR
  and never posts an inline comment. It needs `actions: write` to approve CI
  runs. That permission also lets the job cancel, re-run and start workflows.
- **Secret in an environment.** The `review` job reads the engine secret from the
  `ai-review` environment. When the environment allows only the default branch,
  a workflow file that was changed on another branch cannot read the secret. See
  [Set up](#2-limit-the-secret-to-the-default-branch).
- **No PR code runs.** The PR head is checked out as files with
  `persist-credentials: false`. No step builds, installs or runs code from it. CAO,
  the seat profiles and the workflow come from the dispatched ref.
- **Diff evidence.** `collect` reads Git attributes from the merge base, so a
  `.gitattributes` file in the PR cannot turn a change into
  `Binary files differ`. Git still shows a file with a NUL byte that way, also a
  source file. So `changed_files.json` marks each such file with `binary`, and
  the reviewer prompt says to read the file in the checkout before a seat skips
  it.
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
  Kiro, and `COPILOT_AUTO_UPDATE` and `COPILOT_GITHUB_TOKEN` (or the job token
  as `GITHUB_TOKEN`) for Copilot. No other `GITHUB_*`, `RUNNER_*` or
  `ACTIONS_*` variable of the job reaches them.
- **Pinned tools.** Kiro CLI is a pinned release with a SHA-256 check. Copilot CLI
  comes from the committed lockfile in [`copilot/`](copilot/), installed with
  `npm ci --ignore-scripts`, which checks the integrity hash of each package and
  runs no install script. The filtered environment has no `CI`, so
  `COPILOT_AUTO_UPDATE=false` stops Copilot CLI from downloading a newer
  version. Each action is pinned to a commit SHA. `uv` installs the committed
  `uv.lock`. No cache is used.
- **Credential check.** The `review` job stops `cao-server` and the agents first.
  Then `github_review.py check-secrets` refuses a `review.json` that holds the
  value of an engine credential (raw, Base64 or hex) or a known token pattern.
  This check runs before the upload. `summary` and `publish` check for token
  patterns again.
- **Safe text.** Agent text in the review has no control characters, no live
  @mentions and no live short issue references. The script escapes `\`, `` ` ``,
  `[`, `]`, `&`, `<` and `>` in agent text, so agent text cannot add a link, an
  image, an HTML tag, an HTML comment, an entity or a code span. It breaks each
  @mention and each short issue reference, such as `#12`, `owner/repo#12` or
  `GH-12`, with a zero-width space. It does not break them in a bare `http` or
  `https` URL that GitHub links as a whole, so the break does not change the
  link. GitHub still makes a bare URL into a link, also a URL to an issue. It
  also makes a commit SHA into a link. The check run holds no agent text. The
  job log never shows agent text.
  The run summary shows the review only after `check-secrets` and the checks of
  `github_review.py summary` pass. When the CAO run completes, the log shows the
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
- `publish` approves the waiting CI runs of every commit of the PR, but the
  review reads only the head. An earlier commit can hold a workflow change that
  no review read. A run from a fork gets a read-only job token and no secrets.
- GitHub does not document whether a change request from `github-actions[bot]`
  blocks a merge under a ruleset. If it does, the PR stays blocked until a later
  complete review or a maintainer dismisses the request.
- For a review request, GitHub lists only the permission `pull-requests: write`.
  Other projects report that a request to a team also needs the organization
  permission Members: read, which the job token cannot have. If GitHub refuses
  the request, the job fails, and the log gives the reason.

## Limits

- A PR with more than 1,000 changed files is Blocked.
- Each reviewer reports at most 8 findings and 5 unknowns. A reviewer can skip
  at most 50 files, each with a reason. If a reviewer does not account for each
  changed file, or reads none in full, the review is incomplete. So a PR that
  changes only files that a reviewer skips, such as binary files, is incomplete.
- `REVIEW.md` can have at most 40 checks.
- The report of each seat must fit the CAO limit of a step result, 32 KiB. CAO
  drops a larger report, so the seat has no report.
- Each seat can use 2,700 seconds. The review step of the job can use 150
  minutes. In ten live `kiro_cli` single runs on one PR with about 35 files and
  8,900 added lines, the seat used 19 to more than 30 minutes.
- The review must fit one GitHub comment. The full text is used first, then a
  compact text, then a cut text with a note. The cut is at the end of a line, so
  it cannot split a code span. The `pr-review-result` artifact keeps
  `review.json` with every finding for 7 days.
- `publish` reads at most 5,000 comments of the PR to find the earlier review
  comment. If the PR has more, `publish` posts no comment.
- To act on the PR, `publish` reads at most 1,000 reviews of the PR and 1,000
  waiting runs of the repository, and GitHub lists at most 250 commits of a PR.
  Past a limit, the action fails, and the log says what it did not do.

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
| `Blocked: the checkout is not the head_sha of pr.json` | The checkout holds another commit, or the evidence folder is from another `prepare`. Check out the `head_sha` of `pr.json`, or use the evidence folder that matches the checkout. In the workflow, the checkout and `pr.json` come from the same `prepare`, so this message shows a defect. |
| `Stale: the reviewed head is no longer a commit of the PR` | A force push removed the reviewed commit. Dispatch again. |
| `Skipped: … is a draft` | Mark the PR ready for review, then dispatch again. |
| `Blocked: the review is not publishable: "the policy has no check heading of the form '### A1. Title'"` | `REVIEW.md` on the dispatched ref has no check line. The `policy` errors also include a check ID that is not valid or is used twice, and more than 40 checks. Fix `REVIEW.md`, then dispatch again. |
| Check run `Review did not finish (advisory)` | The `review` job failed or timed out. The check gives the job result, and the job log gives the reason. A review comment on the PR, if there is one, is from an earlier run. |
| Check run `Review not published (advisory)` | The review finished, but `publish` did not post or update the comment, for example because the reviewed head is stale. The `publish` job log gives the reason. |
| `- Change request failed: …`, `- CI runs failed: …` or `- Review request failed: …` | The comment and the check run are posted, but an action on the PR failed. The line gives the reason. Do the action on the PR by hand, or dispatch again. |
| The `review` job fails before its first step, with an error about the protection rules of `ai-review` | The environment allows only the default branch. Dispatch from the default branch. |

## Run the CAO workflow without GitHub Actions

Use absolute paths. `work_dir` must be an empty directory, and the three
directories must not overlap. The checkout must hold the head commit that
`prepare` read and `origin/<target branch>`. `collect` needs Git 2.40 or later.
`policy` is the `REVIEW.md` of a trusted checkout, outside the three directories,
never the copy in the PR checkout.

An agent gets variables from three places:

- `cao-server` gives it the `CAO_*`, `KIRO_*`, `MISE_*` and `AWS_*` variables of
  its own environment.
- CAO opens each agent in the tmux server of `TMUX_TMPDIR` (by default `/tmp`).
  A tmux server gives each new pane the environment of the shell that started
  the server. So if your own tmux server runs there, the agents get its
  environment.
- The pane runs a login shell, which reads the start files in `HOME`, such as
  `~/.profile`.

So isolate CAO as the workflow does. First stop any other `cao-server`, because
the `cao` commands use the server on port 9889. Then run `cao-server` and each
`cao` command through `review_env` below. It gives them a new empty `HOME`
outside the three directories, their own tmux server, and only the credential of
the engine and a few basic variables. Run `prepare`, `git` and `collect` in your
usual shell. The seats can still read each file that your account can read, so
use an account or a container that holds no other credential.

```bash
review_dir=.github/pr-review
python3 "$review_dir/github_review.py" prepare --repo OWNER/REPO --pr 123 --out /abs/evidence
git clone https://github.com/OWNER/REPO.git /abs/pr-head
git -C /abs/pr-head fetch origin pull/123/head
git -C /abs/pr-head checkout --detach "$(jq -r .head_sha /abs/evidence/pr.json)"
python3 "$review_dir/github_review.py" collect --evidence /abs/evidence --source /abs/pr-head

mkdir -m 700 /abs/review-home
review_env() (
  export HOME=/abs/review-home TMUX_TMPDIR=/abs/review-home SHELL=/bin/bash
  for name in $(compgen -e); do
    case " HOME PATH SHELL USER LANG TMUX_TMPDIR KIRO_API_KEY " in
      *" $name "*) ;;
      *) unset "$name" 2> /dev/null || true ;;
    esac
  done
  exec "$@"
)
review_env cao-server > /abs/cao-server.log 2>&1 &
server_pid=$!
until curl -sf http://127.0.0.1:9889/health > /dev/null; do sleep 1; done

for profile in pr_reviewer pr_voter pr_adjudicator; do
  review_env cao install "$review_dir/$profile.md" --provider kiro_cli
done
review_env cao workflow create pr_review --from-file "$review_dir/pr_review.py"
review_env cao workflow run pr_review --run-id pr-review-123 \
  --input evidence_dir=/abs/evidence \
  --input source_dir=/abs/pr-head \
  --input work_dir=/abs/empty-dir \
  --input policy=/abs/trusted/REVIEW.md \
  --input engine=kiro_cli \
  --input reviewers=single \
  --working-directory /abs/empty-dir --json
```

For Copilot, use `copilot_cli` in `cao install` and in the `engine` input, and
keep `COPILOT_GITHUB_TOKEN` instead of `KIRO_API_KEY` in `review_env`. With
plan approval on (the default), the first run is refused, and the error gives its
`plan_id`. Approve it with `review_env cao workflow approve <plan_id>`, then run
again with a new `--run-id`. The optional `step_timeout` input sets the time of
each seat, from 300 to 2,700 seconds.

If a run stops before it ends, for example at a halted step, continue it with
`review_env cao workflow resume pr-review-123`. CAO replays the seats that ended
and does not run them again, and the run takes back its own `panel` directory in
the evidence. A `panel` directory of another run blocks the review, so give each
new run a new evidence directory.

`/abs/evidence/review.json` holds the result. Before you post it or share it,
check it for the credential of the engine:

```bash
python3 "$review_dir/github_review.py" check-secrets \
  --file /abs/evidence/review.json --env KIRO_API_KEY
```

To see the requests without posting them, add `--dry-run /abs/request.json` to
`github_review.py publish`. The file also gives the planned actions on the PR.

When you no longer need the run, stop `cao-server` and the tmux server of the
agents. Give tmux the socket path, so that the command cannot stop another tmux
server:

```bash
kill "$server_pid"
tmux -S "/abs/review-home/tmux-$(id -u)/default" kill-server
```

## Files

- [`REVIEW.md`](../../REVIEW.md): the review policy. Its rules and checks hold
  the lessons of this repository.
- [`docs/pr-review-design.md`](../../docs/pr-review-design.md): the design and the
  ticket contract.
- [`pr_review.py`](pr_review.py): the CAO workflow. It runs the reviewers, applies
  the verdict rules and writes `review.json`.
- [`github_review.py`](github_review.py): the GitHub and Git side: `prepare`,
  `collect`, `check-secrets`, `publish`, `report-failure` and `summary`. Standard
  library only, plus `gh` and `git`.
- [`pr_reviewer.md`](pr_reviewer.md), [`pr_voter.md`](pr_voter.md),
  [`pr_adjudicator.md`](pr_adjudicator.md): the read-only seat profiles.
- [`copilot/`](copilot/): the npm lockfile of the pinned Copilot CLI.
- [`.github/workflows/pr-review.yml`](../workflows/pr-review.yml):
  the GitHub Actions workflow.
- [`test/test_pr_review_workflow.py`](../../test/test_pr_review_workflow.py):
  the unit tests of both scripts and of the seat profiles.
