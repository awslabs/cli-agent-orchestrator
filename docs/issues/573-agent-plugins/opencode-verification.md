# OpenCode MCP disable — verification note

**Summary.** `enabled: false` demonstrably prevents OpenCode from spawning a
`type: "local"` MCP server's subprocess — confirmed by both a source read of the
shipped bundle **and** an empirical run with a positive control that fired.
Option 3 (write `enabled: false` instead of deleting the entry — see `design.md`
§10a) is therefore safe. This note records the evidence so the deviation stays
re-checkable when the pinned OpenCode version changes.

---

## Environment and config isolation

- **Version:** `opencode --version` → `1.18.15`
- **Binary (source evidence target):** `/opt/homebrew/bin/opencode` →
  `/opt/homebrew/Cellar/opencode/1.18.15/libexec/lib/node_modules/opencode-ai/bin/opencode.exe`
  (a 143 MB Bun-compiled Mach-O arm64 binary; the JS/TS is embedded and
  minified inside the `bunfs` bundle — same bytes as
  `.../node_modules/opencode-darwin-arm64/bin/opencode`).
- **Isolation (never touches the real user config):** determined from the
  bundle's own path resolver, not guessed. OpenCode resolves its config dir as
  `$XDG_CONFIG_HOME/opencode` (home via `OPENCODE_TEST_HOME`, else `homedir()`).
  Every probe ran with a sourced env file `/tmp/opencode-probe/env.sh` setting
  `HOME`, `OPENCODE_TEST_HOME`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`,
  `XDG_STATE_HOME`, `XDG_CACHE_HOME` all under `/tmp/opencode-probe/`, plus
  `OPENCODE_DISABLE_PROJECT_CONFIG=1`, `OPENCODE_DISABLE_AUTOUPDATE=1`,
  `OPENCODE_DISABLE_MODELS_FETCH=1`, and `--pure` (no external plugins). No
  credentials were set; no model call is needed because `opencode mcp list`
  builds MCP state without one.
- **Isolation verified empirically** with `opencode debug paths --pure`:

  ```
  home       /tmp/opencode-probe/home
  data       /tmp/opencode-probe/xdg/data/opencode
  config     /tmp/opencode-probe/xdg/config/opencode
  state      /tmp/opencode-probe/xdg/state/opencode
  cache      /tmp/opencode-probe/xdg/cache/opencode
  ```

  The real `~/.config/opencode/opencode.json` **did not exist before and was
  never created** (checked before the run; `git status` on the CAO repo shows no
  `.opencode/` or `.gitignore` droppings — only the pre-existing untracked
  `AGENTS.md`). All probe configs live at
  `/tmp/opencode-probe/xdg/config/opencode/opencode.json` — the exact relative
  location OpenCode reads as its global `opencode.json`, i.e. the real code path,
  just relocated.
- **Trigger command:** `opencode mcp list` — it calls `MCP.status()`, which
  forces the lazy `MCP.state` to build, which is what spawns the enabled
  servers. Confirmed no model/auth required.

The probe configs use the **exact shape CAO writes**
(`opencode_config.py:translate_mcp_server_config`):
`{"type": "local", "command": [...], "enabled": <bool>}`.

---

## `enabled: false` prevents the subprocess from being spawned

### Source evidence

All snippets below are from the embedded server-side **MCP module** inside the
binary above (extracted via `strings`; the module is identifiable by its status
struct identifiers `MCPStatusConnected/Disabled/Failed/NeedsAuth`). The disabled
sentinel is defined once:

```js
J = { status: { status: "disabled" } }
```

**Gate #1 — the state builder loop (`MCP.state`).** It iterates over
`config.mcp` and, for any entry whose `enabled === false`, records a `disabled`
status and `return`s **before** calling the create/spawn function `w`:

```js
// A = Instance.state("MCP.state"): builds runtime state, spawning enabled servers
let Z = V.mcp ?? {};                                  // Z = config.mcp
return yield* $.forEach(Object.entries(Z), ([I, E]) => $.gen(function*() {
  if (!j1(E)) {                                        // j1 = "is object with a 'type'"
    yield* $.logError("Ignoring MCP config entry without type", { key: I });
    return;
  }
  if (E.enabled === !1) {                              // enabled === false
    N.status[I] = { status: "disabled" };              // mark disabled...
    return;                                            // ...and STOP. w() never called.
  }
  let k = yield* w(I, E);                              // else: create -> spawn
  if (N.status[I] = k.status, k.mcpClient) { /* store client */ }
}), { concurrency: "unbounded" });
```

**Gate #2 — the create function `w` (`MCP.create`), defense-in-depth.** Even if
reached, it short-circuits on `enabled === false` before dispatching to
remote/local connect:

```js
w = $.fn("MCP.create")(function*(V, F) {
  if (F.enabled === !1) return J;                      // J = { status: { status: "disabled" } }
  let { client: Z, status: N } = F.type === "remote"
      ? yield* B(V, F)                                 // connectRemote
      : yield* K(V, F);                                // connectLocal  <-- the only spawn path
  ...
});
```

**The spawn itself lives only in `connectLocal` (`K`)** — the subprocess is
constructed with the Bun spawner `j2`, and this code is reachable **only** when
`enabled !== false` (via `w`, via the state loop):

```js
K = $.fn("MCP.connectLocal")(function*(V, F) {
  let [Z, ...N] = F.command,                           // Z = command[0], N = args
      I = yield* X0.directory,
      E = F.cwd ? N5.resolve(I, F.cwd) : I,
      k = new j2({ stderr: "pipe", command: Z, args: N, cwd: E,
                   env: { ...process.env, ...(Z === "opencode" ? { BUN_BE_BUN: "1" } : {}), ...F.environment } });
  return yield* W(k, F.timeout ?? UD).pipe(
    $.map((m) => ({ client: m, status: { status: "connected" } })),
    $.catch((m) => $.succeed({ client: void 0,           // spawn/connect failure is CAUGHT...
                               status: { status: "failed", error: <msg> } })) // ...-> "failed", NOT fatal
  );
});
```

Conclusion (source): a `false` value on `enabled` is checked with strict
`=== false` in two independent places, both of which return before `connectLocal`
runs, so the `new j2({command, args, ...})` subprocess is never constructed.

### Empirical evidence (with positive control)

Config (both entries in one file, exact CAO shape):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "probe-enabled":  { "type": "local", "command": ["/tmp/opencode-probe/wrap_enabled.sh",  "hello-enabled"],  "enabled": true  },
    "probe-disabled": { "type": "local", "command": ["/tmp/opencode-probe/wrap_disabled.sh", "hello-disabled"], "enabled": false }
  }
}
```

