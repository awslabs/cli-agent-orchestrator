"""``git+`` plugin sources: what is normalized, what is refused, and what is cloned.

The defect these tests pin: source-kind detection classified **every** ``git+``
location as a git source, and the resolver then handed that same string to
``git clone`` unchanged. Git reads ``git+https`` / ``git+file`` as a *remote
helper transport* name, not as the underlying URL, so the clone died with
``fatal: remote helper 'git+file' aborted session`` — a raw git stderr string
standing in for what should have been a validation error.

Every assertion here is on an **artifact**, not on a predicate's opinion:

* the argv actually handed to ``subprocess.run`` for ``git clone``, and
* the error text a caller actually receives.

Asserting the classifier's boolean would have passed throughout the defect,
which is precisely how the two sides drifted apart.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Dict, List

import pytest

from cli_agent_orchestrator.agent_plugins import resolver
from cli_agent_orchestrator.agent_plugins.models import PluginSource
from cli_agent_orchestrator.cli.commands.agent_plugin import _make_source

pytestmark = pytest.mark.usefixtures("_isolate_settings")

GIT = shutil.which("git")
requires_git = pytest.mark.skipif(GIT is None, reason="git is not installed")

#: The two ``git+`` forms CAO supports, and the URL each must become before it
#: reaches ``git clone``. Pinned against the production table by
#: ``test_the_supported_table_is_the_one_production_uses`` below, so adding a
#: third supported form cannot be done without extending these expectations —
#: and every behavioural case in this module is generated from this mapping.
EXPECTED_NORMALIZATION: Dict[str, str] = {
    "git+https://": "https://",
    "git+ssh://": "ssh://",
}

#: Every ``git+`` prefix a user could plausibly type. Expectations are *derived*
#: from ``EXPECTED_NORMALIZATION`` membership rather than restated, so a form
#: promoted to supported automatically changes what this table demands of it.
PROBE_GIT_PLUS_PREFIXES: List[str] = [
    "git+https://",
    "git+ssh://",
    "git+file://",
    "git+git://",
    "git+http://",
    "git+ftp://",
]

#: Non-``git+`` locations the classifier already treats as git. These must keep
#: reaching ``git clone`` byte-identically: normalization is scoped to ``git+``.
UNTOUCHED_GIT_LOCATIONS: List[str] = [
    "https://github.com/agentplugins/agent-plugins-example",
    "https://github.com/o/r.git",
    "ssh://git@example.test/x",
    "git://example.test/x.git",
    "git@github.com:owner/repo.git",
]

SUPPORTED_MESSAGE_TOKENS = ("git+https://", "git+ssh://")

#: Markers only a raw ``git`` stderr line — or the wrong exception type —
#: would carry. ``ResolverError`` appears because the refusal helpers below
#: prefix the exception type name, so its absence pins the type too.
RAW_GIT_STDERR_MARKERS = ("fatal:", "aborted session", "remote helper 'git+file'", "ResolverError")


def _supported_prefixes() -> Dict[str, str]:
    """The production table, imported at call time.

    A local import rather than a module-level one so the rest of this file can
    demonstrate its RED failure against the *behaviour* of the shipped code
    instead of erroring at collection on a module that did not exist yet.
    """
    from cli_agent_orchestrator.agent_plugins.git_source import SUPPORTED_GIT_PLUS_PREFIXES

    return dict(SUPPORTED_GIT_PLUS_PREFIXES)


def _clone_argv(location: str, tmp_path: Path, monkeypatch, **kwargs) -> List[str]:
    """Resolve a git source with git stubbed out, and return the clone argv.

    The stub reports success with empty output, so nothing is written and no
    network or filesystem repository is touched; the argv is the artifact.
    """
    calls: List[List[str]] = []

    class _Result:
        stdout = ""
        stderr = ""

    def fake_run(command, **_):
        calls.append(list(command))
        return _Result()

    monkeypatch.setattr(resolver.subprocess, "run", fake_run)
    resolver.resolve(PluginSource(kind="git", location=location, **kwargs), tmp_path / "dest")
    return next(argv for argv in calls if "clone" in argv)


def _clone_target(argv: List[str]) -> str:
    """The location ``git clone`` was pointed at: the token after ``--``."""
    return argv[argv.index("--") + 1]


def _refusal_with_real_git(location: str, tmp_path: Path) -> str:
    """Attempt an install-shaped resolve with the REAL git, and report the error.

    Returns ``"<ExceptionType>: <message>"``, or ``""`` if the source resolved.
    Goes through ``_make_source`` first — the CLI's own classification seam — so
    a refusal raised at classification and one raised at resolution are both
    caught. What must NOT happen is a successful classification followed by a
    git subprocess failure, which is exactly what this reported before the fix.
    """
    try:
        source = _make_source(location, None, None)
    except Exception as exc:  # noqa: BLE001 - the refusal is the subject
        return f"{type(exc).__name__}: {exc}"
    try:
        resolver.resolve(source, tmp_path / "dest")
    except Exception as exc:  # noqa: BLE001 - the refusal is the subject
        return f"{type(exc).__name__}: {exc}"
    return ""


def _refusal(location: str, tmp_path: Path, monkeypatch) -> str:
    """As above, but with git stubbed out to succeed.

    Load-bearing for the supported forms: ``https://example.test/repo.git`` is
    unresolvable, so a real clone would fail and an unreachable *host* would be
    indistinguishable from a refused *form*. With the subprocess stubbed, the
    only error that can surface is the validation verdict under test.
    """

    class _Result:
        stdout = ""
        stderr = ""

    monkeypatch.setattr(resolver.subprocess, "run", lambda command, **_: _Result())
    return _refusal_with_real_git(location, tmp_path)


@requires_git
class TestGitPlusFileIsRefusedNotAttempted:
    """The reviewer's own reproduction: a real local BARE repo, no network.

    ``git+file://`` must not be silently normalized into a working local
    filesystem read. Normalizing it broadly would convert a loud failure into an
    *accepted* read of an arbitrary local path, so this form is refused outright.
    """

    @pytest.fixture
    def bare_repo(self, tmp_path) -> Path:
        """A local bare repository, reachable only via ``file://``."""
        work = tmp_path / "work"
        work.mkdir()
        env = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(work),
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }

        def git(*args: str, cwd: Path) -> None:
            subprocess.run(
                ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, env=env
            )

        git("init", "-q", "-b", "main", cwd=work)
        (work / "plugin.json").write_text(
            '{"name": "demo", "version": "1.0.0"}\n', encoding="utf-8"
        )
        git("add", "-A", cwd=work)
        git("commit", "-q", "-m", "initial", cwd=work)

        bare = tmp_path / "demo.git"
        git("clone", "-q", "--bare", str(work), str(bare), cwd=tmp_path)
        return bare

    def test_the_refusal_names_the_supported_forms(self, bare_repo, tmp_path):
        message = _refusal_with_real_git(f"git+file://{bare_repo}", tmp_path)

        assert message, "git+file:// was accepted and resolved — it must be refused"
        for token in SUPPORTED_MESSAGE_TOKENS:
            assert token in message, f"refusal does not name {token}: {message}"

    def test_the_refusal_is_not_a_raw_git_stderr_string(self, bare_repo, tmp_path):
        """A validation error, not a ``ResolverError`` wrapping git's complaint."""
        message = _refusal_with_real_git(f"git+file://{bare_repo}", tmp_path)

        assert message.startswith("UnsupportedGitSourceError: "), message
        for marker in RAW_GIT_STDERR_MARKERS:
            assert marker not in message, f"refusal carries raw git text {marker!r}: {message}"

    def test_git_is_never_invoked_for_a_refused_form(self, bare_repo, tmp_path, monkeypatch):
        """Refused at validation means no subprocess ran at all."""
        calls: List[List[str]] = []

        def fake_run(command, **_):
            calls.append(list(command))
            raise AssertionError(f"git was invoked for a refused source: {command}")

        monkeypatch.setattr(resolver.subprocess, "run", fake_run)
        _refusal_with_real_git(f"git+file://{bare_repo}", tmp_path)

        assert calls == []

    def test_the_bare_repo_control_still_clones_over_plain_file(self, bare_repo, tmp_path):
        """The control: the same repository via plain ``file://`` must still work."""
        resolved = resolver.resolve(
            PluginSource(kind="git", location=f"file://{bare_repo}"), tmp_path / "dest"
        )

        assert (resolved.root / "plugin.json").is_file()
        assert resolved.resolved_ref and len(resolved.resolved_ref) == 40


