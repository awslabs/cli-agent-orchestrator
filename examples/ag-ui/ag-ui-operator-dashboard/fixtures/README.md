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

## Session timeline

| File | What it is |
|---|---|
| `session-timeline.sse` | `GET /agui/v1/stream` for a supervisor that assigns work to one worker and hands off to another, after which the first worker is closed: a `TOOL_CALL_START` (`assign`, `toolCallId` equal to its `eventId`), a `TOOL_CALL_START` (`handoff`), and a `TOOL_CALL_END` (`closedBy: "completion"`) that closes only the first. The second call stays open. |

| `session-superseded.sse` | `GET /agui/v1/stream` for a supervisor that assigns work to a worker and then assigns that same worker a second task (`POST /terminals/<worker>/input?sender_id=<supervisor>&orchestration_type=assign`), after which the worker is closed: `TOOL_CALL_START` for the first call, `TOOL_CALL_END` with `closedBy: "superseded"` for it when the second call opens, `TOOL_CALL_START` for the second, and `TOOL_CALL_END` (`closedBy: "completion"`) for the second. |

**Source: `cao-server` built at `7fc2f070653242bc01cddf7ff8e337bf2796bf5f`.**

Captured from a private `cao-server` at that commit, with `CAO_AGUI_ENABLED=1`, on a non-default port with a throwaway `HOME` and its own tmux socket. Three `mock_cli` terminals: a supervisor (`POST /sessions`) and two workers created with `POST /sessions/<session>/terminals?caller_id=<supervisor>&defer_init=true` and a body carrying `initial_message` and `initial_message_orchestration_type` (`assign`, then `handoff`). `mock_cli` does not finish on its own, so the first call was closed by deleting its worker (`DELETE /terminals/<id>`): the server closes a call when its receiver's work ends. `session-superseded.sse` comes from the same commit and the same setup, with two `mock_cli` terminals: the supervisor and one worker created the same way with `assign`, then given a second `assign` through `POST /terminals/<worker>/input` with `sender_id` and `orchestration_type`, then deleted. The captures hold metadata only (ids, names and timestamps), with no message text.

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
