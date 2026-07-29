# Managed launch companion protocol

`cao-managed-launch-v1` is a fail-closed, two-phase task-delivery API for
orchestrators that need recovery across HTTP response loss or server restart.
It never falls back to ordinary terminal creation when the capability is absent
or the version differs.

## Sequence

1. `GET /managed-launch/capabilities` and require an exact protocol/capability
   match.
2. Generate a reservation UUID, persist caller intent, and
   `POST /managed-launch/reservations`. Re-query the same UUID after any
   ambiguous response; never allocate another UUID blindly.
3. `POST .../{reservation_id}/launch`. This atomically claims one no-task
   provider launch. A retry only returns the durable record.
4. Query until the exact generation has either a generation-bound readiness
   receipt or a terminal negative/preflight state. Process existence, pane text,
   and a generic `idle` status are not independent readiness evidence.
5. Persist the caller's delivery intent, then `POST .../{reservation_id}/admit`
   once. The delivery UUID and message digest are immutable. A response lost
   after the I/O-attempt record is recovery-only and is never blindly resent.

Successful admission includes a fork-authored provider submission receipt
bound to the reservation, delivery, terminal generation, route, sender, and
message digest, plus the BOOT/project/task/run and exact task/plan/dossier/
lease/command-packet/source-chain digests supplied at admission. A conductor
must persist that receipt rather than manufacture its own acknowledgement.

`POST .../reconcile` is read-only. `POST .../negative` and `POST .../cancel`
accept identity-bound evidence and cannot supersede an admission attempt.
`POST .../cleanup` is the only destructive recovery operation: it accepts the
exact terminal id and generation, persists `cleanup_intended`, deletes that
terminal, verifies its database record is absent, and returns durable cleanup
proof. It is rejected for ready, admitting, or admitted reservations.

`POST /managed-launch/attest-route` performs the same provider-native Codex or
Kimi probe without reserving a terminal or carrying task bytes. It exists for
bounded launch-breaker recovery and cannot submit work.

## Durable states

`reserved` is the only state from which launch I/O can be claimed. `launching`
means provider launch may have happened and is therefore recovery-only. `ready`
contains the authoritative receipt. `admitting` means task I/O may have crossed
the boundary; `admitted` confirms completion. `preflight_blocked`, `negative`,
and `cancelled` prohibit admission. `cleanup_intended` and `cleaned` are
recovery-only. Unknown or corrupt state fails closed.

All launch and admission claims use conditional database updates, so concurrent
server requests have exactly one I/O owner. Observations use compare-and-swap
appends so concurrent evidence is not lost.

## Codex trust and route proof

Managed Codex launches require `trusted_project_root` to equal the existing,
canonical `working_directory`. CAO renders the invocation-only override as the
exact inline table:

```text
projects={"/canonical/worktree"={trust_level="trusted"}}
```

Before terminal launch, a zero-turn app-server probe verifies the exact project
key has `sessionFlags` provenance, verifies the resolved model and reasoning
effort, starts no turn, and confirms `~/.codex/config.toml` is byte-unchanged.
This proof is version-bound to `codex-cli 0.146.0`; any other version is
rejected. If a trust prompt still appears, the managed provider sends zero
prompt keystrokes and records a preflight-blocked outcome.

## Kimi route proof

Managed Kimi launches run a zero-prompt ACP session before terminal launch.
The structured `configOptions` response must resolve the assigned model and
`thought_level` exactly. The proof is version-bound to an exact Kimi Code CLI
build — currently `0.29.1`, with `0.29.0` retained for already-minted sessions
(exact set, never a range) — confirms `~/.kimi/config.toml` is byte-unchanged,
and sends no `session/prompt`.
The terminal generation is then launched with the same model forced by
`--model` and the same effective effort forced by the invocation-only
`KIMI_MODEL_THINKING_EFFORT` environment value. The outer readiness receipt
binds the ACP receipt to the reserved terminal id and generation.

Only Codex and Kimi expose readiness adapters in v1. Other providers are
rejected as unsupported rather than downgraded to pane or footer inference.
