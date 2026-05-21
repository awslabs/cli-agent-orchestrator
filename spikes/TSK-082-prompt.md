# TSK-082 — Fix the staged WSB launcher (clean up after botched debug session)

You are Codex working autonomously on a Python/PowerShell project at
`C:\dev\aws-cao`. Branch: **`psmux-support`** — stay on it. Fork of
awslabs/cli-agent-orchestrator destined for upstream PR #207.

Recent HEAD: `2f837ae fix(scripts): self-spawn into a visible console for sandbox visibility`
plus other in-session commits — read `git log --oneline -20` first.

## Background

A previous Codex run (TSK-081) successfully refactored the WSB test
runner into staged lifecycle commands:

- `scripts/run-tests-in-wsb.ps1` (host, PowerShell 7+)
  - `-Setup`: `wsb start` → connect for WDAG login → `wsb exec` setup.ps1 → persist id
  - `-Run`: `wsb exec` run.ps1 → poll done.flag → report
  - `-Teardown`: `wsb stop` → clear state
  - `-Auto` (default): Setup → Run → Teardown in try/finally
  - State file: `test-output-wsb/.sandbox-id`
  - Other flags: `-DryRun`, `-KeepOpen`, `-StopPrevious`,
    `-TimeoutMinutes`. (`-ShowGui` was removed because the GUI must
    always be open for `--run-as ExistingLogin` to work.)
- `scripts/wsb/setup.ps1` (Windows PS 5.1, runs in sandbox via
  `wsb exec`): scoop install, llvm-mingw, uv, Python, psmux.
- `scripts/wsb/run.ps1` (Windows PS 5.1, runs in sandbox via
  `wsb exec`): robocopy repo to writable, `uv sync --group dev`,
  `uv run pytest`, write done.flag.

Then I (Claude) tried to make it actually work end-to-end and
introduced enough damage that the user wants a clean Codex pass to
finish the job. **Stay disciplined: investigate before changing.**

## Hard facts learned this session (load-bearing)

1. **`wsb exec` ALWAYS exits 0.** The inner command's status is only
   visible via `--raw`, which emits `{"ExitCode": N}` JSON. Without
   `--raw`, every failure was being treated as success.
2. **`wsb exec` does NOT relay inner stdout to the host.** Visibility
   into sandbox-side scripts requires a transcript on the mapped
   output folder (`C:\out\setup.log`, etc.).
3. **`wsb start --config` boots the VM but does NOT log
   WDAGUtilityAccount in.** The user session is created lazily when
   an RDP client attaches. Headless = no logon = `--run-as
   ExistingLogin` exec calls fail forever. Therefore the launcher
   must run `wsb connect --id <id>` (detached, via Start-Process)
   immediately after `wsb start`. The sandbox GUI window IS visible
   on the host desktop — accept this cost.
4. **Wait-SandboxReady polls `whoami` via `wsb exec --run-as
   ExistingLogin`** until exit code 0. With connect happening,
   readiness arrives in 10–15 seconds. Default timeout is 30 s.
5. **wsb exec `--command` argument quoting.** Use the PowerShell
   call operator `& $cli exec ...` for native arg passing. NEVER use
   `Start-Process -ArgumentList @(...)` — it joins with spaces and
   does NOT quote space-containing elements, which breaks
   `--command "powershell -ExecutionPolicy Bypass -File ..."` (wsb
   parses `-ExecutionPolicy`, `Bypass`, `-File` as separate top-level
   args and rejects them).
6. **`Set-ExecutionPolicy -Scope CurrentUser -Force` raises
   SecurityException in WSB.** The effective policy is locked to
   Bypass at a higher scope. Drop the call; the host already invokes
   powershell with `-ExecutionPolicy Bypass`, and scoop's allowed
   list includes `ByPass`.
7. **The `wsb exec`-spawned powershell has no console window** — it
   inherits the broker's windowless stdio. To get a visible console
   on the sandbox desktop (so the user can watch progress), each
   sandbox-side script self-spawns once via `Start-Process
   powershell -Wait -PassThru` and propagates the inner exit code
   via `exit $proc.ExitCode`. Marker env: `CAO_WSB_INNER`. **This is
   already in setup.ps1 / run.ps1; don't break it.**
8. **httptools dep on Windows ARM64**: no `win_arm64` wheel exists
   for any cp version, AND `python-build-standalone` (uv's source)
   has no Windows ARM64 builds, so uv installs x86_64 Python on the
   ARM64 host. cp310-cp313 win_amd64 wheels exist; cp314 has none.
   The user explicitly said "wheels won't help" — they want the
   compile path with scoop+llvm-mingw to work, no Python pinning. I
   reverted my speculative 3.13 pin.
9. **The host has scoop + `mingw-mstorsjo-llvm-ucrt` (main bucket).**
   Marc claims he uses it on his host with NO coercion (no CC env,
   no distutils.cfg). It "just works" for him. That same path needs
   to work in WSB.

## What works right now (verified)

- `wsb exec --id <id> --run-as ExistingLogin --command 'whoami' --raw`
  returns `{"ExitCode": 0}` against a connected sandbox.
- The host launcher passes its DryRun matrix and combo rejection
  (`-Setup -Run` errors).
- PSScriptAnalyzer is clean across all three scripts.
- `wsb start` → `wsb connect` → `Wait-SandboxReady` cycle: confirmed
  works on the user's machine (sandbox GUI opens, login completes,
  whoami returns 0 within ~10s).

