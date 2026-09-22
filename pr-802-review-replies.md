# Replies to the second review round on #802

The PAT here cannot write to the PR (`POST /pulls/802/comments/{id}/replies` → 403
`Resource not accessible by personal access token`), so these are paste-ready.

---

## Reply to `src/cli_agent_orchestrator/services/server_owner.py:125`

(comment id `4063461444`)

Fixed in `179b3aee`. Claims are now keyed by resolved path rather than held in a single
slot: re-entry increments only for the directory already held, and a different
`lock_path` acquires a real `flock` of its own (resolved, so `./x` and `x` are still one
claim and cannot self-deadlock). `release_server_ownership` takes the path it is
releasing, so freeing one state directory leaves the other held.

Pinned by four tests in `TestReentryIsPerStateDirectory`, including one that starts a
genuinely separate OS process against the second directory and requires it to be refused
— under the shared-slot refcount that child took the lock happily. Mutation-checked:
restoring the "any claim" lookup fails two of them.

---

## Reply to `src/cli_agent_orchestrator/runtime_channel/api.py:230`

(comment id `4063461540`)

Half-accepted, and the half I declined is worth stating explicitly.

You are right that the handler discarded the watermarks and that nothing acted on the
only signal carrying them. But persisting the advertised `end_pos` as the resume position
is the one thing this must not do. That position means *bytes the server has actually
received*. The undelivered bytes are still in the runtime's replay buffer —
`Bridge._send` swallows `ConnectionClosed`, which is exactly how a chunk goes missing
while the buffer keeps it — so the stale watermark is what makes the next reconnect
replay them. Advancing it would mark unreceived output as received and discard the only
path that recovers it: a recoverable shortfall becomes silent loss.

On "will request the same replay/gap after restart": requesting that replay is the
recovery working. The genuine loop you are pointing at was that a *bounded* `GapFrame`
did not advance the watermark either, so a reconnect re-requested an already-lost range
forever. That is fixed separately in `bca38fd4` — a bounded gap now consumes the range,
because the runtime is the only party that can say the bytes are gone.

So the heartbeat fix (`179b3aee`) states the discrepancy and leaves the watermark alone:
a shortfall that survives two consecutive heartbeats without progress is logged with the
exact byte range. One observation is not enough, because a reconnect's replay is a stream
of chunks a heartbeat can interleave with, and a warning that fires during ordinary
recovery is one operators learn to ignore.

Seven tests in `TestAHeartbeatWatermarkTheServerIsBehind`, and the alternative is
mutation-checked in both directions: adding `record_position` on heartbeat fails three of
them (including the reconnect that must still ask for the missing bytes), and reporting on
the first observation fails the two that pin the silence.

---

# Replies to the third review round on #802

All four fixed in `4703a0b7`.

---

## Reply to `src/cli_agent_orchestrator/runtime_channel/replay_buffer.py:127`

(comment id `4065003194`)

Correct, and the scenario is the one that matters: the live `GapFrame` is emitted exactly
when the channel is least likely to carry it, so "reported once" is not reported at all.

`replay_from` now returns gaps and bytes **interleaved in stream order** rather than one
leading `GapInfo` plus a chunk list. Chunk starts are the authority, so an interior hole
needs no extra retained marker — only to be looked for: any chunk that does not start
where the previous one ended is a gap, and the resume position is compared against the
running end rather than against `window_start` alone.

The ordering is part of the fix, not a style choice. Sending every gap first and the bytes
afterwards would advance the server's watermark past chunks it has not received yet, so a
reconnect that died mid-replay would leave it claiming bytes that were never delivered —
the same silent loss, one layer up. `Bridge` now walks the returned items and emits each as
it comes.

Three new tests in `test_runtime_channel_output_continuity.py`: the interior hole
re-reported on a resume inside the window, two holes coming back in stream order, and a
mid-chunk resume after a hole still trimming correctly.

---

## Reply to `src/cli_agent_orchestrator/utils/remote_attach.py:47`

(comment id `4065003228`)

Fixed, both halves. The native client now sends `Authorization: Bearer` via
`additional_headers` and the URL carries no credential at all — it is the one caller that
*can* set a header, so it should never have used the browser's concession.

The redaction gap is worth closing too, because the viewer has no alternative: `token` is
now in `_CREDENTIAL_PARAMS` alongside `access_token` and `ticket`. A token named
differently is no less replayable.

