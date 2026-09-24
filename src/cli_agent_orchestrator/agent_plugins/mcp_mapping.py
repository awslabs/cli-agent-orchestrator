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

import ipaddress
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

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
    # Serializers that can express a url entry as well as a local command.
    # ``grok_cli`` emits a TOML ``[mcp_servers.<name>]`` table with ``url``/``type``
    # (its ``type`` vocabulary differs from CAO's — see ``GROK_URL_TRANSPORTS``);
    # ``omp`` and ``mcode`` write a JSON ``mcpServers`` document that carries the
    # entry through.
    "omp": _ALL_TRANSPORTS,
    "grok_cli": _ALL_TRANSPORTS,
    "mcode": _ALL_TRANSPORTS,
    # ``devin_cli``'s dedicated ``mcp_config*.json`` schema carries both shapes:
    # ``command``/``args``/``env`` stdio entries and ``url``/``transport``
    # (``"http"|"sse"``) remotes — see ``DevinCliProvider._DEVIN_TRANSPORTS``.
    "devin_cli": _ALL_TRANSPORTS,
    # No MCP delivery path at all. An empty set, not stdio-only: these providers
    # build no MCP configuration whatsoever, so reporting a *skip* per server is
    # the honest answer and "stdio is deliverable" would be a false claim. The
    # delivery-matrix test derives its expectations from this table, so the two
    # cannot drift.
    "hermes": frozenset(),
    "mock_cli": frozenset(),
}
DEFAULT_TRANSPORTS = _STDIO_ONLY

#: Providers whose config serializer constrains MCP **server names**, and the
#: pattern a name must match to be deliverable.
#:
#: MiniMax Code writes each server into a plugin manifest whose loader rejects a
#: name outside this shape, and ``minimax_code._serialize_server`` raises
#: ``ProviderError`` on one. That raise happens during terminal creation, so
#: without this gate a plugin free to name its server ``Demo_Tools`` (the MCP
#: spec and CAO's own schema place no such restriction) would make the provider
#: **unlaunchable** rather than merely missing a tool. Reported as a skip here so
#: the server is dropped and the agent still starts.
#: Codex builds every server field as a ``-c mcp_servers.<name>.<field>=`` override
#: whose PATH is a TOML dotted path, so a dot in the NAME silently nests the entry
#: under the wrong table (``mcp_servers.acme.tools.command`` ->
#: ``mcp_servers['acme']['tools']``) and ``codex._validate_config_key`` raises during
#: terminal creation. Whether Codex's path parser honours a quoted segment
#: (``mcp_servers."a.b"``) was NOT verified, so the name is isolated rather than
#: quoted. Imported back into the provider so gate and serializer cannot drift.
#: Reported by review 5222539218 on #584 (item 6).
CODEX_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")

#: How each provider's config format carries an MCP server's working directory.
#:
#: ``native`` -- the format has a field for it: Codex ``cwd`` and Antigravity ``cwd``
#: are documented; OpenCode's ``McpLocalConfig.cwd`` and FastMCP's
#: ``StdioMCPServer.cwd`` (Kimi) exist in their schemas. ``shim`` -- the format has
#: NO such field, so the directory is carried by a ``/bin/sh`` wrapper
#: (:func:`~cli_agent_orchestrator.utils.mcp_resolution.apply_cwd_shim`); each of
#: these was checked against the vendor's own MCP documentation on 2026-09-16 and
#: none documents a working-directory key. ``none`` -- the provider builds no MCP
#: configuration at all, so there is nothing to carry (these already report
#: ``mcp.provider_unsupported`` per server).
#:
#: Exhaustive over ``ProviderType`` and asserted so by
#: ``test_the_cwd_delivery_table_is_exhaustive_over_provider_type``, for the same
#: reason ``PROVIDER_TRANSPORTS`` is: a provider omitted here would silently drop
#: the plugin's declared directory with nothing failing.
#: Reported by review 5222539218 on #584 (item 4).
PROVIDER_CWD_DELIVERY: Dict[str, str] = {
    "opencode_cli": "native",
    "kimi_cli": "native",
    "codex": "native",
    "antigravity_cli": "native",
    "grok_cli": "shim",
    "mcode": "shim",
    "kiro_cli": "shim",
    "claude_code": "shim",
    "cursor_cli": "shim",
    "copilot_cli": "shim",
    "omp": "shim",
    # Devin's ``mcp_config`` schema has no stdio working-directory field.
    "devin_cli": "shim",
    "hermes": "none",
    "mock_cli": "none",
}


