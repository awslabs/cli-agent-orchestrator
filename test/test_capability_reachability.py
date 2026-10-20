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
from test.support.capability_scan import (
    REPO_ROOT,
    SRC_ROOT,
    declared_capabilities,
    find_references,
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
# Empty on purpose: as of this commit every declared capability on all four
# surfaces is consumed somewhere in src/. An entry here is a documented debt,
# not a way to silence the scan -- state what reads it instead, or when it will
# be read.
ALLOWLIST: dict[str, str] = {}

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
        results = scan_surface(**SURFACES[surface])
        assert results, f"{surface}: scan found no declarations -- the guard has rotted"

        dead = [r for r in results if not r.reachable and r.capability.name not in ALLOWLIST]
        detail = "; ".join(
            f"{r.capability.name} declared {r.capability.where} "
            f"({len(r.overrides)} override(s), 0 consumers)"
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

        reachable = {
            r.capability.name
            for surface in SURFACES
            for r in scan_surface(**SURFACES[surface])
            if r.reachable
        }
        stale = sorted(set(ALLOWLIST) & reachable)
        assert not stale, (
            f"These now have a consumer: {stale}. Remove them from ALLOWLIST so the "
            "scan protects them from going dead again."
        )


class TestNoStaleCapabilityReads:
    """The reverse direction: a capability read must name a real capability."""

    @staticmethod
    def _capability_getattr_literals():
        """Yield (name, file:line) for every ``getattr(x, "<capability>", ...)``."""
        for path in python_files(SRC_ROOT):
            tree = ast.parse(path.read_text())
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
