"""Tests for the step call fingerprint (issue #583, unit ``step-fingerprint``).

Covers ``services/step_fingerprint.py``: the ten hashed components, the four that are
creation-only, the ``v2:`` scheme marker, and the classification of a stored value.

TWO OF THESE TESTS CARRY THE FILE:

* ``test_sentinel_valued_field_on_create_does_not_collide_with_reuse`` is the only thing that
  proves BR-1a's guarantee is POSITIONAL rather than lexical. It fails if an implementation
  shortens the component tuple on the reuse path instead of substituting per-field — the
  shortened form lets a create call whose ``effective_working_directory`` happens to equal
  ``CREATION_ONLY`` hash identically to a reuse call.
* ``TestCreationOnlySentinel`` needs BOTH halves. Asserting only that the four creation-only
  fields leave a REUSE fingerprint unchanged would also pass under a blanket exclusion of
  those four from the hash — which would be wrong, because on a CREATE call every one of them
  changes what the step produces. ``test_creation_only_fields_do_change_a_create_fingerprint``
  is the half that catches that, and ``test_engine_still_changes_a_reuse_fingerprint`` is the
  half that catches ``engine`` being sentinelised along with them.

OUT OF SCOPE HERE. Assembling the field values, including resolving the effective working
directory after inheritance (units 6 and 8); writing the column and the re-baseline rule
(``begin_step``, unit 6); routing on ``scheme_of``'s answer (``replay-gate``, Bolt 1B). This
unit tests that a field is IN the hash, not that a caller filled it correctly.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import re
from dataclasses import fields as dataclass_fields
from pathlib import Path

from cli_agent_orchestrator.constants import WORKFLOW_ENV_ALLOWLIST
from cli_agent_orchestrator.services import script_runner, step_fingerprint
from cli_agent_orchestrator.services.step_fingerprint import (
    CREATION_ONLY,
    StepCallFields,
    compute,
    scheme_of,
)

_V2_FORM = re.compile(r"^v2:[0-9a-f]{64}$")

# A fully-populated CREATE call. Every optional carries a real value so that flipping any one
# field is a genuine change rather than a default-to-default no-op.
_CREATE = StepCallFields(
    provider="kiro_cli",
    agent="dev",
    prompt="go",
    model="claude-sonnet-4-5",
    engine="v2",
    allowed_tools=("Read", "Write"),
    effective_working_directory="/repo",
    use_worktree=False,
    reused_terminal=False,
    timeout=600.0,
)

# The same call against a REUSED terminal. Identical but for ``reused_terminal``.
_REUSE = StepCallFields(
    provider="kiro_cli",
    agent="dev",
    prompt="go",
    model="claude-sonnet-4-5",
    engine="v2",
    allowed_tools=("Read", "Write"),
    effective_working_directory="/repo",
    use_worktree=False,
    reused_terminal=True,
    timeout=600.0,
)


# ---------------------------------------------------------------------------
# BR-1 — ten components are hashed, and the list is closed
# ---------------------------------------------------------------------------
class TestTenComponents:
    """Each of the ten components, changed ALONE, changes the fingerprint (FR-2's pass
    condition stated literally). Every case starts from a CREATE call, because four of the ten
    are creation-only and would legitimately not change a reuse fingerprint (BR-1a)."""

    def test_provider_changes_the_fingerprint(self):
        other = StepCallFields(**{**vars(_CREATE), "provider": "claude_code"})
        assert compute(_CREATE) != compute(other)

    def test_agent_changes_the_fingerprint(self):
        other = StepCallFields(**{**vars(_CREATE), "agent": "reviewer"})
        assert compute(_CREATE) != compute(other)

    def test_prompt_changes_the_fingerprint(self):
        other = StepCallFields(**{**vars(_CREATE), "prompt": "stop"})
        assert compute(_CREATE) != compute(other)

    def test_model_changes_the_fingerprint(self):
        other = StepCallFields(**{**vars(_CREATE), "model": "claude-opus-4-1"})
        assert compute(_CREATE) != compute(other)

    def test_engine_changes_the_fingerprint(self):
        other = StepCallFields(**{**vars(_CREATE), "engine": "v1"})
        assert compute(_CREATE) != compute(other)

    def test_allowed_tools_changes_the_fingerprint(self):
        other = StepCallFields(**{**vars(_CREATE), "allowed_tools": ("Read", "Bash")})
        assert compute(_CREATE) != compute(other)

    def test_effective_working_directory_changes_the_fingerprint(self):
        """BR-5: the value hashed is the EFFECTIVE directory, so two runs whose agents worked
        in different real directories cannot replay each other's result. The resolution itself
        belongs to units 6 and 8 and is not tested here."""
        other = StepCallFields(**{**vars(_CREATE), "effective_working_directory": "/other"})
        assert compute(_CREATE) != compute(other)

    def test_use_worktree_changes_the_fingerprint(self):
        other = StepCallFields(**{**vars(_CREATE), "use_worktree": True})
        assert compute(_CREATE) != compute(other)

    def test_reused_terminal_changes_the_fingerprint(self):
        """BR-6: the boolean is hashed. This is also what makes BR-1a's guarantee positional —
        a create call and a reuse call differ in THIS component whatever the others hold."""
        assert compute(_CREATE) != compute(_REUSE)

    def test_timeout_changes_the_fingerprint(self):
        """Hashed per Q2's asymmetry: a false DIVERGED costs one human decision, whereas a
        false REPLAY silently pins a failure and ignores the author's raised timeout."""
        other = StepCallFields(**{**vars(_CREATE), "timeout": 900.0})
        assert compute(_CREATE) != compute(other)


