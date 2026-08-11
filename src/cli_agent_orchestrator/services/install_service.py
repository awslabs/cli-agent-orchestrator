"""Service helpers for installing agent profiles."""

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple
from urllib.parse import urlparse

import frontmatter
import requests  # type: ignore[import-untyped]
from pydantic import BaseModel

from cli_agent_orchestrator.agent_plugins.mcp_delivery import (
    McpDeliveryResult,
    apply_plugin_mcp_servers,
    log_delivery_findings,
    merge_plugin_mcp_servers,
    opencode_config_collision_finding,
)
from cli_agent_orchestrator.agent_plugins.mcp_mapping import is_pre_expanded as is_plugin_mcp_entry
from cli_agent_orchestrator.agent_plugins.mcp_mapping import strip_marker as strip_plugin_mcp_marker
from cli_agent_orchestrator.agent_plugins.models import Finding
from cli_agent_orchestrator.agent_plugins.store import InstalledPluginStore
from cli_agent_orchestrator.constants import (
    AGENT_CONTEXT_DIR,
    COPILOT_AGENTS_DIR,
    DEFAULT_PROVIDER,
    KIRO_AGENTS_DIR,
    OPENCODE_AGENTS_DIR,
    SKILLS_DIR,
)
from cli_agent_orchestrator.models.copilot_agent import CopilotAgentConfig
from cli_agent_orchestrator.models.kiro_agent import KiroAgentConfig
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.models.opencode_agent import OpenCodeAgentConfig
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.services.profile_store import write_profile
from cli_agent_orchestrator.utils.agent_profiles import (
    _read_agent_profile_source,
    parse_agent_profile_text,
)
from cli_agent_orchestrator.utils.env import resolve_env_vars, set_env_var
from cli_agent_orchestrator.utils.mcp_resolution import resolve_mcp_server_config
from cli_agent_orchestrator.utils.opencode_config import (
    disable_mcp_server,
    ensure_skills_symlink,
    entry_within_roots,
    is_cao_owned_mcp_entry,
    read_config,
    remove_agent_tools,
    to_opencode_agent_id,
    translate_mcp_server_config,
    upsert_agent_tools,
    upsert_mcp_server,
)
from cli_agent_orchestrator.utils.opencode_permissions import cao_tools_to_opencode_permission
from cli_agent_orchestrator.utils.skill_injection import compose_agent_prompt
from cli_agent_orchestrator.utils.tool_mapping import resolve_allowed_tools

logger = logging.getLogger(__name__)


class InstallResult(BaseModel):
    """Structured result for agent profile installation."""

    success: bool
    message: str
    agent_name: Optional[str] = None
    context_file: Optional[str] = None
    agent_file: Optional[str] = None
    unresolved_vars: Optional[List[str]] = None
    source_kind: Optional[Literal["url", "file", "name"]] = None
    provider: Optional[str] = None


# Profile names are used as filesystem path segments under LOCAL_AGENT_STORE_DIR
# and provider agent dirs. Restricting to [A-Za-z0-9_-] with a 64-char cap blocks
# traversal ("../etc/passwd"), separators, and absolute paths at the boundary.
# CodeQL also recognises this regex as a path-injection sanitiser.
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Per-MCP-server tool-call timeout (milliseconds) injected into cao-mcp-server
# entries in kiro agent profiles. kiro-cli's default MCP tool-call timeout
# (~120s, inherited from the Q Developer CLI) is far too short for the handoff
# tool, which blocks until a spawned worker finishes an entire task — routinely
# minutes. Without a raised timeout kiro cancels the handoff RPC client-side and
# tells the supervisor the tool failed even though CAO is still running the
# worker. 1_200_000 ms (20 min) matches CAO's default handoff/run-step budget.
# This mirrors the kimi_cli provider's tool_call_timeout_ms override.
_KIRO_MCP_TOOL_TIMEOUT_MS = 1_200_000


