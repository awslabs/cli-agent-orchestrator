"""Service helpers for installing agent profiles."""

import errno
import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple
from urllib.parse import urlparse

import frontmatter
import requests  # type: ignore[import-untyped]
from pydantic import BaseModel

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
    ensure_skills_symlink,
    remove_agent_tools,
    to_opencode_agent_id,
    translate_mcp_server_config,
    upsert_agent_tools,
    upsert_mcp_server,
)
from cli_agent_orchestrator.utils.opencode_permissions import cao_tools_to_opencode_permission
from cli_agent_orchestrator.utils.path_validation import (
    flatten_path_separators,
    validate_path_component,
)
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
    """Write the unresolved profile source to the shared context directory.

    The context copy's filename derives from the profile's RESOLVED frontmatter
    ``name:``. That value is NOT covered by ``_PROFILE_NAME_RE`` -- that regex
    validates the install *source handle* (the URL stem / bare-name argument),
    not the resolved name -- and a profile can be installed straight from a URL,
    so the field is attacker-controlled. Without a guard, a name like
    ``../../foo`` or an absolute path steers this write outside
    ``AGENT_CONTEXT_DIR`` and can overwrite a trusted ``.md`` instruction file.

    Three layers, all in this function (see the barrier note below):

    1. ``validate_path_component`` -- the shared segment validator, which rejects
       empty, ``.``/``..``, NUL, every path separator, and anything outside
       ``[A-Za-z0-9._-]``. The allowlist also makes Unicode normalization a
       non-issue: a fullwidth solidus (U+FF0F) is rejected outright rather than
       having to be caught before it folds to ``/`` under NFKC.
    2. Lexical containment under the realpath of the base directory.
    3. ``O_NOFOLLOW`` at the open, so the kernel refuses to write *through* a
       symlink at the final component.
    """
    AGENT_CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    # BARRIER PLACEMENT: the validation and the containment check are inlined
    # here, in the same function as the os.open() sink, rather than factored into
    # a helper. This mirrors the deliberate repetition in
    # ``services/profile_store`` -- CodeQL's py/path-injection dataflow only
    # recognises a barrier that guards, in the same function as the sink, the
    # very variable that reaches it. A helper that returns a validated path is
    # more readable but invisible to the analysis, and this repo has a history of
    # that alert reopening (see profile_store._PROFILE_NAME_RE). Load-bearing,
    # not an oversight.
    safe_name = validate_path_component(agent_name, description="profile name")
    # Resolve only the BASE (so a symlinked context root is handled) and keep the
    # final component UNRESOLVED. Resolving the whole candidate -- as
    # ``safe_join_under_base`` does -- would follow a symlink planted at the
    # target and silently write to wherever it resolves; leaving the final
    # component lexical means such a symlink is refused by O_NOFOLLOW below.
    # That is why this does not simply call ``safe_join_under_base``.
    base = os.path.realpath(AGENT_CONTEXT_DIR)
    candidate = os.path.join(base, f"{safe_name}.md")
    if candidate != base and not candidate.startswith(base + os.sep):
        raise ValueError(
            f"Refusing to write context copy: profile name {agent_name!r} resolves "
            f"to a path outside the agent context directory ({candidate!r})."
        )
    context_file = Path(candidate)
    # O_NOFOLLOW so the kernel itself refuses to write THROUGH a symlink at the
    # final component: a plain ``write_text``/``open`` follows a symlink, so even
    # after the containment check above, a symlink planted at the target
    # (pre-existing, or swapped in via a check-then-write race) would let the
    # write land outside the directory. O_TRUNC (not O_EXCL) so a normal
    # reinstall still overwrites the profile's own regular-file copy. ELOOP on a
    # symlink target becomes a clear refusal rather than an opaque OS error.
    #
    # Mode 0o600: this lives under ~/.aws/cli-agent-orchestrator/ and holds agent
    # instruction content, so it does not need to be group/world readable.
    #
    # PLATFORM NOTE: os.O_NOFOLLOW does not exist on Windows, so getattr(...) is 0
    # there and the kernel-level symlink refusal degrades to a no-op. The name
    # validation and containment check above still hold on Windows; only the
    # write-time symlink/race guard is POSIX-only. Acceptable because the primary
    # deployment target is POSIX and the validation already blocks the traversal
    # vectors; flagged so it is a conscious limitation, not a silent gap.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(context_file, flags, 0o600)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO):
            raise ValueError(
                f"Refusing to write context copy: {context_file} exists and is not a "
                "regular file (symlink, directory, or device). Remove it and reinstall."
            ) from exc
        raise
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(raw_content)
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
        if profile.mcpServers:
            profile.mcpServers = {
                name: resolve_mcp_server_config(dict(cfg), persisted=True)
                for name, cfg in profile.mcpServers.items()
            }

        unresolved_vars = sorted(set(re.findall(r"\$\{(\w+)\}", resolved_content)))
        context_file = _write_context_file(profile.name, raw_content)

        mcp_server_names = list(profile.mcpServers.keys()) if profile.mcpServers else None
        allowed_tools = resolve_allowed_tools(profile.allowedTools, profile.role, mcp_server_names)

        agent_file: Optional[Path] = None
        # Defence in depth. The resolved profile name is attacker-controlled, but
        # _write_context_file above has already REJECTED any name carrying a path
        # separator, so nothing separator-bearing reaches these provider sinks in
        # the normal flow. The flatten stays so each sink is independently safe if
        # the order ever changes or a new caller appears.
        safe_filename = flatten_path_separators(profile.name)

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
            kiro_resources = [
                f"file://{context_file.absolute()}",
                f"skill://{SKILLS_DIR}/**/SKILL.md",
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

            # OpenCode uses a shared opencode.json for MCP declarations. Keep
            # top-level MCP entries default-denied, then re-enable them only
            # for the installed agent. A reinstall without MCP removes stale
            # per-agent grants.
            if profile.mcpServers:
                mcp_names = list(profile.mcpServers.keys())
                for mcp_name, mcp_cfg in profile.mcpServers.items():
                    opencode_mcp_cfg = translate_mcp_server_config(dict(mcp_cfg))
                    upsert_mcp_server(mcp_name, opencode_mcp_cfg)
                upsert_agent_tools(agent_id, mcp_names)
            else:
                remove_agent_tools(agent_id)

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
