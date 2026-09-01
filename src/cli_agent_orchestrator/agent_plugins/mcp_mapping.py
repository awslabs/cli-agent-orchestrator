"""Map a plugin's ``mcp.json`` into CAO's internal MCP shape — **Increment 2 only**.

This is the **only** module in the feature permitted to expand
``${PLUGIN_ROOT}``/``${PLUGIN_DATA}``, validate against ``mcp.schema.json``, or
concern itself with launching a plugin subprocess.

CAO's lingua franca for MCP is the agent-profile ``mcpServers`` dict
(Claude/Q CLI format), from which ``services/install_service.py`` and
``utils/opencode_config.py::translate_mcp_server_config`` already derive every
provider's native form. The mapping therefore targets *that* shape rather than
each provider — every existing per-provider translation then applies unchanged.

The conformance points that are easy to get wrong, and are pinned here:

* **``command`` is one token** (§7.2.1). Never shell-split, never
  placeholder-expanded.
* **Expansion is single-pass and non-recursive** (§9.2). Only ``${PLUGIN_ROOT}``
  and ``${PLUGIN_DATA}``, only in ``args`` elements, ``env`` *values*, and
  ``cwd``. Not ``env`` keys, not ``command``, not ``url``, not header names or
  values. Text introduced by a replacement is **not** rescanned, and an
  unrecognized ``${...}`` stays literal. This is correctness property P9.
* **CAO's own interpolation must not touch a mapped entry.**
  ``install_service`` resolves ``${VAR}`` over profile content, which would
  capture a plugin's literal ``${FOO}`` and violate §9.2's "clients MUST NOT
  perform any other placeholder or environment-variable expansion". Mapped
  entries are marked pre-expanded and skipped by that pass.
* **``env`` must not declare ``PLUGIN_ROOT``/``PLUGIN_DATA``** (§9.2) — such an
  entry is invalidated rather than allowed to override CAO-supplied values.
* **Transport mismatch is a skip with a report**, never a failover: §7.2.1
  explicitly leaves fallback outside the format.
* **Credentials are warned about, not rejected.** §7.2.1/§9.2 forbid them in
  ``env`` and ``headers``, but do not require clients to reject them, and
  blocking an install on a heuristic would be worse than reporting it.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from cli_agent_orchestrator.agent_plugins.containment import resolve_within_root
from cli_agent_orchestrator.agent_plugins.models import Finding, MappedServer, Severity
from cli_agent_orchestrator.agent_plugins.validation import (
    MCP_SCHEMA_FILENAME,
    SchemaUnavailableError,
    _offline_validator,
    _schema_unavailable_finding,
    supported_schema_id,
)

logger = logging.getLogger(__name__)

MCP_FILENAME = "mcp.json"

#: Marker written onto a mapped entry so CAO's profile-level ``${VAR}``
#: interpolation skips it. Prefixed ``x-cao-`` because it is CAO-internal
#: bookkeeping, not part of any portable format, and it is stripped before a
#: provider config is written.
PRE_EXPANDED_KEY = "x-cao-pre-expanded"

#: The two placeholders §9.2 defines. Nothing else is ever expanded.
_PLUGIN_ROOT_TOKEN = "${PLUGIN_ROOT}"
_PLUGIN_DATA_TOKEN = "${PLUGIN_DATA}"

_PLACEHOLDER_RE = re.compile(r"\$\{(PLUGIN_ROOT|PLUGIN_DATA)\}")

#: Transports each CAO provider can actually carry.
#:
#: Grounded in each provider's serializer, not assumed. Audited entry by entry:
#:
#: * ``opencode_cli`` — ``opencode_config.translate_mcp_server_config`` flattens an
#:   entry into ``{"type": "local", "command": [...]}``, so a url-based entry would
#:   reach OpenCode as an empty command.
#: * ``codex`` — the serializer emits ``mcp_servers.<name>.command`` / ``.args`` /
#:   ``.env`` / ``.env_vars`` TOML overrides and has no ``url`` branch at all. A
#:   ``streamable-http`` entry produces a server name carrying only ``env_vars``,
#:   which cannot start.
#: * ``antigravity_cli`` — ``_register_mcp_servers`` always writes
#:   ``{"command": ..., "args": ..., "env": ...}`` into ``mcp_config.json``, so a
#:   url-based entry lands with an empty command.
#: * ``claude_code``, ``kimi_cli``, ``cursor_cli`` — these pass the whole entry
#:   through (``resolve_mcp_server_config`` returns a command-less url entry
#:   unmodified), so a url survives into the provider's own config and the
#:   transport decision is the provider's to make, not CAO's to pre-empt.
#:
#: Listing a provider here makes an undeliverable transport a *reported skip*
#: rather than a silent claim of delivery.
#:
#: The default is **stdio-only**, deliberately conservative: a provider added
#: later and not entered here reports HTTP servers as skipped, which is a visible
#: finding, instead of writing config its serializer cannot express. Every
#: provider CAO ships is entered explicitly, so the default is a safety net for
#: future code rather than a description of anything current.
_STDIO_ONLY = frozenset({"stdio"})
_ALL_TRANSPORTS = frozenset({"stdio", "streamable-http", "sse"})
PROVIDER_TRANSPORTS: Dict[str, frozenset] = {
    # Pass the whole entry through, so a url survives into their own config.
    "kiro_cli": _ALL_TRANSPORTS,
    "claude_code": _ALL_TRANSPORTS,
    "kimi_cli": _ALL_TRANSPORTS,
    "cursor_cli": _ALL_TRANSPORTS,
    "copilot_cli": _ALL_TRANSPORTS,
    # Serializers that can only express a local command.
    "opencode_cli": _STDIO_ONLY,
    "codex": _STDIO_ONLY,
    "antigravity_cli": _STDIO_ONLY,
    # No MCP delivery path at all; listed so the table is exhaustive.
    "hermes": _STDIO_ONLY,
    "mock_cli": _STDIO_ONLY,
}
DEFAULT_TRANSPORTS = _STDIO_ONLY

#: Substrings in an ``env`` key or header name that suggest a credential.
_CREDENTIAL_NAME_HINTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "apikey",
    "api_key",
    "credential",
    "auth",
    "private_key",
    "access_key",
    "session_key",
)

#: Value shapes that look like a credential regardless of the key's name.
_CREDENTIAL_VALUE_RE = re.compile(
    r"""(
        ^Bearer\s+\S+                 # Authorization: Bearer <token>
      | ^(?:gh[pousr]|github_pat)_\w+ # GitHub tokens
      | ^sk-[A-Za-z0-9_-]{16,}        # OpenAI-style keys
      | ^xox[baprs]-[A-Za-z0-9-]+     # Slack tokens
      | ^AKIA[0-9A-Z]{16}$            # AWS access key id
      | ^[A-Za-z0-9+/]{40,}={0,2}$    # long opaque base64 blob
    )""",
    re.VERBOSE,
)


@dataclass(frozen=True)
class MappedMcpResult:
    """Outcome of mapping one plugin's ``mcp.json``."""

    servers: Tuple[MappedServer, ...] = ()
    findings: Tuple[Finding, ...] = ()
    present: bool = False
    """Whether an ``mcp.json`` existed at all."""

    valid: bool = True
    """False when the file itself is unusable, disabling MCP for this plugin.

    A plugin's **skills are unaffected** either way (§7.2.2.2, §10.1).
    """


