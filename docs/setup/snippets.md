# Shared setup snippets

Most setup guides change the running `cao-server`: they install into its Python, write
`settings.json` or restart it. These are the steps they share, written once so every guide
links here instead of carrying its own copy. They were lifted from the
[ephemeral agents runbook](../ephemeral-agents-agent-setup.md).

Each snippet ends with what you should see. If you don't see it, stop and report it: don't
guess past it. The guide that sent you here says when to get the user's OK; a snippet that
writes or restarts something says so too.

The snippets set shell variables that later ones use (`SETTINGS`, `PORT`, `PID`, `CAO_PY`). If
your shell drops variables between commands, start each later command with
`CAO_PY='<path from the find-the-server snippet>';`, or paste the literal path.

**Never print secrets.** None of these read a process's environment, print a variable's value
or `cat` a file that can hold a credential. Keep it that way when you adapt them.

- [Back up `settings.json`](#back-up-settingsjson), and restore it
- [Find the server's Python](#find-the-servers-python), and check the `cao` on `PATH` is its CLI
- [Check an add-on is in uv's receipt](#check-an-add-on-is-in-uvs-receipt), so `cao update`
  keeps it
- [Restart the server and wait for `/health`](#restart-the-server-and-wait-for-health)
- [Reconnect your MCP connection](#reconnect-your-mcp-connection)

## Back up `settings.json`

Do this before the first settings write:

```bash
SETTINGS="$(cao config path)"
if [ -f "$SETTINGS" ]; then cp "$SETTINGS" "$SETTINGS.bak.$(date +%s)"; else echo "no settings.json yet: nothing to back up"; fi
```

**What you should see:** nothing, or `no settings.json yet: nothing to back up`. Keep the backup
path for the guide's undo step. On a fresh install there's no file, so "restore the backup"
later means removing the `settings.json` the setup created. Once the user confirms the setup is
done, delete the `.bak.*` copies: they are plain copies of an operator-only file.

**To restore it,** stop the server first (this needs the user's OK), then:

```bash
SETTINGS="$(cao config path)"
ls "$SETTINGS".bak.*                           # pick the one the backup made
cp "$SETTINGS.bak.<timestamp>" "$SETTINGS"     # or, if there was no backup: rm "$SETTINGS"
```

Then start the server again and run the restart checks below.

## Find the server's Python

Every later check runs in the Python interpreter the running `cao-server` really uses, not in a
guessed path and not in your own virtual environment. Take it from the process listening on the
CAO port. Read only the process's command line, never its environment:

```bash
PORT="${CAO_API_PORT:-9889}"
PID="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t | head -n 1)"
if [ -n "$PID" ]; then
  ps -o args= -p "$PID"   # e.g. /path/to/venv/bin/python3 /path/to/cao-server
  CAO_PY="$(ps -o args= -p "$PID" | awk '{print $1}')"
  "$CAO_PY" -c 'import cli_agent_orchestrator' 2>/dev/null ||   # e.g. a macOS framework Python
    CAO_PY="$(head -n 1 "$(ps -o args= -p "$PID" | awk '{print $2}')" | sed 's/^#! *//')"
elif curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null; then
  echo "/health answers but lsof sees no listener: the server may run as another user: stop and ask"
else
  echo "no cao-server on port $PORT: using the uv tool path, unconfirmed"
  CAO_PY="$(uv tool dir)/cli-agent-orchestrator/bin/python3"
fi
"$CAO_PY" -c 'import sys, cli_agent_orchestrator as c; print(sys.prefix); print(c.__file__)'
readlink -f "$(command -v cao)"; readlink -f "$(dirname "$CAO_PY")/cao"
```

**What you should see:** the import succeeds, and the last line prints the same path twice. That
means the `cao` on your `PATH` is that server's own CLI. If the paths differ, or a path has
spaces (which `awk` cuts), stop and ask. If no server was running, tell the user the path is a
guess until the restart checks confirm it.

Keep the `ps` line: the restart checks compare against it.

**Find out how the server is started:** a foreground terminal, a background process, `launchd`
or a container. The `ps` line above helps. A restart has to use the same way, and CAO won't
start a server for you. Ask the user if it isn't obvious.

## Check an add-on is in uv's receipt

An add-on (a decider, an event plugin, an extra such as `otel` or `agui`) only survives
`cao update` if uv's install receipt records it. Something added with `uv pip install` loads
until the next upgrade or reinstall, and then it's silently gone. See
[Updating CAO](../updating.md) for what `cao update` runs for each kind of install.

First, check the server runs from CAO's `uv tool` environment, which is the common case:

```bash
[ "$("$CAO_PY" -c 'import sys; print(sys.prefix)')" = "$(uv tool dir)/cli-agent-orchestrator" ] \
  && echo "uv tool venv: the receipt applies" || echo "NOT the uv tool venv: stop and ask the user"
```

If it's another environment (a source checkout's `.venv`, pipx, a plain venv), there's no
receipt: ask the user how it's managed, and install only the way they say.

Then print the receipt's requirements, with any credential in a URL hidden:

```bash
"$CAO_PY" - "$(uv tool dir)/cli-agent-orchestrator/uv-receipt.toml" <<'EOF'
import re, sys, tomllib
with open(sys.argv[1], "rb") as f:
    requirements = tomllib.load(f)["tool"]["requirements"]
for entry in requirements:  # print only the requirements, with any user:password@ in a URL hidden
    print({k: re.sub(r"//[^/@]*@", "//<redacted>@", v) if isinstance(v, str) else v
           for k, v in entry.items()})
EOF
```

**What you should see:** one line per requirement, such as
`{'name': 'cli-agent-orchestrator', 'git': 'https://github.com/awslabs/cli-agent-orchestrator.git'}`.
An extra is recorded on the `cli-agent-orchestrator` line; a package added with `--with` gets a
line of its own. Don't `cat` the receipt: a source URL can
carry a credential. If any line shows `<redacted>`, stop: a reinstall that copies that source
is the user's to run, because you can't copy it.

To check one add-on by name, without printing anything else:

```bash
ADDON='<package or extra name>'
grep -q "\"$ADDON\"" "$(uv tool dir)/cli-agent-orchestrator/uv-receipt.toml" \
  && echo "recorded" || echo "NOT recorded"
```

**What you should see:** `recorded` if the receipt keeps it, `NOT recorded` if the next upgrade
will drop it. Run this again after every `cao update` or reinstall, and tell the user to do the
same.

## Restart the server and wait for `/health`

Most opt-in features are read only when `cao-server` starts. A restart affects every live
session, not only yours, so get the user's OK first. Then restart it the way the
find-the-server snippet found it running:

- a foreground terminal: Ctrl-C, then start it again;
- a background process: stop it, then start it again;
- `launchd`: `launchctl kickstart -k gui/$(id -u)/<label>`, with the job label from the user
  (`-k` stops the running instance first; without it, kickstart leaves a running job alone).

Find the process by its port, never by matching command-line text: your own shell can match that
text too. Then check the restart. The block waits for the server, trying once a second: about
30 s if nothing listens, or up to about 90 s if a server accepts the connection but never
answers. Don't replace it with a fixed `sleep`.

```bash
PORT="${CAO_API_PORT:-9889}"
UP=""
for i in $(seq 1 30); do
  if curl -sf --max-time 2 "http://127.0.0.1:${PORT}/health" >/dev/null; then UP=1; break; fi
  sleep 1
done
if [ -n "$UP" ]; then
  echo "server answers"
  PID="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t | head -n 1)"
  ps -o pid,lstart,etime -p "$PID"                   # the start time must be after your change
  ps -o args= -p "$PID"                              # must match the ps line you kept earlier
else
  echo "no answer on port $PORT after 30 tries: stop and report"
fi
```

**What you should see:** `server answers`, a start time after your change, and the same `ps`
line as before. If it prints `no answer`, the restart failed: stop and report it to the user.

Compare the `ps` line with the one from the find-the-server snippet, not with `"$CAO_PY"`: when
that snippet used the shebang fallback, the two differ even after a correct restart. A matching
line means the server still runs the Python you installed into. The guide then says what
proves the feature loaded; a log line that only echoes your settings isn't proof.

## Reconnect your MCP connection

Your own MCP connection doesn't refresh on its own. The CAO MCP server is a separate process
that reads settings when it starts, so a tool that a setting turns on only shows up in a
connection opened after the change.

- In Claude Code, run `/mcp` and reconnect the CAO server.
- In other providers, restart the agent's terminal.

**What you should see:** the tool the guide names is in your tool list. If it isn't, check the
setting the guide wrote, then reconnect again. Tell other connected agents that they keep their
old tool list until they reconnect too.
