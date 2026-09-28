"""Compute and classify a step's call fingerprint (issue #583, unit ``step-fingerprint``).

One definition of "is this the same call?", and a stored value whose provenance is readable
from the value itself. Every replay decision in Bolt 1B routes on this module's output, so a
wrong field list produces either a false REPLAY (a stale result served as fresh) or a
permanent false ``DIVERGED`` (replay never fires at all).

LEAF MODULE: imports ``hashlib``, ``dataclasses`` and ``typing`` ONLY. No I/O, no state, no
configuration, and NO LOGGING OF ANY KIND (SR-1/SR-2). Both are security requirements rather
than style preferences, and both are PRESERVATION requirements — the pure-function shape
already has them, and a later "improvement" is what would take them away:

* SR-1 — nothing is persisted. ``compute`` takes fields, returns a digest, stores nothing.
  Its inputs include ``prompt``, which is exactly where a credential shows up in practice
  (pasted into an instruction, or interpolated by a script). Storing the inputs would create
  a SECOND durable home for that text with its own redaction obligation and its own eviction
  gap. A hash needs no redaction, which is why this module can touch a prompt and remain the
  safest module in the Bolt.
* SR-2 — no field value is echoed into a log line, a message, or an exception. SR-1's whole
  benefit is destroyed by one helpful log line: the obvious diagnostic instinct ("log the
  prompt so we can see what changed") moves a credential out of a hash and into a log file,
  which is typically world-readable on the host and often shipped off it. This module
  therefore has no logger and no ``print``, matching ``secret_gate.py``'s and
  ``step_result.py``'s posture.

SR-3 — ``sha256`` is an IDENTITY function here, not a security boundary. The unit claims
collision resistance for identity and claims nothing about authentication: the fingerprint is
stored in the same row it protects, so anyone able to write ``workflow_run_step``'s
``call_fingerprint`` can simply write whatever value makes a replay match without needing a
collision. Integrity of the journal file is the filesystem's job.

WHAT THIS MODULE DOES NOT DO. It does not assemble the field values (units 6 and 8, including
resolving the effective working directory after inheritance), does not write the column
(``begin_step``, unit 6), and decides nothing (``replay-gate``, Bolt 1B, routes on
``scheme_of``'s answer). It only makes the value and makes its provenance legible.
"""

import hashlib
from dataclasses import dataclass
from typing import Literal, Optional

CREATION_ONLY = "\x01creation-only"
"""Substituted for each of the four creation-only components on a reuse call (BR-1a).

NEVER EDIT THIS VALUE. Editing it silently invalidates every stored reuse-path fingerprint
WITH NO SCHEME CHANGE TO DETECT THE BREAK: a stored ``v2:`` value stays ``v2:`` while meaning
something different, so ``scheme_of``'s classification cannot help and the gate's
classify-before-compare cannot help either — the two populations become indistinguishable
*within* one scheme. That is the one failure mode the versioning design has no answer for.

THIS IS ALSO WHY THE CONSTANT LIVES HERE AND NOT IN ``constants.py`` (TD-2). Every other
member of the ``WORKFLOW_`` family there is a number someone may legitimately revise — a byte
bound, a millisecond timeout. Placing a never-edit value among tunables is an invitation to
exactly that edit. Beside ``compute`` its role is unmistakable: it is part of the hash
contract, like the ``\\x00`` separator and the ``v2:`` prefix, neither of which anyone would
think to hoist into a constants module.

THE VALUE IS NOT A SECURITY MECHANISM (SR-5). It exists to prevent a false ``DIVERGED``, not
to resist forgery, and it is NOT unforgeable: POSIX permits any byte but NUL and ``/`` in a
path, so a directory named exactly ``"\\x01creation-only"`` is constructible. It still cannot
cause a false verdict, because the guarantee is POSITIONAL rather than lexical —
``reused_terminal`` is itself a hashed component, so a create call and a reuse call differ in
that position whatever the other components hold. The leading ``\\x01`` (which ``MODEL_ID_RE``
forbids in a model id, and which no real tool name or directory would carry) is defence in
depth, not the argument.
"""

