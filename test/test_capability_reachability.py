"""Capability-reachability guards (issue #1005, generalising #814 and #752).

The defect class: a capability is DECLARED on a base class and never CONSULTED
on a decision path. Behaviour then falls back to a weaker signal with no error
and no test failure -- #814 (``assume_processing_on_dispatch``'s True branch
unreachable) and #752 are both instances.

Three directions are guarded, over four declaration surfaces:

1. Forward (``TestCapabilitiesAreReachable``): every declared capability has at
   least one consumer in ``src/`` outside its declaring module, or an
   ALLOWLIST entry with a reason. A capability several providers override and
   nothing reads is the silent-fallback shape exactly.
2. Reverse (``TestNoStaleCapabilityReads``): every capability-shaped
   ``getattr(obj, "name", default)`` literal resolves to a capability some base
   class really declares. That idiom is how these flags are read, and its
   default is what makes a renamed or typo'd flag fall back silently instead
   of raising ``AttributeError``.
3. Anti-rot (``TestScanItselfIsWired``): each surface must still find
   declarations, so a refactor that moves a base class turns the guard red
   rather than vacuously green.

The scan lives in ``test/support/capability_scan.py`` so new surfaces are a
list entry, not new code.
"""

import ast
from functools import lru_cache
from test.support.capability_scan import (
    REPO_ROOT,
    SRC_ROOT,
    ScanResult,
    declared_capabilities,
    find_references,
    parse_cached,
    python_files,
    scan_surface,
)

import pytest

# One entry per (base class, implementation glob) pair.
SURFACES = {
    "provider-bool-capabilities": dict(
        declaring_module=SRC_ROOT / "providers/base.py",
        class_name="BaseProvider",
        kinds=("bool_attr", "bool_prop"),
        implementation_glob="providers/*.py",
        include_strings=True,
    ),
    "provider-opt-in-hooks": dict(
        declaring_module=SRC_ROOT / "providers/base.py",
        class_name="BaseProvider",
        kinds=("opt_in_hook",),
        implementation_glob="providers/*.py",
        include_strings=True,
    ),
    "backend-capability-methods": dict(
        declaring_module=SRC_ROOT / "backends/base.py",
        class_name="TerminalBackend",
        kinds=("bool_attr", "bool_prop", "bool_method"),
        implementation_glob="backends/*.py",
        include_strings=True,
    ),
    "plugin-event-names": dict(
        declaring_module=SRC_ROOT / "plugins/events.py",
        class_name="CaoEvent",
        kinds=("event_type",),
        implementation_glob="plugins/**/*.py",
        include_strings=True,
    ),
}

# Capability -> why it is allowed to have no consumer on a decision path.
# An entry here is a documented debt, not a way to silence the scan -- state
# what reads it instead, or when it will be read. `test_allowlist_entries_are_
# still_needed` expires an entry automatically once a real consumer lands.
ALLOWLIST: dict[str, str] = {
    "wait_until_input_ready": (
        "BaseProvider.wait_until_input_ready (providers/base.py) is called only by "
        "ClaudeCodeProvider.initialize, on its own override (providers/claude_code.py). "
        "A consumer inside the only overriding file proves that implementation calls its "
        "own method, not that any other provider's override ever runs (#1005 P2-2). Other "
        "providers that override this hook must call it themselves until a generic caller "
        "(e.g. in BaseProvider's lifecycle) reads it instead -- remove this entry then."
    ),
}

# Capability name prefixes that mark a `getattr` literal as a capability read.
CAPABILITY_PREFIXES = (
    "supports_",
    "allow_",
    "accepts_",
    "blocks_",
    "assume_",
    "honors_",
    "status_recheck_",
)

# getattr literals with a capability-shaped name that are NOT base-class
# capabilities. Each needs a reason.
NON_CAPABILITY_READS: dict[str, str] = {}


@lru_cache(maxsize=None)
def _scan(surface: str) -> tuple[ScanResult, ...]:
    """Cache one surface's scan so the forward and allowlist tests, which both
    scan every surface, do not each re-walk the same trees (#1005 P3-3)."""
    return tuple(scan_surface(**SURFACES[surface]))


def _all_declared_names() -> set[str]:
    names: set[str] = set()
    for kwargs in SURFACES.values():
        names.update(
            c.name
            for c in declared_capabilities(
                kwargs["declaring_module"],
                kwargs["class_name"],
                kinds=kwargs["kinds"],
            )
        )
    return names