class TestSupportedFormsAreNormalizedBeforeClone:
    @pytest.mark.parametrize("prefix", sorted(EXPECTED_NORMALIZATION))
    def test_the_clone_target_has_the_git_prefix_stripped(self, prefix, tmp_path, monkeypatch):
        expected = EXPECTED_NORMALIZATION[prefix]
        argv = _clone_argv(f"{prefix}example.test/repo.git", tmp_path, monkeypatch)

        assert _clone_target(argv) == f"{expected}example.test/repo.git"

    @pytest.mark.parametrize("prefix", sorted(EXPECTED_NORMALIZATION))
    def test_no_argv_token_anywhere_still_carries_the_git_prefix(
        self, prefix, tmp_path, monkeypatch
    ):
        argv = _clone_argv(f"{prefix}example.test/repo.git", tmp_path, monkeypatch)

        assert not any(token.startswith("git+") for token in argv), argv

    @pytest.mark.parametrize("prefix", sorted(EXPECTED_NORMALIZATION))
    def test_depth_branch_and_submodule_behaviour_are_unchanged(
        self, prefix, tmp_path, monkeypatch
    ):
        """Normalization must not disturb the flags around it."""
        argv = _clone_argv(f"{prefix}example.test/repo.git", tmp_path, monkeypatch, ref="release")

        assert argv[argv.index("--depth") + 1] == "1"
        assert argv[argv.index("--branch") + 1] == "release"
        assert "--no-recurse-submodules" in argv
        assert "--no-tags" in argv
        assert argv.index("--") < argv.index(_clone_target(argv))

    @pytest.mark.parametrize("location", UNTOUCHED_GIT_LOCATIONS)
    def test_a_non_git_plus_location_reaches_clone_byte_identically(
        self, location, tmp_path, monkeypatch
    ):
        argv = _clone_argv(location, tmp_path, monkeypatch)

        assert _clone_target(argv) == location


