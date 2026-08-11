"""Codex CLI provider implementation."""

import asyncio
import logging
import os
import re
import shlex
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider, ProviderPreflightBlocked
from cli_agent_orchestrator.services.settings_service import get_server_settings
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.mcp_resolution import resolve_mcp_server_config
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

logger = logging.getLogger(__name__)

# Regex patterns for Codex output analysis
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"
IDLE_PROMPT_PATTERN = r"(?:❯|›|codex>)"
# Number of lines from the bottom of capture to check for the idle prompt.
# With --no-alt-screen, codex output is inline (scrollback contains history),
# so we can't anchor to \Z. Instead, check the last few lines where the prompt
# and status bar appear.
IDLE_PROMPT_TAIL_LINES = 5
# The idle prompt character ❯ (U+276F) is rendered on-screen by capture-pane
# but is NOT written to the raw output stream captured by pipe-pane.  Instead,
# the TUI footer text "? for shortcuts" is reliably present whenever the TUI
# is active.  This is intentionally permissive — _has_idle_pattern() is a
# lightweight pre-check; the real status decision is made by get_status()
# which uses capture-pane (rendered screen).
# Match assistant response start: "assistant:/codex:/agent:" (label style from synthetic
# test fixtures) or "•" bullet point (real Codex interactive output format).
# [^\S\n]* matches horizontal whitespace only (not newlines) so the match anchors
# on the actual bullet line — using \s* would let the match start on a blank
# line above the bullet, breaking per-line tool-call filtering downstream.
ASSISTANT_PREFIX_PATTERN = r"^(?:(?:assistant|codex|agent)\s*:|[^\S\n]*•)"
# MCP tool call marker emitted by Codex when invoking a tool, e.g.
# "• Called cao-mcp-server.load_skill({...})". The body that follows
# (└ ... lines) is the tool's return value, not the model's reply.
# Used to skip these markers when locating the actual response start.
# The "<server>.<tool>(" shape (identifier.identifier followed by an open
# paren) is required so legitimate model bullets like "• Called attention
# to the bug" don't get filtered as tool calls.
MCP_TOOL_CALL_PATTERN = r"^[^\S\n]*•\s+Called\s+[\w-]+\.[\w-]+\("
# Match user input: "You ..." (label style) or "› text" (Codex interactive prompt).
# The "›[^\S\n]*\S" alternative requires a non-whitespace character on the same line
# to distinguish user input ("› what is your role?") from the empty idle prompt ("› ").
# [^\S\n] matches horizontal whitespace only (spaces/tabs), preventing the pattern
# from crossing newline boundaries into subsequent lines.
USER_PREFIX_PATTERN = r"^(?:You\b|›[^\S\n]*\S)"
# Strict idle prompt pattern for extraction: matches empty prompt lines only.
# Distinguishes "› " (idle) from "› user message" (user input with text).
IDLE_PROMPT_STRICT_PATTERN = r"^\s*(?:❯|›|codex>)\s*$"

PROCESSING_PATTERN = r"\b(thinking|working|running|executing|processing|analyzing)\b"
WAITING_PROMPT_PATTERN = r"^(?:Approve|Allow)\b.*\b(?:y/n|yes/no|yes|no)\b"
ERROR_PATTERN = r"^(?:Error:|ERROR:|Traceback \(most recent call last\):|panic:)"
# The managed bridge prefixes provider stderr so it cannot be confused with
# model-authored text. Authentication failures on that channel are terminal
# for the running provider process and must outrank old assistant history;
# otherwise periodic retries repaint the pane and look like useful activity.
FATAL_PROVIDER_DIAGNOSTIC_PATTERN = (
    r"^\[provider diagnostic\].*(?:\b401 Unauthorized\b|"
    r"\bauthentication token is expired\b|\bauth error code:\s*token_expired\b)"
)

# Codex TUI footer indicators (status bar below the idle prompt).
# Used to detect when the bottom lines contain TUI chrome rather than user input.
# v0.110 and earlier: "? for shortcuts" and "N% context left"
# v0.111+: "model · N% left · path" (PR #13202 restored draft footer hints)
# v0.136+: "model · path" (the "N% left" segment was removed)
# The "·\s+[~/]" alternative anchors on the path component of the footer,
# which is shared across v0.111 and v0.136 status bars.
TUI_FOOTER_PATTERN = r"(?:\?\s+for shortcuts|context left|\d+%\s+left|·\s+[~/])"
# Codex TUI progress spinner: "• Working (0s • esc to interrupt)",
# "• Thinking (2s ...)", "• Starting script creation (10s • esc to interrupt)".
# The prefix text varies but the "(Ns • esc to interrupt)" format is consistent.
# Appears inline with --no-alt-screen when the agent is actively processing.
# Must be checked before COMPLETED to avoid false positives (the solid/hollow
# progress bullet can resemble an assistant row and the TUI footer › matches
# the idle prompt).
TUI_PROGRESS_PATTERN = r"[•◦].*\(\d+s\s*•\s*esc to interrupt\)"

# Workspace trust/approval prompt shown when Codex opens a new directory.
# Two known variants:
#   v0.98+: "allow Codex to work in this folder"
#   v0.130+ (git worktree): "Do you trust the contents of this directory?"
# Both indicate the TUI is blocked waiting for user input.
TRUST_PROMPT_PATTERN = r"allow Codex to work in this folder"
TRUST_PROMPT_PATTERN_V2 = r"Do you trust the contents of this directory\?"
TRUST_PROMPT_FOOTER = r"Press enter to continue"
# Codex welcome banner indicating normal startup (no trust prompt)
CODEX_WELCOME_PATTERN = r"OpenAI Codex"