## What is suspected broken (NOT verified end-to-end)

- **Setup never made it past scoop install** in the user's last test
  before this prompt was written. The transcript at
  `test-output-wsb/setup.log` showed `Set-ExecutionPolicy` exploding
  with PermissionDenied (now fixed in commit `8fe30b3`).
- After `8fe30b3`, the user has not retried, but they noticed the
  sandbox UI showed a BLANK desktop — no console window. That's why
  commit `2f837ae` added the self-spawn pattern. **Untested.**

## What might still go wrong (likely investigation targets)

- **`Start-Process powershell` from inside a wsb-exec'd process** —
  does the new console actually appear on the sandbox desktop? If
  not, may need `-WindowStyle Normal` or other adjustments. Test by
  running setup and watching the sandbox.
- **Self-spawn vs Start-Transcript ordering** — setup.ps1 currently
  starts transcript AFTER the self-spawn check, which is correct
  (only the inner runs the transcript). But verify the transcript
  still captures everything.
- **scoop installer under `Set-StrictMode -Version Latest` +
  `$ErrorActionPreference = 'Stop'`** — scoop's installer may write
  to stderr or reference undeclared variables, which under these
  settings becomes a terminating error. If setup fails during
  `Invoke-RestMethod get.scoop.sh | Invoke-Expression`, consider
  relaxing strictness ONLY around the bootstrap calls (scoped local
  `Set-StrictMode -Off`, `$ErrorActionPreference = 'Continue'`),
  not the whole script.
- **Robocopy in run.ps1 may need similar self-handling.** Currently
  robocopy is launched via `Start-Process -Wait -PassThru`. Should
  work but verify the new console can host it.
- **uv sync compile step** — when uv decides to compile httptools
  from source, will scoop's gcc shim be on PATH for the spawned uv
  process? The setup adds `$env:Path = "$psmuxDir;$env:Path"` only
  for the setup script's process. After setup exits, the sandbox
  doesn't retain that PATH for the next wsb exec call. Either:
  - persist scoop's shim dir to Machine PATH inside setup.ps1, OR
  - have run.ps1 re-add it before uv sync.
  Same concern for psmux dir (`C:\tools\psmux`).

## What you must NOT change

- **The `-ShowGui` flag stays REMOVED.** wsb connect runs
  unconditionally because ExistingLogin requires it.
- **No Python pinning.** User vetoed `--python 3.13`.
- **No MSVC / no winget / no vs_BuildTools.exe.** Toolchain is scoop
  + `mingw-mstorsjo-llvm-ucrt` only.
- **No cmd.exe wrappers.** User explicitly rejected this.
- **Don't drop the self-spawn pattern** unless you replace it with
  something equivalently visible AND that propagates exit codes.
- **Don't touch `src/`, `test/`, `pyproject.toml`, `.superpowers/`,
  `docs/`.** Edit only the three scripts:
  - `scripts/run-tests-in-wsb.ps1`
  - `scripts/wsb/setup.ps1`
  - `scripts/wsb/run.ps1`

## What you should do

1. Read `git log --oneline -20` and `git diff 70192da..HEAD --stat`
   to understand the changes already made.
2. Read all three scripts top to bottom.
3. Run `Invoke-ScriptAnalyzer` and the DryRun matrix as a sanity
   check — those should all pass.
4. **Test end-to-end yourself.** This is the part Claude (the
   previous agent) couldn't do well — it has sandbox restrictions on
   spawning node and got into a bad iteration loop. You should:
   - `.\scripts\run-tests-in-wsb.ps1 -Setup -StopPrevious`
   - Watch the sandbox UI: does a console window appear? Does it run
     scoop, uv, etc.?
   - When it returns, inspect `test-output-wsb/setup.log` and the
     state file `test-output-wsb/.sandbox-id`.
   - Then `.\scripts\run-tests-in-wsb.ps1 -Run` and
     `test-output-wsb/run.log`.
   - Finally `.\scripts\run-tests-in-wsb.ps1 -Teardown`.
5. Iterate on whatever fails. Each failure should leave a setup.log
   or run.log with the actual error — read it before changing code.
6. When everything works end-to-end, delete obsolete code and commit
   small, well-titled fixes.

## Hard rules

- Stay on `psmux-support`. No branch switch, no rebase, no push.
- Conventional commits. Co-author both Claudes:
  ```
  Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
  Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
  ```
- Never `--no-verify`.
- PSScriptAnalyzer must pass with `-Severity Error,Warning
  -ExcludeRule PSAvoidUsingWriteHost`.

## Final report (≤200 words)

After end-to-end success, print to STDOUT:
- Commit hashes + one-line descriptions
- Smoke-test results (`-Setup`, `-Run`, `-Teardown` each pass)
- pytest exit code from done.flag
- Any deviations from this brief and why
