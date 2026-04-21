# Phase 6 — Rename OpenCode Config Directory to `~/.aws/opencode/`

## Summary

Phase 6 is a pure path rename: the CAO-owned OpenCode config directory moves from
`~/.aws/opencode_cli/` to `~/.aws/opencode/`. The provider identifier
(`ProviderType.OPENCODE_CLI.value == "opencode_cli"`) is unchanged — this rename
touches only on-disk paths, not code identifiers.

The change is driven by the design doc's Phase 6 spec, which found that
`opencode_cli` was used as a directory name when it should have matched the
more natural `opencode` namespace already used by the tool itself.

## Files Created or Modified

### Modified — `src/cli_agent_orchestrator/constants.py`

- Line 85: `OPENCODE_CONFIG_DIR = Path.home() / ".aws" / "opencode_cli"` →
  `Path.home() / ".aws" / "opencode"`
- `OPENCODE_AGENTS_DIR` and `OPENCODE_CONFIG_FILE` derive transitively — no further
  changes required in this file.

### Modified — `docs/opencode-cli.md`

- All eight `~/.aws/opencode_cli/` path occurrences replaced with `~/.aws/opencode/`
- Inline `OPENCODE_CONFIG_DIR=~/.aws/opencode_cli opencode --help` pre-population
  example updated to `OPENCODE_CONFIG_DIR=~/.aws/opencode opencode --help`

### Modified — `docs/feat-opencode-provider-design.md` (untracked, staged for first time)

- All nine `~/.aws/opencode_cli/` path occurrences replaced with `~/.aws/opencode/`
- `OPENCODE_CONFIG_DIR=~/.aws/opencode_cli` in the launch-command sample updated
- Phase 6 acceptance-criteria description reworded to avoid embedding the old path
  pattern as a literal string (which would have self-referentially failed the check)

### Modified — `CHANGELOG.md`

- Added migration note to the OpenCode CLI provider entry in `[Unreleased]`:
  > CAO's on-disk config directory for OpenCode is `~/.aws/opencode/` — users who
  > installed an earlier pre-release build (which used `~/.aws/opencode_cli`) must
  > re-run `cao install --provider opencode_cli` to populate the new location.

### Modified — `test/test_constants.py`

- `TestOpenCodeConstants::test_opencode_config_dir_resolves_correctly`: updated
  the asserted path from `Path.home() / ".aws" / "opencode_cli"` to
  `Path.home() / ".aws" / "opencode"`.

## How Acceptance Criteria Are Satisfied

| AC | Result |
|----|--------|
| `OPENCODE_CONFIG_DIR` constant updated | ✅ `Path.home() / ".aws" / "opencode"` |
| `rg -n "opencode_cli/" src/ docs/ test/ README.md CHANGELOG.md` → no hits | ✅ Zero hits |
| `ProviderType.OPENCODE_CLI.value == "opencode_cli"` unchanged | ✅ Confirmed |
| CHANGELOG migration note added | ✅ Exists in `[Unreleased]` OpenCode entry |
| All unit tests pass | ✅ 1444 passed, 7 skipped |
| mypy clean on `constants.py` | ✅ No errors |

## Design Decisions and Trade-offs

- **No data migration helper**: Per Phase 6 spec, users must re-run `cao install`. The
  old directory at `~/.aws/opencode_cli/` is simply abandoned — CAO never deletes
  user-owned directories. The CHANGELOG note informs affected users.
- **`opencode_cli` identifier preserved**: Only path strings changed. All CLI flags,
  enum values, test selectors, and provider names continue to use `opencode_cli` — the
  rename is invisible to users of the CLI interface.
- **Design doc self-referential AC text**: The Phase 6 task entry in the design doc
  originally contained `` `rg -n "opencode_cli/"` `` as the acceptance-check command,
  which would itself have matched the grep pattern. Reworded to describe the constraint
  in prose without embedding the old path string.

## Commits

`079f4a9` — `refactor(opencode): rename on-disk config directory from opencode_cli to opencode`

`bd92d87` — `fix(opencode): fall back to first agent-indented line when user message scrolled off viewport`

`89ad483` — `refactor(terminal_service): guard build_skill_catalog() call and update skill-delivery comments`

## Additional Fix Found During Live Verification

### Root-cause: OpenCode alt-screen mode, `history_size ≈ 2`

The Phase 6 live smoke triggered an intermittent e2e failure on `test_assign_report_generator`
("No user message found in OpenCode output"). Investigation showed:

- OpenCode renders its TUI in alternate-screen mode; `tmux history_size` for an active
  OpenCode pane is effectively 2 lines (just the shell prompt before launch).
- `tmux capture-pane -S -2000` therefore returns only the 41 currently visible lines
  (182×41 pane), regardless of `history-limit`.
- The Phase 5 `extraction_tail_lines=2000` fix was a misdiagnosis: the buffer was
  always ~41 lines, not 200. Phase 5 passed because the response happened to be short
  enough on that run to keep the user message in the visible frame.
- When the model produces a longer response (≥35 lines), the user message bar (`┃  `)
  scrolls off the top, and the original code raised `ValueError("No user message found")`.

### Fix (Commit A: `bd92d87`)

`extract_last_message_from_script` now falls back to the first 5-space-indented agent
line as the left boundary when no `┃  ` is found before the completion marker. The
visible frame already contains only the current turn's tail, so multi-turn ambiguity
is not an issue. A unit test `test_fallback_extracts_when_user_message_scrolled_off`
was added to cover this path. e2e: 3/3 PASSED after the fix.
