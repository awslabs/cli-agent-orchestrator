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

**Decider-generic.** Everything below works for any installed decider package. `cao-decider-jev`
(entry-point name `jev`) is the worked example; a self-hosted decider follows the same steps, since
consent, installation and the tier table are decider-package concerns, not CAO-specific to any one
name. CAO bundles and endorses no decider. A published, generally-installable `jev` release is
tracked in #1182 — until then, "install the decider package" means installing from whatever source
the user already has (a git checkout, a wheel, a private index), never a source this skill invents
for you. **Never resolve a decider by a bare, guessed name from PyPI** — see step 3.

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
refusal. Get the user's explicit OK before **any** step that writes `settings.json` (steps 4 and 5)
or restarts the shared server (step 7) — see [Safety](#12-safety).

## 1. Back up `settings.json`

Before touching anything, so every later step has a real restore point:

```bash
SETTINGS="$(cao config path)"
cp "$SETTINGS" "$SETTINGS.bak.$(date +%s)"
```

Keep this path; you'll need it in [Undo](#11-undo). Remove old `settings.json.bak.*` files yourself
once the user confirms the setup is done — they're plaintext copies of an operator-only file.

## 2. Preflight

**The printed version number does not tell you whether this build supports ephemeral agents or
decisions.** A git install of `main` and a published PyPI release can report the identical `cao
--version` string while one has the code and the other does not, and a PyPI release can simply
predate the decision platform entirely (at the time of writing, the latest PyPI release, 2.5.4, has
none of the commands probed below). Checking `--version` alone is the mistake to avoid.

Instead, probe for every command and field this skill goes on to use, and stop at the first one
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
`installed_deciders` key, this build does not have the full decision platform this skill relies on.
Report that plainly and stop — do not proceed to later steps. A build with `cao decisions status`
but without `enable`/`off`/`installed_deciders` is the "passes preflight, fails later" trap the
probes above exist to catch. Once #1182 ships, re-check whether the PyPI release has caught up
before relying on it here.

Then confirm how `cao-server` is actually running (a foreground terminal, a background process,
`launchd`, a container): you'll need to restart it in step 7, and CAO itself does not start a
server for you. Ask the user if it isn't obvious from the shell, tmux, or process list.

## 3. Install the decider into cao-server's environment

**Never let an agent resolve a decider package from a bare, guessed name.** PyPI can (and in the
case of the worked example, does) already host an unrelated package under the short name a user
might say out loud. Use only the decider's exact distribution name as given by the user or the
package's own docs — for the worked example that is **`cao-decider-jev`** (import module
`cao_decider_jev`, `cao.deciders` entry-point name `jev`), never the bare name `jev`. Where the
decider isn't published anywhere reachable yet, say so plainly and point to #1182 — don't invent a
source.

The decider package must load inside the **same Python environment `cao-server` runs from** — not
whatever environment the agent's own shell happens to have active. Find that environment first:

```bash
cao config path          # prints the active settings.json path
cao -V                   # confirms a cao binary is on PATH at all
command -v cao-server    # the cao-server executable's path
cat "$(uv tool dir)/cli-agent-orchestrator/uv-receipt.toml"   # if CAO is a uv tool
```

When CAO is installed as a `uv tool` (the common case; `uv tool dir` prints the tools root), its
environment is `$(uv tool dir)/cli-agent-orchestrator/`. There are two ways to add a decider
package to it. Read the receipt's own `requirements` entry for `cli-agent-orchestrator` first — a
`git = "..."` or `path = "..."` source means pattern A must reinstall from that *same* source, not
from PyPI, or it silently moves the user onto a published release instead:

| Pattern | Command | Survives `cao update` / `uv tool upgrade`? | Also touches CAO itself? |
| --- | --- | --- | --- |
| A: re-run the tool install with `--with`, from the receipt's own source | `uv tool install "<source from the receipt, e.g. git+https://...>" --with "<decider-dist>" --reinstall` | **Only for an unpinned PyPI install.** `uv` records every `--with` package in the tool's own receipt, and a plain `uv tool upgrade` (or `cao update` on an unpinned registry install) reinstalls it too. **For a git or version-pinned install, `cao update` rebuilds the receipt without `--with` at all** (a current CAO gap — see below) | Yes — this reinstalls CAO itself, which may upgrade it unintentionally |
| B: `uv pip install` straight into the tool's venv | `uv pip install --python "$(uv tool dir)/cli-agent-orchestrator/bin/python3" "<decider-dist>"` | **No.** The receipt never records it, so the next `uv tool upgrade` (or any `cao update`) rebuilds the venv from the receipt alone and silently drops the decider | No — leaves CAO's own version untouched |

