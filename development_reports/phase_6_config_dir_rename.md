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

## Commit

`(see git log)`
