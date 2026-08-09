# Recovery control plane (fork foundation)

This document covers the fork-owned foundation of the recovery control
plane: the capability endpoint, managed-launch v2, the resource registry
and DB vintage surface, the actor broker, the containment mechanism
interface, the delivery journal, and the destructive endpoint.

**Status: foundation only, fail-closed.** The recovery *execution* lane
is blocked: the privileged containment mechanism (PF-1a feasibility,
PF-1b production composition), the provider model-input-bound route
receipt (PF-2), and protected-path denial (PF-3) are all **RED**. No
containment artifact has been human-authorized. Every dependent path —
report acceptance/finalization, automated resume, destructive cleanup
that relies on confinement, confinement-dependent admission, and
stronger-route authority — stays preserved/alert-only. The one behavior
usable now is heartbeat-driven **deletion suppression**, because it only
ever *prevents* destructive action.

## Capability endpoint

`GET /managed/recovery-capabilities` returns the single truthful
negotiation surface:

- `containment`: `proven` only against a live, digest-bound proof
  receipt from the human-authorized artifact; otherwise `unproven`
  (always, today).
- `observed_route`: per provider. No pinned provider (Codex 0.146.0,
  Claude Code 2.1.220, Kimi 0.29.1/0.29.0) emits a model-input-bound non-echo
  receipt carrying resolved model and effective effort, so Codex and
  Claude report `unsupported` and Kimi reports `unproven`. Provider
  echo, manifest requests, TUI/footer state, logs, and client-local
  configuration are never reported as provider-observed proof.
- `resume`: per provider, `identity_available` vs `authority_supported`.
  Codex and Claude have resumable identity but no authority; Kimi
  identity is disabled until the installed-CLI ACP
  `session/new` → kill → `session/load` proof passes.
- `heartbeat`, `fence`, `actor_broker`, `delivery_journal`,
  `resource_registry_version`, `receipts`: surface descriptors.

Absence or an unknown field means fail closed for every consumer.

## Managed-launch v2

Protocol `cao-managed-launch-v2`, endpoints under `/managed-launch/v2/`:
`reserve → launching → bound → admitting → admitted`. v2 is a distinct
protocol vintage: v1 reservations never gain v2 semantics, and v2 rows
live in the isolated `managed_launch_v2_reservations` table, so every
v1 query and deleter has zero visibility into them.

- **Zero-task launch** (`POST .../launch`): spawns the managed provider
  bridge with no task bytes, exactly as v1.
- **Native bind** (`POST .../bind`): journals the provider-native
  pre-turn identity receipts (creation + binding payload digests per
  the canonical receipt schemas), publishes the fork-owned binding
  record, and issues the producer fencing token. A crash anywhere
  before bind yields zero task bytes by construction.
- **Admit** (`POST .../admit`): requires the journaled `native_bound`
  digest and an open W13 fence; ambiguous submission is preserved,
  never replayed.
- **Resume** (`POST .../resume`): refused (45,
  prior-generation-unproven) while the containment composition is
  unproven; the refusal fact is journaled.
- **Fence** (`POST /managed-launch/v2/fence`): the W13 fence-install
  RPC. `fenced`/`already-fenced` are the only success outcomes and are
  idempotent on `intent_id`; a sealed generation rejects every
  post-fence input/tool admission at the bridge boundary.
- **Park** (`POST /managed-launch/v2/park`): M3's immutable retained-round
  operation. The request binds its operation/reservation, terminal and exact
  generation, logical task/round, obligation/attempt, and report digest. The
  fork persists the receipt under the same generation lock and adopts a
  compatible W13 receipt rather than clearing or replacing it. A lost reply is
  reconciled read-only at `GET /managed-launch/v2/park/{terminal}/{generation}/{operation}`;
  a changed intent is a conflict, while an exact retry returns the original
  receipt even after a successor exists. All provider-byte lanes (native and
  ACP admission, follow-up, native control/operator input, and native inbox)
  hold that same lock through their final check and actual submission.

