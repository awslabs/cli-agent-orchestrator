---
name: cao-agent-plugin-creator
description: Author a new Agent Plugins 1.0.0 package from an empty directory - write the plugin.json manifest, lay out skills/, optionally add an mcp.json, and check the result with CAO's own validator. Use when the user says "create an Agent Plugin", "scaffold a plugin.json", "package these skills as an Agent Plugin", "which keys does the manifest need", "why is my plugin.json rejected", or "add an mcp.json to my package". States the manifest contract from the schema bytes CAO vendors and pins, and the Agent Skills limits CAO's loader does not check. Not for CAO event plugins, the Python packages that react to cao-server lifecycle events (use cao-plugin). Not for migrating an existing client-specific agent plugin to the portable structure (use migrate-agent-plugin). Not for auditing an existing Agent Plugin package against the limits (use cao-agent-plugin-audit).
---

# Create an Agent Plugin

Take a contributor from an empty directory to an Agent Plugins 1.0.0 package that
CAO's own validator reports as loadable. Every rule below is read from the schema
bytes CAO vendors, or from CAO's validator code, and each one names its source so
you can check it rather than trust it.

## Two unrelated things share the word "plugin"

CAO has two systems that both use the word. Never write it unqualified.

| Term | What it is | Where it lives in CAO |
|---|---|---|
| **Agent Plugin** | A portable directory of Agent Skills, optionally with MCP servers, described by a `plugin.json` manifest | `src/cli_agent_orchestrator/agent_plugins/`, `docs/agent-plugins.md` |
| **event plugin** | A Python package running inside `cao-server`, reacting to lifecycle events | `src/cli_agent_orchestrator/plugins/`, `docs/plugins.md` |
| **Agent Skill** | A directory holding a `SKILL.md`; the unit an Agent Plugin ships | `skills/`, `docs/skills.md` |

This skill is about the first row only. For an event plugin, load `cao-plugin`.
To convert an agent plugin that already exists for one client, load
`migrate-agent-plugin`; this skill starts from nothing.

## The smallest valid package

An Agent Plugin root needs exactly one file:

```text
my-package/
└── plugin.json
```

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "my-package"
}
```

That manifest is complete. Everything else is optional. A package that ships
skills adds a `skills/` directory:

```text
my-package/
├── plugin.json
├── mcp.json                 # optional
└── skills/                  # optional
    ├── first-skill/
    │   └── SKILL.md
    └── second-skill/
        ├── SKILL.md
        └── references/      # anything inside a skill directory is the skill's own business
            └── notes.md
```

## The manifest contract

The authority is the vendored schema,
`src/cli_agent_orchestrator/schemas/agent_plugins/1.0.0/plugin.schema.json`.
Line numbers below are into that file.

### Required keys: `$schema` and `name`, nothing else

`"required": ["$schema", "name"]` (`:63`). Those two keys are the whole required
set. A manifest with only them is valid.

- **`$schema`** must be exactly the string
  `https://agent-plugins.org/schemas/1.0.0/plugin.schema.json` (a `const`, `:8-11`).
  It is an identifier, not a download location; see "Vendored bytes" below.
- **`name`** is a string of **1 to 64 characters** (`minLength`, `maxLength`,
  `:14-15`) matching this pattern (`:16`):

  ```text
  ^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$
  ```

  In words: lowercase ASCII letters, digits, `.` and `-`; it starts and ends with a
  letter or digit; and it never contains `--` or `..`.

| `name` value | Valid? | Why |
|---|---|---|
| `my-package` | yes | |
| `tools.v2` | yes | `.` is allowed inside the name |
| `a` | yes | one character is within 1-64 |
| `My-Package` | no | uppercase |
| `-tools` | no | starts with `-` |
| `tools-` | no | ends with `-` |
| `my--tools` | no | contains `--` |
| `my..tools` | no | contains `..` |
| `my_tools` | no | `_` is not in the character set |

### Optional keys

These are the only other keys the schema defines (`:19-61`):

| Key | Type |
|---|---|
| `version` | string |
| `description` | string |
| `author` | object with only `name`, `email`, `url`, all strings; closed (`:25-39`) |
| `homepage` | string |
| `repository` | string |
| `license` | string |
| `keywords` | array of strings |
| `extensions` | object whose every member is an object, keyed by a reverse-domain namespace (`:55-61`) |

`description` is free text. Nothing checks that it matches the skills you ship, so
keep it accurate yourself: describe what the package contains today, not what you
plan to add.

### The schema is closed

`"additionalProperties": false` (`:64`), and `author` is closed too (`:38`). Any key
outside the sets above is a schema violation. How CAO's validator reports one
depends on where it is, and this was observed by running it:

- **An unknown key inside a defined object**, such as `author.twitter`, is
  **FATAL** (`manifest.invalid`). The package is not loadable.
- **An unrecognized top-level key** is the one tolerance CAO's validator applies
  (`_apply_manifest_tolerances` in
  `src/cli_agent_orchestrator/agent_plugins/validation.py`): a **WARNING**
  (`manifest.unknown_field`), the key is ignored, and the package still loads. The
  conformance fixture `test/agent_plugins/fixtures/corpus/manifest-unknown-field/`
  records exactly this outcome.
