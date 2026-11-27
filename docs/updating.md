# Updating CAO

`cao update` upgrades CAO in place. It reads uv's install receipt
(`<uv tool dir>/cli-agent-orchestrator/uv-receipt.toml`) to detect **how CAO was
installed**, then runs the uv command that actually advances that kind of
install — because a single command (`uv tool upgrade`) does *not* correctly
update every install source.

```bash
cao update
```

## Why the command is source-aware

CAO is installed as a [uv tool](https://docs.astral.sh/uv/concepts/tools/), and
uv records the original install source in the receipt. Different sources need
different upgrade actions:

| Installed via | Receipt shape | `cao update` runs | Why |
|---|---|---|---|
| `uv tool install git+…@main` (recommended) | `git = "…?rev=main"` | `uv tool upgrade cli-agent-orchestrator --reinstall` | uv reuses the receipt, including the Git ref and any `--with` requirements. `--reinstall` refreshes moving refs such as `@main`, while an explicit commit remains pinned. |
| `uv tool install cli-agent-orchestrator` | `{ name = "…" }` | `uv tool upgrade cli-agent-orchestrator` | Re-resolves to the latest published release. |
| `uv tool install 'cli-agent-orchestrator==X.Y.Z'` (or any constraint: `<`, `~=`, …) | `specifier = "…"` | `uv tool install cli-agent-orchestrator@latest --upgrade` | **Any version constraint** can hold the install below the latest release, making `uv tool upgrade` a no-op that still reports success; `@latest` unpins to the newest release. |
| `uv tool install .` (local clone) | `directory = "/path"` | *(nothing — prints guidance)* | A local source tree has no remote to advance. `cao update` tells you to update the source and reinstall. |
| `uv tool install ./dist/x.whl` | `path = "/path/x.whl"` | *(nothing — prints guidance)* | A built wheel is a frozen artifact. `cao update` tells you to rebuild and reinstall. |
| `uv tool install --editable .` | `editable = "/path"` | *(nothing — prints guidance)* | An editable clone has no remote to advance. `cao update` tells you to update the source and reinstall **with `--editable`** to preserve the editable install. |

For local installs, `cao update` exits non-zero and prints the exact commands
to run yourself, e.g. for a directory:

```text
update the local source, then reinstall: uv tool install /path --reinstall
(for a git checkout, first run: git -C /path pull)
```

## Requirements and fallbacks

- **uv must be on `PATH`.** `cao update` upgrades the uv tool install; it does
  not manage a `pip install`. If uv is absent, it says so and points you at
  [uv's install docs](https://docs.astral.sh/uv/).
- **CAO must have been installed as a uv tool.** If it wasn't, `uv` reports the
  install is unknown and `cao update` surfaces that with a non-zero exit —
  update it with the package manager you actually used.
- **A missing, unreadable, or malformed receipt** degrades safely to
  `uv tool upgrade cli-agent-orchestrator` rather than raising a parser
  traceback. (For a *malformed* receipt, `uv tool upgrade` may then reject it
  and exit non-zero with its own message — e.g. "missing a valid receipt" — but
  `cao update` never crashes with a Python traceback.)

## After updating

### Database upgrades, backups and recovery

On startup, CAO checks its numbered schema-migration record before modifying
the database. An unrecorded existing database is backed up before the 2.5
baseline is verified and stamped; future pending upgrades are backed up before
their first migration. Backups are consistent SQLite snapshots in
`CAO_HOME_DIR/db/backups/`, one per upgrade, with no automatic rotation. CAO
refuses an upgrade if it cannot make and verify a backup, or if available space
is less than twice the database file's size. Never use a live file copy as a
backup: it can omit transactions or WAL data.

If startup refuses a newer, unknown or interrupted migration, stop **every**
CAO process first. Move `CAO_HOME_DIR/db/cli-agent-orchestrator.db` and any
matching `-wal` and `-shm` files aside, then copy the indicated backup into
the original database path and reinstall the older CAO release that wrote it.
Inspect the recorded `cao_version` and the error's backup path before choosing
the release. Do not open a migrated database with an older CAO binary; it is
unsupported. A CLI process cannot apply a post-baseline upgrade while the
server is running: restart `cao-server` to let it complete the upgrade.

## Restarting the server

Restart any running `cao-server` so it picks up the new version:

```bash
cao update
# then restart your server, e.g.
cao-server
```