# --- Expansion --------------------------------------------------------------


def expand_placeholders(value: str, root: str, data_dir: str) -> str:
    """Expand ``${PLUGIN_ROOT}``/``${PLUGIN_DATA}`` in one string. Single-pass.

    ``re.sub`` with a replacement *function* is what makes this non-recursive:
    the substituted text is written straight into the output and never re-scanned,
    so a ``PLUGIN_DATA`` path that itself contains the literal characters
    ``${PLUGIN_ROOT}`` stays literal. Any other ``${...}`` never matches the
    pattern and is therefore left exactly as written (§9.2).
    """
    if not isinstance(value, str):
        return value

    def _replace(match: "re.Match[str]") -> str:
        return root if match.group(1) == "PLUGIN_ROOT" else data_dir

    return _PLACEHOLDER_RE.sub(_replace, value)


def _looks_credential_shaped(name: str, value: str) -> bool:
    """Heuristic: does this key/value pair look like a secret?"""
    lowered = name.lower().replace("-", "_")
    if any(hint in lowered for hint in _CREDENTIAL_NAME_HINTS):
        return True
    return bool(isinstance(value, str) and value and _CREDENTIAL_VALUE_RE.match(value.strip()))


# --- Mapping ----------------------------------------------------------------


def map_mcp_config(
    root: Path,
    data_dir: Path,
    cfg: Mapping[str, Any],
    *,
    provider: Optional[str] = None,
    plugin_schema_id: Optional[str] = None,
) -> MappedMcpResult:
    """Map a parsed ``mcp.json`` document into CAO ``mcpServers`` entries.

    Args:
        root: The plugin's ``PLUGIN_ROOT`` (absolute).
        data_dir: The plugin's ``PLUGIN_DATA`` (absolute).
        cfg: The parsed ``mcp.json`` document.
        provider: Target provider, for the transport matrix. ``None`` maps for
            CAO's internal shape without narrowing transports.
        plugin_schema_id: The ``$schema`` declared in the same package's
            ``plugin.json``. When given, a mismatch invalidates the MCP
            configuration (§7.2.2.2) — the two documents must target the same
            specification version.

    Never raises.
    """
    findings: List[Finding] = []
    root_str = str(root)
    data_str = str(data_dir)

    if not isinstance(cfg, Mapping):
        return MappedMcpResult(
            findings=(
                Finding(
                    severity=Severity.SKIPPED,
                    code="mcp.not_an_object",
                    spec_ref="§7.2.2",
                    message="mcp.json must be a JSON object; MCP disabled for this plugin",
                    path=MCP_FILENAME,
                ),
            ),
            present=True,
            valid=False,
        )

    declared = cfg.get("$schema")
    try:
        expected = supported_schema_id(MCP_SCHEMA_FILENAME)
    except SchemaUnavailableError as exc:  # pragma: no cover - packaging defect
        return MappedMcpResult(
            findings=(_schema_unavailable_finding(exc, path=MCP_FILENAME),),
            present=True,
            valid=False,
        )

    if declared != expected:
        return MappedMcpResult(
            findings=(
                Finding(
                    severity=Severity.SKIPPED,
                    code="mcp.schema_unsupported",
                    spec_ref="§7.2.2",
                    message=(
                        f"mcp.json declares $schema {declared!r}; this CAO version pins "
                        f"{expected!r}. MCP disabled for this plugin; its skills are unaffected."
                    ),
                    path=MCP_FILENAME,
                ),
            ),
            present=True,
            valid=False,
        )

    if plugin_schema_id and _version_of(declared) != _version_of(plugin_schema_id):
        return MappedMcpResult(
            findings=(
                Finding(
                    severity=Severity.SKIPPED,
                    code="mcp.schema_version_mismatch",
                    spec_ref="§7.2.2.2",
                    message=(
                        "mcp.json and plugin.json target different specification versions; "
                        "MCP disabled for this plugin. Its skills are unaffected."
                    ),
                    path=MCP_FILENAME,
                ),
            ),
            present=True,
            valid=False,
        )

    schema_errors = _schema_errors(cfg)
    if schema_errors:
        return MappedMcpResult(findings=tuple(schema_errors), present=True, valid=False)

    servers: List[MappedServer] = []
    raw_servers = cfg.get("mcpServers") or {}
    allowed = PROVIDER_TRANSPORTS.get(provider, DEFAULT_TRANSPORTS) if provider else _ALL_TRANSPORTS

    for name in sorted(raw_servers):
        entry = raw_servers[name]
        mapped, entry_findings = _map_entry(
            name, entry, root_str, data_str, root, data_dir, allowed
        )
        findings.extend(entry_findings)
        if mapped is not None:
            servers.append(mapped)

    return MappedMcpResult(
        servers=tuple(servers), findings=tuple(findings), present=True, valid=True
    )


