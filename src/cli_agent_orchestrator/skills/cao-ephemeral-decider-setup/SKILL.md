---
name: cao-ephemeral-decider-setup
description: Set up ephemeral agents and an installed decider (worked example
  cao-decider-jev; also works for any other installed decider package) for a
  user, end to end, on an existing CAO install. Covers preflight, installing
  the decider into cao-server's own environment, the per-provider tier table,
  a safe edit of the ephemeral settings block, handing consent to the human,
  the server/MCP restart, shadow vs on, a smoke test, provider caveats and
  undo. Use when a user asks an agent to "set up a decider" or "turn on
  ephemeral agents", not when choosing whether to delegate to one (see
  cao-agent-routing) or authoring a decider package.
---

# CAO Ephemeral Agent + Decider Setup

Setting this up by hand is easy to get wrong in ways that fail silently: a decider installed into
the wrong Python environment, or under the wrong package name, looks fine until the next CAO
upgrade quietly drops it or an unrelated package runs instead; one missing tier for one provider
refuses every provider's launches; a stale MCP connection fails with no hint that a restart would
fix it. This skill walks an agent through every step in order, up to the one step that is
human-only: granting consent.

**Decider-generic.** Everything below works for any installed decider package; `cao-decider-jev`
(entry-point name `jev`) is the worked example. CAO bundles and endorses no decider. A published
`cao-decider-jev` release is tracked in #1182 — until then, install from whatever source the user
already has (a git checkout, a wheel, a private index), never one this skill invents. **Never
resolve a decider by a bare, guessed name from PyPI** — see step 3.

**Scope.** This skill is setup mechanics only. Whether to delegate to an ephemeral agent at all is
`cao-agent-routing`'s job. Authoring a decider package is out of scope entirely.

## Order of operations

