"""Agent profile models."""

import re
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from cli_agent_orchestrator.constants import (
    PROFILE_MAX_PARAMS,
    PROFILE_PARAM_DESCRIPTION_MAX_CHARS,
    PROFILE_PARAM_MAX_ENUM_VALUES,
    WORKFLOW_STEP_PARAM_KEY_RE,
    WORKFLOW_STEP_PARAM_VALUE_MAX_CHARS,
)
from cli_agent_orchestrator.models.kiro_engine import KiroEngine

PermissionMode = Literal["default", "acceptEdits", "plan", "auto", "bypassPermissions"]

ProfileParamType = Literal["string", "int", "bool"]
ProfileParamValue = Union[StrictBool, StrictInt, StrictStr]

_PARAM_NAME_RE = re.compile(WORKFLOW_STEP_PARAM_KEY_RE)
_PARAM_TYPES = ("string", "int", "bool")
_PARAM_KEYS = ("type", "required", "default", "description", "enum")
# An int default or enum value has at most 18 digits, the same bound as a step's int value
# (WORKFLOW_STEP_PARAM_INT_RE), so a declared value always passes the step check.
_PARAM_INT_LIMIT = 10**18
# How much of an unknown name or key a message quotes. The text comes from the profile.
_QUOTED_CHARS = 80

# One broken rule: where it is, as path parts below ``params``, and what is wrong.
ParamProblem = Tuple[Tuple[Union[str, int], ...], str]


class McpServer(BaseModel):
    """MCP server configuration."""

    type: Optional[str] = None
    command: str
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    timeout: Optional[int] = None


class ContainerPathMap(BaseModel):
    """Single host->guest path mapping for container environments."""

    host: str
    guest: str


class ContainerConfig(BaseModel):
    """Container environment configuration."""

    path_maps: Optional[List[ContainerPathMap]] = None


# ---------------------------------------------------------------------------
# Profile ``params``: the params a workflow step that uses this profile must fill
# ---------------------------------------------------------------------------
# Profiles can be installed from a URL, so the declaration is untrusted. Every size is
# bounded, nothing is evaluated, and no regex from the profile runs: there is no
# ``pattern``, ``minimum``, ``maximum`` or ``maxLength`` key.
#
# ``param_declaration_problems`` is the one statement of the rules. The model raises on
# what it returns, and the profile validator reports it, so the two cannot disagree.
# ``schemas/agent_profile.schema.json`` states the same rules for schema-driven clients,
# except three. Two JSON Schema cannot express: a default is one of the enum values, and an
# int value is not written with a decimal point (JSON has one number type, so ``1.0``
# passes ``"type": "integer"``). The third is that a string default or enum value has no
# ``{{...}}`` placeholder. It uses the bounded check the step params use, so the schema
# does not carry a second copy of the pattern.


def _quoted(value: object) -> str:
    """``repr`` of a name or key from the profile, cut to a bounded length."""
    text = repr(value)
    if len(text) <= _QUOTED_CHARS:
        return text
    return f"{text[:_QUOTED_CHARS]}..."


def _kind(value: object) -> str:
    """Name a parsed YAML value's type the way a profile author would write it."""
    if value is None:
        return "null"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, bool):  # before int: bool is an int subclass
        return "a boolean"
    if isinstance(value, int):
        return "a whole number"
    if isinstance(value, float):
        return "a decimal number"
    if isinstance(value, (list, tuple)):
        return "a list"
    if isinstance(value, dict):
        return "a mapping"
    return type(value).__name__


def _one_line_problem(value: str, limit: int) -> Optional[str]:
    """Why ``value`` is not one line of at most ``limit`` characters, or None."""
    if len(value) > limit:
        return f"is {len(value)} characters (max {limit})"
    if "\n" in value or "\r" in value:
        return "must be one line (no line breaks)"
    return None


def _value_problem(value: object, declared: str) -> Optional[str]:
    """Why ``value`` cannot be a default or enum value of a ``declared`` param, or None.

    A YAML boolean is not a whole number, and a whole number written with a decimal
    point (``1.0``) is not one either.
    """
    if declared == "bool":
        return None if isinstance(value, bool) else f"must be true or false, not {_kind(value)}"
    if declared == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return f"must be a whole number, not {_kind(value)}"
        if abs(value) >= _PARAM_INT_LIMIT:
            return "must be a whole number of at most 18 digits"
        return None
    if not isinstance(value, str):
        return f"must be a string, not {_kind(value)} (quote the value)"
    return _one_line_problem(value, WORKFLOW_STEP_PARAM_VALUE_MAX_CHARS)


