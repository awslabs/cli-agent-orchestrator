# Launch decisions

CAO routes normal delegations to installed profiles through `model.route` when
the caller omits a model. Local and remote assign, elastic assign, handoff and
CLI handoff use it. Both decision points are **off by default**, so normal
launches keep their existing behavior until an operator enables routing.

Plain terminal creates and older clients that do not send the original task
message are not routed. Reusing a terminal and replaying a workflow step launch
nothing, so neither runs a decision. A script workflow's own step is out of
scope for deciders; in shadow or on it gets an `out_of_scope` record. An agent's
assign or handoff from inside that step remains an ordinary delegation.

## Points and states

| Point | Ordered options | Purpose |
| --- | --- | --- |
| `model.route` | `small`, `medium`, `large` | Choose a model size |
| `effort.route` | `low`, `medium`, `high` | Choose reasoning effort for a future spec consumer |

Each point also accepts `unsure`. `off` asks nothing and writes no record.
`shadow` records what the decider would choose without waiting for it or changing
the launch model. `on` waits up to the configured timeout, then uses a confident
answer or a deterministic fallback. An explicit caller value skips the decider.
The installed profile model is a fallback and may be overridden in on mode.

The fallback order is the installed profile default, the policy default, then
the provider default. A required policy tier must map to a model; otherwise the
launch is refused. Policy checks run before scope and decider calls. An explicit
value above a policy ceiling is refused; a decider answer above it is capped.
The launch-time policy check covers every checked provider in the envelope,
including providers other than the target. A missing mapping anywhere in that
set refuses the launch and names `model_tiers.<provider>.<tier>`.

## Delegation results

An eligible delegation in on mode adds a note to the assign, handoff or elastic
assign tool result. CLI handoff includes it when returning early with
`--no-wait` and when waiting on the terminal it just created.

| Result | Example note |
| --- | --- |
| Applied | `ran on model-small (auto: small 0.91)` |
| Capped by policy | `ran on model-medium (auto: large 0.91, capped to medium)` |
| Fallback | `ran on model-default (auto: fallback, unsure)` |
| Model cannot be vouched for | `ran on provider default (auto: fallback, not_honored)` |

The note names a model only when the bound terminal records it and the provider
honors it. Otherwise it says `provider default`. Fallback reasons are fixed
values, including timeout, error, unsure and an unmapped tier. Normal delegation
has no policy envelope, so capping is reserved for callers that supply one.

The server carries the note in the optional `X-CAO-Decision` response header,
not in the response body. Model characters outside the model-ID alphabet are
percent-encoded once, including spaces, non-ASCII and control characters. The
client displays the header verbatim and does not decode it. A header-building
error cannot fail a launch that has already succeeded.

Off, shadow, explicit-model calls, excluded profiles and older clients show no
note. Rejected launches return an error. A terminal-creation idempotent retry
launches nothing and shows no note. Terminal and sibling responses gain no
decision fields.

A decided Kiro model that fails the capability probe is retried with the model
that would have been used without routing. That fallback is probed before any
allocation, recorded as `fallback` / `not_honored`, and reflected in the note.
An unsupported explicit model still returns the existing error.

## Settings

Settings are read fresh for each call, except the shadow limits
`shadow.max_concurrent` and `shadow.max_pending`, which the server reads once at
startup. Point-state precedence is:

1. `CAO_DECISION_MODEL_ROUTE` and `CAO_DECISION_EFFORT_ROUTE` in the server's
   environment. A repeatable `cao-server --decision model.route=shadow` flag sets
   the matching variable for that server process, replacing an inherited value.
2. `decisions.points.<point>.state` in `settings.json`.
3. `off`.

`--decision` refuses an unknown point or state at startup. An invalid state in a
variable or in `settings.json` means off at that layer; it does not fall through.
An unreadable settings file makes every point off. `cao decisions status`
describes the CLI's environment and cannot see a running server's flags; the
server logs effective states at startup.

