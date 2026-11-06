---
name: cao-ephemeral-decider-setup
description: Set up ephemeral agents and an installed decider (worked example
  Jev; also works for a self-hosted decider such as JevK5) for a user, end to
  end, on an existing CAO install. Covers preflight, installing the decider
  into cao-server's own environment, the per-provider tier table, a safe edit
  of the ephemeral settings block, handing consent to the human, the
  server/MCP restart, shadow vs on, a smoke test, provider caveats and undo.
  Use when a user asks an agent to "set up Jev" or "turn on ephemeral agents",
  not when choosing whether to delegate to one (see cao-agent-routing) or
  authoring a decider package.
---

# CAO Ephemeral Agent + Decider Setup

Setting this up by hand is easy to get wrong in ways that fail silently: a
decider installed into the wrong Python environment looks fine until the next
CAO upgrade quietly drops it; one missing tier for one provider refuses every
provider's launches; a stale MCP connection fails with no hint that a restart
would fix it. This skill walks an agent through every step in order, with the
exact commands to run and what to check after each one, up to the one step
that is human-only: granting consent.

**Decider-generic.** Everything below works for any installed decider
package. `jev` is the worked example because it is a real, publicly-named
decider; a self-hosted decider such as `JevK5` follows the same steps, since
consent, installation and the tier table are decider-package concerns, not
CAO-specific to any one name. CAO bundles and endorses no decider. A published
`jev` package and release are tracked in #1182 — until it ships, "install the
decider package" below means installing from whatever source the user already
has (a git checkout, a wheel, a private index), never a source this skill
invents for you.

**Scope.** This skill is setup mechanics only. Whether to delegate to an
ephemeral agent at all is `cao-agent-routing`'s job, not this skill's. Authoring
a decider package is out of scope entirely.

## Order of operations