Tests assert the token is absent from the URL and present as a header, that no token sends
no header, and that `?token=` is scrubbed from an access-log line (with a companion test
that the overlapping `access_token` still redacts whole).

---

## Reply to `src/cli_agent_orchestrator/providers/claude_code.py:490`

(comment id `4065003138`)

Fixed — `write_owner_only`, the same writer the other credential-bearing configs use. It
creates the temp file at 0600 by construction (`mkstemp`, not a `chmod` afterwards) and
publishes with `os.replace`, so the bytes are never reachable at a wider mode and a
pre-existing 0644 inode is replaced rather than inherited. The deterministic
`{terminal_id}.mcp.json` name makes that second case reachable in practice, not just in
principle.

Two tests: a fresh file is 0600, and a pre-seeded 0644 file at that path ends up 0600 with
the new content.

For completeness, since the same pattern appears elsewhere: MiniMax's `servers.mcp.json`
is written into a data directory that is `rmtree`'d and recreated at `mode=0o700`
immediately before, so there is no pre-existing inode and no other account can traverse to
it. Left unchanged deliberately.

---

## Reply to `src/cli_agent_orchestrator/services/status_monitor.py:240`

(comment id `4065003268`)

Correct. `get_provider` overloads `ValueError` across two unrelated situations and the
guard could not tell them apart.

The missing row now raises `TerminalNotFoundError`, a `ValueError` **subclass** — so every
other caller that catches `ValueError` is unchanged — and `_process_chunk` catches only
that. A provider that refused to be built propagates to the caller's handler, which logs
it with a traceback.

New test pins the distinction: a `ValueError("Unknown provider type: typo_cli")` now
escapes `_process_chunk` rather than being swallowed, and the missing-row case stays quiet.

# Replies to the fifth/sixth review rounds on #802

Fifteen more comments across a Copilot pass and a detailed review by guojing1217
(several reproduced end to end on EKS). All fixed; commits noted per reply. As
before, the PAT here cannot post to the PR, so these are paste-ready.

---

## Reply to the terminal-ownership hijack — hello + frame handlers

(comment ids `4068664335`, `4068551547`, `4069096785`, and the metadata half `4065995057`)

Fixed in `b298a8a5`. You are right on every point, including the flap-back on the
real owner's next heartbeat — that was the tell that nothing adjudicated the claim.

`CAO_RUNTIME_TOKEN` is one fleet-wide secret, so a connected runtime is only ever
proven to be *some* authorized executor, never the one that owns a given terminal.
The new `registry.claim_terminal` is the fence every inbound bind now goes through
(hello resume, `StreamFrame`, `EventFrame`, `HeartbeatFrame`): it binds only when
the terminal is already this runtime's (a continuation), or is unbound and the
durable central row either names this runtime or does not exist yet (restart
recovery). Otherwise it refuses, logs both ids, and leaves the binding untouched —
and the hello path drops the refused entry from `resume`, so a claiming runtime is
never handed the stream position of a terminal it never launched. The flap is gone
because a refused claim does not rebind.

On the metadata vector you flagged alongside it: `update_terminal_metadata` now
preserves the server-owned `runtime_id` across an agent's whole-dict replace, so
the placement the fence reads cannot be cleared or repointed through the
`update_metadata` tool. Moving it to a dedicated column is the cleaner end state
and is worth a follow-up; preserving the key closes the hole without a migration.

Tests: registry-level fence (unbound-elsewhere refused, owner reclaims after
restart, a bound terminal is not stealable, no-row is claimable, no flap under an
imposter speaking last), a channel-level replay of the reproduced hello hijack
(resume drops the foreign claim, routing stays with the owner), and two database
tests pinning `runtime_id` preservation.

---

## Reply to `src/cli_agent_orchestrator/api/main.py:3982` — 503 wrapped as 500

(comment id `4068649397`)

Fixed in `aa202634`. Confirmed exactly as you measured: only `delete_terminal`
re-raised `HTTPException`, so `send_terminal_input`/`key` and `get_terminal_output`
caught the 503 in their bare `except Exception` and rewrapped it as a 500 with the
status stringified into the detail.