# The scheme marker carried by every value ``compute`` emits (BR-7/INV-4). Named rather than
# inlined because ``compute`` writes it and ``scheme_of`` reads it: two literals is the drift
# that would let one side stop recognising the other's output.
_V2_PREFIX = "v2:"

# Joins the ten components. Inherited from the three-field scheme it replaces, so the v1 -> v2
# transition is a change of INPUTS only (TD-1) — exactly what the scheme prefix is designed to
# express. See ``compute``'s docstring for the one framing limit this separator carries.
_COMPONENT_SEP = "\x00"

# Joins the members of ``allowed_tools`` INSIDE component 6. Distinct from
# ``_COMPONENT_SEP`` so a tool name can never be mistaken for a component boundary.
_TOOL_SEP = "\x1f"

# Substituted for an optional component that was not supplied. MUST stay distinct from ""
# (BR-7): "not supplied" is not "supplied empty", and the distinction is load-bearing —
# ``working_directory=None`` triggers CWD inheritance while ``working_directory=""`` does not,
# so collapsing them would hash two genuinely different calls the same. Never edit, for the
# same reason as ``CREATION_ONLY``; kept private because no caller or test needs the literal.
_NOT_SUPPLIED = "\x02not-supplied"

# Decimal places used to render ``timeout``. PART OF THE HASH CONTRACT: changing it silently
# rewrites every fingerprint, exactly like editing ``CREATION_ONLY``. Fixed precision is what
# keeps ``600`` and ``600.0`` from forking one call's identity into two.
_TIMEOUT_PRECISION = 6


@dataclass(frozen=True)
class StepCallFields:
    """The execution-affecting inputs to one agent step — ten hashed components (BR-1).

    NEVER PERSISTED. It exists only to be hashed; the *hash* is persisted, the fields are not.
    This is the type's most load-bearing property and it is a security property, not an
    efficiency one (SR-1).

    THE FIELD LIST IS CLOSED, AND ``frozen=True`` IS WHAT MAKES THAT STRUCTURAL. An open list
    invites a later caller to pass "one more thing" and silently change every fingerprint on
    disk, so adding a field is a visible, reviewable edit to this module rather than a
    call-site decision.

    FOUR FIELDS ARE CREATION-ONLY — ``model``, ``allowed_tools``,
    ``effective_working_directory`` and ``use_worktree`` are each replaced by
    :data:`CREATION_ONLY` when ``reused_terminal`` is ``True`` (BR-1a). ``run_agent_step``
    passes all four to ``terminal_service.create_terminal`` (``agent_step.py:409-421``), which
    runs only inside ``if created_here:`` (``:368``); the reuse branch (``:455-457``) validates
    the terminal and sends the prompt without consulting them. Hashing a value the
    implementation discards manufactures a false ``DIVERGED``.

    ``engine`` IS DELIBERATELY NOT IN THAT SET. It is used on BOTH paths —
    ``_validate_reused_terminal(terminal_id, provider, engine)`` (``agent_step.py:457``) — so
    it stays unconditionally hashed. Verified in the control flow rather than assumed by
    symmetry with the other four, and it is the single easiest thing to get wrong here.

    Args:
        provider: the provider CLI. A different binary is a different agent runtime.
        agent: the profile name. Selects system prompt, tool set and default model. Only the
            NAME is hashed, never the profile's content — an edited profile replays under its
            old definition, accepted as environment rather than a call input.
        prompt: the instruction itself.
        model: per-call model override. CREATION-ONLY.
        engine: Kiro engine selection, as the enum's ``value`` (a plain ``str``) — the CALLER
            normalises, because doing it here would mean importing ``enum`` and breaking this
            module's leaf-import property (SR-1). A ``KiroEngine`` member's ``repr`` is not
            stable across versions, which is why the ``value`` is the contract.
        allowed_tools: the capability SET. Sorted before hashing (BR-4) because
            ``("Read", "Write")`` and ``("Write", "Read")`` grant identical capability, and
            order-sensitivity would force a human decision on every resume of a script whose
            tool list was merely reordered. CREATION-ONLY.
        effective_working_directory: the directory the step ACTUALLY ran in, after
            ``run_agent_step``'s inheritance (``agent_step.py:379`` onwards) — NOT
            ``RunStepRequest.working_directory`` as posted (BR-5). When
            ``working_directory is None and caller_id is not None`` the effective directory
            comes from the caller terminal's CWD, so hashing the request's ``None`` would give
            two runs that executed in genuinely different directories the same fingerprint.
            CREATION-ONLY. Assembling this value imposes a timing constraint on units 6 and 8:
            compute AFTER cwd resolution and BEFORE terminal creation.
        use_worktree: an isolated git worktree versus the shared directory is a different
            filesystem. CREATION-ONLY.
        reused_terminal: DERIVED, never the raw ``reuse_terminal_id`` (BR-6). The raw id is
            run-scoped, so hashing it would make every handoff-style call read ``DIVERGED``
            and replay would never fire. The boolean keeps the execution-affecting fact — a
            reused terminal carries prior conversation context, a fresh one does not. Accepted
            limit: two runs that reuse *different* terminals hash identically.
        timeout: the per-call bound. Hashed so that a raised timeout after a timeout-failure
            reads as divergence rather than replaying the failure.
    """

    provider: str
    agent: str
    prompt: str
    model: Optional[str] = None
    engine: Optional[str] = None
    allowed_tools: Optional[tuple[str, ...]] = None
    effective_working_directory: Optional[str] = None
    use_worktree: bool = False
    reused_terminal: bool = False
    timeout: float = 600.0


