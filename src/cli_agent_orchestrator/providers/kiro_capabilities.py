"""Capability probing and command construction for the Kiro wrapper.

Phase 0 intentionally keeps this module independent from terminal allocation.
The probe uses bounded root help, chat help, and version commands; callers must
complete it before creating a backend session, database row, or provider.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Sequence

from cli_agent_orchestrator.models.kiro_engine import KiroEngine

ProbeRunner = Callable[..., subprocess.CompletedProcess[str]]

_FLAG_CAPABILITIES = {
    "profile": "--agent",
    "model": "--model",
    "ui": "--legacy-ui",
    "trust": "--trust-all-tools",
    "mcp_startup": "--require-mcp-startup",
}
_BARE_BOOLEAN_FLAGS = frozenset(
    {
        "--v3",
        "--legacy-ui",
        "--trust-all-tools",
        "--require-mcp-startup",
    }
)
_VALUE_BEARING_FLAGS = frozenset({"--agent", "--model"})
_REQUESTED_FLAGS = frozenset(
    {
        "--agent-engine",
        *_BARE_BOOLEAN_FLAGS,
        *_VALUE_BEARING_FLAGS,
    }
)
_VERSION_RE = re.compile(r"(?:kiro-cli\s+(?:version\s*)?|version\s+)([0-9][^\s]*)", re.I)
_AGENT_ENGINE_TOKEN = r"[A-Za-z0-9][A-Za-z0-9_-]*"
_AGENT_ENGINE_VALUES = rf"{_AGENT_ENGINE_TOKEN}(?:\s*[|,]\s*{_AGENT_ENGINE_TOKEN})*"
_BARE_AGENT_ENGINE_TOKEN = r"v[0-9]+"
_BARE_AGENT_ENGINE_VALUES = rf"{_BARE_AGENT_ENGINE_TOKEN}(?:\s*[|,]\s*{_BARE_AGENT_ENGINE_TOKEN})*"
_OPTION_NAME = r"--[A-Za-z0-9][\w-]*"
_METAVARIABLE = r"(?:<[A-Za-z][A-Za-z0-9_-]*>|\[[A-Za-z][A-Za-z0-9_-]*\]|[A-Z][A-Z0-9_-]*)"
_AGENT_ENGINE_DECLARATION_RE = re.compile(
    rf"""
    ^\s*(?:-[A-Za-z0-9](?:,\s+|\s+))?--agent-engine
    \s+
    (?P<values>
        <{_AGENT_ENGINE_VALUES}>
        |\[{_AGENT_ENGINE_VALUES}\]
        |\{{{_AGENT_ENGINE_VALUES}\}}
        |{_BARE_AGENT_ENGINE_VALUES}
    )
    \s*$
    """,
    re.X,
)
_POSSIBLE_VALUES_RE = re.compile(
    rf"""
    \[?possible\s+values:\s*
    (?P<values>{_AGENT_ENGINE_TOKEN}(?:\s*[|,]\s*{_AGENT_ENGINE_TOKEN})*)
    \]?
    \s*$
    """,
    re.I | re.X,
)
_AGENT_ENGINE_VALUE_DECLARATION_RE = re.compile(
    rf"(?:<{_AGENT_ENGINE_VALUES}>|\[{_AGENT_ENGINE_VALUES}\]|\{{{_AGENT_ENGINE_VALUES}\}}|{_BARE_AGENT_ENGINE_VALUES})"
)
_METAVARIABLE_RE = re.compile(rf"{_METAVARIABLE}")
_OPTION_TOKEN_RE = re.compile(
    rf"(?P<flag>{_OPTION_NAME})(?P<variadic>\.\.\.)?(?:\s+(?P<value>\S+))?"
)
_MALFORMED_AGENT_ENGINE_VALUES_RE = re.compile(
    r"(?:[<\[{][A-Za-z0-9_|, -]*|v[0-9]+(?:\s*[|,]\s*v[0-9]+)*[/]?)$"
)


class KiroCapabilityError(ValueError):
    """A structured, actionable Kiro wrapper capability failure."""

    def __init__(
        self,
        kind: str,
        engine: KiroEngine,
        message: str,
        *,
        capability: Optional[str] = None,
        version: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.engine = engine
        self.capability = capability
        self.version = version


class KiroPhase0KASError(ValueError):
    """Raised after KAS capability probing, before runtime allocation."""

    def __init__(self, profile_has_v2_policy: bool) -> None:
        profile_note = (
            " The selected profile contains v2 allowedTools/toolsSettings that "
            "cannot be translated to Cedar in Phase 0."
            if profile_has_v2_policy
            else ""
        )
        super().__init__(
            "Kiro engine 'kas' is not available in Phase 0: KAS profiles and Cedar "
            "policy translation are not implemented. Retry with engine 'v2'." + profile_note
        )
        self.engine = KiroEngine.KAS


@dataclass(frozen=True)
class KiroCapabilities:
    """Wrapper capabilities discovered for one creation attempt."""

    version: Optional[str]
    flags: frozenset[str]
    agent_engines: frozenset[str] = field(default_factory=frozenset)

    def supports(self, flag: str) -> bool:
        return flag in self.flags

    def supports_agent_engine(self, engine: KiroEngine) -> bool:
        """Return whether wrapper help explicitly advertises the requested selector value."""
        return engine.value in self.agent_engines


def _run(
    runner: ProbeRunner,
    command: Sequence[str],
    timeout: float,
    engine: KiroEngine,
    *,
    stdin: Optional[int] = None,
) -> subprocess.CompletedProcess[str]:
    """Run one bounded probe command and normalize execution failures."""
    try:
        kwargs: dict[str, object] = {
            "capture_output": True,
            "text": True,
            "timeout": timeout,
        }
        if stdin is not None:
            kwargs["stdin"] = stdin
        result = runner(list(command), **kwargs)
    except FileNotFoundError as exc:
        raise KiroCapabilityError(
            "missing_executable",
            engine,
            "Kiro engine "
            f"'{engine.value}' cannot start because 'kiro-cli' was not found. "
            "Install Kiro CLI or select another provider.",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise KiroCapabilityError(
            "timeout",
            engine,
            f"Kiro capability probe for engine '{engine.value}' timed out after {timeout}s.",
        ) from exc

    output = (result.stdout or "") + "\n" + (result.stderr or "")
    if result.returncode != 0 or not output.strip():
        raise KiroCapabilityError(
            "malformed_output",
            engine,
            f"Kiro capability probe for engine '{engine.value}' returned unusable help output.",
        )
    return result


def _is_formal_option_value(flag: str, value: Optional[str]) -> bool:
    """Return whether one option value has a recognized help-declaration form."""
    if value is None:
        return True
    if _METAVARIABLE_RE.fullmatch(value):
        return True
    if flag == "--agent-engine" and (
        _AGENT_ENGINE_VALUE_DECLARATION_RE.fullmatch(value)
        or _MALFORMED_AGENT_ENGINE_VALUES_RE.fullmatch(value)
    ):
        # Retain the selector's structured unsupported-v2 error path for
        # malformed engine-value declarations.
        return True
    return False


def _supports_declared_invocation(flag: str, variadic: bool, value: Optional[str]) -> bool:
    """Return whether CAO's exact invocation is compatible with one declaration."""
    if flag not in _REQUESTED_FLAGS:
        return True
    if variadic:
        return False
    if flag in _BARE_BOOLEAN_FLAGS:
        # ``[VALUE]`` leaves the bare invocation valid; required values do not.
        return value is None or value.startswith("[")
    if flag == "--agent-engine":
        # The separate engine-value parser below verifies that ``v2`` is valid.
        return True
    # CAO always supplies a value for --agent and --model.
    return value is not None


