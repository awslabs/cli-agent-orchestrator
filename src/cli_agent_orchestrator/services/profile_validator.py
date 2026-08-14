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


# Ceilings for the key walk below, on a document that is genuinely huge or
# deeply nested rather than aliased. Generous by ~1000x: the largest bundled
# profile's frontmatter parses to 23 values and nests 3 deep, so no legitimate
# profile approaches either bound.
_MAX_WALK_VALUES = 20_000
_MAX_WALK_DEPTH = 64


def _non_string_key_findings(metadata: object) -> list["ValidationMessage"]:
    """Report every mapping key reachable from ``metadata`` that is not a string.

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

    **Why the walk is bounded.** YAML anchors make a document's value *graph*
    arbitrarily larger than its bytes: ``yaml.safe_load`` resolves each alias to
    another reference to the *same* object, so N chained anchors that each
    reference the previous one twice build a graph an unmemoized walk traverses
    2**N times while memory stays linear. The first version of this function
    carried only a depth cap, which bounded the wrong dimension -- depth was
    never the problem, revisiting shared objects was -- and a 640-byte,
    schema-valid document with 20 anchor levels took ~1s here against ~0s in
    jsonschema, doubling per added level. That is reachable unauthenticated:
    ``POST /agents/profiles/validate`` is scope-exempt and declared ``async``,
    so a synchronous walk on its thread stalls the whole event loop.

    Two bounds, each covering what the other does not:

    - ``seen`` skips any container already walked, keyed on ``id()``. This
      removes the amplification at its source and costs no coverage: a shared
      subtree cannot hold a different set of keys on a second visit, so one
      finding per offending key is the correct output, reported at the first
      path that reaches it. Comparing identity is sound here specifically
      because every value stays reachable from ``metadata`` for the duration of
      the walk, so nothing can be collected and no id can be recycled midway.
    - ``_MAX_WALK_VALUES`` and ``_MAX_WALK_DEPTH`` bound a document that is
      merely enormous, which memoizing identity does not. Exceeding either adds
      an **error** finding, so such a document is rejected rather than quietly
      called valid on the strength of a partial walk.

    Args:
        metadata: Any parsed YAML value. Only mappings and sequences are
            descended into.

    Returns:
        Error findings in document order: one per offending key, plus a final
        one if a bound was reached.
    """
    findings: list[ValidationMessage] = []
    seen: set[int] = set()
    remaining = _MAX_WALK_VALUES
    limit_reached: Optional[str] = None

    def walk(value: object, path: str, depth: int) -> None:
        nonlocal remaining, limit_reached

        if not isinstance(value, (dict, list)):
            return  # A scalar has no keys, and nothing to descend into.
        if limit_reached is not None:
            return
        if depth > _MAX_WALK_DEPTH:
            limit_reached = f"is nested more than {_MAX_WALK_DEPTH} levels deep"
            return
        if id(value) in seen:
            return
        seen.add(id(value))

        children: list[tuple[str, object]]
        if isinstance(value, dict):
            children = []
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
                children.append((child_path, child))
        else:
            children = [
                (f"{path}.{index}" if path else str(index), child)
                for index, child in enumerate(value)
            ]

        for child_path, child in children:
            remaining -= 1
            if remaining <= 0:
                limit_reached = f"holds more than {_MAX_WALK_VALUES} values"
                return
            walk(child, child_path, depth + 1)
            if limit_reached is not None:
                return

    walk(metadata, "", 0)

    if limit_reached is not None:
        findings.append(
            ValidationMessage(
                "error",
                f"Frontmatter {limit_reached}, past the bound this validator will "
                f"traverse, so its mapping keys cannot be fully checked. Simplify "
                f"the document.",
            )
        )

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
