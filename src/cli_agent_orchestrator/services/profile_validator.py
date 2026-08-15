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


# Structural ceilings, applied before anything else walks or validates a
# document. Both sit far above real input: the largest bundled profile's
# frontmatter expands to 23 values and nests 3 deep, which these clear by ~870x
# and ~21x respectively.
_MAX_EXPANDED_VALUES = 20_000
_MAX_DEPTH = 64


def _structural_bound_finding(metadata: object) -> Optional["ValidationMessage"]:
    """Reject a document too large to hand to the rest of the validator.

    Size here is the number of values a *fully expanded* rendering would contain,
    not the number of distinct objects the parser produced. The two diverge
    without limit: ``yaml.safe_load`` resolves every alias to another reference to
    the *same* object, so N chained anchors that each reference the previous one
    twice give ~N distinct objects whose expansion is 2**N values, out of a
    sub-kilobyte body.

    That expansion, not the parsed size, is what the steps downstream pay for:

    - jsonschema builds every error message eagerly, interpolating ``repr`` of the
      offending instance. A 651-byte document with 20 anchor levels that trips one
      ``type`` error produced a single 25 MB message here, doubling per added
      level, so ~26 levels reaches gigabytes. Allocation is the ceiling there, not
      CPU, and no amount of care in this module's own traversal avoids it.
    - the key walk below visits each distinct container once, so it is already
      linear in the parsed structure. It is bounded here only in the sense that it
      is *reached* on documents this function accepted.

    Both land on ``POST /agents/profiles/validate`` in particular: it is
    scope-exempt, so it answers without credentials even when OAuth is
    configured, and it is declared ``async``, so work on its thread delays every
    other request rather than only the caller's own.

    Counted with memoization on ``id()``, which keeps the count itself linear in
    distinct objects, and capped so an enormous document costs no more to reject
    than one sitting just under the ceiling. Comparing identity is sound here
    because every value stays reachable from ``metadata`` throughout, so nothing
    can be collected and no id recycled midway. A container reached again while
    still being counted is a cycle and contributes 1.

    Returns:
        An error finding naming the ceiling that was exceeded, or ``None`` when
        the document is within both.
    """
    memo: dict[int, int] = {}
    ceiling = _MAX_EXPANDED_VALUES + 1
    too_deep = False

    def expanded(value: object, depth: int) -> int:
        nonlocal too_deep
        if not isinstance(value, (dict, list)):
            return 1
        if depth > _MAX_DEPTH:
            too_deep = True
            return 1
        identity = id(value)
        if identity in memo:
            return memo[identity]
        memo[identity] = 1  # Cycle guard, in force while this container counts.
        total = 1
        for child in value.values() if isinstance(value, dict) else value:
            total += expanded(child, depth + 1)
            if total >= ceiling:
                total = ceiling
                break
        memo[identity] = total
        return total

    size = expanded(metadata, 0)

    if too_deep:
        return ValidationMessage(
            "error",
            f"Frontmatter nests more than {_MAX_DEPTH} levels deep, past what this "
            f"validator will inspect. Flatten the document.",
        )
    if size >= ceiling:
        return ValidationMessage(
            "error",
            f"Frontmatter expands to more than {_MAX_EXPANDED_VALUES} values, past "
            f"what this validator will inspect. If it uses YAML anchors, note that "
            f"each alias expands in full. Simplify the document.",
        )
    return None


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

    **Why the walk memoizes.** YAML anchors make a document's value *graph*
    arbitrarily larger than its bytes: ``yaml.safe_load`` resolves each alias to
    another reference to the *same* object, so N chained anchors that each
    reference the previous one twice build a graph an unmemoized walk traverses
    2**N times while memory stays linear. The first version of this function
    carried only a depth cap, which bounded the wrong dimension: depth was never
    the problem, revisiting shared objects was. A 640-byte, schema-valid document
    with 20 anchor levels took ~1s here against ~0s in jsonschema, doubling per
    added level, on a route that is scope-exempt and ``async``.

    ``seen`` skips any container already walked, keyed on ``id()``. That removes
    the amplification at its source and needs no size ceiling of its own: the walk
    is linear in the document's *distinct* containers, and
    :func:`_structural_bound_finding` has already rejected anything whose expansion
    is large before this runs. Comparing identity is sound because every value
    stays reachable from ``metadata`` for the duration of the walk, so nothing can
    be collected and no id recycled midway.

    Skipping repeats costs no coverage: a shared subtree cannot hold a different
    set of keys on a second visit, so one finding per offending key is the correct
    output. Worth knowing that this makes the two halves of
    :func:`validate_frontmatter` report shared values differently. A shared value
    that is *schema*-invalid yields one finding per referencing path, because
    jsonschema does not memoize, while a shared *non-string key* yields exactly
    one, at whichever path reached it first. Both are defensible; a client
    highlighting findings against a document should not assume one convention.

    Args:
        metadata: Any parsed YAML value, already accepted by
            :func:`_structural_bound_finding`. Only mappings and sequences are
            descended into.

    Returns:
        One error finding per offending key, in document order.
    """
    findings: list[ValidationMessage] = []
    seen: set[int] = set()

    def walk(value: object, path: str) -> None:
        if not isinstance(value, (dict, list)):
            return  # A scalar has no keys, and nothing to descend into.
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
            walk(child, child_path)

    walk(metadata, "")

    return findings


def validate_frontmatter(metadata: dict) -> list[ValidationMessage]:
    """Validate a frontmatter dict against the schema and CAO conventions.

    Returns findings in a stable order: deprecated fields, then non-string
    mapping keys, then JSON-Schema errors sorted by path, then ``allowedTools``
    vocabulary warnings, then the role check. An empty list means the profile is
    valid with no advisories.

    A document outside the structural ceilings is the one exception to that
    order: it is reported and nothing further runs, because the later steps are
    exactly what such a document is expensive in.
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

    # 2. Structural ceilings, before anything traverses or validates the
    #    document.
    #
    #    Returning here rather than continuing is the whole point of the check.
    #    Step 3 is linear in distinct containers, but step 4 hands the document
    #    to jsonschema, which interpolates ``repr`` of an offending instance into
    #    every error message it builds -- so on an alias-amplified document,
    #    reporting the ceiling and then running the remaining steps anyway would
    #    pay the exact cost the ceiling exists to avoid.
    structural = _structural_bound_finding(metadata)
    if structural is not None:
        messages.append(structural)
        return messages

    # 3. Non-string mapping keys, which JSON Schema cannot see. Reported before
    #    the schema errors because a document with a non-string key is outside
    #    the format entirely, and because the schema's own findings for such a
    #    document tend to be confusing.
    messages.extend(_non_string_key_findings(metadata))

    # 4. JSON-Schema structural validation.
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

    # 5. allowedTools vocabulary check (advisory, not blocking).
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

    # 6. Role check (advisory — custom roles are valid but worth flagging).
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