def _text(value: Optional[str]) -> str:
    """Normalise an optional string component, keeping ``None`` distinct from ``""`` (BR-7)."""
    return _NOT_SUPPLIED if value is None else value


def _flag(value: bool) -> str:
    """Normalise a boolean to ONE fixed spelling — never ``str(bool)`` incidentally."""
    return "true" if value else "false"


def _timeout(value: float) -> str:
    """Normalise ``timeout`` to fixed-precision text so ``600`` and ``600.0`` are one identity.

    ``repr(600)`` and ``repr(600.0)`` differ, and an int reaching a ``float``-typed field is
    entirely ordinary in Python, so rendering the value's repr would fork one call's identity
    into two for no execution difference at all.
    """
    return f"{float(value):.{_TIMEOUT_PRECISION}f}"


def _tools(value: Optional[tuple[str, ...]]) -> str:
    """Normalise ``allowed_tools``: sorted (BR-4), length-prefixed, joined with ``_TOOL_SEP``.

    The length prefix is what makes the encoding injective. Without it ``()`` and ``("",)``
    would both render as the empty string — two different capability sets hashing alike — and
    a tool name containing ``_TOOL_SEP`` could impersonate two members.
    """
    if value is None:
        return _NOT_SUPPLIED
    ordered = sorted(value)
    return f"{len(ordered)}{_TOOL_SEP}" + _TOOL_SEP.join(ordered)