The server captures these overrides at startup and keeps them until it restarts.
While one is set, `cao decisions set <point> off` and `decisions_set_point` change
only `settings.json`: the running server keeps using the override for that point.
`cao decisions off` turns every point off on a running server, overrides included:
its halt marker is checked above the flag and environment layers (see External
deciders).

```json
{
  "decisions": {
    "points": {
      "model.route": {"state": "off", "decider": "fixed_table", "exclude_profiles": []},
      "effort.route": {"state": "off", "decider": "fixed_table"}
    },
    "on_timeout_ms": 1000,
    "confidence_threshold": 0.7,
    "retention_days": 90,
    "shadow": {"max_concurrent": 4, "max_pending": 64, "timeout_ms": 10000},
    "deciders": {
      "fixed_table": {
        "model.route": {"profiles": {"developer": "small"}, "roles": {"reviewer": "medium"}}
      }
    }
  },
  "model_tiers": {
    "codex": {"small": "model-small", "medium": "model-medium", "large": "model-large"}
  }
}
```

`model_tiers` is top level so explicit spec tiers can use it with all decisions
off. Keys are CAO provider IDs, options are the three model tiers, and values are
model IDs. Invalid mappings are ignored with a warning. Providers with no table
are not checked by configuration validation unless listed in `allowed_providers`;
the launch check still refuses an unmapped required default on the target.

`exclude_profiles` pins installed profiles to their existing model resolution:
no `model.route` question and no `model.route` record for launches that pass
policy. Other points, such as `effort.route`, are still asked and recorded.
Refused launches still write a rejected record in shadow or on. Names not
installed are retained but have no effect. There is no profile-frontmatter
switch for decisions.

`cao decisions tune` accepts an on timeout of 50–5000 ms, a threshold of 0–1 and
a retention of 1–36500 days, and refuses anything else. A retention of `0` is
refused; it does not mean unlimited. Values outside these ranges in
`settings.json` or in the scalar overrides `CAO_DECISION_ON_TIMEOUT_MS` and
`CAO_DECISION_CONFIDENCE_THRESHOLD` are clamped, with a warning. The fixed table
looks up profile first, then role, and returns probability 1.0; a miss gives no
answer. It is a decider, never part of fallback resolution. Shadow defaults to
four concurrent and 64 pending tasks, with a 10000 ms decider timeout. These have
no CLI setter; values in `settings.json` are clamped, with a warning, to 1–1024
concurrent, 0–100000 pending and 50–60000 ms. Overflow is recorded as `shadow_dropped`;
shutdown and crash recovery mark unfinished decisions interrupted.

## Operator controls

```bash
cao decisions status
cao decisions off
cao decisions enable <name>
cao decisions disable <name>
cao decisions show <name>
cao decisions set model.route shadow --decider fixed_table
cao decisions tier codex small model-small
cao decisions tier codex large --unset
cao decisions table model.route --profile developer small
cao decisions table effort.route --role developer medium
cao decisions exclude --add reviewer
cao decisions exclude --remove reviewer
cao decisions tune --on-timeout-ms 1000 --threshold 0.7 --retention-days 90
cao decisions list --point model.route --since 2026-01-01 --limit 100
cao decisions purge --before 2026-01-01
cao decisions purge --all --rotate-key
```

The operations stdio MCP server offers `decisions_status`, `decisions_off`, `decisions_set_point`,
`decisions_set_tier`, `decisions_set_exclusions`, `decisions_list` and
`decisions_purge`. These run locally in-process; there are no decision HTTP
routes. The agent-facing MCP server has no tools to read, change or ask for
decisions. A profile that grants operations MCP tools grants these controls too.

“Operator only” means not settable through MCP or any agent-facing API. It is a
same-user local control, not a privilege boundary: `settings.json` can be edited
by the same user, including an agent with shell access.
The same user can also install a decider package. Concurrent settings writers
can lose updates because the existing settings writer is not atomic.

## Records and telemetry

