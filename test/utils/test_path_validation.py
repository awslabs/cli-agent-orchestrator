"""Tests for the shared path validator (#345 D5, design test 14).

Covers the extracted ``resolve_and_validate_path`` in both its strict
(tmux working-directory) and archive (``allow_create`` / ``allow_file``)
modes, plus the regression that ``TmuxClient`` delegation left tmux
behavior unchanged.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.utils.path_validation import (
    BLOCKED_SYSTEM_DIRECTORIES,
    resolve_and_validate_path,
)

# ── strict mode (tmux semantics: must exist, directory only) ─────────


class TestStrictMode:
    def test_valid_directory(self, tmp_path):
        result = resolve_and_validate_path(str(tmp_path))
        assert result == os.path.realpath(str(tmp_path))

    def test_symlink_canonicalized(self, tmp_path):
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real_dir)
        result = resolve_and_validate_path(str(link))
        assert result == os.path.realpath(str(real_dir))

    def test_blocked_root(self):
        with pytest.raises(ValueError, match="blocked system path"):
            resolve_and_validate_path("/")

    def test_blocked_etc(self):
        with pytest.raises(ValueError, match="blocked system path"):
            resolve_and_validate_path("/etc")

    def test_dotdot_resolving_to_blocked_rejected(self):
        with pytest.raises(ValueError, match="blocked system path"):
            resolve_and_validate_path("/usr/bin/../../etc")

    def test_nonexistent_rejected(self):
        with pytest.raises(ValueError, match="does not exist"):
            resolve_and_validate_path("/nonexistent/dir/xyz")

    def test_file_target_rejected_by_default(self, tmp_path):
        f = tmp_path / "out.tar.gz"
        f.write_text("x")
        with pytest.raises(ValueError, match="does not exist"):
            resolve_and_validate_path(str(f))

    def test_expands_home(self):
        result = resolve_and_validate_path("~")
        assert result == os.path.realpath(os.path.expanduser("~"))

    def test_description_in_error_message(self):
        with pytest.raises(ValueError, match="Export destination does not exist"):
            resolve_and_validate_path("/nonexistent/dir/xyz", description="Export destination")


# ── allow_create (export destination that doesn't exist yet) ─────────


class TestAllowCreate:
    def test_nonexistent_target_under_valid_ancestor(self, tmp_path):
        dest = tmp_path / "exports" / "okf-bundle"
        result = resolve_and_validate_path(str(dest), allow_create=True)
        assert result == os.path.realpath(str(dest))
        # Validation does not create the directory — the caller does.
        assert not dest.exists()

    def test_existing_directory_still_accepted(self, tmp_path):
        result = resolve_and_validate_path(str(tmp_path), allow_create=True)
        assert result == os.path.realpath(str(tmp_path))

    def test_nearest_existing_ancestor_blocked(self):
        # /etc exists and is blocked; /etc/<new> must be rejected via the
        # nearest-existing-ancestor rule.
        with pytest.raises(ValueError, match="blocked system path"):
            resolve_and_validate_path("/etc/new-export-dir/deeper", allow_create=True)

    def test_blocked_target_itself_still_rejected(self):
        with pytest.raises(ValueError, match="blocked system path"):
            resolve_and_validate_path("/etc", allow_create=True)


# ── allow_file (-o out.tar.gz target) ────────────────────────────────


class TestAllowFile:
    def test_existing_file_accepted(self, tmp_path):
        f = tmp_path / "out.tar.gz"
        f.write_text("x")
        result = resolve_and_validate_path(str(f), allow_file=True)
        assert result == os.path.realpath(str(f))

    def test_nonexistent_file_with_allow_create(self, tmp_path):
        f = tmp_path / "out.tar.gz"
        result = resolve_and_validate_path(str(f), allow_create=True, allow_file=True)
        assert result == os.path.realpath(str(f))

    def test_nonexistent_file_without_allow_create_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="does not exist"):
            resolve_and_validate_path(str(tmp_path / "out.tar.gz"), allow_file=True)


# ── design test 14: tmux delegation regression ───────────────────────


@pytest.fixture
def tmux():
    """TmuxClient with a mocked libtmux.Server (no real tmux required)."""
    with patch("cli_agent_orchestrator.clients.tmux.libtmux") as mock_libtmux:
        mock_libtmux.Server.return_value = MagicMock()
        from cli_agent_orchestrator.clients.tmux import TmuxClient

        yield TmuxClient()


class TestTmuxDelegationRegression:
    """Tmux working-directory behavior must be byte-identical post-extraction."""

    def test_valid_directory_unchanged(self, tmux, tmp_path):
        assert tmux._resolve_and_validate_working_directory(str(tmp_path)) == os.path.realpath(
            str(tmp_path)
        )

    def test_defaults_to_cwd(self, tmux, tmp_path):
        with patch("os.getcwd", return_value=str(tmp_path)):
            assert tmux._resolve_and_validate_working_directory(None) == os.path.realpath(
                str(tmp_path)
            )

    def test_blocked_dir_error_message_unchanged(self, tmux):
        with pytest.raises(
            ValueError,
            match=r"Working directory not allowed: /etc \(resolves to blocked system path",
        ):
            tmux._resolve_and_validate_working_directory("/etc")

    def test_nonexistent_error_message_unchanged(self, tmux):
        with pytest.raises(ValueError, match="Working directory does not exist"):
            tmux._resolve_and_validate_working_directory("/nonexistent/dir/xyz")

    def test_file_target_still_rejected_for_tmux(self, tmux, tmp_path):
        f = tmp_path / "out.tar.gz"
        f.write_text("x")
        with pytest.raises(ValueError, match="does not exist"):
            tmux._resolve_and_validate_working_directory(str(f))

    def test_not_yet_existing_dir_still_rejected_for_tmux(self, tmux, tmp_path):
        with pytest.raises(ValueError, match="does not exist"):
            tmux._resolve_and_validate_working_directory(str(tmp_path / "new"))

    def test_blocked_frozenset_alias_preserved(self, tmux):
        assert tmux._BLOCKED_DIRECTORIES is BLOCKED_SYSTEM_DIRECTORIES
        assert "/etc" in tmux._BLOCKED_DIRECTORIES


# ── component-under-base confinement helpers ─────────────────────────


from cli_agent_orchestrator.utils.path_validation import (  # noqa: E402
    safe_join_under_base,
    validate_path_component,
)


class TestValidatePathComponent:
    @pytest.mark.parametrize(
        "value",
        ["global", "project", "shared-key", "a.b", "abc123", "under_score", "KEY.md", "a"],
    )
    def test_valid_components_pass_through_unchanged(self, value):
        assert validate_path_component(value) == value

    @pytest.mark.parametrize("value", ["", ".", ".."])
    def test_empty_or_dot_rejected(self, value):
        with pytest.raises(ValueError):
            validate_path_component(value)

    @pytest.mark.parametrize(
        "value",
        ["a/b", "a\\b", "..%2f", "foo/../bar", "/etc", "a b", "a:b", "a*b", "café"],
    )
    def test_separator_or_disallowed_chars_rejected(self, value):
        with pytest.raises(ValueError):
            validate_path_component(value)

    def test_nul_byte_rejected(self):
        with pytest.raises(ValueError, match="NUL byte"):
            validate_path_component("a\x00b")

    @pytest.mark.parametrize("value", ["topic\n", "topic\r\n", "\ntopic", "a\nb"])
    def test_trailing_or_embedded_newline_rejected(self, value):
        # In Python, ``$`` also matches just before a trailing newline, so the
        # end anchor must be ``\Z`` — otherwise ``"topic\n"`` would slip past
        # the allowlist and become a path segment carrying a newline.
        with pytest.raises(ValueError):
            validate_path_component(value)

    def test_description_in_error_message(self):
        with pytest.raises(ValueError, match="scope_id must"):
            validate_path_component("../evil", description="scope_id")


class TestSafeJoinUnderBase:
    def test_valid_join_stays_under_base(self, tmp_path):
        result = safe_join_under_base(str(tmp_path), "proj", "wiki", "project", "topic.md")
        expected = os.path.join(
            os.path.realpath(str(tmp_path)), "proj", "wiki", "project", "topic.md"
        )
        assert result == expected
        assert result.startswith(os.path.realpath(str(tmp_path)) + os.sep)

    def test_no_components_returns_base(self, tmp_path):
        assert safe_join_under_base(str(tmp_path)) == os.path.realpath(str(tmp_path))

    def test_traversal_component_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            safe_join_under_base(str(tmp_path), "..", "etc")

    def test_separator_in_component_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            safe_join_under_base(str(tmp_path), "../../etc/passwd")

    def test_absolute_ish_component_rejected(self, tmp_path):
        # A leading-slash segment would reset os.path.join to an absolute
        # path outside the base; the component validator rejects it first.
        with pytest.raises(ValueError):
            safe_join_under_base(str(tmp_path), "/etc")

    def test_symlink_escape_is_contained(self, tmp_path):
        # A symlinked base component that resolves outside the base must be
        # caught by the realpath containment guard, not silently followed.
        outside = tmp_path.parent / "outside_base"
        outside.mkdir()
        base = tmp_path / "base"
        base.mkdir()
        # 'link' is a valid single segment but points outside the base.
        (base / "link").symlink_to(outside)
        with pytest.raises(ValueError, match="Path traversal detected"):
            safe_join_under_base(str(base), "link", "topic.md")
