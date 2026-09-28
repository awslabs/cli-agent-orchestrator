"""Resolver tests — local copy isolation, git clone, ``--subdir``, error reporting."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from cli_agent_orchestrator.agent_plugins import resolver
from cli_agent_orchestrator.agent_plugins.models import PluginSource
from cli_agent_orchestrator.agent_plugins.resolver import ResolverError, resolve

from .conftest import build_plugin

pytestmark = pytest.mark.usefixtures("_isolate_settings")

GIT = shutil.which("git")
requires_git = pytest.mark.skipif(GIT is None, reason="git is not installed")


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(cwd),
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    )


@pytest.fixture
def git_repo(tmp_path) -> Path:
    """A local repository with the plugin at ``agent-plugin/demo/``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    build_plugin(repo / "agent-plugin" / "demo", "demo", skills=["alpha"])
    (repo / "README.md").write_text("top level", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "initial", cwd=repo)
    return repo


class TestPathSource:
    def test_directory_contents_are_copied_into_staging(self, tmp_path):
        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])
        staging = tmp_path / "staging"

        resolved = resolve(PluginSource(kind="path", location=str(source)), staging)

        assert resolved.root != source
        assert (resolved.root / "plugin.json").is_file()
        assert (resolved.root / "skills" / "alpha" / "SKILL.md").is_file()
        assert resolved.resolved_ref is None

    def test_staged_copy_is_isolated_from_later_edits(self, tmp_path):
        """Copying — not referencing in place — is what makes validate-then-publish mean something."""
        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])
        resolved = resolve(PluginSource(kind="path", location=str(source)), tmp_path / "staging")

        (source / "skills" / "alpha" / "SKILL.md").write_text("tampered", encoding="utf-8")

        staged_body = (resolved.root / "skills" / "alpha" / "SKILL.md").read_text(encoding="utf-8")
        assert "tampered" not in staged_body

    def test_symlinks_are_preserved_not_followed(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret").write_text("secret", encoding="utf-8")
        source = build_plugin(tmp_path / "src", "demo")
        (source / "escape").symlink_to(outside, target_is_directory=True)

        resolved = resolve(PluginSource(kind="path", location=str(source)), tmp_path / "staging")

        # If the copy followed the link, the escape would become real content
        # inside the staged root and containment would then accept it.
        assert (resolved.root / "escape").is_symlink()

    def test_tilde_is_expanded(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        build_plugin(home / "myplugin", "demo")
        monkeypatch.setenv("HOME", str(home))

        resolved = resolve(PluginSource(kind="path", location="~/myplugin"), tmp_path / "staging")
        assert (resolved.root / "plugin.json").is_file()


class TestUnreachableSources:
    def test_missing_local_path_reports_the_cause(self, tmp_path):
        with pytest.raises(ResolverError, match="does not exist"):
            resolve(
                PluginSource(kind="path", location=str(tmp_path / "nope")),
                tmp_path / "staging",
            )

    def test_a_file_is_not_a_plugin_source(self, tmp_path):
        target = tmp_path / "file.txt"
        target.write_text("x", encoding="utf-8")
        with pytest.raises(ResolverError, match="not a directory"):
            resolve(PluginSource(kind="path", location=str(target)), tmp_path / "staging")

    def test_empty_location_is_rejected(self, tmp_path):
        with pytest.raises(ResolverError, match="empty"):
            resolve(PluginSource(kind="path", location="   "), tmp_path / "staging")

    @requires_git
    def test_failed_git_clone_reports_the_cause(self, tmp_path):
        with pytest.raises(ResolverError, match="git clone"):
            resolve(
                PluginSource(kind="git", location=str(tmp_path / "not-a-repo")),
                tmp_path / "staging",
            )


class TestSubdir:
    def test_subdir_addresses_the_candidate_root(self, tmp_path):
        monorepo = tmp_path / "monorepo"
        build_plugin(monorepo / "agent-plugin" / "demo", "demo", skills=["alpha"])
        (monorepo / "unrelated.txt").write_text("x", encoding="utf-8")

        resolved = resolve(
            PluginSource(kind="path", location=str(monorepo), subdir="agent-plugin/demo"),
            tmp_path / "staging",
        )

        assert (resolved.root / "plugin.json").is_file()
        assert resolved.root.name == "demo"

    def test_subdir_escaping_the_source_tree_is_rejected(self, tmp_path):
        source = build_plugin(tmp_path / "src", "demo")
        with pytest.raises(ResolverError, match="escapes"):
            resolve(
                PluginSource(kind="path", location=str(source), subdir="../../etc"),
                tmp_path / "staging",
            )

    def test_missing_subdir_is_reported(self, tmp_path):
        source = build_plugin(tmp_path / "src", "demo")
        with pytest.raises(ResolverError, match="does not exist"):
            resolve(
                PluginSource(kind="path", location=str(source), subdir="nowhere"),
                tmp_path / "staging",
            )


@requires_git
class TestGitSource:
    def test_clone_records_the_resolved_commit(self, tmp_path, git_repo):
        resolved = resolve(
            PluginSource(kind="git", location=str(git_repo), subdir="agent-plugin/demo"),
            tmp_path / "staging",
        )

        assert (resolved.root / "plugin.json").is_file()
        assert resolved.resolved_ref is not None
        assert len(resolved.resolved_ref) == 40  # a full commit SHA

    def test_clone_with_an_explicit_ref(self, tmp_path, git_repo):
        _git("branch", "release", cwd=git_repo)

        resolved = resolve(
            PluginSource(
                kind="git",
                location=str(git_repo),
                ref="release",
                subdir="agent-plugin/demo",
            ),
            tmp_path / "staging",
        )
        assert (resolved.root / "plugin.json").is_file()

    def test_unknown_ref_is_reported(self, tmp_path, git_repo):
        with pytest.raises(ResolverError, match="git clone"):
            resolve(
                PluginSource(kind="git", location=str(git_repo), ref="no-such-branch"),
                tmp_path / "staging",
            )

    def test_submodules_are_not_initialized(self, tmp_path, git_repo):
        """A stated non-behavior, not an artifact of ``--depth 1``'s default.

        Submodule content must never enter staging, because it would then have
        to be reasoned about under containment.
        """
        inner = tmp_path / "inner"
        inner.mkdir()
        _git("init", "-q", "-b", "main", cwd=inner)
        (inner / "payload.txt").write_text("submodule payload", encoding="utf-8")
        _git("add", "-A", cwd=inner)
        _git("commit", "-q", "-m", "inner", cwd=inner)

        _git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(inner),
            "vendor/inner",
            cwd=git_repo,
        )
        _git("commit", "-q", "-m", "add submodule", cwd=git_repo)

        resolved = resolve(PluginSource(kind="git", location=str(git_repo)), tmp_path / "staging")

        assert not (resolved.root / "vendor" / "inner" / "payload.txt").exists()

    def test_clone_passes_no_recurse_submodules_explicitly(self, tmp_path, monkeypatch):
        """The flag is passed, so a future git default change cannot pull them in."""
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            raise subprocess.CalledProcessError(1, cmd, stderr="stop here")

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(ResolverError):
            resolve(
                PluginSource(kind="git", location="https://example.invalid/x.git"),
                tmp_path / "staging",
            )

        assert "--no-recurse-submodules" in captured["cmd"]
        assert "--depth" in captured["cmd"]

    def test_git_prompts_are_disabled(self, tmp_path, monkeypatch):
        """A private repo must fail with a message, never hang on a prompt."""
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["env"] = kwargs.get("env", {})
            captured["timeout"] = kwargs.get("timeout")
            raise subprocess.CalledProcessError(1, cmd, stderr="denied")

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(ResolverError):
            resolve(
                PluginSource(kind="git", location="https://example.invalid/private.git"),
                tmp_path / "staging",
            )

        assert captured["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert captured["timeout"] and captured["timeout"] > 0


class TestGitInvocationHardening:
    """Every git subprocess runs non-interactive, helper-less and submodule-less.

    **Validates: Requirement 8.2 (unreachable source fails rather than hangs)**

    Asserted against the argv and environment actually handed to
    ``subprocess.run``, not against the module's source text. That matters here
    more than usual: these flags exist to stop a *hang* and a *credential leak*,
    neither of which any functional test can observe, so if they are not asserted
    structurally they are not asserted at all.
    """

    @pytest.fixture
    def captured(self, monkeypatch, tmp_path):
        """Capture argv and env for every git call, and fail the clone."""
        calls = []

        class _Result:
            stdout = ""
            stderr = ""

        def fake_run(command, **kwargs):
            calls.append({"argv": list(command), "env": dict(kwargs.get("env") or {})})
            return _Result()

        monkeypatch.setattr(resolver.subprocess, "run", fake_run)
        resolver.resolve(
            PluginSource(kind="git", location="https://example.test/repo.git"), tmp_path / "dest"
        )
        return calls

    def test_submodule_recursion_is_disabled_for_every_subcommand(self, captured):
        """`-c submodule.recurse=false` is belt to `--no-recurse-submodules`'s braces.

        An operator with `submodule.recurse=true` set globally would otherwise
        have submodules pulled in by subcommands that accept no such flag.
        """
        for call in captured:
            argv = call["argv"]
            assert argv[:3] == ["git", "-c", "submodule.recurse=false"], argv

    def test_the_credential_helper_chain_is_emptied(self, captured):
        """`_git_env` stops a helper from blocking; this stops one from answering.

        A configured helper that *did* answer would hand credentials for one host
        to whatever URL the plugin source happened to name.
        """
        for call in captured:
            assert "credential.helper=" in call["argv"], call["argv"]

    @pytest.mark.parametrize(
        "key,value",
        [
            ("GIT_TERMINAL_PROMPT", "0"),
            ("GIT_ASKPASS", ""),
            ("SSH_ASKPASS", ""),
            ("GCM_INTERACTIVE", "never"),
        ],
    )
    def test_every_prompting_mechanism_is_closed(self, captured, key, value):
        """Four separate mechanisms can prompt, and they ignore each other.

        `GIT_ASKPASS` bypasses `GIT_TERMINAL_PROMPT` entirely, and Git Credential
        Manager is its own process honouring neither — so closing one proves
        nothing about the others.
        """
        for call in captured:
            assert call["env"].get(key) == value, f"{key} not set to {value!r}"

    def test_askpass_is_emptied_rather_than_unset(self, captured, monkeypatch):
        """Unsetting would let git fall back to a system default helper."""
        for call in captured:
            assert "GIT_ASKPASS" in call["env"]
            assert "SSH_ASKPASS" in call["env"]

    def test_ssh_refuses_interactive_host_key_prompts(self, captured):
        assert all("BatchMode=yes" in call["env"].get("GIT_SSH_COMMAND", "") for call in captured)

    def test_tags_are_not_fetched(self, captured):
        """Nothing resolves a version from the repository, so tags are pure cost."""
        clone = next(call for call in captured if "clone" in call["argv"])
        assert "--no-tags" in clone["argv"]

    def test_the_location_is_still_separated_by_a_double_dash(self, captured):
        """Pre-existing guard: a location beginning with `-` is never an option."""
        clone = next(call for call in captured if "clone" in call["argv"])
        assert "--" in clone["argv"]
        assert clone["argv"].index("--") < clone["argv"].index("https://example.test/repo.git")


@requires_git
class TestVcsMetadataIsStripped:
    """``.git`` is not package bytes, and `--depth` does not apply to local clones."""

    def test_the_staged_tree_has_no_git_directory(self, tmp_path, git_repo):
        resolved = resolver.resolve(
            PluginSource(kind="git", location=str(git_repo)), tmp_path / "dest"
        )

        assert not (resolved.staging / "source" / ".git").exists()

    def test_the_commit_is_still_recorded(self, tmp_path, git_repo):
        """The ref must be read before the metadata it comes from is deleted.

        This is the ordering bug the strip could easily introduce: delete `.git`
        first and `resolved_ref` silently becomes `None`, losing the provenance
        an install record exists to hold.
        """
        resolved = resolver.resolve(
            PluginSource(kind="git", location=str(git_repo)), tmp_path / "dest"
        )

        assert resolved.resolved_ref
        assert len(resolved.resolved_ref) == 40

    def test_the_package_content_survives_the_strip(self, tmp_path, git_repo):
        """Removing `.git` must not remove anything else."""
        resolved = resolver.resolve(
            PluginSource(kind="git", location=str(git_repo), subdir="agent-plugin/demo"),
            tmp_path / "dest",
        )

        assert (resolved.root / "plugin.json").is_file()


@requires_git
class TestFullCommitPin:
    """`clone --branch` cannot take a commit, so a pinned commit needs a fetch."""

    def test_a_full_commit_id_resolves(self, tmp_path, git_repo):
        """The case an install record replays: `resolved_ref` fed back as `ref`."""
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=git_repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        resolved = resolver.resolve(
            PluginSource(kind="git", location=str(git_repo), ref=head), tmp_path / "dest"
        )

        assert resolved.resolved_ref == head
        assert (resolved.staging / "source" / "agent-plugin" / "demo" / "plugin.json").is_file()

    def test_an_abbreviated_hash_is_rejected_rather_than_resolved(self, tmp_path, git_repo):
        """Deliberately unsupported: an abbreviation is ambiguous by construction.

        "Install whatever object matches this prefix today" is not a reproducible
        source, so the abbreviated form is left to fail as an unknown branch name
        rather than being silently expanded.
        """
        short = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=git_repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        with pytest.raises(ResolverError):
            resolver.resolve(
                PluginSource(kind="git", location=str(git_repo), ref=short), tmp_path / "dest"
            )

    def test_an_unknown_branch_still_reports_a_clone_failure(self, tmp_path, git_repo):
        """The fallback must not swallow the ordinary bad-ref error."""
        with pytest.raises(ResolverError, match="clone"):
            resolver.resolve(
                PluginSource(kind="git", location=str(git_repo), ref="no-such-branch"),
                tmp_path / "dest",
            )

    def test_the_commit_pin_path_also_strips_metadata(self, tmp_path, git_repo):
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=git_repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        resolved = resolver.resolve(
            PluginSource(kind="git", location=str(git_repo), ref=head), tmp_path / "dest"
        )

        assert not (resolved.staging / "source" / ".git").exists()
