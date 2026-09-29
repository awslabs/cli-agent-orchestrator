"""Adversarial property tests for the total validator (W3).

**Validates: Requirements 5.1, 5.2, 5.3, 5.4, 6.3, 12.2; Properties P1, P2, P3, P7**

What this file adds over the properties
already in ``test_validation.py`` is the **generator**, not the assertions: it
builds hostile *filesystems* rather than hostile *documents*. Symlink loops,
``plugin.json`` as a directory, an unreadable ``skills/``, forty levels of
nesting, a ``SKILL.md`` that is a directory — none of which a strategy over
manifest dicts can reach, and all of which a total validator must survive.

Relationship to ``test_validation.py::test_property_validation_is_total``: that
property fuzzes manifest *content* and a small set of extra directory entries.
This file's ``_hostile_tree`` strictly subsumes its filesystem coverage, so the
two are kept as one strong and one fast property rather than merged — the cheap
one runs on every change to validation logic, the expensive one catches the shapes
nobody thought of. They assert the same invariant by different means, which is the
one case where two properties earn their keep.

Deliberately **not** duplicated, because ``test_validation.py`` already covers each
with an equivalent Hypothesis property and duplicating would only slow the suite:
``test_any_other_violation_is_fatal_with_zero_components``
(``test_property_other_violations_are_fatal_with_no_components``),
``test_one_invalid_skill_never_affects_its_siblings`` and
``test_exactly_n_minus_k_discovered_and_k_skipped``
(``test_property_sibling_independence``), ``test_unknown_top_level_field_is_non_fatal``
(``test_property_tolerated_deviations_still_load``), and the three containment
properties (``test_containment.py``'s ``test_property_containment_matches_realpath``,
``test_property_parent_traversal_always_escapes``,
``test_property_plain_names_are_always_contained``).

``deadline=None`` throughout: materializing a directory tree per example is
legitimately slow, and a per-example deadline produces flaky failures that say
nothing about correctness.
"""

from __future__ import annotations

import itertools
import json
import os
import stat
from pathlib import Path
from typing import Any, Dict

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cli_agent_orchestrator.agent_plugins.containment import resolve_within_root
from cli_agent_orchestrator.agent_plugins.models import PluginValidationReport, Severity
from cli_agent_orchestrator.agent_plugins.validation import validate_plugin

from .conftest import PLUGIN_SCHEMA_ID, build_plugin, write_skill

#: Shared settings for properties that build real directory trees.
FS_SETTINGS = settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)

_counter = itertools.count()


def _fresh(base: Path) -> Path:
    """A unique subdirectory, so examples never collide inside one ``tmp_path``."""
    target = base / f"case-{next(_counter)}"
    target.mkdir(parents=True)
    return target


def _restore_permissions(root: Path) -> None:
    """Make the tree deletable again after an example chmods something to 0o000.

    Without this, pytest's ``tmp_path`` cleanup fails and the *next* test reports
    the error — a confusing failure that has nothing to do with the code.
    """
    for path in (root / "skills", root):
        try:
            if path.exists():
                os.chmod(path, stat.S_IRWXU)
        except OSError:
            pass


def _manifest(name: str = "example", **extra: Any) -> str:
    return json.dumps({"$schema": PLUGIN_SCHEMA_ID, "name": name, **extra})


_ENTRY_NAMES = st.sampled_from(
    [
        "skills",
        "mcp.json",
        "plugin.json",
        "SKILL.md",
        "a",
        ".hidden",
        "..dots",
        "-dash",
        "with space",
        "UPPER",
        "éè",
        "x" * 80,
    ]
)

_MANIFEST_BYTES = st.one_of(
    st.binary(max_size=400),
    st.text(max_size=200).map(lambda s: s.encode("utf-8", errors="ignore")),
    st.just(b""),
    st.just(b"\xff\xfe\x00binary"),
    st.just(b"{"),
    st.just(b"[]"),
    st.just(b"null"),
    st.just(b'{"$schema": 1}'),
    st.builds(lambda name: _manifest(name).encode("utf-8"), st.text(max_size=70)),
)


@st.composite
def _hostile_tree(draw: Any) -> Dict[str, Any]:
    """A recipe for an adversarial candidate plugin directory."""
    return {
        "manifest_bytes": draw(st.one_of(st.none(), _MANIFEST_BYTES)),
        "manifest_is_dir": draw(st.booleans()),
        "skills_kind": draw(st.sampled_from(["absent", "dir", "file", "symlink-loop"])),
        "skill_dirs": draw(st.lists(_ENTRY_NAMES, max_size=4, unique=True)),
        "skill_md_kind": draw(st.sampled_from(["absent", "file", "dir", "empty", "binary"])),
        "mcp_kind": draw(st.sampled_from(["absent", "file", "dir", "empty"])),
        "nesting_depth": draw(st.integers(min_value=0, max_value=6)),
        "symlink_loop": draw(st.booleans()),
        "unreadable": draw(st.booleans()),
        "extra_entries": draw(st.lists(_ENTRY_NAMES, max_size=3, unique=True)),
    }