class TestCapabilitiesAreReachable:
    """A declared capability nothing reads is a feature that does not exist."""

    @pytest.mark.parametrize("surface", sorted(SURFACES))
    def test_every_declared_capability_has_a_consumer(self, surface):
        results = _scan(surface)
        assert results, f"{surface}: scan found no declarations -- the guard has rotted"

        dead = [r for r in results if not r.reachable and r.capability.name not in ALLOWLIST]
        detail = "; ".join(
            f"{r.capability.name} declared {r.capability.where} "
            f"({len(r.overrides)} override(s), {len(r.consumers)} consumer(s), "
            "all inside overriding files)"
            for r in dead
        )
        assert not dead, (
            f"{surface}: these capabilities are declared (and some are overridden by "
            "implementations) yet no decision path in src/ ever reads them, so behaviour "
            f"silently falls back to a weaker signal -- the #814/#752 class: {detail}. "
            "Consult them on the decision path, delete them, or add an ALLOWLIST entry "
            "with a reason."
        )

    def test_allowlist_entries_are_still_needed(self):
        """An allowlist entry is debt; drop it once the capability is consumed.

        Written as one test over the dict rather than a parametrisation so an
        empty ALLOWLIST -- the desired state -- reports as a pass, not a skip.
        """
        missing_reason = [name for name, reason in ALLOWLIST.items() if not reason.strip()]
        assert not missing_reason, f"allowlist entries need a reason: {missing_reason}"

        undeclared = sorted(set(ALLOWLIST) - _all_declared_names())
        assert not undeclared, (
            f"these ALLOWLIST entries name no capability any surface declares: {undeclared}. "
            "The capability was likely deleted or renamed -- drop the stale entry, or it "
            "will silently pre-silence a future dead capability that reuses the name."
        )

        if not ALLOWLIST:
            return  # nothing to re-scan src/ for.

        reachable = {
            r.capability.name for surface in SURFACES for r in _scan(surface) if r.reachable
        }
        stale = sorted(set(ALLOWLIST) & reachable)
        assert not stale, (
            f"These now have a consumer: {stale}. Remove them from ALLOWLIST so the "
            "scan protects them from going dead again."
        )

    def test_allowlist_entry_for_an_undeclared_name_is_caught(self, monkeypatch):
        """Regression for #1005 P3-4: a deleted/renamed capability's entry must
        not linger forever and silently pre-silence a future dead capability
        that reuses the same name."""
        monkeypatch.setitem(
            ALLOWLIST, "supports_capability_that_was_deleted", "stale entry for this test"
        )
        with pytest.raises(AssertionError, match="no capability any surface declares"):
            TestCapabilitiesAreReachable().test_allowlist_entries_are_still_needed()


class TestNoStaleCapabilityReads:
    """The reverse direction: a capability read must name a real capability."""

    @staticmethod
    def _capability_getattr_literals():
        """Yield (name, file:line) for every ``getattr(x, "<capability>", ...)``."""
        for path in python_files(SRC_ROOT):
            tree = parse_cached(path)
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
                    and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)
                ):
                    continue
                name = node.args[1].value
                if name.startswith(CAPABILITY_PREFIXES):
                    yield name, f"{path.relative_to(REPO_ROOT)}:{node.lineno}"

    def test_every_capability_getattr_resolves_to_a_declaration(self):
        declared = _all_declared_names()
        reads = list(self._capability_getattr_literals())
        assert reads, "found no capability getattr reads -- the pattern has rotted"

        orphans = [
            f"{name} at {where}"
            for name, where in reads
            if name not in declared and name not in NON_CAPABILITY_READS
        ]
        assert not orphans, (
            "These read a capability-shaped attribute that no scanned base class "
            "declares. Because `getattr` carries a default, the read cannot raise -- it "
            f"just always takes the fallback branch: {orphans}. Fix the name, declare the "
            "capability, or add a NON_CAPABILITY_READS entry with a reason."
        )