def _declared_option_flags(line: str) -> frozenset[str]:
    """Return invocation-compatible flags from one complete option declaration.

    Help output is untrusted capability input. A capability is advertised only
    when the formal declaration accepts the exact command shape CAO emits.
    """
    declaration = line.strip()
    if not declaration:
        return frozenset()

    short_variadic = False
    short_value: Optional[str] = None
    if declaration.startswith("-") and not declaration.startswith("--"):
        short_match = re.match(r"-[A-Za-z0-9](?P<variadic>\.\.\.)?", declaration)
        if not short_match:
            return frozenset()
        short_variadic = bool(short_match.group("variadic"))
        remainder = declaration[short_match.end() :]
        long_match = re.search(r"(?:,\s*|\s+)(?P<long>--[A-Za-z0-9][\w-]*)", remainder)
        if not long_match:
            return frozenset()
        short_value = remainder[: long_match.start()].strip() or None
        declaration = remainder[long_match.start("long") :]

    option_tokens = re.split(r",\s*(?=--)", declaration)
    parsed_options: list[tuple[str, bool, Optional[str]]] = []
    for index, option_token in enumerate(option_tokens):
        match = _OPTION_TOKEN_RE.fullmatch(option_token.strip())
        if not match:
            return frozenset()
        flag = match.group("flag")
        variadic = bool(match.group("variadic"))
        value = match.group("value")

        if index == 0 and short_value is not None:
            if not _is_formal_option_value(flag, short_value):
                return frozenset()
            if value is None:
                value = short_value
        if index == 0 and short_variadic:
            variadic = True
        if not _is_formal_option_value(flag, value):
            return frozenset()
        parsed_options.append((flag, variadic, value))

    return frozenset(
        flag
        for flag, variadic, value in parsed_options
        if _supports_declared_invocation(flag, variadic, value)
    )


