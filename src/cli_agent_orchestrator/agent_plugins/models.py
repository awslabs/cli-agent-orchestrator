"""Data models for Agent Plugins 1.0.0 support.

Every model here is frozen. A validation report, an install record, and a
finding are all *values* that get rendered by the CLI, the HTTP API, the web
panel, and CI — none of them is a mutable handle onto live state, and freezing
them keeps that true.

Two modelling decisions are load-bearing and are called out where they are
made below:

* ``PluginValidationReport.loadable`` is a **derived property**, not a field, so
  it cannot disagree with the findings it summarizes (correctness property P1).
* ``extensions`` is **deliberately not modelled**. CAO implements no reverse-domain
  extension namespace in Increment 1, and §8.1 requires an unimplemented
  namespace be ignored *without validating its contents*. Modelling the member
  would invite validating it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Literal, Mapping, Optional, Tuple


class Severity(str, Enum):
    """Severity of a single :class:`Finding`.

    ``str`` mixin rather than :class:`enum.StrEnum` because ``requires-python``
    is ``>=3.10`` and ``StrEnum`` only landed in 3.11. The mixin gives the same
    JSON-serializable behaviour on every supported interpreter.
    """

    FATAL = "fatal"
    """The plugin is rejected outright (§11.3 rule 2). No component loads."""

    SKIPPED = "skipped"
    """One component type, one skill, or one MCP server entry was dropped."""

    WARNING = "warning"
    """Reported, but nothing was dropped."""

    INFO = "info"
    """Informational only."""


@dataclass(frozen=True)
class Finding:
    """A structured, severity-tagged report entry.

    ``spec_ref`` is not decoration. Every finding cites the specification clause
    it enforces, which is what makes the implementation auditable against the
    specification during review and is what the conformance corpus asserts
    against.
    """

    severity: Severity
    code: str
    """Stable, machine-readable identifier, e.g. ``"manifest.unknown_field"``."""

    spec_ref: str
    """The specification clause this finding enforces, e.g. ``"§5.2"``."""

    message: str
    path: Optional[str] = None
    """Plugin-root-relative path this finding is about, when it has one."""

    def to_dict(self) -> Dict[str, Any]:
        """Render as a JSON-serializable dict (CLI ``--json``, API, records)."""
        return {
            "severity": self.severity.value,
            "code": self.code,
            "spec_ref": self.spec_ref,
            "message": self.message,
            "path": self.path,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Finding":
        """Rebuild a finding from its persisted form."""
        return cls(
            severity=Severity(data["severity"]),
            code=str(data["code"]),
            spec_ref=str(data["spec_ref"]),
            message=str(data["message"]),
            path=data.get("path"),
        )


@dataclass(frozen=True)
class Author:
    """Manifest ``author`` object. Closed to name/email/url per §5.4."""

    name: Optional[str] = None
    email: Optional[str] = None
    url: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "email": self.email, "url": self.url}


@dataclass(frozen=True)
class PluginManifest:
    """Validated view of ``plugin.json``. Closed per §5.2."""

    schema_id: str
    """The declared ``$schema`` value. Required (§5.3)."""

    name: str
    """Required. Constrained by §5.5 (1–64 chars, ``[a-z0-9.-]``, no ``--``/``..``)."""

    version: Optional[str] = None
    description: Optional[str] = None
    author: Optional[Author] = None
    homepage: Optional[str] = None
    repository: Optional[str] = None
    license: Optional[str] = None
    keywords: Tuple[str, ...] = ()
    # `extensions` is intentionally NOT modelled — see the module docstring.


@dataclass(frozen=True)
class DiscoveredSkill:
    """A skill directory found under a plugin's ``skills/`` and validated."""

    name: str
    directory: Path
    """Absolute, and proven contained in the plugin root by ``containment.py``."""

    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "directory": str(self.directory),
            "description": self.description,
        }


