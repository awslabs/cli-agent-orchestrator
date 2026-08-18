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
# document. The byte ceiling is in *rendered* bytes, the unit the step downstream
# costs in, and is set against the 256 KB cap both write routes put on ``content``:
# a document with no aliasing renders to roughly its own size, so 1 MB is ~3.8x
# the largest request that can arrive. Measured against real input, the largest
# bundled profile's frontmatter renders to 485 bytes and nests 3 deep, clearing
# these by ~2060x and ~21x.
_MAX_RENDERED_BYTES = 1_000_000
_MAX_DEPTH = 64

# Ceiling on a single schema finding's text. Only jsonschema's own messages need
# it: everything this module authors is a fixed sentence plus a path.
_MAX_FINDING_CHARS = 2_000


def _capped(message: str) -> str:
    """Bound one schema finding's text before it reaches a response body.

    A backstop, not the fix. jsonschema interpolates the offending instance into
    the message inside ``iter_errors``, so the allocation has already happened by
    the time this sees the string; :func:`_structural_bound_finding` is what
    prevents it. This bounds two things that guard does not. A document within the
    rendering ceiling can still trip errors on several fields, each rendering its
    own subtree, so the total response is a small multiple of the ceiling rather
    than the ceiling. And it is a second line of defence on a function that has now
    twice bounded the wrong dimension, first depth and then value occurrences.
    """
    if len(message) <= _MAX_FINDING_CHARS:
        return message
    suffix = f"... (message truncated, {len(message)} chars)"
    return f"{message[: _MAX_FINDING_CHARS - len(suffix)]}{suffix}"


def _structural_bound_finding(metadata: object) -> Optional["ValidationMessage"]:
    """Reject a document the rest of the validator cannot safely be handed.

    Three ways a document fails here: it renders too large, it nests too deep, or
    it contains a cycle.

    **Why bytes.** The cost this guard exists to bound is ``repr(instance)``:
    jsonschema builds every error message eagerly, interpolating a rendering of the
    offending instance. ``yaml.safe_load`` resolves each alias to another reference
    to the *same* object, so a document's rendered size is unbounded by its own
    byte count in two separate ways. Chained anchors multiply *structure*: 20
    levels that each reference the previous one twice took a 651-byte body to a
    25 MB message. Aliasing one large scalar multiplies *content*: a 250,055-byte
    body holding a 190,000-character scalar referenced 15,000 times renders to
    2.85 GB. An earlier version of this function counted value *occurrences*, which
    caught the first and missed the second, since every scalar counted as 1
    regardless of length. Counting the bytes each occurrence renders covers both,
    because it is the same unit the downstream step pays in.

    Both land on ``POST /agents/profiles/validate`` in particular: it is
    scope-exempt, so it answers without credentials even when OAuth is configured,
    and it is declared ``async``, so work on its thread delays every other request
    rather than only the caller's own.

    **Why cycles are rejected rather than counted.** A cycle has no finite
    rendering, so any finite number this function returned for one would be a
    fiction. It is also unusable downstream: ``model_dump_json`` raises
    ``PydanticSerializationError: Circular reference detected`` when the Kiro
    materialization path writes the profile out, so accepting one persists a
    document the runtime cannot use. In-progress identities are therefore tracked
    separately from completed ones: revisiting a container that is still being
    measured is a back-edge, while revisiting a finished one is ordinary sharing
    and stays memoized.

    **Why memoization is sound.** Identity is compared rather than value because
    every object stays reachable from ``metadata`` for the duration, so nothing can
    be collected and no id recycled midway. Scalars are memoized too, so a large
    shared scalar is rendered once even when referenced thousands of times, which
    keeps this function's own allocation bounded by the source document while still
    charging its bytes at every reference.

    Returns:
        An error finding naming what was exceeded, or ``None`` when the document is
        within all three bounds.
    """
    memo: dict[int, int] = {}
    in_progress: set[int] = set()
    ceiling = _MAX_RENDERED_BYTES + 1
    exceeded: Optional[str] = None

    def rendered(value: object, depth: int) -> int:
        nonlocal exceeded
        if exceeded is not None:
            return 0

        identity = id(value)

        if not isinstance(value, (dict, list)):
            cached = memo.get(identity)
            if cached is None:
                cached = len(repr(value))
                memo[identity] = cached
            return cached

        if depth > _MAX_DEPTH:
            exceeded = "depth"
            return 0
        if identity in in_progress:
            exceeded = "cycle"
            return 0
        completed = memo.get(identity)
        if completed is not None:
            return completed

        in_progress.add(identity)
        total = 2  # The enclosing braces or brackets.
        children = value.items() if isinstance(value, dict) else enumerate(value)
        for key, child in children:
            # ``'key': `` for a mapping, ``, `` between entries either way. The
            # index of a sequence entry is not rendered, so it costs nothing.
            total += (len(repr(key)) + 2) if isinstance(value, dict) else 0
            total += 2 + rendered(child, depth + 1)
            if exceeded is not None:
                break
            if total >= ceiling:
                total = ceiling
                break
        in_progress.discard(identity)

        memo[identity] = total
        return total

    size = rendered(metadata, 0)

    if exceeded == "cycle":
        return ValidationMessage(
            "error",
            "Frontmatter contains a circular YAML alias, so it has no finite "
            "rendering and cannot be serialized by the providers that consume it. "
            "Remove the self-reference.",
        )
    if exceeded == "depth":
        return ValidationMessage(
            "error",
            f"Frontmatter nests more than {_MAX_DEPTH} levels deep, past what this "
            f"validator will inspect. Flatten the document.",
        )
    if size >= ceiling:
        return ValidationMessage(
            "error",
            f"Frontmatter renders to more than {_MAX_RENDERED_BYTES} bytes, past "
            f"what this validator will inspect. If it uses YAML anchors, note that "
            f"every alias renders its target again in full, so a small document can "
            f"exceed this. Simplify the document.",
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
    #    document: rendered size, nesting depth, and cycles.
    #
    #    Returning here rather than continuing is the whole point of the check.
    #    Step 3 is linear in distinct containers, but step 4 hands the document
    #    to jsonschema, which interpolates a rendering of an offending instance
    #    into every error message it builds -- so on an alias-amplified document,
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
        messages.append(ValidationMessage("error", _capped(error.message), path))

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
