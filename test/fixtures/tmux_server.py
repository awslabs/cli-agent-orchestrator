"""One explicitly named tmux server per fixture, and nothing ambient.

A fixture that creates tmux state must answer one question before it
destroys any: *which server?*  Getting that wrong is not a flaky test —
it is an outage.  A teardown here once ran an unqualified server kill
while ``TMUX`` was inherited from the surrounding pane.  tmux resolves an
unqualified invocation against ``$TMUX`` before it consults anything
else, so the command found the operator's shared server and terminated
every session on it, including work that had nothing to do with the test
run.  ``TMUX_TMPDIR`` had been set, and did not help: it moves where
*default-named* sockets live, which looks like isolation right up to the
moment an inherited ``TMUX`` overrides it.

The rules this module makes unbreakable:

* **One explicit selector, on every invocation.**  A :class:`TmuxServer`
  holds one socket path, and every command it builds begins with ``-S
  <that path>``.  No code path here runs ``tmux`` without it, and a
  second selector in the arguments is refused as ambiguity rather than
  silently resolved.
* **No ambient environment.**  ``TMUX``, ``TMUX_PANE`` and
  ``TMUX_TMPDIR`` are stripped from every subprocess environment this
  module builds.  ``TMUX_TMPDIR`` is deliberately treated as *not* an
  identity, because believing otherwise is what caused the incident.
* **Destruction proves its target first.**  The single destructive verb
  is named once, in :data:`_KILL_SERVER`, and reachable only through
  :meth:`TmuxServer.teardown`, which runs it only after proving the
  socket is one this process created inside its own directory.  If that
  cannot be proven it raises :class:`TmuxSelectorLost` and touches
  nothing.
* **Subprocesses too.**  A server under test shells out to ``tmux``
  itself and accepts no socket argument from us.  :meth:`write_shim`
  puts a ``tmux`` on its ``PATH`` that execs the real binary with this
  server's selector already applied, so "every invocation" covers the
  ones we do not write.
"""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Mapping, Optional

# Removed from every subprocess environment built here.  The first two
# select a server ambiently; the third only relocates default-named
# sockets and must never be mistaken for a server identity.
AMBIENT_SERVER_VARS = ("TMUX", "TMUX_PANE", "TMUX_TMPDIR")

# The flags that name a server.  Exactly one, supplied by us, is allowed.
SELECTOR_FLAGS = ("-S", "-L")

# The one destructive verb, named once so that "does anything else in the
# fixtures run it?" is a question about this constant rather than about
# every string in the tree.
_KILL_SERVER = "kill-server"

# Every socket this process may destroy lives in a directory it created
# with this prefix.  Both halves are checked before any teardown.
_OWNED_DIR_PREFIX = "cao-iso-"

# A unix socket path is capped near 104 bytes, so the socket cannot live
# under pytest's tmp_path (``/private/var/folders/...`` spends most of the
# budget before the test's own name appears).  A short root keeps the
# whole path well inside the limit.
_SOCKET_ROOT = Path("/tmp")

_SESSION_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")

_REAL_TMUX: Optional[str] = None


class TmuxSelectorLost(RuntimeError):
    """A tmux action could not prove which server it would reach.

    Raised instead of falling back to ambient selection, and raised by
    teardown instead of destroying something it cannot name.
    """


def real_tmux_binary() -> str:
    """The operator's tmux, resolved once.

    Resolved eagerly and cached so that a ``PATH`` carrying one of our
    shims cannot make a shim exec itself.
    """
    global _REAL_TMUX
    if _REAL_TMUX is None:
        resolved = shutil.which("tmux")
        if not resolved:
            raise TmuxSelectorLost("tmux is not installed")
        _REAL_TMUX = os.path.realpath(resolved)
    return _REAL_TMUX


