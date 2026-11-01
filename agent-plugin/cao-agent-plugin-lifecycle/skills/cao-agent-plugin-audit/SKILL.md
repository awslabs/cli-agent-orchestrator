---
name: cao-agent-plugin-audit
description: Audit an existing Agent Plugins 1.0.0 package - check its plugin.json manifest and mcp.json against CAO's vendored schemas, and mechanically check every Agent Skill it ships against the name pattern, the 64-character name bound, folder-equals-name, the 1024-character description limit and the 500-line body limit, four of which CAO's own validator never checks. Use when the user says "audit this Agent Plugin", "is this package conformant", "check these skills against the Agent Skills limits", "cao plugin validate says loadable, is it really fine", or "run gate 1 over skills/". Its validator script needs no CAO install and opens no network connection itself; its first run needs its three declared dependencies available. Not for authoring a new Agent Plugin (use cao-agent-plugin-creator). Not for CAO event plugins, the Python packages that react to cao-server lifecycle events (use cao-plugin). Not for auditing a client-specific agent plugin before migrating it (use migrate-agent-plugin).
---

# Audit an Agent Plugin

Check an existing Agent Plugins 1.0.0 package against two things: the vendored
manifest schemas, and the Agent Skills limits. The second half is the reason this
skill exists. **CAO's own validator does not check four of the five skill rules
below (the name pattern, the 64-character name bound, the 1024-character
description limit and the 500-line body limit), and no other artifact CAO ships
enforces them mechanically.**

This skill audits a package that already follows the Agent Plugins 1.0.0
structure. It is not a pre-migration review of a client-specific agent plugin;
that is `migrate-agent-plugin`'s job.

## What CAO checks, and what it does not

CAO's package validation (`cao plugin validate`, and the loader behind it) checks
a skill for exactly three things:

1. frontmatter `name` is present and not blank;
2. frontmatter `description` is present and not blank
   (`src/cli_agent_orchestrator/models/skill.py`);
3. the folder name equals the frontmatter `name`
   (`src/cli_agent_orchestrator/utils/skills.py`).

**It does not check the Agent Skills name pattern, the 64-character name bound, the
1024-character description limit, or the 500-line body limit.** Apart from this
skill, nothing else CAO ships checks them either.

What CAO does with a skill that breaks those limits was at first only inferred
from `models/skill.py`. It has since been observed, in the over-limit experiment
run for issue #1153 (task T3) and again when this skill was written: a package
with one skill whose `description` is 2000 characters, or whose body is 600
lines, validates as `"loadable": true` with `"findings": []` and exit 0, and the
skill is loaded. **That observation covers only the two length limits.** T3 did
not test a skill that breaks the name pattern or the 64-character name bound, so
what CAO does with one is **inferred, not observed**: it is read from
`models/skill.py:12-19`, which checks only that `name` and `description` are not
blank, and `utils/skills.py:66-69`, which checks only that the folder name equals
`name`. Neither applies the pattern or the bound. Reproduce the length case in a
temporary directory:

```bash
W=$(mktemp -d); mkdir -p "$W/pkg/skills/long-desc"
printf '{"$schema":"https://agent-plugins.org/schemas/1.0.0/plugin.schema.json","name":"pkg"}' > "$W/pkg/plugin.json"
printf -- '---\nname: long-desc\ndescription: %s\n---\nbody\n' "$(printf 'x%.0s' $(seq 1 2000))" \
  > "$W/pkg/skills/long-desc/SKILL.md"
CAO_AGENT_PLUGINS_ENABLED=1 cao plugin validate --json "$W/pkg"   # loadable true, findings [], exit 0
rm -rf "$W"
```

So a package that passes `cao plugin validate` has not been shown to meet the
Agent Skills limits. Do not tell anyone CAO rejects an over-limit skill. A skill
over either length limit is loaded (observed); a skill that breaks only the name
pattern or the 64-character bound is, by reading the code, loaded too (inferred).

## Prior art: a checklist, by hand

This is not the first audit aid for Agent Plugins. The `migrate-agent-plugin`
skill (CAO carries it as a conformance fixture under
`test/agent_plugins/fixtures/canonical-example/`) ships
`references/validation-checklist.md`, which covers naming **by hand**: one
checkbox says the manifest `name` "satisfies the v1 length and character rules",
another that the frontmatter `name` "matches the directory name and Agent Skills
naming rules". It is 30 human checkboxes. **It mentions neither the 1024-character
description limit nor the 500-line body limit.** This skill's addition is that
the checks are mechanical, and that it covers those two limits.