`except HTTPException: raise` now sits ahead of each catch-all, so all four remote
arms agree and the PR body's "a disconnected runtime is an explicit 503" is true.
New test asserts 503 (not a 500-wrapping-503) on input, key and output for a
disconnected runtime.

---

## Reply to `src/cli_agent_orchestrator/security/auth.py:344` — issuer fallback

(comment id `4065995103`)

Fixed in `d373dba1`. `extract_principal_from_token` now requires a non-empty
verified `iss`, fail-closed like the `sub` check, rather than defaulting to
`LOCAL_ISSUER`.

One precision for the record: `get_authorization_servers` derives an issuer from
any configured IdP — including the origin of a bare `CAO_AUTH_JWKS_URI` — so
`_verify_token` already pins `iss` whenever auth is on, which made the
`or LOCAL_ISSUER` fallback effectively unreachable. The change removes the
reliance on that derivation and makes the owner path fail closed by construction.

---

## Reply to `src/cli_agent_orchestrator/utils/terminal.py:202` — placement fails open

(comment id `4065995141`)

Fixed in `d775d421`. `_placement_from_the_central_row` returned `None` both for a
confirmed-local terminal and for a transient read failure, so `is_remote` took the
local arm on a DB blip and drove this host's tmux.

The lookup now raises `PlacementUnavailableError` on a read failure, distinct from
the `None` that means confirmed-local. `is_remote` fails closed to `True` on it (a
retryable "not connected" / UNKNOWN beats addressing the wrong local pane — during
a DB outage the row is unreadable anyway), `runtime_for_terminal` reports the
runtime as unknown rather than local, and `claim_terminal` refuses a claim it
cannot adjudicate. The failure is never cached, so a later read still learns the
truth. The exception is contained in the registry, so `is_remote`/`runtime_for_terminal`
keep their existing signatures and no caller changes.

---

## Reply to `src/cli_agent_orchestrator/runtime_channel/api.py:424` — engine not persisted

(comment id `4068551561`)

Fixed in `94d6538b`. `body.engine` is now passed to `db_create_terminal` (runtime
echo preferred, request as fallback) and returned on the `Terminal`, so reuse
validation and the KAS input gate read the engine that was actually launched.

You were right about the knock-on too: `flow_service` still refused non-default
remote engines on the "LAUNCH cannot carry engine" grounds, which stopped being
true when the field was added to `CreateRemoteTerminalBody`. That refusal is gone
and the engine is forwarded. Test: a `kas` flow now launches remotely with the
engine set instead of being refused.

---

## Reply to `src/cli_agent_orchestrator/runtime_channel/api.py:421` — leaked pod on persist failure

(comment id `4068551557`)

Fixed in `94d6538b`. If `db_create_terminal` raises after the provider has
launched, there is no row and no binding — a live agent nothing can route to or
tear down. The persist is now wrapped: on failure a best-effort `TEARDOWN` goes to
the same connection for the launched id (both ids in hand, as you noted), and the
launch failure surfaces as a 500. Tests: the compensating teardown fires for the
leaked id and never binds, and a teardown that itself fails does not mask the
launch error.

---

## Reply to `src/cli_agent_orchestrator/services/inbox_service.py:167` — forgeable sender

(comment id `4068551552`)

Fixed in `892f4e52`. `POST /terminals/{receiver}/inbox/messages` now requires
`sender_id` to name an existing terminal (404 otherwise), so a revoked owner's
agent can no longer name an arbitrary live terminal as sender to slip past the
delivery-time owner gate. The check lives at the central endpoint rather than in
`create_inbox_message`, because on a runtime's local DB a legitimate sender — the
supervisor that dispatched the work — has no local row, and the cross-node callback
path writes through that function directly. Tests cover the forged-sender 404 and
the real-sender accept.

---

## Reply to `src/cli_agent_orchestrator/runtime_channel/registry.py:153` — redelivered result discarded

(comment id `4069096825`)

Fixed in `7d7ad76a`. `resolve` now reports whether the op was pending. On an
unmatched result the channel reconciles before acking: a successful result
carrying a terminal payload with no central row is persisted (`metadata.runtime_id`
set) and bound through the ownership fence, so a LAUNCH result the old server never
acked no longer orphans a live terminal. `owner` is server state the restart lost
and is never sent to the runtime, so a reconciled terminal has none — a known
limitation, far better than a leaked pod. Best-effort, so a failure cannot stall
the ack into an infinite retry. Non-LAUNCH and already-persisted results are
unaffected. Test: a redelivered LAUNCH result for an unknown op persists and binds
the terminal before the ack.