def _flags_from_help(help_output: str) -> frozenset[str]:
    """Extract only known flags from complete option declaration lines."""
    advertised_flags: set[str] = set()
    for line in help_output.splitlines():
        advertised_flags.update(_declared_option_flags(line))
    return frozenset(_REQUESTED_FLAGS & advertised_flags)


def _agent_engines_from_help(help_output: str) -> frozenset[str]:
    """Extract explicitly declared ``--agent-engine`` values from wrapper help.

    Help output is untrusted capability input, so malformed declarations are
    deliberately ignored. A caller can use only values presented either on the
    selector declaration line or in its scoped ``possible values`` block.
    """
    lines = help_output.splitlines()
    for index, line in enumerate(lines):
        if "--agent-engine" not in _declared_option_flags(line):
            continue
        match = _AGENT_ENGINE_DECLARATION_RE.fullmatch(line)
        if match:
            declared_values = _parse_agent_engine_values(match.group("values"))
            if declared_values != frozenset({"ENGINE"}):
                return declared_values
        for detail_line in lines[index + 1 :]:
            if _declared_option_flags(detail_line):
                break
            possible_values = _POSSIBLE_VALUES_RE.fullmatch(detail_line.strip())
            if possible_values:
                return _parse_agent_engine_values(possible_values.group("values"))
    return frozenset()


def _parse_agent_engine_values(declaration: str) -> frozenset[str]:
    """Normalize one syntactically complete agent-engine value declaration."""
    return frozenset(
        value.strip() for value in re.split(r"\s*[|,]\s*", declaration.strip("<>[]{}"))
    )