The node's SQLite `decision_records` table holds one record per point per
launch in shadow or on. Records include state, decider name/version, fallback
value/source, answer and probabilities, capped candidate, applied value, latency,
reason, terminal ID, launched model and whether it was honored. A policy model
fallback records the tier as `fallback_value`; the mapped model appears in
`launched_model` only when bound. Effort results are recorded but not carried in
a launch plan in this version. A launch is recorded as launched when the
provider has been built and its start has completed or been scheduled. A
deferred startup failure updates the record to `launch_failed` without emitting
a second decision span. A cancelled deferred start also moves a previously
launched row to `launch_failed`, with no second emission.
A launch refused by the creator's delegation ceiling is recorded as `not_launched`.

Records never hold task messages, descriptions, purposes, prompts or working directories.
Messages, descriptions and purposes are redacted before every decider call, including the
fixed table. Installed profile names and roles are bounded and redacted facts;
ephemeral targets send neither. The message hash is a keyed HMAC of the redacted
canonical message, with a key identifier; the hash is never sent to deciders.

The HMAC key is `decision-hash.key` in the database directory, mode 0600. Creation
publishes a fully written temporary file with a link that cannot replace an
existing key. A key file found with group or other access is restricted to 0600
when it is read; if that fails, the key is not used, no record is written and a
warning names the file. Restrict or remove the file to recover; removing it starts
a new key, as rotation does.
Rotation removes the key; the next writer creates a new one and live writers use
it without restarting. Purged records are deleted. Rotation breaks linkage to
future records; it cannot revoke keys already copied elsewhere. Rotate with no
launches in flight: a record written while the purge runs keeps a hash from the
old key and is not purged.
An exported record set without the key cannot confirm guessed messages. The
same user can read the key and database.

Retention defaults to 90 days, independent of terminal retention. A startup
sweep marks pending decision work interrupted and pending launches unknown;
rejected rows remain done/not-launched. Store errors are logged and do not fail
a launch. Rejected launches emit telemetry at insertion, shadow tasks emit after
deciding, and other records emit at the first bind. Each record emits once. A
shadow decision that finishes before its launch is bound emits without the
launched model or the honored flag; its record still gets both at bind.
Spans use `cao.decision`; metrics use `cao.decision.requests` and
`cao.decision.latency`. No message, description, hash or probability vector is
exported, and metric labels never contain terminal IDs. Without the
OpenTelemetry packages (the `[otel]` extra), decision telemetry is a no-op.

## Decider packages

An installed package registers a class under `cao.deciders`. Its entry-point
name must equal its declared name; the class declares a version and supported
points. CAO discovers names at startup and constructs a decider only when an
active point needs it. A decider package is trusted code, like any other
installed dependency: CAO imports and constructs its class before checking what
it declares. With every point off, no decider code runs. Failures are cached
until restart. `async decide` must
not block the event loop; every call has a timeout. External deciders must not
expose decision access through agent-tool registration hooks. Core bundles only
`fixed_table`; external deciders are optional installed packages.

## External deciders

An external decider sends nothing until it has its own consent. Consent, endpoint
disclosure and key handling belong to the decider package. Core redacts messages,
descriptions and purposes before every call; redaction removes common credential
shapes using patterns and is not a guarantee. Decision records hold no task content.
Review the package's disclosure to learn exactly what it sends and where it sends it.

CAO asks a decider once per eligible delegation request, before the server's own launch checks. A
request the server then refuses, for example because it exceeds the creator's delegation ceiling,
has still been sent; its records read `not_launched`.

External deciders must send only while holding
`cli_agent_orchestrator.decisions.egress.egress_permit(point, decider)`:

```python
async with egress_permit("model.route", "external") as permitted:
    if not permitted:
        return None
    # Check this package's consent before reading its key or sending a request.
    return await send_request()
```

The permit takes a non-blocking shared lock, then freshly checks the halt marker,
the effective point state and the decider binding. In the running server it uses
the same startup environment snapshot as the engine. It refuses if the lock cannot
be opened or acquired. Keep the permit until the request finishes or is cancelled;
never block the event loop. The lock file must not be removed or replaced while
requests may be running.