def _is_stdio_entry(entry: Any) -> bool:
    """Whether ``entry`` is a stdio server (the only kind that carries a ``cwd``).

    A missing ``type`` is treated as stdio when a ``command`` is present, matching
    how ``_map_entry`` decides which branch to take.
    """

    if not isinstance(entry, Mapping):
        return False
    declared = entry.get("type")
    if isinstance(declared, str):
        return declared == "stdio"
    return "command" in entry


def _cwd_shim_available() -> bool:
    """Whether this host can run the ``/bin/sh`` working-directory wrapper.

    Separate function so tests can force the unavailable branch; the shim itself
    is pure and has no opinion about the host.
    """

    return os.name == "posix" and os.path.exists("/bin/sh")


PROVIDER_SERVER_NAME_PATTERNS: Dict[str, "re.Pattern[str]"] = {
    "mcode": re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"),
    "codex": CODEX_BARE_KEY,
}

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
    """Whether the DOCUMENT is usable -- not whether every entry mapped.

    False only for an envelope-level defect: not an object, a missing or
    mismatched ``$schema``, an unknown top-level key, or ``mcpServers`` that is
    not an object. In each case there is no well-formed entry left to preserve.

    A per-entry failure NEVER sets this False, even when it happens to every
    entry (review 4 item 3 on #584). That is the distinction the reviewer asked
    for, and it is load-bearing downstream: ``cao plugin validate`` exits
    non-zero only for an unloadable package, so a document whose entries were
    individually skipped still exits 0, and ``mcp_delivery`` reports
    ``unusable_config`` only for the envelope case.

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

    document_errors = _document_errors(cfg)
    if document_errors:
        return MappedMcpResult(findings=tuple(document_errors), present=True, valid=False)

    servers: List[MappedServer] = []
    raw_servers = cfg.get("mcpServers") or {}
    allowed = PROVIDER_TRANSPORTS.get(provider, DEFAULT_TRANSPORTS) if provider else _ALL_TRANSPORTS

    if provider and not allowed:
        # The provider builds no MCP configuration at all, so *no* transport
        # would help. Reported as its own code rather than as
        # `transport_unsupported`: the latter names a transport and lists the
        # supported ones, which reads as "declare a different type and it will
        # work" — advice that cannot be followed here. Per server, so an operator
        # reading the install report sees each tool they are not getting.
        findings.extend(
            Finding(
                severity=Severity.SKIPPED,
                code="mcp.provider_unsupported",
                spec_ref="CAO policy",
                message=(
                    f"Provider {provider!r} has no MCP delivery path in CAO; server "
                    f"{name!r} is not delivered. Its skills are unaffected."
                ),
                path=f"{MCP_FILENAME}#{name}",
            )
            for name in sorted(raw_servers)
        )
        return MappedMcpResult(findings=tuple(findings), present=True, valid=True)

    name_pattern = PROVIDER_SERVER_NAME_PATTERNS.get(provider) if provider else None

    # A shim provider on a host with no `/bin/sh` cannot carry the plugin's
    # declared working directory at all. Reported per stdio server rather than
    # passed through, because passing it through would run the server from the
    # wrong directory -- the exact defect item 4 is about -- and silently. Remote
    # entries have no working directory and are unaffected. Mapping time, like the
    # provider-name gate, so nothing reaches launch in the ignoring state.
    cwd_shim_missing = (
        provider is not None
        and PROVIDER_CWD_DELIVERY.get(provider) == "shim"
        and not _cwd_shim_available()
    )

    for name in sorted(raw_servers):
        entry = raw_servers[name]
        entry_schema_errors = _server_errors(name, entry)
        if entry_schema_errors:
            findings.extend(entry_schema_errors)
            continue
        if cwd_shim_missing and _is_stdio_entry(entry):
            findings.append(
                Finding(
                    severity=Severity.SKIPPED,
                    code="mcp.cwd_unsupported",
                    spec_ref="CAO policy",
                    message=(
                        f"Provider {provider!r} has no working-directory field in its MCP "
                        f"config format, so CAO carries it with a /bin/sh wrapper -- and "
                        f"/bin/sh is not available on this host. Server {name!r} declares a "
                        f"working directory that cannot be honoured, so it is not delivered "
                        f"rather than run from the wrong directory. Its skills are unaffected."
                    ),
                    path=f"{MCP_FILENAME}#{name}",
                )
            )
            continue
        mapped, entry_findings = _map_entry(
            name, entry, root_str, data_str, root, data_dir, allowed, name_pattern
        )
        findings.extend(entry_findings)
        if mapped is not None:
            servers.append(mapped)

    return MappedMcpResult(
        servers=tuple(servers), findings=tuple(findings), present=True, valid=True
    )


#: RFC 9110 §5.1 field-name token characters.
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

#: RFC 9110 §5.5 field-value characters, MINUS obs-text. Visible ASCII plus SP
#: and HTAB. obs-text (%x80-FF) is deprecated by RFC 9110 itself and is not
#: reliably carried by HTTP clients, so a non-ASCII value is refused rather than
#: delivered as something the transport may mangle.
_HEADER_VALUE_RE = re.compile(r"^[\t\x20-\x7e]*$")


def _is_loopback(hostname: str) -> bool:
    """Whether a URL hostname names this machine.

    ``127.0.0.0/8`` in full, not just ``127.0.0.1``: the whole block is loopback
    and a sidecar is free to bind ``127.0.0.2``.
    """
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _remote_url_findings(name: str, url: Any, where: str) -> List[Finding]:
    """Semantic checks the schema's ``format: uri`` cannot express.

    ``format`` is an annotation, not an assertion: a schema-valid URI may still
    be a cleartext link to an arbitrary host, or carry userinfo, or a fragment.
    Each is refused per entry, so a sibling server is unaffected.
    """
    if not isinstance(url, str) or not url:
        return [
            Finding(
                severity=Severity.SKIPPED,
                code="mcp.url_invalid",
                spec_ref="§7.2.2",
                message=f"Server {name!r} declares no usable url; entry skipped",
                path=where,
            )
        ]

    def refuse(code: str, why: str) -> List[Finding]:
        return [
            Finding(
                severity=Severity.SKIPPED,
                code=code,
                spec_ref="§7.2.2",
                message=f"Server {name!r} url {why}; entry skipped",
                path=where,
            )
        ]

    try:
        parts = urlsplit(url)
    except ValueError:
        return refuse("mcp.url_invalid", "is not a parseable URL")

    if parts.scheme.lower() not in ("http", "https"):
        return refuse("mcp.url_invalid", "must use http or https")
    try:
        hostname = parts.hostname
    except ValueError:  # malformed IPv6 literal, bad port
        return refuse("mcp.url_invalid", "has an unparseable host")
    if not hostname:
        return refuse("mcp.url_invalid", "has no host")
    if parts.username is not None or parts.password is not None:
        # Never delivered, and never echoed back: the message must not leak the
        # credential it is refusing.
        return refuse("mcp.url_invalid", "must not embed credentials in userinfo")
    if parts.fragment:
        return refuse("mcp.url_invalid", "must not carry a fragment")
    if parts.scheme.lower() == "http" and not _is_loopback(hostname):
        return refuse(
            "mcp.url_insecure",
            "uses cleartext http to a non-loopback host (use https, or bind the "
            "server to loopback)",
        )
    return []


def _header_findings(name: str, headers: Mapping[str, Any], where: str) -> List[Finding]:
    """Reject header names/values a transport cannot carry safely.

    Case-insensitive duplicates are refused rather than resolved: HTTP field
    names are case-insensitive, so ``X-Tenant`` and ``x-tenant`` are one header
    supplied twice. Delivered through a dict the later silently wins, and which
    one the author meant is unknowable.
    """
    seen: Dict[str, str] = {}
    for key, value in headers.items():
        if not isinstance(key, str) or not _HEADER_NAME_RE.match(key):
            return [
                Finding(
                    severity=Severity.SKIPPED,
                    code="mcp.headers_invalid",
                    spec_ref="§7.2.2",
                    message=(
                        f"Server {name!r} declares header name {key!r}, which is not an "
                        f"RFC 9110 token; entry skipped"
                    ),
                    path=where,
                )
            ]
        lowered = key.lower()
        if lowered in seen:
            return [
                Finding(
                    severity=Severity.SKIPPED,
                    code="mcp.headers_invalid",
                    spec_ref="§7.2.2",
                    message=(
                        f"Server {name!r} declares header {lowered!r} twice "
                        f"({seen[lowered]!r} and {key!r}); HTTP field names are "
                        f"case-insensitive, so which value applies is undefined. Entry skipped"
                    ),
                    path=where,
                )
            ]
        seen[lowered] = key
        if not isinstance(value, str) or not _HEADER_VALUE_RE.match(value):
            # The value is NOT echoed: an invalid one is as likely to be a
            # mis-pasted secret as a typo.
            return [
                Finding(
                    severity=Severity.SKIPPED,
                    code="mcp.headers_invalid",
                    spec_ref="§7.2.2",
                    message=(
                        f"Server {name!r} header {key!r} has a value that is not a valid "
                        f"HTTP field value (visible ASCII, space and tab only); entry skipped"
                    ),
                    path=where,
                )
            ]
    return []


def _version_of(schema_id: Optional[str]) -> str:
    """Extract the ``1.0.0`` segment from a canonical schema URL."""
    match = re.search(r"/schemas/([^/]+)/", schema_id or "")
    return match.group(1) if match else ""


#: JSON-pointer fragments for the per-transport server subschemas. A declared
#: ``type`` selects one directly; anything else falls back to the ``server``
#: ``oneOf``, which is what the document-level schema would have applied anyway.
_SERVER_FRAGMENTS = {
    "stdio": "#/$defs/stdioServer",
    "streamable-http": "#/$defs/streamableHttpServer",
    "sse": "#/$defs/sseServer",
}
_SERVER_FRAGMENT_FALLBACK = "#/$defs/server"


def _document_errors(cfg: Mapping[str, Any]) -> List[Finding]:
    """Validate the ENVELOPE only: ``$schema``, unknown keys, ``mcpServers`` shape.

    The servers are deliberately not validated here. Substituting an empty
    ``mcpServers`` object leaves every top-level rule in force -- the schema
    declares ``required: ["$schema", "mcpServers"]`` and
    ``additionalProperties: false``, so a missing or wrong ``$schema`` and any
    unknown top-level key are still caught -- while removing the entries that
    used to make one bad server condemn the file.

    The substitution is skipped when ``mcpServers`` is not a mapping, so that
    defect is still reported by the schema rather than masked by the empty object
    standing in for it.
    """
    envelope: Mapping[str, Any] = cfg
    if isinstance(cfg.get("mcpServers"), Mapping):
        envelope = {**cfg, "mcpServers": {}}
    return _schema_errors(envelope)


def _server_errors(name: str, entry: Any) -> List[Finding]:
    """Validate ONE server entry against its transport's subschema.

    Findings are ``mcp.server_invalid`` rather than ``mcp.invalid``: the codes
    carry the blast radius, so a reader (and ``_LOUD_CODES``) can tell "this entry
    is not delivered" from "this file is not usable".
    """
    declared = entry.get("type") if isinstance(entry, Mapping) else None
    fragment = (
        _SERVER_FRAGMENTS.get(declared, _SERVER_FRAGMENT_FALLBACK)
        if isinstance(declared, str)
        else _SERVER_FRAGMENT_FALLBACK
    )
    try:
        validator = _offline_validator(MCP_SCHEMA_FILENAME, fragment)
    except SchemaUnavailableError as exc:  # pragma: no cover - packaging defect
        return [_schema_unavailable_finding(exc, path=f"{MCP_FILENAME}#{name}")]

    errors = sorted(
        validator.iter_errors(entry),
        key=lambda error: (list(map(str, error.absolute_path)), error.message),
    )
    if not errors:
        return []
    detail = "; ".join(
        (
            f"{'.'.join(str(p) for p in error.absolute_path)}: {error.message}"
            if error.absolute_path
            else error.message
        )
        for error in errors
    )
    return [
        Finding(
            severity=Severity.SKIPPED,
            code="mcp.server_invalid",
            spec_ref="§7.2.2",
            message=(
                f"Server {name!r} does not match the {MCP_SCHEMA_FILENAME} server schema; "
                f"entry skipped, its siblings are unaffected ({detail})"
            ),
            path=f"{MCP_FILENAME}#{name}",
        )
    ]


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
    name_pattern: Optional["re.Pattern[str]"] = None,
) -> Tuple[Optional[MappedServer], List[Finding]]:
    """Map one ``mcpServers`` entry. Failure invalidates only this entry."""
    findings: List[Finding] = []
    where = f"{MCP_FILENAME}#{name}"

    if name_pattern is not None and not name_pattern.fullmatch(name):
        # Checked beside the transport rule because it is the same kind of rule:
        # a schema-valid value the target provider's serializer cannot express.
        # Skipped rather than passed through because this one does not merely
        # produce bad config — MiniMax's `_serialize_server` *raises*, during
        # terminal creation, so passing it through costs the operator the whole
        # agent instead of one tool.
        return None, [
            Finding(
                severity=Severity.SKIPPED,
                code="mcp.server_name_unsupported",
                spec_ref="CAO policy",
                message=(
                    f"Server {name!r} has a name the target provider cannot express "
                    f"(must match {name_pattern.pattern}); entry skipped"
                ),
                path=where,
            )
        ]

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
        # names or values. They ARE checked: the schema's `format: uri` is an
        # annotation, not an assertion, so the semantic rules live here.
        url = entry.get("url")
        url_findings = _remote_url_findings(name, url, where)
        if url_findings:
            return None, findings + url_findings
        config["url"] = url

        headers = entry.get("headers")
        if isinstance(headers, Mapping):
            header_findings = _header_findings(name, headers, where)
            if header_findings:
                return None, findings + header_findings
            config["headers"] = dict(headers)
            # Warning-only by specification: an authorization-shaped value is a
            # hygiene note, never a reason to withhold the server.
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