def sanitized_env(base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """``base`` (default the current environment) with no ambient tmux."""
    env = dict(os.environ if base is None else base)
    for name in AMBIENT_SERVER_VARS:
        env.pop(name, None)
    return env


def default_socket_path(env: Optional[Mapping[str, str]] = None) -> Path:
    """The socket of the shared server an unqualified ``tmux`` would reach.

    Read — never selected against.  Knowing this path exactly is what
    lets a test assert the shared server *survived*, and lets teardown
    refuse it as a target.
    """
    source = os.environ if env is None else env
    inherited = source.get("TMUX", "")
    if inherited:
        # "<socket>,<pid>,<session>" — tmux uses the part before the comma.
        return Path(inherited.split(",", 1)[0])
    root = source.get("TMUX_TMPDIR") or "/tmp"
    return Path(root) / f"tmux-{os.getuid()}" / "default"


@dataclass(frozen=True)
class ServerIdentity:
    """What makes one running tmux server distinguishable from another."""

    socket_path: str
    pid: str


@dataclass(frozen=True)
class SessionIdentity:
    """A session pinned to the exact server process holding it.

    The server pid is part of the identity on purpose: a session name
    that reappeared on a *restarted* server is not the session that was
    there before, and a survival claim must not accept it.
    """

    name: str
    session_id: str
    server_pid: str
    socket_path: str


@dataclass(frozen=True)
class TmuxServer:
    """One tmux server, addressed only by its exact socket path.

    ``owned_root`` is set only for a server this process created, and is
    the sole licence to destroy it.  A handle to somebody else's server
    (the shared one, say) is constructed without it and can be read from,
    but can never be torn down.
    """

    socket_path: Path
    owned_root: Optional[Path] = None

    # ---------------------------------------------------------------- #
    # Command construction — the only place an argv is built
    # ---------------------------------------------------------------- #

    def argv(self, *args: str) -> List[str]:
        """``tmux -S <socket> <args...>``, or raise rather than guess."""
        selector = str(self.socket_path)
        if not selector or not self.socket_path.is_absolute():
            raise TmuxSelectorLost(f"not an absolute socket path: {selector!r}")
        for arg in args:
            if not isinstance(arg, str) or "\x00" in arg:
                raise TmuxSelectorLost(f"argument is not a NUL-free string: {arg!r}")
            if arg in SELECTOR_FLAGS:
                raise TmuxSelectorLost(
                    f"a second server selector ({arg}) would make the target ambiguous"
                )
        return [real_tmux_binary(), "-S", selector, *args]

    def run(
        self,
        *args: str,
        check: bool = True,
        input: Optional[bytes] = None,
        timeout: float = 15.0,
    ) -> subprocess.CompletedProcess:
        """Run one tmux command against this server and nothing else."""
        return subprocess.run(
            self.argv(*args),
            env=sanitized_env(),
            capture_output=True,
            text=input is None,
            input=input,
            check=check,
            timeout=timeout,
        )

    def out(self, *args: str) -> str:
        """Stdout of one successful tmux command, stripped."""
        return self.run(*args).stdout.strip()

    # ---------------------------------------------------------------- #
    # Reading
    # ---------------------------------------------------------------- #

    def alive(self) -> bool:
        """Whether a server is answering on this exact socket.

        ``display-message`` never starts a server, so asking is not a way
        of accidentally creating one.
        """
        try:
            return self.run("display-message", "-p", "#{pid}", check=False).returncode == 0
        except (subprocess.SubprocessError, OSError, TmuxSelectorLost):
            return False

    def identity(self) -> ServerIdentity:
        """Socket and server pid, as this server reports them itself."""
        socket_path, pid = self.out("display-message", "-p", "#{socket_path}\t#{pid}").split("\t")
        return ServerIdentity(socket_path=socket_path, pid=pid)

    def sessions(self) -> Dict[str, SessionIdentity]:
        """Every session on this server, keyed by exact name.

        Listed and matched in Python rather than resolved through a tmux
        target, so a name is compared for equality and never for prefix.
        """
        result = self.run(
            "list-sessions",
            "-F",
            "#{session_name}\t#{session_id}\t#{pid}\t#{socket_path}",
            check=False,
        )
        if result.returncode != 0:
            return {}
        found: Dict[str, SessionIdentity] = {}
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            name, session_id, pid, socket_path = line.split("\t")
            found[name] = SessionIdentity(
                name=name,
                session_id=session_id,
                server_pid=pid,
                socket_path=socket_path,
            )
        return found

    def session(self, name: str) -> SessionIdentity:
        """The named session, or a failure that says it is gone."""
        found = self.sessions().get(name)
        if found is None:
            raise AssertionError(f"session {name!r} is not on {self.socket_path}")
        return found

    # ---------------------------------------------------------------- #
    # Writing
    # ---------------------------------------------------------------- #

    def new_session(self, name: str, *args: str) -> str:
        """Create a detached session and return its printed identity."""
        self._validate_session_name(name)
        return self.out("new-session", "-d", "-s", name, *args)

    def kill_session(self, name: str, *, check: bool = False) -> None:
        """Remove exactly the named session.

        ``=name`` is tmux's exact-match target syntax.  Without it a
        target is a prefix match, which on a shared server can name a
        session the fixture never created.
        """
        self._validate_session_name(name)
        self.run("kill-session", "-t", f"={name}", check=check)

    @staticmethod
    def _validate_session_name(name: str) -> None:
        if not _SESSION_NAME.fullmatch(name or ""):
            raise TmuxSelectorLost(f"not a usable session name: {name!r}")

    # ---------------------------------------------------------------- #
    # Teardown — the only destructive path in this module
    # ---------------------------------------------------------------- #

    def _prove_own_socket(self) -> Path:
        """Return the socket only if this process is proven to own it."""
        if self.owned_root is None:
            raise TmuxSelectorLost(
                f"{self.socket_path} was not created by this process; it may not be destroyed"
            )
        selector = str(self.socket_path)
        if not selector or not self.socket_path.is_absolute():
            raise TmuxSelectorLost(f"not an absolute socket path: {selector!r}")
        if self.socket_path.name == "default":
            raise TmuxSelectorLost("a default-named socket is never a fixture's own server")
        root = self.owned_root.resolve()
        if not root.name.startswith(_OWNED_DIR_PREFIX):
            raise TmuxSelectorLost(f"{root} is not one of this process's socket directories")
        if self.socket_path.resolve().parent != root:
            raise TmuxSelectorLost(f"{self.socket_path} does not live under {root}")
        if self.socket_path.resolve() == default_socket_path().resolve():
            raise TmuxSelectorLost("refusing to destroy the shared default server")
        return self.socket_path

    def teardown(self) -> None:
        """Destroy this server, or prove why it must not be attempted.

        Raises before running anything when ownership cannot be proven,
        so a lost selector ends as a loud failure rather than as a
        broadened target.
        """
        self._prove_own_socket()
        with contextlib.suppress(subprocess.SubprocessError, OSError):
            self.run(_KILL_SERVER, check=False, timeout=10.0)
        with contextlib.suppress(OSError):
            self.socket_path.unlink(missing_ok=True)

    # ---------------------------------------------------------------- #
    # Handing the selector to subprocesses we do not write
    # ---------------------------------------------------------------- #

    def write_shim(self, directory: Path) -> Path:
        """Write a ``tmux`` into ``directory`` that carries this selector.

        A real script, never a symlink: callers resolve the binary with
        ``realpath``, which would follow a link straight back to the
        unqualified tmux and undo the whole point.
        """
        directory.mkdir(parents=True, exist_ok=True)
        shim = directory / "tmux"
        shim.write_text(
            "#!/bin/sh\n"
            f"exec {shlex.quote(real_tmux_binary())} "
            f'-S {shlex.quote(str(self.socket_path))} "$@"\n'
        )
        shim.chmod(0o755)
        if shim.is_symlink():  # pragma: no cover — defensive
            raise TmuxSelectorLost("the shim must be a real file")
        return directory

    def subprocess_env(
        self,
        shim_dir: Path,
        base: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, str]:
        """Environment for a child whose own tmux calls must land here."""
        env = sanitized_env(base)
        env["PATH"] = f"{shim_dir}{os.pathsep}{env.get('PATH', '')}"
        return env


def shared_server(env: Optional[Mapping[str, str]] = None) -> TmuxServer:
    """A read-only handle to the shared/default server.

    Built without ``owned_root``, so every destructive method on it fails
    closed by construction rather than by a caller remembering not to.
    """
    return TmuxServer(socket_path=default_socket_path(env))


@contextlib.contextmanager
def shared_server_sentinel(
    prefix: str = "cao-shared-sentinel",
) -> Iterator[tuple[TmuxServer, SessionIdentity, ServerIdentity]]:
    """A canary session on the real shared server, removed exactly.

    The sentinel is deliberately on the shared server rather than on a
    stand-in.  A stand-in would prove a fixture is careful with a server
    it was told about; only the real one proves it is careful with the
    server nobody told it about.

    Nothing reached through this handle can destroy that server: it is
    built without ownership, so every destructive method on it raises
    before acting, and the only session removed is the one created here,
    addressed by exact-match name.  The command is a bounded sleep so
    that a hard crash of the suite cannot leave a session on the
    operator's server indefinitely.
    """
    shared = shared_server()
    name = f"{prefix}-{uuid.uuid4().hex[:8]}"
    shared.new_session(name, "--", "sh", "-c", "sleep 900")
    try:
        yield shared, shared.session(name), shared.identity()
    finally:
        shared.kill_session(name)


def assert_shared_server_untouched(
    shared: TmuxServer,
    session: SessionIdentity,
    server: ServerIdentity,
) -> None:
    """The shared server is still the same server, holding the same session.

    All three claims are needed.  A server answering on the socket is not
    the same server if it was restarted, and a session with the right
    name on a restarted server is not the session that was there before.
    """
    assert shared.alive(), "the shared server did not survive the fixture"
    assert shared.identity() == server, "the shared server was restarted under us"
    assert shared.session(session.name) == session, "the sentinel session is not the same session"


@contextlib.contextmanager
def isolated_tmux_server(anchor: str = "cao-fixture-anchor") -> Iterator[TmuxServer]:
    """A private tmux server, destroyed with everything inside it.

    The anchor session keeps the server alive across the gaps when a test
    has closed its last real session — without it the server would exit
    on its own and a later teardown would prove nothing.
    """
    root = Path(tempfile.mkdtemp(prefix=_OWNED_DIR_PREFIX, dir=_SOCKET_ROOT))
    server = TmuxServer(socket_path=root / "server.sock", owned_root=root)
    try:
        server.new_session(anchor, "--", "sh", "-c", "sleep 3600")
        yield server
    finally:
        try:
            server.teardown()
        finally:
            shutil.rmtree(root, ignore_errors=True)