- **A non-object `extensions`** is likewise a WARNING and is ignored. A well-formed
  `extensions` object is dropped without validating its contents, because CAO
  implements no extension namespace.

Do not rely on the tolerance. Against the schema an unknown key is invalid, and
another conforming client is free to reject it. Put client-specific data in
`extensions`, under a namespace you control.

### Files the schema does not require

**`README.md`, `CHANGELOG.md` and `LICENSE` are not required by either vendored
schema.** Neither `plugin.schema.json` nor `mcp.schema.json` mentions them. CAO's
own packages carry all three only because CAO's builder writes them
(`render_readme`, `render_changelog` and `SHARED_FILES` in
`scripts/build_agent_plugin.py`). Add them because your readers need them, not
because validation does.

## The `skills/` directory

- **It is optional.** A missing `skills/` is not an error; the package simply ships
  no skills.
- **Each immediate child directory of `skills/` is one Agent Skill, one level
  only.** CAO's discovery (`_discover_skills` in `validation.py`) lists
  `skills/` and never recurses into a grandchild to look for more skills. A skill
  nested at `skills/group/my-skill/` is not discovered as `my-skill`.
- A child whose name starts with `.` is ignored. A stray file directly under
  `skills/` is ignored.
- Each skill directory must contain a regular file named exactly `SKILL.md`, with
  YAML frontmatter declaring non-blank `name` and `description`.
- **The directory name must equal the frontmatter `name`.** CAO checks this.
- **Each skill is validated on its own.** A broken skill is reported as `SKIPPED`
  and its siblings still load. A broken skill never makes the whole package
  unloadable; a broken manifest does.

### The Agent Skills limits CAO's loader does not check

CAO's skill check is exactly three things: `name` is non-blank, `description` is
non-blank (`src/cli_agent_orchestrator/models/skill.py`), and the folder name
equals `name`. It does **not** enforce these Agent Skills limits, and
`cao plugin validate` reports a skill that breaks one only as a `WARNING`:

| Limit | Value |
|---|---|
| Skill `name` | 1-64 characters, matching `^[a-z0-9]+(-[a-z0-9]+)*$` |
| `description` | at most 1024 characters |
| Body | at most 500 lines |

**The body is the text after the closing `---` of the frontmatter, with leading and
trailing whitespace stripped.** Do not measure it with `wc -l SKILL.md`: that
counts the frontmatter and any trailing blank lines, over-counts, and sends you
trimming prose you did not need to trim.

Measure all three directly. This needs only `pyyaml` and the standard library; set
`SKILL` to your skill directory first:

```bash
SKILL=my-package/skills/first-skill
SKILL="$SKILL" uv run --with pyyaml python - <<'PY'
import os, pathlib, re, sys, yaml
d = pathlib.Path(os.environ["SKILL"])
text = (d / "SKILL.md").read_text(encoding="utf-8").strip()
# CAO's rule: after stripping the text, a delimiter is a line of three or more
# dashes starting at column 0. An indented '---' inside a value does not end it.
fm = re.compile(r"^-{3,}\s*$", re.M)
parts = fm.split(text, 2) if fm.match(text) else []
if len(parts) < 3:
    print("SKILL.md has no '---' ... '---' frontmatter block", file=sys.stderr); sys.exit(2)
meta = yaml.safe_load(parts[1]) or {}
missing = [k for k in ("name", "description") if not str(meta.get(k) or "").strip()]
if missing:
    print("frontmatter is missing or blank: " + ", ".join(missing), file=sys.stderr); sys.exit(2)
name, desc = str(meta["name"]).strip(), str(meta["description"]).strip()
body_lines = len(parts[2].strip().splitlines())
ok = (name == d.name and len(name) <= 64
      and re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name)
      and len(desc) <= 1024 and body_lines <= 500)
print(f"name={name} description_chars={len(desc)} body_lines={body_lines}")
sys.exit(0 if ok else 1)
PY
```

It exits 0 when every limit holds and 1 on any breach. A SKILL.md it cannot
measure, with no frontmatter or with `name` or `description` missing or blank,
prints the reason and exits 2, so "cannot measure" is never mistaken for "over a
limit". Like CAO, it strips surrounding whitespace from `name` and `description`
before comparing them. For a whole package at once,
load `cao-agent-plugin-audit`, whose bundled validator checks every skill in a
package against all five rules (name pattern, name length, folder equals `name`,
description length, body length) and also checks the manifest.

## `mcp.json`, if the package ships MCP servers

`mcp.json` is optional and sits at the Agent Plugin root next to `plugin.json`. Its
vendored schema is `src/cli_agent_orchestrator/schemas/agent_plugins/1.0.0/mcp.schema.json`:

- Required: `$schema`, exactly
  `https://agent-plugins.org/schemas/1.0.0/mcp.schema.json`, and `mcpServers`, an
  object keyed by server name.