## The five skill rules

Every skill is checked against all five. The body is the text after the closing
`---` of the frontmatter, stripped; a whole-file line count over-counts. The name
pattern for rule 1 is:

```text
^[a-z0-9]+(-[a-z0-9]+)*$
```

| # | Rule | Limit | Does CAO check it? |
|---|---|---|---|
| 1 | Name pattern | the pattern above | no |
| 2 | Name length | at most 64 characters | no |
| 3 | Folder name equals frontmatter `name` | exact | yes |
| 4 | Description length | at most 1024 characters | no |
| 5 | Body length | at most 500 lines | no |

The full rule set, every finding code and its severity are in
[references/rules.md](references/rules.md).

## Two validators, reported separately

This skill uses two validators and keeps their findings apart: CAO's appear
under their own `cao.` codes, never as the bundled validator's.

**1. The bundled validator, `scripts/validate_agent_plugin.py`.** It is a single
Python file with a PEP 723 block declaring `jsonschema` and `pyyaml`, both of which
it imports, plus `referencing`, the library `jsonschema` uses to resolve schema
references, which it also imports. Run it with `uv run --script`. **It does not
need CAO installed, and it opens no network connection itself**: schema references
resolve only against the two bundled schemas and any other retrieval is refused.
Its first run needs its three declared dependencies available, which means a uv
cache that already holds them or a reachable package index. It validates the manifest against a bundled
copy of the schema bytes under this skill's `schemas/1.0.0/`, and refuses to run
(exit 2) unless those bytes hash to the sha256 values CAO records in
`src/cli_agent_orchestrator/schemas/agent_plugins/1.0.0/PIN.json`. When it runs
inside a CAO checkout or installed wheel, it also finds CAO's `PIN.json` and
refuses to run if the two pins disagree. It never fetches a schema: the `$schema`
URL in a manifest is an identifier, compared as a string.

**2. CAO's own validator, `cao plugin validate --json`.** This needs a CAO
install, and it needs `CAO_AGENT_PLUGINS_ENABLED` set to a truthy value. CAO strips
the value and lowercases it, then compares it against `1`, `true` and `yes`. The
bundled validator runs it only when that variable is already set in its
environment, and reports its verdict on a line of its own. When the variable is
unset it says so and skips this step; it does not set the variable for you.

**With `CAO_AGENT_PLUGINS_ENABLED` unset, `cao plugin validate` exits 1, prints a
refusal on stderr, and emits no JSON and no conformance result.** That exit 1 says
nothing about the package.

### Telling the exit-1 cases apart

`cao plugin validate --json` exits 1 in more than one situation, so **the exit
status is not the discriminator. Whether stdout parses as JSON is.**

| Situation | Exit | stdout |
|---|---|---|
| Gate set, package loadable | 0 | JSON, `"loadable": true` |
| Gate set, a FATAL finding | 1 | JSON, `"loadable": false` |
| Gate set, path does not exist (observed) | 1 | JSON, `"loadable": false`, finding `plugin.root_invalid` |
| Gate unset | 1 | nothing; refusal text on stderr |

If stdout parses, read `loadable` and `findings`. If it does not parse, you know
only that this was not a FATAL report; do not conclude "the gate was unset" from
the missing JSON alone. Report the stderr text you actually saw. The bundled
validator does exactly that: no JSON from `cao plugin validate` while the gate is
set stops the audit with exit 2 and quotes the first stderr line. So does a run
that does not finish within 120 seconds. When CAO's JSON does arrive, every
finding in it is reported as a `WARNING` named `cao.<code>`, so a skill CAO
skips changes the audit's exit status even while CAO still says loadable.

### Schema validation is not the same as an allowlist comparison

Some validators check a manifest by comparing its keys with a hand-written list of
allowed keys, or by comparing that list with a schema's property set. That is not
validating against the schema: it misses types, the `name` pattern, the `const`
on `$schema`, and nested closed objects such as `author`. The bundled validator
runs the vendored JSON Schema (draft 2020-12) over the manifest and over
`mcp.json`. When you cite any other validator, say which of the two it does.

## Run the audit

From the repository root, against an Agent Plugin root (a directory holding
`plugin.json`):

```bash
uv run --script skills/cao-agent-plugin-audit/scripts/validate_agent_plugin.py ./agent-plugin/cao
```