# A step's params refuse a ``{{...}}`` placeholder, and the workflow builder copies a
# default, or the enum value a step picks, into the step's params.
_DEFAULT_PLACEHOLDER = (
    "contains a {{...}} placeholder. A default is copied into workflow steps, "
    "where values are fixed when the workflow is built."
)
_ENUM_PLACEHOLDER = (
    "contains a {{...}} placeholder. An enum value is copied into the workflow steps "
    "that pick it, where values are fixed when the workflow is built."
)


def _has_step_placeholder(value: object) -> bool:
    """True when ``value`` is a string that a step's params would refuse as a placeholder.

    Uses ``_has_placeholder``, the bounded check the step params use, so the two rules
    agree. It is imported here rather than at the top because ``models.workflow`` imports
    ``jsonschema`` and ``yaml``, and every provider imports this module.
    """
    if not isinstance(value, str):
        return False
    from cli_agent_orchestrator.models.workflow import _has_placeholder

    return _has_placeholder(value)


def _entry_problems(name: Optional[str], entry: object) -> List[ParamProblem]:
    """Every rule one declaration breaks, as ``(path, message)`` pairs.

    The path is relative to the entry: ``()`` for the entry itself, ``("default",)`` for
    one key, ``("enum", 2)`` for one enum value. ``name`` is None when a declaration is
    built on its own, outside a profile.
    """
    if isinstance(entry, ProfileParamDecl):
        return []  # already validated
    label = "param" if name is None else f"param {_quoted(name)}"
    if not isinstance(entry, dict):
        return [((), f"{label} must be a mapping, not {_kind(entry)}")]

    problems: List[ParamProblem] = []
    for key in entry:
        if key not in _PARAM_KEYS:
            problems.append(
                (
                    (),
                    f"{label}: key {_quoted(key)} is not supported. A param declares only "
                    "type, required, default, description and enum.",
                )
            )

    declared: Optional[str] = "string"
    if "type" in entry:
        if entry["type"] in _PARAM_TYPES:
            declared = entry["type"]
        else:
            problems.append((("type",), f"{label}: type must be string, int or bool"))
            declared = None  # the default and enum values cannot be checked

    required = entry.get("required", False)
    if not isinstance(required, bool):
        problems.append(
            (("required",), f"{label}: required must be true or false, not {_kind(required)}")
        )

    if "description" in entry:
        description = entry["description"]
        if not isinstance(description, str):
            problems.append(
                (
                    ("description",),
                    f"{label}: description must be a string, not {_kind(description)}",
                )
            )
        else:
            problem = _one_line_problem(description, PROFILE_PARAM_DESCRIPTION_MAX_CHARS)
            if problem is not None:
                problems.append((("description",), f"{label}: description {problem}"))

    enum_values: Optional[List[Any]] = None
    if "enum" in entry:
        values = entry["enum"]
        if declared == "bool":
            problems.append((("enum",), f"{label}: a bool param cannot have an enum"))
        elif not isinstance(values, list):
            problems.append((("enum",), f"{label}: enum must be a list, not {_kind(values)}"))
        elif not 1 <= len(values) <= PROFILE_PARAM_MAX_ENUM_VALUES:
            problems.append(
                (
                    ("enum",),
                    f"{label}: enum has {len(values)} values "
                    f"(it must have 1 to {PROFILE_PARAM_MAX_ENUM_VALUES})",
                )
            )
        elif declared is not None:
            value_problems: List[ParamProblem] = []
            for index, value in enumerate(values):
                problem = _value_problem(value, declared)
                if problem is None and _has_step_placeholder(value):
                    problem = _ENUM_PLACEHOLDER
                if problem is not None:
                    value_problems.append((("enum", index), f"{label}: enum[{index}] {problem}"))
            problems.extend(value_problems)
            if not value_problems:
                # Every value is now a str, or an int that is not a bool, so it hashes and
                # compares by value.
                if len(set(values)) != len(values):
                    problems.append((("enum",), f"{label}: enum lists a value more than once"))
                else:
                    enum_values = values

    if "default" in entry:
        default = entry["default"]
        if required is True:
            problems.append((("default",), f"{label}: a required param cannot have a default"))
        elif declared is not None:
            problem = _value_problem(default, declared)
            if problem is None and _has_step_placeholder(default):
                problem = _DEFAULT_PLACEHOLDER
            if problem is not None:
                problems.append((("default",), f"{label}: default {problem}"))
            elif enum_values is not None and default not in enum_values:
                problems.append((("default",), f"{label}: default must be one of the enum values"))

    return problems


