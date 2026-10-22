# Pull Request Health Workflow

This example evaluates every open GitHub pull request with deterministic rules,
tracks degradation across runs, and produces an auditable Markdown and JSON
report. It also includes a CAO scheduled flow with an exact 14-day cadence.

The score is rule-based. The optional reviewer agent may summarize importance,
but it cannot change scores, categories, next-actor attribution, or actions. Its
prompt reads untrusted PR text (titles, bodies, comments), so treat its output as
advisory prose only — nothing it says can alter a score or trigger an action.

## Notification lifecycle

Notifications are grouped into **notification epochs**. One epoch is at most
three comments:

1. **warning** — the owner is asked for an update. Every epoch starts here, for
   every PR, protected or not.
2. after a `7`-day grace period with no owner activity, either
   - an **escalation** to maintainers, if the PR is protected (P0/P1, or already
     approved) — this is terminal and ends the epoch at two comments; or
   - a **draft recommendation** otherwise. On a PR that is already a draft this
     is worded as a second owner notification; the stage is the same.
3. after a further `14`-day grace period on an unanswered draft recommendation, a
   **closure recommendation**, which is terminal. This is also where the `-25`
   unanswered-draft penalty is applied.

Every one of these is an advisory comment. A protected PR is never recommended
for closure, and no PR is ever moved, labelled, or closed by this workflow.

A terminal advisory (**escalation** or **closure recommendation**) **closes** the
epoch. The ladder is time-driven, not score-driven: any PR still scoring under 60
with the owner as next actor advances when its grace period expires. There is
deliberately no secondary score gate on advancement, because the old `<=50` and
`<30` gates created a dead zone in which a warned PR scoring 51-59 never advanced
and never stopped being re-evaluated.

Reopening a closed epoch requires all of:

- a **90-day** cooldown measured from whichever came last, the terminal advisory
  or the owner's most recent answer to it, and
- the lifetime cap not being reached.

Re-entry always **restarts at a warning**. No epoch can open on a draft
recommendation, an escalation, or a closure recommendation.

Owner activity (a commit or a comment from the PR author after the current
marker) answers the epoch for a **30-day** grace period, reported as
`monitor_response`. When that grace expires the workflow does **not** escalate —
an expired answer settles the epoch onto the 90-day cooldown, anchored on the
owner's activity rather than on the older advisory. Escalating a PR whose owner
replied, merely late, would be the loudest thing this workflow could do.

Lifetime caps per PR: **four notification epochs** and **three escalation**
notifications. At either cap the PR becomes report-only: the decision is recorded
as `report_only`, it appears in the report and in the run's structured decisions,
and **no comment is posted**.

### Marker versioning

Each notification embeds a hidden marker in its comment:

```text
<!-- cao-pr-health:v2 epoch=1 stage=warning score=42 as_of=2026-08-03 -->
```

Stage dedup is **exact on the `(epoch, stage)` pair**. It is not keyed on marker
text, which embeds the emitting run's own score and date and therefore never
matches a later run; and it is not keyed on a date window, which expires and so
would let a spent stage be posted a second time inside a single epoch.

The earlier `cao-pr-health:v1` markers — both the `stage=` form and the
`action=escalation` form — are read as **epoch 0**. A PR that was mid-ladder
when this example changed keeps its place instead of restarting a ladder it has
already climbed.

## Safety model

The workflow defaults to `dry_run` and makes no GitHub changes. Apply mode is
comment-only: it can post PR comments, but it cannot add or remove labels,
change PR state, submit reviews, write commit statuses or checks, or make any
other PR-status change. It has these additional safeguards:

- A PR must score below 60 on two separate dated evaluations before an owner
  warning is eligible.
- Every stage transition additionally requires no owner activity since the
  current marker.
- Every candidate is fetched and scored again immediately before commenting; a
  changed live score, recommendation, or epoch is recorded as
  `skipped_live_drift` and nothing is posted.
- The write boundary accepts only `gh pr comment`; it rejects every other
  GitHub command before starting a subprocess.
- Every comment command contains the explicit repository and PR number.
- Every comment begins with a visible automated PR-health attribution header
  and the same comment-only disclaimer.
- Only hidden markers in comments authored by the authenticated workflow
  identity can advance lifecycle stages or make actions idempotent.
- Every branch that observes a marker returns a decision. Nothing falls through
  to the fresh ladder, so the ladder cannot run backwards.
- The `-25` unanswered-draft penalty applies only while the raw score is under
  60, and the penalized score is never persisted — persisting it would re-apply
  the penalty every run and drift a recovered PR into `abandoned`.
- A per-PR state problem (backfill, clock skew, a reopened PR carrying old
  state) restarts that PR's observation streak; it never aborts the run for the
  other PRs.
- A persisted-state file written by an older schema is migrated, not rejected;
  rejecting it would abort every run for the repository until an operator
  deleted the file by hand.
