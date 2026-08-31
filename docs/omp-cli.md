# Oh My Pi Provider

CAO supports [Oh My Pi (OMP)](https://github.com/can1357/oh-my-pi) through the
public provider name `omp`. The integration starts the user's normal interactive
OMP, then adds only CAO's role context and profile MCP servers.

## Prerequisites

- OMP 17.2.10 on `PATH` (`omp --version`)
- An authenticated OMP model account; run `omp` directly and use its normal
  authentication flow before launching through CAO
- tmux, required by CAO's terminal backend

CAO does not install OMP or modify its authentication, approval mode, tool
policy, model catalog, rules, skills, or project configuration.

## Quick start

```bash
cao install developer --provider omp
uv run cao-server
cao launch --agents developer --provider omp
```

`--auto-approve` skips CAO's workspace confirmation only. It does not change
OMP's approval policy. Choose a model for one launch with:

```bash
cao launch --agents developer --provider omp --model openai/gpt-5.6
```

The explicit `--model` value wins over the profile's `model`; otherwise the
profile value is used. With neither value, OMP chooses from its normal native
configuration.

## Native configuration is preserved

The launch command intentionally omits OMP flags that replace or narrow native
behaviour: `--profile`, `--config`, `--tools`, `--no-tools`, `--no-skills`,
`--no-rules`, `--no-extensions`, `--auto-approve`, and `--approval-mode`.

OMP continues to discover its normal project and user configuration, including
`.omp/` and `~/.omp/agent/` roots. CAO writes a per-terminal context file under
`$CAO_HOME_DIR/tmp/omp/<terminal-id>/context.md` (mode `0600`) and passes it by
`--append-system-prompt`; it never uses `--system-prompt`. OMP's default
workflow, native tools, rules, and skills therefore remain present.

## MCP servers and terminal identity

If an installed CAO profile has `mcpServers`, CAO creates a private extension
root under the same per-terminal directory:

- `index.js` contains the minimal OMP extension entrypoint.
- `.mcp.json` contains only that profile's MCP servers.
- OMP receives `--extension <directory>`.

OMP's extension-package discovery loads sibling `.mcp.json` at priority 90;
normal user/project OMP configuration remains priority 100. Native definitions
therefore stay active and win on a same-name conflict. CAO does not write to
the working tree or `~/.omp`.

For stdio MCP servers, CAO resolves the runtime command and adds
`CAO_TERMINAL_ID` only when the server did not set it already. HTTP/SSE entries
are preserved. Every generated directory is mode `0700`, generated files are
mode `0600`, and terminal cleanup removes only its own directory.

## Tool restrictions and approvals

OMP's `--tools` only filters built-ins; it cannot fully restrict discovered
custom, extension, or MCP tools. CAO restrictions are consequently **advisory**
for `omp`, not hard enforcement. A restricted profile receives CAO's security
prompt and its exact CAO tool list, while OMP's configured tool set,
`tools.approvalMode`, and per-tool policy remain authoritative.

An OMP `Allow tool: <name>` dialog is surfaced as `WAITING_USER_ANSWER`. CAO
holds orchestrated inbox delivery while that dialog is active. Respond in the
OMP terminal using its normal approve/deny controls.

## Status and output

OMP 17.2.10 fixtures cover these terminal markers:

| CAO state | OMP evidence |
| --- | --- |
| `PROCESSING` | `Working… ⟨esc⟩` or a running-tool indicator |
| `WAITING_USER_ANSWER` | `Allow tool: <name>` |
| `ERROR` | active OMP runtime/provider error frame |
| `IDLE` / `COMPLETED` | ready frame; CAO uses dispatch history to distinguish the first ready state from a finished turn |

A later OMP ready status line (`in: … out: … t: … tok/s: …`) makes an older
waiting, error, or processing marker stale. Response extraction uses the final
rendered assistant block before the ready frame and removes only OMP borders,
tool cards, advisor chrome, and footer/status rows. Prose such as `Error:`,
`working`, or `cancel` is retained.

## Supervisor and worker workflow

Install the three profiles, start CAO, then launch the supervisor:

```bash
cao install examples/assign/data_analyst.md --provider omp
cao install examples/assign/report_generator.md --provider omp
cao install examples/assign/analysis_supervisor.md --provider omp
uv run cao-server
cao launch --agents analysis_supervisor --provider omp --auto-approve
```

Ask the supervisor to analyze three distinct datasets and create one report.
The intended flow is parallel `assign` calls to data analysts; each analyst
uses `send_message` for its callback; then the supervisor performs a sequential
`handoff` to `report_generator`; finally it combines all worker outputs. The
supervisor should delegate analysis and report drafting rather than doing either
itself. The CAO MCP extension supplies those orchestration tools; OMP's native
`task` tool is not the CAO workflow mechanism.

## Troubleshooting

**`Oh My Pi not found`** — install OMP so `omp` resolves in the shell inherited
by tmux, then verify with `omp --version`.

**Launch reaches an OMP approval dialog** — answer the OMP dialog or use an
operator-controlled test configuration. CAO intentionally does not pass an
approval-bypass flag.

**No CAO MCP tools** — reinstall the profile with its `mcpServers`, then inspect
the generated per-terminal extension root before terminal cleanup. Do not add
CAO entries to `.omp` or `~/.omp/agent/` manually.

**Status remains processing** — inspect the live terminal for an active
`Working…` / tool frame or an approval dialog. Captured fixtures and regexes
target OMP 17.2.10; recapture terminal fixtures before changing markers for a
new OMP release.
