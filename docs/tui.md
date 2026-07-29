# Terminal UI

`cao tui` is a guided terminal front door over the `cao` CLI. It shows you the
commands that exist, helps you fill in their arguments, previews the exact command
it would run, and then runs or copies that command verbatim.

It is a **shell over the CLI, not a second implementation of it.** Every command
the TUI builds is a real `cao` invocation you could have typed yourself, and the
preview pane always shows the exact `argv`. Nothing the TUI can do is unavailable
from the plain CLI.

## When to use it

Use `cao tui` when you are exploring interactively — you know roughly what you
want but not the flag names, or you want to see a command before committing to it.

Use the plain `cao` CLI for scripts, CI, cron, and anything non-interactive. The
TUI is not scriptable and deliberately has no batch mode; see
[control planes](control-planes.md) for choosing between the surfaces.

## Starting it

```bash
cao tui
```

No separate install step and no Node.js: the TUI is pure Python and ships in the
CAO wheel. `prompt_toolkit` and `pyperclip` are unconditional runtime
dependencies of the package, so a plain `uv tool install` has everything.

`cao-server` is **optional**. Command building, previewing, running, and copying
all work with the server down; only the *live reads* need it — the provider
pre-flight footer and the Profiles browser. When the server is unreachable the TUI
opens on a screen that shows the exact start command:

```bash
cao-server
```

Press `[r]` to re-probe once it is up.

## Keys

The footer always shows the keys valid on the current screen, so the map below is
a reference rather than something to memorise.

| Key | Action |
|---|---|
| Up / Down | Move the selection |
| Tab / Shift-Tab | Move focus between panes |
| Enter | Drill into a group, open a command, or run the open command |
| Esc | Go back one level (or leave the Profiles screen) |
| `[e]` | Edit an argument — press repeatedly to cycle through every argument |
| `[x]` | Clear the currently targeted argument |
| `[/]` | Filter the visible list |
| `[c]` | Copy the previewed command to the system clipboard |
| `[p]` | Open the Profiles browser |
| `[r]` | Re-probe `cao-server` |
| `[q]` / Ctrl-C | Quit |

While an input row is open it owns the keyboard: printable keys are typed into the
buffer rather than treated as commands, Enter commits, and Esc cancels. Ctrl-C
always quits.

## Building a command

1. Move the selection to a command group and press Enter to drill in; press Enter
   again on a command to open it.
2. Press `[e]` to edit an argument. Each press advances to the next argument and
   wraps at the end, so every argument is reachable — including ones you have
   already filled in and want to correct. The input row names the argument it is
   editing.
3. Argument completion offers flag names and, where the CLI documents a fixed set
   of choices, the choice values. Press Tab to accept.
4. Press `[x]` to clear the argument `[e]` is currently targeting. It reverts to
   `(unset)` and disappears from the preview.
5. The preview pane shows the exact command throughout. Press Enter on the open
   command to run it, or `[c]` to copy it.

Running a command suspends the TUI, hands the terminal to `cao`, and resumes when
it exits — so interactive commands behave exactly as they do from your shell. The
`cao` process's output is never captured or reformatted.

## Profiles

Press `[p]` to browse agent profiles. This is a **read-only** view over
`GET /agents/profiles`: it lists the profiles the server knows about and shows the
selected profile's provider, role, tools, and description. Esc returns to the
command list.

Because it is a live read, `[p]` requires a reachable `cao-server`. A failed read
renders a notice in the pane, never a traceback.

## Provider pre-flight

The footer shows which agent CLIs the server reports as installed, as plain
`yes`/`no` text. The line is cached briefly so it cannot issue a network read on
every repaint, and the read is bounded by a short timeout so a slow server cannot
freeze the interface. A failed read degrades to a note; it never blocks command
building.

## Known limitations

Two limitations are **deliberately not addressed** in the change that introduced
this document. Both are recorded here so you are never left at an unexplained dead
end.

### The TUI cannot read from an auth-enabled server

If `cao-server` is running with authentication configured (`AUTH0_DOMAIN` or
`CAO_AUTH_JWKS_URI` set), it answers `401` to unauthenticated requests. The TUI
sends no `Authorization` header and has no credential discovery, so its live reads
— the provider pre-flight footer and the Profiles browser — will not work against
such a server.

What you will see: `providers: cao-server requires authentication (this TUI
cannot authenticate yet)` rather than a misleading "server not reachable". That
message distinction is the whole of the current behaviour; token plumbing is not
implemented.

What to do: use the plain `cao` CLI, the Web UI, or the HTTP API against an
auth-enabled server. Command building, previewing, running, and copying still work
normally in the TUI — only the live reads are unavailable.

### The thin-shell import guard is direct-import only

The TUI's architectural promise is that it stays a thin shell: it may import only
the standard library, `prompt_toolkit`, `requests`,
`cli_agent_orchestrator.constants`, `cli_agent_orchestrator.utils.path_validation`,
and its own modules — never the heavy in-process layers (`services`, `clients`,
`backends`, `providers`, `models`) or the `cli` command modules.

`test/tui/test_thin_shell_boundary.py` enforces that with an allow-list, but it
AST-scans only the files under `src/cli_agent_orchestrator/tui/`. It therefore
constrains what those modules import **directly**, not what their imports in turn
pull in.

A live example: the TUI imports `cli_agent_orchestrator.constants`, which is
allowed; `constants.py` itself imports `cli_agent_orchestrator.models.provider`.
So a "forbidden" layer is reachable *indirectly* today and the guard passes.
Closing the gap means either breaking the `constants` → `models` edge or scanning
the transitive closure, neither of which was in scope. Do not read a green guard
as proof that no heavy layer is loaded — read it as proof that no TUI module names
one itself.

## Related

- [docs/control-planes.md](control-planes.md) — where the TUI fits alongside the
  Web UI, `cao session`, and `cao-ops-mcp`
- [docs/web-ui.md](web-ui.md) — the browser dashboard, the other interactive
  surface
- [docs/agent-profile.md](agent-profile.md) — what the Profiles browser is showing
  you
- [docs/api.md](api.md) — the HTTP routes the TUI's live reads use
- [docs/configuration.md](configuration.md) — server host, port, and agent
  directories
