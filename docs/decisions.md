# Launch decisions

CAO provides a local decision package for choosing a model size and reasoning
effort. Both points are **off by default**. The package and its operator controls
are available, but delegation handlers do not yet call it. Changing a setting
in this version does not change how workers launch.

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

## Settings

Settings are read fresh for each call. Point-state precedence is:

1. Repeatable `cao-server --decision model.route=shadow` flags.
2. `CAO_DECISION_MODEL_ROUTE` and `CAO_DECISION_EFFORT_ROUTE`.
3. `decisions.points.<point>.state` in `settings.json`.
4. `off`.

An invalid state means off at that layer; it does not fall through. An unreadable
settings file makes every point off. Server environment overrides are captured
at startup. `cao decisions status` describes the CLI's environment and cannot
see a running server's flags; the server logs effective states at startup.

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
no question and no record for launches that pass policy. Refused launches still
write a rejected record in shadow or on. Names not installed are retained but
have no effect. There is no profile-frontmatter switch for decisions.

On timeout is clamped to 50–5000 ms; threshold to 0–1. Scalar overrides are
`CAO_DECISION_ON_TIMEOUT_MS` and `CAO_DECISION_CONFIDENCE_THRESHOLD`. The fixed table
looks up profile first, then role, and returns probability 1.0; a miss gives no
answer. It is a decider, never part of fallback resolution. Shadow defaults to
four concurrent and 64 pending tasks. Overflow is recorded as `shadow_dropped`;
shutdown and crash recovery mark unfinished decisions interrupted.

## Operator controls

```bash
cao decisions status
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

The operations stdio MCP server offers `decisions_status`, `decisions_set_point`,
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
a launch plan in this version.

Records never hold task messages, descriptions, purposes, prompts or working directories.
Messages, descriptions and purposes are redacted before every decider call, including the
fixed table. Installed profile names and roles are bounded and redacted facts;
ephemeral targets send neither. The message hash is a keyed HMAC of the redacted
canonical message, with a key identifier; the hash is never sent to deciders.

The HMAC key is `decision-hash.key` in the database directory, mode 0600. Creation
publishes a fully written temporary file with a link that cannot replace an
existing key. Rotation removes the key; the next writer creates a new one and
live writers use it without restarting. Purged records are deleted. Rotation
breaks linkage to future records; it cannot revoke keys already copied elsewhere.
An exported record set without the key cannot confirm guessed messages. The
same user can read the key and database.

Retention defaults to 90 days, independent of terminal retention. A startup
sweep marks pending decision work interrupted and pending launches unknown;
rejected rows remain done/not-launched. Store errors are logged and do not fail
a launch. Rejected launches emit telemetry at insertion, shadow tasks emit after
deciding, and other records emit at the first bind. Each record emits once.
Spans use `cao.decision`; metrics use `cao.decision.requests` and
`cao.decision.latency`. No message, description, hash or probability vector is
exported, and metric labels never contain terminal IDs.

## Decider packages

An installed package registers a class under `cao.deciders`. Its entry-point
name must equal its declared name; the class declares a version and supported
points. CAO discovers names at startup and constructs a decider only when an
active point needs it. Failures are cached until restart. `async decide` must
not block the event loop; every call has a timeout. External deciders must not
expose decision access through agent-tool registration hooks. This version includes only
`fixed_table` and makes no external calls.

## Known limitations

- Delegation wiring is not available yet; operator settings do not affect worker launches.
- YAML workflow launches do not appear in decision records.
- Elastic decision records stay on the remote node and are lost with it.
- Effort routing has no normal delegation consumer and launch plans carry no effort.
- A decider that blocks the event loop cannot be preempted by an asynchronous timeout.
- Terminal records expose the launched model and whether it was honored, independently of decision state; decision fields stay in operator records.
- If a policy tightens between a launch and `create_terminal`'s own idempotent retry, the retry can be refused before replaying the existing terminal. This does not occur with an identity policy.