@dataclass(frozen=True)
class MappedServer:
    """One ``mcp.json`` server entry mapped into CAO's internal MCP shape.

    Increment 2 only. It is modelled here because
    :class:`PluginValidationReport` carries the field in both increments (empty
    in Increment 1) — this is a data shape, not a code path: nothing in
    Increment 1 constructs one, and no placeholder expansion happens here.
    """

    name: str
    """The ``mcpServers`` key, which is also CAO's server name."""

    config: Dict[str, Any] = field(default_factory=dict)
    """CAO-internal ``mcpServers`` entry (Claude/Q CLI shape), fully expanded."""

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "config": dict(self.config)}


@dataclass(frozen=True)
class PluginValidationReport:
    """The total validator's answer for any candidate directory.

    Returned for *every* input — an arbitrary directory, a directory whose
    ``plugin.json`` is random bytes, a symlink loop — and never raised. The CLI,
    the HTTP API, the web panel, the installer, and CI all need the same
    structured answer, and three of those five must render partial success.
    """

    root: Path
    manifest: Optional[PluginManifest] = None
    skills: Tuple[DiscoveredSkill, ...] = ()
    mcp_present: bool = False
    mcp_servers: Tuple[MappedServer, ...] = ()
    findings: Tuple[Finding, ...] = ()

    @property
    def loadable(self) -> bool:
        """``True`` iff the report carries zero ``FATAL`` findings.

        A **property**, deliberately: Requirement 5.4 forbids ``loadable`` being
        settable independently of the findings it summarizes. Deriving it here
        makes correctness property P1's invariant
        (``loadable == (not any FATAL)``) hold by construction rather than by
        the discipline of every call site that builds a report.
        """
        return not any(finding.severity is Severity.FATAL for finding in self.findings)

    @property
    def skill_names(self) -> Tuple[str, ...]:
        """Names of every skill discovered as valid, in discovery order."""
        return tuple(skill.name for skill in self.skills)

    @property
    def blocked_by_cao(self) -> bool:
        """``True`` when the plugin failed because **CAO** is broken, not the plugin.

        There is exactly one such cause today: CAO could not load its own pinned
        schema, so it has nothing to validate against. Without this distinction a
        truncated CAO install reports every plugin as invalid, and an operator
        reasonably concludes the plugins are at fault — the one wrong conclusion,
        because it sends them to fix something that is fine.

        A ``cao.``-namespaced code rather than a severity: this is still FATAL for
        the plugin (nothing can be published), it just is not the plugin's doing.
        """
        return any(finding.code.startswith("cao.") for finding in self.findings)

    def findings_of(self, severity: Severity) -> Tuple[Finding, ...]:
        """Return only the findings at ``severity``."""
        return tuple(f for f in self.findings if f.severity is severity)

    def to_dict(self) -> Dict[str, Any]:
        """Render as a JSON-serializable dict for ``--json`` and the API."""
        manifest = self.manifest
        return {
            "root": str(self.root),
            "loadable": self.loadable,
            # Serialized rather than left for the client to re-derive: a web panel
            # or CI job showing "plugin invalid" when CAO's own schemas are missing
            # sends the operator to fix the wrong thing.
            "blocked_by_cao": self.blocked_by_cao,
            "name": manifest.name if manifest else None,
            "version": manifest.version if manifest else None,
            "description": manifest.description if manifest else None,
            "schema_id": manifest.schema_id if manifest else None,
            "skills": [skill.to_dict() for skill in self.skills],
            "mcp_present": self.mcp_present,
            "mcp_servers": [server.to_dict() for server in self.mcp_servers],
            "findings": [finding.to_dict() for finding in self.findings],
        }


@dataclass(frozen=True)
class PluginSource:
    """Where a plugin came from, as the operator specified it."""

    kind: Literal["path", "git"]
    location: str
    """A local directory path, or a git repository URL."""

    ref: Optional[str] = None
    """Git branch/tag to clone. ``git`` kind only."""

    subdir: Optional[str] = None
    """Repository-relative subdirectory holding the plugin root, if not the top."""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "location": self.location,
            "ref": self.ref,
            "subdir": self.subdir,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PluginSource":
        kind = data.get("kind", "path")
        return cls(
            kind="git" if kind == "git" else "path",
            location=str(data.get("location", "")),
            ref=data.get("ref"),
            subdir=data.get("subdir"),
        )