**Known CAO gap, not just a skill caveat:** for a git install, `cao update` runs `uv tool install
<git-source> --upgrade --reinstall`; for a pinned registry install it runs `uv tool install
cli-agent-orchestrator@latest --upgrade`. Neither passes `--with` (`cli/commands/update.py`,
`_build_command`); only an unpinned PyPI install's plain `uv tool upgrade` carries `--with`
forward. So after **any** `cao update`, re-run the entry-point check below, whichever pattern was
used. Tell the user this is a CAO limitation (`cao update` should carry a receipt's extra
requirements forward), and suggest filing or finding a tracking issue if one doesn't already cover
it.

Pick A when the user accepts CAO being upgraded at the same time, and the "only survives an
unpinned-PyPI upgrade" caveat. Pick B to leave CAO's own version untouched — but warn the user that
**any** future CAO upgrade needs the decider reinstalled, since nothing records it.

**Verify the install really landed where `cao-server` loads from — by entry point, never by
guessing an import name:**

```bash
"$(uv tool dir)/cli-agent-orchestrator/bin/python3" -c "
from importlib.metadata import entry_points
print([e.name for e in entry_points(group='cao.deciders')])
"
```

The worked example's name, `jev`, must appear in that list. An empty list, or an import error,
means the decider went into the wrong interpreter — most often the agent's own `uv run` venv rather
than `cao-server`'s. The same check, run once `cao-server` is back up, is `cao decisions status`'s
`installed_deciders` field (step 7 confirms the *running server*, not just this interpreter, sees
it).

If pattern B was used, periodically re-check the exact distribution is still recorded in the
receipt (pattern A only ever records anything there), or re-run the `uv pip install` after any CAO
upgrade — match the exact distribution name, not a loose substring (`grep -q jev` also matches an
unrelated package such as `cao-decider-jevk5`):

```bash
grep -q '"<decider-dist>"' "$(uv tool dir)/cli-agent-orchestrator/uv-receipt.toml" \
  && echo "recorded" || echo "NOT recorded — will not survive an upgrade"
```

