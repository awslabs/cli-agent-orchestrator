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
