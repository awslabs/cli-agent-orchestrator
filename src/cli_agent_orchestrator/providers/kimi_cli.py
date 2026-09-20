"""Kimi CLI provider implementation.

Kimi CLI (https://kimi.com/code) is Moonshot AI's coding agent CLI tool.
It runs as an interactive TUI using prompt_toolkit in the terminal.

Key characteristics:
- Command: ``kimi`` (installed via ``brew install kimi-cli`` or ``uv tool install kimi-cli``)
- Idle prompt: ``💫`` (thinking mode, default) or ``✨`` (optionally prefixed with ``username@dirname``)
- Processing: No idle prompt visible at bottom while the response is streaming
- Response format: Bullet points prefixed with ``•`` (U+2022)
- Thinking output: Gray italic ``•`` bullets (ANSI color 38;5;244 + italic)
- User input: Displayed in a bordered box using box-drawing characters (╭│╰)
- Auto-approve: ``--yolo`` flag bypasses all tool action confirmations
- Agent profiles: ``--agent-file FILE`` (YAML format, extends built-in 'default' agent)
- MCP config: ``--mcp-config TEXT`` (JSON configuration, repeatable flag)
- Exit commands: ``/exit``, ``exit``, ``quit``, or Ctrl-D
- Status bar: ``HH:MM [yolo] agent (model, thinking) ctrl-x: toggle mode context: X.X%``

Status Detection Strategy:
    Kimi CLI uses a full-screen TUI (prompt_toolkit), so status is detected by
    checking the bottom of tmux capture output:
    - IDLE: Prompt pattern (username@dir💫/✨) visible at bottom, no user input yet
    - PROCESSING: No prompt at bottom (response is streaming)
    - COMPLETED: Prompt at bottom + response content after last user input
    - ERROR: Error message patterns or empty output

Two dialects, one provider id
-----------------------------
The public provider identifier stays ``kimi_cli`` for backward compatibility,
but the CLI it drives has forked into two incompatible TUIs:

``KimiDialect.LEGACY`` — MoonshotAI ``kimi-cli``. Emoji prompt, YAML agent file,
``--mcp-config`` JSON injection, ``--yolo`` meaning "never ask", ``•`` U+2022
bullets, a per-directory single-instance lock worked around with a temp cwd.

``KimiDialect.CODE`` — Kimi Code (``agent-core-v2``). No emoji prompt, boxed
composer, Markdown agent file with ``${base_prompt}``, **no MCP CLI flag at
all**, ``--auto`` meaning "never ask" (``--yolo`` was redefined to "ask when
needed"), ``●`` U+25CF bullets, a braille working indicator, and no per-directory
lock — so the temp cwd is both unnecessary and actively harmful.

The dialect is resolved from the *capabilities* of the resolved binary, never
from a version string (see ``KimiDialect`` / ``_probe_dialect``), and an
unrecognised or self-contradictory capability signature fails closed rather than
silently taking the legacy path.

Row-level semantics for both dialects live in
:mod:`cli_agent_orchestrator.providers.kimi_transcript`; per-worker
``KIMI_CODE_HOME`` construction lives in
:mod:`cli_agent_orchestrator.providers.kimi_runtime_home`.
"""

import asyncio
import enum
import json
import logging
import os
import re
import shlex
import shutil
import stat
import string
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from cli_agent_orchestrator.agent_plugins.mcp_delivery import with_plugin_mcp as _with_plugin_mcp
from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers import kimi_transcript as kt
from cli_agent_orchestrator.providers.base import (
    BaseProvider,
    OutputExtractionError,
    OutputExtractionRejected,
)
from cli_agent_orchestrator.providers.kimi_runtime_home import (
    KimiCodeRuntimeHomeBuilder,
    RuntimeHomeError,
    kimi_agent_name,
    resolve_source_home,
)
from cli_agent_orchestrator.services.settings_service import get_server_settings
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.mcp_resolution import resolve_mcp_server_config

# Portable Agent Plugins `type` -> FastMCP `transport`. Kimi 1.20.0 pins
# fastmcp==2.12.5 and hands each --mcp-config document to
# `fastmcp.mcp_config.MCPConfig`.
#
# WHY THIS EXISTS: `RemoteMCPServer` has no `type` field, so a portable `type` is an
# ignored extra. With `transport` absent FastMCP calls
# `infer_transport_type_from_url`, which returns "sse" iff the URL PATH matches
# `/sse(/|\?|&|$)` and "http" otherwise -- so the declared protocol was decided by
# URL spelling: an SSE server published at `/events` started as Streamable HTTP,
# and a Streamable HTTP server at `/sse` started as SSE. Reported by review
# 5222539218 on #584 (item 5).
#
# Only these spellings translate. An absent or unrecognised `type` is left alone:
# `_map_entry` always emits `type` for a plugin server, so a type-less entry came
# from a hand-written profile, where inventing a `transport` would be a behaviour
# change beyond this finding.
KIMI_TRANSPORTS = {
    "stdio": "stdio",
    "streamable-http": "http",
    "http": "http",
    "sse": "sse",
}
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

logger = logging.getLogger(__name__)

# Serializes concurrent _ensure_mcp_timeout() read-modify-writes to
# ~/.kimi/config.toml -- after the async conversion (issue #494),
# _build_kimi_command runs inside asyncio.to_thread, so N concurrent inits can
# enter this method in N threads at once. Without a lock, the check-then-act
# on _mcp_timeout_configured races (two threads both pass the "not configured
# yet" check) and the read-modify-write itself races (one thread's write can
# clobber content another thread already read).
_KIMI_CONFIG_WRITE_LOCK = threading.Lock()


# Custom exception for provider errors
class ProviderError(Exception):
    """Exception raised for Kimi CLI provider-specific errors."""

    pass


class UnsupportedKimiError(ProviderError):
    """The resolved ``kimi`` binary has an unrecognised capability signature.

    Raised instead of guessing a dialect. A wrong guess is not a degraded
    experience — the two dialects disagree about the MCP mechanism entirely, so
    taking the legacy path against a Kimi Code binary would launch a worker with
    no MCP servers at all and no error to explain why. Failing closed here is the
    only outcome that cannot silently mis-configure a terminal.
    """


class KimiDialect(enum.Enum):
    """Which Kimi TUI a resolved binary implements."""

    LEGACY = "legacy"
    CODE = "code"
    UNKNOWN = "unknown"


# =============================================================================
# Dialect detection — capability signature
# =============================================================================
#
# Detection is capability-based on purpose. Kimi Code is 0.43.1 today, but the
# only stable statement about it is which options it accepts; a version-string
# test would need editing on every release and would be wrong for a backport.
#
# Flags are matched as whole option tokens so ``--auto`` can never match
# ``--auto-approve`` and ``--mcp-config`` can never match a longer word.
def _has_cli_flag(help_text: str, flag: str) -> bool:
    """True when ``help_text`` lists ``flag`` as a standalone CLI option."""

    return (
        re.search(
            r"(?:^|[\s,(])" + re.escape(flag) + r"(?=[\s,=<]|$)",
            help_text,
            re.MULTILINE,
        )
        is not None
    )


#: Legacy-only: the MCP CLI injection Kimi Code removed. Its presence is the
#: single decisive legacy marker, because CAO's legacy path depends on it.
LEGACY_CAPABILITY_FLAGS = ("--mcp-config", "--mcp-config-file")

#: Kimi-Code-only markers. All three must be present.
CODE_CAPABILITY_FLAGS = ("--auto", "--agent-file", "--output-format")


def classify_kimi_capabilities(help_text: str) -> Tuple[KimiDialect, Dict[str, bool]]:
    """Classify a ``kimi --help`` dump into a :class:`KimiDialect`.

    Returns the dialect and the observed flag map (for error messages and
    tests). The decision table is total:

    ==================  ==============  ==================================
    legacy MCP flag     code markers    result
    ==================  ==============  ==================================
    yes                 no              ``LEGACY``
    no                  yes             ``CODE``
    yes                 yes             ``UNKNOWN`` (contradictory)
    no                  no              ``UNKNOWN`` (unrecognised)
    ==================  ==============  ==================================
    """

    observed: Dict[str, bool] = {}
    for flag in LEGACY_CAPABILITY_FLAGS + CODE_CAPABILITY_FLAGS:
        observed[flag] = _has_cli_flag(help_text, flag)

    legacy_sig = any(observed[flag] for flag in LEGACY_CAPABILITY_FLAGS)
    code_sig = all(observed[flag] for flag in CODE_CAPABILITY_FLAGS)

    if legacy_sig and not code_sig:
        return KimiDialect.LEGACY, observed
    if code_sig and not legacy_sig:
        return KimiDialect.CODE, observed
    return KimiDialect.UNKNOWN, observed


def _describe_capabilities(observed: Dict[str, bool]) -> str:
    return " ".join(f"{flag}={'yes' if seen else 'no'}" for flag, seen in observed.items())


# ---------------------------------------------------------------------------
# Launch-scoped environment for the Kimi Code path
# ---------------------------------------------------------------------------

#: MCP tool-call timeout. Carries CAO's legacy intent (600 s) unchanged, moved
#: from a shared ``~/.kimi/config.toml`` mutation to a launch-scoped env binding.
#: Kimi Code reads this as the global ``[mcp] toolTimeoutMs`` default.
KIMI_MCP_TOOL_TIMEOUT_MS = 600_000

#: MCP startup timeout. There is no legacy value to preserve (legacy CAO had no
#: startup knob), so this is a deliberate new bound: 2x Kimi Code's own 30 s
#: default, enough for a cold ``cao-mcp-server`` import, small enough that a
#: genuinely hung server still fails within a minute instead of pinning init.
KIMI_MCP_STARTUP_TIMEOUT_MS = 60_000

#: Auto-update suppression. N workers each performing their own CDN check — and
#: potentially self-installing — is the fan-out risk A0 records. Both names are
#: set because the shared updater reads the legacy alias too.
KIMI_NO_AUTO_UPDATE_ENV = {
    "KIMI_CODE_NO_AUTO_UPDATE": "1",
    "KIMI_CLI_NO_AUTO_UPDATE": "1",
}

#: A3-5 — explicit opt-in for answering Kimi Code's workspace-trust dialog.
#:
#: Measured against Kimi Code 0.43.1 (A3-5 probe, see
#: ``reports/kimi_code_compat/A3-REVIEW-FIX-REPORT.md``): choosing *Don't trust*
#: **exits the process immediately** (exit 0) — there is no restricted TUI, no
#: turn, nothing. So trust is not optional for a working terminal.
#:
#: But *granting* trust is a security decision, because trusting a folder:
#:
#: * starts that repository's project MCP servers — arbitrary commands read
#:   straight out of the checkout (``.mcp.json`` and ``.kimi-code/mcp.json``
#:   are both discovered), and
#: * loads that repository's project ``AGENTS.md``, i.e. instructions.
#:
#: A worker launched in a folder the operator does not control would otherwise
#: execute that folder's commands with no human in the loop. That decision
#: belongs to the operator, not to the provider, so CAO does **not** grant trust
#: by default: it refuses to answer the dialog and fails the terminal with an
#: actionable error. Set ``CAO_KIMI_CODE_TRUST_WORKSPACE=1`` in the cao-server
#: environment to let CAO answer it.
#:
#: Note this is CAO's own policy knob and is deliberately **not** exported to
#: Kimi — Kimi has no corresponding setting (0.43.1 exposes no ``--trust`` flag).
#:
#: This is the *broad* control: it lets CAO grant trust to any folder a terminal
#: is launched in. The narrower, preferred control is to trust one repository in
#: normal Kimi and let the worker inherit it (A4) — the runtime home snapshots
#: the real ``KIMI_CODE_HOME`` trust store, so an already-trusted folder never
#: reaches this dialog in the first place.
KIMI_TRUST_OPT_IN_ENV = "CAO_KIMI_CODE_TRUST_WORKSPACE"

#: Values of :data:`KIMI_TRUST_OPT_IN_ENV` that mean "yes". Matching is
#: case-insensitive and whitespace-trimmed; every other value — including unset,
#: ``""``, ``0`` and ``false`` — means "do not grant". Failing closed on an
#: unrecognised value is intentional: a typo must not silently enable trust.
KIMI_TRUST_OPT_IN_TRUE = frozenset({"1", "true", "yes", "on"})


