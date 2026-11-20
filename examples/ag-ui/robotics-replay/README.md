# Robotics replay: a mock factory run as an AG-UI timeline

This example replays one recorded run of the [hierarchical-factory](../../robotics/hierarchical-factory/)
example as an AG-UI session timeline (#1166). It shows what the AG-UI layer adds to the
robotics viewer: CAO's own orchestration events, meaning the supervisor's agent-to-agent
`assign` and `handoff` calls and an operator approval. The recorder asks for that approval
before it lets the simulated robots move (the example's `allow_motion` gate). The trace itself
does not say what was approved: the prompt text is scrubbed to an empty string.

**This is a MOCK run.** The agents are `mock_cli` seats and the supervisor's and managers'
decisions are scripted by the recorder. The CAO server, its AG-UI streams, the approval card and
the MuJoCo factory floor (simple biped proxies) are real. Nothing here says anything about how
well a real agent would run the factory.

## What is in it

| File | What it is |
|---|---|
| [`replay.html`](replay.html), [`replay-rows.js`](replay-rows.js) | The replay page and its row builder. Zero dependencies. It reads the two files below and nothing else: no stream, no `cao-server`, no `CAO_AGUI_ENABLED`, nothing written. |
| [`trace/robotics-mock-trace.ndjson`](trace/robotics-mock-trace.ndjson) | The recorded AG-UI trace (`cao-dojo-trace/1`), 56 records: 3 `assign` calls and 1 `handoff`, each opened and closed, one `approval_card` raised, the resume with `approved: true`, and two resolutions of that card: first with outcome `expired`, then with outcome `approve`. The trace shows the order, not the cause. The order is consistent with how CAO's approval bridge does it (`services/agui/handoff_approval.py`): while the decision is being delivered the prompt goes away and `expire()` resolves the card as `expired`, and once delivery succeeds the decision is committed and the card is resolved again as `approve` ("delivery beats expire"). Scrubbed by the CAO Dojo's `trace_scrub.py` and checked by its `trace_leaks.py`. |
| [`trace/factory-command-log.json`](trace/factory-command-log.json) | The factory commands (`form_team`, `start_line`, `start_shipping`, `set_pace`) the managers would send. They are **not** AG-UI events. The page places each after the tool call it followed, labelled `factory`. |
| [`tools/record_robotics_trace.py`](tools/record_robotics_trace.py), [`tools/run.sh`](tools/run.sh), [`tools/mock-factory-agent.md`](tools/mock-factory-agent.md), [`tools/replay_lifecycle.py`](tools/replay_lifecycle.py) | The recorder. It starts a private `cao-server` (own `CAO_HOME_DIR`, own tmux server, loopback port 19931 by default, never 9889). It waits, within a bound, for every tool call and subagent run it opened to be closed on the stream, and refuses to write a trace that still has one open. |

## Replay it

```bash
cd examples/ag-ui/robotics-replay
python3 -m http.server 8791 --bind 127.0.0.1
# http://127.0.0.1:8791/replay.html
```

Use Play, Step, Show all and Reset.

## Record it again

Needs `tmux`, `uv` and a checkout synced with `uv sync --extra agui`. The recorder installs
`mujoco` and `pyyaml` for the run only.

```bash
examples/ag-ui/robotics-replay/tools/run.sh /tmp/robotics-out
```

It writes both files, and refuses to write the trace if `trace_leaks.check` reports a finding.
A re-record is **not byte-identical**: the `host` entry carries the machine's own load and
memory figures, timing shifts a few ids, and the number of `STATE_DELTA` frames varies with
how the fleet snapshots fall (23 in the committed trace, 21 in one re-record). The calls, the
approval and the command log are the same. The shape of the run does not change, and the tests
below read the file, not a recording. `_meta.cao_build` names the commit the trace was recorded
on.

## Checks

```bash
uv run pytest test/examples/test_robotics_replay_trace.py
```

- The published trace has no leak finding, and is exactly what `trace_scrub.normalise` writes.
- **Negative control:** four copies, each with one leak injected (a raw id inside a `STATE_DELTA`
  value, a raw `metadata.cao.sender`, a raw `runId`, a host name as a `runner_id`), must each
  produce a finding of the expected rule at the injected path. A rule that stops firing fails its case.
- The command log holds no path, e-mail address, dotted host name (`build-box-3.internal`) or long hex id, and leaky copies are refused. The recorder applies the same rule before it writes the log. A single-label host name reads like a word and is not refused.
- The trace has the expected calls, approval, parties and legal run lifecycles.
- The page and its row builder are static readers (no stream client, no markup parsing, no
  external resource), and the row builder is run under `node` against counts read from the file.
