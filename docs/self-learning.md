# Self-Learning

CAO's self-learning loop lets a multi-agent workflow improve as it runs
repeatedly: agents report **outcomes** of their work, a **retrospector** agent
distills those outcomes into durable memory **lessons**, and — behind a second,
stricter gate — reinforced lessons are **promoted** into agent profile files so
every future session starts with them.

Everything in this document is **opt-in and off by default**. A CAO deployment
that never touches the flags behaves identically to one without the feature.

## The loop at a glance

```
worker/supervisor ──report_outcome (MCP)──▶ workflow_outcomes (SQLite)
                                                   │
session/package completes                          ▼
supervisor ──handoff──▶ retrospector ──reads──▶ list_outcomes (MCP)
                            │
                            └──store_lesson(target_agent_profile)──▶
                               the WORKER's agent-scope memory lessons
                                                   │
lessons recalled in later sessions                 │ (each recall bumps
(injected via <cao-memory> / CLAUDE.md)            │  access_count)
                                                   ▼
operator ──cao memory promote <agent> --apply──▶ "## Learned Patterns" block
                                                 in the agent's profile .md
```

Three tiers, three risk levels:

1. **Outcome capture** (Phase 1) — cheap, content-free records of what
   happened. Signal only.
2. **Lesson storage** — ordinary agent-scope memories written by the
   retrospector; advisory context, forgettable, lint-checked like any other
   memory (see [Memory](memory.md)).
3. **Instruction promotion** (Phase 2) — mutates profile markdown shared by
   every session. Highest risk, strictest gate, dry-run by default.

## Feature flags

The flags nest: **promotion ⊂ learning ⊂ memory**. A disabled parent forces
every child off regardless of the child's setting.

| Setting (settings.json `memory` section) | Default | Env override | Gates |
|---|---|---|---|
| `enabled` | `true` | `CAO_MEMORY_ENABLED` | the whole memory subsystem |
| `learning_enabled` | **`false`** | `CAO_MEMORY_LEARNING_ENABLED` | outcome capture (`report_outcome`, `/outcomes`) |
| `instruction_promotion_enabled` | **`false`** | `CAO_MEMORY_INSTRUCTION_PROMOTION_ENABLED` | profile mutation (`cao memory promote --apply`) |

Enable learning:

```bash
# settings.json
{ "memory": { "learning_enabled": true } }

# or per-process
export CAO_MEMORY_LEARNING_ENABLED=true
```