def _inject_kiro_mcp_timeout(
    mcp_servers: Optional[Dict[str, object]],
) -> Optional[Dict[str, object]]:
    """Return a copy of ``mcp_servers`` with a large ``timeout`` set on every
    cao-mcp-server entry that does not already specify one.

    kiro reads the per-server ``timeout`` field (milliseconds) as its tool-call
    timeout. We only touch entries whose name, command, or args reference the
    bundled orchestration server so a user's other MCP servers keep their own
    (or kiro's default) timeout. An explicit operator-set ``timeout`` is never
    overwritten. The command/args checks cover every form the entry can take:
    the bare console script, a resolved absolute path, the module entrypoint
    (``<python> -m cli_agent_orchestrator.mcp_server.server``), and the legacy
    ``uvx --from git+... cao-mcp-server`` form.
    """
    if not mcp_servers:
        return mcp_servers

    result: Dict[str, object] = {}
    for name, cfg in mcp_servers.items():
        if not isinstance(cfg, dict):
            result[name] = cfg
            continue
        command = cfg.get("command")
        args = cfg.get("args") or []
        is_cao = (
            name == "cao-mcp-server"
            or (isinstance(command, str) and "cao-mcp-server" in command)
            or any(
                isinstance(a, str)
                and ("cao-mcp-server" in a or a == "cli_agent_orchestrator.mcp_server.server")
                for a in args
            )
        )
        if is_cao and "timeout" not in cfg:
            cfg = {**cfg, "timeout": _KIRO_MCP_TOOL_TIMEOUT_MS}
        result[name] = cfg
    return result


# URL path component for allowlisted hosts. Each segment must start with an
# alphanumeric, which forbids "..", "." and hidden segments — and by extension
# any traversal sequence. Used to rebuild a safe URL from validated parts,
# which is the CodeQL-recognised SSRF sanitisation pattern.
_SAFE_URL_PATH_RE = re.compile(r"^(/[A-Za-z0-9_][A-Za-z0-9_.-]*)+\.md$")

# SSRF guard: only fetch profiles from hosts we explicitly trust. Operators can
# extend via CAO_PROFILE_ALLOWED_HOSTS (e.g. an internal profile mirror).
_DEFAULT_ALLOWED_HOSTS = frozenset(
    {
        "github.com",
        "raw.githubusercontent.com",
    }
)

# (connect, read) seconds. Tighter than a single-number timeout: 5s connect fails
# fast on a dead/hostile IP; 30s read leaves room for flaky residential networks
# without letting a slow-loris peer tie up a cao-server worker indefinitely.
_HTTP_TIMEOUT = (5, 30)


def _allowed_download_hosts() -> frozenset:
    override = os.environ.get("CAO_PROFILE_ALLOWED_HOSTS")
    if override:
        hosts = {h.strip().lower() for h in override.split(",") if h.strip()}
        if hosts:
            return frozenset(hosts)
    return _DEFAULT_ALLOWED_HOSTS


def _download_agent(source: str) -> str:
    """Download an agent profile from an https:// URL into the local store.

    File-path handling deliberately does NOT live in this module: only the CLI
    has legitimate filesystem trust, and keeping Path(user_input) out of the
    HTTP-reachable layer closes an entire class of py/path-injection alerts
    (CodeQL #49/#61 kept reopening while this lived here). The CLI entry point
    resolves the local file itself and stores it via profile_store, then calls
    install_agent() with the bare stem, which flows through the "name" branch.
    This function only ever hands profile_store a stem it has already validated,
    never a caller-supplied path.
    """
    # SSRF hardening: narrow what a caller-provided URL can reach before any
    # network I/O happens. https-only rules out http://169.254.169.254/...;
    # the host allowlist rules out arbitrary internal services; the path
    # regex rules out crafted paths that would write outside the store.
    parsed = urlparse(source)
    if parsed.scheme != "https":
        raise ValueError("Profile URL must use https://")
    host = (parsed.hostname or "").lower()
    allowed_hosts = _allowed_download_hosts()
    if host not in allowed_hosts:
        raise ValueError(
            f"Host '{host}' is not in the allowed downloader hosts. "
            "Set CAO_PROFILE_ALLOWED_HOSTS to extend the allowlist."
        )
    # Reject any URL that carries a query string, fragment, or userinfo —
    # none of them are meaningful for a static .md fetch and each is an
    # SSRF foothold (credentials encoded in @, redirect targets in ?next=).
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("Profile URL must not include query, fragment, or userinfo.")
    if not _SAFE_URL_PATH_RE.fullmatch(parsed.path):
        raise ValueError("URL path must match /segment/.../file.md with no traversal segments.")
    filename = parsed.path.rsplit("/", 1)[-1]
    if not _PROFILE_NAME_RE.fullmatch(filename[: -len(".md")]):
        raise ValueError("URL filename stem must match [A-Za-z0-9_-]{1,64}")

    # Look up the canonical host from the allowlist instead of passing the
    # parsed host back through. Belt-and-braces: even if a caller smuggled
    # an odd Unicode codepoint that normalised into a known host name,
    # `safe_host` is guaranteed to be a literal from our trust root.
    safe_host = next(h for h in allowed_hosts if h == host)
    safe_url = f"https://{safe_host}{parsed.path}"

    # allow_redirects=False + explicit is_redirect check: an allowlisted
    # host could otherwise 302 us to an internal target (IMDS, admin panel)
    # and the allowlist would never see the hop.
    response = requests.get(safe_url, timeout=_HTTP_TIMEOUT, allow_redirects=False)
    if response.is_redirect:
        raise ValueError("Redirects are not allowed for profile downloads.")
    response.raise_for_status()

    # The stem was validated against _PROFILE_NAME_RE above; profile_store owns
    # the store join and the atomic write. overwrite=True preserves the
    # pre-existing re-download behaviour of replacing the stored copy.
    stem = filename[: -len(".md")]
    write_profile(stem, response.text, overwrite=True)
    return stem