# ---------------------------------------------------------------------------
# BR-1 / BR-2 / BR-6 — the field list is CLOSED, and six inputs are excluded
# ---------------------------------------------------------------------------
def test_field_list_is_exactly_the_ten_and_excludes_the_six():
    """BR-1's closed list and BR-2's six exclusions, asserted STRUCTURALLY.

    The six excluded inputs — ``session_name``, ``reuse_terminal_id``, ``teardown``,
    ``caller_id``, ``env_vars``, ``ready_timeout`` — cannot be tested behaviourally the way
    the ten can: ``StepCallFields`` has no such fields, so there is no "two calls differing
    only in ``session_name``" to construct. Their absence from the frozen dataclass IS the
    assertion, and it is the half of FR-2 that a naive "hash everything" implementation fails.

    It also carries BR-6's second clause — the module exposes no parameter accepting a
    terminal id — and INV-5: ``provider``/``agent``/``prompt`` (the legacy three-field scheme)
    remain a strict subset of the ten, so no caller loses coverage.

    THIS TEST FAILS THE MOMENT SOMEONE ADDS A FIELD, WHICH IS ITS PURPOSE. An open field list
    lets a later caller pass "one more thing" and silently change every fingerprint already on
    disk. Do not "fix" a failure here by extending the expected tuple — a new component is a
    fingerprint-scheme decision that owes a ``v3:`` prefix or a deliberate re-baseline.
    """
    assert tuple(f.name for f in dataclass_fields(StepCallFields)) == (
        "provider",
        "agent",
        "prompt",
        "model",
        "engine",
        "allowed_tools",
        "effective_working_directory",
        "use_worktree",
        "reused_terminal",
        "timeout",
    )
    present = {f.name for f in dataclass_fields(StepCallFields)}
    for excluded in (
        "session_name",
        "reuse_terminal_id",
        "teardown",
        "caller_id",
        "env_vars",
        "ready_timeout",
    ):
        assert excluded not in present, f"{excluded} is excluded by BR-2 and must not be hashed"
    # INV-5: the legacy scheme's three fields survive intact.
    assert {"provider", "agent", "prompt"} <= present