- Closed at the top level, and every server entry is closed too.
- Each server is one of three shapes: `stdio` (requires `type` and `command`;
  optional `args`, `env`, `cwd`), streamable HTTP or SSE (each requires `type` and
  `url`; optional `headers`).

A minimal stdio example:

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
  "mcpServers": {
    "my-server": {
      "type": "stdio",
      "command": "my-server-binary",
      "args": ["--stdio"]
    }
  }
}
```

An unusable `mcp.json` disables MCP for the package and nothing else: the skills
still load (`_map_mcp` in `validation.py`). Never put a credential in `env` or
`headers`; a package is data that gets copied around.

## Vendored bytes: CAO never fetches a schema

CAO validates against the schema files vendored under
`src/cli_agent_orchestrator/schemas/agent_plugins/1.0.0/`, whose sha256 hashes and
upstream commit are recorded in `PIN.json` in the same directory. **It never
retrieves a schema over the network while loading an Agent Plugin.** The
validator's registry hook refuses every remote retrieval (`_refuse_retrieval` in
`validation.py`). So:

- The `$schema` URL in your manifest is matched as a string. Nothing downloads it.
- What "valid" means is fixed by those vendored bytes, not by whatever the URL
  serves today.
- Do not edit the vendored files or `PIN.json` to make a package pass. A
  `make check-agent-plugins-schemas` failure means the vendored bytes changed.

## Check the package with CAO's validator

The `cao plugin` commands are behind a release gate. **`CAO_AGENT_PLUGINS_ENABLED`
must be set to a truthy value**: CAO strips the value and lowercases it, then
compares it against `1`, `true` and `yes`, so `TRUE` and ` yes ` both work
(`src/cli_agent_orchestrator/agent_plugins/gate.py`). Set it inline, on the
command, rather than exporting it into your shell:

```bash
CAO_AGENT_PLUGINS_ENABLED=1 cao plugin validate ./my-package
```

**With `CAO_AGENT_PLUGINS_ENABLED` unset, `cao plugin validate` exits 1, prints a
refusal on stderr, and emits no JSON at all, even with `--json`.** It has not looked at your package.
That exit 1 is not a conformance result, so do not conclude the package is broken
from it.

For a machine-readable report, add `--json`, still with `CAO_AGENT_PLUGINS_ENABLED`
set inline:

```bash
CAO_AGENT_PLUGINS_ENABLED=1 cao plugin validate --json ./my-package
```

With the gate set, the command exits 0 when the package is loadable and 1 when it
is not. A FATAL finding still prints the JSON report, with `"loadable": false`,
before exiting 1. So when you script around this command, **decide by whether
stdout parses as JSON, never by the exit status**, which is 1 both for an unset gate
and for a FATAL finding.

Read the findings by severity. Text output prints the severity in capitals; the
JSON report spells it in lowercase (`"severity": "fatal"`).

| Severity | Meaning |
|---|---|
| `FATAL` | The package is not loadable. Usually the manifest. |
| `SKIPPED` | One skill, the `skills/` directory, or one MCP server entry was dropped; the rest of the package still loads. |
| `WARNING` | Reported, and nothing was dropped; the package still loads. |

A loadable package can still break the Agent Skills limits, because the validator
only warns about them. Read every `WARNING`, and run the measurement above or the
audit skill as well.

## Step by step

1. **Pick the name.** Check it against the manifest pattern and the 64-character
   bound before you create the directory. Use the same string for the directory.
2. **Write `plugin.json`** with `$schema` and `name`. Add `version`,
   `description`, `license` and the other optional keys only if you will keep them
   accurate.
3. **Add skills**, one directory per skill directly under `skills/`, each with a
   `SKILL.md` whose frontmatter `name` equals its directory name.
4. **Measure each skill** against the three limits with the block above.
5. **Add `mcp.json`** only if the package ships MCP servers.
6. **Validate**: `CAO_AGENT_PLUGINS_ENABLED=1 cao plugin validate --json ./my-package`.
   Expect `"loadable": true`. Read every finding, including the WARNINGs.
7. **Audit** the finished package with `cao-agent-plugin-audit`.

## Mistakes that look like success

- **Validating with the gate unset.** Exit 1 with no JSON looks like a broken
  package. It is a refused command.
- **Trusting the unknown-key tolerance.** CAO loads a manifest with an extra
  top-level key, with a WARNING. The schema does not allow it.
- **Nesting skills.** `skills/group/my-skill/` is not discovered as `my-skill`.
- **A folder name that differs from the frontmatter `name`.** The skill is
  `SKIPPED`, its siblings load, and the package still reports loadable, so the
  missing skill is easy to overlook. Compare the reported skill list with what you
  meant to ship.
- **Treating `loadable` as "meets the Agent Skills limits".** It does not measure
  them.
- **Adding README, CHANGELOG or LICENSE because "validation needs them".** It does
  not.

## Related skills

- `cao-agent-plugin-audit`: audit an existing Agent Plugin package, including the
  limits CAO's loader does not check.
- `cao-plugin`: CAO event plugins, which are unrelated to Agent Plugins.
- `migrate-agent-plugin`: convert an existing client-specific agent plugin to the
  portable structure.