def parse_env_assignment(env_assignment: str) -> Tuple[str, str]:
    """Parse a ``KEY=VALUE`` assignment used for install-time env injection."""
    if "=" not in env_assignment:
        raise ValueError(f"Invalid env var '{env_assignment}'. Expected format KEY=VALUE.")

    key, value = env_assignment.split("=", 1)
    if not key:
        raise ValueError(f"Invalid env var '{env_assignment}'. Key must not be empty.")

    return key, value


def _write_context_file(agent_name: str, raw_content: str) -> Path:
    """Write the unresolved profile source to the shared context directory."""
    AGENT_CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    context_file = AGENT_CONTEXT_DIR / f"{agent_name}.md"
    context_file.write_text(raw_content, encoding="utf-8")
    return context_file


def _build_provider_config(
    profile_name: str,
    resolved_prompt: str,
    description: str,
) -> frontmatter.Post:
    """Create the frontmatter post for a Copilot agent file."""
    return frontmatter.Post(
        resolved_prompt.rstrip(),
        name=profile_name,
        description=description,
    )


def _materialize_opencode_mcp(
    agent_id: str,
    merged_servers: Optional[Dict[str, Any]],
    plugin_delivery: McpDeliveryResult,
    *,
    agent_name: str,
) -> None:
    """Write this agent's MCP servers into the shared ``opencode.json`` safely.

    OpenCode keeps every provider's MCP declarations in one shared file that CAO
    edits in place, which creates two obligations Kiro's/Copilot's wholesale
    rewrites do not have (design.md §10a):

    * **Never clobber a user's entry (Finding 2).** A plugin-derived server whose
      name already exists under an entry CAO cannot prove it owns is dropped with
      a report, not overwritten — and the agent is not granted a tool alias for a
      server CAO did not write.
    * **Disable, don't orphan, a withdrawn server (Finding 1).** Removal deletes a
      plugin's ``PLUGIN_ROOT`` but there is no ``opencode.json`` delete, so a
      server no longer in the desired set is set ``enabled: false`` — only when it
      is provably CAO's (its command resolves inside the plugin store), so a
      user's own server and CAO's ``cao-mcp-server`` are never touched.

    Ownership is a heuristic without persisted provenance; see
    ``opencode_config.is_cao_owned_mcp_entry`` and design.md §10a options 1/2 for
    the exact-cleanup follow-up. Keeping the top-level ``tools`` default-deny and
    the per-agent grant matches the prior in-place behaviour.
    """
    store = InstalledPluginStore()
    plugin_store_roots = (store.plugins_dir, store.data_dir)
    plugin_derived = set(plugin_delivery.servers)

    # Snapshot the pre-write state so a server written earlier in this same pass
    # is never mistaken for a pre-existing user entry on a later iteration.
    existing_before = read_config().get("mcp", {})
    if not isinstance(existing_before, dict):
        existing_before = {}

    collisions: List[Finding] = []

    if merged_servers:
        granted: List[str] = []
        for mcp_name, mcp_cfg in merged_servers.items():
            opencode_mcp_cfg = translate_mcp_server_config(dict(mcp_cfg))
            existing = existing_before.get(mcp_name)
            if (
                mcp_name in plugin_derived
                and isinstance(existing, dict)
                and not is_cao_owned_mcp_entry(
                    existing, opencode_mcp_cfg, plugin_store_roots=plugin_store_roots
                )
            ):
                collisions.append(
                    opencode_config_collision_finding(
                        server_name=mcp_name,
                        plugin=plugin_delivery.owners.get(mcp_name, "unknown"),
                    )
                )
                continue
            upsert_mcp_server(mcp_name, opencode_mcp_cfg)
            granted.append(mcp_name)
        # Grant only the servers actually written for this agent (a dropped
        # collision is excluded); a reinstall without MCP takes the else and
        # withdraws the whole grant, as before.
        upsert_agent_tools(agent_id, granted)
    else:
        remove_agent_tools(agent_id)

    # Finding 1 reconcile: disable any CAO-plugin server no longer desired.
    # Plugin servers are delivered to every agent uniformly, so a server absent
    # from this agent's merged set means its plugin was uninstalled. The
    # plugin-store containment check is what keeps this from touching a user's
    # own server or CAO's ``cao-mcp-server`` (neither resolves inside the store).
    desired = set(merged_servers or {})
    current = read_config().get("mcp", {})
    if isinstance(current, dict):
        for name, cfg in current.items():
            if name in desired or not isinstance(cfg, dict) or cfg.get("enabled") is False:
                continue
            if entry_within_roots(cfg, plugin_store_roots):
                disable_mcp_server(name)

    if collisions:
        log_delivery_findings(McpDeliveryResult(findings=tuple(collisions)), agent_name=agent_name)


