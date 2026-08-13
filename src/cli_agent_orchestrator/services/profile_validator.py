"""Profile frontmatter validation as a shared service.

Validates a *finished agent profile's* frontmatter against
``schemas/agent_profile.schema.json`` plus CAO conventions, so that
``cao profile validate`` and the HTTP surface share one implementation.

Distinct from :func:`agent_scaffold.validate_config`, which validates a
*template config* (the answers fed to a Jinja2 template) against that
template's own schema. This module validates a *profile* against the
*profile* schema.

Findings are returned severity-tagged rather than as pre-formatted strings, so
that callers decide presentation: the CLI renders ``[error] …`` / ``[warn] …``
lines, while the HTTP layer serialises them and lets a client block on errors
without parsing text.

Ref: https://github.com/awslabs/cli-agent-orchestrator/issues/510
"""

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files as _pkg_files
from typing import Literal, Optional

import frontmatter
from jsonschema import Draft202012Validator

from cli_agent_orchestrator.constants import ROLE_TOOL_DEFAULTS

Severity = Literal["error", "warning"]

# Known deprecated frontmatter fields that should trigger warnings.
_DEPRECATED_FIELDS = {"autoApproveTools"}

# Derive valid tool vocabulary from constants (single source of truth).
_VALID_TOOL_VOCAB: set[str] = set()
for _tools in ROLE_TOOL_DEFAULTS.values():
    _VALID_TOOL_VOCAB.update(_tools)

_BUILTIN_ROLES: set[str] = set(ROLE_TOOL_DEFAULTS.keys())


@dataclass(frozen=True)
class ValidationMessage:
    """A single validation finding.

    ``path`` is the dotted frontmatter location for JSON-Schema errors
    (``"(root)"`` when the error is on the document itself), and ``None`` for
    convention checks that are not tied to one key.
    """

    severity: Severity
    message: str
    path: Optional[str] = None


@lru_cache(maxsize=1)
def load_profile_schema() -> dict:
    """Return the agent profile JSON-Schema.

    Anchored through ``importlib.resources`` rather than a relative parent walk
    so the lookup does not depend on this module's position in the package, and
    resolves for both editable and wheel installs. Cached because the schema is
    a packaged resource that cannot change at runtime and the HTTP validate
    endpoint may be called repeatedly.

    Callers must treat the returned dict as read-only; it is shared.
    """
    schema_path = _pkg_files("cli_agent_orchestrator") / "schemas" / "agent_profile.schema.json"
    return json.loads(schema_path.read_text(encoding="utf-8"))


def _non_string_key_findings(
    value: object, path: str = "", _depth: int = 0
) -> list["ValidationMessage"]:
    """Report every mapping key in ``value`` that is not a string.

    Closes a gap between the two formats in play. A profile arrives as **YAML**,
    which allows any scalar as a mapping key, but the format is described by
    **JSON Schema**, where object keys are strings by definition. jsonschema
    therefore does not flag ``mcpServers: {1: {command: echo}}`` at all, while
    ``AgentProfile`` refuses to load it later because ``Dict[str, ...]`` rejects
    the integer key.

    Reported here rather than only on the HTTP write path so that every consumer
    agrees. Otherwise ``cao profile validate`` and
    ``POST /agents/profiles/validate`` would call such a document valid and the
    write routes would then reject it, and a UI that validates before saving
    would show a contradiction.

    The same mismatch reaches further than integer keys: YAML also auto-types
    unquoted dates, so ``2026-01-01:`` becomes a ``datetime.date`` key. Checking
    the key type generally covers those without enumerating them.

    Args:
        value: Any parsed YAML value. Only mappings and sequences are descended.
        path: Dotted path of ``value`` within the document, for the finding.
        _depth: Recursion guard. YAML aliases can build deeply nested or
            self-referential structures, and a validator must not hang on input
            whose whole problem is that it is malformed.

    Returns:
        One error finding per offending key, in document order.
    """
    if _depth > 32:
        return []

    findings: list[ValidationMessage] = []

    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if not isinstance(key, str):
                findings.append(
                    ValidationMessage(
                        "error",
                        f"Mapping key {key!r} is a {type(key).__name__}, not a string. "
                        f"Profile fields are string-keyed; quote it as '{key}'.",
                        child_path,
                    )
                )
            findings.extend(_non_string_key_findings(child, child_path, _depth + 1))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = f"{path}.{index}" if path else str(index)
            findings.extend(_non_string_key_findings(child, child_path, _depth + 1))

    return findings