Disabled behavior (mirrors the memory subsystem's idiom):

- **Writes fail loud, before validation** — `record_outcome()` raises
  `LearningDisabledError`; `PromotionService.apply()` raises
  `PromotionDisabledError`. Nothing is ever partially written.
- **Reads fail silent** — `list_outcomes()` returns `[]`; `GET /outcomes`
  and `POST /outcomes` return 404; the `report_outcome` MCP tool returns a
  `{"disabled": true}` payload the agent can surface and move on from.
- **Errors fail closed, but say so** — an unreadable settings file leaves both
  learning flags `false` (opt-in features fail closed; deliberately opposite to
  `memory.enabled`, which fails open because it is opt-out). Failing closed is
  not the same as *reporting* "disabled", though: `GET`/`POST /outcomes` answer
  **503** when `settings.json` exists but cannot be read, and the MCP tools turn
  that into a plain `error` with **no `disabled` key**. The distinction is
  load-bearing — agents are told to skip a `disabled: true` payload silently, so
  labelling a permissions fault "disabled" is how a broken data directory comes
  to look like a deliberate opt-out. `GET /settings/memory` reports
  `settings_readable` for the same reason.
- **An unreachable server is not "disabled" either** — `report_outcome` and
  `list_outcomes` reach cao-server over HTTP, so a down backplane returns
  `{"success": false, "error": "Failed to connect to cao-server…"}`, never a
  disabled payload.

## Phase 1 — outcome capture

An **outcome** is one row describing one unit of agent work: a workflow step,
a package conversion, a review round.

| Field | Notes |
|---|---|
| `task_label` | short label, e.g. `"convert package CustomerETL (iteration 2)"` (≤200 chars) |
| `success` | boolean |
| `score` | optional 0–100 quality metric (e.g. a benchmark score) |
| `friction_notes` | 1–3 sentences on what was hard or wrong — **conclusions only, never transcripts/logs/file contents** (≤1000 chars) |
| `workflow_name`, `agent_profile` | grouping labels; `agent_profile` defaults from the calling terminal |
| `session_name`, `source_terminal_id` | resolved automatically from the caller's `CAO_TERMINAL_ID` |

Surfaces:

- **MCP tools** — `report_outcome` (how agents report; session and agent
  profile resolve automatically from the calling terminal) and
  `list_outcomes` (how the retrospector reads them back, newest first,
  defaulting to the caller's session).
- **HTTP** `POST /outcomes` (write-scope gated) and
  `GET /outcomes?session_name=&agent_profile=&workflow_name=&limit=`
  (read-scope gated) — the operator surface.
- Storage: `workflow_outcomes` table in CAO's SQLite DB, indexed by
  `(session_name, created_at)` and `(agent_profile, created_at)`.

### Wiring a workflow for learning

Two SOP additions make a workflow learn (see
`agent_store/retrospector.md` and the worked example below):

1. **Supervisors report outcomes** after each meaningful step:

   > 8. **Report the outcome** — call the `report_outcome` MCP tool:
   >    `task_label="convert package <name> (iteration N)"`,
   >    `success=<validator PASS/FAIL>`, `workflow_name="ssis-migration"`,
   >    `agent_profile="transformer"`,
   >    `friction_notes=<1-2 sentence root cause, or "" on pass>`
   >
   > If the tool returns `disabled: true`, skip silently and continue —
   > learning is off for this run and that is expected.

2. **Supervisors hand off to the retrospector** after each work item:

   > 18. **Handoff to retrospector** — message: "Retrospect on session
   >     <session_name>, workflow <name>, package <item>. Agents involved:
   >     transformer, improver." Record its one-line summary in the run log.

Workers additionally get a `## Memory` section telling them to apply
injected lessons and store new ones (the [cao-learning skill](#the-cao-learning-skill)
packages this guidance).

## The retrospector agent

`agent_store/retrospector.md` is a built-in single-purpose profile, patterned
on `memory_manager`: it reads the session's outcomes (`list_outcomes`),
checks which lessons already exist (`memory_recall`), and distills **0–3
lessons per retrospection** via `store_lesson`.

`store_lesson` exists because `memory_store` resolves agent scope from the
**calling** terminal's profile — a retrospector using it would file every
lesson under `retrospector`. `store_lesson` takes a required
`target_agent_profile` and stores into **that worker's** agent scope
(type fixed to `feedback`), so the worker's future sessions and
`cao memory promote <worker>` actually find the lessons. Provenance fields
still record the retrospector as the writer.

Cross-agent writes are **authorized server-side**: the caller's profile —
resolved from its terminal record, never from tool arguments — must declare
the `store_lesson` capability in its frontmatter (the built-in retrospector
does; ordinary worker profiles must not). Without it, a worker calling
`store_lesson(target_agent_profile="reviewer", ...)` is refused — otherwise
any agent could inject permanent feedback into any other agent's future
sessions. Writing to one's *own* scope needs no capability (it grants
nothing beyond `memory_store(scope="agent")`), and a caller whose terminal
context cannot be resolved is refused outright. `list_outcomes` likewise
fails closed: without an explicit `session_name` it requires the caller's
own session and never falls back to a cross-session listing.

Lesson contract (enforced by the profile's rules):

- **Agent scope** (`scope="agent"`) for craft lessons; project scope for
  facts about the codebase. Never global/federated.
- **Type `feedback`** for hard-won corrections (permanent), `project` for
  project facts.
- **1–2 sentences ending with `Applies when: <trigger>`** — the trigger
  clause is what lets curators match the lesson against future tasks.
- Learn from failures **and** successes; cite the task label; store nothing
  when no general pattern emerged ("no lessons" is a valid outcome).
- Never store transcripts, logs, stack traces, or secrets.

Because lessons are ordinary memories, everything in [Memory](memory.md)
applies to them: injection into new sessions, recall (which bumps
`access_count` — the reinforcement signal), `cao memory lint` contradiction
checks, retention, and the audit log.

## Phase 2 — instruction promotion

Promotion moves *reinforced* lessons out of memory and into the agent's
profile markdown, inside a delimited block:

```markdown
<!-- cao-learned:begin -->
## Learned Patterns
<!-- lesson: honor-lookup-cache-mode -->
- Preserve a Lookup transform's cache mode (full, partial, or no cache) ...
  Applies when: translating a Lookup transformation whose CacheType is not full cache.
<!-- cao-learned:end -->
```

### The promotion gate

A lesson is **eligible** when all of:

- agent scope, keyed to the target profile;
- type `feedback` or `project` (the memory **type label** — promotion only
  ever draws from *agent-scope* memories; project-*scope* memories are not
  promotable);
- `access_count >= 3` (configurable via `--min-recalls`) — i.e. it was
  *recalled* at least 3 times after storage. Recall frequency is the
  reinforcement signal: every recall means an agent or curator found the
  lesson relevant again;
- ≤ 400 characters (compact it first if longer).

### Promotion, recall, and retention

Promotion **copies, never moves**. The backing memory stays in the wiki and
continues to participate in recall and injection unchanged; the promoted
text additionally becomes part of the profile itself — present in **every**
session deterministically, without competing for the injection budget or
needing a `memory_recall`. Re-running `promote` never proposes an
already-promoted lesson again unless its memory text changed, in which case
it proposes an **update** to the same block item.

Promoted lessons are **not subject to memory retention**: the profile block
is a file, not a memory, so `cleanup_service` never touches it — once
promoted, a learning survives even if the backing memory were later
forgotten. (In practice the backing memories cannot expire either: agent
scope has no retention window, and `feedback`-type memories are permanent
regardless of scope.) Removing a promoted lesson is always an explicit
operation — a remove delta or editing the profile — never a timer.

### Safety design

> **⚠️ Security: promoted lessons are untrusted instructions.** Lesson text
> is written by agents from agent-observed outcomes. Promotion validates
> length and rejects marker injection, but it does **not** — and cannot —
> validate lesson *content* for adversarial or subtly wrong instructions.
> `cao memory promote --apply` elevates that text into standing profile
> instructions that every future session of the agent obeys. Treat every
> promote diff as an **untrusted-instruction change**: review it with the
> same scrutiny as a pull request that edits an agent's system prompt,
> before applying. Never wire `--apply` into an unattended pipeline.

- **Dry-run by default.** `cao memory promote <agent>` prints the plan;
  only `--apply` mutates, and only when `instruction_promotion_enabled` is
  true. (Mirrors `cao memory heal`.)
- **Itemized deltas, never rewrites.** Each lesson is an addressable
  `<!-- lesson: key -->` item; promotion adds/updates/removes individual
  items and never regenerates the block wholesale — this prevents the
  brevity-bias / context-collapse failure modes of iterative full rewrites.
- **Everything outside the delimited block is preserved byte-for-byte**;
  writes are atomic (temp file + rename); marker injection through lesson
  text is rejected; a corrupted/unclosed marker drops only the marker token,
  never user content.
- **Caps:** max 10 lessons per profile, 400 chars per lesson. At the cap,
  new adds are *skipped* (reported), never silently evicted — removal is
  always an explicit operation.
- **Only writable stores.** Promotion targets profiles in your configured
  agent directories; built-in package profiles are refused on both the
  default lookup and the explicit `--profile-path` route (copy them into
  an agent dir first). Profile files must already exist.
- **Audited.** Every apply writes a content-free entry (profile + lesson
  keys) to the memory audit log.
- **Reviewable and revertible.** Profiles are files — keep them in git,
  review the promote diff like any change, `git checkout` to revert. If a
  metric regresses after a promotion, revert the profile and
  `memory_forget` the lesson.

### CLI

```bash
# Inspect what would be promoted (dry run, always allowed)
cao memory promote transformer

# Apply (requires instruction_promotion_enabled=true)
cao memory promote transformer --apply

# Options
cao memory promote transformer --min-recalls 5 --profile-path ~/agents/transformer.md
```

## The cao-learning skill

The `cao-learning` skill (`skills/cao-learning/SKILL.md`) packages the agent
habits: supervisors learn when to `report_outcome` and how to dispatch the
retrospector; workers learn to apply injected lessons and store new ones with
`Applies when:` triggers. Add it to profiles the way `cao-memory` is added.

## Recommended operating pattern (A/B-validated)

The loop was validated with a 20-package controlled A/B experiment (identical
tasks, learning on vs off; see `docs/self-learning-validation.md`). The
pattern that worked:

1. Enable `learning_enabled` for repeated production-style workflows; leave
   it off for one-off/debug sessions (junk lessons pollute agent scope).
2. Let lessons accumulate and reinforce for a batch of work items
   (~10 items) before promoting anything.
3. Promote **between batches**, dry-run first, and review the diff. Keep
   `instruction_promotion_enabled` off except at that moment (or permanently,
   promoting manually).
4. Use the *next* batch's metrics as the promotion's eval; revert on
   regression.
5. Run `cao memory lint` between batches — contradiction or poison findings
   mean the retrospector is storing noise.

Measured result (LLM-judged proxy task, median-of-3 scoring): on work items
where the baseline struggled, the learning arm won 6/6 with a mean +11-point
gain (sign test p = 0.016); on items the baseline already handled well the
effect was neutral (−0.1). Cost: roughly 2× worker latency from the injected
lesson context — worth it where the alternative is repeated fix iterations.

## Limitations

- **No automatic session-end trigger.** Retrospection runs when a supervisor
  hands off to the retrospector; nothing fires it automatically on
  `post_kill_session` today.
- **The recall-count gate is a signal-free heuristic.** Where a workflow has
  a real fitness metric (validator scores, benchmarks), gate promotions on
  it externally — CAO only checks reinforcement, not "did quality improve".
- **Lessons cost tokens.** Injected context grows with the lesson store;
  prefer curated injection (`memory_manager`) for latency-sensitive setups.

## See also

- [Memory](memory.md) — scopes, injection, lint/heal, audit log
- [Configuration](configuration.md#memory-memory) — all `memory.*` settings
- [API](api.md) — `/outcomes` endpoints
- `docs/self-learning-validation.md` — the A/B experiment write-up
