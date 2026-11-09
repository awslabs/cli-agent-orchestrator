# Fixtures

Frames the unit tests replay. They follow the ambient-stream attribution of
[#1243](https://github.com/awslabs/cli-agent-orchestrator/pull/1243): an event
whose sender is not a terminal CAO knows (here, `terminal_id: "operator"`)
carries no `subagentRunId`, and no subagent is announced for it.

**Source: `cao-server` built at #1243's head, `f9e33841d221007ffd2524d0cf1412bd9e83fb73`.**

## Captured from a running server

A private `cao-server` from that commit, with `CAO_AGUI_ENABLED=1` and
`CAO_MOCK_CLI_SCRIPTED_PROMPTS=1`, on a non-default port with a throwaway
`HOME` and its own tmux socket. Two `mock_cli` terminals (A and B) raised
prompts by having `APPROVAL_REQUIRED: <text>` typed into their panes.

| File | What it is |
|---|---|
| `capabilities.json` | `GET /agui/v1/capabilities` |
| `empty-run.sse` | `POST /agui/v1/run` with nothing pending: `RUN_FINISHED(success)` |
| `one-interrupt.sse` | A run while A waits: `RUN_FINISHED(interrupt)` with one entry |
| `resume-approve.sse` | `resume[]` approving it: `RUN_FINISHED(success)`; A's pane received `y` |
| `two-interrupts.sse` | A and B both waiting: two entries |
| `resume-batched.sse` | One `resume[]` approving A and denying B; the panes received `y` and `n` |
| `reconcile-before-edit.sse` | A waits again |
| `resume-edit-refused.sse` | `editedArgs` sent to an approve/deny-only prompt: `RUN_ERROR` |
| `reconcile-after-edit.sse` | The run after that error: the interrupt is still listed |
| `resume-skipped.sse` | An unknown id and a real deny: `skippedResume` lists the unknown id |
| `resume-no-status.json` | A `resume[]` entry without `status`: HTTP 422 and its body |
| `ambient.sse` | `GET /agui/v1/stream` for the whole capture (bounded by `maxSeconds`): the six components from A, one `metric` from sender `operator` (no `subagentRunId`), a `metric` with markup in its props, and the construct's `approval_card` frames |

## Generated with that commit's code

No shipped producer can put these on the wire today, so they come from the
same commit's code run in-process, not from a hand-written shape.

| File | How |
|---|---|
| `raw-offlist.generated.json` | `AmbientRun.feed()` on `OFF_LIST_UI` from `test/_support/agui_ambient_records.py`: the `RAW` frame with `event.rejected_component` that the stream emits for an off-list record. `emit_ui` refuses such a call with 400 before the bus, so a real call never produces it. |
| `edit-offered.generated.sse` | `run_plane_stream()` with an `AgentHandoffWithApproval` holding a Kiro `permission_request` interrupt: a `responseSchema` with `editedArgs`. `mock_cli` never offers edit. |

HTTP 403, 404 and 501 bodies are built in the tests from the route code
(`api/main.py`), because this server could not produce them without other
configuration.

The fixtures hold metadata only: ids, reasons, timestamps and the props the
capture script sent. They contain no terminal output.