def _version_of(schema_id: Optional[str]) -> str:
    """Extract the ``1.0.0`` segment from a canonical schema URL."""
    match = re.search(r"/schemas/([^/]+)/", schema_id or "")
    return match.group(1) if match else ""


def _schema_errors(cfg: Mapping[str, Any]) -> List[Finding]:
    """Validate the document against the pinned ``mcp.schema.json``."""
    try:
        validator = _offline_validator(MCP_SCHEMA_FILENAME)
    except SchemaUnavailableError as exc:  # pragma: no cover - packaging defect
        return [_schema_unavailable_finding(exc, path=MCP_FILENAME)]

    errors = sorted(
        validator.iter_errors(dict(cfg)),
        key=lambda error: (list(map(str, error.absolute_path)), error.message),
    )
    return [
        Finding(
            severity=Severity.SKIPPED,
            code="mcp.invalid",
            spec_ref="§7.2.2",
            message=(
                f"{'.'.join(str(p) for p in error.absolute_path)}: {error.message}"
                if error.absolute_path
                else error.message
            ),
            path=MCP_FILENAME,
        )
        for error in errors
    ]


def _map_entry(
    name: str,
    entry: Any,
    root_str: str,
    data_str: str,
    root: Path,
    data_dir: Path,
    allowed_transports: frozenset,
) -> Tuple[Optional[MappedServer], List[Finding]]:
    """Map one ``mcpServers`` entry. Failure invalidates only this entry."""
    findings: List[Finding] = []
    where = f"{MCP_FILENAME}#{name}"

    if not isinstance(entry, Mapping):
        return None, [
            Finding(
                severity=Severity.SKIPPED,
                code="mcp.server_invalid",
                spec_ref="§7.2.2",
                message=f"Server {name!r} is not an object; entry skipped",
                path=where,
            )
        ]

    transport = entry.get("type")
    if transport not in allowed_transports:
        # §7.2.2 rule 4: skip with a report, never fail over to a different
        # transport — §7.2.1 leaves fallback outside the format entirely.
        return None, [
            Finding(
                severity=Severity.SKIPPED,
                code="mcp.transport_unsupported",
                spec_ref="§7.2.2",
                message=(
                    f"Server {name!r} declares transport {transport!r}, which the target "
                    f"provider does not support; entry skipped (supported: "
                    f"{', '.join(sorted(allowed_transports))})"
                ),
                path=where,
            )
        ]

    config: Dict[str, Any] = {"type": transport}

    if transport == "stdio":
        mapped_stdio, stdio_findings = _map_stdio(
            name, entry, root_str, data_str, root, data_dir, where
        )
        findings.extend(stdio_findings)
        if mapped_stdio is None:
            return None, findings
        config.update(mapped_stdio)
    else:
        # `url` is never placeholder-expanded (§9.2), and neither are header
        # names or values.
        config["url"] = entry.get("url")
        headers = entry.get("headers")
        if isinstance(headers, Mapping):
            config["headers"] = dict(headers)
            findings.extend(_credential_findings(headers, name, "headers", where))

    config[PRE_EXPANDED_KEY] = True
    return MappedServer(name=name, config=config), findings