1. [Preflight](#1-preflight)
2. [Install the decider into cao-server's environment](#2-install-the-decider-into-cao-servers-environment)
3. [Build the complete tier table](#3-build-the-complete-tier-table)
4. [Edit the `ephemeral` settings block safely](#4-edit-the-ephemeral-settings-block-safely)
5. [Hand consent to the human](#5-hand-consent-to-the-human)
6. [Restart the server, reconnect MCP](#6-restart-the-server-reconnect-mcp)
7. [Shadow versus on](#7-shadow-versus-on)
8. [Smoke test](#8-smoke-test)
9. [Provider caveats](#9-provider-caveats)
10. [Undo](#10-undo)
11. [Safety](#11-safety)

Stop and report back to the user at the first failed check in any step —
don't guess past a refusal.

## 1. Preflight

**The printed version number does not tell you whether this build supports
ephemeral agents or decisions.** A git install of `main` and a published
PyPI release can report the identical `cao --version` string while one has
the code and the other does not — the version in `pyproject.toml` isn't
bumped on every merge. Checking `--version` alone is the mistake to avoid.

Instead, probe for the commands directly:

```bash
cao decisions --help    # exit 0 and a command list: decisions CLI exists
cao decisions status    # exit 0 and JSON: the decision engine is wired up
```

If either exits non-zero or prints "No such command", this build does not
have the decision platform. Report that plainly and stop — do not proceed to
later steps, which will fail confusingly instead of clearly.

Then confirm how `cao-server` is actually running (a foreground terminal, a
background process, `launchd`, a container): you'll need to restart it in
step 6, and CAO itself does not start a server for you. Ask the user if it
isn't obvious from the shell, tmux, or process list.

## 2. Install the decider into cao-server's environment

The decider package must load inside the **same Python environment
`cao-server` runs from** — not whatever environment the agent's own shell
happens to have active. Find that environment first:

```bash
cao config path          # prints the active settings.json path
cao -V                   # confirms a cao binary is on PATH at all
command -v cao-server     # the cao-server executable's path
cat "$(uv tool dir)/cli-agent-orchestrator/uv-receipt.toml"   # if CAO is a uv tool
```

When CAO is installed as a `uv tool` (the common case; `uv tool dir` prints
the tools root), its environment is `$(uv tool dir)/cli-agent-orchestrator/`.
There are two ways to add a decider package to it, with a real trade-off:

| Pattern | Command | Survives `cao update` / `uv tool upgrade`? | Also touches CAO itself? |
| --- | --- | --- | --- |
| A: re-run the tool install with `--with` | `uv tool install cli-agent-orchestrator --with jev --reinstall` | **Yes** — `uv` records every `--with` package in the tool's own receipt (`uv-receipt.toml`'s `requirements` list), so a later upgrade reinstalls it too | Yes — this reinstalls CAO itself, which may upgrade it unintentionally |
| B: `uv pip install` straight into the tool's venv | `uv pip install --python "$(uv tool dir)/cli-agent-orchestrator/bin/python3" jev` | **No.** The tool's own receipt never records it. The next `uv tool upgrade cli-agent-orchestrator` (or `cao update`) rebuilds the venv **from the receipt alone** and silently drops the decider — no error, it just goes back to no answer at call time | No — leaves CAO's own version untouched |

Pick A when the user is fine with CAO being upgraded at the same time, or
when the decider is published and `--with <package-name>` is enough. Pick B
when the user wants the decider added without touching CAO's own version —
but warn them explicitly that a future CAO upgrade will need the decider
reinstalled, since nothing records it.

**Verify the install really lands in the tool's own environment, not an
unrelated one:**

```bash
"$(uv tool dir)/cli-agent-orchestrator/bin/python3" -c "import jev; print(jev.__file__)"
```

An import error here means the decider went into the wrong interpreter, most
often the agent's own `uv run` venv rather than `cao-server`'s.

If pattern B was used, periodically re-check the receipt lists it (pattern A
only) or re-run the `uv pip install` after any CAO upgrade:

```bash
grep -q jev "$(uv tool dir)/cli-agent-orchestrator/uv-receipt.toml" && echo "recorded" || echo "NOT recorded — will not survive an upgrade"
```

A decider installed any other way (editable source checkout, a local
repository on `PYTHONPATH`) must still end up importable from that same
interpreter; apply the same import check before moving on.

## 3. Build the complete tier table

`cao decisions tier` maps one `(provider, tier)` pair to one model ID. **A
missing tier anywhere in `allowed_providers` refuses *every* provider's
create**, not just the one with the gap — this is the single most confusing
failure mode in the whole setup, because the error gives no hint which
provider or tier is actually missing.

```bash
cao decisions tier claude_code small  us.anthropic.claude-haiku-5-5
cao decisions tier claude_code medium sonnet
cao decisions tier claude_code large  opus
cao decisions tier codex small  global.openai.gpt-6-luna
cao decisions tier codex medium global.openai.gpt-6.1-sol
cao decisions tier codex large  global.openai.gpt-6-astra
cao decisions tier codex large --unset     # remove a mapping
```

Model IDs differ per login — the ones above are examples from one real
account, not fixed values. Codex in particular needs fully-qualified IDs
(`global.openai.<name>`, not a bare model name); check what the user's own
Codex login accepts before filling the table in. For each provider actually
in `ephemeral.allowed_providers` (or in `allowed_providers` at the
installed-profile level, for ordinary delegation), every tier from
`ephemeral.min_tier` (or `small` if unset) through `ephemeral.max_tier` must
be mapped, plus `ephemeral.default_tier`. An invalid model ID counts as
unmapped.

Confirm the table is complete before touching anything else:

```bash
cao decisions status | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)['model_tiers'], indent=2))"
```

A gap anywhere here means every ephemeral create for every allowed provider
fails with `policy_config_error:model_tiers.<provider>.<tier>` once decisions
are later turned on — fix it now, not after step 7.

## 4. Edit the `ephemeral` settings block safely

**There is no CLI for this block.** `cao config get`/`cao config set` only
reach `cao config list`'s known, flat keys — `ephemeral.*` is not one of
them. You must hand-edit `settings.json`. Get the user's explicit OK before
touching this file; it is operator-only and shared with every other setting.

1. Find the file and back it up:
   ```bash
   SETTINGS="$(cao config path)"
   cp "$SETTINGS" "$SETTINGS.bak.$(date +%s)"
   ```
2. Edit only the `ephemeral` object. Keep every other top-level key as-is —
   `model_tiers`, `decisions`, and anything else in the file must survive
   untouched. At minimum you are likely setting:
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
   merged into (not replacing) the existing `ephemeral` object and the rest of
   the file. See [Operator settings](../../docs/ephemeral-agents.md#operator-settings)
   for the full key list and defaults.
3. Verify the result with CAO's own loader — not just that it's valid JSON,
   since valid JSON can still fail CAO's own policy checks:
   ```bash
   uv run python -c "
   from cli_agent_orchestrator.services.ephemeral_service import read_settings, validate_block
   s = read_settings()
   validate_block(s)
   print('ephemeral block OK:', s)
   "
   ```
   A `policy_config_error:<key>` here means fix the named key and re-run this
   check before restarting the server — a broken block refuses every
   ephemeral create and claim at runtime, not just a warning.
4. If the check fails and you can't make it pass, restore the backup from
   step 1 rather than leaving the file broken.

## 5. Hand consent to the human

**Consent is human-only. An agent must never run the consent command, type
into its prompt, or set an API key on the user's behalf.** `cao decisions
enable <name>` (which runs the decider's own consent command) needs an
interactive TTY precisely so that an agent with shell access cannot drive it
non-interactively.

Give the user this exact block to run themselves, in a real terminal:

```bash
cao decisions enable jev
```

If the decider needs an API key and doesn't have one yet, the decider
package's own hidden-prompt command is also theirs to run — check the
decider's own docs or `--help` for the exact subcommand (for a package that
follows the console-script contract in [Decider packages](../../docs/decisions.md#decider-packages),
that's `cao-decider-<name> -- --help`, e.g. `cao-decider-jev -- --help`). You
may check **whether** a key is present without ever seeing or printing its
value:

```bash
cao env list 2>&1 | grep -qi '^JEV_API_KEY=' && echo "present" || echo "missing"
```

Never echo the matched line's value, pipe it through `cat`, or print the
environment with a bare `env`/`printenv` — print variable *names* only (see
[Safety](#11-safety)).

`cao decisions enable <name>` only sets `model.route`'s decider to
`shadow`-ready; it does not itself turn routing on, and `effort.route` stays
off. Turning routing on is step 7, which the agent *can* run, since it is not
a consent action.

## 6. Restart the server, reconnect MCP

A newly installed decider is discovered at `cao-server` startup, not live.
Restart it the way it's actually running (found in step 1 — a foreground
terminal gets Ctrl-C then relaunched, a background process gets killed and
relaunched, `launchd` gets `launchctl kickstart`, and so on). Verify the
restart actually happened rather than trusting that the command you ran
succeeded:

```bash
curl -sf http://127.0.0.1:9889/health   # exit 0: something is listening
lsof -nP -iTCP:9889 -sTCP:LISTEN        # find the PID
ps -o pid,lstart,etime -p <pid-from-above>   # start time newer than your restart
```

**Your own MCP connection does not auto-refresh.** The CAO MCP server process
that this agent's own terminal is talking to predates the restart and will
keep failing to see `create_ephemeral_agent` silently until it's told to
reconnect. In Claude Code, send `/mcp` and reconnect the CAO server; in other
providers, restart the agent's own terminal/session. Confirm the tool is
actually visible before declaring this step done — don't assume a clean
`/mcp` run means the tool list refreshed.

## 7. Shadow versus on

`cao decisions set model.route <state> --decider <name>` and the matching
`effort.route` call move a point between three states — `off` (nothing
asked, nothing recorded), `shadow` (the decider's answer is recorded but
never applied — the child always runs on the fallback tier), and `on` (the
decider's answer is applied, within `[min_tier, max_tier]`, if confident).

Always land in `shadow` first:

```bash
cao decisions set model.route shadow --decider jev
cao decisions set effort.route shadow --decider jev
```

Let a few real launches happen, then check that the decider is actually
producing answers (see step 8's reading of `cao decisions list`) before
asking the user whether to flip to `on`:

```bash
cao decisions set model.route on --decider jev
cao decisions set effort.route on --decider jev
```

`cao decisions off` is the one command that halts every point at once,
including a running server, and waits for in-flight decider calls to drain —
reach for it over three separate `set … off` calls when something looks
wrong (see [Undo](#10-undo)).

## 8. Smoke test

Create a small, a medium and a large ephemeral task, all with
`model_tier: "auto"` (and `effort: "auto"` if that's routed too), through
`create_ephemeral_agent` or `POST /ephemeral-agents`. Read the result two
ways:

1. **The assign/handoff note**, in the delegation result or the
   `X-CAO-Decision` response header, e.g. `ran on model-small (auto: small
   1.00)` or `ran on model-default (auto: fallback, unsure)`. See
   [Delegation results](../../docs/decisions.md#delegation-results) for every
   note shape.
2. **`cao decisions list`**, to confirm the record itself:
   ```bash
   cao decisions list --point model.route --limit 10
   ```
   Check `outcome` (`applied` vs `fallback`) and, on a fallback, `reason`.
   `low_confidence` (confidence under `confidence_threshold`, default `0.7`)
   is an expected, normal fallback reason for a borderline task — not a bug
   to chase. A `timeout`, `error`, or `decider_unavailable` reason, repeated
   across more than one task, is worth investigating before calling the
   setup done.

Declaring the setup finished without reading both of these is the smoke
test's own common failure: a note that looks right can still hide a decider
that is actually failing and falling back every time.

## 9. Provider caveats

- **Codex model IDs are per-login.** The same tier needs a different,
  fully-qualified ID (`global.openai.<name>`) depending on whose Codex
  account is signed in. Re-verify the table (step 3) after any account
  change rather than reusing another user's IDs.
- **Kiro's v3 engine refuses an un-allow-listed `kiro-cli` version** before a
  helper ever launches, with `kiro_v3_unsupported_version` (no pinned
  engine schema for that version at all) or `kiro_v3_unverified_version` (an
  allow-listed version pending operator verification — lift it only with the
  operator's own `kiro.v3_allow_unverified_versions` opt-in). The failure can
  surface to the caller as a bare `unexpected_failure`, with the real reason
  only in the server's own log — check there first when a Kiro ephemeral
  helper refuses for no visible reason.
- **Provider tool enforcement differs.** `claude_code` is `hard` (native tool
  refusal, no subagents/Monitor/Skill/plan mode). `codex` is `advisory` (the
  child runs `--yolo` with a shell; the tool list is a suggestion, not a
  restriction). `kiro_cli` is `hard` on v3, enforced through a per-launch
  agent file that CAO checks before every task. Don't describe Codex's list
  as enforced to a user — it isn't.

## 10. Undo

```bash
cao decisions set model.route off
cao decisions set effort.route off
# or, to halt everything at once and wait for in-flight calls to drain:
cao decisions off
```

Then, with the user's OK, set `ephemeral.enabled` back to `false` in
`settings.json` (step 4's backup is your restore point) and restart the
server. Leave consent revocation and uninstalling the decider package to the
user — `cao decisions disable <name>` runs the decider's own revoke command
and, like enable, needs the same human-only interactive step.

## 11. Safety

- **Never print an environment variable's value.** Check *presence* only
  (`grep -q '^NAME='`, never `grep '^NAME='` without `-q`, and never a bare
  `env`/`printenv`, which would dump every value including unrelated
  secrets). Print variable *names*, never values, in every report back to
  the user.
- **Never grant consent, run a decider's consent command, or type an API key
  in on the user's behalf** — see step 5. This holds even if the user asks
  the agent to "just enable it" — hand them the exact command instead.
- **Edit `settings.json` only with the user's explicit OK**, and always back
  it up first (step 4) — it is operator-only, shared with every other
  setting, and a concurrent writer can lose an update.

## Symptom → fix

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `cao decisions --help` says "No such command" | This build predates the decision platform | Stop; tell the user to install a build with it, e.g. a git install of `main` |
| Decider answers worked yesterday, now every launch falls back | A CAO upgrade reinstalled the tool venv and dropped a decider installed with plain `uv pip install` | Re-check the receipt (step 2); reinstall with pattern A, or re-run the `uv pip install` |
| Every create refuses with `policy_config_error:model_tiers.*` | A tier is missing for one of the allowed providers | Re-run step 3's completeness check; fill the gap |
| `create_ephemeral_agent` is missing from this agent's own tool list after a restart | The agent's MCP connection predates the server restart | Reconnect MCP (`/mcp` in Claude Code) or restart this agent's own terminal |
| A Kiro ephemeral helper refuses with `unexpected_failure (400)` | The running `kiro-cli` isn't allow-listed for v3 | Check the server log for `kiro_v3_unsupported_version` / `kiro_v3_unverified_version`; upgrade or get the operator's `v3_allow_unverified_versions` opt-in |
| A medium-complexity task's model.route record shows `fallback` / `low_confidence` | Confidence came in below `confidence_threshold` (default `0.7`) | Expected behavior, not a bug — only investigate if it recurs on tasks that shouldn't be borderline |
| Editing `ephemeral.*` seems to do nothing | `cao config set` doesn't know this key — there is no CLI for this block | Hand-edit `settings.json` (step 4) |

## Related skills

- `cao-agent-routing` — choosing whether and where to delegate at all; this
  skill only covers getting ephemeral agents and a decider set up.
- `cao-session-management` — launching and messaging the sessions that create
  ephemeral agents.
- `cao-contributing` — if you're changing CAO's own decision-platform code
  rather than configuring an install of it.