def _compute_tui_footer_cutoff(all_lines: list) -> int:
    """Compute the character position where the TUI footer area starts.

    Scans backward from the last line to find the TUI footer status bar
    (matches TUI_FOOTER_PATTERN), then continues upward to include any
    blank lines and the suggestion hint line (› with text) that appear
    above the status bar as part of the footer area.

    Returns the character position in the joined text (``'\\n'.join(all_lines)``)
    where the footer starts. Returns ``len('\\n'.join(all_lines))`` if no
    footer is found.
    """
    n = len(all_lines)
    footer_start_idx = n

    # Find the status bar line (last TUI_FOOTER_PATTERN match in the bottom area)
    for i in range(n - 1, max(n - IDLE_PROMPT_TAIL_LINES - 1, -1), -1):
        if re.search(TUI_FOOTER_PATTERN, all_lines[i]):
            footer_start_idx = i
            break

    if footer_start_idx == n:
        return len("\n".join(all_lines))

    # Scan upward from the status bar to include blank lines and the
    # suggestion hint (› with text) that are part of the TUI footer chrome.
    for j in range(footer_start_idx - 1, max(footer_start_idx - 4, -1), -1):
        line = all_lines[j]
        if not line.strip():
            footer_start_idx = j
        elif re.match(rf"\s*{IDLE_PROMPT_PATTERN}", line):
            footer_start_idx = j
            break
        else:
            break

    return len("\n".join(all_lines[:footer_start_idx]))