- Runs for the same repository are serialized with a local file lock.

## Authorization: approval, not prose

Dry-run mode is automatic. **Apply mode is not authorized by any wording in the
flow template or in this file** — prose is not an authorization boundary. It is
authorized by CAO's plan-approval gate:

- A `plan_id` is computed at run start from the run's execution-affecting fields,
  and those include the run `inputs`. `mode=apply` therefore produces a
  **different plan** than `mode=dry_run`, and approving one never authorizes the
  other.
- Enable the gate with `workflow.require_approval` in `settings.json` (or
  `CAO_WORKFLOW_REQUIRE_APPROVAL`, which can only turn it **on**). It is
  **off by default**, so an unapproved plan still runs on a default install.
- A new or changed plan is refused once: run it, copy the `plan_id` from the
  refusal, then `cao workflow approve <plan_id>` and run again. There is no way
  to revoke an approval.
- Editing `pr_health.py` changes the source hash, which changes the `plan_id`,
  which needs a fresh approval.

This gate is a same-user local control, not a privilege boundary: CAO and the
workflow script both run as the invoking user. What it does provide is that an
apply plan cannot execute unnoticed under an approval granted for a different
plan.

> **A registered apply schedule is still a standing unattended comment grant.**
> It comments on contributors' pull requests in the configured repository under
> the operator's `gh` identity with no per-run review. It cannot change PR
> status. Trial the template against a fork you own before registering it against
> a shared repository, and read the dry-run report for at least one full cycle
> first.

The dry-run and apply schedules are separate templates. Both may be registered
together: the guard emits mode-qualified run and snapshot identifiers so they
never collide on a shared due date.

## Journal record, replay, and resume

The run's structured output is the journal record, and it is the intended way to
read what the workflow decided:

| Field | Meaning |
| --- | --- |
| `policy` | the six fixed lifecycle values this run applied |
| `decisions` | per-PR structured decisions (capped at 200 rows inline) |
| `decisions_file` | the complete per-PR decision list on disk |
| `artifact_dir` | the run's artifact directory |
| `snapshot_file`, `scores_file`, `report_file` | deterministic inputs and report |
| `enforcement_file`, `enforcement_results` | per-PR comment outcomes |
| `posted_github_comments` | whether any comment was actually posted |

Each decision row carries `number`, `score`, `raw_score`, `recommended_action`,
`notification_epoch`, `marker_stage`, `marker_epoch`, `action_reasons`, and
`notified`.

**Replay and resume never post a duplicate comment**, and this holds by two
independent mechanisms:

- Resume re-executes a script top-to-bottom rather than skipping completed work,
  so correctness cannot depend on in-process bookkeeping. The `(epoch, stage)`
  marker already on the PR is what makes a second pass return `already_applied`.
- A completed run's frozen `manifest.json` short-circuits a re-run with the same
  `repo`, `as_of`, `snapshot_id`, and `mode`: the stored manifest is re-emitted
  and the apply phase does not run at all. Re-running the same `snapshot_id` with
  *different* inputs is rejected rather than silently reinterpreted.

Both properties are covered by tests, including a three-year fortnightly
simulation that asserts a permanently stale PR receives exactly 12 comments and
a permanently stale protected PR exactly 7.

## Platform gaps

This example is deliberately conservative about APIs it does not have, and these
gaps are recorded rather than guessed around:

- **Step recovery policy.** The reviewer step uses `run_step()`, which declares
  no recovery policy. Newer CAO releases add `step(..., recovery=...)` to the
  `cao_workflow` shim, which is the correct surface for a read-only
  `idempotent` analysis step. The shim vendored with this checkout exports only
  `run_step`, so migrating would break the example here; the call is left on
  `run_step` and the reviewer step is idempotent by construction (it reads two
  artifact files and writes one).
- **Plan approval enforcement.** `cao workflow approve` and
  `workflow.require_approval` are the gate described above. They are not present
  in every CAO release; on a build without them, apply mode has no platform gate
  at all and the separate-template split is the only control.
- **Per-step recovery decisions on resume.** Newer releases let a halted script
  step be resolved with an explicit `rerun`/`skip` decision. This example does
  not depend on that: every step is safe to re-execute because of the marker
  dedup above.

## Scoring

| Dimension | Maximum | Deterministic signals |
| --- | ---: | --- |
| CI | 20 | passing, pending, missing, or failing checks |
| Mergeability | 15 | clean, blocked/behind, unknown, or conflicting |
| Review | 15 | approved, review required, draft, or changes requested |
| Engagement | 40 | days since the latest commit |
| Completeness | 10 | description, rationale, tests, and focused scope |

Health bands are `healthy` (85-100), `active` (70-84), `watch` (60-69),
`at_risk` (51-59), `stalled` (30-50), and `abandoned` (0-29).
Priority is calculated separately so an unhealthy but important PR is escalated
rather than discarded.

## Prerequisites

