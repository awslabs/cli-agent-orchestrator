# Set up ephemeral agents and a decider: a runbook an agent can follow

This page is for the case where you ask your agent to "turn on ephemeral agents and set up the
decider for me". It is written so the agent can follow it step by step. It is also plain enough
for you to read along and follow what the agent is doing.

**Why a runbook.** Most of the ways this setup goes wrong are silent:

- the decider gets installed into a different Python environment from `cao-server`, so it never
  loads;
- a later CAO upgrade quietly removes the decider again;
- one provider is missing one model tier, and then every helper is refused, for every provider;
- the agent's own MCP connection is older than the change, so the new tool never shows up, and
  nothing says why.

Each step below ends with a check, so a failure shows up at that step instead of three steps
later.

For what ephemeral agents and decisions are, see [Ephemeral agents](ephemeral-agents.md) and
[Launch decisions](decisions.md). This page only covers getting them set up.

## Before you start

- **Ephemeral agents are experimental.** [Ephemeral agents](ephemeral-agents.md) warns:
  "Keep `ephemeral.enabled` false outside testing. It defaults to false, and stays false until
  the rest of the lifecycle cleanup work (#1079) ships and creating an ephemeral agent from
  inside a workflow step is refused (#1080)." Tell the user this warning and get their explicit
  OK before step 4.
- **The worked example is Jev**, a hosted decider from TypeSafe. Its package is
  `cao-decider-jev`, and it registers the decider name `jev`. CAO doesn't bundle or endorse any
  decider; any other decider package works the same way, with its own name, key and consent
  steps.
- **The package isn't published yet.** A published release is tracked in #1182. Until then, the
  user must give you the source they already have: a git URL, a local checkout or a wheel. If
  they have none, stop. Never guess a source, and never install a bare `jev` from PyPI: that
  name belongs to an unrelated project.
- **You need a CAO build with the decision commands this runbook uses.** A git install of
  `main` has them. PyPI 2.5.4 has an early decision platform with `status`, `set`, `tier`,
  `table`, `exclude`, `tune`, `list` and `purge`, but lacks `enable`, `disable`, `show`, `off`
  and the `installed_deciders` status key. Step 2's probes stop that build, even though a git
  install can print the same version number.
- **macOS or Linux.** On Windows, stop and ask.

### Rules for the agent

1. **Stop at the first failed check** and report it to the user. Don't guess past a refusal.
2. **Ask for the user's explicit OK before** you install into cao-server's environment (step 3),
   write `settings.json` (steps 4, 7, 8 and 9) or restart the shared server (step 6). A restart
   affects every live session, not only yours.
3. **Consent is the user's, never yours.** Never run the consent command, type at its prompt, or
   enter an API key. Hand the user the exact command (step 5).
4. **Never print secrets.** Never run a bare `env` or `printenv`, never echo a value, and never
   `cat` a file that holds a key. Report variable *names* only.
5. Use the exact package name `cao-decider-jev` from this page or from the user, never a short
   name you heard.

If your shell drops variables between commands, start each later command with
`CAO_PY='<path from step 2>';`, or paste the literal path.

## The steps at a glance

1. Back up `settings.json`.
2. Preflight: find the server's Python, and check the build.
3. Install the decider into that Python.
4. Turn on helpers and fill the tier table.
5. Hand the key and consent steps to the user.
6. Restart the server and reconnect your MCP.
7. Watch in shadow.
8. Switch on and run a smoke test.
9. Undo, if needed.

## 1. Back up `settings.json`

```bash
SETTINGS="$(cao config path)"
if [ -f "$SETTINGS" ]; then cp "$SETTINGS" "$SETTINGS.bak.$(date +%s)"; else echo "no settings.json yet: nothing to back up"; fi
```

Keep the backup path for step 9. On a fresh install there's no file, so "restore the backup"
later means removing the `settings.json` this setup created. Once the user confirms the setup is
done, delete the `.bak.*` copies: they are plain copies of an operator-only file.

## 2. Preflight

**Find the Python interpreter the running `cao-server` really uses.** Every later check runs in
it, not in a guessed path and not in your own virtual environment. Take it from the process
listening on the CAO port. Read only the process's command line, never its environment:

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

What you should see: the import succeeds, and the last line prints the same path twice. That
means the `cao` on your `PATH` is that server's own CLI. If the paths differ, or a path has
spaces (which `awk` cuts), stop and ask. If no server was running, tell the user the path is a
guess until step 6 confirms it.

**Check the build has the decision platform.** The version number can't tell you, so probe the
commands this runbook uses:

```bash
cao decisions --help            # exit 0; the list includes enable, disable and off
cao decisions enable --help     # exit 0: the consent command exists
cao decisions off --help        # exit 0: the global stop exists
cao decisions set --help        # exit 0; the usage names model.route and effort.route
cao decisions status            # exit 0; JSON with an "installed_deciders" key
```

If any of these fails, prints "No such command", or the JSON has no `installed_deciders` key, this
build is too old. Report that and stop. The fix is the user's call, for example a git install of
`main`.

**Find out how the server is started:** a foreground terminal, a background process, `launchd`
or a container. The `ps` line above helps. Step 6 restarts it, and CAO won't start one for you.
Ask the user if it isn't obvious.

## 3. Install the decider into the server's Python

The package must land in the **same environment `cao-server` runs from**. The two install
patterns below only apply when that environment is CAO's `uv tool` environment, which is the
common case:

```bash
[ "$("$CAO_PY" -c 'import sys; print(sys.prefix)')" = "$(uv tool dir)/cli-agent-orchestrator" ] \
  && echo "uv tool venv: patterns A/B apply" || echo "NOT the uv tool venv: stop and ask the user"
"$CAO_PY" - "$(uv tool dir)/cli-agent-orchestrator/uv-receipt.toml" <<'EOF'
import re, sys, tomllib
with open(sys.argv[1], "rb") as f:
    requirements = tomllib.load(f)["tool"]["requirements"]
for entry in requirements:  # print only the requirements, with any user:password@ in a URL hidden
    print({k: re.sub(r"//[^/@]*@", "//<redacted>@", v) if isinstance(v, str) else v
           for k, v in entry.items()})
EOF
```

This prints one line per requirement in uv's install receipt, such as
`{'name': 'cli-agent-orchestrator', 'git': 'https://github.com/awslabs/cli-agent-orchestrator.git'}`.
Don't `cat` the receipt: a source URL can carry a credential. If any line shows `<redacted>`,
stop: the user must run the pattern A reinstall themselves, because you can't copy that source.

If it's another environment (a source checkout's `.venv`, pipx, a plain venv), ask the user how
it's managed, and install only the way they say.

**Always name the package together with its source**, as `"cao-decider-jev @ <source>"`. uv then
refuses the install if the source turns out to be a different package (`Package metadata name …
does not match given name …`, or for a wheel, `Requested package name … does not match … in the
distribution filename`). Examples:
`"cao-decider-jev @ git+https://<host>/<repo>.git"` or `"cao-decider-jev @ /path/to/checkout"`.
Once the package is on PyPI, plain `"cao-decider-jev"` works.

Pick a pattern with the user:

| Pattern | Command | Survives a CAO upgrade? | Touches CAO itself? |
| --- | --- | --- | --- |
| A: reinstall the tool with the decider added | `uv tool install <CAO source> <each existing extra> --with "cao-decider-jev @ <source>" --reinstall` | Only for a plain PyPI install upgraded with `uv tool upgrade`. For a git install, `cao update` drops it (see below). | Yes: CAO is reinstalled and may move to a newer version |
| B: add it to the tool's environment | `uv pip install --python "$CAO_PY" "cao-decider-jev @ <source>"` | **No.** The receipt never records it, so any CAO upgrade silently drops it | Not CAO's own package, but it can change dependencies CAO shares (such as `httpx`). Run it with `--dry-run` first and show the user the changes |

**For pattern A, copy the source and the extras from the receipt.** The `cli-agent-orchestrator`
entry in `requirements` gives `<CAO source>`:

| Receipt entry for `cli-agent-orchestrator` | `<CAO source>` |
| --- | --- |
| `git = "<url>"` | `"git+<url>"` |
| `git = "<url>?rev=<ref>"` (uv records a pinned commit, tag or branch this way) | `"git+<url>@<ref>"`. **Don't pass `?rev=` back to uv:** it silently ignores it, installs the latest commit and drops the pin |
| `git = "<url>?<anything else>"` | stop and ask the user |
| `editable = "<path>"` | `--editable "<path>"`. Without `--editable`, uv makes a static copy, and later edits to the checkout stop reaching CAO |
| `directory = "<path>"` or `path = "<path>"` | `"<path>"` |
| `specifier = "<spec>"` | `"cli-agent-orchestrator<spec>"` |
| only `name` | `cli-agent-orchestrator` |
| anything else (extras, a marker, …) | stop and ask the user |

`--with` *replaces* the receipt's extra packages; it doesn't add to them. Repeat every other entry
in its own form (`--with "<name><spec>"`, `--with "git+<url>"`, `--with "<path>"`, or
`--with-editable "<path>"`), then add the decider. Afterwards, re-run the receipt block above. The CAO
entry must keep the same field (`editable` stays `editable`), and every earlier entry must still
be there.

**Verify the decider loads in the server's Python:**

```bash
"$CAO_PY" -c "
from importlib.metadata import entry_points
print([e.name for e in entry_points(group='cao.deciders')])
"
grep -q '"cao-decider-jev"' "$(uv tool dir)/cli-agent-orchestrator/uv-receipt.toml" \
  && echo "recorded" || echo "NOT recorded"
```

What you should see: `['fixed_table', 'jev']`. `fixed_table` is CAO's built-in decider. Then
`recorded` after pattern A, or `NOT recorded` after pattern B. If `jev` is missing, the package
went into the wrong environment.

**Tell the user about upgrades.** The check above shows that the decider loads *now*, and
whether the receipt records it. It can't promise the decider survives the next upgrade, and
often it won't:

- `cao update` on a git install runs `uv tool install <git source> --upgrade --reinstall` without
  any `--with`, so it removes the decider even after pattern A;
- on a local install, `cao update` changes nothing and prints a reinstall command that has no
  `--with` either;
- after pattern B, any upgrade removes it.

So the user (or you, with their OK) must re-run the check above after **any** CAO upgrade or
reinstall, reinstall the decider if `jev` is missing, and restart the server. This is a current
CAO limitation.

## 4. Turn on helpers and fill the tier table

Both parts write `settings.json`, so get the user's OK first.

**The helper settings.** `cao config set` writes keys in the `ephemeral` block and keeps the rest
of the file:

```bash
cao config set ephemeral.enabled true
cao config set ephemeral.allowed_providers '["claude_code","codex"]'
cao config set ephemeral.min_tier small
cao config set ephemeral.max_tier large
cao config set ephemeral.default_tier medium
```

Each line prints the value it stored. Only list the providers the user actually wants. For every
key and its default, see [Operator settings](ephemeral-agents.md#operator-settings).
`cao config set` doesn't validate these keys, and a misspelled key is stored and then ignored.
The loader check below catches both problems.

**The tier table.** Each size, for each allowed provider, maps to one real model id:

```bash
cao decisions tier claude_code small  <small-model-id>
cao decisions tier claude_code medium <medium-model-id>
cao decisions tier claude_code large  <large-model-id>
cao decisions tier codex small  <small-model-id>
cao decisions tier codex medium <medium-model-id>
cao decisions tier codex large  <large-model-id>
```

Ask the user for the model ids. They depend on the login this CAO uses, so never copy them from
another account or from an example. Codex logins sometimes need a qualified id such as
`global.openai.<name>`. `tier` refuses an unknown provider or a malformed model id with "invalid
provider, tier or model", and an unknown tier with a usage error (`Invalid value`). A wrong id
that looks valid only fails later, when a helper launches. To remove a
mapping, use `cao decisions tier <provider> <tier> --unset`.

**The trap.** Every tier from `min_tier` to `max_tier`, plus `default_tier`, must be mapped for
**every** allowed provider. One gap refuses every helper for every provider.

**Check both parts with CAO's own loader**, run in the server's Python:

```bash
"$CAO_PY" -c "
from cli_agent_orchestrator.services.ephemeral_service import read_settings, validate_block, EphemeralPolicyError
try:
    block = read_settings()
    validate_block(block)
    print('OK')
    print('unknown keys (ignored):', block['_unknown_keys'])
except EphemeralPolicyError as e:
    print('rule:', e.rule, '| detail:', e.detail)
"
```

What you should see: `OK`, then `unknown keys (ignored): []`.

- `rule: policy_config_error:model_tiers | detail: model_tiers.codex.medium is not mapped (required
  by policy)` names the exact gap. Map it, and run the check again.
- A non-empty unknown-keys list, such as `['enabeld']`, is a misspelled key. Fix the name.
- If you can't make the check pass, restore the step 1 backup rather than leave the file broken.

To review the whole table, run
`cao decisions status | "$CAO_PY" -c "import json,sys; print(json.dumps(json.load(sys.stdin)['model_tiers'], indent=2))"`.

## 5. Hand the key and consent steps to the user

**These are the user's steps. Don't run them, and don't type into them.** The consent command
needs an interactive terminal, so that no agent can drive it.

Give the user this block to run in their own terminal:

```bash
cao-decider-jev key set          # hidden prompt: paste the TypeSafe API key there, never on a command line
cao-decider-jev key check --auth # should end with {"status": 200, "alias_listed": true}
cao decisions enable jev         # shows what Jev sends and where; type api.typesafe.ai to agree
```

For **both patterns A and B**, give the user the full path
`"$(dirname "$CAO_PY")/cao-decider-jev"` in place of each bare `cao-decider-jev` in the block
above. uv links only the main package's executables, so neither pattern puts the decider
executable on `PATH`. `enable` ends with:

```text
Consent granted; effective states:
{"effort.route": "off", "model.route": "shadow", "provider.route": "off"}
```

The exact list of points can grow with CAO versions; what matters is `"model.route": "shadow"`.

**What you may check yourself.** These two commands read state without showing the key:

```bash
"$(dirname "$CAO_PY")/cao-decider-jev" key check   # shape only: present, length and so on; never the value
cao decisions show jev                              # Jev's disclosure and status, no secrets
```

For another decider, use that package's own key-check and status commands. Don't guess an
environment variable name, and don't use `cao env list`: it prints values. Jev's own README is
the authority for its key, consent and revoke commands.

## 6. Restart the server and reconnect your MCP

`cao-server` only looks for deciders when it starts. Get the user's OK, then restart it the way
step 2 found it running:

- a foreground terminal: Ctrl-C, then start it again;
- a background process: stop it, then start it again;
- `launchd`: `launchctl kickstart -k gui/$(id -u)/<label>`, with the job label from the user
  (`-k` stops the running instance first; without it, kickstart leaves a running job alone).

Find the process by its port, never by matching command-line text: your own shell can match that
text too. Then check the restart:

```bash
PORT="${CAO_API_PORT:-9889}"
curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null && echo "server answers"
PID="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t | head -n 1)"
ps -o pid,lstart,etime -p "$PID"                   # the start time must be after your install
ps -o args= -p "$PID"                              # must match the ps line you saw in step 2
"$(dirname "$CAO_PY")/cao" decisions status | "$CAO_PY" -c "import json,sys; print([d['name'] for d in json.load(sys.stdin)['installed_deciders']])"
```

The `ps` line must be the same command as in step 2, so the server still runs the Python you
installed into. Compare it with step 2's `ps` line, not with `"$CAO_PY"`: when step 2 used the
shebang fallback, the two differ even after a correct restart. `jev` must be in the last list.
The server log line `points=model.route=shadow:jev` isn't proof:
it shows the binding, which reads the same whether or not the package loads. The real proof
comes in step 7.

**Your own MCP connection doesn't refresh on its own.** The CAO MCP server only offers
`create_ephemeral_agent` if `ephemeral.enabled` was already true when the MCP process started.
In Claude Code, run `/mcp` and reconnect the CAO server. In other providers, restart the agent's
terminal. Confirm `create_ephemeral_agent` is in your tool list before you go on.

## 7. Watch in shadow

In **shadow**, `model.route` asks the decider for every helper launched with `auto`, and every
normal delegation to an installed profile that omits a model. Both send the redacted task
message to the decider. CAO records the answer, but helpers still run on `default_tier` and
installed profiles keep their existing model. Jev's consent already put `model.route` there.
Use `cao decisions exclude --add <profile>` to keep an installed profile on its own model and
skip its `model.route` calls and records. This writes settings, so get the user's OK first.
Bind effort routing too, if the user wants it (also a settings write):

```bash
cao decisions set model.route shadow --decider jev
cao decisions set effort.route shadow --decider jev
```

Let the user's work create a few helpers with `model_tier: "auto"` **and assign or hand off a
task to each** (creating alone writes no record), then read the records:

```bash
cao decisions list --point model.route --limit 10
```

What you should see: each record has `outcome: "shadow"` and no `reason`. In shadow,
assign and handoff results carry no note; that's normal.

- Even one `decider_unavailable` means the server couldn't load the decider. Go back to steps 3
  and 6.
- `timeout`, `error`, `invalid_answer` or `no_answer`, repeated on more than one task, needs a
  look before you switch on. `cao decisions show jev` names the cause, for example a missing key
  or missing consent.
- `fallback` with `out_of_scope` comes from script workflow steps, which deciders never size.
  Ignore it here.

## 8. Switch on and run a smoke test

In **on** mode, `model.route` covers every helper launched with `auto`, and every normal
delegation to an installed profile that omits a model. Those delegations may run on a
different model: the installed profile's model is a fallback and may be overridden. Tell the
user this before switching on. Use `cao decisions exclude --add <profile>` to keep a profile
on its own model (a settings write, so get the user's OK).

When the user agrees, switch on:

```bash
cao decisions set model.route on --decider jev
cao decisions set effort.route on --decider jev   # only if they want effort routing
```

Then create three helpers with `create_ephemeral_agent` (or `POST /ephemeral-agents`), all with
`model_tier: "auto"` (and `effort: "auto"` if effort routing is on): one small, one medium and
one large task. **Assign or hand off a real task to each one**, because the note lands on the
assign or handoff, not on the create. Read the result in two places:

1. **The note** in the delegation result, or the `X-CAO-Decision` response header. For example,
   `ran on haiku (auto: small 1.00)` or `ran on model-default (auto: fallback, unsure)`. Every
   shape is listed in [Delegation results](decisions.md#delegation-results).
2. **The records**, with `cao decisions list --point model.route --limit 10`. `outcome` should be
   `applied`, `capped` or `fallback`; on a fallback, read `reason`.

A `fallback` with `low_confidence` on a borderline task is normal: the confidence was below the
0.7 threshold. `timeout`, `error` or `decider_unavailable` on more than one task is not. Read
both places before you call the setup done: a note can look right while the decider falls back
every time.

**Provider caveats to tell the user:**

- **Codex** model ids depend on the login. Tool lists are advisory for Codex helpers (they run
  with a shell), while Claude Code and Kiro v3 enforce them.
- **Kiro v3** refuses unsupported `kiro-cli` versions with `kiro_v3_unsupported_version` and
  pending, unverified versions with `kiro_v3_unverified_version`; the creating agent receives
  the specific rule and its remedy. Use a supported version for an unsupported refusal, or a
  verified version for an unverified refusal. `kiro-cli` 3.1.0 is pending: it is refused by
  default and only the operator can admit it with `kiro.v3_allow_unverified_versions`. Older
  CAO builds returned a bare `unexpected_failure`, with the real reason only in the server log.

## 9. Undo

Each of these writes settings or restarts the server, so get the user's OK.

1. Stop every point at once. This waits for in-flight decider calls:
   ```bash
   cao decisions off
   ```
   It stays in force until a point is set again.
2. Put `settings.json` back the way it was. With the server stopped, restore the step 1 backup;
   if step 1 found no file, delete the `settings.json` this setup created:
   ```bash
   SETTINGS="$(cao config path)"
   ls "$SETTINGS".bak.*                           # pick the one step 1 made
   cp "$SETTINGS.bak.<timestamp>" "$SETTINGS"     # or, if there was no backup: rm "$SETTINGS"
   ```
   If the user wants to keep later, unrelated settings changes, undo only this setup's keys
   instead: `cao config set ephemeral.enabled false`, then
   `cao decisions tier <provider> <tier> --unset` for each mapping this setup added.
3. Start the server again (step 6's checks), and tell other connected agents that they keep
   seeing `create_ephemeral_agent` until they reconnect MCP.

These are **the user's** steps:

- revoke consent, with `cao decisions disable jev`;
- delete the key, with `cao-decider-jev key delete`;
- uninstall the package: run pattern A again without the decider's `--with`, or
  `uv pip uninstall --python "$CAO_PY" cao-decider-jev` for pattern B.

Delete the `settings.json.bak.*` files once the user no longer needs them.

## Symptom → fix

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `cao decisions` says "No such command", or `enable`/`off` are missing | The build lacks the commands this runbook needs; PyPI 2.5.4 has an early version without consent and stop commands | Stop. The user installs a build with these commands, such as a git install of `main` |
| uv says `Package metadata name … does not match given name`, or `Requested package name … does not match … in the distribution filename` | The source isn't the package you named | Stop and ask the user for the right source |
| `jev` is missing from the entry-point list | It was installed into a different Python | Re-run step 2, then reinstall with `--python "$CAO_PY"` |
| It worked yesterday; now every launch falls back with `decider_unavailable` | A CAO upgrade rebuilt the tool and dropped the decider | Re-run step 3's check and reinstall, then restart |
| Every create fails with `policy_config_error:model_tiers` | One allowed provider is missing a tier (the detail names it) | Map that tier, and re-run step 4's loader check |
| An `ephemeral.*` change seems to do nothing | A misspelled key, which CAO ignores | Step 4's loader check lists unknown keys; fix the name |
| `create_ephemeral_agent` is missing from your tools | Your MCP connection is older than `ephemeral.enabled: true` | `/mcp` → reconnect in Claude Code, or restart your terminal |
| `cao decisions enable jev` says it needs an interactive terminal | An agent ran it | Correct: hand the command to the user (step 5) |
| Shadow records show `no_answer` | No consent or no key yet | `cao decisions show jev` names which; the user fixes it (step 5) |
| A Kiro helper launch is refused with `kiro_v3_unsupported_version` or `kiro_v3_unverified_version` | Its version is unsupported or pending operator verification | Follow the refusal's remedy; for pending versions, use a verified version or ask the operator to opt in (step 8) |
| A medium task falls back with `low_confidence` | Confidence was below 0.7 | Normal. Investigate only if it keeps happening on clear-cut tasks |

## FAQ

**Can the agent do the whole thing?** Everything except the key and consent (step 5), which the
user does in their own terminal, and the OKs before installs, settings writes and restarts.

**Do I have to use Jev?** No. Any package that registers a `cao.deciders` entry point works.
Swap in its package name, decider name and its own key and consent commands.

**Does shadow cost anything?** Yes: `model.route` asks the decider for every helper launched
with `auto`, and every normal delegation to an installed profile that omits a model. Both
send the redacted task message, even though the answer isn't applied. That's the price of
seeing its picks first. Use `cao decisions exclude --add <profile>` to keep an installed
profile on its own model and skip those calls (a settings write, so get the user's OK).

**What does Jev receive?** `cao decisions show jev` prints the full disclosure, which is what
the user agrees to. For each delegation, one request per point that is in shadow or on goes to
TypeSafe (`api.typesafe.ai`, US-hosted). It carries:

- the delegated task message, after CAO's secret redaction;
- for a helper, its purpose and creator-written description (redacted); for a normal
  delegation, the target profile's name and its description (redacted);
- fixed question wording and the model alias;
- the user's API key, their IP address, and a User-Agent naming the package.

It doesn't send the helper's brief, the profile's role, the provider, working directories, file
contents, terminal, session or request ids, or decision records. Redaction is a list of patterns,
not a guarantee, and TypeSafe keeps the data unless the user has a zero-retention agreement.