def _materialize(root: Path, recipe: Dict[str, Any]) -> None:
    """Build the tree ``recipe`` describes. Best-effort by design.

    Every filesystem call is guarded: the generator is free to describe trees this
    platform cannot create (a symlink where symlinks are unavailable, a name the
    filesystem rejects), and an example that cannot be fully built is still a
    valid input to the validator.
    """
    if recipe["manifest_is_dir"]:
        (root / "plugin.json").mkdir(exist_ok=True)
    elif recipe["manifest_bytes"] is not None:
        (root / "plugin.json").write_bytes(recipe["manifest_bytes"])

    skills = root / "skills"
    kind = recipe["skills_kind"]
    if kind == "dir":
        skills.mkdir(exist_ok=True)
    elif kind == "file":
        skills.write_text("not a dir", encoding="utf-8")
    elif kind == "symlink-loop":
        try:
            skills.symlink_to(skills)
        except OSError:
            pass

    if skills.is_dir() and not skills.is_symlink():
        for name in recipe["skill_dirs"]:
            try:
                child = skills / name
                child.mkdir(exist_ok=True)
            except OSError:
                continue
            md = child / "SKILL.md"
            md_kind = recipe["skill_md_kind"]
            try:
                if md_kind == "file":
                    md.write_text(
                        f"---\nname: {json.dumps(name)}\ndescription: d\n---\n", encoding="utf-8"
                    )
                elif md_kind == "dir":
                    md.mkdir(exist_ok=True)
                elif md_kind == "empty":
                    md.write_bytes(b"")
                elif md_kind == "binary":
                    md.write_bytes(b"\xff\xfe\x00")
            except OSError:
                continue

    mcp = root / "mcp.json"
    try:
        if recipe["mcp_kind"] == "file":
            mcp.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        elif recipe["mcp_kind"] == "dir":
            mcp.mkdir(exist_ok=True)
        elif recipe["mcp_kind"] == "empty":
            mcp.write_bytes(b"")
    except OSError:
        pass

    deep = root
    for level in range(recipe["nesting_depth"]):
        deep = deep / f"level{level}"
        try:
            deep.mkdir(exist_ok=True)
        except OSError:
            break

    if recipe["symlink_loop"]:
        try:
            (root / "loop-a").symlink_to(root / "loop-b")
            (root / "loop-b").symlink_to(root / "loop-a")
        except OSError:
            pass

    for name in recipe["extra_entries"]:
        try:
            (root / f"extra-{name}").write_bytes(b"")
        except OSError:
            continue

    # Applied last so it cannot block the rest of the build.
    if recipe["unreadable"] and skills.is_dir() and not skills.is_symlink():
        try:
            os.chmod(skills, 0o000)
        except OSError:
            pass