To include CAO's own verdict, set the gate inline on the same command:

```bash
CAO_AGENT_PLUGINS_ENABLED=1 uv run --script \
  skills/cao-agent-plugin-audit/scripts/validate_agent_plugin.py ./agent-plugin/cao
```

To check a directory of skills that is not a package, use `--skills-dir`. This is
the mode gate 1 uses over this repository's `skills/`:

```bash
uv run --script skills/cao-agent-plugin-audit/scripts/validate_agent_plugin.py --skills-dir skills/
```

**`skills/` in this repository is not an Agent Plugin root.** It holds
`skills/plugin.json`, an inert manifest with no `$schema`, so the positional form
rejects it with a FATAL finding. `--skills-dir` treats each immediate child that
contains a `SKILL.md` as one skill and **ignores everything else**: a `plugin.json`
at the top of the directory, and any child without a `SKILL.md`, such as
`skills/vendor/`. It prints an `ignored:` line for each. It reads no manifest, so
the manifest checks do not run in this mode.

Inside an installed Agent Plugin, the script is at
`skills/cao-agent-plugin-audit/scripts/validate_agent_plugin.py` under the package
root; use that path instead.

### Exit status

| Exit | Meaning |
|---|---|
| 0 | No finding above advisory. |
| 1 | At least one finding: FATAL, SKIPPED, LIMIT or WARNING. |
| any other | The audit itself failed: bad arguments, an unreadable or missing input, schema bytes that do not hash to the pin, or a crash. Its findings, if any, are not a result. |

Treat exit 2 as "the audit did not happen", never as "findings" and never as
"clean". The script never reads stdin.

## Reading the output

Each finding is one line: severity, code, subject in brackets, message. The last
line is a summary with a count per severity and the exit status.

| Severity | Makes the package unloadable? | Meaning |
|---|---|---|
| `FATAL` | **yes** | CAO would reject the whole package. Almost always the manifest. |
| `SKIPPED` | no | CAO would drop one skill, or MCP, and load the rest. |
| `LIMIT` | no | Breaks an Agent Skills limit. CAO does not check these: a skill over a length limit is loaded (observed); one that breaks only the name pattern or the 64-character bound is loaded too, by reading the code (inferred). |
| `WARNING` | no | CAO reports it and loads the package, for example an unrecognized top-level manifest key. |
| `ADVISORY` | no | Input for a human judgement. Never changes the exit status. |

The manifest is checked with CAO's two tolerances mirrored, so the verdict matches
CAO's: an unrecognized top-level key and a non-object `extensions` are WARNINGs
and are dropped before schema validation. Every other schema violation, including
an unknown key inside `author`, is FATAL.

### The description listing is advisory, and only a listing

In package mode, whenever it could read the manifest, the audit emits one
`ADVISORY` line, `manifest.description_listing`. It prints the manifest's declared
`description` beside the names of the skills that would load: a skill reported
`SKIPPED` is left out of the list. A manifest can describe skills the
package does not ship, and every mechanical check here still passes. **The audit
does not judge whether the two correspond.** That is a question about meaning, and
it is yours to answer by reading the line. The advisory alone never makes the
audit exit non-zero.

## Step by step

1. Run the bundled validator against the package root. Record the exit status.
2. If exit is 2, fix the cause it printed and run again. Nothing else is a result.
3. Read every FATAL first: the package does not load until they are gone.
4. Read SKIPPED: each one is a skill or an MCP server CAO will silently drop.
   Compare the discovered skill list with what the package is meant to ship.
5. Read LIMIT: fix each skill until it is inside the limits.
6. Read the ADVISORY listing and decide whether the description is accurate.
7. Run again with `CAO_AGENT_PLUGINS_ENABLED=1` set inline to add CAO's verdict.
   Report the two validators' results separately.

## When you report an audit

- Quote the command and its exit status.
- Quote the summary line verbatim.
- Name which validator produced each finding.
- Say "loadable" only about CAO's verdict, and say which limits it does not check.
- Do not say the package meets the Agent Skills limits unless the bundled
  validator ran and exited 0 or 1 with no LIMIT finding.

## Related skills

- `cao-agent-plugin-creator`: author a new Agent Plugin from an empty directory.
- `cao-plugin`: CAO event plugins, which are unrelated to Agent Plugins.
- `migrate-agent-plugin`: convert a client-specific agent plugin to the portable
  structure.