# ---------------------------------------------------------------------------
# BR-1a — four components are hashed ONLY when a terminal is created
# ---------------------------------------------------------------------------
class TestCreationOnlySentinel:
    """``run_agent_step`` DISCARDS ``model``, ``allowed_tools``,
    ``effective_working_directory`` and ``use_worktree`` on the reuse path
    (``agent_step.py:455-457`` does only ``_validate_reused_terminal`` and then sends the
    prompt), so hashing their values would manufacture a false ``DIVERGED``: a script that
    reuses a terminal and changes ``model`` between runs executes IDENTICALLY, yet the gate
    would demand a human decision that has no meaning.

    BOTH HALVES OF THIS CLASS ARE LOAD-BEARING. The four "unchanged on reuse" tests alone
    would also pass under a blanket exclusion of those fields from the hash, which would be
    wrong — see ``test_creation_only_fields_do_change_a_create_fingerprint``.
    """

    def test_model_does_not_change_a_reuse_fingerprint(self):
        other = StepCallFields(**{**vars(_REUSE), "model": "claude-opus-4-1"})
        assert compute(_REUSE) == compute(other)

    def test_allowed_tools_does_not_change_a_reuse_fingerprint(self):
        other = StepCallFields(**{**vars(_REUSE), "allowed_tools": ("Bash",)})
        assert compute(_REUSE) == compute(other)

    def test_effective_working_directory_does_not_change_a_reuse_fingerprint(self):
        """The resolution block is inside ``if created_here:`` (``agent_step.py:368``), so it
        never runs on a reuse call — the effective directory then belongs to the reused
        terminal rather than to this call's inputs."""
        other = StepCallFields(**{**vars(_REUSE), "effective_working_directory": "/elsewhere"})
        assert compute(_REUSE) == compute(other)

    def test_use_worktree_does_not_change_a_reuse_fingerprint(self):
        other = StepCallFields(**{**vars(_REUSE), "use_worktree": True})
        assert compute(_REUSE) == compute(other)

    def test_creation_only_fields_do_change_a_create_fingerprint(self):
        """THE HALF THAT KEEPS THE SENTINEL FROM BEING A BLANKET EXCLUSION.

        The same four pairs as above, on a CREATE call, must all differ. Without this, an
        implementation that simply dropped the four fields from the hash entirely would pass
        every other test in this class while losing four genuine execution-affecting inputs —
        a false REPLAY on the create path, which is the failure FR-1 exists to prevent.
        """
        for field, value in (
            ("model", "claude-opus-4-1"),
            ("allowed_tools", ("Bash",)),
            ("effective_working_directory", "/elsewhere"),
            ("use_worktree", True),
        ):
            other = StepCallFields(**{**vars(_CREATE), field: value})
            assert compute(_CREATE) != compute(other), f"{field} must be hashed on a create call"

    def test_engine_still_changes_a_reuse_fingerprint(self):
        """``engine`` is NEVER sentinelised — it is used on BOTH paths
        (``_validate_reused_terminal(terminal_id, provider, engine)``, ``agent_step.py:457``).
        Verified in the control flow rather than assumed by symmetry with the other four, and
        it is the single easiest thing to get wrong in this module.

        THIS TEST ASSERTS THAT A LIMIT EXISTS, NOT THAT IT IS DESIRABLE — and the alternative
        is worse, which is why the limit is accepted. ``_validate_reused_terminal`` returns
        early when ``requested_engine is None`` (``agent_step.py:77-78``), so on a reuse call
        ``engine=None`` and ``engine=<the terminal's persisted engine>`` proceed identically;
        hashing ``engine`` unconditionally therefore yields a false ``DIVERGED`` for a caller
        who switches between those two across runs. Sentinelising ``engine`` would be worse:
        an engine MISMATCH raises ``ValueError`` (``:93-97``) rather than executing, so a run
        that switched to a mismatching engine would hash identically to the prior successful
        run, the gate would REPLAY, and the validator would never be reached — the caller
        would receive a stale success where they should have received a mismatch error. A
        false ``DIVERGED`` costs one human decision; that false REPLAY silently swallows a
        real configuration error. DO NOT "FIX" THIS BY SENTINELISING ``engine``.
        """
        reuse_without_engine = StepCallFields(**{**vars(_REUSE), "engine": None})
        assert compute(_REUSE) != compute(reuse_without_engine)


def test_sentinel_valued_field_on_create_does_not_collide_with_reuse():
    """A CREATE call whose ``effective_working_directory`` literally equals ``CREATION_ONLY``
    still hashes differently from the same call with ``reused_terminal=True``.

    THIS IS THE TEST THAT PROVES THE GUARANTEE IS POSITIONAL, NOT LEXICAL. The sentinel is NOT
    unforgeable — POSIX permits any byte but NUL and ``/`` in a path, so a directory named
    exactly ``"\\x01creation-only"`` is constructible (SR-5 withdrew the earlier "unreachable
    value" claim as an overclaim). What makes a collision impossible is that
    ``reused_terminal`` is itself a hashed component, so the two paths differ in THAT position
    whatever the others hold.

    IT FAILS IF THE REUSE PATH SHORTENS THE COMPONENT TUPLE instead of substituting per-field:
    six components ending in the reuse flag can be made to coincide with ten whose
    creation-only slots already hold the sentinel. Per-field substitution keeps the count at
    ten on both paths, so the two cannot collide by arity either.
    """
    forged = StepCallFields(**{**vars(_CREATE), "effective_working_directory": CREATION_ONLY})
    reused = StepCallFields(**{**vars(forged), "reused_terminal": True})
    assert compute(forged) != compute(reused)


