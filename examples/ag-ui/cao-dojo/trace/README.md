# CAO Dojo trace

This directory holds the recorded AG-UI trace from #1116 and the shape file that
the keyless drift check compares against.

| File | Status | What it is |
|---|---|---|
| `cao-dojo-trace.shape.json` | committed | The tier-2 baseline. It is the trace projected onto what tier 2 compares: the record sequence, each record's key paths, and a closed set of values. It holds no provider-specific value, so a keyless `mock_cli` recording and a Claude Code recording produce the same bytes. Regenerate it with `uv run python examples/ag-ui/cao-dojo/tools/record_trace.py --mock --write-shape`. |
| `cao-dojo-trace.ndjson` | committed | The published trace: three captures (`none`, `approve`, `reject`) from a real Claude Code permission prompt (`claude-code:permission_request`), normalised and leak-checked. |

## The published trace

It was recorded against the AG-UI 1.0 run plane and ambient stream (#1232), so
`STATE_SNAPSHOT` carries no `thread_id`/`run_id` and `known_gaps.json` is empty.
`_meta.shape_sha256` is the hash of the shape file it was recorded against.

Re-record it when the debt check reports it stale, which happens when someone
regenerates the shape file after a deliberate frame change:

- every PR gate still passes, because tier 1 runs on an in-process capture of
  CAO's own producers as well as the trace, and tier 2 runs a live `mock_cli`
  recording against the shape file;
- the debt check warns on every PR and **blocks the release path** with
  `trace missing or stale` until the trace is re-recorded;
- the tier-1 checks that compare the published trace with CAO's own producers
  (the snapshot interior and the card mapper) skip the published trace while it
  is stale, and keep running on the offline capture, where they only compare
  CAO's producers with themselves;
- a `known_gaps.json` entry passes while at least one trace still observes its
  gap. Both lanes take the union across traces: the Python tier 1 over the
  offline capture and the published trace, the node lane over the mock
  recording and the published trace. So if a keyless change removes a listed
  key, **keep its entry until the re-record**: the stale published trace still
  carries the key, and both lanes fail on an unlisted key.

An `ag-ui-protocol` minor bump also needs a re-record before the next release:
`_meta.ag_ui_protocol` names the SDK the trace was recorded with, and tier 1
only warns when it differs from the installed one. The release check does not
catch this, because it compares the trace with the shape file, not with the
installed SDK.

To re-record it, from a clean checkout with a logged-in Claude Code on `PATH`:

```bash
make record-agui-trace
```

That runs `record_trace.py --live --out examples/ag-ui/cao-dojo/trace/cao-dojo-trace.ndjson`
against an isolated server on port 19899 (`RECORD_PORT=<port>` to change it). It
runs two short `haiku` sessions (approve and reject). The recording of the
committed trace used about 1,000 output tokens, plus about 313,000 cache-write
and 120,000 cache-read input tokens, almost all of it Claude Code's own
system prompt. It refuses to write the trace if the live
recording does not project onto the committed shape file, and it stamps
`_meta.shape_sha256` so the debt check can tell when the shape moves on.

See [`../README.md`](../README.md#recorded-trace-and-drift-checks) for the gates.
