# Local Tool Install Verification Design

## Goal

Verify that this rebased `aws-cao` checkout works when installed as a local `uv` tool, using the actual packaged CLI entry points rather than only `uv run` from the repo checkout.

## Scope

This verification covers:
- local installation from the current checkout via `uv tool install . --reinstall`
- installed CLI entry points resolving and starting correctly
- minimal command-surface verification for `cao` and `cao-server`

This verification does **not** yet cover:
- full end-to-end multi-agent orchestration
- exhaustive provider validation
- a publishable wheel or release process
- deep tmux/psmux runtime workflow validation beyond a minimal smoke path

## Chosen Approach

Use the local checkout directly as the installation source:

```powershell
uv tool install . --reinstall
```

Then verify the installed entry points with:

```powershell
cao --help
cao-server --help
```

## Why This Approach

This is the shortest path that still validates the thing we care about:
- packaging metadata from `pyproject.toml`
- console script generation for `cao` / `cao-server`
- installed-tool behavior rather than only in-repo execution

It also avoids the unnecessary extra step of building a wheel first, which would test a broader packaging surface than required for this immediate verification.

## Alternatives Considered

### 1. `uv sync` + `uv run`
Pros:
- least invasive to the local tool environment
- very good for development-time verification

Cons:
- does not verify the installed-tool path
- does not prove the console entry points are wired correctly in the tool environment

### 2. Build wheel, then install wheel
Pros:
- closest to release packaging behavior
- validates built artifact production explicitly

Cons:
- broader and slower than needed
- extra moving parts for the first verification pass

## Execution Plan

1. Install from the local checkout with `uv tool install . --reinstall`
2. Verify `cao --help` exits successfully
3. Verify `cao-server --help` exits successfully
4. Inspect output for obvious import/path/entry-point failures
5. If all pass, treat packaging and installed CLI surface as verified
6. Optionally proceed to a minimal Windows/psmux runtime smoke test in a separate step

## Success Criteria

Verification succeeds if all of the following are true:
- `uv tool install . --reinstall` completes successfully
- `cao --help` exits successfully
- `cao-server --help` exits successfully
- no import errors, missing-module errors, or entry-point failures appear

## Risks / Caveats

- This mutates the local `uv` tool installation for `cli-agent-orchestrator`
- If another CAO install already exists, this step intentionally replaces it with the local checkout version
- Passing `--help` checks proves installability and entry-point wiring, but not the full Windows/psmux orchestration path

## Follow-up After This Verification

If the install and help checks pass, the next verification step should be a minimal runtime smoke test relevant to this branch's goal: one CAO path that exercises the installed CLI against the local Windows/psmux environment.