class TestGoldenVectors:
    """THE HASH CONTRACT, SPELLED OUT INDEPENDENTLY OF THE IMPLEMENTATION.

    Every other test here is differential — it compares two fingerprints and asserts they
    agree or differ. Differential tests cannot see three properties that BR-1/BR-1a/BR-7 all
    call load-bearing, because a wrong implementation moves BOTH sides of every comparison:

    * COMPONENT ORDER (BR-1). Two calls differing in one field differ under ANY ordering, so
      no differential test notices a reordering — yet reordering changes every hash on disk
      with no scheme change to signal it.
    * COMPONENT COUNT ON THE REUSE PATH (BR-1a). An implementation that SHORTENS the tuple to
      six instead of substituting per-field passes every differential test in this file,
      including the positional one: a six-component join and a ten-component join simply
      produce different text, so the create/reuse pairs still differ and the four
      creation-only fields still leave a reuse fingerprint unchanged.
    * THE SEPARATOR, THE SENTINEL'S PLACEMENT, AND ``timeout``'s PRECISION — each silently
      rewrites every stored value if edited.

    These two vectors are derived from the DESIGN, not read off the code: BR-1's field order,
    BR-1a's four substitutions with ``engine`` excluded, BR-7's ``\\x00`` join and ``v2:``
    prefix. IF ONE FAILS, RE-DERIVE THE EXPECTED STRING FROM THOSE RULES — do not paste in
    whatever the implementation now produces, because that is precisely the silent break the
    vectors exist to make loud.
    """

    @staticmethod
    def _digest(joined: str) -> str:
        return "v2:" + hashlib.sha256(joined.encode("utf-8")).hexdigest()

    def test_create_path_vector(self):
        """Ten components, in BR-1's order, NUL-joined. ``allowed_tools`` is sorted (BR-4) and
        length-prefixed inside its own component so that ``()`` and ``("",)`` cannot render
        alike; booleans use one fixed spelling; ``timeout`` is fixed-precision text."""
        joined = (
            "kiro_cli\x00"  # 1 provider
            "dev\x00"  # 2 agent
            "go\x00"  # 3 prompt
            "claude-sonnet-4-5\x00"  # 4 model
            "v2\x00"  # 5 engine
            "2\x1fRead\x1fWrite\x00"  # 6 allowed_tools, sorted + length-prefixed
            "/repo\x00"  # 7 effective_working_directory
            "false\x00"  # 8 use_worktree
            "false\x00"  # 9 reused_terminal
            "600.000000"  # 10 timeout
        )
        assert joined.count("\x00") == 9, "ten components means nine separators"
        assert compute(_CREATE) == self._digest(joined)

    def test_reuse_path_vector(self):
        """STILL TEN COMPONENTS. Four slots hold the sentinel; ``engine`` (slot 5) does NOT,
        because it is used on both code paths. This vector is the only thing in the file that
        fails when the reuse path shortens the tuple rather than substituting per-field."""
        joined = (
            "kiro_cli\x00"  # 1 provider
            "dev\x00"  # 2 agent
            "go\x00"  # 3 prompt
            "\x01creation-only\x00"  # 4 model         -> sentinel
            "v2\x00"  # 5 engine        -> NEVER sentinelised
            "\x01creation-only\x00"  # 6 allowed_tools -> sentinel
            "\x01creation-only\x00"  # 7 effective_working_directory -> sentinel
            "\x01creation-only\x00"  # 8 use_worktree  -> sentinel
            "true\x00"  # 9 reused_terminal
            "600.000000"  # 10 timeout
        )
        assert joined.count("\x00") == 9, "substitution is per-field; the count stays ten"
        assert compute(_REUSE) == self._digest(joined)


