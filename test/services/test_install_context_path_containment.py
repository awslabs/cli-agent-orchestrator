"""Security regression: the agent context-copy path cannot escape its directory.

Covers GHSA-6m35-gcf5-xm75. `_write_context_file` builds the context-copy
filename from the profile's RESOLVED frontmatter `name:`. That value is not
covered by `_PROFILE_NAME_RE` (which guards the install *source handle*) and is
attacker-controlled when a profile is installed from a URL, so an unguarded name
could steer the write outside `AGENT_CONTEXT_DIR`.

Two layers of coverage:

- `TestContextPathContainment` exercises `_write_context_file` directly against
  each escape class: relative traversal, absolute paths, backslash separators,
  NUL, and three symlink shapes (including one resolving *inside* the base, which
  isolates the `O_NOFOLLOW` guard from the containment check).
- `TestInstallAgentRefusesHostileResolvedName` drives the public `install_agent`
  entry point, pinning the actual attack path end to end.
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
        # the context dir stayed empty (see the install_agent tests below for the
        # broader "nothing written anywhere" assertion)
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


@pytest.fixture
def install_env(tmp_path, monkeypatch):
    """Redirect every directory ``install_agent`` writes to under tmp_path.

    Mirrors the fixture in test/cli/commands/test_install_opencode.py, but for
    the service entry point rather than the CLI, and it also redirects the kiro
    and copilot agent dirs so a hostile name cannot escape into a real one.
    """
    store = tmp_path / "agent-store"
    context = tmp_path / "agent-context"
    store.mkdir()
    context.mkdir()
    provider_dirs = {}
    for key, attr in (
        ("opencode", "OPENCODE_AGENTS_DIR"),
        ("kiro", "KIRO_AGENTS_DIR"),
        ("copilot", "COPILOT_AGENTS_DIR"),
    ):
        d = tmp_path / key
        provider_dirs[key] = d
        monkeypatch.setattr(f"cli_agent_orchestrator.services.install_service.{attr}", d)

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.profile_store.LOCAL_AGENT_STORE_DIR", store
    )
    monkeypatch.setattr("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR", store)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.install_service.AGENT_CONTEXT_DIR", context
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_agent_dirs", lambda: {}
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.install_service.ensure_skills_symlink", lambda: None
    )
    return {"store": store, "context": context, "providers": provider_dirs, "root": tmp_path}


class TestInstallAgentRefusesHostileResolvedName:
    """End-to-end: the ACTUAL attack path, through the public entry point.

    The tests above exercise `_write_context_file` directly. These drive
    `install_agent`, which is what `cao install` and the
    `POST /agents/profiles/install` endpoint call, so they pin the property that
    matters: a profile whose *source handle* is perfectly valid but whose
    frontmatter `name:` is hostile fails the install and writes nothing. This is
    the layer that proves the guard is actually reachable from user input.
    """

    def _write_profile(self, install_env, resolved_name: str) -> None:
        # The handle "trusted-handle" passes _PROFILE_NAME_RE; the frontmatter
        # name is what an attacker controls when the profile is fetched by URL.
        (install_env["store"] / "trusted-handle.md").write_text(
            f"---\nname: {resolved_name}\ndescription: Hostile\n---\nBody\n",
            encoding="utf-8",
        )

    @pytest.mark.parametrize(
        "resolved_name",
        [
            "../../evil",
            "a/../../evil",
            "/etc/evil",
            "..\\..\\evil",
            "sub/dir",
            "..",
        ],
    )
    @pytest.mark.parametrize("provider", ["opencode_cli", "kiro_cli", "copilot_cli"])
    def test_install_fails_and_writes_nothing(self, install_env, resolved_name, provider):
        self._write_profile(install_env, resolved_name)

        result = install_service.install_agent("trusted-handle", provider=provider)

        assert result.success is False
        assert "profile name" in result.message
        # No context copy, and no provider agent file anywhere.
        assert list(install_env["context"].iterdir()) == []
        for d in install_env["providers"].values():
            assert not d.exists() or list(d.iterdir()) == []
        # And nothing escaped into the tmp root beside the dirs we created.
        assert sorted(p.name for p in install_env["root"].iterdir()) == [
            "agent-context",
            "agent-store",
        ]

    def test_legitimate_name_still_installs(self, install_env):
        self._write_profile(install_env, "developer")

        result = install_service.install_agent("trusted-handle", provider="opencode_cli")

        assert result.success is True, result.message
        assert (install_env["context"] / "developer.md").is_file()
        assert (install_env["providers"]["opencode"] / "developer.md").is_file()