class TestUnsupportedFormsAreRefused:
    @pytest.mark.parametrize("prefix", PROBE_GIT_PLUS_PREFIXES)
    def test_an_unsupported_git_plus_transport_is_refused_with_a_clear_message(
        self, prefix, tmp_path, monkeypatch
    ):
        if prefix in EXPECTED_NORMALIZATION:
            pytest.skip(f"{prefix} is a supported form")

        message = _refusal(f"{prefix}example.test/repo.git", tmp_path, monkeypatch)

        assert message, f"{prefix} was accepted; it must be refused"
        assert message.startswith("UnsupportedGitSourceError: "), message
        for token in SUPPORTED_MESSAGE_TOKENS:
            assert token in message, f"refusal does not name {token}: {message}"

    def test_a_bare_git_plus_word_is_refused(self, tmp_path, monkeypatch):
        """No scheme at all — ``git+foo`` is not a URL and not a path."""
        message = _refusal("git+foo", tmp_path, monkeypatch)

        assert message
        for token in SUPPORTED_MESSAGE_TOKENS:
            assert token in message, message


class TestClassificationCannotClaimWhatTheResolverCannotConsume:
    """The reviewer's exact point, as an invariant over the whole probe table.

    For every ``git+`` form: either classification refuses it outright, or the
    string that reaches ``git clone`` is a transport git actually speaks. The
    third possibility — classified as git, handed over raw, dies in the
    subprocess — is the defect, and it is what fails here.
    """

    @pytest.mark.parametrize("prefix", PROBE_GIT_PLUS_PREFIXES)
    def test_every_accepted_form_is_clonable(self, prefix, tmp_path, monkeypatch):
        location = f"{prefix}example.test/repo.git"

        try:
            source = _make_source(location, None, None)
        except Exception as exc:  # noqa: BLE001 - refusal is an acceptable outcome
            for token in SUPPORTED_MESSAGE_TOKENS:
                assert token in str(exc), f"refusal does not name {token}: {exc}"
            return

        if source.kind != "git":
            pytest.fail(f"{location} classified as {source.kind!r}, which reports a path error")

        argv = _clone_argv(location, tmp_path, monkeypatch)
        assert not _clone_target(argv).startswith("git+"), argv

    @pytest.mark.parametrize("prefix", PROBE_GIT_PLUS_PREFIXES)
    def test_acceptance_matches_the_production_supported_table(self, prefix, tmp_path, monkeypatch):
        """Derived expectation: supported iff the production table says so."""
        supported = _supported_prefixes()
        location = f"{prefix}example.test/repo.git"
        refused = bool(_refusal(location, tmp_path, monkeypatch))

        assert refused is (prefix not in supported), (
            f"{prefix}: refused={refused} but production table "
            f"{'contains' if prefix in supported else 'omits'} it"
        )

    def test_the_supported_table_is_the_one_production_uses(self):
        """Pins this module's expectations to the shipped table.

        A future third supported form fails here until these expectations are
        extended, which is what stops a new form being added without the
        behavioural cases above covering it.
        """
        assert _supported_prefixes() == EXPECTED_NORMALIZATION