def param_declaration_problems(params: object) -> List[ParamProblem]:
    """Every rule a profile's ``params`` block breaks, as ``(path, message)`` pairs.

    The path is relative to ``params``: ``()`` for the block itself, ``(name,)`` for one
    declaration, and ``(name, key, ...)`` below it. A tuple, because a param name may
    contain a dot. An empty list means the block is valid. A block with more than
    ``PROFILE_MAX_PARAMS`` entries is reported without looking at them, so the work stays
    bounded.
    """
    if not isinstance(params, dict):
        return [((), f"params must be a mapping of names to declarations, not {_kind(params)}")]
    if len(params) > PROFILE_MAX_PARAMS:
        return [((), f"params declares {len(params)} params (max {PROFILE_MAX_PARAMS})")]
    problems: List[ParamProblem] = []
    for name, entry in params.items():
        if not isinstance(name, str) or not _PARAM_NAME_RE.fullmatch(name):
            problems.append(
                (
                    (),
                    f"param name {_quoted(name)} is invalid "
                    f"(must match {WORKFLOW_STEP_PARAM_KEY_RE})",
                )
            )
            continue
        for path, message in _entry_problems(name, entry):
            problems.append(((name, *path), message))
    return problems


def _raise_on(problems: List[ParamProblem]) -> None:
    if problems:
        raise ValueError("; ".join(message for _path, message in problems))


class ProfileParamDecl(BaseModel):
    """One param a workflow step that uses the profile must fill (CAO-native).

    Serialized by ``model_dump(exclude_none=True)``: ``type`` and ``required`` always
    appear, ``default``, ``description`` and ``enum`` only when set. An int default is
    a JSON number and a bool default a JSON bool.
    """

    model_config = ConfigDict(extra="forbid")

    type: ProfileParamType = "string"
    required: StrictBool = False
    default: Optional[ProfileParamValue] = None
    description: Optional[StrictStr] = None
    enum: Optional[List[ProfileParamValue]] = None

    @model_validator(mode="before")
    @classmethod
    def _check_declaration(cls, data: Any) -> Any:
        _raise_on(_entry_problems(None, data))
        return data