def install_agent(
    source: str,
    provider: Optional[str] = None,
    env_vars: Optional[Dict[str, str]] = None,
) -> InstallResult:
    """Install an agent profile for the requested provider.

    ``provider`` resolution follows the same precedence as launch/handoff
    (see ``resolve_provider``): an explicit argument wins, then the profile's
    frontmatter ``provider:`` key, then ``DEFAULT_PROVIDER``. Pass ``None``
    to defer to the profile.

    ``source`` must be either an https:// URL on the allowlist or a bare
    profile name matching ``_PROFILE_NAME_RE``. Local ``.md`` file paths
    are deliberately NOT accepted here — the CLI copies user files into
    the local store itself and then calls this function with the resulting
    bare stem. This split is what lets the HTTP/MCP surface share this
    function safely: every caller reaches the same two sanitised shapes,
    and no call site constructs ``Path(user_input)`` through this module.
    """
    try:
        valid_providers = [provider_type.value for provider_type in ProviderType]
        # An explicit provider is validated up front so bad input fails fast
        # BEFORE any URL download or env-file mutation. Frontmatter providers
        # are validated after the profile is parsed (below).
        if provider is not None and provider not in valid_providers:
            return InstallResult(
                success=False,
                message=(
                    f"Invalid provider '{provider}'. "
                    f"Valid providers: {', '.join(valid_providers)}"
                ),
            )

        if source.startswith(("http://", "https://")):
            agent_name = _download_agent(source)
            source_kind: Literal["url", "name"] = "url"
        else:
            # `source` is treated as a bare profile name and feeds
            # _read_agent_profile_source() which builds Path objects from it.
            # Enforce the sanitiser at the boundary so every downstream sink
            # (agent_profiles.py and the provider-dir loop) sees safe input.
            if not _PROFILE_NAME_RE.fullmatch(source):
                return InstallResult(
                    success=False,
                    message=(
                        f"Invalid profile name '{source}'. "
                        "Expected a name matching [A-Za-z0-9_-]{1,64}, "
                        "an https:// URL, or (CLI only) a local .md file path."
                    ),
                )
            agent_name = source
            source_kind = "name"

        if env_vars:
            for key, value in env_vars.items():
                set_env_var(key, value)

        raw_content = _read_agent_profile_source(agent_name)
        resolved_content = resolve_env_vars(raw_content)
        profile = parse_agent_profile_text(resolved_content, agent_name)

        # No explicit provider — honour the profile's frontmatter ``provider:``
        # key, mirroring resolve_provider() on the launch/handoff paths. Bogus
        # frontmatter values warn and fall back to the default; built-in store
        # profiles carry no frontmatter provider and keep the default.
        if provider is None:
            if profile.provider and profile.provider in valid_providers:
                provider = profile.provider
            else:
                if profile.provider:
                    logger.warning(
                        "Agent profile '%s' has invalid provider '%s'. "
                        "Valid providers: %s. Falling back to '%s'.",
                        profile.name,
                        profile.provider,
                        valid_providers,
                        DEFAULT_PROVIDER,
                    )
                provider = DEFAULT_PROVIDER

        # Resolve the bundled cao-mcp-server console script to a PATH-independent
        # invocation before materializing provider configs. The
        # configs Kiro/Q write to disk are consumed verbatim by those CLIs, so
        # resolution must happen here rather than at launch time. persisted=True
        # prefers the stable PATH launcher (e.g. ~/.local/bin/cao-mcp-server)
        # over the versioned venv-internal path, so a later `uv tool upgrade`
        # does not leave the written config pointing at a relocated binary.
        # Agent Plugins: merge installed plugins' mcp.json servers into this
        # profile's mcpServers. Placed HERE, between provider resolution and
        # CAO's own ${VAR} resolution below, and both halves of that placement
        # are load-bearing. After provider resolution, because the transport
        # matrix is provider-dependent (OpenCode carries stdio only, so a
        # url-based plugin server must be skipped for that provider alone).
        # Before the resolution pass, because the pre-expanded marker exists
        # precisely to be seen by that pass and skipped — merging afterwards
        # would leave the marker unread and leak it into provider config files.
        #
        # `profile.mcpServers` is the one shape from which install_service and
        # utils/opencode_config.translate_mcp_server_config already derive every
        # provider's native MCP form, so this single merge reaches all of them
        # with no per-provider code.
        #
        # Shared with the launch path (`mcp_delivery.with_plugin_mcp`) rather than
        # duplicated: review on #584 found the two had silently disagreed, because
        # this merge existed only here while five providers re-read the profile
        # from disk at launch.
        plugin_mcp = apply_plugin_mcp_servers(
            profile, provider=provider, persisted=True, normalize_existing=True
        )
        log_delivery_findings(plugin_mcp, agent_name=profile.name)

        unresolved_vars = sorted(set(re.findall(r"\$\{(\w+)\}", resolved_content)))
        context_file = _write_context_file(profile.name, raw_content)

        mcp_server_names = list(profile.mcpServers.keys()) if profile.mcpServers else None
        allowed_tools = resolve_allowed_tools(profile.allowedTools, profile.role, mcp_server_names)

        agent_file: Optional[Path] = None
        safe_filename = profile.name.replace("/", "__")

        if provider == ProviderType.KIRO_CLI.value:
            if profile.engine == KiroEngine.KAS:
                raise ValueError(
                    "Kiro KAS profiles cannot be installed in Phase 0: CAO cannot "
                    "render KAS profiles or translate allowedTools/toolsSettings to Cedar. "
                    "Set engine: v2 or wait for a later migration phase."
                )
            KIRO_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
            # Kiro natively supports skill:// resources with progressive loading
            # (metadata at startup, full content on demand).
            #
            # TWO globs, not one, and the second is not redundant. Kiro expands
            # these itself, so which files it finds depends on ITS glob
            # implementation, and `**` does not have one agreed meaning for
            # directory symlinks: Python's own stdlib `glob.glob(recursive=True)`
            # descends into them while `pathlib.Path.glob` does not. Agent-plugin
            # skills are projected into SKILLS_DIR as symlinks to the plugin
            # store, so under the stricter reading every plugin skill would be
            # invisible to Kiro alone while reaching all six other providers.
            #
            # `*/SKILL.md` is immune to that ambiguity: a single-level match names
            # the symlink as a directory entry and resolves through it, with no
            # recursive descent to opt out of. It covers exactly the layout CAO
            # guarantees — skills are immediate children of the store (see
            # docs/skills.md "No nested skill directories") — so it alone is
            # sufficient for CAO-managed skills. `**/SKILL.md` is kept because
            # Kiro supports nested skill directories natively even though CAO's
            # own catalog does not, and dropping it would silently narrow a
            # capability operators may already rely on.
            #
            # Duplicate matches are not a concern: both patterns resolve to the
            # same absolute paths, and Kiro deduplicates the skills it loads by
            # path. Verified by TestKiroFilesystemGlob in
            # test/agent_plugins/test_delivery_providers.py.
            kiro_resources = [
                f"file://{context_file.absolute()}",
                f"skill://{SKILLS_DIR}/**/SKILL.md",
                f"skill://{SKILLS_DIR}/*/SKILL.md",
            ]
            raw_prompt = (
                profile.prompt.strip() if profile.prompt and profile.prompt.strip() else None
            )
            kiro_agent_config = KiroAgentConfig(
                name=profile.name,
                description=profile.description,
                tools=profile.tools if profile.tools is not None else ["*"],
                allowedTools=allowed_tools,
                resources=kiro_resources,
                prompt=raw_prompt,
                # Raise the cao-mcp-server tool-call timeout so kiro doesn't
                # cancel long handoff RPCs client-side (see helper docstring).
                mcpServers=_inject_kiro_mcp_timeout(profile.mcpServers),
                toolAliases=profile.toolAliases,
                toolsSettings=profile.toolsSettings,
                hooks=profile.hooks,
                model=profile.model,
            )
            agent_file = KIRO_AGENTS_DIR / f"{safe_filename}.json"
            agent_file.write_text(
                kiro_agent_config.model_dump_json(indent=2, exclude_none=True),
                encoding="utf-8",
            )

        elif provider == ProviderType.COPILOT_CLI.value:
            COPILOT_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
            system_prompt = profile.system_prompt.strip() if profile.system_prompt else ""
            fallback_prompt = profile.prompt.strip() if profile.prompt else ""
            base_prompt = system_prompt or fallback_prompt
            if not base_prompt:
                raise ValueError(
                    f"Agent '{profile.name}' has no usable prompt content for Copilot "
                    "(both system_prompt and prompt are empty or whitespace)"
                )

            prompt = compose_agent_prompt(profile, base_prompt=base_prompt) or base_prompt
            copilot_agent_config = CopilotAgentConfig(
                name=profile.name,
                description=profile.description,
                prompt=prompt,
            )
            agent_file = COPILOT_AGENTS_DIR / f"{safe_filename}.agent.md"
            agent_file.write_text(
                frontmatter.dumps(
                    _build_provider_config(
                        profile_name=copilot_agent_config.name,
                        resolved_prompt=copilot_agent_config.prompt,
                        description=copilot_agent_config.description,
                    )
                ),
                encoding="utf-8",
            )

        elif provider == ProviderType.OPENCODE_CLI.value:
            OPENCODE_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
            ensure_skills_symlink()
            # OpenCode discovers skills natively from OPENCODE_CONFIG_DIR/skills,
            # so the installed system prompt should not embed the CAO skill catalog.
            body = profile.system_prompt or profile.prompt or ""
            opencode_agent_config = OpenCodeAgentConfig(
                description=profile.description,
                mode="all",
                permission=cao_tools_to_opencode_permission(allowed_tools),
            )
            agent_id = to_opencode_agent_id(profile.name)
            agent_file = OPENCODE_AGENTS_DIR / f"{agent_id}.md"
            agent_file.write_text(
                frontmatter.dumps(
                    frontmatter.Post(
                        body.rstrip() if body else "",
                        **opencode_agent_config.model_dump(exclude_none=True),
                    )
                ),
                encoding="utf-8",
            )

            # OpenCode uses a shared opencode.json for MCP declarations. Unlike
            # Kiro/Copilot, whose per-agent files are rewritten wholesale, this
            # file is edited in place — so delivery must also guard a user's own
            # entries and actively disable a plugin server that removal withdrew
            # (there is no delete). See _materialize_opencode_mcp / design.md §10a.
            _materialize_opencode_mcp(
                agent_id, profile.mcpServers, plugin_mcp, agent_name=profile.name
            )

        return InstallResult(
            success=True,
            message=f"Agent '{profile.name}' installed successfully",
            agent_name=profile.name,
            context_file=str(context_file),
            agent_file=str(agent_file) if agent_file else None,
            unresolved_vars=unresolved_vars or None,
            source_kind=source_kind,
            provider=provider,
        )

    except requests.RequestException as exc:
        return InstallResult(success=False, message=f"Failed to download agent: {exc}")
    except FileNotFoundError as exc:
        return InstallResult(success=False, message=str(exc))
    except Exception as exc:
        return InstallResult(success=False, message=f"Failed to install agent: {exc}")