---

## Reply to `src/cli_agent_orchestrator/runtime_channel/api.py:431` — runtime_id in agent-writable metadata

(comment id `4065995057`)

Addressed in `b298a8a5` (see the ownership-hijack reply above). `runtime_id` is now
preserved across `update_terminal_metadata`'s whole-dict replace, so the routing
binding cannot be cleared or repointed through the agent-facing tool. I agree the
durable end state is a server-owned column rather than a metadata key, and the
ownership fence gives a second reason to move it there; flagging that as a
follow-up rather than folding a schema migration into this PR.

---

## Reply to `examples/cao-clusters/kubernetes/eks/broker.yaml:95` — placeholder guard blocks deploy

(comment id `4069016103`)

Fixed in `7ae9e4b6`. `deploy.sh`'s guard now excludes comment lines, so the
commented `arn:aws:iam::<account>:role/<worker-role>` example no longer trips it —
a YAML comment cannot become a bad image name or an empty CIDR — while the guard
stays strict for real unrendered values. Kept the example as a comment rather than
switching to `ACCOUNT`/`WORKER_ROLE`, since the comment-line exclusion is the more
general fix (a future commented placeholder will not regress it either).

---

## Reply to `examples/cao-clusters/kubernetes/eks/deploy.sh:283` — rollout status on OnDelete

(comment id `4069016118`)

Fixed in `7ae9e4b6`. Confirmed: `cao-server` is `updateStrategy: OnDelete` and
`kubectl rollout status` rejects any non-RollingUpdate strategy up front with exit
1, so under `set -euo pipefail` the deploy aborted right after `kubectl apply -k`
and the later gates never ran. Swapped the server gate to
`kubectl wait --for=condition=ready pod/cao-server-0`, which is strategy-agnostic.
The supervisor (RollingUpdate) and the two Deployments keep `rollout status`.

---

## Reply to `examples/cao-clusters/kubernetes/eks/broker.py:125` — unauthenticated GET /runtimes

(comment ids `4069096743`, and the same on line 1841)

Fixed in `7ae9e4b6`. Both `GET /runtimes` reads (`_connected_runtimes` and
`_central_runtime_terminals`) now send a Bearer from a new
`CAO_ELASTIC_CENTRAL_API_TOKEN` when set, via `_central_api_headers()`. Unset — the
default, matching the example's default-off API posture — sends no header and
nothing changes. As you note, the runtime-channel token authorizes the WS channel,
not the HTTP API, so it was never accepted here; this gives the broker an
explicitly configured central-API credential for when auth is enabled.

# Replies to the seventh review round on #802

Six Copilot follow-ups on the fifth/sixth-round fixes. Four were real gaps in
those fixes and are closed in `75f0bb99`; two are design-level and I've explained
the mitigation already in place and proposed them as follow-ups rather than fold
a half-verified protocol/auth change into this PR.

---

## Reply to `runtime_channel/api.py:333` — GapFrame not fenced

(comment id `4069662218`)

Correct, and a real miss on my part. Fixed in `75f0bb99`. The `GapFrame` branch
now calls `claim_terminal` before recording the position or publishing the gap,
exactly like `StreamFrame`/`EventFrame`/`HeartbeatFrame`. Test: a forged gap for
another runtime's terminal is dropped, and neither the watermark nor the routing
moves.

---

## Reply to `runtime_channel/api.py:526` — teardown outcome ignored

(comment id `4069662263`)