class AgentProfile(BaseModel):
    """Agent profile configuration with Q CLI agent fields."""

    name: str
    description: str
    provider: Optional[str] = None  # Provider override (e.g. "claude_code", "kiro_cli")
    system_prompt: Optional[str] = None  # The markdown content
    role: Optional[str] = None  # "supervisor", "developer", "reviewer", "workflow_scout"
    # CAO-native. The most this agent may grant to children it creates through
    # assign/handoff/workflow steps (its delegation ceiling). None = the role
    # default: ``*`` for ``supervisor``, the agent's own allowedTools otherwise.
    # Consumed by CAO's create-terminal policy only; never passed to provider JSON.
    delegateTools: Optional[List[str]] = None
    # Kiro v2/v3 selection ("kas" is an alias of v3); omitted resolves to v2.
    engine: Optional[KiroEngine] = None

    # CAO-native. Per-agent skill-catalog scope: when set, only skills whose name
    # matches one of these patterns (exact name or fnmatch glob, e.g. "ads-*") are
    # injected into this agent's "## Available Skills" catalog at launch. None =
    # the full catalog (backward-compatible); [] = no skills advertised. Consumed
    # by CAO when composing the prompt, not passed through to provider JSON.
    skills: Optional[List[str]] = None

    # Discovery metadata used by `cao profile find` / the find_profiles MCP
    # tool (#340). Declared here so parse paths built on this model keep the
    # fields (pydantic silently drops undeclared keys).
    capabilities: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    # CAO-native. Host->guest path maps for container-backed agents. Consumed by
    # the provider layer to translate host paths (e.g. temp prompt/MCP files)
    # into the guest paths the containerized CLI sees; not passed to provider JSON.
    container: Optional[ContainerConfig] = None

    # CAO-native. Per-profile override for provider initialization timeout (seconds).
    # When set, this value is used as the hard outer cap for CLI agent initialization
    # instead of the server default (60s from settings_service). Allows containerized
    # profiles to declare longer init times (e.g., 180s) without changing global config.
    provider_init_timeout: Optional[int] = None

    # CAO-native. The params a workflow step that uses this profile must fill, by
    # name, in declaration order (see ProfileParamDecl). Read by the workflow builder
    # and by the step check at validate and run start. Never passed to provider JSON.
    # Step values are journaled in plaintext, so a param never holds a secret.
    params: Optional[Dict[str, ProfileParamDecl]] = None

    @field_validator("params", mode="before")
    @classmethod
    def _check_params(cls, value: Any) -> Any:
        if value is not None:
            _raise_on(param_declaration_problems(value))
        return value

    # Q CLI agent fields (all optional, will be passed through to JSON)
    prompt: Optional[str] = None
    mcpServers: Optional[Dict[str, Any]] = None
    tools: Optional[List[str]] = Field(default=None)
    toolAliases: Optional[Dict[str, str]] = None
    allowedTools: Optional[List[str]] = None
    toolsSettings: Optional[Dict[str, Any]] = None
    resources: Optional[List[str]] = None
    hooks: Optional[Dict[str, Any]] = None
    useLegacyMcpJson: Optional[bool] = None
    model: Optional[str] = None
    permissionMode: Optional[PermissionMode] = None
    native_agent: Optional[str] = None  # Claude Code native agent name (thin-wrapper mode)

    # Codex-only. Names a $CODEX_HOME/<name>.config.toml file (by default
    # ~/.codex/<name>.config.toml), which codex layers on top of the base user
    # config. NOT a [profiles.<name>] table inside config.toml -- that was the
    # older layout and codex now refuses to start at all when --profile is
    # combined with it (#967).
    # Used as --profile <name> when yolo mode is not active; unrestricted
    # allowed tools still force --yolo. min_length=1 prevents an explicit
    # empty string from silently degrading to --yolo, since this is a
    # permission-floor knob.
    codexProfile: Optional[str] = Field(default=None, min_length=1)

    # Codex-only. Inline Codex config overrides passed as `-c key=value` at
    # launch (e.g. {"model_reasoning_effort": "xhigh", "service_tier": "fast",
    # "features.fast_mode": True}). Keys may be dotted paths into Codex's
    # config.toml schema; values are serialized to TOML scalars (strings are
    # quoted, bools/numbers emitted bare). Applied in both the default --yolo
    # path and the --profile <codexProfile> path, so per-agent knobs like
    # reasoning effort or fast mode need no global ~/.codex/config.toml edits
    # or named profile files. Composes with codexProfile; because Codex applies
    # CLI overrides last, these win on key conflicts.
    codexConfig: Optional[Dict[str, Any]] = None

    # Hermes-only. Optionally names a Hermes profile wrapper command (for
    # example one created by `hermes profile alias <profile>`). When omitted,
    # the Hermes provider launches the default `hermes` command.
    hermesProfile: Optional[str] = Field(default=None, min_length=1)

    # Kimi Code-only. Omission inherits the operator's swarm default; false
    # opts out. The provider requires a verified bounded Kimi executable before
    # enabling swarm and never expands the profile's native tool allowlist.
    kimiSwarm: Optional[bool] = Field(default=None, strict=True)
    kimiSwarmMaxConcurrency: Optional[int] = Field(default=None, ge=1, le=10, strict=True)

    # Claude Code-only. Per-agent Claude Code knobs mapped to CLI flags at
    # launch: {"effort": "<low|medium|high|xhigh>"} -> `--effort <level>` and
    # {"fallback_model": "<model>"} -> `--fallback-model <model>`. Lets a
    # profile set per-agent reasoning effort without relying on the
    # machine-global `effortLevel` in ~/.claude/settings.json. This is the
    # Claude analog of codexConfig for the codex provider; the top-level
    # `model` field still maps to `--model`.
    claudeConfig: Optional[Dict[str, Any]] = None

    # Kiro CLI-only. Per-agent Kiro launch knobs: {"effort": "<level>"} ->
    # `--effort <level>`, requested from the wrapper's help before launch.
    # Defined exactly as #1074 defines it, so either change lands unchanged;
    # the top-level `model` field still maps to `--model`.
    kiroConfig: Optional[Dict[str, Any]] = None

    # Grok-only. Explicitly permits Grok's own subagents, workflows, and /goal
    # engine in this CAO terminal. Omission remains ``None`` so existing profile
    # API responses do not gain a new false-valued field; Grok resolves None as
    # disabled because those workers are outside CAO's profile, callback, and
    # terminal-accounting boundaries.
    grokNativeWorkflows: Optional[bool] = None