def refresh_installed_agents_for_plugin_mcp() -> List[str]:
    """Re-materialize provider configs so plugin MCP servers appear and disappear.

    Skill delivery needs no equivalent: skills are *projected* into ``SKILLS_DIR``
    and every provider reads that store (or a catalog rebuilt from it) at launch,
    so installing a plugin makes its skills reachable without rewriting a single
    provider file. MCP is the opposite — ``mcpServers`` is **baked into each
    provider's config at install time**: Kiro's agent JSON carries it inline, and
    OpenCode's shared ``opencode.json`` carries it plus a per-agent tool grant.
    Nothing re-reads a plugin's ``mcp.json`` later. So without this, a plugin's
    servers would only reach agents installed *after* the plugin, and uninstalling
    a plugin would leave its servers configured in every provider file that
    already had them — pointing at a ``PLUGIN_ROOT`` that no longer exists.

    The mechanism is deliberately "re-run the real thing" rather than a targeted
    patch of each provider file. Editing configs in place would mean a second
    implementation of every provider's MCP shape, which would drift from
    ``install_agent`` and is exactly the per-provider duplication the mapping
    design exists to avoid. ``install_agent`` is idempotent for a bare profile
    name — it reads the profile from the local store, downloads nothing, and
    mutates no environment when no ``env_vars`` are passed — so replaying it
    reproduces the current profile plus the current plugin set.

    Best effort by contract: this is called from the plugin install and uninstall
    paths, and a profile that has since been deleted, or a provider config that
    cannot be written, must not fail the plugin operation. Failures are logged and
    skipped.

    Returns:
        The names of the agents whose provider config was re-materialized, in the
        order attempted. Useful to tests and to callers that want to report how
        far the refresh reached; never raises.
    """
    refreshed: List[str] = []

    if not AGENT_CONTEXT_DIR.is_dir():
        return refreshed

    # ``AGENT_CONTEXT_DIR/<name>.md`` is CAO's existing marker for "this agent is
    # CAO-managed" — ``skill_injection._is_cao_managed_copilot_agent`` already
    # uses exactly this test, so reusing it keeps one definition of managed.
    for context_file in sorted(AGENT_CONTEXT_DIR.glob("*.md")):
        agent_name = context_file.stem
        safe_filename = agent_name.replace("/", "__")

        for provider, artifact in (
            (ProviderType.KIRO_CLI.value, KIRO_AGENTS_DIR / f"{safe_filename}.json"),
            (ProviderType.COPILOT_CLI.value, COPILOT_AGENTS_DIR / f"{safe_filename}.agent.md"),
            (
                ProviderType.OPENCODE_CLI.value,
                OPENCODE_AGENTS_DIR / f"{to_opencode_agent_id(agent_name)}.md",
            ),
        ):
            # Only re-materialize what is actually installed. Installing an agent
            # for a provider the operator never chose would be a side effect, not
            # a refresh.
            try:
                if not artifact.is_file():
                    continue
            except OSError:  # pragma: no cover - unreadable provider dir
                continue

            try:
                result = install_agent(agent_name, provider)
            except Exception as exc:  # pragma: no cover - install_agent is total
                logger.warning(
                    "Could not refresh agent '%s' for provider '%s' after an agent-plugin "
                    "change: %s",
                    agent_name,
                    provider,
                    exc,
                )
                continue

            if result.success:
                refreshed.append(agent_name)
            else:
                logger.warning(
                    "Could not refresh agent '%s' for provider '%s' after an agent-plugin "
                    "change: %s",
                    agent_name,
                    provider,
                    result.message,
                )

    return refreshed
