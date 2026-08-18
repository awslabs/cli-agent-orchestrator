"""Security regression: the agent context-copy path cannot escape its directory.

`_write_context_file` builds the context-copy filename from the profile's
RESOLVED frontmatter `name:`. That value is not covered by `_PROFILE_NAME_RE`
(which guards the install *source handle*) and is attacker-controlled when a
profile is installed from a URL, so an unguarded name could steer the write
outside `AGENT_CONTEXT_DIR`. These tests pin the fix against the escape classes:
relative traversal, absolute paths, backslash separators, and symlink escapes.
"""

import os
import stat

import pytest

from cli_agent_orchestrator.services import install_service


@pytest.fixture
def context_dir(tmp_path, monkeypatch):
    d = tmp_path / "agent-context"
    d.mkdir()
    monkeypatch.setattr("cli_agent_orchestrator.services.install_service.AGENT_CONTEXT_DIR", d)
    return d


class TestContextPathContainment:
    @pytest.mark.parametrize(
        "hostile_name",
        [
            "../../evil",
            "a/../../evil",
            "deep/../../../evil",
            "/etc/evil",  # absolute (POSIX)
            "C:\\Windows\\evil",  # absolute (Windows)
            "..\\..\\evil",  # backslash traversal
            "a\\b",  # backslash separator
            "sub/dir",  # plain separator
            "..",
            ".",
            "",
        ],
    )
    def test_hostile_resolved_name_is_refused(self, context_dir, hostile_name):
        with pytest.raises(ValueError):
            install_service._write_context_file(hostile_name, "---\nname: x\n---\nbody\n")
        # nothing was written anywhere under the parent of the context dir
        assert list(context_dir.iterdir()) == []

    def test_absolute_path_into_home_is_refused(self, context_dir, tmp_path):
        decoy = tmp_path / "home" / ".claude"
        decoy.mkdir(parents=True)
        (decoy / "CLAUDE.md").write_text("ORIGINAL trusted instructions\n")
        with pytest.raises(ValueError):
            install_service._write_context_file(
                str(decoy / "CLAUDE"), "---\nname: x\n---\nINJECTED\n"
            )
        assert (decoy / "CLAUDE.md").read_text() == "ORIGINAL trusted instructions\n"

    def test_symlink_at_target_pointing_outside_is_not_followed(self, context_dir, tmp_path):
        # A symlink planted at the target, pointing outside, must not be written
        # through. The final component is left unresolved by the path guard, so
        # this is caught by O_NOFOLLOW at the open, not by containment.
        outside = tmp_path / "outside"
        outside.mkdir()
        (context_dir / "evil.md").symlink_to(outside / "pwned.md")
        with pytest.raises(ValueError):
            install_service._write_context_file("evil", "---\nname: x\n---\nINJECTED\n")
        assert not (outside / "pwned.md").exists()

    def test_symlinked_target_regular_file_outside_is_refused(self, context_dir, tmp_path):
        outside_file = tmp_path / "outside.md"
        outside_file.write_text("original\n")
        (context_dir / "evil.md").symlink_to(outside_file)
        with pytest.raises(ValueError):
            install_service._write_context_file("evil", "---\nname: x\n---\nINJECTED\n")
        assert outside_file.read_text() == "original\n"

    def test_symlink_at_target_pointing_INSIDE_base_is_still_refused(self, context_dir):
        # Isolates the O_NOFOLLOW guard: this symlink resolves to a path INSIDE
        # the context dir, so the realpath-containment check would ALLOW it — only
        # the no-follow open refuses it. Guards against a future refactor silently
        # dropping O_NOFOLLOW (the containment test would still pass without it).
        (context_dir / "real.md").write_text("real target\n")
        (context_dir / "evil.md").symlink_to(context_dir / "real.md")
        with pytest.raises(ValueError):
            install_service._write_context_file("evil", "---\nname: x\n---\nINJECTED\n")
        # the symlink's in-base target was not written through
        assert (context_dir / "real.md").read_text() == "real target\n"

    def test_nul_byte_in_name_is_refused(self, context_dir):
        with pytest.raises(ValueError):
            install_service._write_context_file("a\x00b", "---\nname: x\n---\nbody\n")
        assert list(context_dir.iterdir()) == []

    def test_normal_name_writes_inside(self, context_dir):
        written = install_service._write_context_file(
            "developer", "---\nname: developer\n---\nBe helpful.\n"
        )
        assert written == context_dir / "developer.md"
        assert written.is_file()
        assert not written.is_symlink()
        assert os.path.realpath(written).startswith(os.path.realpath(context_dir) + os.sep)

    def test_reinstall_over_own_regular_copy_is_allowed(self, context_dir):
        install_service._write_context_file("developer", "---\nname: developer\n---\nv1\n")
        # a normal reinstall overwrites the profile's own prior regular-file copy
        written = install_service._write_context_file(
            "developer", "---\nname: developer\n---\nv2\n"
        )
        assert "v2" in written.read_text()
        assert stat.S_ISREG(os.lstat(written).st_mode)


class TestProviderFilenameSeparatorFlattening:
    """The provider agent-file sinks flatten BOTH path separators.

    These sinks derive a flat filename from the attacker-controlled resolved
    profile name. `/` was already flattened; a bare `\\` survived, which is a
    path separator on Windows and would traverse out of the provider dir there.
    """

    @pytest.mark.parametrize(
        "hostile_name",
        ["..\\..\\evil", "a\\b", "..\\../mixed", "C:\\Windows\\evil"],
    )
    def test_no_separator_survives_the_flatten(self, hostile_name):
        from cli_agent_orchestrator.utils.opencode_config import to_opencode_agent_id

        # install_service's kiro/copilot safe_filename + opencode's agent id must
        # leave no OS path separator that could traverse on any platform.
        for produced in (
            hostile_name.replace("/", "__").replace("\\", "__"),  # the safe_filename form
            to_opencode_agent_id(hostile_name),
        ):
            assert "/" not in produced
            assert "\\" not in produced

    def test_skill_injection_refresh_flattens_backslash(self, tmp_path, monkeypatch):
        # refresh_installed_agent_for_profile builds a Copilot filename from the
        # resolved name; confirm the path it targets has no surviving separator.
        from types import SimpleNamespace

        from cli_agent_orchestrator.utils import skill_injection

        copilot_dir = tmp_path / "copilot"
        copilot_dir.mkdir()
        monkeypatch.setattr(skill_injection, "COPILOT_AGENTS_DIR", copilot_dir)
        monkeypatch.setattr(
            skill_injection,
            "load_agent_profile",
            lambda name: SimpleNamespace(name="..\\..\\evil"),
        )
        captured = {}

        def _fake_refresh(md_path, profile):
            captured["path"] = md_path
            return False  # simulate "no existing file", as the real code would

        monkeypatch.setattr(skill_injection, "refresh_agent_md_prompt", _fake_refresh)

        skill_injection.refresh_installed_agent_for_profile("some-source")

        target = captured["path"]
        # the target stays a direct child of COPILOT_AGENTS_DIR (no traversal)
        assert target.parent == copilot_dir
        assert "\\" not in target.name.replace(".agent.md", "")
        assert "/" not in target.name.replace(".agent.md", "")
