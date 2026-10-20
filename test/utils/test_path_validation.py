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
    flatten_path_separators,
    safe_join_under_base,
    validate_path_component,
)


class TestFlattenPathSeparators:
    """The lossy sibling of ``validate_path_component``, used by the provider
    agent-file sinks where a separator is folded rather than rejected.

    Security regression for GHSA-6m35-gcf5-xm75: only ``/`` was folded, so a
    resolved profile ``name:`` of ``..\\..\\evil`` kept its backslashes and
    traversed out of the provider agent directory on Windows.
    """

    @pytest.mark.parametrize(
        "value",
        [
            "..\\..\\evil",
            "a\\b",
            "..\\../mixed",
            "C:\\Windows\\evil",
            "../../evil",
            "sub/dir",
        ],
    )
    def test_no_separator_survives(self, value):
        produced = flatten_path_separators(value)
        assert "/" not in produced
        assert "\\" not in produced

    @pytest.mark.parametrize("value", ["developer", "my__agent", "a.b-c_d", ""])
    def test_separator_free_input_is_unchanged(self, value):
        assert flatten_path_separators(value) == value

    def test_idempotent(self):
        once = flatten_path_separators("a/b\\c")
        assert flatten_path_separators(once) == once


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


@pytest.mark.skipif(os.name != "posix", reason="POSIX system paths")
class TestBlockedSubtrees:
    """The blocklist is a set of subtrees for system locations, not only exact names."""

    def test_library_roots_are_blocked_in_their_canonical_spelling(self):
        """On usr-merged Linux ``/lib`` resolves to ``/usr/lib`` before the check runs,
        so ``/lib`` on its own never fired; the canonical roots must be listed too."""
        from cli_agent_orchestrator.utils.path_validation import _blocked_reason

        assert _blocked_reason("/usr/lib/systemd/system/evil.service") != ""
        assert _blocked_reason("/usr/lib64/cao-evil") != ""
        assert _blocked_reason("/lib/cao-evil") != ""
        assert _blocked_reason("/lib64/cao-evil") != ""
        # And through the real validator, whatever /lib resolves to on this host.
        if os.path.isdir("/lib"):
            with pytest.raises(ValueError, match="blocked system"):
                resolve_and_validate_path("/lib/cao-evil", allow_create=True)
        # /usr/libexec and /usr/local/lib are not system library roots here.
        assert _blocked_reason("/usr/local/lib/x") == ""
        assert _blocked_reason("/usr/libexec/x") == ""

    def test_crontab_spool_is_blocked(self):
        from cli_agent_orchestrator.utils.path_validation import _blocked_reason

        assert _blocked_reason("/var/spool/cron/crontabs/root") != ""
        assert _blocked_reason("/var/spool/mail/x") == ""  # only the cron spool

    def test_root_home_is_a_blocked_subtree(self):
        """``/root/.ssh/authorized_keys`` and ``/root/.bashrc`` are persistence for
        whoever reaches the API of a cao-server running as root."""
        from cli_agent_orchestrator.utils.path_validation import _blocked_reason

        assert _blocked_reason("/root") != ""
        assert _blocked_reason("/root/.ssh/authorized_keys") != ""
        assert _blocked_reason("/root/.bashrc") != ""
        assert _blocked_reason("/root/projects/app") != ""
        assert _blocked_reason("/rootfs/x") == ""  # lookalike prefix

    @pytest.mark.parametrize("target", ["/etc/hosts", "/dev/null"])
    def test_files_inside_blocked_subtrees_are_refused_even_with_allow_file(self, target):
        assert os.path.exists(target)
        with pytest.raises(ValueError, match="beneath blocked system path"):
            resolve_and_validate_path(target, allow_file=True)

    def test_existing_directory_inside_a_blocked_subtree_is_refused(self):
        candidates = [
            d for d in ("/etc/ssl", "/etc/ssh", "/dev/fd", "/usr/bin") if os.path.isdir(d)
        ]
        assert candidates, "no blocked-subtree child directory exists on this host"
        with pytest.raises(ValueError, match="blocked system"):
            resolve_and_validate_path(candidates[0])

    def test_new_name_deep_inside_a_blocked_subtree_is_refused(self):
        # Caught at the resolved-path step: the subtree rule is a string-prefix
        # test, so it does not need the target to exist.
        with pytest.raises(ValueError, match="beneath blocked system path"):
            resolve_and_validate_path("/etc/ssl/new/deeper", allow_create=True)

    def test_children_of_exact_only_roots_stay_allowed(self, tmp_path):
        # tmp_path lives under /tmp (Linux) or /private/var/folders (macOS);
        # both roots are exact-only, so projects there remain valid.
        assert resolve_and_validate_path(str(tmp_path)) == os.path.realpath(str(tmp_path))

    def test_dev_shm_is_carved_out_of_the_dev_subtree(self):
        from cli_agent_orchestrator.utils.path_validation import _blocked_reason

        assert _blocked_reason("/dev/shm") == ""
        assert _blocked_reason("/dev/shm/cao-export") == ""
        assert _blocked_reason("/dev/shmem") != ""  # lookalike is still under /dev
        assert _blocked_reason("/dev/null") != ""

    def test_lookalike_prefix_is_not_blocked(self, tmp_path):
        # "/etcetera" shares a string prefix with "/etc" but is not inside it;
        # emulate with a directory whose name starts like a blocked root.
        look = tmp_path / "etc_like"
        look.mkdir()
        assert resolve_and_validate_path(str(look)) == os.path.realpath(str(look))
