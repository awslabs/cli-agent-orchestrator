# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "jsonschema>=4.18",
#     "pyyaml>=6.0",
#     "referencing>=0.28",
# ]
# ///
"""Audit an Agent Plugins 1.0.0 package, or a directory of Agent Skills.

Two modes, chosen by the arguments:

  validate_agent_plugin.py PACKAGE_ROOT
      PACKAGE_ROOT is an Agent Plugin root, the directory holding plugin.json.
      Checks the manifest against the bundled schema bytes, checks mcp.json if
      present, discovers skills the way CAO's validator does at the top level
      (the nested-link walk is left to CAO's own verdict), and checks every
      skill against the five Agent Skills rules. Also runs
      `cao plugin validate --json` when CAO_AGENT_PLUGINS_ENABLED is set, and
      reports each finding CAO returns as a WARNING.

  validate_agent_plugin.py --skills-dir DIR
      Each immediate child of DIR that contains a SKILL.md is one skill, checked
      against the five rules. Everything else is ignored and reported as ignored:
      a plugin.json at the top of DIR, and any child without a SKILL.md.

The five rules: name pattern, name length (64), folder name equals frontmatter
name, description length (1024 characters), body length (500 lines, counted
after the closing frontmatter delimiter, stripped).

Exit status: 0 = no finding above advisory; 1 = at least one finding;
any other status = the audit itself failed (bad arguments, unreadable input,
schema bytes that do not hash to the pin, a `cao plugin validate` that prints
no JSON or does not finish within CAO_VALIDATE_TIMEOUT_SECONDS, or a crash).

This script never reads stdin and opens no network connection itself: schema
references are resolved only against the two bundled schemas, and any other
retrieval is refused. Running it with `uv run --script` needs its three declared
dependencies available, which on a first run means a uv cache or a package index.
The https string below is the $schema identifier, compared as text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import jsonschema
import yaml
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource

PLUGIN_SCHEMA_ID = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"

# The sha256 of each bundled schema file. These equal the hashes CAO records in
# src/cli_agent_orchestrator/schemas/agent_plugins/1.0.0/PIN.json. A bundled file
# that does not hash to its pin stops the audit (exit 2) rather than validating
# against unknown bytes.
PINNED_SHA256 = {
    "plugin.schema.json": "0a4aad95ce337878ad38802ebf0daa3fde76abe3f65400c86bcbb1ec0b3ab883",
    "mcp.schema.json": "6539175bfcdf43085855183e86da40ea94b166547a72b47ae9a0a390516d3acb",
}
SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas" / "1.0.0"
PIN_RELATIVE = (
    Path("src/cli_agent_orchestrator/schemas/agent_plugins/1.0.0/PIN.json"),
    Path("schemas/agent_plugins/1.0.0/PIN.json"),
)

SKILL_NAME_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
NAME_MAX = 64
DESCRIPTION_MAX = 1024
BODY_MAX = 500
GATE_VAR = "CAO_AGENT_PLUGINS_ENABLED"
GATE_TRUTHY = ("1", "true", "yes")
# How long `cao plugin validate --json` may run before the audit gives up (exit 2).
CAO_VALIDATE_TIMEOUT_SECONDS = 120

# Severity order. Only ADVISORY leaves the exit status at 0.
FATAL = "FATAL"  # the package is not loadable by CAO
SKIPPED = "SKIPPED"  # CAO drops one skill, or MCP, and loads the rest
LIMIT = "LIMIT"  # breaks an Agent Skills limit; CAO loads it and only warns
WARNING = "WARNING"  # CAO reports it (or a finding CAO returned) and the package still loads
ADVISORY = "ADVISORY"  # input for a human judgement; never changes the exit status


class AuditError(Exception):
    """The audit could not run to completion. Maps to exit status 2."""


class Report:
    def __init__(self) -> None:
        self.lines: List[str] = []
        self.counts: Dict[str, int] = {s: 0 for s in (FATAL, SKIPPED, LIMIT, WARNING, ADVISORY)}

    def info(self, text: str) -> None:
        self.lines.append(text)

    def finding(self, severity: str, code: str, subject: str, message: str) -> None:
        self.counts[severity] += 1
        self.lines.append(f"{severity:<8} {code} [{subject}]: {message}")

    def exit_status(self) -> int:
        return 1 if any(n for s, n in self.counts.items() if s != ADVISORY) else 0

    def summary(self) -> str:
        parts = ", ".join(f"{n} {s.lower()}" for s, n in self.counts.items())
        return f"summary: {parts}; exit {self.exit_status()}"


# --- schemas -----------------------------------------------------------------


def _find_cao_pin() -> Optional[Path]:
    """Look for CAO's PIN.json above this script; present in a CAO checkout or wheel."""
    for ancestor in Path(__file__).resolve().parents:
        for rel in PIN_RELATIVE:
            candidate = ancestor / rel
            if candidate.is_file():
                return candidate
    return None


