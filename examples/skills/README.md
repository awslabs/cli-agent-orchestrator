# Skills Lifecycle Example

This example demonstrates **CAO's reusable skill lifecycle** — install a skill,
discover it, load its full guidance, apply it from an agent profile, update it
through the documented force path, and remove it cleanly — using a small,
self-contained skill bundled in this directory. The skill system itself is the
feature; it is not hidden setup for something else. The canonical reference is
[docs/skills.md](../../docs/skills.md).

## What it demonstrates

| Step | Command / surface | What proves it worked |
|------|-------------------|-----------------------|
| Install | `cao skills add python-review-checklist/` | The skill folder lands in `<CAO_HOME_DIR>/skills/` |
| Reinstall guard | `cao skills add` again (no `--force`) | Refused: `Skill '...' already exists. Use --force to overwrite it.` |
| Update (documented force path) | `cao skills add ... --force` | Overwrite succeeds — the only overwrite path |
| Discover | `cao skills list` | The skill is reported with its description |
| Load full body | `load_skill` MCP tool / `load_skill_content` | The complete Markdown body, output contract included |
| Profile linkage | profile `skills: [python-review-checklist]` | `build_skill_catalog(profile.skills)` — the exact call terminal launch makes — advertises only this skill |
| Fixture task | `fixture/apply_checklist.py` | `Checklist applied: 4/4 items` — observable behavior derived from the skill guidance |
| Remove | `cao skills remove python-review-checklist` (and the example decoy) | Both example skills disappear; default mode reports `No skills found`, while live mode may still list server-seeded built-ins |

## Prerequisites

- Default mode: `cao` on PATH (installed with the package). **No cao-server,
  no provider credentials, no network access** — the whole run is local and
  deterministic. The fixture helper scripts resolve the interpreter that ships
  `cao` (uv venvs place `python` next to the entry-point shims) and fall back
  to `python3`.
- `--live` mode additionally requires the `claude_code` CLI installed and
  authenticated. The script starts and stops its own `cao-server` on a free
  localhost port with the same temporary `CAO_HOME_DIR`; an already-running
  operator server is not used.

## Isolation

Every step runs inside a temporary `CAO_HOME_DIR` created with `mktemp -d` and
exported **before** any `cao` invocation (`constants.py` reads the env var once
at import). Your installed skills, profiles, and agent files are never touched;
the temporary home is deleted on exit.

## Running

```bash
./run.sh            # deterministic lifecycle — no server, no credentials
./run.sh --live     # + one real claude_code session (needs the authenticated claude CLI)
```

### Expected output (default mode)

`run.sh` logs each step to stderr (`[skills-example] step: ...`) and fails fast
with `[skills-example] FAIL: <reason>` on the first broken assertion. A full
pass ends with:

```
[skills-example] OK: full skill lifecycle verified
```

Exit code `0`; any assertion failure exits `1` and names the step.

### Expected output (--live mode)

After the deterministic steps, the script starts a **dedicated** `cao-server`
with the same temporary `CAO_HOME_DIR` and a free localhost port, then launches
the bundled `skills_lifecycle_reviewer` profile headless on `claude_code`. It
polls the session to a terminal state (`idle`/`completed`; `error` and
`waiting_user_answer` fail fast), then reads the full captured response from
JSON and asserts it contains the skill's output-contract line. The launch uses
`--auto-approve`, which skips the headless confirmation while preserving the
reviewer role's read-only tool policy; it deliberately does not use `--yolo`.
`CAO_SKILLS_LIVE_TIMEOUT` (default 600s) and `CAO_SKILLS_POLL_INTERVAL`
(default 5s) tune the polling.

## Why claude_code

The profile uses `provider: claude_code` because CAO delivers skills to
claude_code through the **runtime-prompt catalog** this example demonstrates —
see [docs/skills.md](../../docs/skills.md) §
["How Skills Work by Provider"](../../docs/skills.md#how-skills-work-by-provider).
Kiro CLI and OpenCode discover skills natively, Copilot receives a baked
catalog at install time; demonstrating every mechanism is a non-goal here —
this example shows the lifecycle, on one representative provider.

## Failure behavior

- Reinstalling without `--force` over an existing skill is **refused**, never a
  silent overwrite (`Skill '...' already exists. Use --force to overwrite it.`).
- With the skill absent, `fixture/apply_checklist.py` exits non-zero with
  `error: skill 'python-review-checklist' is not available` — which is exactly
  why the `Checklist applied:` line means something when it does appear.

## Canonical vs installed copies

The bundled skill is **not** one of CAO's built-in seeded skills. Built-in
skills are auto-seeded into the skill store at `cao-server` startup as
canonical copies that your edits preserve — see
[docs/skills.md](../../docs/skills.md) §
["Builtin skill seeding"](../../docs/skills.md#builtin-skill-seeding). This
example's skill is installed from a plain folder via `cao skills add`, becomes
a regular installed copy, and is removed again at the end.

## Updating a skill

The example demonstrates the guarded update path: reinstalling without
`--force` is refused; `cao skills add <folder> --force` is the documented
overwrite. Editing an installed skill in place is covered in
[docs/skills.md](../../docs/skills.md) §
["Updating a Skill"](../../docs/skills.md#updating-a-skill).

## Cleanup

- The temporary `CAO_HOME_DIR` is removed by the script's exit trap in both
  modes.
- In `--live` mode the dedicated cao-server and session are shut down by the
  same trap.
- Nothing else is created — no worktrees, no terminals outside the temporary
  home, no files in the repository.

## Tests

```bash
uv run pytest examples/skills/tests/ -v
```

- `test_skills_example.py` — deterministic, runs in normal CI: install,
  discovery, full-body retrieval, profile scoping (positive and empty-filter
  negative), the reinstall guard, AST-backed fixture findings (including a
  clean-fixture negative), the fixture's failure behavior, and a `run.sh`
  smoke test.
- `test_skills_live.py` — real-provider test, gated behind
  `CAO_RUN_LIVE_PROVIDER_TESTS=1` (never runs in CI or by default):

  ```bash
  CAO_RUN_LIVE_PROVIDER_TESTS=1 uv run pytest examples/skills/tests/test_skills_live.py -v
  ```

## Profile collision note

The bundled profile is named `skills_lifecycle_reviewer` (not a bare
`reviewer`), following the `tool_restrictions_` precedent in
[examples/tool-restrictions/](../tool-restrictions/): CAO ships built-in
profiles like `reviewer`, and `cao install` stores a profile under its
filename stem, so a bare name would overwrite the built-in.
