"""Containment tests — Agent Plugins §4.1, correctness property P3.

§4.1 permits a symlink whose target resolves *within* the plugin root and
requires rejecting one that escapes, so every assertion here is about
**realpath** behaviour rather than string shape. A lexical implementation would
pass the ``../`` cases and fail every symlink case.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cli_agent_orchestrator.agent_plugins.containment import (
    canonical_root,
    is_within_root,
    resolve_within_root,
)


@pytest.fixture
def root(tmp_path) -> Path:
    inside = tmp_path / "plugin"
    (inside / "skills" / "demo").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.txt").write_text("secret", encoding="utf-8")
    return inside


class TestBasicContainment:
    def test_root_itself_is_contained(self, root):
        assert resolve_within_root(root, ".") == Path(os.path.realpath(root))

    def test_descendant_is_contained(self, root):
        resolved = resolve_within_root(root, "skills/demo")
        assert resolved == Path(os.path.realpath(root / "skills" / "demo"))

    def test_nonexistent_descendant_is_still_contained(self, root):
        # A path that does not exist yet is not an escape. The validator relies
        # on this to distinguish "plugin.json is missing" from "plugin.json
        # points outside the root" — two very different findings.
        assert resolve_within_root(root, "plugin.json") is not None

    def test_relative_parent_escape_is_rejected(self, root):
        assert resolve_within_root(root, "../outside/secret.txt") is None

    def test_absolute_path_outside_is_rejected(self, root, tmp_path):
        assert resolve_within_root(root, tmp_path / "outside") is None

    def test_dotdot_that_returns_inside_is_accepted(self, root):
        # Lexically alarming, genuinely contained. Rejecting it would be wrong.
        assert resolve_within_root(root, "skills/../skills/demo") is not None

    def test_sibling_prefix_is_not_contained(self, tmp_path):
        # `/x/plugin-extra` must not count as inside `/x/plugin` just because
        # the string starts with it. This is the classic prefix bug.
        (tmp_path / "plugin").mkdir()
        (tmp_path / "plugin-extra").mkdir()
        assert resolve_within_root(tmp_path / "plugin", tmp_path / "plugin-extra") is None


class TestSymlinks:
    def test_symlink_resolving_inside_is_permitted(self, root):
        link = root / "linked-skill"
        link.symlink_to(root / "skills" / "demo", target_is_directory=True)
        assert resolve_within_root(root, link) is not None

    def test_symlink_escaping_root_is_rejected(self, root, tmp_path):
        link = root / "escape"
        link.symlink_to(tmp_path / "outside", target_is_directory=True)
        assert resolve_within_root(root, link) is None

    def test_escape_through_a_symlinked_parent_is_rejected(self, root, tmp_path):
        # The lexical path never leaves the root; only realpath reveals it does.
        bridge = root / "skills" / "bridge"
        bridge.symlink_to(tmp_path / "outside", target_is_directory=True)
        assert resolve_within_root(root, "skills/bridge/secret.txt") is None

    def test_symlink_loop_does_not_raise(self, root):
        a = root / "loop-a"
        b = root / "loop-b"
        a.symlink_to(b)
        b.symlink_to(a)
        # Containment must answer, not hang or explode, on a cyclic link.
        resolve_within_root(root, a)
        assert is_within_root(root, "skills/demo")


class TestDegenerateInputs:
    def test_missing_root_canonicalizes_but_is_not_a_directory(self, tmp_path):
        assert canonical_root(tmp_path / "nope") is not None

    def test_non_path_candidate_returns_none(self, root):
        assert resolve_within_root(root, 42) is None  # type: ignore[arg-type]

    def test_empty_candidate_resolves_to_the_root(self, root):
        assert resolve_within_root(root, "") == Path(os.path.realpath(root))


# --- Property 3: Containment ------------------------------------------------
# Validates: Requirements 7.1, 7.2, 7.3, 7.4


_SEGMENTS = st.sampled_from(["a", "b", "..", ".", "skills", "demo", "nested"])


@given(segments=st.lists(_SEGMENTS, min_size=0, max_size=8))
@settings(
    max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_property_containment_matches_realpath(root, segments):
    """Whatever the answer, it agrees with a realpath comparison.

    This is the property stated in design.md: for every path the validator would
    accept, ``realpath(p)`` is the root or a descendant of it.
    """
    candidate = os.path.join(*segments) if segments else "."
    resolved = resolve_within_root(root, candidate)
    root_real = os.path.realpath(root)

    if resolved is None:
        joined = os.path.realpath(os.path.join(root_real, candidate))
        assert joined != root_real and not joined.startswith(root_real + os.sep)
    else:
        real = os.path.realpath(resolved)
        assert real == root_real or real.startswith(root_real + os.sep)


@given(depth=st.integers(min_value=1, max_value=12))
@settings(
    max_examples=50, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_property_parent_traversal_always_escapes(root, depth):
    """Enough ``../`` always escapes, at every depth."""
    assert resolve_within_root(root, "/".join([".."] * depth) + "/outside") is None


@given(name=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_", min_size=1, max_size=16))
@settings(
    max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_property_plain_names_are_always_contained(root, name):
    """A single ordinary path segment is never an escape."""
    assert resolve_within_root(root, name) is not None
