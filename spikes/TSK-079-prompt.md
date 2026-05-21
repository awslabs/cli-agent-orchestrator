# TSK-079 — Cleanup WSB launcher + implement clean self-shutdown

You are Codex working autonomously on a Python/PowerShell project at
`C:\dev\aws-cao`. Branch: **`psmux-support`** — stay on it. This is a fork
of awslabs/cli-agent-orchestrator destined for an upstream PR. The current
HEAD is `ff8d178 test(scripts): add Windows Sandbox test runner`.

## Project context

CAO is a Python orchestrator for CLI agents (claude, codex, gemini,
copilot). The target work for this task is a Windows Sandbox (WSB)
test runner consisting of two PowerShell scripts:

- `scripts/run-tests-in-wsb.ps1` — host-side launcher (PowerShell 7+).
- `scripts/wsb/run-tests.ps1` — sandbox-side runner (Windows PowerShell 5.1
  — that is what the WSB image ships).

The host launcher generates a `.wsb` config, launches Windows Sandbox,
polls `test-output-wsb\done.flag` for the pytest exit code, then surfaces
results.

## Two tasks

### Task A — Cleanup and simplify the launcher

The launcher accumulated comments and code paths from a trial-and-error
debugging session. Make it tighter without removing load-bearing logic:

- **Trim historical / "we used to do X" commentary.** Keep comments only
  where they explain *why* something is non-obvious (e.g. the
  scriptblock-as-arglist pattern, the `WindowsSandboxRemoteSession`
  liveness probe, the `shutdown /p` choice).
- **Simplify the pre-flight.** It currently does `Get-Command
  WindowsSandbox.exe` and *then* opportunistically queries
  `Get-WindowsOptionalFeature` only when elevated. Decide whether the
  elevated branch is actually pulling its weight — if it just produces
  a marginally clearer error message, drop it and keep a single clean
  Get-Command probe.
- **Factor repeated patterns** if you find any (e.g. `[wsb]`-prefixed
  output is currently just a small helper; check if there's anything
  bigger that could become a helper).
- **Do not weaken behaviour.** All existing flags (`-DryRun`,
  `-KeepOpen`, `-TimeoutMinutes`) must keep working with their current
  semantics. All exit codes (0 = pytest-passed, 1–4 = pytest, 124 =
  timeout, 125 = sandbox closed before result) must be preserved.

### Task B — Replace inside-sandbox `shutdown /s /t 0 /f` with the clean teardown

The current default-mode LogonCommand uses
`start powershell -wait {...}; shutdown /s /t 0 /f`
which closes the sandbox but raises the cosmetic Windows dialog
**0x80072746** ("connection aborted by host"). Research finished in
`.superpowers/notes/wsb-shutdown-research.md` (READ THIS FILE before
designing) recommends:

- **24H2+ path (preferred):** the `wsb` CLI's `wsb start --config <path>
  --raw` returns a JSON blob containing the sandbox `id`. Wait for
  `done.flag`, then `wsb stop --id <id>` from the host. No
  inside-sandbox shutdown call. No 0x80072746 dialog.
- **Pre-24H2 fallback:** keep launching via `WindowsSandbox.exe
  <wsb-file>`, and have the LogonCommand append `shutdown /p` (NOT
  `/s /t 0 /f` — `/p` is immediate power-off, less UI noise) after
  the runner exits. Accept that this path will surface the cosmetic
  0x80072746 dialog.

Implement both paths with autodetection:

```powershell
$wsbCli = Get-Command 'wsb' -ErrorAction SilentlyContinue
if ($wsbCli) { ... 24H2 path ... } else { ... fallback ... }
```

`-KeepOpen` mode is unchanged: no shutdown command anywhere, the
sandbox lingers until the user closes it manually.

The poll loop logic needs to handle both modes too:
- 24H2 mode: after `done.flag` is observed, the host calls
  `wsb stop --id $sandboxId`; the existing
  `WindowsSandboxRemoteSession`-based detection becomes a backup for
  the case where the user closes the sandbox manually before tests
  finish.
- Fallback mode: existing logic stays, but the LogonCommand string
  changes to use `shutdown /p`.

## Hard constraints

- **Stay on `psmux-support` branch.** Do not switch, rebase, fetch, or
  push. Do not create new branches.
- **Conventional commits.** Co-author both Claude personalities:
  ```
  Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
  Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
  ```
- **Never `--no-verify`.** If a hook fails, fix the cause, do not
  bypass.
- **Edit only:** `scripts/run-tests-in-wsb.ps1`, `scripts/wsb/run-tests.ps1`.
  Do not touch `src/`, `test/`, `pyproject.toml`, the spec file in
  `.superpowers/specs/`, or anything else.
- **Two commits expected:** one for cleanup (Task A), one for the
  shutdown rework (Task B). If they are too tangled, one commit is OK
  but call it out in the final report.

## Verification before commit

For each commit run all of these — zero errors required:

1. **PSScriptAnalyzer**:
   ```powershell
   Invoke-ScriptAnalyzer -Path scripts/run-tests-in-wsb.ps1 -Severity Error,Warning -ExcludeRule PSAvoidUsingWriteHost
   Invoke-ScriptAnalyzer -Path scripts/wsb/run-tests.ps1   -Severity Error,Warning -ExcludeRule PSAvoidUsingWriteHost
   ```
   `PSAvoidUsingWriteHost` warnings on interactive progress lines are
   acceptable — keep them excluded.

2. **Dry-run both modes** of the launcher and confirm exit 0:
   ```powershell
   .\scripts\run-tests-in-wsb.ps1 -DryRun
   .\scripts\run-tests-in-wsb.ps1 -DryRun -KeepOpen
   ```
   Read the printed `.wsb` XML and confirm the `<LogonCommand>` content
   matches your design for each mode.

3. **Generated `.wsb` XML must parse cleanly:**
   ```powershell
   $w = Get-ChildItem $env:TEMP\cao-wsb-*.wsb | Sort-Object LastWriteTime -Descending | Select-Object -First 1
   [xml](Get-Content $w.FullName -Raw) | Out-Null
   ```

4. **Don't actually launch a sandbox.** That's manual-test territory.
   The user has tested every prior iteration end-to-end and will run
   this one too.

## Don't get distracted

- Don't change the runner's bootstrap logic (uv install, psmux
  download, `uv sync` retry — all of that stays as-is).
- Don't change the spec doc.
- If you spot something genuinely broken outside Tasks A/B, note it in
  the final report but **do not fix it**.

## Final report

After both commits land, write a final summary (≤200 words) to STDOUT:

- Both commit hashes.
- One-line description of each commit.
- Static verification results for both commits.
- Any deviations from this brief and why.
- Anything Marc still needs to test manually.

Keep it tight — Marc reads this report directly.