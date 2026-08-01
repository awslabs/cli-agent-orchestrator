# Pull Request Health Workflow

This example evaluates every open GitHub pull request with deterministic rules,
tracks degradation across runs, and produces an auditable Markdown and JSON
report. It also includes a CAO scheduled flow with an exact 14-day cadence.

The score is rule-based. The optional reviewer agent may summarize importance,
but it cannot change scores, categories, next-actor attribution, or actions. Its
prompt reads untrusted PR text (titles, bodies, comments), so treat its output as
advisory prose only — nothing it says can alter a score or trigger an action.

## Safety model

The workflow defaults to `dry_run` and makes no GitHub changes. Apply mode has
additional safeguards:

- A PR must score below 60 on two separate dated evaluations before an owner
  warning is eligible.
- A score at or below 50 can move a PR to draft only after a warning has been
  unanswered for at least seven days.
- A PR can become a closure candidate only after another 14 days without owner
  activity and a final score below 30.
- P0/P1 and approved PRs are escalated instead of drafted or closed.
- Closure requires the PR number in the explicit `close_allowlist`.
- Every candidate is fetched and scored again immediately before mutation.
- Only hidden markers in comments authored by the authenticated workflow
  identity can advance lifecycle stages or make actions idempotent.
- Each lifecycle stage notifies the owner at most once, ever. Idempotency keys
  on the marker's stage, not its text, so a repeat run cannot re-post a
  notification even though the marker embeds that run's score and date.
- Lifecycle progression does not depend on the run cadence. The
  furthest-advanced marker owns the PR's grace period, so a weekly, biweekly, or
  ad-hoc run reaches the same stage after the same elapsed time.
- A per-PR state problem (backfill, clock skew, a reopened PR carrying old
  state) restarts that PR's observation streak; it never aborts the run for the
  other PRs.
- Runs for the same repository are serialized with a local file lock.

The dry-run and apply schedules are separate templates. Registering the apply
template is a deliberate standing authorization for future comments and draft
changes. Both templates keep `close_allowlist` empty, so unattended runs cannot
close PRs. Both may be registered together: the guard emits mode-qualified run
and snapshot identifiers so they never collide on a shared due date.

> **Apply mode is a standing unattended write-grant.** Once registered, the
> apply schedule comments on and drafts other contributors' pull requests under
> the operator's `gh` identity, with no per-run review. Drafting someone's PR
> has real social impact. Trial the apply template against a fork you own before
> registering it against a shared repository, and read the dry-run report for at
> least one full cycle first.

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

## Apply eligible actions

Review the dry-run report first, then use a new run and snapshot ID:

```bash
cao workflow run pr_health \
  --run-id pr-health-apply-2026-08-03 \
  --input repo=awslabs/cli-agent-orchestrator \
  --input as_of=2026-08-03 \
  --input snapshot_id=apply-2026-08-03 \
  --input importance_analysis=true \
  --input importance_provider=claude_code \
  --input importance_agent=reviewer \
  --input mode=apply \
  --input close_allowlist= \
  --json
```

An empty `close_allowlist` permits eligible comments and draft transitions but
prevents closure. To approve specific closures, pass a comma-separated list such
as `--input close_allowlist=123,456`.

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
differentiates each mode's `run_id` and `snapshot_id`. Its empty closure
allowlist must remain empty for unattended operation.

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

- [`pr_health.py`](pr_health.py): deterministic workflow and guarded enforcement
- [`pr-health-biweekly.md`](pr-health-biweekly.md): non-mutating scheduled flow
- [`pr-health-biweekly-apply.md`](pr-health-biweekly-apply.md): explicitly
  authorized comment/draft scheduled flow
- [`pr_health_biweekly_guard.py`](pr_health_biweekly_guard.py): exact 14-day gate

This example is a reference policy. Adjust thresholds and priority labels to
match the repository's contribution and maintainer policies before enabling
apply mode.