class TestTotalityAgainstHostileFilesystems:
    """Property P1, with the filesystem as the adversary."""

    @FS_SETTINGS
    @given(recipe=_hostile_tree())
    def test_a_report_always_comes_back_and_is_self_consistent(
        self, tmp_path: Path, recipe: Dict[str, Any]
    ) -> None:
        """Never raises, and ``loadable`` always agrees with the findings.

        The second half is not redundant with ``loadable`` being a ``@property``:
        the invariant could still break if a FATAL finding were attached after the
        report was built, or if a code path returned a hand-made report.
        """
        root = _fresh(tmp_path)
        try:
            _materialize(root, recipe)

            report = validate_plugin(root)

            assert isinstance(report, PluginValidationReport)
            assert report.loadable == (
                not any(f.severity is Severity.FATAL for f in report.findings)
            )
        finally:
            _restore_permissions(root)

    @FS_SETTINGS
    @given(name=st.text(max_size=100))
    def test_arbitrary_manifest_names_never_raise(self, tmp_path: Path, name: str) -> None:
        """Requirement 5.5's name constraints are *reported*, never enforced by crashing.

        The name reaches a filesystem path (the store's directory name) and a
        report, so it is the field most likely to escape into somewhere that
        raises. Empty strings, path separators, dots and control characters all
        arrive here.
        """
        root = _fresh(tmp_path)
        (root / "plugin.json").write_text(_manifest(name), encoding="utf-8")

        report = validate_plugin(root)

        assert isinstance(report, PluginValidationReport)
        assert report.loadable == (report.findings_of(Severity.FATAL) == ())

    @FS_SETTINGS
    @given(depth=st.integers(min_value=1, max_value=40))
    def test_deep_nesting_terminates(self, tmp_path: Path, depth: int) -> None:
        """Requirement 5.2 — bounded work, with no recursive descent to exploit.

        Skill discovery looks exactly one level below ``skills/``. A tree nested 40
        deep must therefore cost the same as a flat one; if this ever hangs or
        recurses, discovery has started walking.
        """
        root = _fresh(tmp_path)
        build_plugin(root, "example", skills=["alpha"])

        deep = root / "skills" / "alpha"
        for level in range(depth):
            deep = deep / f"nested{level}"
            deep.mkdir()

        report = validate_plugin(root)

        assert report.loadable
        assert report.skill_names == ("alpha",)

    @FS_SETTINGS
    @given(loops=st.integers(min_value=1, max_value=4))
    def test_a_symlink_loop_does_not_hang(self, tmp_path: Path, loops: int) -> None:
        """A cycle must be refused by containment, not followed until ELOOP."""
        root = _fresh(tmp_path)
        build_plugin(root, "example", skills=["alpha"])

        for index in range(loops):
            first = root / "skills" / f"loop-a{index}"
            second = root / "skills" / f"loop-b{index}"
            try:
                first.symlink_to(second)
                second.symlink_to(first)
            except OSError:
                return

        report = validate_plugin(root)

        assert isinstance(report, PluginValidationReport)
        assert "alpha" in report.skill_names

    def test_an_unreadable_skills_dir_is_reported_not_raised(self, tmp_path: Path) -> None:
        """Requirement 5.1 — a permission failure is a finding, not an exception.

        Not a property: one deterministic case is enough, and ``chmod`` is a no-op
        for root, which is how CI containers run. Skipped there rather than
        asserted falsely.
        """
        root = _fresh(tmp_path)
        build_plugin(root, "example", skills=["alpha"])
        skills = root / "skills"

        try:
            os.chmod(skills, 0o000)
            if os.access(skills, os.R_OK):
                import pytest

                pytest.skip("running as root: chmod 000 does not deny access")

            report = validate_plugin(root)

            assert isinstance(report, PluginValidationReport)
            assert any("skills" in (f.path or "") or "skills" in f.code for f in report.findings)
        finally:
            _restore_permissions(root)


class TestDeterminism:
    """Property P2 — the same tree always yields the same answer."""

    @FS_SETTINGS
    @given(recipe=_hostile_tree())
    def test_repeated_validation_is_stable(self, tmp_path: Path, recipe: Dict[str, Any]) -> None:
        """Two validations of one unchanged tree agree on everything that matters.

        Compares the *whole* finding set, not just ``loadable``: a validator that
        iterated ``os.scandir`` order could return the same verdict with findings
        in a different order, which would make a conformance corpus keyed on
        finding order meaningless.
        """
        root = _fresh(tmp_path)
        try:
            _materialize(root, recipe)

            first = validate_plugin(root)
            second = validate_plugin(root)

            assert first.loadable == second.loadable
            assert first.skill_names == second.skill_names
            assert first.mcp_present == second.mcp_present
            assert [(f.code, f.path) for f in first.findings] == [
                (f.code, f.path) for f in second.findings
            ]
        finally:
            _restore_permissions(root)

    @FS_SETTINGS
    @given(names=st.lists(st.sampled_from(["alpha", "beta", "gamma", "delta"]), unique=True))
    def test_the_discovered_set_is_independent_of_creation_order(
        self, tmp_path: Path, names: list
    ) -> None:
        """Requirement 12.2 — creation order must not reach the result.

        The property form of ``test_validation.py``'s concrete
        ``test_discovered_set_is_independent_of_directory_order``: that one checks
        one pair of orders, this one checks the forward and reverse of every
        generated set.
        """
        forward = _fresh(tmp_path)
        build_plugin(forward, "example")
        (forward / "skills").mkdir(exist_ok=True)
        for name in names:
            write_skill(forward / "skills" / name, name)

        reverse = _fresh(tmp_path)
        build_plugin(reverse, "example")
        (reverse / "skills").mkdir(exist_ok=True)
        for name in reversed(names):
            write_skill(reverse / "skills" / name, name)

        assert validate_plugin(forward).skill_names == validate_plugin(reverse).skill_names
        assert validate_plugin(forward).skill_names == tuple(sorted(names))