def test_creation_only_sentinel_value_is_pinned():
    """The sentinel's exact value is part of the hash contract, not an implementation detail.

    A mismatch between two code paths manufactures exactly the false ``DIVERGED`` BR-1a exists
    to prevent, so the value is pinned here as well as in the module. Its leading ``\\x01``
    cannot appear in a model id (``MODEL_ID_RE`` admits only ``[A-Za-z0-9._:/-]``) and no real
    tool name or directory would carry it — defence in depth, not the guarantee.

    IF THIS TEST FAILS, THE VALUE WAS EDITED, AND EVERY STORED REUSE-PATH FINGERPRINT IS NOW
    SILENTLY WRONG with no scheme change to detect it. Do not update the expected literal.
    """
    assert CREATION_ONLY == "\x01creation-only"


# ---------------------------------------------------------------------------
# BR-4 — ``allowed_tools`` is sorted before hashing
# ---------------------------------------------------------------------------
class TestAllowedToolsSorting:
    """A capability SET, not a sequence: ``("Read", "Write")`` and ``("Write", "Read")`` grant
    identical capability, so order-sensitivity would force a human decision on every resume of
    a script whose tool list was merely reordered."""

    def test_reordering_does_not_change_the_fingerprint(self):
        reordered = StepCallFields(**{**vars(_CREATE), "allowed_tools": ("Write", "Read")})
        assert compute(_CREATE) == compute(reordered)

    def test_a_different_member_changes_the_fingerprint(self):
        other = StepCallFields(**{**vars(_CREATE), "allowed_tools": ("Read", "Bash")})
        assert compute(_CREATE) != compute(other)

    def test_none_and_empty_tuple_are_distinguishable(self):
        """A supplied empty list is not the same as no list at all: ``None`` leaves the
        provider's default tool set in place, an empty tuple asks for none."""
        not_supplied = StepCallFields(**{**vars(_CREATE), "allowed_tools": None})
        empty = StepCallFields(**{**vars(_CREATE), "allowed_tools": ()})
        assert compute(not_supplied) != compute(empty)


# ---------------------------------------------------------------------------
# BR-7 — the returned form, determinism, and boundary safety
# ---------------------------------------------------------------------------
class TestReturnedForm:
    def test_form_is_v2_prefix_plus_64_lowercase_hex(self):
        """INV-4: every value ``compute`` emits carries the prefix, so it can never be mistaken
        for a legacy value — which is what keeps the two populations distinguishable forever
        (BR-9)."""
        value = compute(_CREATE)
        assert _V2_FORM.match(value), value
        assert scheme_of(value) == "v2"

    def test_compute_is_deterministic(self):
        """INV-1. ``sha256`` over text with no salt and no locale dependence, so two processes
        agree as readily as two calls."""
        assert compute(_CREATE) == compute(_CREATE)
        twin = StepCallFields(**vars(_CREATE))
        assert compute(_CREATE) == compute(twin)

    def test_field_boundaries_cannot_collide(self):
        """``("a", "b")`` must not hash equal to ``("ab", "")`` — the NUL separator's job. The
        three-field scheme this replaces already had the property; it matters MORE after the
        widening, which is when boundary collisions become reachable across ten components."""
        left = StepCallFields(provider="a", agent="b", prompt="c")
        right = StepCallFields(provider="ab", agent="", prompt="c")
        assert compute(left) != compute(right)

    def test_none_and_empty_string_are_distinguishable_for_an_optional(self):
        """The distinction is load-bearing beyond tidiness: ``working_directory=None`` triggers
        CWD inheritance while ``working_directory=""`` does not, so collapsing them would hash
        two genuinely different calls the same."""
        not_supplied = StepCallFields(**{**vars(_CREATE), "effective_working_directory": None})
        empty = StepCallFields(**{**vars(_CREATE), "effective_working_directory": ""})
        assert compute(not_supplied) != compute(empty)

    def test_integer_and_float_timeout_are_one_identity(self):
        """``600`` and ``600.0`` are the same bound and must not fork identity. An int reaching
        a ``float``-typed field is entirely ordinary in Python, and ``repr`` would differ — so
        the normalisation is fixed-precision text, not the value's repr."""
        as_int = StepCallFields(**{**vars(_CREATE), "timeout": 600})
        as_float = StepCallFields(**{**vars(_CREATE), "timeout": 600.0})
        assert compute(as_int) == compute(as_float)

    def test_all_optionals_none_hashes_without_raising(self):
        """INV-2: total on well-typed input. The minimal call — three required strings, every
        optional at its default — yields a valid ``v2:`` value."""
        assert _V2_FORM.match(compute(StepCallFields(provider="p", agent="a", prompt="")))

    def test_nul_bytes_in_a_component_hash_without_raising(self):
        """A prompt may contain anything, including NUL, and must not raise.

        THE SECOND ASSERTION DOCUMENTS A LIMIT RATHER THAN A DESIRED PROPERTY. Joining ten
        components with a single ``\\x00`` is not injective when a component itself contains
        ``\\x00``: ``agent="b\\x00c", prompt="d"`` and ``agent="b", prompt="c\\x00d"`` join to
        the same text and therefore hash alike. ``business-rules.md``'s edge-case row claims
        such collisions are "unreachable because the separator is the delimiter, not an
        escape", which overstates it — the counterexample below is the disproof.

        It is nevertheless accepted, not fixed: reaching it requires choosing two ADJACENT
        components at once, and per SR-3 there is no input-choosing adversary in the threat
        model — anyone who controls a workflow script already controls the step's execution and
        gains nothing from a collision. Closing it means length-prefixed framing, which is a
        scheme change and therefore a ``v3:`` decision, not a silent edit. This test pins
        current behaviour so that change cannot happen unnoticed.
        """
        assert _V2_FORM.match(compute(StepCallFields(provider="p", agent="a", prompt="x\x00y")))
        shifted_left = StepCallFields(provider="a", agent="b\x00c", prompt="d")
        shifted_right = StepCallFields(provider="a", agent="b", prompt="c\x00d")
        assert compute(shifted_left) == compute(shifted_right)


