"""The total validator for candidate Agent Plugin directories.

:func:`validate_plugin` answers, for **any** directory, whether it is a loadable
Agent Plugin and what components it contains — and it never raises. The CLI, the
HTTP API, the web panel, the installer, and CI all need the same structured
answer, and three of those five must render *partial* success, so a total
function returning a report is the only shape that serves all five. This is
correctness property P1.

Schema handling is offline by construction (§5.2 forbids retrieving a schema
while loading a plugin): the schemas are read from bytes committed under
``schemas/agent_plugins/1.0.0/`` via ``importlib.resources``, and the
``referencing`` registry handed to ``jsonschema`` **refuses every retrieval**, so
an unexpected ``$ref`` raises instead of quietly becoming a network call.

MCP components
--------------
``mcp.json`` is detected here and mapped by delegation: :func:`_map_mcp` calls
``mcp_mapping.load_and_map``, so the report carries ``mcp_present`` and the mapped
servers. This module owns no expansion or schema logic for MCP itself — the
placeholder rules, the transport matrix, and the ``mcp.schema.json`` validation
all live in ``mcp_mapping``, and delivery into agent profiles lives in
``mcp_delivery``. Keeping all three separate is what lets this module stay a
*total function over a directory* with no notion of where the result goes.

(Increment 1 stopped at detection: it recorded ``mcp_present=True`` with an "MCP
not supported in this CAO version" finding and read nothing, which is what §11.3
rule 1 and §7.2.2 rule 4 prescribe for an unsupported component type. Increment 2
replaced that finding with real mapping. Whichever increment, **an unusable
``mcp.json`` never affects the plugin's skills** — §7.2.2.2, §10.1.)
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource

from cli_agent_orchestrator.agent_plugins.containment import canonical_root, resolve_within_root
from cli_agent_orchestrator.agent_plugins.models import (
    Author,
    DiscoveredSkill,
    Finding,
    PluginManifest,
    PluginValidationReport,
    Severity,
)

logger = logging.getLogger(__name__)

# --- Pinned schema location -------------------------------------------------
# Vendored under the existing ``schemas`` package, so the wheel ships them via
# the existing ``[tool.hatch.build.targets.wheel] packages`` entry with no
# packaging change. ``1.0.0`` is not a Python identifier, so the files are
# reached by traversal rather than as a subpackage.
_SCHEMA_PACKAGE = "cli_agent_orchestrator.schemas"
_SCHEMA_SUBPATH = ("agent_plugins", "1.0.0")

PLUGIN_SCHEMA_FILENAME = "plugin.schema.json"
MCP_SCHEMA_FILENAME = "mcp.schema.json"

# Fixed component locations (§6.1). CAO looks *only* here — never by search.
_SKILLS_DIRNAME = "skills"
_MCP_FILENAME = "mcp.json"
_MANIFEST_FILENAME = "plugin.json"


class SchemaUnavailableError(RuntimeError):
    """Raised when a vendored schema cannot be read or parsed.

    This is **CAO's** defect — a truncated wheel, a bad vendoring run, a file
    deleted from an installed package — not the plugin's. It is a distinct type
    rather than a finding because the difference is actionable in opposite
    directions: a plugin defect means fix or distrust the plugin, while this
    means the CAO installation is broken and *every* plugin will fail the same
    way.

    :func:`validate_plugin` still converts it into a ``FATAL`` finding rather
    than propagating — the validator is total — but the finding it produces
    carries :data:`CAO_SCHEMA_UNAVAILABLE` and says whose fault it is, so the
    distinction survives into what an operator actually reads. See
    :attr:`PluginValidationReport.blocked_by_cao`.
    """


#: Backwards-compatible alias. The old name described *where* the schema came
#: from; the new one describes what went wrong, which is what a caller needs.
PinnedSchemaError = SchemaUnavailableError

#: Finding code for "CAO could not load its own schema". Deliberately namespaced
#: ``cao.`` rather than ``schema.`` or ``manifest.``: every other code in a
#: report describes something about the *plugin*, and grouping this with them is
#: exactly the conflation that makes a packaging failure look like a bad plugin.
CAO_SCHEMA_UNAVAILABLE = "cao.schema_unavailable"


def _schema_unavailable_finding(exc: Exception, *, path: Optional[str] = None) -> Finding:
    """Build the FATAL finding for a CAO-side schema failure.

    One constructor, used by every call site, so the message cannot drift between
    the manifest path and the MCP path — an operator hitting this on two
    different plugins must not get two different explanations of one fault.
    """
    return Finding(
        severity=Severity.FATAL,
        code=CAO_SCHEMA_UNAVAILABLE,
        spec_ref="§5.2",
        message=(
            f"This is a problem with the CAO installation, not with the plugin: "
            f"CAO could not load its own pinned Agent Plugins schema ({exc}). "
            f"Every plugin will fail validation until it is fixed. Reinstall CAO, "
            f"or re-vendor the schemas with `make refresh-agent-plugins-schemas` "
            f"if you are working in a checkout."
        ),
        path=path,
    )


@lru_cache(maxsize=None)
def load_pinned_schema(filename: str) -> Dict[str, Any]:
    """Read one vendored schema from the package, with no network access.

    Cached: the bytes are immutable for the life of the process and validation
    runs once per plugin per install, list, and CI check.
    """
    if filename not in (PLUGIN_SCHEMA_FILENAME, MCP_SCHEMA_FILENAME):
        raise SchemaUnavailableError(f"Unknown pinned schema: {filename!r}")
    try:
        anchor = resources.files(_SCHEMA_PACKAGE)
        for part in _SCHEMA_SUBPATH:
            anchor = anchor.joinpath(part)
        raw = anchor.joinpath(filename).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
        raise SchemaUnavailableError(
            f"Vendored schema {filename!r} is not readable: {exc}"
        ) from exc
    try:
        parsed: Dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SchemaUnavailableError(
            f"Vendored schema {filename!r} is not valid JSON: {exc}"
        ) from exc
    return parsed


def _refuse_retrieval(uri: str) -> Resource:
    """Registry hook that refuses every remote schema retrieval (§5.2).

    Making this an explicit, loud failure rather than a silent fetch is the
    point: if a future schema revision introduces a ``$ref`` CAO has not
    vendored, validation must break visibly in CI instead of opening a network
    connection at plugin-load time.
    """
    # `referencing`'s exceptions and Registry are attrs-generated; mypy reads
    # their synthesized __init__ signatures wrongly and reports phantom
    # call-arg errors against unrelated builtins. Both kwargs are correct and
    # are exercised by test_schema_pin.py::TestRegistryRefusesRetrieval.
    raise NoSuchResource(ref=uri)  # type: ignore[call-arg]


@lru_cache(maxsize=None)
def _offline_validator(filename: str) -> Draft202012Validator:
    """Build a ``Draft202012Validator`` bound to an offline-only registry."""
    schema = load_pinned_schema(filename)
    registry: Registry = Registry(retrieve=_refuse_retrieval).with_resources(  # type: ignore[call-arg]
        [
            (
                str(load_pinned_schema(name).get("$id", name)),
                Resource.from_contents(load_pinned_schema(name)),
            )
            for name in (PLUGIN_SCHEMA_FILENAME, MCP_SCHEMA_FILENAME)
        ]
    )
    return Draft202012Validator(schema, registry=registry)


@lru_cache(maxsize=None)
def supported_schema_id(filename: str) -> str:
    """Return the canonical ``$id`` of a vendored schema.

    Read from the vendored bytes rather than hardcoded, so refreshing the pin to
    a new specification version cannot leave a stale identifier behind in code.
    """
    schema_id = load_pinned_schema(filename).get("$id")
    if not isinstance(schema_id, str) or not schema_id:
        raise SchemaUnavailableError(f"Vendored schema {filename!r} declares no $id")
    return schema_id


@lru_cache(maxsize=None)
def _known_manifest_fields() -> Tuple[str, ...]:
    """Top-level manifest fields the pinned schema recognizes.

    Derived from the schema's own ``properties`` so the "unknown top-level
    field" tolerance below stays tied to the pinned bytes instead of drifting
    against a hand-maintained list.
    """
    properties = load_pinned_schema(PLUGIN_SCHEMA_FILENAME).get("properties", {})
    return tuple(sorted(properties)) if isinstance(properties, dict) else ()


# --- Validation -------------------------------------------------------------


def validate_plugin(root: Path) -> PluginValidationReport:
    """Validate a candidate Agent Plugin directory. Never raises.

    Args:
        root: Any directory. It need not exist, be readable, or be a plugin.

    Returns:
        A :class:`PluginValidationReport`. ``report.loadable`` is ``True`` iff no
        finding has ``FATAL`` severity — derived, never set independently.
    """
    root_path = Path(root)
    try:
        return _validate_plugin_inner(root_path)
    except Exception as exc:  # pragma: no cover - the totality backstop
        # P1 says validation is total. Anything that escapes the per-step
        # handling below is a CAO bug, but it must still surface as a report:
        # every caller of this function renders findings, and three of them
        # cannot handle an exception without failing an operator's whole
        # command over one bad plugin.
        logger.exception("Unexpected error while validating agent plugin at %s", root_path)
        return PluginValidationReport(
            root=root_path,
            findings=(
                Finding(
                    severity=Severity.FATAL,
                    code="internal.error",
                    spec_ref="§11.3",
                    message=f"Internal error while validating this plugin: {exc}",
                ),
            ),
        )


def _validate_plugin_inner(root_path: Path) -> PluginValidationReport:
    findings: List[Finding] = []

    root_real = canonical_root(root_path)
    if root_real is None or not os.path.isdir(root_real):
        findings.append(
            Finding(
                severity=Severity.FATAL,
                code="plugin.root_invalid",
                spec_ref="§4.2",
                message=f"Not a readable directory: {root_path}",
            )
        )
        return PluginValidationReport(root=root_path, findings=tuple(findings))
    root_dir = Path(root_real)

    manifest_data, manifest_findings = _read_manifest(root_dir)
    findings.extend(manifest_findings)
    if manifest_data is None:
        # A fatal manifest violation rejects the plugin *before any component
        # loads* (§5.2, AC4): discover nothing.
        return PluginValidationReport(root=root_dir, findings=tuple(findings))

    sanitized, tolerance_findings = _apply_manifest_tolerances(manifest_data)
    findings.extend(tolerance_findings)

    findings.extend(_schema_findings(sanitized))
    if any(f.severity is Severity.FATAL for f in findings):
        return PluginValidationReport(root=root_dir, findings=tuple(findings))

    manifest = _build_manifest(sanitized)

    skills, skill_findings = _discover_skills(root_dir)
    findings.extend(skill_findings)

    mcp_present, mcp_servers, mcp_findings = _map_mcp(root_dir, manifest)
    findings.extend(mcp_findings)

    return PluginValidationReport(
        root=root_dir,
        manifest=manifest,
        skills=tuple(skills),
        mcp_present=mcp_present,
        mcp_servers=mcp_servers,
        findings=tuple(findings),
    )


def _read_manifest(root_dir: Path) -> Tuple[Optional[Dict[str, Any]], List[Finding]]:
    """Locate, read, and JSON-parse ``plugin.json``.

    Returns ``(None, findings)`` for every fatal outcome. The manifest lives at a
    fixed location (§6.1) and is looked for nowhere else.
    """
    findings: List[Finding] = []

    contained = resolve_within_root(root_dir, _MANIFEST_FILENAME)
    if contained is None:
        # §4.1's first ladder rung: plugin.json outside the root rejects the
        # whole plugin, not just a component.
        findings.append(
            Finding(
                severity=Severity.FATAL,
                code="manifest.escapes_root",
                spec_ref="§4.1",
                message="plugin.json resolves outside the plugin root",
                path=_MANIFEST_FILENAME,
            )
        )
        return None, findings

    if not contained.exists():
        findings.append(
            Finding(
                severity=Severity.FATAL,
                code="manifest.missing",
                spec_ref="§4.2",
                message="No plugin.json at the plugin root",
                path=_MANIFEST_FILENAME,
            )
        )
        return None, findings

    if not contained.is_file():
        findings.append(
            Finding(
                severity=Severity.FATAL,
                code="manifest.not_a_file",
                spec_ref="§4.2",
                message="plugin.json exists but is not a regular file",
                path=_MANIFEST_FILENAME,
            )
        )
        return None, findings

    try:
        raw = contained.read_bytes()
    except OSError as exc:
        findings.append(
            Finding(
                severity=Severity.FATAL,
                code="manifest.unreadable",
                spec_ref="§5.1",
                message=f"plugin.json could not be read: {exc}",
                path=_MANIFEST_FILENAME,
            )
        )
        return None, findings

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        findings.append(
            Finding(
                severity=Severity.FATAL,
                code="manifest.invalid_encoding",
                spec_ref="§5.1",
                message=f"plugin.json is not valid UTF-8: {exc}",
                path=_MANIFEST_FILENAME,
            )
        )
        return None, findings

    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        findings.append(
            Finding(
                severity=Severity.FATAL,
                code="manifest.invalid_json",
                spec_ref="§5.1",
                message=f"plugin.json is not valid JSON: {exc}",
                path=_MANIFEST_FILENAME,
            )
        )
        return None, findings

    if not isinstance(parsed, dict):
        findings.append(
            Finding(
                severity=Severity.FATAL,
                code="manifest.not_an_object",
                spec_ref="§5.2",
                message="plugin.json must be a JSON object",
                path=_MANIFEST_FILENAME,
            )
        )
        return None, findings

    schema_findings = _check_schema_declaration(parsed)
    if schema_findings:
        return None, findings + schema_findings

    return parsed, findings


def _check_schema_declaration(manifest: Dict[str, Any]) -> List[Finding]:
    """Select validation rules from ``$schema`` (§5.2, §5.3).

    A recognized canonical identifier selects the locally pinned rules; an
    unrecognized one rejects the plugin and reports the unsupported version. No
    network retrieval happens in either branch — an unknown ``$schema`` is a
    rejection, never a fetch.
    """
    declared = manifest.get("$schema")
    if not isinstance(declared, str) or not declared:
        return [
            Finding(
                severity=Severity.FATAL,
                code="manifest.schema_missing",
                spec_ref="§5.3",
                message="plugin.json must declare a '$schema' string",
                path=_MANIFEST_FILENAME,
            )
        ]

    try:
        supported = supported_schema_id(PLUGIN_SCHEMA_FILENAME)
    except SchemaUnavailableError as exc:
        return [_schema_unavailable_finding(exc)]

    if declared != supported:
        return [
            Finding(
                severity=Severity.FATAL,
                code="manifest.schema_unsupported",
                spec_ref="§5.2",
                message=(
                    f"Unsupported plugin manifest schema {declared!r}. "
                    f"This CAO version pins {supported!r}."
                ),
                path=_MANIFEST_FILENAME,
            )
        ]
    return []


def _apply_manifest_tolerances(
    manifest: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[Finding]]:
    """Strip the two — and only two — non-fatal manifest deviations.

    The manifest is **closed** (§5.2): the pinned schema sets
    ``additionalProperties: false``, so anything left in the document after this
    pass and rejected by the schema is fatal. The specification carves out
    exactly two tolerances, and they are applied here rather than by loosening
    the schema, so the closed-ness stays visible in the vendored bytes:

    1. **An unrecognized top-level field** (§5.2) — reported, ignored, plugin
       still loads.
    2. **A non-object ``extensions``** (§8.1) — reported, ignored, plugin still
       loads.

    A well-formed ``extensions`` object is dropped **without a finding and
    without validating its contents**: CAO implements no reverse-domain
    namespace (M2 is deliberately unresolved), and §8.1 requires an
    unimplemented namespace be ignored *without* validating what is inside it.
    Letting the schema see it would validate it.
    """
    findings: List[Finding] = []
    sanitized = dict(manifest)

    if "extensions" in sanitized:
        extensions = sanitized.pop("extensions")
        if not isinstance(extensions, dict):
            findings.append(
                Finding(
                    severity=Severity.WARNING,
                    code="manifest.extensions_not_object",
                    spec_ref="§8.1",
                    message=(
                        "'extensions' must be an object; the member was ignored and "
                        "the plugin still loaded"
                    ),
                    path=_MANIFEST_FILENAME,
                )
            )
        # An object-valued `extensions` names only namespaces CAO does not
        # implement, so it is ignored silently — no finding, no validation.

    known = set(_known_manifest_fields())
    for field_name in sorted(set(sanitized) - known):
        sanitized.pop(field_name)
        findings.append(
            Finding(
                severity=Severity.WARNING,
                code="manifest.unknown_field",
                spec_ref="§5.2",
                message=(
                    f"Unrecognized top-level manifest field {field_name!r} was ignored; "
                    f"the plugin still loaded"
                ),
                path=_MANIFEST_FILENAME,
            )
        )

    return sanitized, findings


def _schema_findings(sanitized: Dict[str, Any]) -> List[Finding]:
    """Validate the sanitized manifest against the pinned, offline schema."""
    try:
        validator = _offline_validator(PLUGIN_SCHEMA_FILENAME)
    except SchemaUnavailableError as exc:
        return [_schema_unavailable_finding(exc)]

    errors = sorted(
        validator.iter_errors(sanitized),
        key=lambda error: (list(map(str, error.absolute_path)), error.message),
    )

    findings: List[Finding] = []
    for error in errors:
        location = ".".join(str(part) for part in error.absolute_path)
        is_name = bool(error.absolute_path) and error.absolute_path[0] == "name"
        findings.append(
            Finding(
                severity=Severity.FATAL,
                code="manifest.name_invalid" if is_name else "manifest.invalid",
                # §5.5 constrains the name specifically; everything else is the
                # closed-manifest rule in §5.2.
                spec_ref="§5.5" if is_name else "§5.2",
                message=(f"{location}: {error.message}" if location else error.message),
                path=_MANIFEST_FILENAME,
            )
        )
    return findings


def _build_manifest(sanitized: Dict[str, Any]) -> PluginManifest:
    """Build the validated manifest view. Only called after schema success."""
    raw_author = sanitized.get("author")
    author = (
        Author(
            name=raw_author.get("name"),
            email=raw_author.get("email"),
            url=raw_author.get("url"),
        )
        if isinstance(raw_author, dict)
        else None
    )
    keywords = sanitized.get("keywords")
    return PluginManifest(
        schema_id=str(sanitized["$schema"]),
        name=str(sanitized["name"]),
        version=sanitized.get("version"),
        description=sanitized.get("description"),
        author=author,
        homepage=sanitized.get("homepage"),
        repository=sanitized.get("repository"),
        license=sanitized.get("license"),
        keywords=tuple(keywords) if isinstance(keywords, list) else (),
    )


def _discover_skills(root_dir: Path) -> Tuple[List[DiscoveredSkill], List[Finding]]:
    """Discover skills as immediate, non-recursive children of ``skills/``.

    Each skill is validated **independently**: one broken skill is skipped with a
    report and its siblings still load (§7.1, property P7). Children are
    iterated in sorted order so both the discovered set and the finding order
    are independent of ``os.scandir`` ordering (Requirement 12.2).
    """
    findings: List[Finding] = []
    discovered: List[DiscoveredSkill] = []

    contained = resolve_within_root(root_dir, _SKILLS_DIRNAME)
    if contained is None:
        findings.append(
            Finding(
                severity=Severity.SKIPPED,
                code="skills.escapes_root",
                spec_ref="§4.1",
                message="'skills' resolves outside the plugin root; no skills were loaded",
                path=_SKILLS_DIRNAME,
            )
        )
        return discovered, findings

    if not contained.exists():
        # §6.2: a missing fixed component location is not an error.
        return discovered, findings

    if not contained.is_dir():
        findings.append(
            Finding(
                severity=Severity.SKIPPED,
                code="skills.not_a_directory",
                spec_ref="§6.2",
                message=(
                    "'skills' exists but is not a directory; the skills component "
                    "type was skipped"
                ),
                path=_SKILLS_DIRNAME,
            )
        )
        return discovered, findings

    try:
        children = sorted(contained.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        findings.append(
            Finding(
                severity=Severity.SKIPPED,
                code="skills.unreadable",
                spec_ref="§6.2",
                message=f"'skills' could not be listed: {exc}",
                path=_SKILLS_DIRNAME,
            )
        )
        return discovered, findings

    for child in children:
        if child.name.startswith("."):
            continue  # dot-entries are never skills
        rel = f"{_SKILLS_DIRNAME}/{child.name}"

        if resolve_within_root(root_dir, child) is None:
            findings.append(
                Finding(
                    severity=Severity.SKIPPED,
                    code="skill.escapes_root",
                    spec_ref="§4.1",
                    message=f"Skill '{child.name}' resolves outside the plugin root; skipped",
                    path=rel,
                )
            )
            continue

        if not child.is_dir():
            continue  # a stray file under skills/ is not a skill candidate

        skill_md = child / "SKILL.md"
        if resolve_within_root(root_dir, skill_md) is None:
            findings.append(
                Finding(
                    severity=Severity.SKIPPED,
                    code="skill.escapes_root",
                    spec_ref="§4.1",
                    message=(
                        f"SKILL.md for skill '{child.name}' resolves outside the "
                        f"plugin root; skipped"
                    ),
                    path=f"{rel}/SKILL.md",
                )
            )
            continue

        if not skill_md.is_file():
            findings.append(
                Finding(
                    severity=Severity.SKIPPED,
                    code="skill.missing_skill_md",
                    spec_ref="§7.1",
                    message=f"Skill directory '{child.name}' has no SKILL.md; skipped",
                    path=rel,
                )
            )
            continue

        try:
            # CAO's own loader is the authority here on purpose: it enforces the
            # same folder-name == frontmatter-name equality the Agent Skills
            # specification requires, and projection depends on that equality
            # holding (the projected link is named with the unprefixed skill
            # name). Validating with anything looser would admit skills that
            # then fail at delivery time.
            from cli_agent_orchestrator.utils.skills import validate_skill_folder

            metadata = validate_skill_folder(child)
        except Exception as exc:
            findings.append(
                Finding(
                    severity=Severity.SKIPPED,
                    code="skill.invalid",
                    spec_ref="§7.1",
                    message=f"Skill '{child.name}' is invalid and was skipped: {exc}",
                    path=rel,
                )
            )
            continue

        discovered.append(
            DiscoveredSkill(
                name=metadata.name,
                directory=child,
                description=metadata.description or "",
            )
        )

    return discovered, findings


def _map_mcp(root_dir: Path, manifest: PluginManifest) -> Tuple[bool, tuple, List[Finding]]:
    """Discover and map ``mcp.json`` at its fixed location (§6.1).

    Increment 2. Increment 1 recorded ``mcp_present`` and reported the component
    type as unsupported; now the file is validated against the pinned
    ``mcp.schema.json`` and mapped into CAO's internal MCP shape.

    Whatever happens here, **the plugin's skills are unaffected** (§7.2.2.2,
    §10.1): an unusable ``mcp.json`` disables MCP for the plugin and nothing
    else, and one bad server entry invalidates only that entry.

    Imported inside the function because ``mcp_mapping`` imports the pinned
    schema loader from this module; a module-level import would be circular.
    """
    from cli_agent_orchestrator.agent_plugins.mcp_mapping import load_and_map

    data_dir = _plugin_data_dir_for(manifest.name)
    result = load_and_map(root_dir, data_dir, plugin_schema_id=manifest.schema_id)
    return result.present, result.servers, list(result.findings)


def _plugin_data_dir_for(name: str) -> Path:
    """Where this plugin's ``PLUGIN_DATA`` will live once installed.

    Resolved even when validating an arbitrary directory that will never be
    installed, so a ``${PLUGIN_DATA}`` placeholder expands to the same path
    ``cao plugin validate`` predicts and the eventual install actually uses.
    """
    from cli_agent_orchestrator.constants import AGENT_PLUGIN_DATA_DIR

    return AGENT_PLUGIN_DATA_DIR / name
