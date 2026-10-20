"""Plugin git sources are allowlisted by scheme and host before git runs.

``git clone`` reads the text before ``://`` as a transport. Handed an operator
or API-supplied string unchanged, the plugin resolver would clone ``file://``
(any repository the server user can read), connect to ``git://host:port`` (a
TCP connection from the server to anything it can reach), or run ``ext::``
(arbitrary command execution wherever git's protocol policy allows it). ``--``
on the argv stops option injection and nothing else. The profile downloader
already refuses everything but https to an allowlisted host; these tests hold
the plugin path to the same rule, at two layers: ``git_clone_target`` refuses
before any subprocess, and ``_git_env`` pins ``GIT_ALLOW_PROTOCOL`` so git
itself refuses the transports if anything ever reaches it another way.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cli_agent_orchestrator.agent_plugins import git_source, resolver
from cli_agent_orchestrator.agent_plugins.git_source import (
    ALLOWED_HOSTS_ENV,
    DEFAULT_ALLOWED_HOSTS,
    UnsupportedGitSourceError,
    allowed_hosts,
    git_clone_target,
)
from cli_agent_orchestrator.agent_plugins.models import PluginSource


@pytest.fixture(autouse=True)
def _default_allowlist(monkeypatch):
    """These tests are ABOUT the allowlist, so they run against the shipped
    default (github.com), not the package-wide test allowlist that lets other
    modules name example hosts behind a mocked subprocess. Tests that exercise
    the operator override set the variable themselves."""
    monkeypatch.delenv(ALLOWED_HOSTS_ENV, raising=False)


ACCEPTED = {
    "https://github.com/org/repo.git": "https://github.com/org/repo.git",
    "https://github.com/org/repo": "https://github.com/org/repo",
    "https://GitHub.com/org/repo/": "https://github.com/org/repo/",
    "git+https://github.com/org/repo": "https://github.com/org/repo",
    "ssh://git@github.com/org/repo.git": "ssh://git@github.com/org/repo.git",
    "ssh://git@github.com:443/org/repo": "ssh://git@github.com:443/org/repo",
    "https://github.com:8443/org/repo": "https://github.com:8443/org/repo",
    "ssh://git@github.com:22/org/repo": "ssh://git@github.com/org/repo",
    "git+ssh://git@github.com/org/repo": "ssh://git@github.com/org/repo",
    "git@github.com:org/repo.git": "git@github.com:org/repo.git",
    "github.com:org/repo.git": "github.com:org/repo.git",
    "  https://github.com/org/repo.git  ": "https://github.com/org/repo.git",
}

REFUSED = {
    # transports
    "file:///tmp/victim.git": "transport",
    "git://127.0.0.1:9000/anything": "transport",
    "git://169.254.169.254/latest": "transport",
    "ext::sh -c id": "remote-helper",
    "fd::3": "remote-helper",
    "http://github.com/org/repo": "transport",
    "git+file:///tmp/x": "git+",
    "git+git://github.com/x": "git+",
    # hosts
    "https://evil.example/org/repo": "host",
    "https://github.com.evil.example/x": "host",
    "https://raw.githubusercontent.com/o/r": "host",
    "git@gitlab.com:org/repo.git": "host",
    "ssh://git@bitbucket.org/x": "host",
    # url hygiene
    "https://user:pw@github.com/org/repo": "credentials",
    "https://token@github.com/org/repo": "credentials",
    "ssh://git:pw@github.com/org/repo": "credentials",
    "https://github.com/org/repo?x=1": "query",
    "https://github.com/org/repo#frag": "fragment",
    "https://github.com/org/repo with space": "whitespace",
    "https://github.com/org/\nrepo": "whitespace",
    # not git locations at all
    "/tmp/local/repo": "not an https",
    "./repo": "not an https",
    "-oProxyCommand=id": "not an https",
    "": "empty",
    "github.com:/abs/path": "not an https",  # scp path must not be absolute
}


class TestCloneTarget:
    @pytest.mark.parametrize("location,expected", sorted(ACCEPTED.items()))
    def test_accepted_forms_are_rebuilt_from_validated_parts(self, location, expected):
        assert git_clone_target(location) == expected

    @pytest.mark.parametrize("location,reason", sorted(REFUSED.items()))
    def test_refused_forms_name_the_reason(self, location, reason):
        with pytest.raises(UnsupportedGitSourceError) as exc:
            git_clone_target(location)
        assert reason.lower() in str(exc.value).lower(), str(exc.value)
        assert ALLOWED_HOSTS_ENV in str(exc.value)

    def test_the_refusal_is_a_value_error_not_a_resolver_error(self):
        with pytest.raises(ValueError):
            git_clone_target("file:///x")
        assert not issubclass(UnsupportedGitSourceError, resolver.ResolverError)


class TestHostAllowlist:
    def test_default_is_github_only(self, monkeypatch):
        monkeypatch.delenv(ALLOWED_HOSTS_ENV, raising=False)
        assert allowed_hosts() == DEFAULT_ALLOWED_HOSTS == frozenset({"github.com"})

    def test_operator_override_replaces_the_default(self, monkeypatch):
        monkeypatch.setenv(ALLOWED_HOSTS_ENV, " git.corp.example , GitLab.example ")
        assert allowed_hosts() == frozenset({"git.corp.example", "gitlab.example"})
        assert git_clone_target("https://git.corp.example/team/plugin.git") == (
            "https://git.corp.example/team/plugin.git"
        )
        with pytest.raises(UnsupportedGitSourceError):
            git_clone_target("https://github.com/org/repo")

    def test_blank_override_keeps_the_default(self, monkeypatch):
        monkeypatch.setenv(ALLOWED_HOSTS_ENV, " , ")
        assert allowed_hosts() == DEFAULT_ALLOWED_HOSTS

    def test_an_allowed_host_still_cannot_use_a_refused_transport(self, monkeypatch):
        monkeypatch.setenv(ALLOWED_HOSTS_ENV, "127.0.0.1,localhost")
        for location in ("git://127.0.0.1:9000/x", "file://localhost/tmp/x"):
            with pytest.raises(UnsupportedGitSourceError):
                git_clone_target(location)


class TestResolverNeverSpawnsGitForARefusedSource:
    @pytest.fixture
    def no_git(self, monkeypatch):
        calls = []

        def fake_run(*args, **kwargs):
            calls.append(args)
            raise AssertionError("git was spawned for a refused source")

        monkeypatch.setattr(resolver.subprocess, "run", fake_run)
        return calls

    @pytest.mark.parametrize(
        "location",
        [
            "file:///tmp/victim.git",
            "git://127.0.0.1:9000/anything",
            "ext::sh -c id",
            "https://evil.example/org/repo",
            "https://github.com/org/repo?x=1",
            "/tmp/local/repo",
        ],
    )
    def test_refused_before_any_subprocess(self, location, tmp_path, no_git):
        with pytest.raises(UnsupportedGitSourceError):
            resolver.resolve(PluginSource(kind="git", location=location), tmp_path / "dest")
        assert no_git == []

    def test_an_api_forced_git_kind_meets_the_same_wall(self, tmp_path, no_git):
        """``POST /plugins`` with ``kind: git`` skips the CLI's shape heuristic and
        hands the string straight to the git branch; the allowlist is in that
        branch, not in the heuristic, so it applies regardless."""
        with pytest.raises(UnsupportedGitSourceError):
            resolver.resolve(
                PluginSource(kind="git", location="file:///home/user/private.git"),
                tmp_path / "dest",
            )
        assert no_git == []


class TestGitItselfIsPinned:
    """Defence in depth: even a location that somehow reached git must be refused by git."""

    def test_every_subprocess_call_carries_the_pinned_env(self, monkeypatch, tmp_path):
        """Not skipped when git is absent: deleting ``env=_git_env()`` from
        ``_run_git`` must fail here, not only on a machine with git."""
        seen = []

        def fake_run(command, **kwargs):
            seen.append((command, kwargs.get("env")))
            raise subprocess.CalledProcessError(128, command, stderr="stop")

        monkeypatch.setattr(resolver.subprocess, "run", fake_run)
        monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "always")
        with pytest.raises(resolver.ResolverError):
            resolver.resolve(
                PluginSource(kind="git", location="https://github.com/org/repo.git"),
                tmp_path / "dest",
            )
        assert seen, "no git command was built"
        for command, env in seen:
            assert env is not None, command
            assert env["GIT_ALLOW_PROTOCOL"] == "https:ssh", command
            assert env["GIT_TERMINAL_PROMPT"] == "0", command

    def test_a_ref_beginning_with_a_dash_is_refused_before_git(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            resolver.subprocess, "run", lambda *a, **k: pytest.fail("git ran for a bad ref")
        )
        with pytest.raises(resolver.ResolverError):
            resolver.resolve(
                PluginSource(
                    kind="git", location="https://github.com/org/repo", ref="--upload-pack=x"
                ),
                tmp_path / "dest",
            )

    def test_git_env_pins_the_transport_policy(self, monkeypatch):
        monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "always")  # a hostile ambient value
        env = resolver._git_env()
        assert env["GIT_ALLOW_PROTOCOL"] == "https:ssh"

    def test_every_git_invocation_refuses_redirects(self, monkeypatch, tmp_path):
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            raise subprocess.CalledProcessError(128, command, stderr="stop")

        monkeypatch.setattr(resolver.subprocess, "run", fake_run)
        with pytest.raises(resolver.ResolverError):
            resolver.resolve(
                PluginSource(kind="git", location="https://github.com/org/repo.git"),
                tmp_path / "dest",
            )
        assert commands, "no git command was built"
        for command in commands:
            flags = [command[i + 1] for i, tok in enumerate(command) if tok == "-c"]
            assert "http.followRedirects=false" in flags, command

    @pytest.mark.skipif(not __import__("shutil").which("git"), reason="git not installed")
    @pytest.mark.parametrize(
        "location_fmt",
        ["file://{repo}", "git://127.0.0.1:1/x", "ext::sh -c true"],
    )
    def test_real_git_refuses_the_transports_under_the_pinned_env(self, tmp_path, location_fmt):
        """The pin is the property; prove it against the installed git, with the
        same environment the resolver passes, and a repo that WOULD clone."""
        repo = tmp_path / "victim.git"
        subprocess.run(["git", "init", "-q", "--bare", str(repo)], check=True)
        location = location_fmt.format(repo=repo)
        env = resolver._git_env()
        proc = subprocess.run(
            [
                "git",
                "-c",
                "protocol.ext.allow=always",
                "clone",
                "--depth",
                "1",
                "--",
                location,
                str(tmp_path / "out"),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode != 0
        assert "not allowed" in proc.stderr, proc.stderr
        assert not (tmp_path / "out").exists()


class TestSecondPassHardening:
    """Found by adversarial probing of the first version of the allowlist."""

    @pytest.mark.parametrize(
        "location,reason",
        [
            ("ssh://-oProxyCommand=id@github.com/org/repo", "user name"),
            ("-oProxyCommand=id@github.com:org/repo", "not an https"),
            ("git@github.com:-org/repo", "repository path"),
            ("git@github.com:org/repo;id", "repository path"),
            ("git@github.com:org/../../etc", "repository path"),
            ("https://github.com/-org/repo", "repository path"),
            ("https://github.com/org/../repo", "repository path"),
            ("https://github.com/org/repo;id", "repository path"),
            ("https://github.com/org/%2e%2e/repo", "repository path"),
            ("git+https://github.com:evil.example/x", "port"),
            ("https://github.com:notaport/x", "port"),
            ("https://github.com:0x1F90/x", "port"),
        ],
    )
    def test_refused_with_the_right_reason(self, location, reason):
        with pytest.raises(UnsupportedGitSourceError) as exc:
            git_clone_target(location)
        assert reason.lower() in str(exc.value).lower(), str(exc.value)

    def test_a_malformed_port_is_a_refusal_not_a_crash(self):
        """``urlsplit(...).port`` raises a bare ValueError for a non-numeric port;
        left uncaught it would surface as a 500 instead of a 400."""
        with pytest.raises(UnsupportedGitSourceError):
            git_clone_target("https://github.com:evil.example/x")

    @pytest.mark.parametrize(
        "location,expected",
        [
            ("https://GITHUB.COM/Org/Repo.git", "https://github.com/Org/Repo.git"),
            ("https://github.com:443/org/repo", "https://github.com/org/repo"),
            ("ssh://git@github.com/org/repo.git", "ssh://git@github.com/org/repo.git"),
            ("git@github.com:org/repo.git", "git@github.com:org/repo.git"),
            ("https://github.com/org/repo/", "https://github.com/org/repo/"),
            ("https://github.com/org/repo_1.2-x", "https://github.com/org/repo_1.2-x"),
        ],
    )
    def test_ordinary_repository_paths_still_pass(self, location, expected):
        assert git_clone_target(location) == expected


class TestReviewRoundOne:
    """haofeif's review of the first head (PR #847).

    P2: the first-character rule on path segments and user names rejected safe
    names the base revision accepted -- GitHub's dot-prefixed repositories and
    the ``_git`` ssh user -- with no allowlist setting that could restore them.
    P3: ``urlsplit`` raises its own ``ValueError`` before the port guard, which
    nothing converted, so two malformed URLs were a 500 instead of a refusal.
    """

    @pytest.mark.parametrize(
        "location,expected",
        [
            ("https://github.com/example/.github", "https://github.com/example/.github"),
            ("git+https://github.com/example/.github", "https://github.com/example/.github"),
            ("git@github.com:example/.dotfiles.git", "git@github.com:example/.dotfiles.git"),
            (
                "ssh://git@github.com/example/.dotfiles.git",
                "ssh://git@github.com/example/.dotfiles.git",
            ),
            ("https://github.com/_org/_repo", "https://github.com/_org/_repo"),
            ("https://github.com/org/...", "https://github.com/org/..."),
            ("https://github.com/org/.repo.git/", "https://github.com/org/.repo.git/"),
        ],
    )
    def test_safe_leading_dots_and_underscores_in_repository_paths_pass(self, location, expected):
        assert git_clone_target(location) == expected

    @pytest.mark.parametrize(
        "location,expected",
        [
            (
                "ssh://_git@git.corp.example/team/plugin.git",
                "ssh://_git@git.corp.example/team/plugin.git",
            ),
            ("_git@git.corp.example:team/plugin.git", "_git@git.corp.example:team/plugin.git"),
            (
                "git+ssh://_git@git.corp.example/team/plugin.git",
                "ssh://_git@git.corp.example/team/plugin.git",
            ),
        ],
    )
    def test_an_underscore_prefixed_ssh_user_passes_for_an_allowed_host(
        self, monkeypatch, location, expected
    ):
        monkeypatch.setenv(ALLOWED_HOSTS_ENV, "git.corp.example")
        assert git_clone_target(location) == expected

    @pytest.mark.parametrize(
        "location,reason",
        [
            # the forbidden cases the first-character rule was standing in for
            ("https://github.com/org/./repo", "repository path"),
            ("https://github.com/org/../repo", "repository path"),
            ("https://github.com/./repo", "repository path"),
            ("https://github.com/..", "repository path"),
            ("git@github.com:./repo", "repository path"),
            ("git@github.com:org/..", "repository path"),
            ("git@github.com:..", "repository path"),
            ("https://github.com/org/-repo", "repository path"),
            ("https://github.com/org/--upload-pack=x", "repository path"),
            ("git@github.com:org/-repo", "repository path"),
            ("https://github.com/org/re$po", "repository path"),
            ("https://github.com/org/re`po", "repository path"),
            ("https://github.com/org/re|po", "repository path"),
            ("https://github.com/org/re\\po", "repository path"),
            ("https://github.com/org/r%2fepo", "repository path"),
            ("ssh://-git@github.com/org/repo", "user name"),
            ("ssh://.git@github.com/org/repo", "user name"),
            ("ssh://gi$t@github.com/org/repo", "user name"),
            ("-git@github.com:org/repo", "user name"),
        ],
    )
    def test_the_actual_forbidden_cases_are_still_refused(self, location, reason):
        with pytest.raises(UnsupportedGitSourceError) as exc:
            git_clone_target(location)
        assert reason.lower() in str(exc.value).lower(), str(exc.value)

    @pytest.mark.parametrize(
        "location",
        [
            "https://[github.com/example/plugin",  # unmatched IPv6 bracket
            "https://github.com：443/example/plugin",  # full-width colon, NFKC check
            "git+https://[github.com/example/plugin",
            "ssh://git@[github.com/example/plugin",
        ],
    )
    def test_a_url_the_parser_itself_rejects_is_a_refusal_not_a_crash(self, location):
        with pytest.raises(UnsupportedGitSourceError) as exc:
            git_clone_target(location)
        assert "not a well-formed url" in str(exc.value).lower(), str(exc.value)
        assert ALLOWED_HOSTS_ENV in str(exc.value)