def _map_stdio(
    name: str,
    entry: Mapping[str, Any],
    root_str: str,
    data_str: str,
    root: Path,
    data_dir: Path,
    where: str,
) -> Tuple[Optional[Dict[str, Any]], List[Finding]]:
    """Map a stdio entry's ``command``/``args``/``env``/``cwd``."""
    findings: List[Finding] = []
    config: Dict[str, Any] = {}

    # `command` is a single token and is NEVER expanded or split (§7.2.1).
    command = entry.get("command")
    if not isinstance(command, str) or not command:
        return None, [
            Finding(
                severity=Severity.SKIPPED,
                code="mcp.command_invalid",
                spec_ref="§7.2.1",
                message=f"Server {name!r} has no usable `command`; entry skipped",
                path=where,
            )
        ]

    # A `./`-rooted command is plugin-relative and must stay inside the root.
    if command.startswith("./") or command.startswith(".\\"):
        resolved = resolve_within_root(root, command)
        if resolved is None:
            return None, [
                Finding(
                    severity=Severity.SKIPPED,
                    code="mcp.command_escapes_root",
                    spec_ref="§4.1",
                    message=(
                        f"Server {name!r} command {command!r} resolves outside the plugin "
                        f"root; entry skipped"
                    ),
                    path=where,
                )
            ]
        config["command"] = str(resolved)
    else:
        config["command"] = command

    args = entry.get("args")
    if isinstance(args, list):
        config["args"] = [expand_placeholders(a, root_str, data_str) for a in args]

    env = entry.get("env")
    if isinstance(env, Mapping):
        # §9.2: CAO supplies PLUGIN_ROOT/PLUGIN_DATA itself, after applying the
        # configured env. A plugin declaring either key would override
        # CAO-supplied values, so the entry is invalidated rather than merged.
        forbidden = sorted({"PLUGIN_ROOT", "PLUGIN_DATA"} & set(env))
        if forbidden:
            return None, [
                Finding(
                    severity=Severity.SKIPPED,
                    code="mcp.env_reserved_key",
                    spec_ref="§9.2",
                    message=(
                        f"Server {name!r} declares reserved env key(s) "
                        f"{', '.join(forbidden)}; entry skipped. CAO supplies both itself."
                    ),
                    path=where,
                )
            ]
        # Keys are never expanded; only values are.
        config["env"] = {
            key: expand_placeholders(value, root_str, data_str) for key, value in env.items()
        }
        findings.extend(_credential_findings(env, name, "env", where))

    cwd = entry.get("cwd")
    if cwd is None:
        # §9.1: omitted `cwd` defaults to the plugin root.
        config["cwd"] = root_str
    else:
        expanded = expand_placeholders(cwd, root_str, data_str)
        base = data_dir if str(cwd).startswith(_PLUGIN_DATA_TOKEN) else root
        resolved = resolve_within_root(base, expanded)
        if resolved is None:
            return None, [
                Finding(
                    severity=Severity.SKIPPED,
                    code="mcp.cwd_escapes_root",
                    spec_ref="§4.1",
                    message=(
                        f"Server {name!r} cwd {cwd!r} resolves outside its "
                        f"{'PLUGIN_DATA' if base is data_dir else 'PLUGIN_ROOT'}; entry skipped"
                    ),
                    path=where,
                )
            ]
        config["cwd"] = str(resolved)

    # Supplied by CAO, after the plugin's own env, per §9.1's ordering.
    config.setdefault("env", {})
    config["env"][_PLUGIN_ROOT_ENV] = root_str
    config["env"][_PLUGIN_DATA_ENV] = data_str

    return config, findings