Each wrapper appends `pid/ppid/time/argv` to a sentinel file when the OS execs
it, then exits (we are proving the *spawn*, not speaking MCP). Command run:

```
opencode mcp list --pure --print-logs --log-level INFO
```

Raw result:

```
┌  MCP Servers
... level=WARN message="server unavailable" key=probe-enabled type=local status=failed
●  ✗ probe-enabled failed
│      MCP error -32000: Connection closed
│      /tmp/opencode-probe/wrap_enabled.sh hello-enabled
●  ○ probe-disabled disabled
│      /tmp/opencode-probe/wrap_disabled.sh hello-disabled
└  2 server(s)

=== SENTINELS ===
-- enabled.spawned --
ENABLED spawned pid=89700 ppid=89619 time=1786315312.258211000 argv=[hello-enabled]
-- disabled.spawned --
MISSING (expected)
```

- **Positive control fired:** `enabled.spawned` **EXISTS** — the harness can and
  did observe a spawn. (`probe-enabled` shows `✗ failed / Connection closed`
  only because the wrapper exits without completing the MCP handshake; the
  process was nonetheless spawned, which is the point.)
- **`disabled.spawned` is MISSING** — `enabled: false` prevented the spawn.
- OpenCode's own status for `probe-disabled` is `○ disabled`, matching the
  source's `{ status: "disabled" }`.

**Conclusion: `enabled: false` prevents the subprocess spawn — Option 3 holds.**

---

## A disabled entry with a non-existent `command` causes no config error

This is the real post-uninstall state: a disabled entry whose `PLUGIN_ROOT` (and
thus the executable) was deleted. Config:

```json
{ "mcp": { "gone-plugin-srv": { "type": "local",
  "command": ["/tmp/opencode-probe/does-not-exist-ever", "--serve"], "enabled": false } } }
```

(`/tmp/opencode-probe/does-not-exist-ever` confirmed to not exist.)

```
$ opencode mcp list --pure --print-logs --log-level INFO      # EXIT=0
┌  MCP Servers
●  ○ gone-plugin-srv disabled
│      /tmp/opencode-probe/does-not-exist-ever --serve
└  1 server(s)

$ opencode debug config --pure                                 # EXIT=0
{ "$schema": "...", "mcp": { "gone-plugin-srv": { "type": "local",
  "command": ["/tmp/opencode-probe/does-not-exist-ever", "--serve"], "enabled": false } },
  "agent": {}, "mode": {}, "plugin": [], "command": {}, "username": "plau" }
```

- `mcp list` exit **0**; `debug config` exit **0**. Config parses and resolves
  cleanly; the entry is reported as `○ disabled`.
- No spawn attempt occurred (`disabled.spawned` absent) — the missing path is
  never touched, exactly as the source predicts.

**Conclusion: a disabled entry pointing at a deleted executable is benign —
OpenCode starts normally and raises no config error.**