def _toml_scalar(value: Any) -> str:
    """Serialize a Python scalar to a TOML literal for a ``-c key=<value>`` override.

    Strings become quoted TOML basic strings (backslash, quote, tab, CR, and newline escaped so
    tmux ``send_keys`` keeps the launch command on one line); bools become
    ``true``/``false``; ints and floats are emitted bare. Non-scalar values (dict/list/None) raise ``TypeError`` so a misconfigured profile fails fast. ``bool`` is checked
    before ``int`` because ``bool`` is a subclass of ``int`` in Python, so the
    order here is load-bearing — a flipped order would render ``True`` as ``1``.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str):
        raise TypeError(
            "codexConfig values must be scalars (str, bool, int, or float); "
            f"got {type(value).__name__}"
        )
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\t", "\\t")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )
    return f'"{escaped}"'


# codexConfig keys are dotted CONFIG PATHS ("features.fast_mode") — dots are
# the path separator and intentional. MCP server names and env keys are single
# TOML BARE KEYS: a dot there would silently create a NESTED table
# (mcp_servers.my.srv.command → mcp_servers['my']['srv'], not
# mcp_servers['my.srv']), so codex would never find the server.
_CODEX_CONFIG_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_CODEX_BARE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_config_key(key: Any, *, source: str, allow_dots: bool = False) -> str:
    """Validate a key that is interpolated into a Codex ``-c`` override path.

    Spaces, ``=``, quotes, or control characters are rejected so a
    misconfigured profile fails fast with a clear error instead of silently
    emitting a malformed ``-c`` override (an unescaped quote or newline in the
    KEY half would corrupt the TOML the same way an unescaped value would).

    ``allow_dots=True`` permits dotted config paths (codexConfig keys like
    ``features.fast_mode``). MCP server names and env keys must be single
    TOML bare keys: a dot there would nest the entry under the wrong TOML
    table (see pattern comment above). ``source`` names the profile field
    for the error message.
    """
    if allow_dots:
        pattern = _CODEX_CONFIG_KEY_PATTERN
        expected = "a dotted config path over [A-Za-z0-9_.-] (e.g. 'features.fast_mode')"
    else:
        pattern = _CODEX_BARE_KEY_PATTERN
        expected = (
            "a single TOML bare key over [A-Za-z0-9_-] (no dots -- a dot "
            "would nest the entry under the wrong TOML table)"
        )
    # fullmatch, not match: with ``$`` alone, re.match accepts a TRAILING
    # newline ("srv\n" passes ^...$), which is exactly the bug class this
    # validation exists to close.
    if not isinstance(key, str) or not pattern.fullmatch(key):
        raise ValueError(f"Invalid {source} key {key!r}: must be {expected}")
    return key


def _toml_override(key: str, value: Any) -> str:
    """Build one ``key=<toml-scalar>`` Codex ``-c`` override, validating the key.

    Key validation is delegated to :func:`_validate_config_key`.
    Value-serialization failures from :func:`_toml_scalar` are re-raised with
    the offending key for context.
    """
    _validate_config_key(key, source="codexConfig", allow_dots=True)
    try:
        return f"{key}={_toml_scalar(value)}"
    except TypeError as exc:
        raise TypeError(f"codexConfig key '{key}': {exc}") from exc


def render_trusted_project_override(project_root: str) -> str:
    """Render the single-purpose invocation-only Codex trust override.

    This is intentionally not exposed through the general ``codexConfig``
    serializer: the absolute path is a quoted inline-table key, and treating it
    as a dotted path would silently target a different configuration node.
    """
    if not isinstance(project_root, str) or not os.path.isabs(project_root):
        raise ValueError("trusted_project_root must be an absolute path")
    if os.path.realpath(project_root) != project_root:
        raise ValueError("trusted_project_root must already be a canonical realpath")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in project_root):
        raise ValueError("trusted_project_root must not contain control characters")
    return f'projects={{{_toml_scalar(project_root)}={{trust_level="trusted"}}}}'


# ---------------------------------------------------------------------------
# The ONE Codex argument composer.
#
# The ordinary CodexProvider, the unmanaged pre-task bootstrap, and the
# managed-v2 adapter all consume ``compose_codex_core_args`` so the zero-turn
# bootstrap and the resumed TUI cannot drift apart on profile selection,
# developer instructions, MCP serialization, codexConfig, trust, or route
# ordering.  Each caller appends only its own suffixes: the bootstrap appends
# its pinned route and the app-server flags; the TUI appends its TUI flags,
# the observed/pinned route, and the exact resume id.
# ---------------------------------------------------------------------------

#: TUI-only flags.  ``--no-alt-screen`` keeps output in scrollback so tmux
#: capture-pane is reliable; ``--disable shell_snapshot`` avoids TTY input
#: conflicts in tmux.  Explicit (not buried in the core) because the app-server
#: bootstrap must NOT receive them.
CODEX_TUI_FLAGS = ["--no-alt-screen", "--disable", "shell_snapshot"]

#: App-server-only flags.  The resumed TUI must NOT receive them.
CODEX_APP_SERVER_FLAGS = ["app-server", "--stdio"]

#: Default MCP tool timeout (seconds) as a TOML float.  Codex deserializes
#: ``tool_timeout_sec`` via ``Option<f64>``, so an integer is silently rejected
#: and falls back to the 60s default.
CODEX_DEFAULT_MCP_TOOL_TIMEOUT_SEC = 600.0


@dataclass(frozen=True)
class CodexRoute:
    """The route a Codex launch pins: model and reasoning effort.

    Both fields optional.  An empty/None ``model`` is the provider-default
    (omitted from ``thread/start`` and the argv — the bootstrap lets Codex
    pick, and records the actual).  An empty/None ``effort`` is omitted —
    effort stays a config/argv selection and the bootstrap records null when
    the provider reports none.  Never invent a ``provider-default`` or
    empty-string route.
    """

    model: Optional[str] = None
    effort: Optional[str] = None


def _codex_mcp_args(mcp_servers: Optional[list]) -> list[str]:
    """Serialize resolved MCP server material into Codex ``-c`` overrides.

    ``mcp_servers`` is the resolved structure produced once from the loaded
    profile (by :func:`resolve_codex_mcp_material_entry`): a list of dicts,
    each carrying exactly one transport.  A command/stdio entry has ``name``,
    ``command``, ``args``, ``env`` (list of ``{name, value}``), ``env_vars``
    (list of strings), and ``tool_timeout_sec`` (number or None).  A
    URL/streamable-HTTP entry has ``name``, ``url``, and an optional
    ``bearer_token_env_var`` — no subprocess surface at all, so no
    command/args/env/env_vars and no ``CAO_TERMINAL_ID`` injection.

    One implementation of MCP serialization/timeouts so the bootstrap and TUI
    agree, and so the same validation closes the same traps on every path: the
    server name / env key are TOML bare keys (a dot would nest under the wrong
    table), env_vars are strings, the timeout is a positive number, and an
    entry with no usable transport is refused rather than silently skipped.
    ``type: http`` is profile-side information and is never serialized — Codex
    selects the HTTP transport from ``url`` itself.
    """
    args: list[str] = []
    for server in mcp_servers or []:
        name = _validate_config_key(server["name"], source="mcpServers name")
        prefix = f"mcp_servers.{name}"
        if "url" in server:
            # URL/streamable-HTTP transport: the URL plus an optional bearer
            # token env var name.  No command/args/env/env_vars keys, and no
            # CAO_TERMINAL_ID injection into a subprocess that does not exist.
            url = server.get("url")
            if not isinstance(url, str) or not url:
                raise ValueError(f"mcpServers {name!r} url must be a non-empty string, got {url!r}")
            args.extend(["-c", f"{prefix}.url={_toml_scalar(url)}"])
            token = server.get("bearer_token_env_var")
            if token is not None:
                if not isinstance(token, str) or not token:
                    raise ValueError(
                        f"mcpServers {name!r} bearer_token_env_var must be a non-empty "
                        f"string, got {token!r}"
                    )
                args.extend(["-c", f"{prefix}.bearer_token_env_var={_toml_scalar(token)}"])
            continue
        command = server.get("command")
        if not isinstance(command, str) or not command:
            raise ValueError(
                f"mcpServers {name!r} must configure exactly one usable transport "
                f"(a non-empty command or a non-empty url); got neither"
            )
        args.extend(["-c", f"{prefix}.command={_toml_scalar(command)}"])
        server_args = "[" + ", ".join(_toml_scalar(a) for a in (server.get("args") or [])) + "]"
        args.extend(["-c", f"{prefix}.args={server_args}"])
        for item in server.get("env") or []:
            key = _validate_config_key(item["name"], source="mcpServers env")
            args.extend(["-c", f"{prefix}.env.{key}={_toml_scalar(str(item['value']))}"])
        env_vars = list(server.get("env_vars") or [])
        # Forward CAO_TERMINAL_ID so MCP servers (e.g. cao-mcp-server) can
        # identify the current session; Codex does not forward env to MCP
        # subprocesses by default.
        if "CAO_TERMINAL_ID" not in env_vars:
            env_vars.append("CAO_TERMINAL_ID")
        for index, value in enumerate(env_vars):
            if not isinstance(value, str):
                raise ValueError(
                    f"mcpServers {name!r} env_vars[{index}] must be a string, "
                    f"got {type(value).__name__}"
                )
        env_vars_toml = "[" + ", ".join(_toml_scalar(v) for v in env_vars) + "]"
        timeout = server.get("tool_timeout_sec")
        if timeout is None:
            timeout = CODEX_DEFAULT_MCP_TOOL_TIMEOUT_SEC
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError(f"mcpServers {name!r} tool_timeout_sec must be a positive number")
        args.extend(
            [
                "-c",
                f"{prefix}.env_vars={env_vars_toml}",
                "-c",
                f"{prefix}.tool_timeout_sec={_toml_scalar(float(timeout))}",
            ]
        )
    return args


def resolve_codex_mcp_material_entry(
    *, name: str, config: Mapping[str, Any], terminal_id: str
) -> dict[str, Any]:
    """The ONE resolved Codex MCP material entry, from a profile config.

    Both the managed-v2 material builder (``_profile_material_from_profile``)
    and the ordinary provider fallback (``CodexProvider._resolve_codex_profile_material``)
    consume this so bootstrap/TUI/managed-v2 can never build different shapes.

    An entry carries exactly one usable transport, never invented: a
    command/stdio server (non-empty ``command``) or a URL/streamable-HTTP
    server (non-empty ``url``).  An entry with both, with neither, or with an
    empty-string transport is refused with a typed ``ValueError`` (the same
    typed boundary every composer consumer already maps).  ``type: http`` is
    profile-side information and is not carried into the material: Codex
    selects the HTTP transport from the ``url`` key itself.

    Command entries keep the established shape byte-for-byte (args, sorted
    env with the ``CAO_TERMINAL_ID`` default, env_vars, tool timeout).  URL
    entries carry only ``url`` and an optional non-empty
    ``bearer_token_env_var`` — there is no subprocess to receive env or a
    timeout, and ``CAO_TERMINAL_ID`` is never injected into one.
    """
    command = config.get("command")
    url = config.get("url")
    usable_command = isinstance(command, str) and bool(command)
    usable_url = isinstance(url, str) and bool(url)
    if usable_command == usable_url:
        raise ValueError(
            f"mcpServers {name!r} must configure exactly one usable transport: "
            f"got command={command!r} and url={url!r}"
        )
    if usable_url:
        token = config.get("bearer_token_env_var")
        if token is not None and (not isinstance(token, str) or not token):
            raise ValueError(
                f"mcpServers {name!r} bearer_token_env_var must be a non-empty string, "
                f"got {token!r}"
            )
        entry: dict[str, Any] = {"name": name, "url": url}
        if token is not None:
            entry["bearer_token_env_var"] = token
        return entry
    env = {str(key): str(item) for key, item in (config.get("env") or {}).items()}
    env.setdefault("CAO_TERMINAL_ID", terminal_id)
    return {
        "name": name,
        "command": command,
        "args": [str(item) for item in (config.get("args") or [])],
        "env": [{"name": key, "value": value} for key, value in sorted(env.items())],
        # env_vars are NAMES of vars to forward — pass them through
        # verbatim so the shared Codex composer is the single fail-fast
        # validator (a non-string entry is a malformed profile).
        "env_vars": list(config.get("env_vars") or []),
        "tool_timeout_sec": config.get("tool_timeout_sec"),
    }


def compose_codex_core_args(
    *,
    codex_profile: Optional[str],
    codex_config: Optional[dict],
    system_prompt: str,
    mcp_servers: Optional[list],
    allowed_tools: Optional[list[str]],
    trusted_project_root: Optional[str],
) -> list[str]:
    """The shared core Codex argv, in the one canonical order.

    Both the zero-turn bootstrap and the resumed TUI consume these EXACT args:
    profile/yolo selection, the fully-composed developer instructions, the
    resolved MCP servers, the profile's ``codexConfig`` overrides, and the
    canonical trust override.  Each caller then appends only its own route,
    TUI/app-server, and resume suffixes.

    ``system_prompt`` is the ALREADY-COMPOSED developer instruction (base body
    + runtime skill catalog + the shared restricted-tool security prompt and
    explicit tool list); the composer emits it verbatim so the bootstrap and
    TUI cannot rebuild subtly different contracts from a reloaded profile.
    """
    yolo = bool(allowed_tools and "*" in allowed_tools)
    args: list[str] = []
    if codex_profile and not yolo:
        args.extend(["--profile", codex_profile])
    else:
        args.extend(["--yolo"])
    if system_prompt:
        args.extend(["-c", f"developer_instructions={_toml_scalar(system_prompt)}"])
    args.extend(_codex_mcp_args(mcp_servers))
    for key, value in (codex_config or {}).items():
        args.extend(["-c", _toml_override(key, value)])
    if trusted_project_root:
        args.extend(["-c", render_trusted_project_override(trusted_project_root)])
    return args


def codex_route_suffix(route: Optional[CodexRoute]) -> list[str]:
    """The last-wins route override, emitted AFTER the core args.

    Appended after the core (and therefore after any ``codexConfig`` knob or
    named profile) so neither can silently select a different route.  An empty
    model/effort is omitted — the ordinary path's provider-default route
    emits nothing here, and the bootstrap records the actual model/effort the
    provider returned.  Never emits an empty-string or invented route.
    """
    if route is None:
        return []
    args: list[str] = []
    if route.model:
        args.extend(["--model", route.model])
    if route.effort:
        args.extend(["-c", _toml_override("model_reasoning_effort", route.effort)])
    return args


def _find_assistant_marker(text: str) -> Optional[re.Match[str]]:
    """Find the first ASSISTANT_PREFIX_PATTERN match in ``text`` whose line
    is not an MCP tool-call marker.

    Codex emits ``• Called <server>.<tool>(...)`` when invoking an MCP tool;
    that bullet matches ASSISTANT_PREFIX_PATTERN but is followed by tool
    output, not the model's reply. Anchoring on it would conflate tool
    output with the model response (status: false COMPLETED;
    extraction: skill-body leak).
    """
    for m in re.finditer(ASSISTANT_PREFIX_PATTERN, text, re.IGNORECASE | re.MULTILINE):
        line_end = text.find("\n", m.start())
        if line_end == -1:
            line_end = len(text)
        line = text[m.start() : line_end]
        if re.match(MCP_TOOL_CALL_PATTERN, line):
            continue
        return m
    return None


class ProviderError(Exception):
    """Exception raised for provider-specific errors."""

    pass


class CodexProvider(BaseProvider):
    """Provider for Codex CLI tool integration."""

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list] = None,
        skill_prompt: Optional[str] = None,
        trusted_project_root: Optional[str] = None,
        expected_model: Optional[str] = None,
        expected_effort: Optional[str] = None,
        native_session_id: Optional[str] = None,
        codex_profile_material: Optional[dict] = None,
        codex_executable: Optional[str] = None,
    ):
        """Initialize provider state."""
        super().__init__(terminal_id, session_name, window_name, allowed_tools, skill_prompt)
        self._initialized = False
        # The pre-task bootstrap-minted thread id the launch argv must resume
        # (``codex ... resume <id>``); None keeps the legacy ambient launch.
        self._native_session_id = native_session_id
        self._agent_profile = agent_profile
        self._trusted_project_root = trusted_project_root
        self._expected_model = expected_model
        self._expected_effort = expected_effort
        # The EXACT profile material create_terminal resolved once
        # (developer instructions, MCP servers, tool policy). When supplied,
        # the resumed TUI consumes the same core args the zero-turn bootstrap
        # used and never reloads a potentially-changed profile.  None falls
        # back to loading ``agent_profile`` for direct/unit construction.
        self._codex_profile_material = codex_profile_material
        # The EXACT digest-verified executable the pre-task bootstrap proved.
        # The resumed TUI launches this absolute path and never re-resolves a
        # bare ``codex`` through the pane's ambient PATH (an existing tmux
        # session can inherit a different PATH and resolve another build).
        # None keeps the legacy bare-name launch for direct/unit construction
        # that never ran a bootstrap.
        self._codex_executable = codex_executable

    def _resolve_codex_profile_material(self) -> dict:
        """The fully-composed Codex profile material the launch argv consumes.

        When ``create_terminal`` resolved the material once, it
        passes that EXACT material through the provider constructor so the
        resumed TUI consumes the same developer instructions, MCP servers, and
        tool policy the zero-turn bootstrap used — never a reloaded profile or
        a subtly different contract.  The fallback composes from the loaded
        profile plus this provider's constructor ``allowed_tools`` /
        ``skill_prompt`` (the same composition the resumed TUI always
        applied), so direct/unit construction without the pre-task seam still
        routes through the ONE shared composer.
        """
        if self._codex_profile_material is not None:
            return self._codex_profile_material
        if self._agent_profile is None:
            return {
                "profile": None,
                "allowed_tools": self._allowed_tools,
                "system_prompt": self._apply_skill_prompt(""),
                "mcp_servers": [],
            }
        try:
            profile = load_agent_profile(self._agent_profile)
        except Exception as e:
            raise ProviderError(f"Failed to load agent profile '{self._agent_profile}': {e}")
        # Compose the developer instructions exactly as the resumed TUI always
        # has: the profile body, the runtime skill catalog supplied to this
        # provider, then the restricted-tool security prompt.
        system_prompt = profile.system_prompt if profile.system_prompt is not None else ""
        system_prompt = self._apply_skill_prompt(system_prompt)
        if self._allowed_tools and "*" not in self._allowed_tools:
            from cli_agent_orchestrator.constants import SECURITY_PROMPT

            tools_list = ", ".join(self._allowed_tools)
            tool_constraint = f"\nYou only have access to these tools: {tools_list}\n"
            system_prompt = SECURITY_PROMPT + tool_constraint + system_prompt
        mcp_servers: list = []
        for server_name, server_config in (profile.mcpServers or {}).items():
            cfg = (
                dict(server_config)
                if isinstance(server_config, dict)
                else server_config.model_dump(exclude_none=True)
            )
            cfg = resolve_mcp_server_config(cfg)
            # The ONE Codex material shape, identical to the managed-v2
            # builder: exactly one usable transport per entry
            # (command/stdio or url/streamable-HTTP), validated typed and
            # fail-closed.
            mcp_servers.append(
                resolve_codex_mcp_material_entry(
                    name=server_name,
                    config=cfg,
                    terminal_id=self.terminal_id,
                )
            )
        return {
            "profile": profile,
            "allowed_tools": self._allowed_tools,
            "system_prompt": system_prompt,
            "mcp_servers": mcp_servers,
        }

    def _build_codex_command(self) -> str:
        """Build the Codex launch command via the ONE shared argument composer.

        The resumed TUI consumes the same precomposed core args
        the zero-turn bootstrap used (profile/yolo selection, developer
        instructions, MCP servers, codexConfig, canonical trust), then adds
        only its TUI flags, the observed/pinned route, and the exact resume id.
        """
        material = self._resolve_codex_profile_material()
        profile = material.get("profile")
        core = compose_codex_core_args(
            codex_profile=getattr(profile, "codexProfile", None),
            codex_config=getattr(profile, "codexConfig", None),
            system_prompt=material.get("system_prompt") or "",
            mcp_servers=material.get("mcp_servers") or [],
            allowed_tools=material.get("allowed_tools") or self._allowed_tools,
            trusted_project_root=self._trusted_project_root,
        )
        # TUI flags sit right after the yolo/profile choice (one arg for
        # ``--yolo``, two for ``--profile <name>``); the route is appended
        # last (last-wins) and the resume id is the final positional.
        choice_len = 2 if core and core[0] == "--profile" else 1
        executable = self._codex_executable or "codex"
        command_parts = [executable, *core[:choice_len], *CODEX_TUI_FLAGS, *core[choice_len:]]
        # The route: a caller-sealed expected model/effort wins; otherwise
        # the profile's own route — ``codexConfig.model`` override over the
        # bare ``profile.model`` field, and the codexConfig effort — the
        # same effective route the pre-task bootstrap pinned.  Either may
        # be empty.
        codex_config = getattr(profile, "codexConfig", None)
        config = codex_config if isinstance(codex_config, dict) else {}
        effort_cfg = config.get("model_reasoning_effort")
        config_model = config.get("model")
        route = CodexRoute(
            model=self._expected_model
            or (config_model if isinstance(config_model, str) else "")
            or (getattr(profile, "model", None) or ""),
            effort=self._expected_effort or str(effort_cfg or ""),
        )
        command_parts.extend(codex_route_suffix(route))
        # The pre-task minted id is resumed exactly (``codex ... resume
        # <id>``) — the TUI never silently creates an unrelated fresh
        # conversation.
        if self._native_session_id:
            command_parts.extend(["resume", self._native_session_id])

        return shlex.join(command_parts)

    async def _handle_trust_prompt(self, timeout: float = 20.0) -> None:
        """Auto-accept the workspace trust prompt if it appears.

        Codex shows a folder approval dialog when opening a new directory.
        This sends Enter to accept the default option (allow Codex to work).
        CAO assumes the user trusts the working directory since they confirmed
        workspace access during the launch command.

        Two known dialog variants:
          v0.98+: "allow Codex to work in this folder"
          v0.130+ (git worktree): "Do you trust the contents of this directory?"
        """
        start_time = time.time()
        while time.time() - start_time < timeout:
            output = get_backend().get_history(self.session_name, self.window_name)
            if not output:
                await asyncio.sleep(1.0)
                continue

            # Clean ANSI codes for reliable text matching
            clean_output = re.sub(ANSI_CODE_PATTERN, "", output)

            if re.search(TRUST_PROMPT_PATTERN, clean_output):
                if self._trusted_project_root is not None:
                    raise ProviderPreflightBlocked(
                        "repository-trust",
                        "Codex displayed a repository-trust prompt despite invocation-only "
                        "trust pre-authorization; refusing all input",
                    )
                from cli_agent_orchestrator.services.status_monitor import status_monitor

                logger.info("Codex workspace trust prompt (v1) detected, auto-accepting")
                status_monitor.notify_input_sent(self.terminal_id)
                get_backend().send_special_key(self.session_name, self.window_name, "Enter")
                return

            if re.search(TRUST_PROMPT_PATTERN_V2, clean_output) and re.search(
                TRUST_PROMPT_FOOTER, clean_output
            ):
                if self._trusted_project_root is not None:
                    raise ProviderPreflightBlocked(
                        "repository-trust",
                        "Codex displayed a repository-trust prompt despite invocation-only "
                        "trust pre-authorization; refusing all input",
                    )
                from cli_agent_orchestrator.services.status_monitor import status_monitor

                logger.info("Codex workspace trust prompt (v2) detected, auto-accepting")
                status_monitor.notify_input_sent(self.terminal_id)
                get_backend().send_special_key(self.session_name, self.window_name, "Enter")
                return

            # Check if Codex has fully started (welcome banner visible)
            if re.search(CODEX_WELCOME_PATTERN, clean_output):
                logger.info("Codex started without trust prompt")
                return

            await asyncio.sleep(1.0)

        pane_tail = ""
        try:
            output = get_backend().get_history(self.session_name, self.window_name)
            if output:
                pane_tail = "\n".join(output.splitlines()[-10:])
        except Exception:
            pass
        logger.error(
            "Codex trust prompt handler timed out — no trust dialog or welcome banner detected. "
            "Pane tail:\n%s",
            pane_tail,
        )

    async def initialize(self) -> bool:
        """Initialize Codex provider by starting codex command."""
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        init_timeout = get_server_settings()["provider_init_timeout"]
        if not await wait_for_shell(self.terminal_id, timeout=init_timeout):
            raise TimeoutError(f"Shell initialization timed out after {init_timeout}s")

        # Capture the shell process name before launching codex — used later to
        # detect when codex has exited and the pane is back to a bare shell.
        self.shell_baseline = get_backend().get_pane_current_command(
            self.session_name, self.window_name
        )

        # Send a warm-up command before launching codex.
        # Codex exits immediately in freshly-created tmux sessions where the shell
        # has not yet processed a full interactive command cycle.
        # Arm the StatusMonitor stickiness gate: each send_keys here represents
        # external input that must be allowed to drive PROCESSING transitions
        # past any previously-latched ready state.
        status_monitor.notify_input_sent(self.terminal_id)
        get_backend().send_keys(self.session_name, self.window_name, "echo ready")
        await asyncio.sleep(2.0)

        # Build command with flags and agent profile (developer_instructions).
        # --no-alt-screen: run in inline mode so output stays in normal scrollback,
        #   making tmux capture-pane reliable.
        # --disable shell_snapshot: avoid TTY input conflicts (SIGTTIN) in tmux
        #   caused by the shell_snapshot subprocess inheriting stdin.
        command = self._build_codex_command()
        status_monitor.notify_input_sent(self.terminal_id)
        get_backend().send_keys(self.session_name, self.window_name, command)

        # Handle workspace trust prompt if it appears (new/untrusted directories)
        await self._handle_trust_prompt(timeout=20.0)

        if not await wait_until_status(
            self.terminal_id,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=float(get_server_settings()["provider_init_timeout"]),
            polling_interval=1.0,
        ):
            raise TimeoutError("Codex initialization timed out after 60 seconds")

        self._initialized = True
        return True

    # Codex 0.146.0 redraws its inline TUI in place and leaves completed
    # progress rows in the raw pipe-pane buffer. Opt into the composited
    # viewport path so status/inbox callers use the spatial detector below.
    supports_screen_detection = True

    def get_status_from_screen(self, screen_lines: list[str]) -> TerminalStatus:
        """Detect live 0.146.0 activity from a rendered viewport.

        Inline Codex keeps completed progress rows in scrollback.  The raw
        detector deliberately scans history, which is useful for response
        extraction but makes a stale ``Starting MCP servers (... esc to
        interrupt)`` row look like current work forever.  In the rendered TUI,
        a current progress row is adjacent to the live (last) composer prompt.
        Require that spatial relationship before returning ``PROCESSING``;
        otherwise preserve the generic detector's waiting/error/completed
        answers and treat its history-only processing result as idle.
        """
        rows = [strip_terminal_escapes(row).rstrip() for row in screen_lines]
        if not any(row.strip() for row in rows):
            return TerminalStatus.UNKNOWN
        prompt_index = next(
            (
                index
                for index in range(len(rows) - 1, -1, -1)
                if re.match(r"^\s*(?:❯|›|codex>)(?:\s|$)", rows[index])
            ),
            None,
        )
        if prompt_index is None:
            return self.get_status("\n".join(rows))
        current_region = "\n".join(rows[max(0, prompt_index - 6) : prompt_index])
        if re.search(TUI_PROGRESS_PATTERN, current_region, re.MULTILINE):
            return TerminalStatus.PROCESSING
        status = self.get_status("\n".join(rows))
        return TerminalStatus.IDLE if status is TerminalStatus.PROCESSING else status

    def get_status(self, output: str) -> TerminalStatus:
        # Native status (herdr): trust the backend's agent state when available;
        # on herdr the buffer is never fed, so buffer parsing can't leave UNKNOWN.
        native = self._resolve_native_status(output)
        if native is not None:
            return native

        # herdr never pushes a buffer (pipe_pane is a no-op there); read live
        # pane content instead of falling through to "no output" on every call.
        output = self._resolve_buffer(output)
        if not output:
            return TerminalStatus.UNKNOWN

        # Detect when the codex process has exited and the pane is back to a
        # bare shell. The pane's current command will revert to the shell
        # (e.g. "zsh") that was running before we launched codex. Returning
        # ERROR prevents the inbox service from typing a queued message into
        # the shell — which would execute it as arbitrary commands.
        if self._initialized and self.shell_baseline:
            current_cmd = get_backend().get_pane_current_command(
                self.session_name, self.window_name
            )
            if current_cmd == self.shell_baseline:
                return TerminalStatus.ERROR

        # Strip the RAW pipe-pane escapes (cursor positioning, in-place redraws),
        # not just SGR colour codes — otherwise cursor sequences survive and the
        # idle ``›`` prompt / structural checks below misfire on the raw stream.
        clean_output = strip_terminal_escapes(output)
        tail_output = "\n".join(clean_output.splitlines()[-25:])

        last_nonempty_line = next(
            (line for line in reversed(clean_output.splitlines()) if line.strip()),
            "",
        )
        if re.search(
            FATAL_PROVIDER_DIAGNOSTIC_PATTERN,
            last_nonempty_line,
            re.IGNORECASE,
        ):
            return TerminalStatus.ERROR

        # Search for user messages, excluding the Codex TUI footer when present.
        # The TUI footer (idle prompt hint like "› Summarize recent commits" +
        # status bar "? for shortcuts / context left") can contain › followed by
        # suggestion text, which USER_PREFIX_PATTERN would incorrectly match as
        # user input, preventing COMPLETED detection.
        # Only apply the cutoff when TUI footer indicators are actually present
        # to avoid over-excluding in short outputs or test fixtures.
        all_lines = clean_output.splitlines()
        tui_footer_detected = any(
            re.search(TUI_FOOTER_PATTERN, line) for line in all_lines[-IDLE_PROMPT_TAIL_LINES:]
        )
        if tui_footer_detected:
            cutoff_pos = _compute_tui_footer_cutoff(all_lines)
        else:
            cutoff_pos = len(clean_output)

        last_user = None
        for match in re.finditer(USER_PREFIX_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE):
            if match.start() < cutoff_pos:
                last_user = match

        output_after_last_user = clean_output[last_user.start() :] if last_user else clean_output
        # Skip MCP tool-call markers — those mark "model invoked a tool", not
        # "model has replied", and shouldn't gate WAITING/ERROR detection.
        assistant_after_last_user = bool(
            last_user and _find_assistant_marker(output_after_last_user) is not None
        )

        # Check trust prompt early — the trust menu uses › which matches the idle prompt
        # pattern, and PROCESSING_PATTERN matches "running" in "You are running Codex in..."
        if re.search(TRUST_PROMPT_PATTERN, clean_output):
            return TerminalStatus.WAITING_USER_ANSWER

        # V2 trust dialog ("Do you trust the contents of this directory?" / "Press enter
        # to continue"). Only classify as WAITING when BOTH the question AND the footer
        # appear in the bottom region — avoids false positives if the question text
        # appears in scrollback from a previous model response.
        bottom_region = "\n".join(clean_output.splitlines()[-15:])
        if re.search(TRUST_PROMPT_PATTERN_V2, bottom_region) and re.search(
            TRUST_PROMPT_FOOTER, bottom_region
        ):
            return TerminalStatus.WAITING_USER_ANSWER

        # Check bottom of captured output for idle prompt.
        # With --no-alt-screen, scrollback contains history so we can't anchor
        # to end-of-string. Instead, check only the last few lines.
        bottom_lines = clean_output.strip().splitlines()[-IDLE_PROMPT_TAIL_LINES:]
        has_idle_prompt_at_end = any(
            re.match(rf"\s*{IDLE_PROMPT_PATTERN}", line, re.IGNORECASE) for line in bottom_lines
        )

        # Only treat ERROR/WAITING prompts as actionable if they appear after the last user message
        # and are not part of an assistant response.
        if last_user is not None:
            if not assistant_after_last_user:
                if re.search(
                    WAITING_PROMPT_PATTERN,
                    output_after_last_user,
                    re.IGNORECASE | re.MULTILINE,
                ):
                    return TerminalStatus.WAITING_USER_ANSWER
                if re.search(
                    ERROR_PATTERN,
                    output_after_last_user,
                    re.IGNORECASE | re.MULTILINE,
                ):
                    return TerminalStatus.ERROR
        else:
            if re.search(WAITING_PROMPT_PATTERN, tail_output, re.IGNORECASE | re.MULTILINE):
                return TerminalStatus.WAITING_USER_ANSWER
            if re.search(ERROR_PATTERN, tail_output, re.IGNORECASE | re.MULTILINE):
                return TerminalStatus.ERROR
        if has_idle_prompt_at_end:
            # Check for TUI progress indicator ("• Working (0s • esc to interrupt)").
            # With --no-alt-screen, the TUI footer (› hint + status bar) is always
            # rendered at the bottom, even during processing. The • in the progress
            # spinner matches ASSISTANT_PREFIX_PATTERN, causing a false COMPLETED.
            # Detect the spinner and return PROCESSING before checking for COMPLETED.
            if re.search(TUI_PROGRESS_PATTERN, tail_output, re.MULTILINE):
                return TerminalStatus.PROCESSING

            # Consider COMPLETED only if we see an assistant marker (skipping
            # MCP tool-call markers) after the last user message. Without the
            # tool-call filter, "• Called <server>.<tool>(...)" emitted before
            # the model has actually replied would trip COMPLETED prematurely.
            if last_user is not None:
                if _find_assistant_marker(clean_output[last_user.start() :]) is not None:
                    return TerminalStatus.COMPLETED

                return TerminalStatus.IDLE

            # No user-message marker in the cleaned buffer. Two cases:
            # - Fresh init: no assistant content either → IDLE.
            # - Long-running response: the › user marker has been evicted from
            #   the 8KB rolling buffer by the time the response settles, but an
            #   assistant bullet is still visible. Without this branch we'd
            #   return IDLE forever and ``wait_for_status(completed)`` in the
            #   e2e tests would time out.
            # Search above the TUI footer cutoff so the › suggestion-hint and
            # status-bar lines aren't confused with a model reply.
            if _find_assistant_marker(clean_output[:cutoff_pos]) is not None:
                return TerminalStatus.COMPLETED
            return TerminalStatus.IDLE

        # If we're not at an idle prompt and we don't see explicit errors/permission prompts,
        # assume the CLI is still producing output.
        return TerminalStatus.PROCESSING

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract Codex's final response from terminal output.

        Supports two output formats:
        - Label style: "You ...\\nassistant: response\\n❯" (synthetic/test format)
        - Bullet style: "› user message\\n• response\\n›" (real Codex interactive mode)

        Primary approach: find the last user message and extract everything between
        the end of that line and the next empty idle prompt.
        Fallback: use assistant marker based extraction when no user message is found.
        """
        # Strip ALL terminal escape sequences, not just SGR colour codes. The
        # narrow ANSI_CODE_PATTERN (``\x1b[...m``) leaves cursor-movement (H),
        # erase (K), and scroll CSI sequences in place; codex's TUI emits those
        # heavily, so an SGR-only strip returned raw escape garbage
        # (``[49;2H[K[38;2;...m``) as the "response", failing extraction. Use
        # the shared strip which also normalises \r and column-1 cursor moves to
        # newlines — this is fed a tmux capture-pane render (already laid out),
        # so the line-based extraction below still anchors correctly.
        clean_output = strip_terminal_escapes(script_output)

        # Primary: find last user message, extract response between it and idle prompt.
        # Exclude the Codex TUI footer from user-message matching when detected.
        all_lines = clean_output.splitlines()
        tui_footer_detected = any(
            re.search(TUI_FOOTER_PATTERN, line) for line in all_lines[-IDLE_PROMPT_TAIL_LINES:]
        )
        if tui_footer_detected:
            cutoff_pos = _compute_tui_footer_cutoff(all_lines)
        else:
            cutoff_pos = len(clean_output)

        user_matches = [
            m
            for m in re.finditer(USER_PREFIX_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE)
            if m.start() < cutoff_pos
        ]

        if user_matches:
            last_user = user_matches[-1]

            # Find the first assistant response marker (• or assistant:) after
            # the user message, skipping "• Called <server>.<tool>(...)" MCP
            # tool call markers — those are followed by tool output, not the
            # model's reply. Anchoring on a tool call marker would pull tool
            # output (e.g. skill body text) into the extracted response.
            asst_after_user = _find_assistant_marker(clean_output[last_user.start() :])

            if asst_after_user:
                response_start = last_user.start() + asst_after_user.start()
            else:
                # No assistant marker found; fall back to skipping one line
                user_line_end = clean_output.find("\n", last_user.start())
                if user_line_end == -1:
                    user_line_end = len(clean_output)
                response_start = user_line_end + 1

            # Find extraction boundary: empty idle prompt or TUI footer area.
            # With --no-alt-screen, the TUI footer (› hint + status bar) has no
            # empty idle prompt. Use cutoff_pos as the boundary when TUI is present.
            idle_after = re.search(
                IDLE_PROMPT_STRICT_PATTERN,
                clean_output[response_start:],
                re.MULTILINE,
            )
            if idle_after:
                end_pos = response_start + idle_after.start()
            elif tui_footer_detected:
                end_pos = cutoff_pos
            else:
                end_pos = len(clean_output)

            response_text = clean_output[response_start:end_pos].strip()

            if response_text:
                # Strip "assistant:" prefix if present (label format)
                response_text = re.sub(
                    r"^(?:assistant|codex|agent)\s*:\s*",
                    "",
                    response_text,
                    count=1,
                    flags=re.IGNORECASE,
                )
                return response_text.strip()

        # Fallback: assistant marker based extraction (no user message found).
        # Filter out "• Called <tool>(...)" MCP tool call markers so we anchor
        # on the model's actual reply, not tool output.
        all_matches = list(
            re.finditer(ASSISTANT_PREFIX_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE)
        )
        matches = []
        for m in all_matches:
            line_end = clean_output.find("\n", m.start())
            if line_end == -1:
                line_end = len(clean_output)
            line = clean_output[m.start() : line_end]
            if re.match(MCP_TOOL_CALL_PATTERN, line):
                continue
            matches.append(m)

        if not matches:
            raise ValueError("No Codex response found - no assistant marker detected")

        last_match = matches[-1]
        start_pos = last_match.end()

        idle_after = re.search(
            IDLE_PROMPT_STRICT_PATTERN,
            clean_output[start_pos:],
            re.MULTILINE,
        )
        end_pos = start_pos + idle_after.start() if idle_after else len(clean_output)

        final_answer = clean_output[start_pos:end_pos].strip()

        if not final_answer:
            raise ValueError("Empty Codex response - no content found")

        return final_answer

    def exit_cli(self) -> str:
        """Get the command to exit Codex CLI."""
        return "/exit"

    def cleanup(self) -> None:
        """Clean up Codex CLI provider."""
        self._initialized = False
