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
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files as _pkg_files
from itertools import islice
from typing import Any, Iterator, Literal, Optional

import frontmatter
from jsonschema import Draft202012Validator as _Draft202012Validator
from jsonschema import ValidationError, validators

from cli_agent_orchestrator.constants import ROLE_TOOL_DEFAULTS

Severity = Literal["error", "warning"]


_DEFAULT_ADDITIONAL_PROPERTIES = _Draft202012Validator.VALIDATORS["additionalProperties"]


def _ordered_additional_properties(
    validator: Any,
    additional_properties: object,
    instance: object,
    schema: dict[str, Any],
) -> Iterator[ValidationError]:
    """Validate object-valued additional properties in document order.

    jsonschema's default keyword implementation first converts matching property
    names to a set. When the outer validator takes a bounded prefix, that makes
    the selected findings depend on ``PYTHONHASHSEED``. Walking the already
    ordered mapping directly keeps the prefix stable without materializing or
    sorting the omitted tail.

    Boolean-valued ``additionalProperties`` keeps the default implementation so
    its single aggregate error and wording remain unchanged.
    """
    if isinstance(additional_properties, dict) and isinstance(instance, dict):
        properties = schema.get("properties", {})
        patterns = tuple(schema.get("patternProperties", {}))
        for property_name, value in instance.items():
            if property_name in properties:
                continue
            if isinstance(property_name, str) and any(
                re.search(pattern, property_name) for pattern in patterns
            ):
                continue
            yield from validator.descend(value, additional_properties, path=property_name)
        return

    yield from _DEFAULT_ADDITIONAL_PROPERTIES(validator, additional_properties, instance, schema)


Draft202012Validator = validators.extend(
    _Draft202012Validator,
    {"additionalProperties": _ordered_additional_properties},
)

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

# Ceiling on a single finding's text. The collector applies it to every producer,
# even though jsonschema is the one that can interpolate a large instance.
_MAX_FINDING_CHARS = 2_000

# Aggregate response ceilings. The finding count includes the omission marker,
# and the text budget counts UTF-8 bytes from both messages and paths. The marker
# is reserved up front so truncation can always be reported within both limits.
_MAX_FINDINGS = 100
_MAX_FINDING_TEXT_BYTES = 100_000
_MAX_FINDING_PATH_BYTES = 2_000
_OMISSION_MESSAGE = "Additional validation findings omitted."
_PATH_TRUNCATION_SUFFIX = "... (path truncated)"