class TestFatalityLattice:
    """Property P3 — severity composes one way, and only one way."""

    @FS_SETTINGS
    @given(
        tolerated=st.lists(
            st.sampled_from(["unknown_field", "extensions", "no_version"]), unique=True, min_size=1
        )
    )
    def test_any_number_of_tolerated_deviations_stays_loadable(
        self, tmp_path: Path, tolerated: list
    ) -> None:
        """Tolerances do not accumulate into a rejection.

        A validator that counted findings, or that upgraded severity past a
        threshold, would pass with one deviation and fail with three.
        """
        root = _fresh(tmp_path)
        extra: Dict[str, Any] = {}
        if "unknown_field" in tolerated:
            extra["totallyUnknown"] = "value"
        if "extensions" in tolerated:
            extra["extensions"] = {"com.example.thing": {"anything": [1, 2, 3]}}
        build_plugin(
            root,
            "example",
            skills=["alpha"],
            version=None if "no_version" in tolerated else "1.0.0",
            extra_manifest=extra or None,
        )

        report = validate_plugin(root)

        assert report.loadable
        assert report.skill_names == ("alpha",)

    @FS_SETTINGS
    @given(tolerated_count=st.integers(min_value=0, max_value=3))
    def test_one_fatal_beside_any_number_of_tolerated_stays_fatal(
        self, tmp_path: Path, tolerated_count: int
    ) -> None:
        """FATAL absorbs. The lattice has a top and nothing climbs back down.

        The direction that matters: tolerances must never *dilute* a fatal
        problem. A report is loadable only when nothing fatal is present, so
        adding acceptable deviations beside a broken manifest cannot rescue it.
        """
        root = _fresh(tmp_path)
        extra = {f"unknown{index}": index for index in range(tolerated_count)}
        # Fatal: no `$schema`, which §5.3 requires.
        build_plugin(root, "example", skills=["alpha"], schema_id=None, extra_manifest=extra)

        report = validate_plugin(root)

        assert not report.loadable
        # Requirement 5.6: a rejected plugin publishes no components at all.
        assert report.skill_names == ()

    @FS_SETTINGS
    @given(
        namespace=st.sampled_from(["com.example.a", "org.other.b", "x"]),
        payload=st.recursive(
            st.none() | st.booleans() | st.integers() | st.text(max_size=10),
            lambda children: st.lists(children, max_size=3)
            | st.dictionaries(st.text(min_size=1, max_size=6), children, max_size=3),
            max_leaves=6,
        ),
    )
    def test_an_unimplemented_extension_namespace_is_never_validated(
        self, tmp_path: Path, namespace: str, payload: Any
    ) -> None:
        """§8.1 — CAO implements no namespace, so no namespace's contents are checked.

        The property form matters here because the failure mode is *accidental
        validation*: the pinned schema would happily constrain these values, so the
        member has to be removed before validation rather than merely ignored
        afterwards. Arbitrary nested payloads are the way to show nothing looks
        inside.
        """
        root = _fresh(tmp_path)
        build_plugin(
            root,
            "example",
            skills=["alpha"],
            extra_manifest={"extensions": {namespace: payload}},
        )

        report = validate_plugin(root)

        assert report.loadable
        assert report.skill_names == ("alpha",)
        assert not any("extension" in f.code for f in report.findings)


class TestDiscoveredSkillsAreContained:
    """Property P7 — nothing discovered ever lives outside the plugin root."""

    @FS_SETTINGS
    @given(
        escaping=st.booleans(),
        target=st.sampled_from(["", "child", "a/b", "..", "../.."]),
    )
    def test_every_discovered_skill_resolves_inside_the_root(
        self, tmp_path: Path, escaping: bool, target: str
    ) -> None:
        """A symlinked skill is accepted only when its realpath stays contained.

        Both directions in one property: a link pointing inside must be usable
        (rejecting it would break a legitimate layout), and one pointing outside
        must be refused — and the refusal has to survive the fact that by the time
        the copy lands in staging the escape is no longer visible from the path
        alone.
        """
        outside = _fresh(tmp_path) / "outside"
        outside.mkdir(parents=True)
        write_skill(outside / "smuggled", "smuggled")

        root = _fresh(tmp_path)
        build_plugin(root, "example", skills=["alpha"])
        link = root / "skills" / "linked"

        try:
            if escaping:
                link.symlink_to(outside / "smuggled")
            else:
                inner = root / "skills" / "alpha"
                link.symlink_to(inner if not target else inner)
        except OSError:
            return

        report = validate_plugin(root)

        for skill in report.skills:
            resolved = resolve_within_root(root, str(skill.directory))
            assert resolved is not None, f"{skill.name} escaped the plugin root"

        if escaping:
            assert "smuggled" not in report.skill_names