@dataclass(frozen=True)
class PluginRecord:
    """Install record persisted at ``AGENT_PLUGINS_DIR/.state/<name>.json``.

    This is the durable, CAO-owned view of an installed plugin. It never lives
    inside a ``PLUGIN_ROOT`` — CAO does not mutate package bytes — and it is the
    only state the deterministic collision rule reads (``name``, never
    ``installed_at``; see ``projection.py``).
    """

    name: str
    version: Optional[str]
    source: PluginSource
    resolved_ref: Optional[str]
    """Git commit SHA when the source was a repository, else ``None``."""

    installed_at: datetime
    schema_id: str
    skill_names: Tuple[str, ...]
    projected_skill_names: Tuple[str, ...] = ()
    """Subset of ``skill_names`` actually projected. The difference is always
    explained by a ``SKIPPED`` finding (the collision case)."""

    findings: Tuple[Finding, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "source": self.source.to_dict(),
            "resolved_ref": self.resolved_ref,
            "installed_at": self.installed_at.isoformat(),
            "schema_id": self.schema_id,
            "skill_names": list(self.skill_names),
            "projected_skill_names": list(self.projected_skill_names),
            "findings": [finding.to_dict() for finding in self.findings],
        }

    def to_json(self) -> str:
        """Serialize with stable key order so records diff cleanly."""
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PluginRecord":
        """Rebuild a record from disk, tolerating a missing/garbled timestamp."""
        raw_installed_at = data.get("installed_at")
        try:
            installed_at = datetime.fromisoformat(str(raw_installed_at))
        except (TypeError, ValueError):
            installed_at = datetime.fromtimestamp(0, tz=timezone.utc)
        return cls(
            name=str(data["name"]),
            version=data.get("version"),
            source=PluginSource.from_dict(data.get("source", {})),
            resolved_ref=data.get("resolved_ref"),
            installed_at=installed_at,
            schema_id=str(data.get("schema_id", "")),
            skill_names=tuple(data.get("skill_names", ())),
            projected_skill_names=tuple(data.get("projected_skill_names", ())),
            findings=tuple(Finding.from_dict(f) for f in data.get("findings", ())),
        )


@dataclass(frozen=True)
class AffectedSession:
    """A live session whose profile references a skill a removal would pull."""

    terminal_id: str
    session_name: str
    profile_name: str
    skill_names: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "terminal_id": self.terminal_id,
            "session_name": self.session_name,
            "profile_name": self.profile_name,
            "skill_names": list(self.skill_names),
        }


@dataclass(frozen=True)
class InstallOutcome:
    """Result of an install attempt, successful or not."""

    report: PluginValidationReport
    installed: bool = False
    record: Optional[PluginRecord] = None
    projection_findings: Tuple[Finding, ...] = ()
    dry_run: bool = False

    @property
    def findings(self) -> Tuple[Finding, ...]:
        """Validation findings followed by projection findings."""
        return tuple(self.report.findings) + tuple(self.projection_findings)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "installed": self.installed,
            "dry_run": self.dry_run,
            "report": self.report.to_dict(),
            "record": self.record.to_dict() if self.record else None,
            "projection_findings": [f.to_dict() for f in self.projection_findings],
        }


@dataclass(frozen=True)
class UninstallOutcome:
    """Result of an uninstall attempt."""

    name: str
    removed: bool
    purged_data: bool = False
    affected_sessions: Tuple[AffectedSession, ...] = ()
    projection_findings: Tuple[Finding, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "removed": self.removed,
            "purged_data": self.purged_data,
            "affected_sessions": [s.to_dict() for s in self.affected_sessions],
            "projection_findings": [f.to_dict() for f in self.projection_findings],
        }