def kimi_trust_opt_in(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Whether the operator has opted in to granting workspace trust (A3-5).

    ``environ`` is injectable so tests can assert the policy without mutating
    the process environment.
    """

    env = os.environ if environ is None else environ
    return env.get(KIMI_TRUST_OPT_IN_ENV, "").strip().lower() in KIMI_TRUST_OPT_IN_TRUE


#: Seconds to wait for the launch shell to report its ``kimi`` resolution.
KIMI_PROBE_TIMEOUT_SECONDS = 20.0

#: Last line the probe command writes into the probe file. Its presence proves
#: the `--help` dump finished; the pane is never consulted (it echoes the typed
#: command, which would match this marker before the command even ran).
KIMI_PROBE_END_MARKER = "CAO-KIMI-PROBE-END"

#: The shell the probe program and the launch line are handed to, chosen
#: explicitly and absolutely. Both are POSIX and contain constructs a non-POSIX
#: pane shell cannot parse — ``fish`` rejects ``${`` outright ("${ is not a valid
#: variable"), so a fish pane never wrote the completion marker and a working
#: Kimi Code binary was classified UNKNOWN. The pane's own shell must therefore
#: only ever parse a shell-agnostic invocation; the POSIX text runs in this child
#: shell, which inherits the pane's environment (PATH, HOME, KIMI_CODE_HOME) so
#: ``command -v kimi`` still observes exactly what the launched ``kimi`` sees.
KIMI_COMPATIBLE_SHELL = "/bin/sh"

#: Characters that every shell we can meet (POSIX sh, bash, zsh, fish, csh)
#: treats as an ordinary literal. A token built only from these needs no quoting
#: at all, and that is the only transport that is provably identical across
#: shells: POSIX quoting is *not* fish quoting. ``'\''`` — the POSIX idiom for an
#: embedded apostrophe — ends the string early in fish when a backslash precedes
#: the apostrophe ("Unexpected end of string, quotes are not balanced", exit
#: 127), and ``\\`` means one backslash in fish but two in POSIX sh. Reproduced
#: with a probe path of ``/tmp/back\'quote/kimi-probe.txt``.
SHELL_SAFE_CHARS = frozenset(string.ascii_letters + string.digits + "_-./:=+,@%")


def is_shell_safe_token(value: str) -> bool:
    """True when ``value`` can be typed into any shell without quoting."""

    return bool(value) and all(char in SHELL_SAFE_CHARS for char in value)


def shell_safe_temp_root() -> str:
    """A temp root whose path needs no quoting in any shell.

    The operator's temp root is used when it is already safe, which is the normal
    case. Otherwise ``/tmp`` is used: a dynamic value that cannot be transported
    safely must not be transported at all, and falling back keeps the probe and
    the launch working on a hostile ``TMPDIR`` instead of failing on it.
    """

    for candidate in (tempfile.gettempdir(), "/tmp"):
        try:
            resolved = os.path.realpath(candidate)
        except OSError:  # pragma: no cover - defensive
            continue
        if os.path.isdir(resolved) and is_shell_safe_token(resolved):
            return resolved
    return "/tmp"


#: Body of the POSIX probe program. It is written to a file (never typed at the
#: pane) so the pane shell has nothing to parse but safe tokens. ``$1`` is the
#: probe file, passed as a positional parameter rather than interpolated, so no
#: dynamic value ever becomes shell syntax.
KIMI_PROBE_PROGRAM = (
    "{ printf 'CAO_KIMI_BIN=%s\\n' \"$(command -v kimi 2>/dev/null)\"; "
    "printf 'CAO_KIMI_HOME=%s\\n' \"${KIMI_CODE_HOME:-$HOME/.kimi-code}\"; "
    "kimi --help 2>&1; "
    "printf '\\n%s\\n' '" + KIMI_PROBE_END_MARKER + '\'; } > "$1" 2>&1'
)


def build_kimi_probe_command(script_path: str, probe_path: str) -> str:
    """The exact command typed into the pane to run the capability probe.

    Every token is drawn from the shell-safe alphabet *by construction*, so the
    pane's shell — whatever it is — parses the same command and has nothing to
    expand, split or unquote. The POSIX program lives in ``script_path`` and is
    executed by an explicitly selected ``/bin/sh``, with the probe file passed as
    a positional parameter.
    """

    if not is_shell_safe_token(script_path) or not is_shell_safe_token(probe_path):
        raise ValueError(
            "Kimi probe paths must be shell-safe (see is_shell_safe_token); "
            f"got script={script_path!r} probe={probe_path!r}"
        )
    return " ".join((KIMI_COMPATIBLE_SHELL, script_path, probe_path))


def build_kimi_launch_command(script_path: str) -> str:
    """The exact command typed into the pane to launch Kimi.

    The launch line itself is POSIX (it quotes the operator's own paths, model
    names and binary), so it is written to ``script_path`` and handed to
    ``/bin/sh``. The pane shell sees only this fixed, quote-free invocation.
    """

    if not is_shell_safe_token(script_path):
        raise ValueError(f"Kimi launch script path must be shell-safe; got {script_path!r}")
    return " ".join((KIMI_COMPATIBLE_SHELL, script_path))


#: Successful probes only, keyed by binary identity. A failed probe is never
#: cached — an UNKNOWN verdict must not become sticky, or a transient PATH or
#: filesystem problem at boot would permanently disable the provider for this
#: process.
_KIMI_DIALECT_CACHE: Dict[Tuple[str, int, int], Tuple[KimiDialect, Dict[str, bool]]] = {}
_KIMI_DIALECT_CACHE_LOCK = threading.Lock()


def _binary_identity(path: str) -> Optional[Tuple[str, int, int]]:
    """Return ``(path, mtime_ns, size)`` for a resolved binary, or None."""

    try:
        info = os.stat(path)
    except OSError:
        return None
    return (path, info.st_mtime_ns, info.st_size)


def _cached_dialect(binary: str) -> Optional[Tuple[KimiDialect, Dict[str, bool]]]:
    identity = _binary_identity(binary)
    if identity is None:
        return None
    with _KIMI_DIALECT_CACHE_LOCK:
        return _KIMI_DIALECT_CACHE.get(identity)


def _cache_dialect(binary: str, dialect: KimiDialect, observed: Dict[str, bool]) -> None:
    """Cache a *successful* classification keyed on the binary's identity."""

    if dialect is KimiDialect.UNKNOWN:
        return
    identity = _binary_identity(binary)
    if identity is None:
        return
    with _KIMI_DIALECT_CACHE_LOCK:
        _KIMI_DIALECT_CACHE[identity] = (dialect, dict(observed))


def reset_dialect_cache() -> None:
    """Drop cached classifications. Used by tests and by config reload paths."""

    with _KIMI_DIALECT_CACHE_LOCK:
        _KIMI_DIALECT_CACHE.clear()


def _read_text_or_empty(path: str) -> str:
    """Read a file that another process may still be writing, or return "".

    A missing file, a permission problem, or a decode failure all mean "no
    usable content yet" for the probe poll, which has its own deadline. Reading
    a file that is mid-write is safe here: the caller only proceeds once the
    end marker is present, and the marker is written last.
    """

    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


@dataclass
class KimiProbeResult:
    """Outcome of the launch-shell capability probe.

    ``binary`` is the absolute path the launch shell resolved and the exact path
    the launch command reuses, which is what makes "probe and exec are the same
    executable" a property of the code rather than a hope.
    """

    dialect: KimiDialect
    binary: str
    source_home: Path
    observed: Dict[str, bool] = field(default_factory=dict)


# =============================================================================
# Regex patterns for Kimi CLI output analysis
# =============================================================================

# Strip ANSI escape codes for clean text matching.
# Matches sequences like \x1b[0m, \x1b[38;5;244m, \x1b[1m, etc.
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"

# Kimi idle prompt: ``💫`` or ``✨`` (optionally prefixed with ``username@dirname``).
# ✨ appears in normal agent mode (--no-thinking).
# 💫 appears when thinking mode is enabled (default behavior).
# Kimi CLI v1.20.0+ renders just the emoji; earlier versions showed ``username@dirname💫``.
# The prefix is made optional to support both formats.
IDLE_PROMPT_PATTERN = r"(?:\w+@[\w.-]+)?[✨💫]"

# Number of lines from bottom to scan for the idle prompt.
# Kimi's TUI renders empty padding lines between the prompt and the status bar.
# The padding depends on terminal height: a 46-row terminal has ~32 empty lines
# between the prompt (line ~14 after the welcome banner) and the status bar.
# Must be large enough to cover the tallest expected terminal.
IDLE_PROMPT_TAIL_LINES = 50

# Simplified idle pattern for log file monitoring.
# Just looks for either emoji marker, which is sufficient for quick detection.
IDLE_PROMPT_PATTERN_LOG = r"[✨💫]"

# Kimi welcome banner, shown once during startup inside a bordered box.
# Used to detect successful initialization without needing to wait for prompt.
# Alternation, not replacement: Kimi Code renamed the product line, so the
# banner now reads "Welcome to Kimi Code!" while legacy builds still print
# "Welcome to Kimi Code CLI!". Deliberately NOT a dialect detector — a banner is
# cosmetic text and the two dialects are separated by capability (A1.1).
WELCOME_BANNER_PATTERN = r"Welcome to Kimi Code(?: CLI)?!"

# Startup upgrade-reminder dialog. When a newer kimi-cli is available, kimi
# renders an interactive menu ("[Enter] Upgrade now  [q] Not now  [s] Skip
# reminders for version X") BEFORE the REPL and blocks on a keypress. Left
# unanswered, kimi never reaches its ready prompt and init times out (the boot
# gate holds it PROCESSING). We answer 's' to skip reminders for this version
# (persisted, so it does not recur until the next release).
UPGRADE_PROMPT_PATTERN = r"Skip reminders for version|Upgrade now"

# User input box boundaries (pre-v1.20.0). Kimi displayed user messages in a bordered box:
#   ╭──────────────────────────────╮
#   │ user message text             │
#   ╰──────────────────────────────╯
# In v1.20.0+, user input appears on the prompt line: ``💫 user message``
USER_INPUT_BOX_START_PATTERN = r"╭─"
USER_INPUT_BOX_END_PATTERN = r"╰─"

# Prompt line with user input (v1.20.0+ format).
# Matches ``💫 some text`` or ``✨ some text`` — a prompt emoji followed by non-whitespace
# on the SAME line. Uses [^\S\n]+ (horizontal whitespace only) to avoid matching
# across newlines (a bare ``💫`` followed by blank lines then status bar).
# An optional ``user@host`` prefix used to lead this pattern. It never changed
# whether the pattern matched — every use is an unanchored search, so the emoji is
# found with or without it — and its `\w+@` cost one rescan per word character on
# input that never reaches an ``@`` (quadratic backtracking, CWE-1333).
PROMPT_WITH_INPUT_PATTERN = r"[✨💫][^\S\n]+\S"

# Response/thinking bullet pattern: ``•`` (U+2022) or ``●`` (U+25CF) at the
# start of a line. Both thinking (internal monologue) and response (final answer)
# use the same glyph within a dialect, so styling — not the character — is what
# separates them in extraction:
# - Thinking (legacy): gray italic (\x1b[38;5;244m• ... \x1b[3m)
# - Thinking (Kimi Code): \x1b[38;5;244m● \x1b[3m…  (same gray, U+25CF glyph)
# - Response: \x1b[38;5;253m● \x1b[39m… (Kimi Code) / bare ``•`` (legacy)
# Anchored at column 0 to match its historical contract; leading-whitespace
# tolerant matching, and the "bullet plus payload" rule, are provided by the
# shared classifier (`kimi_transcript.is_response_marker_line` /
# `BULLET_ANY_RE`), which is what every status path now consumes.
RESPONSE_BULLET_PATTERN = r"^[•●]\s"

# Thinking bullet detection in raw (ANSI-preserved) output.
# Thinking lines are drawn in gray (38;5;244) before the bullet character.
# Both dialects' glyphs are accepted: the glyph changed between Kimi releases
# (``•`` -> ``●``) but the styling did not, which is why keying on the style is
# what actually survives a release.
THINKING_BULLET_RAW_PATTERN = r"\x1b\[38;5;244m\s*[•●]"

# Kimi TUI status bar at the bottom of the screen.
# Format: "HH:MM  [yolo]  agent (model, thinking)  ctrl-x: toggle mode  context: X.X%"
# Used to identify TUI chrome that should be excluded from content analysis.
STATUS_BAR_PATTERN = r"\d+:\d+\s+.*(?:agent|shell)\s*\("

# ---------------------------------------------------------------------------
# Newest "Kimi Code" TUI (the redesigned CLI). Older builds rendered an emoji
# prompt (✨/💫) at the input line; the redesign instead shows a boxed input
# area ("── input ──"), a bottom status bar ("yolo  agent (<model> ●) …"), and a
# "context: 12.3% (n/Nk)" usage line — with NO bare emoji prompt. Detection that
# keyed on the emoji therefore never observed IDLE and timed out at init.
# ---------------------------------------------------------------------------
# Either of these confirms the new TUI is up at its prompt: the context-usage
# footer, or the status bar's "agent (<model> ●)" segment (● = U+25CF).
# The gap between "(" and "●" holds a model name, so it is bounded and stops at
# the first ●. An unbounded `[^)]*` made the unanchored search re-walk the whole
# buffer from every "agent(" in it — quadratic backtracking (CWE-1333) on output
# an agent can put on screen at will.
NEW_TUI_STATUS_PATTERN = r"context:\s*\d+(?:\.\d+)?%|agent\s*\([^)●]{0,80}●"
# Live working indicator: a braille glyph (U+2800-U+28FF). Kimi Code 0.43.1
# animates "⠙ working…" and clears it when the turn finishes.
#
# Moon phases (U+1F311-U+1F318) were previously included here. They must NOT be:
# the current TUI rotates an idle *tip* row through the same slot
# ("🌕 · Tip: ctrl-s to add guidance…"), so treating a moon glyph as work reads a
# settled terminal as PROCESSING (A0 defect D1; fixtures 05 and 09 both exhibit
# it). The tip row is positively excluded by kimi_transcript.is_idle_tip_line,
# and a bare moon row with no tip suffix is still accepted as work because that
# shape is the *legacy* processing glyph and the fail-safe direction for an
# ambiguous frame is "still working".
NEW_TUI_SPINNER_PATTERN = r"[\u2800-\u28ff]"
# Moon-phase glyphs, kept as a named pattern so the exclusion is explicit and
# testable rather than an absence.
NEW_TUI_MOON_TIP_PATTERN = r"[\U0001F311-\U0001F318]"
# Boot/MCP chrome also renders braille glyphs while the terminal is genuinely
# idle at the welcome screen ("⠧ MCP Servers: 0/1 connected", "⠦ cao-mcp-server
# (connecting)", "⠋ Resolving dependencies..."). Those must NOT count as a
# live turn-in-flight spinner or a freshly-booted terminal would never read
# IDLE. The decision now lives in the shared classifier, which identifies those
# rows structurally (whole-row anchoring) rather than by substring, so an answer
# that merely quotes them is not chrome. Kept as a documented pattern for the
# shapes it names; nothing gates on it.
NEW_TUI_BOOT_CHROME_PATTERN = re.compile(
    r"MCP Servers|\(connecting\)|Resolving dependencies|connecting to mcp servers"
    r"|Loading configuration|Loading agent|Restoring conversation",
    re.IGNORECASE,
)


def _is_live_turn_spinner_line(
    line: str,
    semantics: "kt.SpinnerSemantics" = kt.SpinnerSemantics.LEGACY,
) -> bool:
    """True when ``line`` carries a live turn-in-flight spinner glyph.

    Delegates to the shared transcript classifier so boot chrome, the idle
    rotating tip row, and the dialect split between a bare moon (the legacy
    working glyph) and a braille indicator (the Kimi Code working glyph) are all
    resolved in exactly one place (see
    :mod:`cli_agent_orchestrator.providers.kimi_transcript`).

    ``semantics`` defaults to the legacy rules so a caller that has not resolved
    a dialect keeps the historical behaviour.
    """
    # The caller may hand in a raw, ANSI-bearing row, so the escape-free form has
    # to be derived rather than assumed: the indicator is identified by its
    # position at the row's prefix, and a leading colour sequence would hide it.
    return kt.is_live_spinner_line(kt.strip_sgr(line), line, semantics)


# Response markers.
#
# There is deliberately no locally-defined bullet regex here any more. The
# previous pair — `ANY_BULLET_PATTERN = r"(?m)^[^\S\n]*[•●]"` and
# `BULLET_LINE_PATTERN = re.compile(r"^[^\S\n]*[•●]")` — matched a bare bullet
# with nothing after it, so a narrow terminal that wrapped the status bar into a
# row beginning `●)` or `•)` was read as assistant output. That latched
# "input received" on an idle terminal and reported COMPLETED: the PR #664
# narrow-terminal defect class, reintroduced next to the classifier that had
# already been fixed for it.
#
# All status paths now go through the shared helpers
# `kimi_transcript.is_response_marker_line` / `has_response_marker`, which
# require a bullet *plus a payload*. `BULLET_LINE_PATTERN` is kept as an alias to
# the single shared definition so the status paths and the ReDoS regression test
# keep pointing at one pattern.
BULLET_LINE_PATTERN = kt.BULLET_ANY_RE

# Generic error patterns for detecting failure states in terminal output.
ERROR_PATTERN = (
    r"^(?:Error:|ERROR:|Traceback \(most recent call last\):|ConnectionError:|APIError:)"
)


class KimiCliProvider(BaseProvider):
    """Provider for Kimi CLI tool integration.

    Manages the lifecycle of a Kimi CLI session in a tmux window,
    including initialization, status detection, response extraction,
    and cleanup. Kimi CLI agent profiles are optional — if not provided,
    Kimi uses its built-in default agent.
    """

    # Class-level flag: ensures ~/.kimi/config.toml MCP timeout is set only once,
    # even when multiple KimiCliProvider instances are created in parallel (e.g.,
    # 3 data_analyst workers via assign). Without this, concurrent read/write to
    # the config file causes race conditions and file corruption.
    _mcp_timeout_configured = False

    # Class-level prompt regex shared between status detection
    # and ``extract_session_context``. Bounded quantifiers
    # (no unbounded ``*`` / ``+`` — defeats ReDoS on pathological pane bytes).
    # Matches the v1.20+ idle-line shape ``[user@host]💫 message`` AND the
    # bare-emoji ``💫 message`` form. The optional ``\S`` tail matches "user
    # text follows on the same line" — used to slice the message off after
    # the prompt marker.
    _KIMI_PROMPT_RE = re.compile(r"(?:\w{1,32}@[\w.\-]{1,64})?[✨💫][^\S\n]{1,4}\S")
    # Response/thinking markers used to bound a user message line. Matches
    # the same bullets the IDLE/PROCESSING path uses, in both dialects.
    _KIMI_RESPONSE_MARKER_RE = re.compile(r"^[•●]\s")

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list] = None,
        skill_prompt: Optional[str] = None,
        model: Optional[str] = None,
    ):
        """Initialize provider state."""
        super().__init__(terminal_id, session_name, window_name, allowed_tools, skill_prompt)
        self._initialized = False
        self._agent_profile = agent_profile
        # Explicit per-call override for profile.model, see initialize().
        self._model = model
        # Track temp directory for cleanup (created when agent profile needs temp files)
        self._temp_dir: Optional[str] = None
        # Shell-safe scratch directory for the artifacts typed at the pane (the
        # probe program and the launch script). See _ensure_shell_safe_dir.
        self._shell_safe_dir: Optional[str] = None
        # Latching flag: set True when user input box (╭─) is detected in ANY
        # get_status() call. Persists even after the box scrolls out of the
        # tmux capture window (200 lines). This is needed because:
        # 1. Long responses push the user input box out of capture range
        # 2. Not all responses use • bullets (tables, numbered lists, etc.)
        # Without this, get_status() returns IDLE instead of COMPLETED after
        # the agent finishes processing, causing handoff to time out.
        self._has_received_input = False
        # Wallclock of the last send_input() dispatch (terminal_service calls
        # mark_input_received). Used by the newest-TUI status path: right
        # after a paste, the TUI repaints the ready chrome (status bar) before
        # the spinner's first frame, so a position-based spinner-vs-ready
        # compare reads COMPLETED ~100ms into the new turn. With the
        # StatusMonitor ready-latch, that false COMPLETED is pinned for the
        # whole turn (observed: supervisor-assign e2e extracting mid-flight
        # output). A short dispatch grace bridges the gap until the first
        # spinner frame arrives.
        self._last_dispatch_time = 0.0

        # --- dialect state (Kimi Code compatibility) -------------------------
        # Resolved by _probe_dialect() during initialize(), from the capabilities
        # of the binary the *launch shell* resolves. Stays None until then, and
        # None means "legacy": _build_kimi_command() is the legacy builder and is
        # also called directly by tests and by no-probe callers.
        self._dialect: Optional[KimiDialect] = None
        # Absolute path of the resolved binary, reused verbatim for the launch so
        # the probe and the exec provably refer to the same file (PR #664's bug
        # was probing one PATH and launching from another).
        self._kimi_binary: Optional[str] = None
        # Effective source KIMI_CODE_HOME, captured by the same launch-shell probe.
        self._kimi_source_home: Optional[Path] = None
        # Per-worker runtime home builder (Kimi Code only).
        self._runtime_home_builder: Optional[KimiCodeRuntimeHomeBuilder] = None
        # Latched once the workspace-trust dialog has been answered, so its
        # lingering text cannot cause a second keypress.
        self._trust_handled = False

    @property
    def paste_enter_count(self) -> int:
        """Kimi CLI's prompt_toolkit submits on single Enter after bracketed paste."""
        return 1

    def mark_input_received(self) -> None:
        """Record a dispatched task (called by terminal_service after send_input).

        Latches ``_has_received_input`` (the buffer-evidence latch can miss it
        when a long paste scrolls the echo out of the rolling window). The
        ``_last_dispatch_time`` stamp (used by the newest-TUI dispatch-grace
        check in get_status()) and the shared native-status tracking come from
        ``super().mark_input_received()``.
        """
        super().mark_input_received()
        self._has_received_input = True

    def _try_load_profile(self):
        """Best-effort profile load for timeout resolution only.

        Returns None on any load failure instead of raising -- unlike
        ``_build_kimi_command``'s inline load, which legitimately raises
        ``ProviderError`` on a broken profile. This helper only feeds
        ``BaseProvider.get_init_timeout``, so a missing/unloadable profile
        should fall back to the server default here, not abort init before
        the real (error-raising) load in ``_build_kimi_command`` gets a chance
        to report the actual problem.
        """
        if self._agent_profile is None:
            return None
        try:
            return _with_plugin_mcp(load_agent_profile(self._agent_profile), "kimi_cli")
        except Exception:
            return None

    # =====================================================================
    # Kimi Code compatibility: dialect probe
    # =====================================================================

    def _ensure_temp_dir(self, prefix: str = "cao_kimi_") -> str:
        """Return this provider's scratch directory, creating it on first use.

        Created under a shell-safe root so every path CAO derives from it can be
        typed into any pane shell without quoting — see
        :func:`is_shell_safe_token`. The generated name is ``<prefix><random>``,
        which is itself drawn from the safe alphabet.
        """

        if not self._temp_dir:
            self._temp_dir = tempfile.mkdtemp(prefix=prefix, dir=shell_safe_temp_root())
        return self._temp_dir

    def _ensure_shell_safe_dir(self) -> str:
        """A scratch directory whose path needs no quoting in any shell.

        Normally the provider's own temp dir. A caller-supplied ``_temp_dir``
        with shell-hostile characters is not used for transport: the probe and
        launch artifacts move to a safe directory instead, so a hostile path can
        never reach a shell parser.
        """

        if self._shell_safe_dir is None:
            base = self._ensure_temp_dir()
            if is_shell_safe_token(base):
                self._shell_safe_dir = base
            else:
                self._shell_safe_dir = tempfile.mkdtemp(
                    prefix="cao_kimi_", dir=shell_safe_temp_root()
                )
        return self._shell_safe_dir

    @staticmethod
    def _write_private_script(directory: str, name: str, body: str) -> str:
        """Write an executable helper script and return its path."""

        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.chmod(path, 0o700)
        return path

    def _materialize_launch_command(self, command: str) -> str:
        """Move a POSIX launch line into a script and return the pane command.

        The launch line quotes the operator's own paths, model names and resolved
        binary, so it is POSIX text that an arbitrary pane shell must not be
        asked to parse. It is written to a CAO-owned script and handed to
        ``/bin/sh``; the pane receives a fixed invocation built only from
        shell-safe characters.
        """

        directory = self._ensure_shell_safe_dir()
        script = self._write_private_script(
            directory,
            "kimi-launch.sh",
            "#!/bin/sh\n# CAO-managed launch line (POSIX). Do not edit.\nexec /bin/sh -c "
            + shlex.quote(command)
            + "\n",
        )
        return build_kimi_launch_command(script)

    @staticmethod
    def _dialect_failure_reason(observed: Dict[str, bool]) -> str:
        legacy_sig = any(observed[flag] for flag in LEGACY_CAPABILITY_FLAGS)
        code_sig = all(observed[flag] for flag in CODE_CAPABILITY_FLAGS)
        if legacy_sig and code_sig:
            return (
                "capability signature is self-contradictory: the binary advertises "
                "legacy MCP CLI injection and the Kimi Code option set at once"
            )
        return "capability signature is unrecognised: neither dialect's markers are present"

    def _unsupported_message(self, binary: str, observed: Dict[str, bool], reason: str) -> str:
        """Build the fail-closed error text (never contains credentials)."""

        return (
            f"Unsupported Kimi CLI build: could not determine dialect for {binary!r}.\n"
            f"  resolved binary : {binary}\n"
            f"  detected flags  : {_describe_capabilities(observed)}\n"
            f"  reason          : {reason}\n"
            "  CAO requires a build that is either legacy kimi-cli "
            "(advertises --mcp-config) or Kimi Code "
            "(advertises --auto + --agent-file + --output-format). "
            "Refusing to guess, because the two dialects use incompatible MCP "
            "mechanisms and a wrong guess would launch this worker with no MCP "
            "servers and no error."
        )

    async def _probe_kimi_environment(self) -> "KimiProbeResult":
        """Resolve the binary, its capabilities, and the effective source home.

        Everything is resolved by the **launch shell** — the tmux pane's own
        shell, which is the shell that will exec ``kimi`` — not by the
        cao-server process. PR #664's bug was exactly this divergence: the
        server probed one ``PATH`` while the pane launched from another, so the
        dialect decided at init did not describe the binary that actually ran.

        The pane is asked for three things in one command, all redirected into a
        file the CAO process reads afterwards (so ``kimi --help`` never floods
        the pane):

        * ``CAO_KIMI_BIN`` — ``command -v kimi`` as the launch shell sees it,
        * ``CAO_KIMI_HOME`` — the effective ``KIMI_CODE_HOME`` in that same shell,
        * the full ``--help`` text, which is the capability signature.

        The command is a POSIX program handed to an explicitly selected shell
        (:data:`KIMI_PROBE_SHELL`), not typed at the pane's interactive shell —
        see :func:`build_kimi_probe_command`. The program is still *resolved in*
        the pane's environment, so the probe and the launch agree even when the
        pane runs a shell that cannot parse the program itself.

        The returned absolute path is then used verbatim for the launch, so
        probe and exec provably refer to the same file.

        Raises:
            UnsupportedKimiError: the probe could not complete, resolved no
                absolute binary, or produced an unrecognised/contradictory
                capability signature. Never falls back to the legacy dialect.
        """

        probe_dir = await asyncio.to_thread(self._ensure_shell_safe_dir)
        probe_path = os.path.join(probe_dir, "kimi-probe.txt")

        # The program is POSIX and contains `${VAR:-default}` and `$(...)`. It is
        # written to a file and handed to an explicitly selected shell instead of
        # being typed at the pane, because the pane's shell is the operator's
        # choice: `fish` cannot parse either construct, so it would fail before
        # writing the completion marker and a working Kimi Code binary would be
        # classified UNKNOWN. The command typed at the pane is built only from
        # shell-safe characters, so no shell has to quote, split or expand
        # anything — POSIX quoting is not fish quoting, and a probe path
        # containing a backslash-before-apostrophe broke `shlex.quote` under fish.
        #
        # This shell is a child of the pane shell, so PATH/HOME/KIMI_CODE_HOME —
        # and therefore `command -v kimi` — are exactly what the launched `kimi`
        # will see.
        #
        # The completion signal is read from the FILE, never from the pane. The
        # pane is unusable for this: `send_keys` types the script as literal
        # text, so the terminal echoes the whole command line -- including
        # whatever sentinel string it contains -- *before* the command has run.
        # Polling the pane for that string therefore matched instantly and the
        # probe read a half-written file, classifying a perfectly good Kimi Code
        # binary as UNKNOWN. The end marker is written by the shell as the last
        # thing it does, so its presence in the file means the dump is complete.
        script_path = await asyncio.to_thread(
            self._write_private_script, probe_dir, "kimi-probe.sh", KIMI_PROBE_PROGRAM + "\n"
        )
        probe_script = build_kimi_probe_command(script_path, probe_path)

        await asyncio.to_thread(
            get_backend().send_keys, self.session_name, self.window_name, probe_script
        )

        deadline = time.monotonic() + KIMI_PROBE_TIMEOUT_SECONDS
        text = ""
        while time.monotonic() < deadline:
            text = await asyncio.to_thread(_read_text_or_empty, probe_path)
            if KIMI_PROBE_END_MARKER in text:
                break
            await asyncio.sleep(0.25)

        if KIMI_PROBE_END_MARKER not in text:
            raise UnsupportedKimiError(
                f"Unsupported Kimi CLI build: the launch shell did not answer the "
                f"capability probe within {KIMI_PROBE_TIMEOUT_SECONDS:.0f}s.\n"
                f"  probe file      : {probe_path}\n"
                f"  bytes written   : {len(text)}\n"
                "  CAO resolves the kimi binary inside the launch shell so that the "
                "probed and the launched executable are provably the same file; it "
                "will not fall back to the legacy dialect when that resolution fails."
            )

        binary = ""
        source_home_raw = ""
        help_lines: List[str] = []
        for line in text.splitlines():
            if line.startswith("CAO_KIMI_BIN="):
                binary = line[len("CAO_KIMI_BIN=") :].strip()
            elif line.startswith("CAO_KIMI_HOME="):
                source_home_raw = line[len("CAO_KIMI_HOME=") :].strip()
            elif line.strip() == KIMI_PROBE_END_MARKER:
                continue
            else:
                help_lines.append(line)

        if not binary or not os.path.isabs(binary):
            raise UnsupportedKimiError(
                f"Unsupported Kimi CLI build: the launch shell did not resolve "
                f"'kimi' to an absolute path (got {binary!r}).\n"
                "  Ensure 'kimi' is a real executable on the launch shell's PATH "
                "(a shell function or alias cannot be probed or launched reliably)."
            )

        dialect, observed = classify_kimi_capabilities("\n".join(help_lines))
        if dialect is KimiDialect.UNKNOWN:
            raise UnsupportedKimiError(
                self._unsupported_message(binary, observed, self._dialect_failure_reason(observed))
            )

        # Cache only a successful classification, keyed on the binary's identity.
        # A failure above is never cached, so a transient probe problem cannot
        # permanently disable this provider for the process.
        await asyncio.to_thread(_cache_dialect, binary, dialect, observed)

        self._dialect = dialect
        self._kimi_binary = binary
        self._kimi_source_home = resolve_source_home(source_home_raw)
        logger.info(
            "kimi_dialect_resolved terminal=%s dialect=%s binary=%s source_home=%s flags=%s",
            self.terminal_id,
            dialect.value,
            binary,
            self._kimi_source_home,
            _describe_capabilities(observed),
        )
        return KimiProbeResult(
            dialect=dialect,
            binary=binary,
            source_home=self._kimi_source_home,
            observed=observed,
        )

    async def _resolve_dialect(self) -> "KimiProbeResult":
        """Return the dialect for this terminal, probing once per binary.

        A cached verdict short-circuits the pane round-trip, but the cache key
        includes the binary's ``mtime`` and size, so a binary replaced in place is
        re-probed automatically.
        """

        cached = None
        if self._kimi_binary:
            cached = _cached_dialect(self._kimi_binary)
        if cached is not None:
            dialect, observed = cached
            self._dialect = dialect
            logger.debug(
                "kimi_dialect_cached terminal=%s dialect=%s", self.terminal_id, dialect.value
            )
            return KimiProbeResult(
                dialect=dialect,
                binary=self._kimi_binary or "kimi",
                source_home=resolve_source_home(None),
                observed=dict(observed),
            )
        return await self._probe_kimi_environment()

    # =====================================================================
    # Kimi Code compatibility: launch command
    # =====================================================================

    def _render_markdown_agent(self, profile: Any) -> Optional[str]:
        """Render the launch-only Markdown agent file, or None when empty.

        Kimi Code replaced the legacy YAML agent file (``agent: extend: default``
        + ``system_prompt_path``) with a Markdown file whose body may interpolate
        ``${base_prompt}``. Emitting ``${base_prompt}`` **before** CAO's own text
        is what keeps Kimi's base system prompt intact and appends CAO's
        instructions to it, instead of replacing the whole prompt (the
        #664-class failure).
        """

        system_prompt = ""
        if profile is not None and profile.system_prompt is not None:
            system_prompt = profile.system_prompt
        system_prompt = self._apply_skill_prompt(system_prompt)

        # Prepend security constraints for soft enforcement. Kimi Code's
        # `tools`/`disallowedTools` frontmatter could enforce this natively, but
        # CAO keeps the prompt-level guarantee for this change: kimi_cli is
        # registered as a soft-enforcement provider and silently upgrading an
        # advisory restriction to a hard one is a separate decision.
        if self._allowed_tools and "*" not in self._allowed_tools:
            from cli_agent_orchestrator.constants import SECURITY_PROMPT

            tools_list = ", ".join(self._allowed_tools)
            tool_constraint = f"\nYou only have access to these tools: {tools_list}\n"
            system_prompt = SECURITY_PROMPT + tool_constraint + system_prompt

        if not system_prompt.strip():
            return None

        name = kimi_agent_name(self.terminal_id)
        description = json.dumps(f"CAO launch-scoped agent for terminal {self.terminal_id}"[:200])
        return (
            "---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            "---\n"
            "\n"
            "${base_prompt}\n"
            "\n"
            f"{system_prompt}\n"
        )

    def _build_kimi_code_command(self) -> str:
        """Build the Kimi Code launch command.

        Contract (frozen by A1.2/A1.3/A1.4/A1.5/A1.6):

        * **No ``cd``.** The CAO terminal's own working directory is Kimi's cwd,
          which is what restores git-root resolution, repository ``AGENTS.md``
          discovery, project MCP discovery and a stable session namespace. The
          legacy temp-cwd workaround exists to dodge a per-directory lock that
          A0 proved does not exist in Kimi Code.
        * ``KIMI_CODE_HOME`` points at this worker's runtime home, so each worker
          gets its own ``mcp.json`` and arbitrary per-profile MCP surfaces stay
          isolated.
        * ``CAO_TERMINAL_ID`` is exported for the Kimi process: Kimi Code's stdio
          MCP children inherit the parent environment, so one export reaches
          every server (including user-level ones) without per-server injection.
        * MCP timeouts travel as launch-scoped env bindings, never as a mutation
          of the user's ``config.toml``.
        * Auto-update is disabled for every CAO-managed worker, so N workers do
          not each poll the CDN and self-install.
        * ``--auto`` (never ask), not ``--yolo`` (ask when needed): CAO workers
          must run unattended.
        """

        binary = self._kimi_binary
        if not binary:
            raise ProviderError(
                "Kimi Code launch requires a resolved binary; "
                "_probe_kimi_environment() must run first."
            )

        profile = None
        if self._agent_profile is not None:
            try:
                profile = load_agent_profile(self._agent_profile)
            except Exception as e:
                raise ProviderError(f"Failed to load agent profile '{self._agent_profile}': {e}")

        temp_dir = self._ensure_temp_dir()
        source_home = self._kimi_source_home or resolve_source_home(None)

        mcp_servers = profile.mcpServers if profile is not None else None
        builder = KimiCodeRuntimeHomeBuilder(source_home, Path(temp_dir))
        try:
            runtime = builder.build(mcp_servers)
        except RuntimeHomeError as exc:
            raise ProviderError(f"Failed to build Kimi Code runtime home: {exc}") from exc
        self._runtime_home_builder = builder

        command_parts = [
            "env",
            f"KIMI_CODE_HOME={runtime.home}",
            f"CAO_TERMINAL_ID={self.terminal_id}",
            "TERM=xterm-256color",
            f"KIMI_MCP_TOOL_TIMEOUT_MS={KIMI_MCP_TOOL_TIMEOUT_MS}",
            f"KIMI_MCP_STARTUP_TIMEOUT_MS={KIMI_MCP_STARTUP_TIMEOUT_MS}",
        ]
        for key, value in KIMI_NO_AUTO_UPDATE_ENV.items():
            command_parts.append(f"{key}={value}")

        command_parts.extend([binary, "--auto"])

        # self._model is an explicit per-call override (handoff/assign's own
        # `model` parameter) and wins over the profile's static model field.
        resolved_model = self._model or (profile.model if profile is not None else None)
        if resolved_model:
            command_parts.extend(["--model", resolved_model])

        agent_markdown = self._render_markdown_agent(profile)
        if agent_markdown is not None:
            agent_path = os.path.join(temp_dir, "kimi-code-agent.md")
            with open(agent_path, "w", encoding="utf-8") as handle:
                handle.write(agent_markdown)
            os.chmod(agent_path, 0o600)
            command_parts.extend(["--agent-file", agent_path])

        # Deliberately no `cd`: the tmux window already sits in the CAO project
        # cwd, and that is exactly the working directory Kimi Code must keep.
        return shlex.join(command_parts)

    # =====================================================================
    # Kimi Code compatibility: workspace trust
    # =====================================================================

    @staticmethod
    def _normalise_workspace(path: str) -> str:
        """Normalise a workspace path for comparison (realpath, no trailing /)."""

        try:
            resolved = os.path.realpath(path)
        except OSError:  # pragma: no cover - defensive
            resolved = path
        return resolved.rstrip("/") or "/"

    async def _handle_trust_dialog(self, output: str) -> bool:
        """Answer Kimi Code's workspace-trust dialog for this terminal's folder.

        Kimi Code raises this dialog for **any** untrusted cwd, even when the
        repository declares no project MCP servers, and its default selection is
        *Trust this folder*. Blindly sending Enter therefore silently grants
        project-MCP trust to whatever folder the terminal launched in — which is
        precisely why this handler is positive-identification only:

        * the exact dialog must be present (title + navigation hint + a
          recognised option set), and
        * the workspace the dialog names must equal the pane's actual working
          directory, and
        * the selection must be readable.

        If the selection is already *Trust this folder* it is accepted. Otherwise
        CAO navigates deterministically, re-reads the pane, and only then accepts.
        Every other outcome — unknown layout, unreadable selection, mismatched
        workspace — fails closed without sending a key.

        A3-5: answering at all additionally requires the operator's explicit
        opt-in (:func:`kimi_trust_opt_in`). This method never grants project
        trust on its own initiative — see the module-level note on
        :data:`KIMI_TRUST_OPT_IN_ENV` for the evidence behind that.

        A4: this handler is only reached for a folder the operator has *not*
        already trusted. A worker's runtime home inherits the trust records from
        the real ``KIMI_CODE_HOME`` (see
        :class:`~cli_agent_orchestrator.providers.kimi_runtime_home.KimiCodeRuntimeHomeBuilder`),
        so a folder already trusted in normal Kimi produces no dialog here at
        all. That is what makes pre-trusting one repository — rather than
        setting the server-wide opt-in — a real answer to this error.

        Returns True when the dialog was handled (so the caller can reset its
        idle timer), False when no trust dialog is on screen. Raises
        :class:`ProviderError` when a dialog is present but must not be answered.
        """

        if self._trust_handled:
            return False

        rows = output.split("\n")
        dialog = kt.detect_trust_dialog(rows)
        if dialog is None:
            return False

        # A3-5 — the security gate. Kimi Code will not run without trust
        # (choosing "Don't trust" exits the process), so the only question is
        # *who decides*. Answering it here means CAO starts the launched
        # folder's project MCP servers and loads its project AGENTS.md, so the
        # decision is the operator's, and it must be explicit. Refusing here is
        # loud and actionable rather than silent; it fires only when a dialog is
        # actually on screen, so an already-trusted folder is unaffected.
        if not kimi_trust_opt_in():
            raise ProviderError(
                "Kimi Code is asking to trust this folder and CAO will not "
                "answer it. Trusting a folder starts that repository's project "
                "MCP servers and loads its project AGENTS.md, so the decision "
                "belongs to you.\n"
                f"  folder: {dialog.workspace or '<unreadable>'}\n"
                "  option A (preferred, this one workspace): run `kimi` in that "
                "folder with your normal KIMI_CODE_HOME and choose "
                "'Trust this folder'. CAO inherits the decision, so the prompt "
                "will not come back for it.\n"
                f"  option B (broader, every workspace): set "
                f"{KIMI_TRUST_OPT_IN_ENV}=1 in the cao-server environment and "
                "restart cao-server. This lets CAO grant trust to whatever "
                "folder a terminal is launched in, including folders you have "
                "not reviewed."
            )

        pane_cwd = None
        try:
            pane_cwd = await asyncio.to_thread(
                get_backend().get_pane_working_directory, self.session_name, self.window_name
            )
        except Exception as exc:  # noqa: BLE001 - backend may not implement it
            logger.debug(
                "kimi_trust_pane_cwd_unavailable terminal=%s err=%s", self.terminal_id, exc
            )

        if not pane_cwd or not dialog.workspace:
            raise ProviderError(
                "Kimi Code workspace-trust dialog detected but the target folder "
                "could not be verified, so CAO will not answer it.\n"
                f"  dialog workspace: {dialog.workspace!r}\n"
                f"  pane workspace  : {pane_cwd!r}"
            )

        if self._normalise_workspace(pane_cwd) != self._normalise_workspace(dialog.workspace):
            raise ProviderError(
                "Kimi Code workspace-trust dialog is asking about a folder other "
                "than this terminal's working directory; refusing to answer it.\n"
                f"  dialog workspace: {dialog.workspace}\n"
                f"  pane workspace  : {pane_cwd}"
            )

        if dialog.selected_option is None:
            raise ProviderError(
                "Kimi Code workspace-trust dialog detected but no selection marker "
                f"({kt.TRUST_SELECT_MARKER!r}) could be read; refusing to guess."
            )

        from cli_agent_orchestrator.services.status_monitor import status_monitor

        if dialog.selected_option != kt.TRUST_OPTION_TRUST:
            target_index = dialog.options.index(kt.TRUST_OPTION_TRUST)
            current_index = dialog.selected_index
            if current_index is None or current_index == target_index:
                raise ProviderError(
                    "Kimi Code workspace-trust dialog selection could not be "
                    "navigated deterministically; refusing to guess."
                )
            delta = current_index - target_index
            key = "Up" if delta > 0 else "Down"
            logger.info(
                "kimi_trust_navigating terminal=%s from=%s to=%s presses=%d",
                self.terminal_id,
                dialog.selected_option,
                kt.TRUST_OPTION_TRUST,
                abs(delta),
            )
            status_monitor.notify_input_sent(self.terminal_id)
            for _ in range(abs(delta)):
                await asyncio.to_thread(
                    get_backend().send_special_key, self.session_name, self.window_name, key
                )
                await asyncio.sleep(0.2)

            verified = False
            for _ in range(4):
                await asyncio.sleep(0.3)
                fresh = await asyncio.to_thread(
                    get_backend().get_history, self.session_name, self.window_name
                )
                if not isinstance(fresh, str):
                    continue
                recheck = kt.detect_trust_dialog(fresh.split("\n"))
                if recheck is not None and recheck.selected_option == kt.TRUST_OPTION_TRUST:
                    verified = True
                    break
            if not verified:
                raise ProviderError(
                    "Kimi Code workspace-trust dialog did not move to "
                    f"'{kt.TRUST_OPTION_TRUST}' after deterministic navigation; "
                    "refusing to send Enter."
                )

        logger.info(
            "kimi_trust_accepting terminal=%s workspace=%s",
            self.terminal_id,
            dialog.workspace,
        )
        status_monitor.notify_input_sent(self.terminal_id)
        await asyncio.to_thread(
            get_backend().send_special_key, self.session_name, self.window_name, "Enter"
        )
        self._trust_handled = True
        return True

    def _build_kimi_command(self, binary: Optional[str] = None) -> str:
        """Build Kimi CLI command with agent profile and MCP config if provided.

        Returns properly escaped shell command string for tmux send_keys.
        Uses shlex.join() for safe escaping of all arguments.

        Command structure:
            cd <temp_dir> && TERM=xterm-256color kimi --yolo [--agent-file FILE] [--mcp-config JSON]

        The ``cd`` is required because Kimi CLI v1.20.0+ enforces a per-directory
        single-instance lock — only one kimi process can run in a given directory.
        Each provider instance gets its own temp directory to avoid conflicts.

        The ``TERM=xterm-256color`` override is needed because Kimi CLI v1.20.0+
        silently exits when TERM=tmux-256color (the tmux default).

        The --yolo flag auto-approves all tool actions, which is required for
        non-interactive operation in CAO-managed tmux sessions.

        Args:
            binary: Absolute path of the ``kimi`` executable resolved by the
                launch-shell probe. ``initialize()`` always passes it, so the
                launched file is provably the probed file. The default keeps the
                bare ``kimi`` token for direct callers and tests, preserving this
                builder's historical output byte-for-byte.
        """
        command_parts = [binary or "kimi", "--yolo"]

        # Always create a temp directory for this instance.
        # Kimi CLI v1.20.0+ has a per-directory single-instance lock, so each
        # provider instance needs its own working directory.
        temp_dir = self._ensure_temp_dir()

        profile = None
        if self._agent_profile is not None:
            try:
                profile = _with_plugin_mcp(load_agent_profile(self._agent_profile), "kimi_cli")
            except Exception as e:
                raise ProviderError(f"Failed to load agent profile '{self._agent_profile}': {e}")

        # self._model is an explicit per-call override (handoff/assign's own
        # `model` parameter) and wins over the profile's own static model
        # field when both are given; applies even with no profile at all
        # (matches codex.py/hermes.py's own resolution shape).
        resolved_model = self._model or (profile.model if profile else None)
        if resolved_model:
            command_parts.extend(["--model", resolved_model])

        if profile is not None:
            try:
                # Build agent file from profile's system prompt.
                # Kimi uses YAML agent files with a system_prompt_path pointing
                # to a markdown file. We create both in the temp directory.
                system_prompt = profile.system_prompt if profile.system_prompt is not None else ""
                system_prompt = self._apply_skill_prompt(system_prompt)

                # Prepend security constraints for soft enforcement (Kimi CLI has no
                # native tool restriction mechanism). Only applied when tool
                # restrictions are active (not unrestricted "*").
                if self._allowed_tools and "*" not in self._allowed_tools:
                    from cli_agent_orchestrator.constants import SECURITY_PROMPT

                    tools_list = ", ".join(self._allowed_tools)
                    tool_constraint = f"\nYou only have access to these tools: {tools_list}\n"
                    system_prompt = SECURITY_PROMPT + tool_constraint + system_prompt

                if system_prompt:
                    # Write the system prompt as a markdown file
                    prompt_file = os.path.join(temp_dir, "system.md")
                    with open(prompt_file, "w") as f:
                        f.write(system_prompt)

                    # Create the agent YAML that extends the default agent
                    # and points to our custom system prompt file.
                    # Written as plain string to avoid adding PyYAML dependency.
                    agent_yaml = (
                        "version: 1\n"
                        "agent:\n"
                        "  extend: default\n"
                        "  system_prompt_path: ./system.md\n"
                    )
                    agent_file = os.path.join(temp_dir, "agent.yaml")
                    with open(agent_file, "w") as f:
                        f.write(agent_yaml)

                    command_parts.extend(["--agent-file", agent_file])

                # Add MCP server configuration if present in the agent profile.
                # Kimi accepts --mcp-config as a JSON string (repeatable flag).
                if profile.mcpServers:
                    # Set MCP tool call timeout to 600s by modifying ~/.kimi/config.toml
                    # directly. We cannot use --config flag because it causes Kimi CLI
                    # to bypass its default config file, which breaks OAuth authentication
                    # (shows "model: not set" and /login says "restart without --config").
                    # Class-level guard ensures this runs only once per process.
                    self._ensure_mcp_timeout()

                    mcp_config = {}
                    for server_name, server_config in profile.mcpServers.items():
                        if isinstance(server_config, dict):
                            mcp_config[server_name] = dict(server_config)
                        else:
                            mcp_config[server_name] = server_config.model_dump(exclude_none=True)

                        # Resolve the bundled cao-mcp-server console script to a
                        # PATH-independent invocation.
                        mcp_config[server_name] = resolve_mcp_server_config(mcp_config[server_name])

                        # Forward CAO_TERMINAL_ID so MCP servers (e.g. cao-mcp-server)
                        # can identify the current terminal for handoff/assign operations.
                        # Kimi CLI does not automatically forward parent shell env vars
                        # to MCP subprocesses, so we inject it explicitly via the env field.
                        env = mcp_config[server_name].get("env", {})
                        if "CAO_TERMINAL_ID" not in env:
                            env["CAO_TERMINAL_ID"] = self.terminal_id
                            mcp_config[server_name]["env"] = env

                        # Select the declared protocol explicitly rather than
                        # letting FastMCP infer it from the URL path. `cwd` is
                        # untouched -- `StdioMCPServer.cwd` honours it.
                        entry = mcp_config[server_name]
                        declared = entry.get("type")
                        translated = (
                            KIMI_TRANSPORTS.get(declared) if isinstance(declared, str) else None
                        )
                        if translated is not None:
                            entry["transport"] = translated
                            del entry["type"]

                    command_parts.extend(["--mcp-config", json.dumps(mcp_config)])

            except Exception as e:
                raise ProviderError(
                    f"Failed to build kimi command from agent profile "
                    f"'{self._agent_profile}': {e}"
                )

        # cd to unique temp dir (per-directory lock) + set TERM for tmux compatibility
        kimi_cmd = shlex.join(command_parts)
        return f"cd {shlex.quote(temp_dir)} && TERM=xterm-256color {kimi_cmd}"

    @classmethod
    def _ensure_mcp_timeout(cls) -> None:
        """Ensure MCP tool call timeout is set to 600s in ~/.kimi/config.toml.

        Called once per process (guarded by class-level flag). Kimi CLI defaults
        to tool_call_timeout_ms=60000 (60s) for MCP tool calls, which is too short
        for handoff operations. We modify the config file directly instead of using
        ``--config`` CLI flag, because ``--config`` causes Kimi CLI to bypass the
        default config file and breaks OAuth authentication.

        The timeout is NOT restored on cleanup because:
        1. Multiple Kimi instances may share the config file concurrently
        2. 600s is a strictly better default for anyone using MCP tools
        3. Restoring while other instances are running causes race conditions

        issue #494: ``_build_kimi_command`` (the sole caller) now runs inside
        ``asyncio.to_thread``, so N concurrent inits can enter this method in N
        threads at once. ``_KIMI_CONFIG_WRITE_LOCK`` serializes the whole
        check-then-act (class flag + read-modify-write) so only one thread ever
        touches ``config.toml`` at a time -- in-process only: a second
        cao-server process, or the ``kimi`` CLI itself, writing between our read
        and ``os.replace`` is still a last-writer-wins lost update. The write
        itself is atomic (tmp file + ``os.replace``) so a concurrent reader
        (e.g. a ``kimi`` process starting up) never sees a torn/partial file.
        """
        with _KIMI_CONFIG_WRITE_LOCK:
            if cls._mcp_timeout_configured:
                return

            config_path = Path.home() / ".kimi" / "config.toml"
            if not config_path.exists():
                logger.warning(
                    f"Kimi config not found at {config_path}, skipping MCP timeout override"
                )
                cls._mcp_timeout_configured = True
                return

            try:
                content = config_path.read_text()

                # Match the existing timeout line under [mcp.client] section
                # Format: tool_call_timeout_ms = 60000
                pattern = r"(tool_call_timeout_ms\s*=\s*)(\d+)"
                match = re.search(pattern, content)
                if match:
                    current_value = int(match.group(2))
                    if current_value < 600000:
                        new_content = re.sub(pattern, r"\g<1>600000", content)
                        existing_mode = stat.S_IMODE(os.stat(config_path).st_mode)
                        tmp_path = config_path.with_suffix(".toml.tmp")
                        with open(tmp_path, "w") as f:
                            f.write(new_content)
                        os.chmod(tmp_path, existing_mode)
                        os.replace(tmp_path, config_path)
                        logger.info(
                            f"Set MCP tool_call_timeout_ms to 600000 "
                            f"(was {current_value}) in {config_path}"
                        )
                else:
                    logger.warning(
                        f"tool_call_timeout_ms not found in {config_path}, "
                        "MCP tool calls may time out during handoff"
                    )
            except Exception as e:
                logger.warning(f"Failed to set MCP timeout in {config_path}: {e}")

            cls._mcp_timeout_configured = True

    async def _handle_startup_dialog(
        self, idle_gap: Optional[float] = None, outer_timeout: Optional[float] = None
    ) -> None:
        """Dismiss kimi's startup dialogs if they appear.

        Two dialog classes are handled here:

        1. The upgrade-reminder dialog (both dialects): polls the pane for the
           interactive "[s] Skip reminders for version X" menu and answers 's' so
           kimi can proceed to its ready prompt.
        2. The Kimi Code workspace-trust dialog (``KimiDialect.CODE`` only),
           delegated to :meth:`_handle_trust_dialog`, which positively
           identifies the dialog and the folder before sending any key.

        Mirrors ClaudeCodeProvider._handle_startup_prompts (once PR #451 lands):
        exits early if kimi is already ready (no dialog → no delay).

        issue #494: this is a real coroutine, not sync code called from an
        async caller. This method is awaited directly from initialize(), which
        runs on cao-server's single asyncio event loop. Every tmux-backed call
        here (``get_history``/``send_keys``) is a blocking subprocess exec, and
        a plain ``time.sleep`` would block the WHOLE OS thread -- freezing every
        other in-flight request -- for as long as this loop runs. All blocking
        calls are offloaded to a worker thread via ``asyncio.to_thread`` and all
        sleeps are ``asyncio.sleep``, so this coroutine yields the event loop
        instead of freezing it (see PR #451 for the ClaudeCodeProvider fix this
        mirrors, and issue #494 for why this method and its Antigravity/Copilot
        counterparts needed the same fix).

        Idle-gap semantics (see issue #400): a cold or containerized start can
        render the dialog LATE, past the old fixed ~20s window. Rather than a
        total-window budget, ``idle_gap`` is the maximum quiet stretch tolerated
        with no new prompt: answering the dialog resets the idle timer, and the
        loop exits once no prompt appears for ``idle_gap`` seconds (or kimi is
        ready). Total runtime is hard-capped by ``outer_timeout``.

        The idle-gap exit only starts counting once the first dialog has been
        handled -- until then, a first dialog arriving later than ``idle_gap``
        (the scenario issue #400 itself reports) would otherwise be missed: the
        loop would exit at the idle-gap boundary having never seen it. Before
        any dialog is observed, only ``outer_timeout`` can end the loop.

        Args:
            idle_gap: Seconds of no-new-prompt quiet that ends the loop. Defaults
                to the ``startup_prompt_handler_timeout`` setting.
            outer_timeout: Hard cap (seconds) on total handler runtime. Defaults
                to the ``provider_init_timeout`` setting; initialize() passes the
                per-profile-resolved value so a containerized profile's longer
                init budget also governs this handler (mirrors ClaudeCodeProvider).
        """
        if idle_gap is None:
            idle_gap = get_server_settings()["startup_prompt_handler_timeout"]
        if outer_timeout is None:
            outer_timeout = get_server_settings()["provider_init_timeout"]
        outer_deadline = time.monotonic() + outer_timeout
        last_prompt_time = time.monotonic()
        any_prompt_handled = False
        upgrade_dismissed = False
        while True:
            now = time.monotonic()
            if now >= outer_deadline:
                logger.warning("Kimi startup dialog handler hit provider_init_timeout outer cap")
                return
            if any_prompt_handled and now - last_prompt_time >= idle_gap:
                return  # no new prompt within the idle gap — startup settled
            output = await asyncio.to_thread(
                get_backend().get_history, self.session_name, self.window_name
            )
            if output:
                clean_output = re.sub(ANSI_CODE_PATTERN, "", output)
                # Kimi Code raises a workspace-trust dialog for ANY untrusted
                # cwd, even with no project MCP files, and its default selection
                # is "Trust this folder". Answer it only through the positive
                # identification in _handle_trust_dialog — never a blind Enter —
                # and only when the operator has opted in (A3-5); otherwise that
                # method raises instead of granting.
                if self._dialect is KimiDialect.CODE and not self._trust_handled:
                    if await self._handle_trust_dialog(output):
                        any_prompt_handled = True
                        last_prompt_time = time.monotonic()  # reset idle timer
                        await asyncio.sleep(0.5)
                        continue
                # Answer the upgrade dialog once; its text lingers in the buffer
                # after dismissal, so the flag stops a re-answer on later polls.
                if not upgrade_dismissed and re.search(UPGRADE_PROMPT_PATTERN, clean_output):
                    from cli_agent_orchestrator.services.status_monitor import status_monitor

                    logger.info("Kimi upgrade-reminder dialog detected, skipping reminders")
                    status_monitor.notify_input_sent(self.terminal_id)
                    # 's' = "Skip reminders for version X"; single-key menu, no Enter.
                    await asyncio.to_thread(
                        get_backend().send_keys,
                        self.session_name,
                        self.window_name,
                        "s",
                        enter_count=0,
                    )
                    upgrade_dismissed = True
                    any_prompt_handled = True
                    last_prompt_time = time.monotonic()  # reset idle timer
                    await asyncio.sleep(1.0)
                    continue
                # Already at a ready prompt → no dialog to handle, stop early.
                if self.get_status(output) in (
                    TerminalStatus.IDLE,
                    TerminalStatus.COMPLETED,
                ):
                    return
            await asyncio.sleep(1.0)

    async def initialize(self) -> bool:
        """Initialize Kimi CLI provider by starting the kimi command.

        Steps:
        1. Wait for the shell prompt in the tmux window
        2. Build and send the kimi command
        3. Wait for Kimi to reach IDLE state (welcome banner + prompt)

        Returns:
            True if initialization completed successfully

        Raises:
            TimeoutError: If shell or Kimi CLI doesn't start within timeout

        issue #494: ``_build_kimi_command`` does blocking file I/O (mkdtemp,
        writing system.md/agent.yaml, and the ~/.kimi/config.toml
        read-modify-write via ``_ensure_mcp_timeout``) and ``get_backend().
        send_keys`` is a blocking subprocess exec -- both offloaded to a
        worker thread via ``asyncio.to_thread`` for the same reason as
        ``_handle_startup_dialog`` (see its docstring): so nothing in
        initialize() blocks the shared event loop under concurrent session
        creation.
        """
        # Resolve the per-profile provider_init_timeout override (if any) so it
        # governs the startup-dialog handler's outer cap too, mirroring
        # ClaudeCodeProvider. Best-effort: a missing/unloadable profile falls
        # back to the server default here; _build_kimi_command below still
        # raises its own ProviderError on a genuine load failure.
        init_timeout = self.get_init_timeout(self._try_load_profile())
        # The readiness wait (dialog handler + wait_until_status) keeps its
        # existing 120s floor above the server's provider_init_timeout default
        # (60s) -- first-run setup / concurrent launches routinely exceed 60s.
        # A profile override raises this further for containerized launches.
        # Both waits MUST share this value: before this fix the dialog handler
        # capped at the (shorter) server default while wait_until_status used
        # a hardcoded 120s, so a dialog appearing after 60s but before 120s
        # was never dismissed.
        ready_timeout = max(120.0, init_timeout)

        # Wait for shell prompt to appear in the tmux window
        if not await wait_for_shell(self.terminal_id, timeout=init_timeout):
            raise TimeoutError(f"Shell initialization timed out after {init_timeout}s")

        # Resolve the dialect from the launch shell's own `kimi`. This runs
        # BEFORE the launch because the two dialects need different commands
        # (different MCP mechanism, different agent file format, different
        # cwd contract). A failure here raises UnsupportedKimiError and the
        # terminal is not launched at all — never a silent legacy fallback.
        probe = await self._resolve_dialect()

        # Build properly escaped command string
        if probe.dialect is KimiDialect.CODE:
            command = await asyncio.to_thread(self._build_kimi_code_command)
        else:
            command = await asyncio.to_thread(self._build_kimi_command, probe.binary)

        # The launch line is POSIX text that quotes the operator's own paths,
        # model names and resolved binary. Hand it to /bin/sh via a CAO-owned
        # script so the pane's shell — which may be fish, zsh, or anything else —
        # never has to parse POSIX quoting it may not share.
        command = await asyncio.to_thread(self._materialize_launch_command, command)

        # Send Kimi command to the tmux window
        await asyncio.to_thread(
            get_backend().send_keys, self.session_name, self.window_name, command
        )

        # Dismiss the startup upgrade-reminder dialog before waiting for ready:
        # unanswered it blocks kimi from reaching its prompt (init would time out).
        await self._handle_startup_dialog(outer_timeout=ready_timeout)

        # Wait for Kimi CLI to reach IDLE or COMPLETED state (prompt visible).
        # Accept both IDLE and COMPLETED — some CLI versions show a startup
        # message that get_status() interprets as a completed response.
        if not await wait_until_status(
            self.terminal_id,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=ready_timeout,
            polling_interval=1.0,
        ):
            raise TimeoutError(f"Kimi CLI initialization timed out after {ready_timeout} seconds")

        self._initialized = True
        return True

    def _spinner_semantics(self) -> "kt.SpinnerSemantics":
        """The spinner rules for the dialect this terminal resolved to.

        A0 measured that a bare moon phase means "working" on the legacy TUI but
        appears only in Kimi Code's *idle* rotating tip, where the live indicator
        is braille. Reading a moon as work under Kimi Code would pin a settled
        terminal at PROCESSING (and would drop a legitimate `🌕` line out of an
        answer), so the two dialects must not share one rule.

        Falls back to the legacy rules when no dialect has been resolved yet —
        the same default the classifier uses, so an un-probed terminal behaves
        exactly as it did before this change.
        """
        if self._dialect is KimiDialect.CODE:
            return kt.SpinnerSemantics.CODE
        return kt.SpinnerSemantics.LEGACY

    def get_status(self, output: str) -> TerminalStatus:
        """Get Kimi CLI status by analyzing terminal output.

        Status detection logic:
        1. Strip ANSI codes for reliable text matching
        2. Latch ``_has_received_input`` when user input box (╭─) is detected
        3. Check bottom N lines for the idle prompt pattern
        4. If prompt found + input was received → COMPLETED
        5. If prompt found + no input yet → IDLE
        6. If no prompt: agent is PROCESSING (streaming response)
        7. Check for ERROR patterns as fallback

        The latching flag approach is necessary because:
        - Long responses (>200 lines) push the user input box out of the
          tmux capture window, so checking for ╭─ on every call is unreliable
        - Not all responses use ``•`` bullets (structured output like tables,
          numbered lists, report templates have no bullet markers at all)
        - The flag is set during the PROCESSING phase when the user input box
          IS still visible in the capture, and persists through completion

        Args:
            output: Terminal output buffer (rolling buffer, up to
                ``state_buffer_max`` bytes -- server setting, 32KB default)

        Returns:
            TerminalStatus indicating current state
        """
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

        # Strip the RAW pipe-pane escapes (cursor positioning, in-place redraws),
        # not just SGR colour codes, so the bottom-anchored prompt/processing
        # checks see clean, line-oriented text on the raw stream.
        clean_output = strip_terminal_escapes(output)

        # --- Newest "Kimi Code" TUI (redesigned CLI) ---
        # This build has no bare ✨/💫 prompt; readiness is the bottom status bar
        # ("agent (<model> ●)") / "context: N%" footer with the empty "── input ──"
        # box, and a turn-in-flight is a braille spinner ("⠧ Thinking… Ns · N
        # tokens") that is cleared on completion. Gate on the new-TUI markers so
        # legacy (emoji-prompt) builds keep the path below unchanged.
        if re.search(NEW_TUI_STATUS_PATTERN, clean_output):
            # A response bullet appears only once a turn produces output
            # (thinking or response); the welcome banner / update nag have none.
            # Latch it so a long response that scrolls the bullets out of the
            # rolling buffer still reads COMPLETED rather than IDLE. Crucially,
            # nothing latches at init, so a freshly-launched terminal reads IDLE
            # (not COMPLETED), avoiding a premature-completion race when the
            # first task is sent.
            #
            # The shared helper requires a bullet *plus a payload*, so a wrapped
            # status-bar fragment (`●)`) does not latch a terminal that never
            # received input (A3-1).
            if kt.has_response_marker(clean_output):
                self._has_received_input = True

            # PROCESSING vs ready. A spinner-vs-status-bar position compare is
            # unreliable here: the TUI renders the live spinner BETWEEN the
            # "── input ──" rule and the status bar and repaints the status
            # bar with every frame, so the ready chrome is the freshest
            # content even mid-turn (observed: a supervisor turn flapping
            # completed↔processing 29 times in one 57KB stream, which the
            # StatusMonitor ready-latch then pinned at a false COMPLETED).
            # Two in-flight signals, validated by replaying captured live
            # streams; either one means a turn is running:
            # - spinner glyph (braille/moon, incl. tool-call lines like
            #   "⠹ Using handoff({…})") within the freshest tail lines —
            #   frames land every ~100ms while the agent works, and the
            #   turn-finished repaint (input rule + ~12 blank box lines +
            #   separator + status bar + context footer) pushes stale frames
            #   beyond this window;
            # - the last spinner glyph rendered AFTER the last "•" bullet —
            #   catches chunk boundaries mid-repaint where streamed thinking
            #   text has temporarily pushed the spinner out of the tail
            #   window (a finished turn always ends with bullets as the
            #   freshest non-chrome content).
            lines = clean_output.splitlines()
            semantics = self._spinner_semantics()
            last_spinner = max(
                (i for i, line in enumerate(lines) if _is_live_turn_spinner_line(line, semantics)),
                default=-1,
            )
            last_bullet = max(
                (i for i, line in enumerate(lines) if kt.is_response_marker_line(line)),
                default=-1,
            )
            spinner_in_tail = last_spinner >= 0 and last_spinner >= len(lines) - 15
            if spinner_in_tail or last_spinner > last_bullet:
                return TerminalStatus.PROCESSING

            # Dispatch grace: for a few seconds after send_input(), trust the
            # dispatch over the chrome. The paste repaints the status bar
            # (ready chrome lands LAST in the stream) before the turn's first
            # spinner frame, so the checks above briefly read "ready" ~100ms
            # into a new turn — and the StatusMonitor ready-latch would pin
            # that false COMPLETED until the next input.
            if self._last_dispatch_time and time.time() - self._last_dispatch_time < 5.0:
                return TerminalStatus.PROCESSING

            # The stream looks ready — confirm against the RENDERED pane.
            # A ready-looking chunk boundary is byte-identical mid-turn vs at
            # real completion (measured on captured streams: stale spinner
            # ~21 lines back, bullets 2-3 from the end in BOTH), so the raw
            # stream alone cannot split them. The rendered pane can: tmux's
            # compositor has resolved every in-place redraw, so a spinner
            # glyph visible in the pane tail is live, not stale. Gated to
            # post-dispatch only (boot screens legitimately show braille
            # like '⠧ MCP Servers: 0/1' while idle at the welcome screen,
            # and init readiness is already handled by the stream path).
            if self._last_dispatch_time:
                try:
                    pane_tail = get_backend().get_history(
                        self.session_name,
                        self.window_name,
                        tail_lines=25,
                        strip_escapes=True,
                    )
                    if any(
                        _is_live_turn_spinner_line(line, semantics)
                        for line in pane_tail.splitlines()
                    ):
                        return TerminalStatus.PROCESSING
                except Exception:
                    # Pane unavailable (deleted window, backend hiccup) —
                    # fall through to the stream-derived ready status.
                    pass

            if re.search(ERROR_PATTERN, clean_output, re.MULTILINE):
                return TerminalStatus.ERROR

            return TerminalStatus.COMPLETED if self._has_received_input else TerminalStatus.IDLE

        # --- Legacy emoji-prompt TUI ---
        # Check the bottom lines for the idle prompt.
        # Kimi's TUI has padding lines between prompt and status bar.
        # Use end-of-line anchor (\s*$) to distinguish a bare prompt ("user@dir💫")
        # from a prompt with user input after it ("user@dir💫 some text"),
        # which appears when the user has typed a command.
        all_lines = clean_output.strip().splitlines()
        bottom_lines = all_lines[-IDLE_PROMPT_TAIL_LINES:]
        idle_prompt_eol = IDLE_PROMPT_PATTERN + r"\s*$"
        has_idle_prompt = any(re.search(idle_prompt_eol, line) for line in bottom_lines)

        # Latch: detect user input to distinguish IDLE from COMPLETED.
        # Supports two formats:
        #
        # Pre-v1.20.0: User input in bordered box (╭─...╰─).
        #   - During PROCESSING (no idle prompt): any ╭─ means user input
        #   - During IDLE/COMPLETED: count ╰─ occurrences (welcome banner = 1, input = 2+)
        #
        # v1.20.0+: User input on prompt line (``💫 message text``).
        #   - Detect prompt emoji followed by non-whitespace text
        if not self._has_received_input:
            # v1.20.0+: prompt line with text after the emoji
            if re.search(PROMPT_WITH_INPUT_PATTERN, clean_output):
                self._has_received_input = True
            # Pre-v1.20.0: input box detection
            elif not has_idle_prompt:
                if re.search(USER_INPUT_BOX_START_PATTERN, clean_output):
                    self._has_received_input = True
            else:
                box_end_count = len(re.findall(USER_INPUT_BOX_END_PATTERN, clean_output))
                if box_end_count >= 2:
                    self._has_received_input = True

        if has_idle_prompt:
            if self._has_received_input:
                # Guard against premature COMPLETED: if processing indicators are
                # visible in the bottom lines, Kimi is still working even though
                # the idle prompt is present. This happens when get_status() is
                # polled in the brief window between task submission and Kimi
                # clearing the prompt to start streaming.
                for line in bottom_lines:
                    stripped = line.strip()
                    # Braille spinner with tool name: "⠼ Using Shell (...)"
                    if re.search(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s+Using\s", stripped):
                        return TerminalStatus.PROCESSING
                    # Moon phase emoji alone on a line = thinking indicator
                    if stripped in {"🌑", "🌒", "🌓", "🌔", "🌕", "🌖", "🌗", "🌘"}:
                        return TerminalStatus.PROCESSING
                return TerminalStatus.COMPLETED

            return TerminalStatus.IDLE

        # No idle prompt at bottom — check for errors before assuming processing
        if re.search(ERROR_PATTERN, clean_output, re.MULTILINE):
            return TerminalStatus.ERROR

        # No prompt visible and no error: Kimi is actively processing/streaming
        return TerminalStatus.PROCESSING

    # Opt in to pyte rendered-screen detection (gated by CAO_PYTE_STATUS).
    supports_screen_detection = True

    def get_status_from_screen(self, screen_lines: List[str]) -> TerminalStatus:
        """Detect status from a pyte-composited viewport (escape-free rows).

        The composited screen removes the need for the raw-stream hacks the
        buffer path carries (the get_history re-capture and the dispatch-grace
        window): a spinner visible in the rendered pane tail is unambiguously
        live, and the response bullets are present without eviction. Called by
        the StatusMonitor only on settled / rising-edge frames.
        """
        rows = [ln.rstrip() for ln in screen_lines if ln.strip()]
        if not rows:
            return TerminalStatus.UNKNOWN
        joined = "\n".join(rows)
        tail = rows[-18:]

        # Boot gate: Kimi draws its status bar BEFORE it can accept input —
        # while MCP servers are still connecting it shows "connecting to mcp
        # servers" / "cao-mcp-server (connecting)". Reporting IDLE here is
        # premature: a message delivered in this window is pasted into the boot
        # screen and silently absorbed (observed live — an inbox message
        # delivered 1.3s after a premature IDLE left the receiver stuck). Treat
        # the connecting state as PROCESSING so init waits for a real ready
        # prompt.
        #
        # Two things changed here (A3-2 / A3-3):
        #
        # * The row shape is matched STRUCTURALLY by the shared classifier
        #   (`MCP_BOOT_ROW_RE`, whole-row anchored) instead of by a substring
        #   search. A substring search gated on plain assistant prose —
        #   "connecting to mcp servers is only a phrase" is an answer line.
        # * Response rows are excluded through the shared bullet semantics,
        #   which cover both dialects' glyphs. The previous `re.match(r"\s*•")`
        #   excluded only the legacy `•`, so a Kimi Code answer whose `●` row
        #   mentioned the boot chrome re-stranded a genuinely COMPLETED terminal
        #   at PROCESSING on every settled frame — and since the boot gate
        #   precedes the ready check, the inbox (delivers only on IDLE/COMPLETED)
        #   then never delivered to that terminal.
        if any(kt.MCP_BOOT_ROW_RE.match(ln) for ln in rows if not kt.is_response_marker_line(ln)):
            return TerminalStatus.PROCESSING

        # Newest "Kimi Code" TUI: readiness is the status bar / context footer.
        if re.search(NEW_TUI_STATUS_PATTERN, joined):
            semantics = self._spinner_semantics()
            if any(_is_live_turn_spinner_line(ln, semantics) for ln in tail):
                return TerminalStatus.PROCESSING
            if re.search(ERROR_PATTERN, joined, re.MULTILINE):
                return TerminalStatus.ERROR
            return (
                TerminalStatus.COMPLETED if kt.has_response_marker(joined) else TerminalStatus.IDLE
            )

        # Legacy emoji-prompt TUI: bare ✨/💫 prompt visible at the bottom.
        if any(re.search(IDLE_PROMPT_PATTERN, ln) for ln in tail):
            return (
                TerminalStatus.COMPLETED if kt.has_response_marker(joined) else TerminalStatus.IDLE
            )

        if re.search(ERROR_PATTERN, joined, re.MULTILINE):
            return TerminalStatus.ERROR
        # No Kimi TUI chrome on the composited screen at all (boot screen, or a
        # torn-down pane back at the shell). On the RAW path "no prompt = still
        # streaming" is a safe default, but on a fully rendered screen the
        # absence of all TUI chrome means we are NOT looking at an active Kimi
        # turn — so report UNKNOWN rather than a false PROCESSING.
        return TerminalStatus.UNKNOWN

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract Kimi's final response from terminal output.

        Supports two formats:

        Pre-v1.20.0 (input box format):
        1. Find the last user input box (╭─...╰─) in clean text
        2. Collect all content between the box end and the next prompt
        3. Filter out thinking bullets (gray ANSI-styled lines)

        v1.20.0+ (inline prompt format):
        1. Find the last prompt-with-input line (``💫 message text``)
        2. Collect all content between that line and the next bare prompt
        3. Filter out thinking bullets

        Fallback for long responses (markers scrolled out of capture):
        - Extract all content from start of capture up to the idle prompt
        - Filter out thinking/status bar lines

        Args:
            script_output: Raw terminal output from tmux capture

        Returns:
            Extracted response text with ANSI codes stripped

        Raises:
            ValueError: If no response content can be extracted
        """
        clean_output = re.sub(ANSI_CODE_PATTERN, "", script_output)

        # Work line-by-line for reliable mapping between raw and clean output.
        raw_lines = script_output.split("\n")
        clean_lines = clean_output.split("\n")

        # Strategy 0 (layout-driven, Kimi Code): the newest TUI renders the
        # transcript ABOVE a persistent composer, so "everything after the last
        # input box" anchors on the composer and extracts the STATUS FOOTER as
        # the answer. Measured on the 0.43.1 captures: the legacy rule returns
        # `A0 Gemini 2.5 Flash thinking  <dir>  master [±]  shift-tab to Plan
        # mode …` — 173 characters of pure chrome — for a completed turn whose
        # real answer is `STEP 1 … A0-FIXTURE-DONE.`.
        #
        # The layout is self-describing, so the region is located from the
        # classifier's own row kinds instead of from box-drawing positions:
        # start after the last USER_INPUT echo (its wrapped continuation rows
        # included), end where the composer, a dialog, or the status footer
        # begins. Returns None when the pane shows no echo at all (legacy
        # emoji-prompt TUIs, boot screens), which keeps the historical rules
        # below in sole charge of those shapes.
        #
        # Kinds are computed ONCE for the whole capture by the shared
        # sequence-aware classifier. The extractor must not re-derive context
        # from a slice: a tool-payload row carries no marker of its own, so the
        # header that identifies it has to be in scope (D6).
        row_kinds = kt.classify_rows(raw_lines, clean_lines, self._spinner_semantics())
        layout_region = self._locate_response_region(row_kinds)
        if layout_region is not None:
            layout_start, layout_end = layout_region
            return self._collect_response_text(
                raw_lines, clean_lines, layout_start, layout_end, row_kinds
            )

        # Strategy 1: Find the last user input box end line (╰─) — pre-v1.20.0
        box_end_idx = None
        # Only consider box-end lines that come AFTER the welcome banner.
        # The welcome banner itself has ╰─, so we skip it by finding the
        # welcome banner line first.
        welcome_idx = 0
        for i, line in enumerate(clean_lines):
            if re.search(WELCOME_BANNER_PATTERN, line):
                welcome_idx = i
        for i in range(welcome_idx + 1, len(clean_lines)):
            if re.search(USER_INPUT_BOX_END_PATTERN, clean_lines[i]):
                box_end_idx = i

        # Strategy 2: Find the last prompt-with-input line — v1.20.0+
        prompt_input_idx = None
        for i, line in enumerate(clean_lines):
            if re.search(PROMPT_WITH_INPUT_PATTERN, line):
                prompt_input_idx = i

        # Choose the best anchor: the LATEST marker wins. The newest "Kimi
        # Code" TUI draws decorative ╰─ boxes during boot (its own welcome box
        # and MCP-server banners like FastMCP's) but renders user messages as
        # ✨-prefixed prompt lines — so a box-end can match boot chrome ABOVE
        # the real message and box-first priority would slice the response
        # from the boot screen. The response always follows the LAST user
        # input, whichever marker style rendered it.
        if box_end_idx is not None and prompt_input_idx is not None:
            response_start = max(box_end_idx, prompt_input_idx) + 1
        elif box_end_idx is not None:
            response_start = box_end_idx + 1
        elif prompt_input_idx is not None:
            response_start = prompt_input_idx + 1
        else:
            # Neither marker found — long response scrolled everything out
            return self._extract_without_input_box(raw_lines, clean_lines, row_kinds)

        # Find where the response ends: the next bare idle prompt
        # (legacy/v1.20 TUIs), or the newest-TUI footer chrome — the
        # "── input ──" box rule or the status bar / context footer
        # (NEW_TUI_STATUS_PATTERN). Without the footer stops, a newest-TUI
        # response would run to end-of-capture and drag the empty input box
        # and status bar into the extracted message.
        idle_prompt_eol = IDLE_PROMPT_PATTERN + r"\s*$"
        new_tui_input_rule = r"^\s*─{2,}\s*input\s*─{2,}"
        prompt_idx = len(clean_lines)  # default: end of output
        for i in range(response_start, len(clean_lines)):
            line = clean_lines[i]
            if (
                re.search(idle_prompt_eol, line)
                or re.match(new_tui_input_rule, line)
                or re.search(NEW_TUI_STATUS_PATTERN, line)
            ):
                prompt_idx = i
                break

        response_end = prompt_idx

        # Collect all non-empty lines for the fallback response
        all_response_lines = [
            clean_lines[i].strip()
            for i in range(response_start, response_end)
            if i < len(clean_lines) and clean_lines[i].strip()
        ]

        if not all_response_lines:
            raise OutputExtractionError(
                "Empty Kimi CLI response - no content found after the input marker"
            )

        return self._collect_response_text(
            raw_lines, clean_lines, response_start, response_end, row_kinds
        )

    @staticmethod
    def _locate_response_region(
        kinds: List[kt.KimiLineKind],
    ) -> Optional[Tuple[int, int]]:
        """Locate the response region from row kinds alone.

        Returns ``(start, end)`` as a half-open slice, or ``None`` when the pane
        carries no user-input echo (in which case there is nothing to anchor on
        and the caller falls back to the historical box/prompt rules).

        The end anchor is the first row after the echo that belongs to the
        ready frame, a dialog, or the footer — in the Kimi Code layout the
        composer sits *below* the transcript, so the transcript's own end is
        exactly where that chrome starts. Falling through to end-of-capture
        keeps a pane whose composer was pushed out of the capture window
        working.
        """

        echo_idx = -1
        for index, kind in enumerate(kinds):
            if kind is kt.KimiLineKind.USER_INPUT:
                echo_idx = index
        if echo_idx < 0:
            return None

        end_anchors = (
            kt.KimiLineKind.READY_INPUT_FRAME,
            kt.KimiLineKind.APPROVAL_DIALOG,
            kt.KimiLineKind.TRUST_DIALOG,
            kt.KimiLineKind.STATUS_FOOTER,
            kt.KimiLineKind.BOOT_CHROME,
        )
        for index in range(echo_idx + 1, len(kinds)):
            if kinds[index] in end_anchors:
                return echo_idx + 1, index
        return echo_idx + 1, len(kinds)

    def _classify_response_region(
        self,
        raw_lines: List[str],
        clean_lines: List[str],
        start: int,
        end: int,
        kinds: Optional[List[kt.KimiLineKind]] = None,
    ) -> Tuple[List[str], List[kt.KimiLineKind]]:
        """Classify the candidate rows of a response region.

        Returns the assistant-visible text (only rows the shared classifier
        places in ``ANSWER_KINDS``) alongside every non-blank row's kind, so the
        caller can tell "nothing was recognised" apart from "everything was
        reasoning".

        ``kinds`` is the whole-capture result of the shared sequence-aware
        classifier (``classify_rows``); the region is a *slice* of it. Passing
        the full-sequence kinds is what lets a tool payload row inside the
        region be recognised as payload rather than as an indented prose
        continuation. It is computed here only when the caller had no
        whole-capture kinds to give.
        """

        if kinds is None:
            kinds = kt.classify_rows(raw_lines, clean_lines, self._spinner_semantics())

        answers: List[str] = []
        region_kinds: List[kt.KimiLineKind] = []
        for i in range(start, end):
            clean_line = clean_lines[i] if i < len(clean_lines) else ""
            kind = kinds[i] if i < len(kinds) else kt.KimiLineKind.BLANK
            if kind is kt.KimiLineKind.BLANK:
                continue
            region_kinds.append(kind)
            if kind in kt.ANSWER_KINDS:
                answers.append(clean_line.strip())
        return answers, region_kinds

    @staticmethod
    def _reject_thinking_only(kinds: List[kt.KimiLineKind]) -> None:
        """Fail closed when a turn produced nothing but reasoning.

        The previous behaviour here was to return the unfiltered reasoning text
        as the "response" whenever every candidate row looked like thinking.
        That is a leak: CAO hands the extracted string to handoff/assign callers
        and to the memory layer, so a turn whose final answer was never rendered
        (or whose marker changed between Kimi releases) would silently publish
        the model's private reasoning as its answer. Raising is the only safe
        outcome — the caller sees an extraction failure instead of a plausible
        but wrong message.

        Raised as :class:`OutputExtractionRejected`, not the retryable
        :class:`OutputExtractionError`: this is a deliberate refusal about
        content that was found, so it must not be retried and must never be
        replaced by the raw-transcript fallback, which contains the reasoning
        that was just refused.
        """

        if kinds and all(kind is kt.KimiLineKind.THINKING_BULLET for kind in kinds):
            raise OutputExtractionRejected(
                "Kimi returned only reasoning output for this turn: every candidate "
                "line was classified as a thinking bullet, so there is no final "
                "answer to extract. Refusing to return reasoning text as the response."
            )

    def _collect_response_text(
        self,
        raw_lines: List[str],
        clean_lines: List[str],
        start: int,
        end: int,
        kinds: Optional[List[kt.KimiLineKind]] = None,
    ) -> str:
        """Filter a response region down to its assistant-visible text."""

        answers, region_kinds = self._classify_response_region(
            raw_lines, clean_lines, start, end, kinds
        )
        self._reject_thinking_only(region_kinds)
        if not answers:
            raise OutputExtractionRejected(
                "No extractable content in Kimi CLI output: every candidate line in "
                "the response region was TUI chrome, a user echo, or reasoning."
            )
        return "\n".join(answers).strip()

    def _extract_without_input_box(
        self,
        raw_lines: list,
        clean_lines: list,
        kinds: Optional[List[kt.KimiLineKind]] = None,
    ) -> str:
        """Fallback extraction when user input box has scrolled out of capture.

        For long responses (>200 lines), the user input box (╭─/╰─) and early
        response content are no longer in the tmux capture window. In this case,
        extract all content from the start of capture up to the last idle prompt,
        filtering out status bar and welcome banner lines.

        Args:
            raw_lines: Raw output split by newlines (ANSI preserved)
            clean_lines: ANSI-stripped output split by newlines
            kinds: whole-capture result of the shared sequence-aware classifier

        Returns:
            Extracted response text

        Raises:
            ValueError: If no extractable content found
        """
        # Find the last idle prompt line
        prompt_idx = len(clean_lines)
        for i in range(len(clean_lines) - 1, -1, -1):
            if re.search(IDLE_PROMPT_PATTERN, clean_lines[i]):
                prompt_idx = i
                break

        # Collect content from start to prompt through the shared classifier, so
        # thinking filtering here cannot drift from the main extraction path.
        answers, region_kinds = self._classify_response_region(
            raw_lines, clean_lines, 0, prompt_idx, kinds
        )
        self._reject_thinking_only(region_kinds)

        if not answers:
            # Reached only when no input marker was found anywhere in the capture,
            # so this is the "capture too shallow / marker missing" case, not a
            # refusal about content that was found: it stays retryable.
            raise OutputExtractionError(
                "No extractable content in Kimi CLI output (input box scrolled out): "
                "every candidate line was TUI chrome, a user echo, or reasoning."
            )

        return "\n".join(answers).strip()

    def exit_cli(self) -> str:
        """Get the command to exit Kimi CLI.

        Kimi CLI supports several exit commands: /exit, exit, quit, or Ctrl-D.
        We use /exit as it's the most reliable and consistent.
        """
        return "/exit"

    async def extract_session_context(self) -> Dict[str, Any]:
        """Tmux-primary session extraction for Kimi.

        Mirrors the universal pattern used by the other providers
        (Claude Code / Codex / Kiro / Copilot). Returns the locked
        6-field shape from ``_build_context_dict``. Empty tmux history
        returns the LITERAL empty dict ``{}``. All
        emitted strings flow through ``_sanitize_for_log`` at this
        producer layer (sanitised at both produce and consume). Never raises
        out — top-level ``except Exception`` returns ``{}`` with a
        sanitised WARNING. ``KeyboardInterrupt`` and ``SystemExit``
        propagate.
        """
        from cli_agent_orchestrator.services.wiki_compiler import _sanitize_for_log

        try:
            output = get_backend().get_history(self.session_name, self.window_name)
            if not output:
                return {}  # literal empty dict, not a populated-empty one

            clean = re.sub(ANSI_CODE_PATTERN, "", output)

            user_messages: list = []
            lines = clean.splitlines()
            i = 0
            while i < len(lines):
                m = self._KIMI_PROMPT_RE.search(lines[i])
                if not m:
                    i += 1
                    continue
                msg_lines: list = []
                # Text after the prompt emoji on the same line.
                after = lines[i][m.end() - 1 :].strip()
                if after:
                    msg_lines.append(after)
                i += 1
                while i < len(lines):
                    if self._KIMI_PROMPT_RE.search(lines[i]) or self._KIMI_RESPONSE_MARKER_RE.match(
                        lines[i]
                    ):
                        break
                    if lines[i].strip():
                        msg_lines.append(lines[i].strip())
                    i += 1
                if msg_lines:
                    user_messages.append(" ".join(msg_lines))

            last_response = ""
            try:
                last_response = self.extract_last_message_from_script(output)
            except ValueError:
                pass

            return self._build_context_dict(
                provider_name="kimi_cli",
                last_task=_sanitize_for_log(user_messages[-1] if user_messages else ""),
                key_decisions=[
                    _sanitize_for_log(s) for s in self._extract_decisions(last_response)
                ],
                open_questions=[
                    _sanitize_for_log(s) for s in self._extract_questions(user_messages)
                ],
                files_changed=[_sanitize_for_log(s) for s in self._extract_file_paths(clean)],
            )
        except (KeyboardInterrupt, SystemExit):
            # Control flow MUST propagate.
            raise
        except Exception as e:
            logger.warning(
                "kimi_extract_session_context_failed reason=%s",
                _sanitize_for_log(str(e))[:200],
            )
            return {}

    def cleanup(self) -> None:
        """Clean up Kimi CLI provider resources.

        Removes any temporary files created for agent profiles
        and resets the initialization state. MCP timeout is NOT restored
        because multiple Kimi instances may share the config file concurrently.
        """
        # Remove temp directory if it was created for agent profile
        if self._temp_dir:
            if os.path.exists(self._temp_dir):
                shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = None

        self._initialized = False
        self._has_received_input = False