def validate_frontmatter(metadata: dict) -> list[ValidationMessage]:
    """Validate a frontmatter dict against the schema and CAO conventions.

    Returns findings in a stable order: deprecated fields, then non-string
    mapping keys, then JSON-Schema errors sorted by path, then ``allowedTools``
    vocabulary warnings, then the role check. An empty list means the profile is
    valid with no advisories.
    """
    messages: list[ValidationMessage] = []

    # 1. Deprecated fields first, before ``additionalProperties: false``
    #    rejects them with a less helpful message.
    for field in sorted(_DEPRECATED_FIELDS):
        if field in metadata:
            messages.append(
                ValidationMessage(
                    "warning",
                    f"'{field}' is deprecated and rejected by CAO 2.2+. "
                    f"Use 'allowedTools' instead.",
                )
            )

    # 2. Non-string mapping keys, which JSON Schema cannot see. Reported before
    #    the schema errors because a document with a non-string key is outside
    #    the format entirely, and because the schema's own findings for such a
    #    document tend to be confusing.
    messages.extend(_non_string_key_findings(metadata))

    # 3. JSON-Schema structural validation.
    #
    # The sort key stringifies each path component. Raw components are whatever
    # the document used as mapping keys, so a profile with mixed-type keys (for
    # example ``mcpServers: {1: {}, x: {}}``) yields paths that cannot be ordered
    # against each other and would raise TypeError mid-sort. Such a document is
    # already schema-invalid; it must be *reported* as invalid rather than crash
    # the validator.
    validator = Draft202012Validator(load_profile_schema())
    for error in sorted(validator.iter_errors(metadata), key=lambda e: [str(p) for p in e.path]):
        path = ".".join(str(p) for p in error.absolute_path) or "(root)"
        messages.append(ValidationMessage("error", error.message, path))

    # 4. allowedTools vocabulary check (advisory, not blocking).
    #
    # Each entry is type-checked before the membership test. ``_VALID_TOOL_VOCAB``
    # is a set, so ``tool not in`` hashes ``tool``, and an unhashable element
    # (``allowedTools: [[Read]]``) would raise TypeError. The schema already
    # rejects a non-string entry, so this check only has to avoid crashing on
    # input the caller will be told about anyway.
    allowed = metadata.get("allowedTools")
    if allowed and isinstance(allowed, list):
        for tool in allowed:
            if not isinstance(tool, str):
                continue
            if tool not in _VALID_TOOL_VOCAB:
                messages.append(
                    ValidationMessage(
                        "warning",
                        f"allowedTools entry '{tool}' is not in CAO's recognized "
                        f"vocabulary. It may be silently ignored by some providers.",
                    )
                )

    # 5. Role check (advisory — custom roles are valid but worth flagging).
    #
    # Same hashing hazard as above: ``role: [developer]`` is unhashable. The
    # schema reports the type error, so this advisory check simply stands aside.
    role = metadata.get("role")
    if isinstance(role, str) and role and role not in _BUILTIN_ROLES:
        messages.append(
            ValidationMessage(
                "warning",
                f"role '{role}' is not a built-in CAO role "
                f"({', '.join(sorted(_BUILTIN_ROLES))}). "
                f"Ensure it is defined in your settings.json custom roles.",
            )
        )

    return messages


def validate_profile_text(text: str) -> list[ValidationMessage]:
    """Parse profile markdown and validate its frontmatter.

    Convenience wrapper for callers holding a whole profile document rather
    than a parsed metadata dict, so the frontmatter parse is not duplicated at
    each call site.

    Raises:
        ValueError: ``text`` could not be parsed as frontmatter.
    """
    try:
        post = frontmatter.loads(text)
    except Exception as e:
        raise ValueError(f"Error reading profile: {e}") from e
    return validate_frontmatter(post.metadata)