- `cao-server` running
- `gh` installed and authenticated for the target repository
- CAO `developer` and `reviewer` profiles available
- A headless provider for the optional importance analysis

## Install the workflow

```bash
mkdir -p ~/.aws/cli-agent-orchestrator/workflows
install -m 0644 \
  examples/workflows/pr-health/pr_health.py \
  ~/.aws/cli-agent-orchestrator/workflows/pr_health.py

cao workflow validate \
  ~/.aws/cli-agent-orchestrator/workflows/pr_health.py
```

## Run a dry evaluation

Supply the date explicitly. The same inputs always select the same snapshot and
artifact directory, which keeps resume behavior deterministic.

```bash
cao workflow run pr_health \
  --run-id pr-health-dry-2026-08-03 \
  --input repo=awslabs/cli-agent-orchestrator \
  --input as_of=2026-08-03 \
  --input snapshot_id=dry-2026-08-03 \
  --input importance_analysis=true \
  --input importance_provider=claude_code \
  --input importance_agent=reviewer \
  --input mode=dry_run \
  --json
```

Artifacts are written under:

```text
~/.local/state/cao/pr-health/<percent-encoded-owner%2Frepository>/runs/<snapshot-id>/
```

That directory is reported as `artifact_dir` and contains `snapshot.json`,
`scores.json`, `report.md`, `decisions.json`, `manifest.json`, and — in apply
mode — `enforcement.json`.

## Apply eligible actions

Review the dry-run report first. With `workflow.require_approval` enabled, the
first `mode=apply` run is refused and prints its `plan_id`; approve that plan,
then run again with a new run and snapshot ID:

```bash
cao workflow approve <plan_id-from-the-refusal>

cao workflow run pr_health \
  --run-id pr-health-apply-2026-08-03 \
  --input repo=awslabs/cli-agent-orchestrator \
  --input as_of=2026-08-03 \
  --input snapshot_id=apply-2026-08-03 \
  --input importance_analysis=true \
  --input importance_provider=claude_code \
  --input importance_agent=reviewer \
  --input mode=apply \
  --json
```

Apply mode only posts eligible comments. Draft and close recommendations remain
advisory; maintainers must make any PR-status change separately. Decisions at a
lifetime cap are reported as `report_only` and are never commented.

`importance_provider` and `importance_agent` select the headless CAO reviewer
step used for advisory importance synthesis. They do not affect deterministic
scores or actions.

## Schedule every two weeks

Traditional cron expressions cannot represent a continuous 14-day interval
across month boundaries. The included flow runs every Monday and uses
[`pr_health_biweekly_guard.py`](pr_health_biweekly_guard.py) to execute only on
dates exactly divisible by 14 from `ANCHOR`.

1. Edit `REPOSITORY` and `ANCHOR` in the guard.
2. Choose either the dry-run template or the explicitly authorized apply
   template.
3. Copy the chosen flow and guard to a durable local directory.
4. Register the chosen flow.

```bash
mkdir -p ~/.cao/flows
install -m 0755 \
  examples/workflows/pr-health/pr_health_biweekly_guard.py \
  ~/.cao/flows/pr_health_biweekly_guard.py
install -m 0644 \
  examples/workflows/pr-health/pr-health-biweekly.md \
  ~/.cao/flows/pr-health-biweekly.md

cao schedule add ~/.cao/flows/pr-health-biweekly.md
cao schedule list
```

For an apply schedule, install and register
`pr-health-biweekly-apply.md` instead — or in addition, since the guard
differentiates each mode's `run_id` and `snapshot_id`. The template fixes the
repository via `[[repo]]` and authorizes no GitHub write except comments on
live-revalidated PR numbers from that repository.

CAO uses APScheduler weekday numbering, where `0` is Monday. The flow therefore
uses `0 9 * * 0` for Monday at 09:00 in the server's local timezone. The
`cao-server` process must remain running for scheduled flows to execute.

The guard derives `as_of` from the **UTC** date, not the server's local date,
because the scoring rules compare it against GitHub's UTC timestamps. A
local-date `as_of` would shift the 7/14/21-day threshold crossings by a day for
runs scheduled near midnight.

Manage the schedule with:

```bash
cao schedule disable pr-health-biweekly
cao schedule enable pr-health-biweekly
cao schedule remove pr-health-biweekly
```

## Files

- [`pr_health.py`](pr_health.py): deterministic workflow and comment-only notifications
- [`pr-health-biweekly.md`](pr-health-biweekly.md): non-mutating scheduled flow
- [`pr-health-biweekly-apply.md`](pr-health-biweekly-apply.md): explicitly
  authorized comment-only scheduled flow
- [`pr_health_biweekly_guard.py`](pr_health_biweekly_guard.py): exact 14-day gate

This example is a reference policy. Adjust thresholds and priority labels to
match the repository's contribution and maintainer policies before enabling
apply mode.