A decider installed any other way (editable source checkout, a local repository on `PYTHONPATH`)
must still end up importable from that same interpreter; apply the same entry-point check before
moving on.

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
`enabled`. But it does **not** validate the value against CAO's own policy rules, and doesn't show
up in `cao config list`, so a typo'd key name writes successfully and silently does nothing. For a
list value (`allowed_providers`) or several keys at once, hand-edit the file. Get the user's
explicit OK before touching this file either way — the backup you need is already in place from
step 1.

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
2. Verify the result with CAO's own loader, run in `cao-server`'s own interpreter (found in step 3)
   — not the agent's own `uv run` venv, which either isn't `cao-server`'s code at all, or is a
   different checkout of it:
   ```bash
   "$(uv tool dir)/cli-agent-orchestrator/bin/python3" -c "
   from cli_agent_orchestrator.services.ephemeral_service import read_settings, validate_block, EphemeralPolicyError
   try:
       validate_block(read_settings())
       print('OK')
   except EphemeralPolicyError as e:
       print('rule:', e.rule, '| detail:', e.detail)
   "
   ```
   Print only `OK`, or the `rule`/`detail` pair — never the whole settings dict. A
   `policy_config_error:<key>` means fix the named key and re-run this check before restarting the
   server — a broken block refuses every ephemeral create and claim at runtime. This same check
   also covers whether step 4's tier table is complete, since `validate_block` calls the same tier
   validation.
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
"$(uv tool dir)/cli-agent-orchestrator/bin/cao-decider-jev" --help
```

You may check **whether** a key is present without ever seeing or printing its value — using the
decider's own status/key-check command, not a guess at an environment variable name (`cao env list`
prints every managed variable's *value*, and nothing in CAO's own environment layer is specific to
any one decider). For the worked example, that is the package's own shape-only check (presence and
length, never the value):

```bash
"$(uv tool dir)/cli-agent-orchestrator/bin/cao-decider-jev" key check
```

Use the equivalent for whatever decider is actually installed — check its own `--help` or docs for
the exact subcommand; don't invent an environment-variable name for it. Never echo a value, pipe a
key-bearing file through `cat`, or print the environment with a bare `env`/`printenv` (see
[Safety](#12-safety)).

`cao decisions enable <name>` only sets `model.route`'s decider to `shadow`-ready; it does not
itself turn routing on, and `effort.route` stays off. Turning routing on is step 8, which the agent
*can* run without the user's OK as a consent action — it still needs the user's OK as a
settings-writing action, like every other write in this skill.

## 7. Restart the server, reconnect MCP

A newly installed decider is discovered at `cao-server` startup, not live. Get the user's OK, then
restart it the way it's actually running (found in step 2: foreground gets Ctrl-C then relaunched,
background gets killed and relaunched, `launchd` gets `launchctl kickstart`). Find the exact
process by the port it's actually listening on, not by matching process arguments — an unrelated
process (including this agent's own shell) can share the same command-line text. The port is
`CAO_API_PORT` if set, else `9889` (`constants.py::SERVER_PORT`) — read it from how this install
actually starts its server, don't assume the default:

```bash
PORT="${CAO_API_PORT:-9889}"
curl -sf "http://127.0.0.1:${PORT}/health"        # exit 0: something is listening
lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN              # the PID actually bound to that port
ps -o pid,lstart,etime -p <pid-from-above>          # start time newer than your restart
```

Then confirm the *server* actually discovered the decider, not just this CLI process (`cao
decisions status`'s `installed_deciders` field runs in whatever process calls it, which may not be
the server). The running server logs its effective settings, including discovered deciders, at
startup and on every settings change — check that log for the restart you just did.

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

Always land in `shadow` first. Setting a point to `shadow` or `on` here is *CAO's* own mechanism; a
decider package may also do its own version of this as part of its consent command (the worked
example's `cao decisions enable jev` has Jev itself call `set_point("model.route", "shadow",
"jev")`) — that is Jev's own consent behavior, not something `cao decisions enable` does
generically, and it doesn't need step 6's human-only gate, since step 8 isn't granting or revoking
consent:

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

Check that `outcome` is `shadow` with no failure `reason` present. A `timeout`, `error`,
`invalid_answer`, `no_answer`, or `decider_unavailable` reason, repeated across more than one task,
is worth investigating before flipping to `on` — an all-`shadow`, no-note result is the expected
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

```bash
cao decisions set model.route off
cao decisions set effort.route off
# or, to halt everything at once and wait for in-flight calls to drain:
cao decisions off
```

Then, with the user's OK: set `ephemeral.enabled` back to `false` in `settings.json` (step 1's
backup is your restore point) and restart the server (step 7's checks again, to confirm it's back
and the log shows the change); remove the tier mappings this setup added (`cao decisions tier
<provider> <tier> --unset` for each), unless the user wants to keep them; leave the decider
**binding** (`--decider <name>` on the point) as is — `off` state already stops it from being
asked; tell every other already-connected agent session that `create_ephemeral_agent` keeps being
advertised until each one reconnects its MCP connection, since this agent can't fix that remotely;
leave consent revocation and uninstalling the decider package to the user (`cao decisions disable
<name>`, like enable, needs the same human-only interactive step); and remove the
`settings.json.bak.*` files this setup created, once the user confirms they no longer need them.

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
| Decider answers worked yesterday, now every launch falls back | A CAO upgrade reinstalled the tool venv and dropped a decider installed with plain `uv pip install` (pattern B), or dropped a pattern-A `--with` package on a git/pinned install (a current CAO gap) | Re-check the entry-point list (step 3); reinstall with the correct pattern for this install's source |
| Every create refuses with `policy_config_error:model_tiers` | A tier is missing for one of the allowed providers (the detail names which `provider.tier`) | Re-run step 4's completeness check; fill the gap |
| `create_ephemeral_agent` is missing from this agent's own tool list after enabling it | This agent's MCP connection started before `ephemeral.enabled` became true | Reconnect MCP (`/mcp` in Claude Code) or restart this agent's own terminal |
| A Kiro ephemeral helper refuses with `unexpected_failure (400)` | The running `kiro-cli` isn't allow-listed for v3 | Check the server log for `kiro_v3_unsupported_version` / `kiro_v3_unverified_version`; pin to a listed version, or ask the operator about their `v3_allow_unverified_versions` opt-in |
| A medium-complexity task's model.route record shows `fallback` / `low_confidence` | Confidence came in below `confidence_threshold` (default `0.7`) | Expected behavior, not a bug — only investigate if it recurs on tasks that shouldn't be borderline |
| Editing `ephemeral.*` seems to do nothing | A hand edit used the wrong key name, or `cao config set` wrote a key CAO's own validator (step 5) then rejects | Re-run step 5's loader check; it names the exact rejected key |

## Related skills

- `cao-agent-routing` — choosing whether and where to delegate at all; this skill only covers
  getting ephemeral agents and a decider set up.
- `cao-session-management` — launching and messaging the sessions that create ephemeral agents.
- `cao-contributing` — if you're changing CAO's own decision-platform code rather than configuring
  an install of it.