1. [Back up `settings.json`](#1-back-up-settingsjson)
2. [Preflight](#2-preflight)
3. [Install the decider into cao-server's environment](#3-install-the-decider-into-cao-servers-environment)
4. [Build the complete tier table](#4-build-the-complete-tier-table)
5. [Edit the `ephemeral` settings block safely](#5-edit-the-ephemeral-settings-block-safely)
6. [Hand consent to the human](#6-hand-consent-to-the-human)
7. [Restart the server, reconnect MCP](#7-restart-the-server-reconnect-mcp)
8. [Shadow versus on](#8-shadow-versus-on)
9. [Smoke test](#9-smoke-test)
10. [Provider caveats](#10-provider-caveats)
11. [Undo](#11-undo)
12. [Safety](#12-safety)

Stop and report back to the user at the first failed check in any step — don't guess past a
refusal. Get the user's explicit OK before **any** step that writes `settings.json` or restarts the
shared server (steps 4, 5, 7, 8 and 11) — see [Safety](#12-safety).

## 1. Back up `settings.json`

Before touching anything, so every later step has a real restore point:

```bash
SETTINGS="$(cao config path)"
if [ -f "$SETTINGS" ]; then cp "$SETTINGS" "$SETTINGS.bak.$(date +%s)"; else echo "no settings.json yet: nothing to back up"; fi
```

Keep this path for [Undo](#11-undo). On a fresh install there is no file yet, so "restore the
backup" later means removing the `settings.json` this setup created. Remove the `.bak.*` copies once
the user confirms the setup is done — they're plaintext copies of an operator-only file.

## 2. Preflight

**The printed version number does not tell you whether this build supports ephemeral agents or
decisions.** A git install of `main` and a published PyPI release can report the identical `cao
--version` string while one has the code and the other does not, and a PyPI release can simply
predate the decision platform entirely (at the time of writing, the latest PyPI release, 2.5.4, has
none of the commands probed below). Checking `--version` alone is the mistake to avoid.

**First, find the interpreter the running `cao-server` really uses** — every later check runs in
it, never in a guessed path or the agent's own `uv run` venv. Take it from the process listening on
the CAO port (`CAO_API_PORT` if this install sets it, else `9889`). Read only the process's command
line, never its environment:

```bash
PORT="${CAO_API_PORT:-9889}"
PID="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t | head -n 1)"
if [ -n "$PID" ]; then
  ps -o args= -p "$PID"   # e.g. /path/to/venv/bin/python3 /path/to/cao-server
  CAO_PY="$(ps -o args= -p "$PID" | awk '{print $1}')"
else
  echo "no cao-server on port $PORT: using the uv tool path, unconfirmed"
  CAO_PY="$(uv tool dir)/cli-agent-orchestrator/bin/python3"
fi
"$CAO_PY" -c 'import sys, cli_agent_orchestrator as c; print(sys.prefix); print(c.__file__)'
readlink -f "$(command -v cao)"; readlink -f "$(dirname "$CAO_PY")/cao"
```

The import must succeed, and the last line must print one path twice (the `cao` on `PATH` is that
server's own CLI). Otherwise — or if the path has spaces and `awk` cut it — stop and ask the user.
If no server was running, tell the user the fallback is a guess until step 7 confirms it. If your
shell drops variables between commands, start each later one with `CAO_PY=<that path>`. These are
POSIX-shell commands; on Windows, stop and ask.

Then probe for every command and field this skill goes on to use, and stop at the first one
that fails — passing some but not all of these still means later steps fail confusingly instead of
clearly:

```bash
cao decisions --help            # exit 0, command list includes enable/disable/off
cao decisions enable --help     # exit 0: the human-consent command exists
cao decisions off --help        # exit 0: the all-points halt exists
cao decisions set --help        # exit 0, usage names model.route and effort.route
cao decisions status            # exit 0, JSON with an "installed_deciders" key
```

If any of these exits non-zero, prints "No such command", or the `status` JSON has no
`installed_deciders` key, this build lacks the decision platform this skill relies on: report that
and stop. A build with `status` but without `enable`/`off`/`installed_deciders` is the "passes
preflight, fails later" trap these probes catch.

Then confirm how `cao-server` is started (a foreground terminal, a background process, `launchd`, a
container; the `ps` line helps): step 7 restarts it, and CAO does not start a server for you. Ask
the user if it isn't obvious.

## 3. Install the decider into cao-server's environment

**Never let an agent resolve a decider package from a bare, guessed name.** PyPI can (and in the
case of the worked example, does) already host an unrelated package under the short name a user
might say out loud. Use only the decider's exact distribution name as given by the user or the
package's own docs — for the worked example that is **`cao-decider-jev`** (import module
`cao_decider_jev`, `cao.deciders` entry-point name `jev`), never the bare name `jev`. Where the
decider isn't published anywhere reachable yet, say so plainly and point to #1182 — don't invent a
source.

The decider package must load inside the **same Python environment `cao-server` runs from** —
`"$CAO_PY"` from step 2, not whatever environment the agent's own shell has active. Patterns A and B
below only apply when that environment is CAO's `uv tool` venv (the common case):

```bash
[ "$("$CAO_PY" -c 'import sys; print(sys.prefix)')" = "$(uv tool dir)/cli-agent-orchestrator" ] \
  && echo "uv tool venv: patterns A/B apply" || echo "NOT the uv tool venv: stop and ask the user"
cat "$(uv tool dir)/cli-agent-orchestrator/uv-receipt.toml"
```

If it isn't the uv tool venv (a source checkout's `.venv` under `uv run`, pipx, a plain venv), stop
and ask the user how that environment is managed; install the decider only the way they say.

| Pattern | Command | Survives `cao update` / `uv tool upgrade`? | Also touches CAO itself? |
| --- | --- | --- | --- |
| A: re-run the tool install with the receipt's own source and every `--with` | `uv tool install <CAO source> <each existing extra> --with "<decider-dist>" --reinstall`, mapped from the receipt as below | **Only for an unpinned PyPI install.** `uv` records every `--with` package in the receipt, and a plain `uv tool upgrade` (or `cao update` on an unpinned registry install) reinstalls it too. **For a git or version-pinned install, `cao update` rebuilds the receipt without `--with` at all**; for a local install it refuses and prints a reinstall hint that has no `--with` either (a current CAO gap — see below) | Yes — this reinstalls CAO itself, which may upgrade it unintentionally |
| B: `uv pip install` straight into the tool's venv | `uv pip install --python "$CAO_PY" "<decider-dist>"` | **No.** The receipt never records it, so the next `uv tool upgrade` (or any `cao update`) rebuilds the venv from the receipt alone and silently drops the decider | No — leaves CAO's own version untouched |

**Pattern A must keep the install's source and its extras.** Read the receipt's `requirements`
list. The `cli-agent-orchestrator` entry gives `<CAO source>`:

| Receipt entry for `cli-agent-orchestrator` | `<CAO source>` |
| --- | --- |
| `git = "<url>"`, maybe with `rev`/`tag`/`branch` | `"git+<url>"`, or `"git+<url>@<rev>"` |
| `editable = "<path>"` | `--editable "<path>"` — without `--editable`, uv turns it into a static copy and later edits to the checkout silently stop reaching CAO |
| `directory = "<path>"` or `path = "<path>"` | `"<path>"` |
| `specifier = "<spec>"` | `"cli-agent-orchestrator<spec>"` |
| only `name` | `cli-agent-orchestrator` |
| any other field (extras, a marker, …) | none: stop and ask the user |

`--with` *replaces* the receipt's extra requirements, it doesn't add to them. So repeat every other
entry in the list in its own form — `--with "<name><spec>"`, `--with "git+<url>"`, `--with
"<path>"`, or `--with-editable "<path>"` for an `editable` one — then add the new decider. After
the reinstall, re-read the receipt: the CAO entry must keep the same field (`editable` stays
`editable`), and every earlier entry must still be there.

**Known CAO gap, not just a skill caveat:** `cao update` runs `uv tool install <git-source>
--upgrade --reinstall` for a git install and `uv tool install cli-agent-orchestrator@latest
--upgrade` for a pinned one; neither passes `--with` (`cli/commands/update.py`, `_build_command`).
For a local install (`editable`/`directory`/`path`) it changes nothing and prints a `uv tool install
… --reinstall` hint, which drops every `--with` if run as printed. So after **any** CAO upgrade or
reinstall, re-run the entry-point check below. Tell the user this is a CAO limitation, and suggest
filing or finding a tracking issue.

Pick A when the user accepts CAO being reinstalled (maybe upgraded) too. Pick B to leave CAO's
version untouched — but warn that **any** CAO upgrade then needs the decider reinstalled.

**Verify the install really landed where `cao-server` loads from — by entry point, never by
guessing an import name:**

```bash
"$CAO_PY" -c "
from importlib.metadata import entry_points
print([e.name for e in entry_points(group='cao.deciders')])
"
```

`jev` must appear in that list. An empty list, or an import error, means the decider went into the
wrong interpreter. The running server only picks it up after the restart in step 7.

After pattern A, this confirms the receipt recorded the decider — by the exact quoted name, since
`grep -q jev` would also match an unrelated `cao-decider-jev-extra`. Pattern B is never recorded,
so it prints "NOT recorded": reinstall after every CAO upgrade. Either way, re-run the entry-point
check after any `cao update`:

```bash
grep -q '"<decider-dist>"' "$(uv tool dir)/cli-agent-orchestrator/uv-receipt.toml" \
  && echo "recorded" || echo "NOT recorded"
```

## 4. Build the complete tier table

`cao decisions tier` maps one `(provider, tier)` pair to one model ID. **A missing tier anywhere in
`allowed_providers` refuses *every* provider's create**, not just the one with the gap — the single
most confusing failure mode in the whole setup. The refusal below names the exact missing
`provider.tier`, so once you see it the fix is mechanical; the danger is not expecting the gap to
block unrelated providers at all.

```bash
cao decisions tier claude_code small  <small-model-id>
cao decisions tier claude_code medium <medium-model-id>
cao decisions tier claude_code large  <large-model-id>
cao decisions tier codex small  <small-model-id>
cao decisions tier codex medium <medium-model-id>
cao decisions tier codex large  <large-model-id>
cao decisions tier codex large --unset     # remove a mapping
```

Model IDs depend entirely on the login this CAO install is using — never reuse IDs from another
account or a doc example. `cao decisions tier` refuses an outright-invalid provider, tier or model
ID when you set it; a wrong-but-well-formed ID instead surfaces later, at launch (see step 10 for
Codex). For each provider actually in `ephemeral.allowed_providers`, every tier from
`ephemeral.min_tier` (or `small` if unset) through `ephemeral.max_tier` must be mapped, plus
`ephemeral.default_tier`. (Normal, non-ephemeral delegation has no `allowed_providers` envelope of
its own — don't look for one at the installed-profile level.)

Confirm the table is complete before touching anything else:

```bash
cao decisions status | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)['model_tiers'], indent=2))"
```

A gap anywhere here means every ephemeral create for every allowed provider is refused with
`policy_config_error:model_tiers`, detail `model_tiers.<provider>.<tier> is not mapped (required by
policy)` — naming the exact gap. This check runs on **every** ephemeral create and claim already,
whenever `min_tier`/`max_tier`/`default_tier` are set, not only after decisions are later turned on
— fix it now, not after step 8.

## 5. Edit the `ephemeral` settings block safely

**There is no validated CLI for most of this block.** `cao config set ephemeral.<key> <value>`
writes a scalar key straight into `settings.json` (verified: creates `{"ephemeral": {"enabled":
true}}`, read back by `cao config get`) — prefer it over a hand edit for one scalar key such as
`enabled`. It also writes a JSON list as a real list (`cao config set ephemeral.allowed_providers
'["claude_code","codex"]'`). But it does **not** validate the value against CAO's own policy rules,
and doesn't show up in `cao config list`, so a typo'd key name writes successfully and silently
does nothing — the check below names it. Prefer `cao config set` for every key; hand-edit the file
only when you can't use it. Get the user's explicit OK before touching this file either way — the
backup you need is already in place from step 1.

1. Edit only the `ephemeral` object. Keep every other top-level key as-is — `model_tiers`,
   `decisions`, and anything else in the file must survive untouched. At minimum you are likely
   setting:
   ```json
   {
     "ephemeral": {
       "enabled": true,
       "allowed_providers": ["claude_code", "codex"],
       "min_tier": "small",
       "max_tier": "large",
       "default_tier": "small"
     }
   }
   ```
   merged into (not replacing) the existing `ephemeral` object and the rest of the file. See
   [Operator settings](../../docs/ephemeral-agents.md#operator-settings) for the full key list and
   defaults.
2. Verify the result with CAO's own loader, run in `cao-server`'s own interpreter (`"$CAO_PY"`
   from step 2), never the agent's own `uv run` venv:
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
   Print only `OK` and the unknown key *names*, or the `rule`/`detail` pair — never the whole
   settings dict. A non-empty unknown-keys list is a typo'd or unsupported key: CAO ignores it
   (the server only logs a warning on create), so fix its name. A `policy_config_error:<key>`
   means fix the named key and re-run this check before restarting the server — a broken block
   refuses every ephemeral create and claim. It also re-checks step 4's tier table.
3. If the check fails and you can't make it pass, restore the backup from step 1 rather than
   leaving the file broken.

## 6. Hand consent to the human

**Consent is human-only. An agent must never run the consent command, type into its prompt, or set
an API key on the user's behalf.** `cao decisions enable <name>` (which runs the decider's own
consent command) needs an interactive TTY precisely so that an agent with shell access cannot drive
it non-interactively.

Give the user this exact block to run themselves, in a real terminal:

```bash
cao decisions enable jev
```

If the decider needs an API key and doesn't have one yet, the decider package's own hidden-prompt
command is also theirs to run — check its own docs or `--help` for the exact subcommand. Run the
console script directly, by its full path in the tool's own venv (normally *not* on the agent's
`PATH` unless the install used `--with-executables-from`), or through CAO's own forwarding form for
its status/consent/revoke surface (`cao decisions show <name> -- --help`, forwarding to the
package's `status` subcommand — see [Decider packages](../../docs/decisions.md#decider-packages)):

```bash
"$(dirname "$CAO_PY")/cao-decider-jev" --help
```

You may check **whether** a key is present without ever seeing or printing its value — using the
decider's own status/key-check command, not a guess at an environment variable name (`cao env list`
prints every managed variable's *value*, and nothing in CAO's own environment layer is specific to
any one decider). For the worked example, that is the package's own shape-only check (presence and
length, never the value):

```bash
"$(dirname "$CAO_PY")/cao-decider-jev" key check
```

Use the equivalent for whatever decider is actually installed — check its own `--help` or docs for
the exact subcommand; don't invent an environment-variable name for it. Never echo a value, pipe a
key-bearing file through `cat`, or print the environment with a bare `env`/`printenv` (see
[Safety](#12-safety)).

Consent itself doesn't turn routing on, but a decider's consent command may also set a point: Jev's
sets `model.route` to `shadow`, and leaves `effort.route` off. Moving points is step 8. It is not a
consent action, so the agent may run it — but only with the user's OK, like every other
settings write in this skill.

## 7. Restart the server, reconnect MCP

A newly installed decider is discovered at `cao-server` startup, not live. Get the user's OK, then
restart it the way it's actually running (found in step 2: foreground gets Ctrl-C then relaunched,
background gets killed and relaunched, `launchd` gets `launchctl kickstart`). Find the process by
the port it listens on (step 2's `PORT`), never by matching process arguments — an unrelated
process, including this agent's own shell, can share the same command-line text:

```bash
PORT="${CAO_API_PORT:-9889}"
curl -sf "http://127.0.0.1:${PORT}/health"        # exit 0: something is listening
PID="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t | head -n 1)"
ps -o pid,lstart,etime -p "$PID"                   # start time newer than your restart
ps -o args= -p "$PID" | awk '{print $1}'           # must still equal "$CAO_PY"
```

Then confirm what the restarted server sees. It lists the `cao.deciders` entry points of its own
environment once, at startup, so with the same interpreter and a start time after the install, it
sees what that interpreter's own `cao` reports:

```bash
"$(dirname "$CAO_PY")/cao" decisions status | "$CAO_PY" -c "import json,sys; print([d['name'] for d in json.load(sys.stdin)['installed_deciders']])"
```

`jev` must be in the list. If step 2 fell back to the uv tool path, re-run step 2's interpreter
check now that a server is up. Don't use the server log as proof: its `Decision settings: …
points=model.route=shadow:jev` line shows the *binding*, which reads the same whether or not the
package is installed. The final proof is in step 8: after a real launch in shadow, no record has
`reason: decider_unavailable`.

**Your own MCP connection does not auto-refresh.** The CAO MCP server process this agent's own
terminal is talking to registers `create_ephemeral_agent` once, at its own startup, and only if
`ephemeral.enabled` was already true at that moment — a `cao-server` restart by itself is not the
cause; an MCP process that started (or last reconnected) *before* `ephemeral.enabled` became true
is what's stale, and that can be true with no server restart at all. In Claude Code, send `/mcp`
and reconnect the CAO server; in other providers, restart the agent's own terminal/session. Confirm
the tool is actually visible before declaring this step done. The same applies after
[Undo](#11-undo): other already-connected sessions keep seeing the tool until they reconnect.

## 8. Shadow versus on

`cao decisions set model.route <state> --decider <name>` and the matching `effort.route` call move
a point between three states — `off` (nothing asked, nothing recorded), `shadow` (the decider's
answer is recorded but never applied — the child always runs on the fallback tier), and `on` (the
decider's answer is applied, within `[min_tier, max_tier]`, if confident). Get the user's OK before
this step too — it's a settings write like step 5, even though running it isn't itself a consent
action.

Always land in `shadow` first (Jev's consent may already have put `model.route` there — see step
6; the command is safe to repeat):

```bash
cao decisions set model.route shadow --decider jev
cao decisions set effort.route shadow --decider jev
```

Let a few real launches happen, then read them **the shadow way**, not the "on" way — in shadow
there is no assign/handoff note at all (the note only appears once a point is `on`), and every
record's `outcome` reads `shadow`, never `applied`/`fallback`:

```bash
cao decisions list --point model.route --limit 10
```

Check that `outcome` is `shadow` with no failure `reason` present. This is the server-side proof
step 7 promised: these records are written by the running server. Even one `decider_unavailable`
means that server could not load the decider (cached until its next restart) — go back to steps 3
and 7. A `timeout`, `error`, `invalid_answer`, or `no_answer` reason, repeated across more than one
task, is worth investigating before flipping to `on` — an all-`shadow`, no-note result is the expected
shape of a working shadow phase, not a sign nothing is happening.

Once the user agrees to flip to `on`:

```bash
cao decisions set model.route on --decider jev
cao decisions set effort.route on --decider jev
```

and re-read with step 9's reading, which does have a note and does distinguish `applied`/`capped`
from `fallback`.

`cao decisions off` is the one command that halts every point at once, including a running server,
and waits for in-flight decider calls to drain — reach for it over three separate `set … off` calls
when something looks wrong (see [Undo](#11-undo)).

## 9. Smoke test

Create a small, a medium and a large ephemeral task, all with `model_tier: "auto"` (and `effort:
"auto"` if that's routed too). Run this only after step 8's flip to `on` — reading it during shadow
hides both signals below. Create each helper through `create_ephemeral_agent` or `POST
/ephemeral-agents`, then **assign or handoff an actual task to it** — the note below lands on that
assign/handoff, not on the create call. Read the result two ways:

1. **The assign/handoff note**, in the delegation result or the `X-CAO-Decision` response header,
   e.g. `ran on model-small (auto: small 1.00)` or `ran on model-default (auto: fallback, unsure)`.
   See [Delegation results](../../docs/decisions.md#delegation-results) for every note shape.
2. **`cao decisions list`**, to confirm the record itself:
   ```bash
   cao decisions list --point model.route --limit 10
   ```
   Check `outcome` (`applied`, `capped`, or `fallback`) and, on a fallback, `reason`.
   `low_confidence` (confidence under `confidence_threshold`, default `0.7`) is an expected, normal
   fallback reason for a borderline task — not a bug to chase. A `timeout`, `error`, or
   `decider_unavailable` reason, repeated across more than one task, is worth investigating before
   calling the setup done.

Declaring the setup finished without reading both of these is the smoke test's own common failure:
a note that looks right can still hide a decider that is actually failing and falling back every
time.

## 10. Provider caveats

- **Codex model IDs are per-login, and sometimes need a qualified form** (`global.openai.<name>`).
  Use exactly what the user's own Codex login accepts — never reuse IDs from another account — and
  re-verify the table (step 4) after any account change. Step 4's `tier` command still refuses an
  outright-malformed ID; a *wrong but well-formed* ID for the signed-in account instead surfaces
  later, as a launch failure, not a tier refusal.
- **Kiro's v3 engine refuses an un-allow-listed `kiro-cli` version** before a helper launches, with
  `kiro_v3_unsupported_version` (no pinned schema for that version) or `kiro_v3_unverified_version`
  (allow-listed, pending operator verification). If the refused version is *newer* than the
  allow-listed ones, pin `kiro-cli` back down or wait for CAO to add support — "upgrade" is usually
  the wrong direction. Lifting `kiro_v3_unverified_version` needs the operator's own
  `kiro.v3_allow_unverified_versions` opt-in in `settings.json`; this agent must never set that key
  itself. The failure can surface as a bare `unexpected_failure`, with the real reason only in the
  server's own log.
- **Provider tool enforcement differs.** `claude_code` is `hard` (native refusal, no
  subagents/Monitor/Skill/plan mode). `codex` is `advisory` (the child runs `--yolo` with a shell;
  the tool list is a suggestion). `kiro_cli` is `hard` on v3, enforced through a per-launch agent
  file. Don't describe Codex's list as enforced to a user — it isn't.

## 11. Undo

Every command here writes `settings.json` or restarts the server, so get the user's OK first:

```bash
cao decisions set model.route off
cao decisions set effort.route off
# or, to halt everything at once and wait for in-flight calls to drain:
cao decisions off
```

Then set `ephemeral.enabled` back to `false` (step 1's backup is your restore point; if there was
none, remove the file this setup created) and restart the server (step 7's checks again); remove
the tier mappings this setup added (`cao decisions tier <provider> <tier> --unset` for each),
unless the user wants to keep them; leave the decider **binding** (`--decider <name>` on the
point) as is — `off` already stops it being asked; tell every other connected agent session that
`create_ephemeral_agent` stays advertised until it reconnects MCP; leave consent revocation (`cao
decisions disable <name>`) and uninstalling the decider package to the user, by policy; and remove
the `settings.json.bak.*` files once the user confirms they no longer need them.

## 12. Safety

- **Never print an environment variable's value.** Check *presence* only, through a decider's own
  status/key-check command (step 6), never a bare `env`/`printenv`. Print variable *names*, never
  values, in any report.
- **Never grant consent, run a decider's consent command, or type an API key in on the user's
  behalf** (step 6), even if asked to "just enable it" — hand them the exact command instead.
- **Edit `settings.json`, or run anything that writes it** (`cao config set`, `cao decisions tier`,
  `cao decisions set`) **only with the user's explicit OK** — it's operator-only, shared with every
  other setting; the step-1 backup is always in place first.
- **Restarting `cao-server` (step 7) needs the user's OK too** — it affects every live session, not
  just this one.

## Symptom → fix

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `cao decisions --help` says "No such command", or `enable`/`off` are missing | This build predates the decision platform, or (on the current PyPI release) hasn't caught up to it yet | Stop; tell the user to install a build with it, e.g. a git install of `main` |
| Decider answers worked yesterday, now every launch falls back | A CAO upgrade reinstalled the tool venv and dropped a decider installed with plain `uv pip install` (pattern B), or dropped a pattern-A `--with` package on a git/pinned install or a local reinstall from `cao update`'s hint (a current CAO gap) | Re-check the entry-point list (step 3); reinstall with the correct pattern for this install's source |
| Every create refuses with `policy_config_error:model_tiers` | A tier is missing for one of the allowed providers (the detail names which `provider.tier`) | Re-run step 4's completeness check; fill the gap |
| `create_ephemeral_agent` is missing from this agent's own tool list after enabling it | This agent's MCP connection started before `ephemeral.enabled` became true | Reconnect MCP (`/mcp` in Claude Code) or restart this agent's own terminal |
| A Kiro ephemeral helper refuses with `unexpected_failure (400)` | The running `kiro-cli` isn't allow-listed for v3 | Check the server log for `kiro_v3_unsupported_version` / `kiro_v3_unverified_version`; pin to a listed version, or ask the operator about their `v3_allow_unverified_versions` opt-in |
| A medium-complexity task's model.route record shows `fallback` / `low_confidence` | Confidence came in below `confidence_threshold` (default `0.7`) | Expected behavior, not a bug — only investigate if it recurs on tasks that shouldn't be borderline |
| Editing `ephemeral.*` seems to do nothing | A typo'd key name: CAO ignores unknown keys (nothing rejects them; the server only logs `ephemeral settings: unknown keys are ignored` on create) | Re-run step 5's loader check; its unknown-keys list names the typo. Compare with the [Operator settings](../../docs/ephemeral-agents.md#operator-settings) list |

## Related skills

- `cao-agent-routing` — choosing whether and where to delegate at all; this skill only covers
  getting ephemeral agents and a decider set up.
- `cao-session-management` — launching and messaging the sessions that create ephemeral agents.
- `cao-contributing` — if you're changing CAO's own decision-platform code rather than configuring
  an install of it.