`cao decisions off` creates `decisions.halt`, writes every point off, then waits
for in-flight permit holders to finish. The wait is the configured shadow timeout
plus five seconds. A successful return requires the drain to finish, the halt
marker to remain present, and every returned point to be off. Every external
decider that follows the permit rule has then drained and cannot start another
request while the halt remains active. If a concurrent explicit change re-enables
decisions during off, the CLI exits 1 and operations MCP reports failure, asking
the operator to re-run `cao decisions off`. The command does not recreate the
marker over that newer action. A timeout produces a non-zero CLI exit code,
and the halt remains active. If settings cannot be read
or saved, the marker still switches every point off and the result reports
`settings_saved: false`. Fix the settings before re-enabling decisions.

The marker overrides settings, environment and startup flags. Setting any point
to `shadow` or `on` removes it and prints effective local states. This may also
re-enable other points whose environment or startup flags select `shadow` or `on`;
the CLI cannot see a running server's startup flags. `cao decisions status` shows
`halted` and installed deciders' names, distribution versions and `builtin` or
`external` classification without importing them. External entries carry the
label “sends data off this machine only after its own consent”.

A distribution providing the `cao.deciders` entry `<name>` can also provide the
console script `cao-decider-<name>`, targeting
`main(argv: Sequence[str] | None = None) -> int`. Core calls that function
in-process: `enable <name>` passes `["consent", *extra]`, `disable <name>` passes
`["revoke", *extra]`, and `show <name>` passes `["status", *extra]`. Additional
arguments are forwarded, and the script's returned exit code becomes the CLI exit
code. Use `-- --help` after the name to request the package's help. A decider with
no script, including `fixed_table`, reports “no operator commands for <name>” and
exits with code 2. Granting consent requires the package's interactive TTY command;
operations MCP can read status and turn everything off, but cannot grant consent.

Consent is a same-user local control, not a privilege boundary. CAO runs as you
and verifies nothing about who granted consent; a package's record of who granted
it is a provenance note, not an identity claim. Any process running as you,
including an agent with shell access, can edit `settings.json` or a package's
consent file and can read any key or endpoint the package stores. What keeps
agents out is each profile's tool restrictions, and two rules a package must
follow: no agent-facing tool may grant consent, and its consent command must
need an interactive terminal. CAO's own agent-facing tools offer no consent.

## Known limitations

- Deferred assignment reports placement and the scheduled model before provider initialization finishes. A startup failure can occur afterward; after abrupt server loss, restart recovery can record a terminal-side failure while the decision record retains its last launch status as launched.
- YAML workflow launches do not appear in decision records.
- Elastic decision records stay on the remote node and are lost with it.
- Effort routing has no normal delegation consumer and launch plans carry no effort.
- A decider that blocks the event loop cannot be preempted by an asynchronous timeout.
- Terminal records expose the launched model and whether it was honored, independently of decision state; decision fields stay in operator records.
- If a policy tightens between a launch and `create_terminal`'s own idempotent retry, the retry can be refused before replaying the existing terminal. This does not occur with an identity policy. Each replayed delegation create writes its own `not_launched` row; in shadow or on the decider is asked again.
- A direct API delegation to a reserved ephemeral name without a claim, carrying a per-call model, is refused at the delegation site with HTTP 400 "set the tier in the spec instead of a per-call model". The response names no reason code and no decision record is written. The ephemeral launch policy never sees the request, so it does not log the refusal either. Assign and handoff refuse reserved names before sending any request. A claimed launch skips the delegation site and is refused, and logged, by the ephemeral policy itself.
- A delegation is decided before the server applies the creator's delegation ceiling. A launch
  the ceiling refuses (403) has still been decided: in shadow or on, the decider was asked once
  with the redacted message, and in on mode the refusal waits for that answer. Its records are
  `not_launched` with no terminal ID. Local assign and handoff check the ceiling before sending,
  so this affects direct API calls, remote placement and installed-policy refusals.