def load_schemas(report: Report) -> Dict[str, Dict[str, Any]]:
    schemas: Dict[str, Dict[str, Any]] = {}
    for filename, pinned in PINNED_SHA256.items():
        path = SCHEMA_DIR / filename
        if not path.is_file():
            raise AuditError(f"bundled schema missing: {path}")
        data = path.read_bytes()  # hash and parse the SAME bytes
        actual = hashlib.sha256(data).hexdigest()
        if actual != pinned:
            raise AuditError(f"bundled {filename} hashes to {actual}, pin is {pinned}")
        schemas[filename] = json.loads(data.decode("utf-8"))
    _SCHEMA_RESOURCES[:] = [
        (str(schema.get("$id", name)), Resource.from_contents(schema))
        for name, schema in schemas.items()
    ]
    report.info("schemas: bundled plugin.schema.json and mcp.schema.json hash to the pinned sha256")

    pin = _find_cao_pin()
    if pin is None:
        report.info(
            "schemas: no CAO PIN.json found above this script; checked against the embedded pin only"
        )
        return schemas
    try:
        recorded = {
            name: entry["sha256"] for name, entry in json.loads(pin.read_text())["files"].items()
        }
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AuditError(f"CAO PIN.json at {pin} is unreadable: {exc}") from exc
    if recorded != PINNED_SHA256:
        raise AuditError(
            f"CAO PIN.json at {pin} records {recorded}, this validator pins {PINNED_SHA256}; "
            "re-bundle the schemas before auditing"
        )
    report.info(f"schemas: pin agrees with CAO's PIN.json at {pin}")
    return schemas


def _refuse_retrieval(uri: str) -> Resource:
    """Registry hook that refuses every retrieval, as CAO's validator does.

    The bundled schemas use only in-document "#/$defs" references. A reference to
    anything else must fail loudly here rather than reach the network.
    """
    # referencing's exceptions and Registry have attrs-generated __init__ signatures
    # that mypy reads wrongly; ref= and retrieve= are correct (CAO's validation.py
    # carries the same two ignores).
    raise NoSuchResource(ref=uri)  # type: ignore[call-arg]


_SCHEMA_RESOURCES: List[Tuple[str, Resource]] = []


def _registry() -> Registry:
    return Registry(retrieve=_refuse_retrieval).with_resources(  # type: ignore[call-arg]
        _SCHEMA_RESOURCES
    )


def _schema_errors(schema: Dict[str, Any], instance: Any) -> List[str]:
    validator = jsonschema.Draft202012Validator(schema, registry=_registry())
    errors = sorted(
        validator.iter_errors(instance), key=lambda e: (list(map(str, e.absolute_path)), e.message)
    )
    out = []
    for error in errors:
        location = ".".join(str(p) for p in error.absolute_path)
        out.append(f"{location}: {error.message}" if location else error.message)
    return out


# --- manifest ----------------------------------------------------------------


