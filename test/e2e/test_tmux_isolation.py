"""Acceptance for the fixtures' tmux isolation, proved on a live server.

This suite exists because a fixture teardown once destroyed the
operator's shared tmux server and every session on it.  The fixture had
set ``TMUX_TMPDIR`` and believed that was isolation; it also inherited
``TMUX`` from the pane it ran in, and tmux resolves an unqualified
invocation against ``$TMUX`` first, so an unqualified teardown reached
the shared server instead of the fixture's own.

The claim under test is therefore not "the helper works" but "the helper
cannot do that again", which needs three different kinds of evidence:

* **Construction** — every command the helper builds names one socket,
  and a second selector or a missing one is a refusal, not a fallback.
* **Refusal** — teardown proves ownership before it runs anything, and
  when it cannot, no subprocess is started at all.  That last part is
  asserted by making *any* subprocess launch fail the test.
* **Survival** — a sentinel session on the real shared server, pinned by
  name, session id and the server's own pid, is still exactly itself
  after a fixture succeeds and after a fixture fails mid-flight, while
  the fixture's own server is gone from a socket path named exactly.

The sentinel is deliberately on the shared server rather than on a
stand-in.  A stand-in would prove the helper is careful with a server it
was told about; only the real one proves it is careful with the server
nobody told it about.  Nothing here can destroy that server: the handle
to it is constructed without ownership, so every destructive method on
it raises before acting, and the only session it removes is the sentinel
it created, addressed by exact-match name.

Run with: ``pytest -m e2e test/e2e/test_tmux_isolation.py -v``
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from test.fixtures import tmux_server as iso
from test.fixtures.cao_server import _subprocess_env
from test.fixtures.tmux_server import (
    AMBIENT_SERVER_VARS,
    TmuxSelectorLost,
    TmuxServer,
    assert_shared_server_untouched,
    default_socket_path,
    isolated_tmux_server,
    real_tmux_binary,
    sanitized_env,
    shared_server,
    shared_server_sentinel,
)
from typing import Iterator

import pytest

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parents[2]

# Enough of tmux's surface to show the selector is on the read path, the
# write path and the teardown path alike.
VERBS = (
    ("new-session", "-d", "-s", "x"),
    ("list-sessions",),
    ("display-message", "-p", "#{pid}"),
    ("send-keys", "-t", "%1", "-l", "--", "hello"),
    ("kill-session", "-t", "=x"),
    (iso._KILL_SERVER,),
)


def _owned(tmp_path: Path, name: str = "server.sock") -> TmuxServer:
    """A handle that *looks* owned without a server existing behind it.

    Used by the refusal tests, which must not spawn anything.
    """
    root = tmp_path / "cao-iso-fake"
    root.mkdir(exist_ok=True)
    return TmuxServer(socket_path=root / name, owned_root=root)


class TestOneExplicitSelector:
    """Rule: one unique selector, on every invocation, or nothing."""

    def test_every_command_names_the_one_socket(self, tmp_path):
        server = _owned(tmp_path)
        for verb in VERBS:
            argv = server.argv(*verb)
            assert argv[0] == real_tmux_binary()
            assert argv[1:3] == ["-S", str(server.socket_path)]
            assert list(verb) == argv[3:]

    def test_a_second_selector_is_ambiguity_and_is_refused(self, tmp_path):
        """Not "last one wins" — a target nobody chose is the whole bug."""
        server = _owned(tmp_path)
        for extra in ("-S", "-L"):
            with pytest.raises(TmuxSelectorLost, match="ambiguous"):
                server.argv(extra, "somewhere-else")

    @pytest.mark.parametrize("selector", ["", "relative/path.sock"])
    def test_a_selector_that_names_no_server_builds_no_command(self, selector):
        with pytest.raises(TmuxSelectorLost):
            TmuxServer(socket_path=Path(selector)).argv("list-sessions")


class TestNoAmbientEnvironment:
    """Rule: strip ``TMUX``; ``TMUX_TMPDIR`` is not a server identity."""

    def test_an_inherited_server_never_reaches_a_child(self, monkeypatch):
        monkeypatch.setenv("TMUX", "/private/tmp/tmux-501/default,28665,0")
        monkeypatch.setenv("TMUX_PANE", "%4")
        monkeypatch.setenv("TMUX_TMPDIR", "/tmp/somewhere")
        env = sanitized_env()
        for name in AMBIENT_SERVER_VARS:
            assert name not in env, f"{name} survived into a fixture subprocess"

    def test_a_temp_directory_is_never_the_identity(self, tmp_path, monkeypatch):
        """Two servers under one ``TMUX_TMPDIR`` are still two servers.

        The identity is the socket path and only the socket path; the
        variable that used to stand in for it is not even passed on.
        """
        monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
        first = _owned(tmp_path, "a.sock")
        second = _owned(tmp_path, "b.sock")
        assert first.argv("list-sessions") != second.argv("list-sessions")
        assert "TMUX_TMPDIR" not in first.subprocess_env(tmp_path / "bin")

    def test_the_managed_server_subprocess_is_stripped_too(self, tmp_path, monkeypatch):
        """The server under test shells out to tmux; its env matters most."""
        monkeypatch.setenv("TMUX", "/private/tmp/tmux-501/default,28665,0")
        monkeypatch.setenv("TMUX_PANE", "%4")
        env = _subprocess_env(tmp_path, 65000)
        for name in AMBIENT_SERVER_VARS:
            assert name not in env, f"{name} survived into the managed server"


class TestDestructionProvesItsTarget:
    """Rule: no unqualified destruction; fail closed on selector loss."""

    def test_a_server_this_process_did_not_create_cannot_be_destroyed(self):
        with pytest.raises(TmuxSelectorLost, match="not created by this process"):
            shared_server().teardown()

    def test_the_shared_socket_is_refused_even_when_handed_an_owned_root(self, tmp_path):
        """Ownership is proven from the path, not asserted by the caller."""
        root = tmp_path / "cao-iso-fake"
        root.mkdir()
        impostor = TmuxServer(socket_path=default_socket_path(), owned_root=root)
        with pytest.raises(TmuxSelectorLost):
            impostor.teardown()

    def test_a_default_named_socket_inside_an_owned_root_is_still_refused(self, tmp_path):
        with pytest.raises(TmuxSelectorLost, match="default-named"):
            _owned(tmp_path, "default").teardown()

    def test_a_lost_selector_starts_no_process_at_all(self, tmp_path, monkeypatch):
        """The refusal has to come *before* anything is executed.

        Any launch during this test is a failure, whatever it would have
        run — a destructive command that fails to find its target still
        proves the code was willing to look for one.
        """

        def forbidden(*args, **kwargs):
            raise AssertionError(f"a subprocess was started during a refusal: {args!r}")

        monkeypatch.setattr(subprocess, "run", forbidden)
        root = tmp_path / "cao-iso-fake"
        root.mkdir()
        with pytest.raises(TmuxSelectorLost):
            TmuxServer(socket_path=Path(""), owned_root=root).teardown()
        with pytest.raises(TmuxSelectorLost):
            TmuxServer(socket_path=tmp_path / "elsewhere.sock", owned_root=root).teardown()

    def test_the_destructive_verb_is_named_exactly_once(self):
        """A grep-level guard, so the rule cannot regress quietly.

        Any future fixture that spells the verb out is a new unqualified
        teardown waiting to happen; the only legitimate occurrence is the
        constant that the guarded path uses.
        """
        occurrences = []
        for directory in ("test/e2e", "test/fixtures"):
            for path in sorted((REPO_ROOT / directory).rglob("*.py")):
                for number, line in enumerate(path.read_text().splitlines(), 1):
                    if iso._KILL_SERVER in line:
                        occurrences.append((str(path.relative_to(REPO_ROOT)), number, line.strip()))
        assert [where for where, _, _ in occurrences] == [
            "test/fixtures/tmux_server.py"
        ], occurrences
        assert occurrences[0][2].startswith("_KILL_SERVER =")


@pytest.fixture(scope="module")
def sentinel() -> Iterator[tuple[TmuxServer, iso.SessionIdentity, iso.ServerIdentity]]:
    with shared_server_sentinel() as canary:
        yield canary


_assert_untouched = assert_shared_server_untouched


class TestTheSharedServerSurvives:
    """Rule: sentinel acceptance, on both the success and failure paths."""

    def test_the_fixture_server_is_a_different_server(self, sentinel):
        shared, session, server = sentinel
        with isolated_tmux_server() as fixture:
            assert fixture.socket_path != shared.socket_path
            assert fixture.identity().pid != server.pid
            assert fixture.alive()
        _assert_untouched(shared, session, server)

    def test_success_path_cleanup_removes_the_fixture_server_only(self, sentinel):
        shared, session, server = sentinel
        with isolated_tmux_server() as fixture:
            fixture.new_session("cao-work", "--", "sh", "-c", "sleep 600")
            assert "cao-work" in fixture.sessions()
            socket_path, owned_root = fixture.socket_path, fixture.owned_root
            gone = TmuxServer(socket_path=socket_path)

        assert not gone.alive(), "the fixture server outlived its fixture"
        assert not socket_path.exists()
        assert owned_root is not None and not owned_root.exists()
        _assert_untouched(shared, session, server)

    def test_injected_failure_cleanup_removes_the_fixture_server_only(self, sentinel):
        """The path the incident took: cleanup running under an exception."""
        shared, session, server = sentinel
        captured: dict[str, object] = {}

        with pytest.raises(RuntimeError, match="injected"):
            with isolated_tmux_server() as fixture:
                fixture.new_session("cao-work", "--", "sh", "-c", "sleep 600")
                captured["socket"] = fixture.socket_path
                captured["root"] = fixture.owned_root
                captured["alive"] = fixture.alive()
                raise RuntimeError("injected mid-fixture failure")

        assert captured["alive"] is True, "the fixture server was never up to begin with"
        socket_path = captured["socket"]
        assert isinstance(socket_path, Path)
        assert not TmuxServer(socket_path=socket_path).alive()
        assert not socket_path.exists()
        _assert_untouched(shared, session, server)


class TestSubprocessesInheritTheSelector:
    """Rule 1 reaches the tmux invocations the fixture does not write."""

    def test_a_child_resolving_tmux_from_path_lands_on_the_fixture_socket(self, sentinel):
        shared, session, server = sentinel
        with isolated_tmux_server() as fixture:
            shim_dir = fixture.write_shim(fixture.owned_root / "bin")
            env = fixture.subprocess_env(shim_dir)

            resolved = subprocess.run(
                ["sh", "-c", "command -v tmux"],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            assert resolved == str(shim_dir / "tmux")

            landed = subprocess.run(
                ["sh", "-c", "tmux display-message -p '#{socket_path}'"],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            # Resolved on both sides: the claim is "the same socket file",
            # and tmux is free to report the path it opened rather than the
            # spelling it was handed.
            assert Path(landed).resolve() == fixture.socket_path.resolve()
            assert Path(landed).resolve() != shared.socket_path.resolve()
        _assert_untouched(shared, session, server)

    def test_the_shim_is_a_real_file_a_realpath_cannot_escape(self, sentinel):
        """Callers resolve tmux with ``realpath``; a symlink would undo this."""
        with isolated_tmux_server() as fixture:
            shim = fixture.write_shim(fixture.owned_root / "bin") / "tmux"
            assert not shim.is_symlink()
            assert os.path.realpath(shim) == str(shim.resolve())
            assert str(fixture.socket_path) in shim.read_text()