def probe_kiro_capabilities(
    engine: KiroEngine,
    requested: Iterable[str],
    *,
    timeout: float = 5.0,
    chat_help_timeout: float = 10.0,
    runner: ProbeRunner = subprocess.run,
) -> KiroCapabilities:
    """Probe the selected engine mechanism and each requested wrapper feature.

    Released wrappers keep chat-specific options out of root help. Chat help
    receives a closed stdin so the probe cannot enter interactive mode, and its
    timeout is independently configurable because wrapper startup can be slower
    than the root command.
    """
    root_help_result = _run(runner, ("kiro-cli", "--help"), timeout, engine)
    root_help_output = (root_help_result.stdout or "") + "\n" + (root_help_result.stderr or "")
    chat_help_result = _run(
        runner,
        ("kiro-cli", "chat", "--help"),
        chat_help_timeout,
        engine,
        stdin=subprocess.DEVNULL,
    )
    chat_help_output = (chat_help_result.stdout or "") + "\n" + (chat_help_result.stderr or "")
    flags = _flags_from_help(root_help_output) | _flags_from_help(chat_help_output)
    agent_engines = _agent_engines_from_help(chat_help_output)

    version: Optional[str] = None
    try:
        version_result = _run(runner, ("kiro-cli", "--version"), timeout, engine)
        version_output = (version_result.stdout or "") + "\n" + (version_result.stderr or "")
        version_match = _VERSION_RE.search(version_output) or _VERSION_RE.search(
            root_help_output + "\n" + chat_help_output
        )
        if version_match:
            version = version_match.group(1)
    except KiroCapabilityError as exc:
        if exc.kind not in {"malformed_output"}:
            raise
        version_match = _VERSION_RE.search(root_help_output + "\n" + chat_help_output)
        if version_match:
            version = version_match.group(1)

    required = ["--agent-engine" if engine == KiroEngine.V2 else "--v3"]
    for capability in requested:
        try:
            required.append(_FLAG_CAPABILITIES[capability])
        except KeyError as exc:
            raise ValueError(f"Unknown Kiro capability requested: {capability!r}") from exc

    for flag in required:
        if flag not in flags:
            detail = f" Detected wrapper version: {version}." if version else ""
            retry = " Retry with engine 'v2'." if engine == KiroEngine.KAS else ""
            raise KiroCapabilityError(
                "unsupported_capability",
                engine,
                f"Kiro engine '{engine.value}' requires wrapper flag {flag!r}, "
                f"but it was not advertised by Kiro wrapper help.{detail}{retry}",
                capability=flag,
                version=version,
            )
    if engine == KiroEngine.V2 and not agent_engines:
        detail = f" Detected wrapper version: {version}." if version else ""
        raise KiroCapabilityError(
            "unsupported_capability",
            engine,
            "Kiro engine 'v2' requires Kiro wrapper help to advertise "
            "'--agent-engine' with an explicit 'v2' value, but it did not."
            f"{detail}",
            capability="--agent-engine=v2",
            version=version,
        )
    if engine == KiroEngine.V2 and KiroEngine.V2.value not in agent_engines:
        detail = f" Detected wrapper version: {version}." if version else ""
        raise KiroCapabilityError(
            "unsupported_capability",
            engine,
            "Kiro engine 'v2' requires '--agent-engine' to accept 'v2', "
            "but the wrapper did not advertise that value."
            f"{detail}",
            capability="--agent-engine=v2",
            version=version,
        )
    return KiroCapabilities(
        version=version,
        flags=flags,
        agent_engines=agent_engines,
    )


def build_kiro_command(
    engine: KiroEngine,
    agent_profile: str,
    *,
    model: Optional[str] = None,
    yolo: bool = False,
    legacy_ui: bool = False,
) -> list[str]:
    """Build a deterministic Kiro command without executing it."""
    if engine == KiroEngine.KAS:
        command = ["kiro-cli", "--v3", "chat"]
    else:
        command = ["kiro-cli", "chat", "--agent-engine", KiroEngine.V2.value]
        if legacy_ui:
            command.append("--legacy-ui")
        if yolo:
            command.append("--trust-all-tools")
    if model:
        command.extend(["--model", model])
    command.extend(["--agent", agent_profile])
    return command


def requested_kiro_capabilities(
    engine: KiroEngine, *, model: Optional[str], yolo: bool
) -> set[str]:
    """Return every wrapper feature used by the launch and fallback lifecycle."""
    requested = {"profile"}
    if model:
        requested.add("model")
    if engine == KiroEngine.V2:
        # Non-yolo launches may retry with --legacy-ui after a TUI startup
        # timeout, so this flag must be verified before any allocation.
        requested.add("ui")
        if yolo:
            requested.add("trust")
    return requested
