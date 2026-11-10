# Rules and finding codes

Every check `scripts/validate_agent_plugin.py` performs, with its finding code and
severity. Severities, in order: `FATAL` (the package does not load), `SKIPPED`
(one skill or MCP is dropped, the rest loads), `LIMIT` (an Agent Skills limit CAO
does not enforce), `WARNING` (CAO reports it and loads the package), `ADVISORY`
(input for a human; never changes the exit status).

The "CAO" column says whether CAO's own validator reports the same condition.
"warns" means `cao plugin validate` reports it as a `WARNING` with the same code
and still loads the skill.

## Schema bytes

These stop the audit with exit 2 instead of producing a finding, because a
validator running on unknown schema bytes has no result to report.

| Condition | Effect |
|---|---|
| A bundled schema file under `schemas/1.0.0/` is missing | exit 2 |
| A bundled schema file does not hash to its pinned sha256 | exit 2 |
| CAO's `PIN.json` is found above the script and records different hashes | exit 2 |
| CAO's `PIN.json` is found but cannot be read | exit 2 |

The pinned values are the `PINNED_SHA256` constant in the script. They equal the
`sha256` entries in `src/cli_agent_orchestrator/schemas/agent_plugins/1.0.0/PIN.json`.

## Manifest (`plugin.json`), package mode only

| Code | Severity | CAO | Condition |
|---|---|---|---|
| `manifest.missing` | FATAL | yes | no `plugin.json` at the package root |
| `manifest.invalid_json` | FATAL | yes | not UTF-8, not JSON, or unreadable |
| `manifest.not_an_object` | FATAL | yes | the document is not a JSON object |
| `manifest.schema_missing` | FATAL | yes | no `$schema` key |
| `manifest.schema_unsupported` | FATAL | yes | `$schema` is not the 1.0.0 `plugin.schema.json` identifier |
| `manifest.unknown_field` | WARNING | yes | a top-level key outside the schema's properties; ignored, as CAO does |
| `manifest.extensions_not_object` | WARNING | yes | `extensions` is not an object; ignored, as CAO does |
| `manifest.invalid` | FATAL | yes | any other violation of the vendored schema: a missing `name`, a `name` outside the pattern or the 1-64 bound, a wrong type, an unknown key inside `author` |
| `manifest.description_listing` | ADVISORY | no | emitted whenever a manifest was read: the declared `description` beside the names of the skills that would load (a SKIPPED skill is left out) |

`manifest.description_listing` lists two things side by side. It makes no
judgement about whether they correspond, and no input changes its severity.

## `mcp.json`, package mode only

| Code | Severity | CAO | Condition |
|---|---|---|---|
| `mcp.invalid_json` | SKIPPED | yes | not UTF-8, not JSON, or unreadable; CAO disables MCP and loads the skills |
| `mcp.invalid` | SKIPPED | yes | violates the vendored `mcp.schema.json` |

An absent `mcp.json` is not a finding.

## Skill discovery, package mode only

Discovery mirrors CAO's top-level checks, in CAO's order: immediate children of
`skills/`, never recursive; a child whose name starts with `.` is ignored; a child
that resolves outside the package root is skipped; a regular file directly under
`skills/` is ignored; a `SKILL.md` that resolves outside the root, through a
symlink, is skipped; a missing `SKILL.md` is skipped. An absent `skills/` is not a
finding.

**Not mirrored:** CAO also walks every nested entry inside a skill and skips the
skill if a nested symlink escapes the root, dangles or loops. The bundled walk does
not repeat that. With `CAO_AGENT_PLUGINS_ENABLED` set, CAO's own findings are
reported (see "CAO's verdict" below), so such a skill still changes the exit
status; with the gate unset it is not detected.

| Code | Severity | CAO | Condition |
|---|---|---|---|
| `skills.not_a_directory` | SKIPPED | yes | `skills` exists and is not a directory |
| `skill.escapes_root` | SKIPPED | yes | the child, or its `SKILL.md`, resolves outside the package root |
| `skill.missing_skill_md` | SKIPPED | yes | the child has no `SKILL.md` |

In `--skills-dir` mode, a child without a `SKILL.md` is not a finding: it is
printed as `ignored:`. So is a `plugin.json` at the top of the directory.

## Each skill, both modes

| Code | Severity | CAO | Condition |
|---|---|---|---|
| `skill.unparseable` | SKIPPED | yes | no `---` frontmatter, unclosed frontmatter, invalid YAML, or frontmatter that is not a mapping |
| `skill.blank_field` | SKIPPED | yes | `name` or `description` missing or blank |
| `skill.folder_name` | SKIPPED | yes | folder name differs from frontmatter `name` |
| `skill.name_pattern` | LIMIT | warns | `name` does not match the Agent Skills pattern below |
| `skill.name_length` | LIMIT | warns | `name` is longer than 64 characters |
| `skill.description_length` | LIMIT | warns | `description` is longer than 1024 characters |
| `skill.body_length` | LIMIT | warns | the body, after the closing `---` and stripped, is longer than 500 lines |

The Agent Skills name pattern:

```text
^[a-z0-9]+(-[a-z0-9]+)*$
```

It is stricter than the manifest `name` pattern, which also allows `.`.

## CAO's verdict, package mode only

Runs only when `CAO_AGENT_PLUGINS_ENABLED` is already set to `1`, `true` or
`yes` (stripped, lowercased) and a `cao` executable is on `PATH`.

| Code | Severity | Condition |
|---|---|---|
| `cao.<code>` | WARNING | one per finding in CAO's JSON, whatever CAO's own severity; the message names that severity. A `LIMIT` finding reappears here as `cao.skill.<limit>` |
| `cao.not_loadable` | FATAL | `cao plugin validate --json` printed JSON whose `loadable` is not `true` |

The command runs with the package root resolved to an absolute path and a timeout
of 120 seconds (`CAO_VALIDATE_TIMEOUT_SECONDS`). If it prints no JSON while the
gate is set, or does not finish in time, the audit stops with exit 2 and, for no
JSON, quotes the first line of stderr. It does not infer why.
