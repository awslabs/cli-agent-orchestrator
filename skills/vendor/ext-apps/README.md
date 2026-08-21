# Vendored ext-apps MCP Apps builder skills (offline path)

This directory holds an **offline / air-gapped** copy of the four MCP Apps
*builder* skills from the upstream
[`modelcontextprotocol/ext-apps`](https://github.com/modelcontextprotocol/ext-apps)
project:

- `create-mcp-app`
- `add-app-to-server`
- `migrate-oai-app`
- `convert-web-app`

The bytes here are vendored **unmodified** at a pinned upstream tag. See
[`NOTICE`](./NOTICE) for the exact source commit and the Apache-2.0 attribution.

## Online vs. offline — which path do I use?

There are two ways to get these skills into an agent. They deliver the *same*
upstream content; pick based on whether the machine has network access.

### Online path (default, no vendoring needed)

When the machine can reach the internet, use the **`mcp-apps-builder`** bridge
skill (shipped with CAO). It equips an agent with the upstream builder skills
on demand via the upstream distribution channel (`npx skills add` / the plugin
marketplace). Nothing in this directory is required for the online path — it
fetches the latest published skills directly from upstream.

### Offline path (this directory)

When the machine is **air-gapped** or cannot fetch from the upstream channel at
runtime, use the vendored copy committed here. The skills are plain folders, so
you add them with the normal CAO skill-add flow:

```bash
cao skills add skills/vendor/ext-apps/create-mcp-app
cao skills add skills/vendor/ext-apps/add-app-to-server
cao skills add skills/vendor/ext-apps/migrate-oai-app
cao skills add skills/vendor/ext-apps/convert-web-app
```

> **Opt-in by design.** These vendored skills are **not** part of CAO's default
> seed set (`seed_default_skills()` only reads the *package* skills under
> `src/cli_agent_orchestrator/skills/`, not this repo-root `skills/vendor/`
> tree). So `cao init` does **not** install them and the default footprint is
> unchanged — you add them explicitly only when you need the offline path.

## How the vendoring works

[`scripts/vendor_ext_apps_skills.py`](../../../scripts/vendor_ext_apps_skills.py)
clones the upstream repo at a **pinned** tag/commit (constants `PINNED_REF` /
`PINNED_SHA` in that script), copies the four skill folders into
`skills/vendor/ext-apps/<skill>/`, and (re)writes [`NOTICE`](./NOTICE) recording
the exact source commit. It is idempotent — re-running with the same pin
reproduces byte-for-byte identical output.

```bash
# Vendor / refresh against the current pin
python scripts/vendor_ext_apps_skills.py     # or: make refresh-ext-apps-skills

# Verify the on-disk copy matches the pin (CI / pre-commit)
python scripts/vendor_ext_apps_skills.py --check   # or: make check-ext-apps-skills
```

`--check` exit codes: `0` = in sync, `1` = drift (re-vendor + commit), `2` =
network-gated (the pin could not be fetched, so it could not be verified in this
environment).

## Refreshing the pin to a newer upstream release

1. Pick the new upstream tag and resolve its commit SHA, e.g.:

   ```bash
   git ls-remote --tags https://github.com/modelcontextprotocol/ext-apps.git
   ```

2. Update **both** `PINNED_REF` and `PINNED_SHA` in
   `scripts/vendor_ext_apps_skills.py`. They must agree — the script clones
   `PINNED_REF` and aborts if the resolved `HEAD` differs from `PINNED_SHA`, so
   a moved/retagged ref can never silently change the vendored bytes.

3. Re-vendor and commit the result:

   ```bash
   make refresh-ext-apps-skills
   git add skills/vendor/ext-apps scripts/vendor_ext_apps_skills.py
   git commit -S -m "chore: refresh vendored ext-apps skills to <new tag>"
   ```

4. (Optional) Re-validate each skill folder still loads:

   ```bash
   uv run python -c "from pathlib import Path; \
   from cli_agent_orchestrator.utils.skills import validate_skill_folder; \
   [validate_skill_folder(Path('skills/vendor/ext-apps')/s) for s in \
   ['create-mcp-app','add-app-to-server','migrate-oai-app','convert-web-app']]"
   ```