def _capped(message: str) -> str:
    """Bound one finding's text before it reaches a response body.

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


def _capped_path(path: Optional[str]) -> Optional[str]:
    """Bound one finding path in the byte unit used by response serialization."""
    if path is None:
        return None
    encoded = path.encode("utf-8")
    if len(encoded) <= _MAX_FINDING_PATH_BYTES:
        return path

    suffix_bytes = _PATH_TRUNCATION_SUFFIX.encode("utf-8")
    prefix_budget = _MAX_FINDING_PATH_BYTES - len(suffix_bytes)
    prefix = encoded[:prefix_budget].decode("utf-8", errors="ignore")
    return f"{prefix}{_PATH_TRUNCATION_SUFFIX}"


class _FindingCollector:
    """Apply one count and aggregate-text budget across every finding producer."""

    def __init__(self) -> None:
        self._findings: list[ValidationMessage] = []
        self._remaining_regular_slots = _MAX_FINDINGS - 1
        self._remaining_text_bytes = _MAX_FINDING_TEXT_BYTES - len(
            _OMISSION_MESSAGE.encode("utf-8")
        )
        self._omitted_severity: Optional[Severity] = None

    @property
    def remaining_regular_slots(self) -> int:
        """Number of findings available before the reserved marker slot."""
        return self._remaining_regular_slots

    def add(self, finding: ValidationMessage) -> bool:
        """Add one normalized finding, or mark it omitted and reject it."""
        normalized = ValidationMessage(
            finding.severity,
            _capped(finding.message),
            _capped_path(finding.path),
        )
        text_bytes = len(normalized.message.encode("utf-8"))
        if normalized.path is not None:
            text_bytes += len(normalized.path.encode("utf-8"))

        if self._remaining_regular_slots == 0 or text_bytes > self._remaining_text_bytes:
            self.mark_omitted(normalized.severity)
            return False

        self._findings.append(normalized)
        self._remaining_regular_slots -= 1
        self._remaining_text_bytes -= text_bytes
        return True

    def mark_omitted(self, severity: Severity) -> None:
        """Record omission without counting or exhausting the producer."""
        if self._omitted_severity is None or severity == "error":
            self._omitted_severity = severity

    def finalize(self) -> list[ValidationMessage]:
        """Return findings with exactly one trailing marker when truncated."""
        if self._omitted_severity is None:
            return list(self._findings)
        return [
            *self._findings,
            ValidationMessage(self._omitted_severity, _OMISSION_MESSAGE),
        ]


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


def _collect_non_string_key_findings(metadata: object, collector: _FindingCollector) -> bool:
    """Add non-string mapping keys in document order until the budget fills.

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

    ``seen`` skips any container already walked, keyed on ``id()``. That removes
    alias amplification at its source and costs no coverage: a shared subtree
    cannot hold different keys on a second visit. Comparing identity is sound
    because every value stays reachable from ``metadata`` for the duration, so
    nothing can be collected and no id recycled midway.

    A shared value that is *schema*-invalid still yields one finding per
    referencing path because jsonschema does not memoize, while a shared
    *non-string key* yields exactly one at the first path reaching it. A client
    highlighting findings against a document should not assume one convention.

    The collector replaces the former accumulated list. No intermediate children
    list is built, and a rejected candidate stops traversal immediately.

    Returns:
        ``True`` when the walk completed, or ``False`` when the collector rejected
        a finding and traversal stopped immediately.
    """
    seen: set[int] = set()

    def walk(value: object, path: str) -> bool:
        if not isinstance(value, (dict, list)):
            return True
        if id(value) in seen:
            return True
        seen.add(id(value))

        if isinstance(value, dict):
            # Preserve the previous direct-keys-before-descendants ordering without
            # materializing an unbounded intermediate children list.
            for key in value:
                child_path = f"{path}.{key}" if path else str(key)
                if not isinstance(key, str):
                    if not collector.add(
                        ValidationMessage(
                            "error",
                            f"Mapping key {key!r} is a {type(key).__name__}, not a string. "
                            f"Profile fields are string-keyed; quote it as '{key}'.",
                            child_path,
                        )
                    ):
                        return False
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                if not walk(child, child_path):
                    return False
        else:
            for index, child in enumerate(value):
                child_path = f"{path}.{index}" if path else str(index)
                if not walk(child, child_path):
                    return False

        return True

    return walk(metadata, "")


def validate_frontmatter(metadata: dict) -> list[ValidationMessage]:
    """Validate a frontmatter dict against the schema and CAO conventions.

    Returns a bounded prefix in stable producer order: deprecated fields, then
    non-string mapping keys, then a bounded JSON-Schema prefix sorted by path,
    then ``allowedTools`` vocabulary warnings, then the role check. At most
    ``_MAX_FINDINGS`` are returned, including one final omission marker when the
    shared count or aggregate-text budget fills. An empty list means the profile
    is valid with no advisories.

    A document outside the structural ceilings is the one exception to that
    order: it is reported and nothing further runs, because the later steps are
    exactly what such a document is expensive in.
    """
    collector = _FindingCollector()

    # 1. Deprecated fields first, before ``additionalProperties: false``
    #    rejects them with a less helpful message.
    for field in sorted(_DEPRECATED_FIELDS):
        if field in metadata:
            if not collector.add(
                ValidationMessage(
                    "warning",
                    f"'{field}' is deprecated and rejected by CAO 2.2+. "
                    f"Use 'allowedTools' instead.",
                )
            ):
                return collector.finalize()

    # 2. Structural ceilings, before anything traverses or validates the
    #    document: rendered size, nesting depth, and cycles.
    structural = _structural_bound_finding(metadata)
    if structural is not None:
        collector.add(structural)
        return collector.finalize()

    # 3. Non-string mapping keys, which JSON Schema cannot see. Stream findings
    #    into the shared collector and stop the walk as soon as its budget fills.
    if not _collect_non_string_key_findings(metadata, collector):
        return collector.finalize()

    # 4. JSON-Schema structural validation. Consume only the remaining capacity
    #    plus one lookahead before sorting, so the iterator and sort allocation
    #    are bounded without exhausting the omitted tail merely to count it.
    validator = Draft202012Validator(load_profile_schema())
    remaining = collector.remaining_regular_slots
    sampled_errors = list(islice(validator.iter_errors(metadata), remaining + 1))
    omitted_schema_errors = len(sampled_errors) > remaining
    for error in sorted(sampled_errors[:remaining], key=lambda e: [str(p) for p in e.path]):
        path = ".".join(str(p) for p in error.absolute_path) or "(root)"
        if not collector.add(ValidationMessage("error", error.message, path)):
            return collector.finalize()
    if omitted_schema_errors:
        collector.mark_omitted("error")
        return collector.finalize()

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
                if not collector.add(
                    ValidationMessage(
                        "warning",
                        f"allowedTools entry '{tool}' is not in CAO's recognized "
                        f"vocabulary. It may be silently ignored by some providers.",
                    )
                ):
                    return collector.finalize()

    # 6. Role check (advisory — custom roles are valid but worth flagging).
    #
    # Same hashing hazard as above: ``role: [developer]`` is unhashable. The
    # schema reports the type error, so this advisory check simply stands aside.
    role = metadata.get("role")
    if isinstance(role, str) and role and role not in _BUILTIN_ROLES:
        collector.add(
            ValidationMessage(
                "warning",
                f"role '{role}' is not a built-in CAO role "
                f"({', '.join(sorted(_BUILTIN_ROLES))}). "
                f"Ensure it is defined in your settings.json custom roles.",
            )
        )

    return collector.finalize()


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