---

## An `enabled: true` entry with a missing command: per-launch "failed" spawn, no crash

The "before" state Finding 1 describes. Same config as the previous case but `enabled: true`:

```json
{ "mcp": { "gone-plugin-srv": { "type": "local",
  "command": ["/tmp/opencode-probe/does-not-exist-ever", "--serve"], "enabled": true } } }
```

```
$ opencode mcp list --pure --print-logs --log-level INFO      # EXIT=0  (opencode itself does NOT crash)
┌  MCP Servers
... level=WARN message="server unavailable" key=gone-plugin-srv type=local status=failed
●  ✗ gone-plugin-srv failed
│      ENOENT: no such file or directory, posix_spawn '/tmp/opencode-probe/does-not-exist-ever'
│      /tmp/opencode-probe/does-not-exist-ever --serve
└  1 server(s)
```

- Observable symptom: server status `✗ failed` with
  `ENOENT: no such file or directory, posix_spawn '/tmp/opencode-probe/does-not-exist-ever'`,
  plus a `level=WARN message="server unavailable" ... status=failed` log line.
- `opencode mcp list` still exits **0** — the failure is caught (`connectLocal`'s
  `$.catch → { status: "failed" }`) and is not fatal to OpenCode.

**Conclusion: an `enabled: true` entry with a deleted command produces a
failing `posix_spawn` (ENOENT) and a "server unavailable" warning on every
launch — a permanent, per-launch failed spawn attempt. This is precisely the
harm Finding 1 documents, and precisely what Option 3 removes.**

---

## Inert-command fallback (`command: ["true"]`) — not required

This fallback was to be exercised **only if `enabled: false` did not stop the spawn**. The result above shows it
does, so the fallback is unnecessary and was not exercised. (For the record, the
source path for a hypothetical `command: ["true"]` would still spawn `true`,
which exits 0 immediately and would therefore report `failed`/"Connection
closed" like the enabled positive control — i.e. it is strictly worse than
`enabled: false`, which produces a clean `disabled` status with no spawn at all.)

---

## Implications for the fix

- **Option 3 is safe as recommended.** Setting `"enabled": false` (a JSON
  boolean) on a CAO-delivered `type: "local"` entry stops OpenCode 1.18.15 from
  spawning it — verified in source (two strict `=== false` gates before the
  spawn) and empirically (positive control fired; disabled entry never spawned).
  It also neutralizes the Q3 harm: the post-uninstall state becomes a clean
  `disabled` instead of a per-launch `ENOENT posix_spawn` failure.

- **A post-removal check should be observational, not a config read-back.**
  After `cao plugin remove`, assert on OpenCode's runtime
  view: `opencode mcp list` reports the CAO/plugin server as **`disabled` (○)**
  rather than `connected`/`failed`, and no child process is spawned for it.
  That is a stronger proof than re-reading the JSON we just wrote, and it does
  not need a model. (Note the cross-provider caveat still stands:
  "disappears entirely" is only true for Kiro/Copilot; for OpenCode the correct
  assertion is "shows as disabled / does not spawn".)

- **Deviation to record explicitly (like the Kiro `pathlib`-vs-`glob` split):
  Option 3 leaves inert cruft that is user-visible.** The disabled entry is not
  removed — `opencode mcp list` still lists it (as `○ disabled`) and prints its
  now-dangling command line. This is cosmetic, but a user inspecting their MCP
  servers will see leftover CAO/plugin entries accumulate across install/remove
  cycles. Follow-up Option 1 (record delivered names, prune them) or Option 2
  (`x-cao-plugin` marker + prune) is still the eventual cleanup path.

- **Correctness constraint for the implementer: write a JSON boolean `false`,
  never a string/number.** The gate is strict `enabled === false`. `"false"`,
  `0`, or `null` would all be truthy-for-this-purpose and OpenCode **would
  spawn**. CAO's helper already emits a real boolean at `opencode_config.py:124`
  (`"enabled": True`), so flipping it to `False` via `json.dumps` is correct;
  just don't stringify it.

- **Prefer `enabled: false` over dropping the entry's `type`.** OpenCode's
  `j1` guard ignores any entry lacking a `type` and logs `logError("Ignoring MCP
  config entry without type")` on **every** launch. `enabled: false` yields a
  clean `disabled` status with no error log, so it is the better inert state.

- **No CAO-side test can catch a future OpenCode behavior change here** — this is
  third-party behavior in a minified bundle. Pin the observed version in the
  deviation note (**verified against OpenCode 1.18.15**) so a future bump is a
  prompt to re-verify. This finding does **not** address Finding 2 (install-side
  silent clobber of a user's hand-written entry); that still needs the separate
  install-side "name already exists and was not placed by CAO" guard.