# ---------------------------------------------------------------------------
# BR-8 — ``scheme_of`` is total and three-way
# ---------------------------------------------------------------------------
class TestSchemeOf:
    """Three-way rather than a boolean: ``absent`` and ``legacy`` route the same way today but
    they are different FACTS — "never recorded" versus "recorded under narrower rules" — and
    collapsing them would discard that distinction permanently."""

    def test_none_is_absent(self):
        assert scheme_of(None) == "absent"

    def test_bare_hex_is_legacy(self):
        """A value written by the deleted three-field function: 64 hex characters, no prefix.
        BR-9 — it is CLASSIFIED, never equality-compared against a fresh ``v2`` value, because
        the two always differ and the comparison would report "the script changed" when the
        truth is "this value cannot be verified"."""
        legacy = hashlib.sha256("\x00".join(("kiro_cli", "dev", "go")).encode("utf-8")).hexdigest()
        assert len(legacy) == 64
        assert scheme_of(legacy) == "legacy"

    def test_prefixed_hex_is_v2(self):
        assert scheme_of("v2:" + "0" * 64) == "v2"

    def test_empty_string_is_legacy_and_does_not_raise(self):
        """INV-3: total. An empty column value is malformed, not a scheme."""
        assert scheme_of("") == "legacy"

    def test_malformed_v2_is_still_v2(self):
        """VALIDITY IS NOT THE SCHEME'S QUESTION. The prefix is the value's own CLAIM about how
        it was computed; a malformed digest simply fails the subsequent equality comparison and
        reads as ``DIVERGED``, which needs no separate verdict."""
        assert scheme_of("v2:xyz") == "v2"
        assert scheme_of("v2:") == "v2"


# ---------------------------------------------------------------------------
# BR-3 — ``env_vars`` is excluded because the allowlist forbids anything else
# ---------------------------------------------------------------------------
def test_workflow_env_allowlist_is_exactly_the_three_run_scoped_keys():
    """THIS TEST EXISTS TO FAIL WHEN THE ALLOWLIST WIDENS. DO NOT "FIX" IT BY UPDATING THE
    EXPECTED SET.

    ``env_vars`` is excluded from the fingerprint (BR-2/BR-3), and that exclusion is FORCED
    rather than chosen: every admitted key is run-scoped — the run id differs per run and the
    generation increments — so hashing them would make every fingerprint differ on every run,
    every settled row would read ``DIVERGED``, and replay would never fire. The feature would
    be inert.

    But the reason holds only while the allowlist stays narrow. Admitting a key that DOES
    affect execution turns the silent exclusion into a false REPLAY. A failure here is the
    moment a fingerprint decision is owed — answer it in ``step_fingerprint.py``, not in this
    assertion.
    """
    assert WORKFLOW_ENV_ALLOWLIST == {
        "CAO_WORKFLOW_RUN_ID",
        "CAO_WORKFLOW_STEP_ID",
        "CAO_WORKFLOW_GENERATION",
    }


