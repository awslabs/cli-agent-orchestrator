# TSK-081 — Refactor WSB launcher into Setup/Run/Teardown/Auto stages

You are Codex working autonomously on a Python/PowerShell project at
`C:\dev\aws-cao`. Branch: **`psmux-support`** — stay on it. Fork of
awslabs/cli-agent-orchestrator destined for upstream PR #207.

Recent HEAD: `70192da fix(scripts): use VS Build Tools bootstrapper instead of winget`
plus subsequent commits in this session — read `git log --oneline -10`
to confirm.

## Context

Two scripts exist today:

- `scripts/run-tests-in-wsb.ps1` (host, PowerShell 7+) — generates a
  `.wsb` config with a LogonCommand that runs the in-sandbox script,
  launches WSB via `wsb start --config <xml> --raw`, polls `done.flag`,
  stops via `wsb stop --id`. Single-shot: every test invocation =
  full bootstrap. Has flags `-DryRun`, `-KeepOpen`, `-ShowGui`,
  `-StopPrevious`, `-TimeoutMinutes`.

- `scripts/wsb/run-tests.ps1` (sandbox, Windows PowerShell 5.1) —
  runs as the LogonCommand: installs uv, psmux, MSVC Build Tools,
  copies repo, runs `uv sync` + pytest, writes `done.flag`.

This wastes ~10 min per run on bootstrap. The new design splits the
lifecycle so bootstrap happens once and tests can be re-run cheaply.

The host is **Windows ARM64**. uv falls back to x86_64 Python (no
ARM64 build in python-build-standalone). httptools (transitive dep)
has no `win_arm64` wheel and no `win_amd64` wheel for cp314, so the
sandbox needs a working C/C++ toolchain to compile from source.

**Toolchain decision: drop MSVC entirely; use scoop + llvm-mingw
(mstorsjo build).** Marc uses this on the host with NO coercion —
setuptools picks it up via PATH. The package is in scoop's `main`
bucket: `mingw-mstorsjo-llvm-ucrt`. ZIP install is much faster than
the MSVC bootstrapper (~30s–1min vs 2–5min).

## Goal

Split the host script into explicit lifecycle stages driven by
`wsb exec`. The `.wsb` config keeps `MappedFolders` and redirection
settings but **drops the LogonCommand entirely** — the host drives
everything via `wsb exec --run-as ExistingLogin`.

### Stages

| Flag         | Behaviour                                                                |
|--------------|--------------------------------------------------------------------------|
| `-Setup`     | wsb start → wait for session → exec setup.ps1 (scoop, uv, Python, psmux, llvm-mingw) → persist sandbox id |
| `-Run`       | error if no state file → clear done.flag → exec run.ps1 → wait → surface exit code from done.flag |
| `-Teardown`  | read sandbox id → wsb stop → delete state file                          |
| `-Auto`      | Setup → Run → Teardown (always, in finally; -KeepOpen skips Teardown)   |
| (no flag)    | Same as `-Auto` (default behaviour matches today)                        |

Exactly one stage flag at a time. `-Auto` is the default. Reject
combinations like `-Setup -Run` with a clear error.

### State persistence

Write the sandbox id to `test-output-wsb/.sandbox-id` after `-Setup`
succeeds. `-Run` and `-Teardown` read it.

**`-Setup` re-entry rule:** if state file exists AND
`wsb list --raw` shows that id is still running, no-op (log
"sandbox <id> already running, skipping setup"). If file exists but
the id is gone, delete the stale file and proceed. `-StopPrevious`
unconditionally clears any running sandbox first.

### `-Run` contract

1. State file must exist; otherwise exit with a clear error
   ("run -Setup first").
2. Delete `done.flag` if present (stale from prior run).
3. `wsb exec` run.ps1 (synchronous) — its stdout/stderr stream to
   the host console. That IS the visibility into the sandbox; do not
   try to also tail a log file.
4. After exec returns, read pytest exit code from `done.flag`.
5. Surface run.log / junit.xml summaries the same way as today.