class TestConsumerLiteralPrecision:
    """A string literal only proves a decision-path read in a proving position.

    Regression for the false negative both reviewers independently built: a
    bare ``LABEL = "supports_x"`` assignment used to count as a CONSUMER
    merely because its value matched a capability name, letting a declared,
    provider-overridden, never-consulted capability (the #814/#752 shape)
    pass the guard undetected.
    """

    def test_bare_literal_assignment_is_not_a_consumer(self, tmp_path):
        declaring = tmp_path / "base.py"
        declaring.write_text("class Base:\n    supports_probe: bool = False\n")
        decoy = tmp_path / "decoy.py"
        decoy.write_text('PROBE_LABEL = "supports_probe"\n')

        refs = find_references(
            ["supports_probe"],
            declaring_module=declaring,
            implementation_files=[],
            search_files=[declaring, decoy],
            include_strings=True,
        )
        assert not refs["supports_probe"].consumers, (
            "a bare string-literal assignment must not count as a consumer -- "
            "it shares the capability's name without reading it"
        )

    @pytest.mark.parametrize(
        "source",
        [
            'getattr(provider, "supports_probe", False)\n',
            '@hook("supports_probe")\ndef on_probe():\n    pass\n',
            '_MAP = {"supports_probe": "launch"}\n',
            'if event_type == "supports_probe":\n    pass\n',
        ],
    )
    def test_literal_in_a_proving_position_is_a_consumer(self, tmp_path, source):
        declaring = tmp_path / "base.py"
        declaring.write_text("class Base:\n    supports_probe: bool = False\n")
        reader = tmp_path / "reader.py"
        reader.write_text(source)

        refs = find_references(
            ["supports_probe"],
            declaring_module=declaring,
            implementation_files=[],
            search_files=[declaring, reader],
            include_strings=True,
        )
        assert refs["supports_probe"].consumers, f"expected a consumer for: {source!r}"

    @pytest.mark.parametrize(
        "assignment",
        [
            "self.supports_probe: bool = True\n",
            "self.supports_probe, self._x = True, 1\n",
        ],
    )
    def test_store_context_attribute_assignment_is_not_a_consumer(self, tmp_path, assignment):
        """Regression for P2-2 kind 2: an annotated or tuple-unpacked ``self.x =
        ...`` is a Store-context assignment, not a read, whatever shape the
        target takes."""
        declaring = tmp_path / "base.py"
        declaring.write_text("class Base:\n    supports_probe: bool = False\n")
        impl = tmp_path / "impl.py"
        impl.write_text(f"class Impl(Base):\n    def __init__(self):\n        {assignment}")

        refs = find_references(
            ["supports_probe"],
            declaring_module=declaring,
            implementation_files=[impl],
            search_files=[declaring, impl],
            include_strings=True,
        )
        assert not refs[
            "supports_probe"
        ].consumers, (
            f"a Store-context attribute assignment must not count as a consumer: {assignment!r}"
        )

    @pytest.mark.parametrize(
        "assignment",
        [
            "self.supports_probe: bool = True\n",
            "self.supports_probe, self._x = True, 1\n",
        ],
    )
    def test_annotated_or_tuple_override_is_recorded_as_an_override(self, tmp_path, assignment):
        """Regression for #1005 P2-1: an annotated or tuple-unpacked ``self.x =
        ...`` must register as an OVERRIDE, so a same-file read of it does not
        count as an outside consumer."""
        declaring = tmp_path / "base.py"
        declaring.write_text("class Base:\n    supports_probe: bool = False\n")
        impl = tmp_path / "impl.py"
        impl.write_text(
            "class Impl(Base):\n"
            f"    def __init__(self):\n        {assignment}"
            "    def decide(self):\n        return self.supports_probe\n"
        )

        refs = find_references(
            ["supports_probe"],
            declaring_module=declaring,
            implementation_files=[impl],
            search_files=[declaring, impl],
            include_strings=True,
        )
        result = refs["supports_probe"]
        assert result.overrides, f"expected impl.py to register as an override: {assignment!r}"
        assert not result.reachable, (
            "a same-file read of an annotated or tuple-unpacked override must not make "
            f"the capability reachable: {assignment!r}"
        )

    def test_declaring_module_own_read_is_not_a_consumer(self, tmp_path):
        """Regression for #1005 P3-2: the declaring module's own internal reads
        are excluded wholesale, even with no override anywhere."""
        declaring = tmp_path / "base.py"
        declaring.write_text(
            "class Base:\n"
            "    supports_probe: bool = False\n"
            "    def decide(self):\n"
            "        return self.supports_probe\n"
        )

        refs = find_references(
            ["supports_probe"],
            declaring_module=declaring,
            implementation_files=[],
            search_files=[declaring],
            include_strings=True,
        )
        result = refs["supports_probe"]
        assert (
            not result.reachable
        ), "a read inside the declaring module itself must not count as a consumer"

    def test_self_call_inside_the_only_override_is_not_reachable(self, tmp_path):
        """Regression for P2-2 kind 1 (the live ``wait_until_input_ready`` shape):
        a self-call inside the sole overriding implementation proves only that
        implementation uses its own method, not that anything outside it does."""
        declaring = tmp_path / "base.py"
        declaring.write_text(
            "class Base:\n    def wait_until_input_ready(self) -> None:\n        pass\n"
        )
        impl = tmp_path / "impl.py"
        impl.write_text(
            "class Impl(Base):\n"
            "    def initialize(self):\n"
            "        self.wait_until_input_ready()\n"
            "    def wait_until_input_ready(self) -> None:\n"
            "        ...\n"
        )

        refs = find_references(
            ["wait_until_input_ready"],
            declaring_module=declaring,
            implementation_files=[impl],
            search_files=[declaring, impl],
            include_strings=True,
        )
        result = refs["wait_until_input_ready"]
        assert result.overrides, "expected impl.py to register as an override"
        assert not result.reachable, (
            "a self-call inside the only overriding file must not make the capability "
            "reachable -- it proves only that this implementation uses its own method"
        )