Provider contracts are pinned: Codex resume is exactly
`codex resume <id>` / `codex exec resume <id>`; Kimi is
`--session <id>` / `-r <id>`; Claude is `--resume <uuid>`.
`--continue`, `--last`, newest-session shortcuts, `--ephemeral`,
`--fork-session`, and `--no-session-persistence` are refused. Installed
version drift fails closed.

## Heartbeat producer

The schema-2 heartbeat record lives at
`<COMPANION_DIR>/<terminal_id>/<generation>/heartbeat.json` (0600),
written through the P-MUT fenced CAS under the per-generation lock.
Writes require the currently registered producer fencing token (UUID +
strictly increasing fence number); a superseded producer is refused at
the compare step. Beats are emitted on provider-native activity only
(app-server events, ACP updates, Claude hook events), coalesced to one
durable write per 20 s while a turn is active, with a final
`turn.state=terminal` beat on turn-terminal events. No secrets, prompt
text, or transcript content is stored; the launch nonce appears only as
a digest. A missing callback can never delete an actively heartbeating
generation; missing/malformed/wrong-identity records are unknown ⇒
alert-only, never death.

## Resource registry and DB vintage surface

The code-owned registry (`services/resource_registry.py`, one
owner-only SQLite DB per side, `registry_schema_version = 1`) is the
mandatory path for generation resource constructors, lookups, monitors,
cleanups, and deleters. The lifecycle is journal-first:
`declared → created → active → draining → closed → deleted`, with
`aborted` lawful only on a verified-absence receipt. Owned desired
identities embed the `entry_id` so a crash between physical creation
and observed-ID capture is recoverable by discovery; every mutation is
a `state_seq` CAS journaled in `resource_event`; drains run in reverse
dependency order. The isolated v2 DB table is the vintage surface:
pre-existing rows are immutable v1 by absence, and rollback refuses
until a full v2 drain.

## Actor broker

`services/actor_broker.py` issues one-use, short-lived, HMAC-signed
assertions binding the report digest/path, project/task/run/obligation/
attempt, terminal generation, native session, launch nonce digest, and
route-chain head. Issuance requires kernel peer identity
(`LOCAL_PEERCRED`/`LOCAL_PEERPID` on macOS, `SO_PEERCRED` on Linux) and
live provider-tree lineage; same-UID collectors, reconcilers, and
siblings are refused. The signing key is memory-only. Platform
inability is `actor-unavailable` — provenance fails closed.

## Containment mechanism interface

`services/containment.py` is the production-composition interface for
the selected architecture (macOS Endpoint Security auth client + signed
manager daemon), whose artifact is built and proven in the separately
human-authorized `cao-containment-ext` companion lane — not here. With
no authorization and no live digest-bound proof receipt, `status()` is
always `unproven` and every containment-dependent operation refuses.
This module does not pretend the artifact or any PF proof exists.

## Delivery journal

`services/delivery_journal.py` records honest transport/submission
milestones: `accepted → terminal_queued → submitted →
submit-acked | submit-ambiguous → consumer-acked`. It never claims
exactly-once provider submission: the accept-then-bridge-death window
is `submit-ambiguous`, terminal for automated handling, resolved by
consumer/human reconciliation against the logical callback identity —
never by blind re-submission.

## Conditional destructive endpoint

`POST /managed/destructive` (service:
`services/destructive_endpoint.py`) is the single endpoint for fork
destructive effects. Under the per-generation lock it consumes the
single-use intent id, verifies the exact binding set against the
fork-owned binding record, and refuses with zero mutation when the
generation's heartbeat reads ACTIVE (or is malformed/wrong-identity) or
any binding mismatches. Effects that require the containment
composition refuse while containment is unproven. A missing callback
never deletes an actively heartbeating generation.