### `-Auto` cleanup contract

Wrap Setup+Run inside `try/finally`. Finally calls Teardown on:
- pytest failure
- Run error (exec fails, done.flag missing)
- Setup throws after `wsb start` succeeded (so id was captured)
- Ctrl-C
- timeout
- any unhandled throw

`-KeepOpen` overrides — print the manual-stop hint instead.

## Hard constraints

### `wsb exec --run-as` requires an active user session

After `wsb start`, the WDAGUtilityAccount auto-logs in but it isn't
instant. You must add a `Wait-SandboxReady` poll BEFORE the first
real exec call:

```powershell
function Wait-SandboxReady ([string]$Id, [int]$TimeoutSec = 60) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        & $wsbCli.Source exec --id $Id --run-as ExistingLogin `
            --command 'whoami' 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { return }
        Start-Sleep -Seconds 2
    }
    throw "Sandbox $Id never accepted exec calls within $TimeoutSec s"
}
```

`--run-as System` is NOT a substitute — it has no access to
user-installed scoop/uv/psmux. Always use `ExistingLogin`.

### Toolchain install in setup.ps1

Replace the entire MSVC bootstrapper block with:

```powershell
# Install scoop (single-line installer, runs as user; no admin)
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned -Force
Invoke-RestMethod get.scoop.sh | Invoke-Expression

# Refresh PATH so `scoop` resolves
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' +
            [Environment]::GetEnvironmentVariable('Path', 'Machine')

scoop install mingw-mstorsjo-llvm-ucrt
```

`mingw-mstorsjo-llvm-ucrt` is in scoop's default `main` bucket — no
`scoop bucket add`. Verified on the host. After install, scoop's
shim dir is on PATH and `gcc --version` works without further env
twiddling. Setuptools picks it up from PATH; **no `CC`/`CXX` env or
`distutils.cfg` shim required** (Marc confirmed).

### Python version pinning

Project's `requires-python = ">=3.10"`. uv's default pick (3.14)
breaks because httptools cp314 win_amd64 wheel does not exist.
However, since we're compiling from source via mingw anyway, you do
NOT need to pin Python — uv will pick whatever, mingw will build it.
Keep it simple: don't add a `--python` flag to uv sync.

### Drop the LogonCommand

The .wsb XML emitted by the host script should keep `MappedFolders`,
`Networking`, redirection toggles — but **omit `<LogonCommand>`
entirely**. The host drives setup/run via wsb exec. This also makes
the `Get-LogonCommand` helper obsolete; remove it.

### Keep these flags working

| Flag                | Behaviour                                                  |
|---------------------|------------------------------------------------------------|
| `-DryRun`           | Print what would happen for the chosen stage; no launch/exec/stop. For `-DryRun -Auto` print the .wsb XML (as today) and stop there. For other stages, print the resolved sandbox id (if any) and the wsb exec command(s) that would be issued. Exit 0. |
| `-KeepOpen`         | In `-Auto`, skip Teardown; print manual-stop hint with id. |
| `-ShowGui`          | After Setup launches the sandbox, run `wsb connect --id` detached so the UI opens. |
| `-StopPrevious`     | Before Setup, list and stop every running sandbox. Already implemented; keep. |
| `-TimeoutMinutes`   | Cap on the synchronous `wsb exec` call for run.ps1. If exec hasn't returned in N minutes, stop polling and exit 124. (For Setup, no timeout; setup is bounded by network speed.) |

### Exit codes (preserve)

- 0..4: pytest exit (read from done.flag after -Run)
- 1: Windows Sandbox not available (preflight)
- 124: -Run timed out
- 125: sandbox vanished mid -Run (exec returned non-zero AND
       sandbox no longer in `wsb list`)
- 130: cancelled by user (Ctrl-C)

## File plan

Edit/create these only:

1. `scripts/run-tests-in-wsb.ps1` — restructure into stages.
   Suggested structure: parse stage, then dispatch to functions
   `Invoke-Setup`, `Invoke-Run`, `Invoke-Teardown`, `Invoke-Auto`.
   Helpers: `Wait-SandboxReady`, `Get-SandboxId`, `Set-SandboxId`,
   `Remove-SandboxId`.
2. `scripts/wsb/setup.ps1` (NEW) — runs inside the sandbox via
   `wsb exec`. Installs scoop, uv, psmux, llvm-mingw. Uses
   `#Requires -Version 5.1` header (sandbox PowerShell version).