def _read_json(path: Path) -> Tuple[Optional[Any], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except UnicodeDecodeError as exc:
        return None, f"not UTF-8: {exc}"
    except ValueError as exc:
        return None, f"not valid JSON: {exc}"
    except OSError as exc:
        return None, f"unreadable: {exc}"


def check_manifest(root: Path, schema: Dict[str, Any], report: Report) -> Optional[Dict[str, Any]]:
    path = root / "plugin.json"
    if not path.is_file():
        report.finding(
            FATAL, "manifest.missing", "plugin.json", "no plugin.json at the package root"
        )
        return None
    data, problem = _read_json(path)
    if problem:
        report.finding(FATAL, "manifest.invalid_json", "plugin.json", problem)
        return None
    if not isinstance(data, dict):
        report.finding(
            FATAL, "manifest.not_an_object", "plugin.json", "the manifest must be a JSON object"
        )
        return None

    if "$schema" not in data:
        report.finding(
            FATAL,
            "manifest.schema_missing",
            "plugin.json",
            f"$schema is required and must be {PLUGIN_SCHEMA_ID}",
        )
    elif data["$schema"] != PLUGIN_SCHEMA_ID:
        report.finding(
            FATAL,
            "manifest.schema_unsupported",
            "plugin.json",
            f"$schema is {data['$schema']!r}; expected {PLUGIN_SCHEMA_ID}",
        )

    # CAO's two tolerances, mirrored so the verdict matches CAO's: an unrecognized
    # top-level key and a non-object `extensions` are WARNINGs and are dropped
    # before schema validation. The schema itself is closed.
    sanitized = dict(data)
    if "extensions" in sanitized:
        extensions = sanitized.pop("extensions")
        if not isinstance(extensions, dict):
            report.finding(
                WARNING,
                "manifest.extensions_not_object",
                "plugin.json",
                "'extensions' must be an object; CAO ignores it and loads the package",
            )
    known = set(schema.get("properties", {}))
    for key in sorted(set(sanitized) - known):
        sanitized.pop(key)
        report.finding(
            WARNING,
            "manifest.unknown_field",
            "plugin.json",
            f"top-level key {key!r} is outside the closed schema; CAO ignores it and loads the package, another client may reject it",
        )
    for message in _schema_errors(schema, sanitized):
        report.finding(FATAL, "manifest.invalid", "plugin.json", message)
    return data


def check_mcp(root: Path, schema: Dict[str, Any], report: Report) -> None:
    path = root / "mcp.json"
    if not path.exists():
        report.info("mcp.json: absent (optional)")
        return
    data, problem = _read_json(path)
    if problem:
        report.finding(
            SKIPPED,
            "mcp.invalid_json",
            "mcp.json",
            f"{problem}; CAO disables MCP for this package and still loads its skills",
        )
        return
    errors = _schema_errors(schema, data)
    for message in errors:
        report.finding(
            SKIPPED,
            "mcp.invalid",
            "mcp.json",
            f"{message}; CAO drops what fails and still loads the skills",
        )
    if not errors:
        report.info("mcp.json: valid against the bundled mcp.schema.json")


# --- skills ------------------------------------------------------------------


# CAO reads SKILL.md with python-frontmatter (``utils/skills.py``), whose YAML
# handler splits on this pattern. A delimiter is three or more dashes starting at
# column 0, so an indented ``---`` inside a block scalar is part of the value.
_FM_BOUNDARY = re.compile(r"^-{3,}\s*$", re.MULTILINE)


def _split_frontmatter(text: str) -> Tuple[str, str]:
    """Split SKILL.md the way CAO's loader does, without importing the library.

    python-frontmatter strips the whole text first, requires it to open with a
    delimiter, then splits at most twice. Porting the rule rather than declaring
    the dependency keeps a seeded ``uv run --script`` free of a network fetch for
    it.
    """
    text = text.strip()
    if not _FM_BOUNDARY.match(text):
        raise ValueError("SKILL.md does not start with a '---' frontmatter delimiter")
    parts = _FM_BOUNDARY.split(text, 2)
    if len(parts) < 3:
        raise ValueError("SKILL.md has no closing '---' frontmatter delimiter")
    return parts[1], parts[2]


def check_skill(skill_dir: Path, report: Report) -> Optional[str]:
    """Check one skill against the five rules. Returns its name if CAO would load it."""
    folder = skill_dir.name
    try:
        frontmatter, body = _split_frontmatter((skill_dir / "SKILL.md").read_text(encoding="utf-8"))
        meta = yaml.safe_load(frontmatter)
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
        report.finding(SKIPPED, "skill.unparseable", folder, f"{exc}; CAO would skip this skill")
        return None
    if not isinstance(meta, dict):
        report.finding(
            SKIPPED,
            "skill.unparseable",
            folder,
            "frontmatter is not a mapping; CAO would skip this skill",
        )
        return None

    name, description = meta.get("name"), meta.get("description")
    loadable = True
    for field, value in (("name", name), ("description", description)):
        if not isinstance(value, str) or not value.strip():
            report.finding(
                SKIPPED,
                "skill.blank_field",
                folder,
                f"frontmatter {field} is missing or blank; CAO would skip this skill",
            )
            loadable = False
    if not isinstance(name, str) or not isinstance(description, str):
        return None
    name, description = name.strip(), description.strip()

    body_lines = len(body.strip().splitlines())
    report.info(
        f"skill {folder}: name {len(name)} chars, description {len(description)} chars, body {body_lines} lines"
    )

    if name != folder:
        report.finding(
            SKIPPED,
            "skill.folder_name",
            folder,
            f"folder name {folder!r} differs from frontmatter name {name!r}; CAO would skip this skill",
        )
        loadable = False
    if not SKILL_NAME_RE.fullmatch(name):
        report.finding(
            LIMIT,
            "skill.name_pattern",
            folder,
            f"name {name!r} fails the Agent Skills pattern ^[a-z0-9]+(-[a-z0-9]+)*$",
        )
    if len(name) > NAME_MAX:
        report.finding(
            LIMIT,
            "skill.name_length",
            folder,
            f"name is {len(name)} characters; the limit is {NAME_MAX}",
        )
    if len(description) > DESCRIPTION_MAX:
        report.finding(
            LIMIT,
            "skill.description_length",
            folder,
            f"description is {len(description)} characters; the limit is {DESCRIPTION_MAX}",
        )
    if body_lines > BODY_MAX:
        report.finding(
            LIMIT,
            "skill.body_length",
            folder,
            f"body is {body_lines} lines after the frontmatter; the limit is {BODY_MAX}",
        )
    return name if loadable else None


def _within(real_root: Path, candidate: Path) -> bool:
    """Whether ``candidate``, with every symlink resolved, is inside ``real_root``."""
    try:
        resolved = Path(os.path.realpath(candidate))
    except OSError:
        return False
    return resolved == real_root or real_root in resolved.parents


def discover_package_skills(root: Path, report: Report) -> List[str]:
    """Mirror CAO's top-level discovery: immediate, non-recursive children of skills/.

    The order of checks follows CAO's ``_discover_skills``: dot entries, the
    child's containment, non-directories, the SKILL.md's containment, then its
    presence. CAO's walk of every nested entry in a skill (``_validate_skill_tree``)
    is not repeated here; with the gate set, CAO's own findings cover it.
    """
    skills_dir = root / "skills"
    if not skills_dir.exists():
        report.info("skills/: absent (optional)")
        return []
    if not skills_dir.is_dir():
        report.finding(
            SKIPPED,
            "skills.not_a_directory",
            "skills",
            "'skills' exists but is not a directory; CAO loads no skills",
        )
        return []
    real_root = Path(os.path.realpath(root))
    loaded: List[str] = []
    for child in sorted(skills_dir.iterdir(), key=lambda p: p.name):
        if child.name.startswith("."):
            continue
        if not _within(real_root, child):
            report.finding(
                SKIPPED,
                "skill.escapes_root",
                child.name,
                "resolves outside the package root; CAO would skip this skill",
            )
            continue
        if not child.is_dir():
            continue
        if not _within(real_root, child / "SKILL.md"):
            report.finding(
                SKIPPED,
                "skill.escapes_root",
                child.name,
                "SKILL.md resolves outside the package root; CAO would skip this skill",
            )
            continue
        if not (child / "SKILL.md").is_file():
            report.finding(
                SKIPPED,
                "skill.missing_skill_md",
                child.name,
                "no SKILL.md; CAO would skip this directory",
            )
            continue
        name = check_skill(child, report)
        if name:
            loaded.append(name)
    return loaded


# --- CAO's own verdict ---------------------------------------------------------


def run_cao_validate(root: Path, report: Report) -> None:
    """Run `cao plugin validate --json` when the gate is set, and report it separately.

    The discriminator is whether stdout parses as JSON, never the exit status: an
    unset gate and a FATAL finding both exit 1, but only the second prints JSON.
    No-JSON is reported with whatever stderr said, and no cause is inferred from it.
    """
    if os.environ.get(GATE_VAR, "").strip().lower() not in GATE_TRUTHY:
        report.info(f"cao plugin validate: not run ({GATE_VAR} is not set to 1, true or yes)")
        return
    cao = shutil.which("cao")
    if cao is None:
        report.info("cao plugin validate: not run (no cao executable on PATH)")
        return
    try:
        proc = subprocess.run(
            [cao, "plugin", "validate", "--json", str(root.resolve())],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=CAO_VALIDATE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise AuditError(
            f"cao plugin validate --json did not finish within {CAO_VALIDATE_TIMEOUT_SECONDS}s"
        ) from exc
    try:
        verdict = json.loads(proc.stdout)
    except ValueError:
        stderr = proc.stderr.strip().splitlines()
        seen = stderr[0] if stderr else "(stderr was empty)"
        raise AuditError(
            f"cao plugin validate --json exited {proc.returncode} and printed no JSON; stderr: {seen}"
        )
    loadable = verdict.get("loadable")
    findings = verdict.get("findings") or []
    report.info(
        f"cao plugin validate --json ({cao}): exit {proc.returncode}, loadable {loadable}, {len(findings)} findings"
    )
    # Each CAO finding is reported as a WARNING, so it changes the audit's exit
    # status even when CAO still calls the package loadable (a SKIPPED skill, say).
    for item in findings:
        if not isinstance(item, dict):
            continue
        code = item.get("code", "unknown")
        report.finding(
            WARNING,
            f"cao.{code}",
            str(item.get("path") or "plugin.json"),
            f"CAO reported {item.get('severity', '?')}: {item.get('message', '')}",
        )
    if loadable is not True:
        report.finding(
            FATAL,
            "cao.not_loadable",
            "plugin.json",
            "CAO's own validator reports the package not loadable",
        )


# --- entry points --------------------------------------------------------------


def audit_package(root: Path) -> Report:
    report = Report()
    report.info(f"cao-agent-plugin-audit: package {root}")
    schemas = load_schemas(report)
    manifest = check_manifest(root, schemas["plugin.schema.json"], report)
    check_mcp(root, schemas["mcp.schema.json"], report)
    loaded = discover_package_skills(root, report)
    if manifest is not None:
        declared = manifest.get("description")
        declared_text = json.dumps(declared) if isinstance(declared, str) else "(no description)"
        report.finding(
            ADVISORY,
            "manifest.description_listing",
            "plugin.json",
            f"declared description: {declared_text}; discovered skills: {', '.join(loaded) or '(none)'}. "
            "Read them side by side; this audit makes no judgement about whether they correspond.",
        )
    run_cao_validate(root, report)
    return report


def audit_skills_dir(directory: Path) -> Report:
    report = Report()
    report.info(f"cao-agent-plugin-audit: skills directory {directory}")
    if (directory / "plugin.json").exists():
        report.info(
            "ignored: plugin.json at the top of the directory (--skills-dir reads no manifest)"
        )
    for child in sorted(directory.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        if not (child / "SKILL.md").is_file():
            report.info(f"ignored: {child.name}/ (no SKILL.md)")
            continue
        check_skill(child, report)
    return report


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="validate_agent_plugin.py",
        description=(
            "Audit an Agent Plugins 1.0.0 package root (a directory holding plugin.json), "
            "or with --skills-dir a directory of Agent Skills, against the bundled schema "
            "bytes and the five Agent Skills rules."
        ),
        epilog=(
            "--skills-dir: each immediate child holding a SKILL.md is one skill. A plugin.json "
            "at the top of the directory is ignored, and so is any child without a SKILL.md. "
            "Exit status: 0 no finding above advisory, 1 at least one finding, any other "
            "status the audit itself failed. Never reads stdin."
        ),
    )
    parser.add_argument(
        "package_root",
        nargs="?",
        type=Path,
        help="Agent Plugin root, the directory holding plugin.json",
    )
    parser.add_argument(
        "--skills-dir", type=Path, help="audit each immediate child holding a SKILL.md as one skill"
    )
    args = parser.parse_args(argv)
    if (args.package_root is None) == (args.skills_dir is None):
        parser.error("give exactly one of PACKAGE_ROOT or --skills-dir")
    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    target: Path = args.skills_dir if args.skills_dir is not None else args.package_root
    try:
        if not target.is_dir():
            raise AuditError(f"not a directory: {target}")
        report = audit_skills_dir(target) if args.skills_dir is not None else audit_package(target)
    except AuditError as exc:
        print(f"audit failed: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # a crash is "the audit failed", never "no findings"
        print(f"audit failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    for line in report.lines:
        print(line)
    print(report.summary())
    return report.exit_status()


if __name__ == "__main__":
    sys.exit(main())
