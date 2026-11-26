# Screenshots of the CAO window

Frames of the CAO window, taken with `herdr-app shot` at the window's 1280 by 820 size. The left sidebar is at its default 268 px unless a line says otherwise.

| Image | What it shows |
|---|---|
| [01-overview-dark.png](01-overview-dark.png) | The whole window, dark theme: Sessions, the Agents tree of a session with the agent's recap, the top-bar tabs and the status bar counts. |
| [02-overview-light.png](02-overview-light.png) | The same view in a light theme. |
| [03-no-sessions-dark.png](03-no-sessions-dark.png) | A cao-server with no sessions: "No CAO sessions" and "Select a session". The toast at the bottom right is Herdr's own update notice. |
| [04-sessions-filtered-asking-dark.png](04-sessions-filtered-asking-dark.png) | The asking filter on, from the status bar: the header reads `1 of 5 · asking` beside its `clear` control. |
| [05-started-by-schedule-dark.png](05-started-by-schedule-dark.png) | A session a schedule started: the `inferred from the session name` label above the "Started by schedule" link. |
| [06-started-by-schedule-light.png](06-started-by-schedule-light.png) | The same view in a light theme. |
| [07-agents-tree-pinned-light.png](07-agents-tree-pinned-light.png) | A supervisor with eight workers, all pinned, so each has a tab in the top bar. |
| [08-tab-strip-scrolled-light.png](08-tab-strip-scrolled-light.png) | The same session with a worker focused: the tab strip has scrolled to its tab. |
| [09-schedules-list-dark.png](09-schedules-list-dark.png) | The right sidebar open at the list of schedules, one enabled with its next run and one off. |
| [10-schedule-details-dark.png](10-schedule-details-dark.png) | A schedule's details: its switch, Run now, Edit, the schedule in words, the next three runs, the last run and the target. |
| [11-schedule-disable-confirm-dark.png](11-schedule-disable-confirm-dark.png) | The inline confirmation before a schedule is disabled. |
| [12-workflows-list-light.png](12-workflows-list-light.png) | The right sidebar open at the list of workflows. |
| [13-workflow-details-dark.png](13-workflow-details-dark.png) | A workflow's details: Run, Schedule... and Edit, its mode and steps, and that no schedule runs it. |
| [14-new-session-profiles-dark.png](14-new-session-profiles-dark.png) | The New session dialog with its profile list and the providers, the ones not installed marked so. |
| [15-new-session-tool-policy-dark.png](15-new-session-tool-policy-dark.png) | The rest of New session: the message, the three tool policies and the equivalent `cao launch` command. |
| [16-yolo-typed-confirm-dark.png](16-yolo-typed-confirm-dark.png) | The YOLO confirmation, which asks for the session name to be typed. |
| [17-new-schedule-field-error-dark.png](17-new-schedule-field-error-dark.png) | New schedule with a name the server's rule refuses, the error under the field and Create disabled. |
| [18-new-workflow-dark.png](18-new-workflow-dark.png) | New workflow with the Python template filled in. |
| [19-discard-form-dark.png](19-discard-form-dark.png) | "Discard this form?" when a form with edits is closed. |
| [20-run-workflow-dark.png](20-run-workflow-dark.png) | The Run form of a workflow: a field for its declared input `version` and the optional working directory. |
| [21-status-help-dark.png](21-status-help-dark.png) | The help that the status bar's `?` opens, with the known-gap label. |
| [22-narrow-sidebar-dark.png](22-narrow-sidebar-dark.png) | The left sidebar at its 200 px minimum: names end in `…`, the marks stay. |
| [23-sidebar-hidden-dark.png](23-sidebar-hidden-dark.png) | The left sidebar hidden (the fold button or Cmd-B): the top bar starts after the room for the traffic lights, with the button that shows the sidebar again. |
| [24-sidebar-hidden-light.png](24-sidebar-hidden-light.png) | The same in a light theme. |

## What these frames are, and what they leave out

The terminals in these frames are stand-ins: a small mock CLI (`mock_cli`) that prints a few lines and reports its state, and that Herdr takes for Claude Code, so it draws the Claude Code logo and the "Claude Code" pane header. No real agent ran. The sessions (`cao-demo-*`, `cao-flow-demo-weekly-cleanup`), the schedules, the workflow, the profiles and the folders under `/tmp/cao-shots-work` are demo data on a scratch cao-server and a scratch window home, not anyone's work. Frames 10, 11 and 13 to 21 were taken before two header fixes and show the old text in the left sidebar: the Agents block cuts the schedule link to `Start…` beside its label, behind the dialog or in full, and 21 shows the sidebar at a wider width than the default. Frame 22 shows a fault that remains at the 200 px minimum: the `inferred from the session name` label is a few pixels too wide and is clipped. The frames do not show the macOS traffic lights, because `herdr-app shot` draws the window's content without the system's window buttons, so 23 and 24 show the room left for them, not the buttons. Not shown: Herdr's own fold to a rail, which this window no longer uses (its fold hides the sidebar); hover tooltips; the card shown after a launch; the Cancel and Resume confirmations of a workflow run; the dialogs in a light theme; the "CAO unreachable" and "CAO machine not connected" notices; the window against an older cao-server that lacks the role labels or the flow routes; and the Dock tile, the Dock badge and Cmd-Tab.