3. `scripts/wsb/run.ps1` (NEW) — runs inside the sandbox via
   `wsb exec`. Robocopy `C:\src\aws-cao` → `C:\work\aws-cao`,
   then `cd C:\work\aws-cao`, `uv sync --group dev`,
   `uv run pytest test/ --ignore=test/e2e/ "--junit-xml=C:\out\junit.xml"`.
   Always writes pytest exit code to `C:\out\done.flag` in finally.
   Re-source `vcvarsall` is NOT needed (no MSVC). Remove the
   `Invoke-Native` helper if dead — no log redirection needed since
   wsb exec relays stdout/stderr to host console.
4. `scripts/wsb/run-tests.ps1` — DELETE. The staged scripts replace
   it.

## Verification before each commit

```powershell
Invoke-ScriptAnalyzer -Path scripts/run-tests-in-wsb.ps1 -Severity Error,Warning -ExcludeRule PSAvoidUsingWriteHost
Invoke-ScriptAnalyzer -Path scripts/wsb/setup.ps1 -Severity Error,Warning -ExcludeRule PSAvoidUsingWriteHost
Invoke-ScriptAnalyzer -Path scripts/wsb/run.ps1 -Severity Error,Warning -ExcludeRule PSAvoidUsingWriteHost
```

`PSAvoidUsingWriteHost` is acceptable on interactive progress lines.

DryRun smoke tests (must each exit 0):

```powershell
.\scripts\run-tests-in-wsb.ps1 -DryRun
.\scripts\run-tests-in-wsb.ps1 -DryRun -Auto
.\scripts\run-tests-in-wsb.ps1 -DryRun -Setup
.\scripts\run-tests-in-wsb.ps1 -DryRun -Run
.\scripts\run-tests-in-wsb.ps1 -DryRun -Teardown
```

Reject combo:

```powershell
.\scripts\run-tests-in-wsb.ps1 -Setup -Run    # must error, exit non-zero
```

Generated `.wsb` XML must parse cleanly:

```powershell
$w = Get-ChildItem $env:TEMP\cao-wsb-*.wsb | Sort LastWriteTime -Desc | Select -First 1
[xml](Get-Content $w.FullName -Raw) | Out-Null    # must succeed
```

Confirm the .wsb has NO `<LogonCommand>` element after refactor.

**Don't actually launch a sandbox** — Marc tests end-to-end himself.

## Hard rules

- **Stay on `psmux-support`.** No branch switch, no rebase, no
  push.
- **Conventional commits.** 2–3 expected:
  1. host script staged refactor
  2. split sandbox runner into setup.ps1 + run.ps1, delete old
  3. (optional) polish
- **Co-author both Claudes:**
  ```
  Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
  Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
  ```
- **Never `--no-verify`.** Fix hook failures, don't bypass.
- **Touch only:** `scripts/run-tests-in-wsb.ps1`,
  `scripts/wsb/setup.ps1`, `scripts/wsb/run.ps1`,
  `scripts/wsb/run-tests.ps1` (delete). Do not modify `src/`,
  `test/`, `pyproject.toml`, `.superpowers/`, `docs/`.

## Don't get distracted

- Don't rewrite the spec docs.
- Don't add MSVC fallback. The decision is mingw via scoop.
- If something looks broken outside the file plan, note it in the
  final report; don't fix it.

## Final report (≤200 words)

After commits land, print to STDOUT:
- Commit hashes + one-line descriptions
- Static check results (PSScriptAnalyzer + DryRun matrix)
- Any deviations from this brief and why
- Manual-test punch list for Marc (which stage to run first, etc.)