Correct. Fixed in `75f0bb99`. `send_command` returns a non-OK `CommandResultFrame`
for a runtime-side teardown failure rather than raising, so the compensating
teardown now inspects the outcome and requires a `deleted`/`absent` confirmation;
an unconfirmed cleanup is logged at ERROR ("did not confirm cleanup ... the agent
may still be running") rather than passed over. The launch failure still surfaces
as the 500. Test added for the FAILED-outcome path.

---

## Reply to `runtime_channel/registry.py:314` — local terminal claimable

(comment id `4069806722`)

Correct — `_placement_from_the_central_row` returned `None` for both a
confirmed-local row and an absent one, so `claim_terminal` let a runtime claim an
existing local terminal. Fixed in `75f0bb99`: a new `_placement_state` returns a
tri-state (`named` / `local` / `absent`). A `local` row (exists, names no runtime)
is now refused — claiming it would redirect a real local pane's routing/status —
while `absent` (a phantom id, or the pre-commit window of a tracked launch) stays
claimable. Test: a confirmed-local terminal is refused; a no-row id still binds.

---

## Reply to `mcp_server/stdio_bridge.py:96` — URL fallback not fail-closed

(comment id `4069806622`)

Correct. Fixed in `75f0bb99`. The shim now requires `CAO_MCP_HTTP_URL` explicitly
(`resolve_shared_endpoint_url`, SystemExit otherwise), matching its documented
"missing URL is fatal" contract. `shared_endpoint_url()` keeps its local-bind
fallback for its legitimate same-host callers; only the shim, whose whole purpose
is to forward to a shared endpoint, must fail closed rather than quietly become
the per-agent in-pod server it removes. Test: the shim refuses to start without a
shared URL.

---

## Reply to `runtime_channel/api.py:325` — generation not enforced

(comment id `4069806687`)

Fair, and I want to be straight about what is and isn't fixed here. The concrete
takeover-hijack that `generation` was meant to fence — a different runtime seizing
a terminal's identity — is now closed by the ownership fence (`claim_terminal`):
a runtime can only bind a terminal the durable row places on it, so a foreign
runtime cannot take over routing regardless of generation. What `generation` would
add on top is disambiguating a *legitimate* reassignment (same terminal id, new
generation, position reset to 0) from a stale continuation on the watermark path.

Actually advancing and enforcing `generation` at the assignment boundary is a
protocol change across the runtime and the server (nothing increments it today,
as the existing comments note), and it interacts with the replay-position and
status-fencing logic that this PR already reworked twice under review. I'd rather
land it as a focused follow-up than bolt a half-enforced generation onto the end
of this PR. Filing it as such; the security-relevant half is covered here.

---

## Reply to `test/api/test_inbox_sender_validation.py:52` — sender not bound to caller

(comment id `4069876635`)

Right that the existence check is necessary but not sufficient: it proves
`sender_id` names a real terminal, not that the caller is that terminal, so a
revoked runtime naming a foreign *non-revoked* terminal still passes and the owner
gate then evaluates the impersonated terminal's owner. It is a strict improvement
over the previous unvalidated param, and I've kept it.

The full fix — binding `sender_id` to the authenticated/request-scoped caller —
needs care I don't want to rush in this PR: the callback caller in the broker
gateway topology authenticates as a forwarded worker identity, not as the sender
terminal, and a naive "sender.owner must equal caller principal" check would
reject legitimate cross-owner callbacks (a worker answering a supervisor owned by
someone else). Deriving the sender server-side from the caller's identity is the
right shape, but it depends on the gateway auth model this PR does not otherwise
touch. Filing it as a follow-up rather than shipping a check that could drop valid
inbox deliveries. Flagging clearly so it is a decision, not an omission.

# Replies to the eighth review round on #802

Three more Copilot follow-ups, all real and all fixed in `1d992728`.

---

## Reply to `api/main.py:3650` — remote caller misrouted to local on placement failure

(comment id `4070130429`)

Correct. `runtime_for_terminal()` returns `None` both for a genuinely-local
caller and for a remote one whose placement lookup transiently failed (the
fail-closed change earlier this round has it swallow `PlacementUnavailableError`
and return `None`), so this branch could create the worker in the central
container for a caller that lives in a runtime. It now gates on `is_remote()`,
which fails closed to `True` on an unreadable placement: if the caller is remote
(or unknown) but the runtime cannot be resolved, it raises a retryable 503 rather
than falling through to the local path. Test added for that exact state.

---

## Reply to `api/main.py:7758` — pre-script written at umask then narrowed

(comment id `4070130483`)

Correct, and it was my own regression: I wrote the uploaded pre-script with
`write_text` then `chmod(0o700)`, the same publish-then-narrow window the earlier
`write_owner_only` work existed to close. Fixed by giving `write_owner_only` an
owner-only `mode` parameter (masked to `0o700`, so a caller cannot widen past the
owner triad) and using it here: the body is owner-only from the first byte via
`mkstemp` + `os.fchmod` before `os.replace`, never group/other-readable at any
instant. Tests: `mode=0o700` yields exactly `0o700`, and a `0o755` request is
masked to `0o700`.

---

## Reply to `utils/terminal.py:233` — registry read unsynchronized across threads

(comment id `4070130514`)

Correct. `effective_status` runs in a worker thread (`asyncio.to_thread`) while
the channel loop mutates the registry on the event-loop thread, so `get_status`'s
liveness-check-then-read could straddle a disconnect and return a stale
COMPLETED, and cross-thread iterations could raise on dict mutation. Added a
`threading.RLock` guarding the in-memory routing/status reads and writes
(`get_status`, `set_status`, `register`, `unregister`, `bind`/`unbind`,
`reconcile_hello`, `record_position`, `remote_terminal_ids`, `list_runtimes`).
DB I/O (the placement lookup) is deliberately kept OUT of the lock so a
worker-thread read never blocks the event loop. A concurrency test hammers
`get_status`/`remote_terminal_ids`/`list_runtimes` against register/unregister
churn and asserts no raise and no stale value.

# Reply to the ninth review round on #802

## Reply to `security/auth.py:350` — non-string sub/iss coerced

(comment id `4070265201`)

Correct. Fixed in `86bcdeff`. `str(claims.get("sub"/"iss"))` would coerce a
malformed claim (`null`, a list, an object) into an owner id like `"None"` or
`"[1, 2]"` — a value that then becomes the canonical principal revocation and
ownership checks key on. Both `sub` and `iss` now require a non-empty string via
an `isinstance` check and fail closed otherwise. Parametrized tests cover
null/list/dict/int for each.

# Replies to the tenth review round on #802

One concrete fix (`c3f7404a`); three re-raises of design-level concerns I'm
deferring with reasoning rather than rushing a cross-component change into this
PR.

---

## Reply to `runtime_channel/bridge.py:836` — websockets version floor

(comment id `4070392051`)

Correct and concrete. Fixed in `c3f7404a`. The bridge and native attach pass
`additional_headers`, which only the new asyncio `websockets.connect` accepts
(14.0+); 12/13 routed to the legacy client's `extra_headers` and would
`TypeError` before connecting. Raised the floor to `websockets>=14.0` (installed
is 15.0.1, so only the declared lower bound moves).

---

## Reply to `api/main.py:7324` — sender not bound to caller (re-raise)

(comment id `4070392004`, same as `4069876635`)

Standing by the earlier reply: the existence check is a strict improvement over
the previous unvalidated param and I've kept it, but binding `sender_id` to the
authenticated caller depends on the broker-gateway auth model this PR does not
touch — the callback caller authenticates as a forwarded worker identity, not as
the sender terminal, so a naive "sender.owner == caller principal" check would
reject legitimate cross-owner callbacks and silently drop valid inbox
deliveries. Deriving the sender server-side from caller context is the right
shape and I've filed it as a follow-up rather than ship a check I cannot verify
against that auth model here. Flagging it as a deliberate decision, not an
oversight.

---

## Reply to `runtime_channel/registry.py:289` and `services/fifo_reader.py:255` — generation never advances

(comment ids `4070392105`, `4070392143`, with `4069806687`)

These three converge on one real gap: `generation` is always 0, nothing advances
it on a stream restart, and the server does not fence on it — so a replacement
stream or reused terminal id can be accepted as a continuation of the old one,
and a reader rearm can splice a new stream onto the old watermark without a
gap/generation marker.

I'm treating this as a dedicated follow-up rather than folding it into this PR,
for two reasons. First, the security-relevant half of what generation was meant
to prevent — a *different runtime* taking over a terminal's identity — is already
closed by the ownership fence (`claim_terminal`): a foreign runtime cannot bind a
terminal the durable row does not place on it, regardless of generation. What
remains is same-runtime stream-restart fencing, a correctness concern, not an
authorization bypass. Second, a correct fix is a coordinated protocol change
across the `ReplayBuffer` (carry and reset a generation), `fifo_reader` (advance
it on rearm/restart), the bridge (stamp frames), and the server registry
(validate and transition at the boundary) — touching the exact replay/resume and
watermark logic this PR has already reworked across several review rounds.
Landing a half-enforced generation on top of that is more likely to introduce
silent replay corruption than to remove it. It deserves its own change with its
own test pass, which I've filed.
