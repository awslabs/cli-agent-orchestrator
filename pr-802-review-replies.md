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
