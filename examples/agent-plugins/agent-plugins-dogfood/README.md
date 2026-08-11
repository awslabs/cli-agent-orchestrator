# Agent Plugins dog-food

A gated, asserting example that dog-foods CAO's own Agent Plugins pipeline:
CAO installs its **own** `cao` agent plugin (`agent-plugin/cao`) through its own
`cao plugin` / `cao install` commands, and asserts the result at every step —
exiting non-zero on any drift.

This is proof-of-work, not a tutorial. [`run.sh`](run.sh) **is** the shift-left
test that [`tools/record-dogfood-demo.mjs`](tools/record-dogfood-demo.mjs) gates
its GIF on: a broken pipeline cannot print the `PASS` marker, so it cannot
produce a green recording. See the ["Verified by
dog-fooding"](../../../docs/agent-plugins.md#verified-by-dog-fooding) section of
the Agent Plugins guide for the embedded GIF.

> `cao plugin` is **hidden** from `cao --help` pending maintainer decision M1
> (Requirement 16.5) — it is reachable and fully usable, just deliberately
> unadvertised. The recording narrates this so a hidden command does not look
> broken. It does **not** open any ship gate.

## What each step asserts

| # | Command | Assertion |
|---|---|---|
| 1 | `cao plugin validate ./agent-plugin/cao` | Manifest is loadable, the four shipped skills are named (`cao-agent-routing`, `cao-session-management`, `cao-supervisor-protocols`, `cao-worker-protocols`), and `mcp_present` with the `cao-ops` server. |
| 2 | `cao plugin add ./agent-plugin/cao` | Installed; each skill is **projected** into the skill store as a symlink whose target resolves into `…/agent-plugins/cao/skills/` (the plugin store). |
| 3 | `cao install … --provider kiro_cli` | The R1 fix, end to end: the emitted agent JSON carries the `skill://` globs **and** the `cao-ops` server with `PLUGIN_ROOT`/`PLUGIN_DATA` expanded to real paths in `env`, **no** `x-cao-pre-expanded` marker, and `@cao-ops` granted in `allowedTools`. |
| 4 | `cao install … --provider opencode_cli` | The server lands in `opencode.json` with a real boolean `"enabled": true`. **Finding 2** (in an isolated config): installing over a user's **own** same-named entry **preserves** it and **emits a finding** — user configuration is never silently clobbered. |
| 5 | `cao plugin remove cao` | Cross-provider removal, per the handoff's §2a. **Kiro/Copilot:** the server is **absent** from the rewritten config (wholesale rewrite). **OpenCode:** there is no in-place delete, so the server is **disabled** (`enabled: false`) — CAO never deletes a key it may not own. |

## Offline by default, live behind a flag

`opencode` is installed locally but is **not** on CI, so the OpenCode
observational proof is gated behind `CAO_DOGFOOD_LIVE=1`, with an offline
substitute that still gates the recording.

| Assertion | Offline (CI records this) | Live (`CAO_DOGFOOD_LIVE=1`, needs `opencode`) |
|---|---|---|
| Steps 1–4 | ✅ run | ✅ run |
| Step 5 — Kiro absence | ✅ config read-back | ✅ config read-back |
| Step 5 — OpenCode disabled | ✅ `opencode.json` shows `enabled: false` (proves CAO's **write**) | ✅ same |
| Step 5 — OpenCode **no-spawn** | ⏭️ skipped | ✅ `opencode mcp list` reports `cao-ops` as `disabled` (○) and a **sentinel wrapper** confirms no subprocess is spawned, with a **positive control** that does spawn so "no spawn" is *observed* |

Reading back the `enabled: false` we just wrote proves our own write, not the
behaviour the fix depends on — so §2a requires the **observational** step. It is
the live half here because it needs the provider binary; the offline substitute
still fails the recording if CAO stops disabling the server. The observed
no-spawn behaviour was verified against **OpenCode 1.18.15** (see
[`../../../docs/issues/573-agent-plugins/opencode-verification.md`](../../../docs/issues/573-agent-plugins/opencode-verification.md)).

## Running

```sh
# The asserting script directly (from the repo root):
./examples/agent-plugins/agent-plugins-dogfood/run.sh                 # offline
CAO_DOGFOOD_LIVE=1 ./examples/agent-plugins/agent-plugins-dogfood/run.sh  # + live OpenCode

# Or produce the GIF (offline) via the recorder:
cd examples/agent-plugins/agent-plugins-dogfood/tools
npm install && npm run playwright:install && npm run record
```

It runs against **this checkout's** code (`uv run cao …`), not the installed
`cao`, which predates the feature.

## Safety and isolation

Every command runs under a scratch `HOME` + `CAO_HOME_DIR` + `CAO_AGENTS_DIR`
(an anonymous `mktemp` directory), so the run **never** reads or writes the
operator's real `~/.aws/opencode/opencode.json`, real Kiro agents directory, or
real CAO home. The `PLUGIN_ROOT` that step 3 deliberately shows is therefore a
synthetic scratch path, never a real home path — see `CONTRIBUTING.md` §
*Recording test fixtures safely* for why that matters (incident #436). The
recorder captures raw command output to temp files and renders only curated,
identity-free lines (it never renders raw `ls -l`, whose owner column would leak
the OS username).