def compute(fields: StepCallFields) -> str:
    """Hash the ten components into ``"v2:<64 lowercase hex>"``.

    TEN COMPONENTS IN A FIXED ORDER, AND THE ORDER IS PART OF THE CONTRACT (BR-1). Reordering
    them changes every hash on disk with no scheme change to signal it — the same silent break
    as editing :data:`CREATION_ONLY`.

    ON A REUSE CALL, SUBSTITUTION IS PER-FIELD AND THE TUPLE IS NEVER SHORTENED (BR-1a). Four
    components become :data:`CREATION_ONLY`; the count stays ten on both paths, so a create
    call and a reuse call cannot collide by arity either. ``engine`` is NOT among them (see
    :class:`StepCallFields`).

    THE PREFIX IS PART OF THE VALUE, NOT A SEPARATE COLUMN (BR-7/INV-4). Provenance travels
    with the value, so a row read by any future consumer is self-describing without a schema
    lookup — and without it a legacy 64-hex value and a ``v2`` 64-hex value are
    indistinguishable, so the only available operation is equality, which ALWAYS fails across
    schemes. Every legacy row would then report ``DIVERGED`` ("the script changed between
    runs") when the truth is "this value was computed under narrower rules and cannot be
    verified" — two situations that need different verdicts and different remedies.

    TOTAL on well-typed input: it raises nothing (INV-2), which is also why no exception path
    can carry a field value (SR-2). A ``None`` where the signature says ``str`` is a type
    violation and surfaces as ``TypeError`` rather than being silently normalised — hashing it
    would hide the caller's bug behind a plausible digest.

    ONE STATED FRAMING LIMIT. The components are joined with a single ``\\x00``, inherited from
    the scheme this replaces (TD-1). That makes ordinary field boundaries safe — ``("a", "b")``
    cannot hash equal to ``("ab", "")`` — but it is NOT injective when a component itself
    contains ``\\x00``: ``agent="b\\x00c", prompt="d"`` and ``agent="b", prompt="c\\x00d"`` join
    to the same text. Only ``prompt`` realistically carries NUL bytes, and reaching the
    collision requires choosing two adjacent components at once, which per SR-3 is outside the
    threat model — an attacker who controls a workflow script already controls the step's
    execution and gains nothing from a collision. Recorded rather than hidden: closing it means
    length-prefixed framing, which is a scheme change and therefore a ``v3:`` decision.
    """
    if fields.reused_terminal:
        # ``run_agent_step`` DISCARDS these four on the reuse path (agent_step.py:455-457),
        # so hashing their values would manufacture a false DIVERGED (BR-1a): a script that
        # reuses a terminal and changes ``model`` between runs executes IDENTICALLY — both
        # values discarded — yet the fingerprints would differ and the gate would demand a
        # human decision that has no meaning.
        model = CREATION_ONLY
        allowed_tools = CREATION_ONLY
        effective_working_directory = CREATION_ONLY
        use_worktree = CREATION_ONLY
    else:
        model = _text(fields.model)
        allowed_tools = _tools(fields.allowed_tools)
        effective_working_directory = _text(fields.effective_working_directory)
        use_worktree = _flag(fields.use_worktree)

    components = (
        fields.provider,
        fields.agent,
        fields.prompt,
        model,
        _text(fields.engine),
        allowed_tools,
        effective_working_directory,
        use_worktree,
        _flag(fields.reused_terminal),
        _timeout(fields.timeout),
    )
    joined = _COMPONENT_SEP.join(components)
    return _V2_PREFIX + hashlib.sha256(joined.encode("utf-8")).hexdigest()


def scheme_of(stored: Optional[str]) -> Literal["v2", "legacy", "absent"]:
    """Classify a stored ``call_fingerprint`` by the scheme it was written under (BR-8).

    ``None`` -> ``absent`` (the row predates the column, or this unit); a value lacking the
    ``v2:`` prefix -> ``legacy`` (the three-field scheme this unit replaced); otherwise
    ``v2``.

    THREE-WAY RATHER THAN A BOOLEAN. ``absent`` and ``legacy`` route the same way today (both
    to ``DECISION_REQUIRED``) but they are different FACTS — "never recorded" versus "recorded
    under narrower rules" — and a later agent is asked to reconstruct events from persisted
    evidence. Collapsing them would discard that distinction permanently.

    VALIDITY IS NOT THE SCHEME'S QUESTION. ``"v2:xyz"`` classifies as ``v2``: the function
    reports what the value CLAIMS. Whether the digest is well-formed is a different concern
    and no consumer needs it, because a malformed ``v2`` value simply fails the subsequent
    equality comparison and reads as ``DIVERGED``.

    TOTAL over its declared type (INV-3): it raises on no ``str`` — including ``""`` and
    arbitrary malformed text — and none on ``None``. The bound is the declared type and that
    is deliberate: the only real caller reads a SQLite ``TEXT`` column, whose Python domain is
    exactly ``str | None``, so an ``isinstance`` guard here would be a branch no caller can
    reach.

    A LEGACY VALUE IS CLASSIFIED, NEVER COMPARED (BR-9). That routing belongs to
    ``replay-gate``; this function's contract is only that the two populations stay
    distinguishable forever, which holds because every value ``compute`` emits carries the
    prefix (INV-4).
    """
    if stored is None:
        return "absent"
    if stored.startswith(_V2_PREFIX):
        return "v2"
    return "legacy"