# ---------------------------------------------------------------------------
# BR-10 / INV-7 — exactly one fingerprint function exists
# ---------------------------------------------------------------------------
def test_old_three_field_fingerprint_function_is_gone():
    """``script_runner._step_call_fingerprint`` is DELETED, not deprecated beside the new one.

    Two fingerprint functions in one codebase is the defect FR-2 exists to prevent: the route
    must not be able to compute identity two ways. The deleted function was private and had
    exactly one caller, so no deprecation period is owed.

    Its two properties did not go untested. Determinism and NUL boundary separation moved to
    ``TestReturnedForm`` above; the journal round-trip that pinned its output is asserted
    against the real production call site in
    ``test_settlement_rewire.py``, which drives ``run_agent_step`` and compares the digest
    reaching ``begin_step`` against ``compute``.
    """
    assert not hasattr(script_runner, "_step_call_fingerprint")
    source = Path(inspect.getsourcefile(script_runner) or "").read_text(encoding="utf-8")
    assert "_step_call_fingerprint" not in source


def test_compute_emits_a_v2_classifiable_value():
    """INV-7's other half: what the live call site produces is classifiable as CURRENT.

    The live call site is ``run_agent_step`` (issue #583, unit ``settlement-rewire`` BR-1),
    which supplies all ten fields in the one window BR-5 permits — after cwd resolution and
    before terminal creation. ``record_step_completion`` computed a three-field value here
    until that unit landed; it now computes none at all, because it runs at SETTLE, too late
    for BR-5. Round-tripping the real call site is
    ``test_settlement_rewire.py``'s job; this asserts only that ``compute``'s output is
    classifiable as current rather than as legacy.
    """
    assert scheme_of(compute(StepCallFields(provider="kiro_cli", agent="dev", prompt="go"))) == "v2"


# ---------------------------------------------------------------------------
# SR-1 / SR-2 — the module persists nothing and echoes nothing
# ---------------------------------------------------------------------------
class TestModulePurity:
    """Four of this unit's five security requirements are PRESERVATION requirements: the
    pure-function shape already has them, and a later "improvement" is what would take them
    away. These two tests are the forcing function for that."""

    @staticmethod
    def _module_ast() -> ast.Module:
        path = Path(inspect.getsourcefile(step_fingerprint) or "")
        return ast.parse(path.read_text(encoding="utf-8"))

    def test_module_imports_only_hashlib_dataclasses_and_typing(self):
        """SR-1: nothing is persisted, and the import list is where that is enforceable.

        The inputs include ``prompt``, which is exactly where a credential shows up in
        practice. Storing them would create a SECOND durable home for that text with its own
        redaction obligation and its own eviction gap; a hash needs neither. A failure here
        means someone added a file, socket, or database import to the safest module in the
        Bolt.
        """
        imported: set[str] = set()
        for node in ast.walk(self._module_ast()):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert imported == {"hashlib", "dataclasses", "typing"}, imported

    def test_neither_function_accepts_a_path_a_connection_or_a_writer(self):
        """SR-1, the other half: no persistence parameter can be smuggled in."""
        assert list(inspect.signature(compute).parameters) == ["fields"]
        assert list(inspect.signature(scheme_of).parameters) == ["stored"]

    def test_module_contains_no_logger_and_no_print(self):
        """SR-2: SR-1's benefit is destroyed by one helpful log line.

        The obvious diagnostic instinct — "log the prompt so we can see what changed" — moves
        a credential out of a hash and into a log file, which is typically world-readable on
        the host and often shipped off it. ``compute`` also raises nothing on well-typed input,
        so no exception path can carry a field value either (INV-2).

        The check walks CALLS in the AST rather than grepping the source, because this module's
        own docstrings discuss logging at length and a text search would match its prose.
        """
        forbidden = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
        for node in ast.walk(self._module_ast()):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                assert node.func.id != "print", "SR-2: no print in this module"
            elif isinstance(node.func, ast.Attribute):
                assert node.func.attr not in forbidden, f"SR-2: no {node.func.attr}() call here"