class TestEventEmissionRequiresInstantiation:
    """Regression for P2-1: a subscriber or lookup table must not prove an event fires.

    The only proof that a decision path ever dispatches a ``CaoEvent`` subclass
    is instantiating it; a ``@hook("post_x")`` subscriber or an event-name
    lookup table vouches for receipt, not dispatch.
    """

    def test_subscribed_but_never_instantiated_event_is_not_reachable(self, tmp_path):
        declaring = tmp_path / "events.py"
        declaring.write_text(
            "class CaoEvent:\n    pass\n\n\n"
            'class PostProbeEvent(CaoEvent):\n    event_type: str = "post_probe"\n'
        )
        subscriber = tmp_path / "subscriber.py"
        subscriber.write_text('@hook("post_probe")\ndef on_probe():\n    pass\n')

        results = {
            r.capability.name: r
            for r in scan_surface(
                declaring_module=declaring,
                class_name="CaoEvent",
                kinds=("event_type",),
                implementation_glob="*.py",
                include_strings=True,
                search_root=tmp_path,
            )
        }
        assert not results["post_probe"].reachable, (
            "a @hook subscriber alone must not prove the event is ever emitted -- "
            "nothing in src/ instantiates PostProbeEvent"
        )

    def test_event_instantiated_only_in_declaring_module_is_not_reachable(self, tmp_path):
        """Regression for #1005 P3-2: the declaring module's own instantiation
        does not count either -- ``events.py`` naming its own event proves
        nothing about a decision path outside it."""
        declaring = tmp_path / "events.py"
        declaring.write_text(
            "class CaoEvent:\n    pass\n\n\n"
            'class PostProbeEvent(CaoEvent):\n    event_type: str = "post_probe"\n\n\n'
            "PostProbeEvent()\n"
        )

        results = {
            r.capability.name: r
            for r in scan_surface(
                declaring_module=declaring,
                class_name="CaoEvent",
                kinds=("event_type",),
                implementation_glob="*.py",
                include_strings=True,
                search_root=tmp_path,
            )
        }
        assert not results["post_probe"].reachable, (
            "instantiating the event class inside its own declaring module must not "
            "count as a consumer"
        )

    def test_instantiated_event_is_reachable(self, tmp_path):
        declaring = tmp_path / "events.py"
        declaring.write_text(
            "class CaoEvent:\n    pass\n\n\n"
            'class PostProbeEvent(CaoEvent):\n    event_type: str = "post_probe"\n'
        )
        emitter = tmp_path / "emitter.py"
        emitter.write_text("from events import PostProbeEvent\n\nPostProbeEvent()\n")

        results = {
            r.capability.name: r
            for r in scan_surface(
                declaring_module=declaring,
                class_name="CaoEvent",
                kinds=("event_type",),
                implementation_glob="*.py",
                include_strings=True,
                search_root=tmp_path,
            )
        }
        assert results["post_probe"].reachable, "instantiating the event class must count"


class TestScanItselfIsWired:
    """Anti-rot: a moved base class must fail loudly, not pass vacuously."""

    @pytest.mark.parametrize("surface", sorted(SURFACES))
    def test_surface_still_resolves(self, surface):
        kwargs = SURFACES[surface]
        assert kwargs["declaring_module"].is_file(), (
            f"{surface}: declaring module {kwargs['declaring_module']} is gone -- "
            "point the surface at its new home"
        )
        caps = declared_capabilities(
            kwargs["declaring_module"], kwargs["class_name"], kinds=kwargs["kinds"]
        )
        assert caps, (
            f"{surface}: {kwargs['class_name']} declares nothing matching "
            f"{kwargs['kinds']} -- the declaration shape changed and the scan is now "
            "vacuous"
        )

    def test_known_capabilities_are_present(self):
        """Spot-check the names #814/#752 were about, so a rename is noticed."""
        declared = _all_declared_names()
        for expected in (
            "assume_processing_on_dispatch",
            "allow_raw_transcript_fallback",
            "supports_screen_detection",
            "supports_event_inbox",
        ):
            assert expected in declared, f"{expected} no longer declared -- update the scan"