_PLUGIN_ROOT_ENV = "PLUGIN_ROOT"
_PLUGIN_DATA_ENV = "PLUGIN_DATA"


def _credential_findings(
    values: Mapping[str, Any],
    server: str,
    block: str,
    where: str,
) -> List[Finding]:
    """Warn — never block — on credential-shaped ``env``/``headers`` values."""
    findings: List[Finding] = []
    for key in sorted(values):
        value = values[key]
        if not _looks_credential_shaped(key, value if isinstance(value, str) else ""):
            continue
        findings.append(
            Finding(
                severity=Severity.WARNING,
                code="mcp.credential_shaped_value",
                spec_ref="§9.2",
                message=(
                    f"Server {server!r} {block} key {key!r} looks credential-shaped. The "
                    f"specification forbids credentials here and CAO does not treat this as a "
                    f"supported credential mechanism — use `cao env` and the secret gate "
                    f"instead. The plugin was installed anyway; the value is unchanged."
                ),
                path=where,
            )
        )
    return findings


# --- Convenience + integration ---------------------------------------------


def load_and_map(
    root: Path,
    data_dir: Path,
    *,
    provider: Optional[str] = None,
    plugin_schema_id: Optional[str] = None,
) -> MappedMcpResult:
    """Read ``<root>/mcp.json`` if present and map it. Never raises."""
    contained = resolve_within_root(root, MCP_FILENAME)
    if contained is None:
        return MappedMcpResult(
            findings=(
                Finding(
                    severity=Severity.SKIPPED,
                    code="mcp.escapes_root",
                    spec_ref="§4.1",
                    message="mcp.json resolves outside the plugin root; MCP configuration ignored",
                    path=MCP_FILENAME,
                ),
            ),
            present=False,
            valid=False,
        )

    if not contained.exists():
        return MappedMcpResult(present=False, valid=True)  # §6.2: not an error

    if not contained.is_file():
        return MappedMcpResult(
            findings=(
                Finding(
                    severity=Severity.SKIPPED,
                    code="mcp.not_a_file",
                    spec_ref="§6.2",
                    message="mcp.json exists but is not a regular file; MCP configuration ignored",
                    path=MCP_FILENAME,
                ),
            ),
            present=False,
            valid=False,
        )

    try:
        cfg = json.loads(contained.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        return MappedMcpResult(
            findings=(
                Finding(
                    severity=Severity.SKIPPED,
                    code="mcp.invalid_json",
                    spec_ref="§7.2.2",
                    message=(
                        f"mcp.json could not be parsed ({exc}); MCP disabled for this plugin. "
                        f"Its skills are unaffected."
                    ),
                    path=MCP_FILENAME,
                ),
            ),
            present=True,
            valid=False,
        )

    return map_mcp_config(root, data_dir, cfg, provider=provider, plugin_schema_id=plugin_schema_id)


def is_pre_expanded(entry: Mapping[str, Any]) -> bool:
    """Whether a CAO ``mcpServers`` entry came from a plugin and is already expanded."""
    return bool(isinstance(entry, Mapping) and entry.get(PRE_EXPANDED_KEY))


def strip_marker(entry: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the entry without CAO's internal marker.

    Called before a provider config is written: the marker is CAO bookkeeping
    and must never reach a provider's own configuration file.
    """
    return {key: value for key, value in entry.items() if key != PRE_EXPANDED_KEY}
