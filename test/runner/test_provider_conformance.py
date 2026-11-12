"""The provider launch contract under the remote backend, checked per provider (#745).

Contract: with ``terminal.backend=remote`` a provider's launch does no
execution-host work on cao-server's host. In particular it never

- spawns a process there (a ``which``/``--help`` probe, a version check, a
  helper binary);
- reads the server's identity (``geteuid``/``getuid``) to decide a flag;
- reads, writes, lists or probes anything on its own behalf there: a provider
  home under the server's HOME (``~/.claude``, ``~/.copilot``, ``~/.grok``, ...)
  or a provider execution area under CAO_HOME_DIR (``providers/``, ``grok/``,
  ``ephemeral/``, ``tmp/<provider>/``), and writes nothing outside CAO's
  control plane (no scratch dir under /tmp either).

Allowed on the server: CAO's own control plane (:data:`CONTROL_PLANE_READ`:
agent profiles, skills, plugins, settings, the database...) and the files
staged under the server's ``tmp/{terminal_id}*``, which the backend ships to
the runner. Anything the CLI needs from its own host is asked of the
terminal's runner (see ``RemoteBackend.merge_user_json``,
``execution_user_is_root``).

Every provider is launched here, through its real ``initialize()`` against a
fake remote backend (its waits stubbed), in a sandbox that records what the
launch does on this host, from its own thread and from any work it starts
(including work submitted to an executor that predates it):

- spawns, ``which`` probes and uid reads. Where a harmless result exists
  (``which`` finds a fake path, ``subprocess.run`` exits 0 with no output) it is
  returned, so the launch carries on and its later dependencies show too;
- a read under the server's HOME, CAO_HOME_DIR, working directory or temp dir
  that is not CAO's control plane (listing CAO_HOME_DIR or its tmp/ counts:
  that exposes provider areas and other terminals' staged files); a write
  anywhere outside the control plane and the terminal's staging, links and
  moves included, which is also refused so the launch leaves nothing here;
- the same reads, writes and spawns reached through names no patch covers
  (``io.FileIO``, ``os.remove``, ``os.truncate``, ``os.utime``,
  ``sqlite3.connect``, ``os.posix_spawnp``, exec, fork, shutil copies, moves
  and ``rmtree``...), seen through an interpreter audit hook (PEP 578);
  ``os.mkfifo`` and ``os.mknod``, which raise no audit event, are patched;
- a path under the server's HOME, CAO_HOME_DIR (except staged files), working
  directory or temp dir baked into a command the launch sends. A literal ``~`` or ``$HOME``
  is not recorded: the runner's shell expands it to the runner's home.

Then:

- a provider in the remote allowlist (``remote_backend.DEFAULT_REMOTE_PROVIDERS``)
  must record nothing; adding a provider to the allowlist puts it under that
  check;
- every other provider is refused by the remote backend before its launch code
  runs, and its launch is still run here through a test-only seam so that its
  exact violations, and where its sandboxed launch stops, are pinned in
  :data:`KNOWN_VIOLATIONS` and :data:`KNOWN_OUTCOMES`. A change to either fails
  the test, so a port shrinks the inventory on purpose and nothing grows it
  unnoticed.

Known limits of the inventory (it guides porting; the remote backend refuses
these providers anyway, and a ported provider faces the strict check above):

- only ``initialize()`` is covered: host probes made while the terminal is
  created (kiro_cli's) are not in it;
- reads elsewhere (/etc, the Python install) are not recorded: every import
  and TLS setup reads there, and CAO's own install provides cao-mcp-server;
- a refused write raises, and a launch may stop there, so dependencies after
  its first refused write, or after a wait that never sees the fake pane ready,
  are not recorded (kimi_cli stops at its /tmp scratch dir, grok_cli before it
  sends ``GROK_HOME``). Running past them would need an isolated mirror of the
  filesystem; :data:`KNOWN_OUTCOMES` pins where each launch stops, so a stop
  that moves is still seen.

Port order for the refused providers, as agreed on #745: kiro_cli (two
capability probes and its agent-directory checks, the broadest test of the
contract), codex, copilot_cli (its server-side ``--help`` probe silently drops
``--additional-mcp-config``), then the rest by usage. Each port comes with its
row here and one live run on a runner.
"""

import asyncio
import builtins
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Dict, List

import pytest

from cli_agent_orchestrator.backends import remote_backend as rb_mod
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.providers.manager import ProviderManager

# CAO's own control plane under CAO_HOME_DIR: what a launch may touch on the
# server. Everything else under CAO_HOME_DIR (provider execution areas such as
# providers/, grok/ and ephemeral/) and anything under the server's HOME is
# execution-host work. Of tmp/ only the terminal's staged files count as CAO's:
# the backend ships those to the runner.
CONTROL_PLANE_WRITE = ("db", "logs", "locks", "fifos")
CONTROL_PLANE_READ = CONTROL_PLANE_WRITE + (
    "agent-store",
    "agent-context",
    "skills",
    "agent-plugins",
    "agent-plugin-data",
    "memory",
    "workflows",
    "flows",
    "settings.json",
    "config.json",
    ".env",
)
TERMINAL_ID = "aaaaaaaa"
LAUNCH_TIMEOUT = 15.0


class _Sandbox:
    """Records host actions taken by the launch: its own thread and any work it starts.

    Threads alive before the launch began (a watchdog another test left in this
    worker) are ignored and get the real call, unless they run work the launch
    submitted to an executor. Threads are told apart by object, not ident, so a
    reused ident cannot hide a launch thread.
    """

    def __init__(self, home: str, cao_home: str, scratch: str, profile_store=()) -> None:
        import cli_agent_orchestrator

        self.home = home
        self.cao_home = cao_home
        self.scratch = scratch
        self.package = os.path.dirname(os.path.abspath(cli_agent_orchestrator.__file__))
        self.cwd = os.getcwd()
        self.read_scope = list(dict.fromkeys([self.cwd, tempfile.gettempdir(), "/tmp"]))
        self.staging = os.path.join(cao_home, "tmp")
        # Plus the installed-profile store CAO resolves agent profiles from
        # (KIRO_AGENTS_DIR, which install_service writes): control plane too.
        self.readable = [os.path.join(cao_home, n) for n in CONTROL_PLANE_READ] + [
            os.path.abspath(os.fspath(d)) for d in profile_store
        ]
        self.writable = [os.path.join(cao_home, n) for n in CONTROL_PLANE_WRITE]
        # The same directory under its resolved name (macOS: /tmp and /var are
        # symlinks into /private) is mapped back to the name used above, so a
        # provider that resolves its paths is judged and recorded alike.
        # Computed now, before the sandbox patches os.lstat.
        self._aliases = sorted(
            {
                os.path.realpath(r): r
                for r in (self.home, self.cao_home, self.cwd, self.scratch, *self.read_scope)
                if os.path.realpath(r) != r
            }.items(),
            key=lambda item: -len(item[0]),
        )
        self.violations: List[str] = []
        self._lock = threading.Lock()
        self._active = False
        self._foreign: set = set()
        self._local = threading.local()

    def begin(self) -> None:
        current = threading.current_thread()
        self._foreign = {t for t in threading.enumerate() if t is not current}
        self._active = True

    def end(self) -> None:
        self._active = False

    def watching(self) -> bool:
        if not self._active:
            return False
        return getattr(self._local, "launch", False) or (
            threading.current_thread() not in self._foreign
        )

    def carry(self, fn):
        """Wrap work submitted by the launch so it is watched on any worker thread."""

        def run(*args, **kwargs):
            previous = getattr(self._local, "launch", False)
            self._local.launch = True
            try:
                return fn(*args, **kwargs)
            finally:
                self._local.launch = previous

        return run

    def canonical(self, text: str) -> str:
        for real, alias in self._aliases:
            if text == real or text.startswith(real + os.sep):
                return alias + text[len(real) :]
        return text

    def record(self, kind: str, detail: str) -> None:
        detail = " ".join(self.canonical(word) for word in detail.split(" "))
        detail = detail.replace(self.cao_home, "$CAO_HOME").replace(self.home, "~")
        with self._lock:
            if f"{kind}: {detail}" not in self.violations:
                self.violations.append(f"{kind}: {detail}")

    @staticmethod
    def _text(path):
        try:
            text = os.fspath(path)
        except TypeError:
            return None
        if isinstance(text, bytes):
            text = text.decode(errors="replace")
        return text

    def _abs(self, path):
        text = self._text(path)
        if text is None or "__pycache__" in text:
            return None
        return self.canonical(os.path.abspath(text))

    @staticmethod
    def _under(text, roots) -> bool:
        return any(text == r or text.startswith(r + os.sep) for r in roots)

    def _cao_owned(self, text, roots, op) -> bool:
        if text in (self.cao_home, self.staging):
            # The directories themselves only for a probe or the staging mkdir:
            # listing them would expose provider areas and other terminals'
            # staged files.
            return op in ("os.stat", "os.lstat", "os.access", "os.mkdir")
        if text.startswith(os.path.join(self.staging, TERMINAL_ID)):
            return True  # this terminal's staged files
        return self._under(text, roots)

    def host_read(self, path, op="open") -> bool:
        """A read of something on this host that is not CAO's control plane.

        In scope: the server's HOME and CAO_HOME_DIR, its working directory
        and the temp dir. CAO's own package and install (sys.prefix: its
        cao-mcp-server entry point) and this test's scratch are not; neither is
        the rest of the filesystem (/etc, the system Python), which every
        import and TLS setup reads.
        """
        text = self._abs(path)
        if text is None or self._cao_owned(text, self.readable, op):
            return False
        if self._under(text, [self.home, self.cao_home]):
            return True
        if op in ("os.stat", "os.lstat", "os.access") and any(
            r.startswith(text + os.sep) for r in self.readable + [self.cao_home]
        ):
            return False  # resolving an allowed path probes each ancestor
        if self._under(text, [self.package, sys.prefix, self.scratch]):
            return False  # CAO's own code and install (its cao-mcp-server), this test's scratch
        return self._under(text, self.read_scope)

    def host_write(self, path, op="open") -> bool:
        """A write anywhere but CAO's control plane and this terminal's staging."""
        text = self._abs(path)
        return text is not None and not self._cao_owned(text, self.writable, op)

    def refuse_write(self, name, path) -> None:
        self.record("write", f"{name} {os.fspath(path)}")
        raise PermissionError(f"no writes outside CAO's control plane: {path}")

    def audit(self, event: str, args: tuple) -> None:
        """Judge an interpreter audit event (see :func:`_audit_hook`).

        The patched functions below catch the common names; this catches the
        same operations reached any other way (``io.FileIO``, ``os.remove``,
        ``sqlite3.connect``, ``os.posix_spawnp``, ...). An event a patch has
        already refused never gets here, and one it recorded is recorded under
        the same text, so nothing is counted twice.
        """
        if event == "open":
            path, mode, flags = (tuple(args) + (None, None, None))[:3]
            if path is None or isinstance(path, int):
                return  # an fd: its open was judged when it was opened
            writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and bool(flags & _WRITE_FLAGS)
            )
            if writes:
                if self.host_write(path, "open"):
                    self.refuse_write("open", path)
            elif self.host_read(path, "open"):
                self.record("host read", f"open {os.fspath(path)}")
        elif event in _AUDITED_PATH_WRITES:
            path = args[0] if args else None
            if path is not None and not isinstance(path, int) and self.host_write(path, event):
                self.refuse_write(event, path)
        elif event in _AUDITED_DESTINATION_WRITES:
            if len(args) >= 2 and self.host_write(args[1], event):
                self.refuse_write(event, args[1])
        elif event == "sqlite3.connect":
            database = os.fspath(args[0]) if args else ":memory:"
            if isinstance(database, bytes):
                database = database.decode(errors="replace")
            if database == ":memory:" or database.startswith("file::memory:"):
                return
            if self.host_write(database.removeprefix("file:").split("?")[0], event):
                self.refuse_write(event, database)
        elif event in _AUDITED_SPAWNS:
            # subprocess.Popen's event is (executable, args, cwd, env), and
            # its executable is None unless one was given.
            first = args[0] if args and args[0] else (args[1] if len(args) > 1 else None)
            if isinstance(first, (list, tuple)):
                first = first[0] if first else None
            program = os.path.basename(os.fspath(first)) if first else ""
            self.record("spawn", f"{event} {program}".rstrip())
            raise OSError("no processes on cao-server under the remote backend")
        elif event == "glob.glob":
            pattern = args[0] if args else None
            if isinstance(pattern, (str, bytes)) and self.host_read(
                os.path.dirname(os.fspath(pattern)) or ".", "os.listdir"
            ):
                self.record("host read", f"glob.glob {os.fspath(pattern)}")


# Interpreter audit events (PEP 578) the sandbox judges beyond its patches.
# A path event's first argument is the path written.
_AUDITED_PATH_WRITES = frozenset(
    {
        "os.remove",
        "os.truncate",
        "os.utime",
        "os.chown",
        "os.chflags",
        "os.setxattr",
        "os.removexattr",
        "shutil.rmtree",
        "shutil.chown",
    }
)
# (src, dst): the destination is written.
_AUDITED_DESTINATION_WRITES = frozenset(
    {"shutil.copyfile", "shutil.copymode", "shutil.copystat", "shutil.copytree", "shutil.move"}
)
# subprocess.Popen and os.system fork and exec in C and raise only their own
# event, so a name bound before the sandbox began is seen only through these.
_AUDITED_SPAWNS = frozenset(
    {
        "os.posix_spawn",
        "os.exec",
        "os.fork",
        "os.forkpty",
        "os.spawn",
        "subprocess.Popen",
        "os.system",
    }
)
# Bound at import, before any sandbox patches the module attributes, as a
# provider's own ``from subprocess import Popen`` would be.
_EARLY_POPEN = subprocess.Popen
_EARLY_SYSTEM = os.system

# The sandbox the audit hook reports to, or None. A hook cannot be removed once
# added, so one is added for the process and does nothing while this is None.
_AUDIT_TARGET: Dict[str, object] = {"sandbox": None}
_AUDIT_LOCAL = threading.local()


def _audit_hook(event: str, args: tuple) -> None:
    box = _AUDIT_TARGET["sandbox"]
    if box is None or getattr(_AUDIT_LOCAL, "busy", False):
        return
    _AUDIT_LOCAL.busy = True  # judging an event must not judge its own I/O
    try:
        if box.watching():  # type: ignore[attr-defined]
            box.audit(event, args)  # type: ignore[attr-defined]
    finally:
        _AUDIT_LOCAL.busy = False


sys.addaudithook(_audit_hook)


_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


@pytest.fixture
def sandbox(monkeypatch, tmp_path, tmp_path_factory):
    from concurrent.futures import ThreadPoolExecutor

    from cli_agent_orchestrator.constants import CAO_HOME_DIR, KIRO_AGENTS_DIR

    home = tmp_path / "server-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    box = _Sandbox(
        str(home),
        os.fspath(CAO_HOME_DIR),
        os.fspath(tmp_path_factory.getbasetemp()),
        profile_store=[KIRO_AGENTS_DIR],
    )

    real_submit = ThreadPoolExecutor.submit

    def submit(self, fn, /, *args, **kwargs):
        if box.watching():
            fn = box.carry(fn)
        return real_submit(self, fn, *args, **kwargs)

    monkeypatch.setattr(ThreadPoolExecutor, "submit", submit)

    # Process primitives are recorded and answered with a harmless empty result
    # where one exists, so a launch carries on past them and its later
    # dependencies are recorded too. Popen and asyncio subprocesses cannot be
    # faked safely: they raise.
    def spawn(name, real, fake):
        def guarded(*args, **kwargs):
            if not box.watching():
                return real(*args, **kwargs)
            argv = args[0] if args else kwargs.get("args")
            first = argv[0] if isinstance(argv, (list, tuple)) and argv else argv
            box.record(
                "spawn", f"{name} {os.path.basename(str(first).split()[0] if first else '')}"
            )
            if fake is None:
                raise OSError("no processes on cao-server under the remote backend")
            return fake(argv, kwargs)

        return guarded

    def empty_output(argv, kwargs):
        return (
            ""
            if kwargs.get("text") or kwargs.get("encoding") or kwargs.get("universal_newlines")
            else b""
        )

    fakes = {
        "run": lambda argv, kwargs: subprocess.CompletedProcess(
            argv, 0, empty_output(argv, kwargs), empty_output(argv, kwargs)
        ),
        "Popen": None,
        "check_output": empty_output,
        "check_call": lambda argv, kwargs: 0,
        "call": lambda argv, kwargs: 0,
    }
    for name, fake in fakes.items():
        monkeypatch.setattr(
            subprocess, name, spawn(f"subprocess.{name}", getattr(subprocess, name), fake)
        )
    monkeypatch.setattr(os, "system", spawn("os.system", os.system, lambda argv, kwargs: 0))
    monkeypatch.setattr(
        os, "popen", spawn("os.popen", os.popen, lambda argv, kwargs: io.StringIO(""))
    )
    for name in ("create_subprocess_exec", "create_subprocess_shell"):
        monkeypatch.setattr(asyncio, name, spawn(name, getattr(asyncio, name), None))

    real_which = shutil.which

    def which(cmd, *args, **kwargs):
        if not box.watching():
            return real_which(cmd, *args, **kwargs)
        box.record("which", cmd)
        return f"/nonexistent/bin/{cmd}"  # found, so the launch carries on

    monkeypatch.setattr(shutil, "which", which)
    for name in ("geteuid", "getuid"):
        real_uid = getattr(os, name)

        def uid(name=name, real_uid=real_uid):
            if box.watching():
                box.record("uid", name)
            return real_uid()

        monkeypatch.setattr(os, name, uid)

    # Reads: anything under the server's HOME or CAO_HOME_DIR that is not CAO's
    # control plane counts. Writes: anything outside the control plane and this
    # terminal's staging counts (a scratch dir under /tmp is execution-host work
    # too), and is refused so a launch leaves nothing behind on this host.
    refuse_write = box.refuse_write

    def guard_open(name, real, is_write):
        def guarded(path, *args, **kwargs):
            if box.watching():
                if is_write(args, kwargs):
                    if box.host_write(path, name):
                        refuse_write(name, path)
                elif box.host_read(path, name):
                    box.record("host read", f"{name} {os.fspath(path)}")
            return real(path, *args, **kwargs)

        return guarded

    def mode_writes(args, kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        return isinstance(mode, str) and any(c in mode for c in "wax+")

    def flags_write(args, kwargs):
        flags = args[0] if args else kwargs.get("flags", 0)
        return bool(flags & _WRITE_FLAGS)

    never = lambda args, kwargs: False  # noqa: E731
    always = lambda args, kwargs: True  # noqa: E731

    # builtins.open and io.open are one function bound under two names:
    # pathlib's read_text/write_text go through io.open.
    guarded_open = guard_open("open", builtins.open, mode_writes)
    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_open)
    monkeypatch.setattr(os, "open", guard_open("os.open", os.open, flags_write))
    for name in ("stat", "lstat", "listdir", "scandir", "access"):
        monkeypatch.setattr(os, name, guard_open(f"os.{name}", getattr(os, name), never))
    # mkfifo and mknod raise no audit event, so they are patched like mkdir.
    for name in ("mkdir", "chmod", "unlink", "rmdir", "mkfifo", "mknod"):
        monkeypatch.setattr(os, name, guard_open(f"os.{name}", getattr(os, name), always))
    # Path.mkdir(exist_ok=True) and os.makedirs(exist_ok=True) swallow an error
    # when the directory already exists here, so a refused os.mkdir alone would
    # let the launch carry on or stop depending on what this host happens to
    # hold (a /tmp left by an earlier run). Refuse them before they look.
    import pathlib

    real_path_mkdir = pathlib.Path.mkdir

    def path_mkdir(self, *args, **kwargs):
        if box.watching() and box.host_write(self, "os.mkdir"):
            refuse_write("Path.mkdir", self)
        return real_path_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "mkdir", path_mkdir)
    real_makedirs = os.makedirs

    def makedirs(name, *args, **kwargs):
        if box.watching() and box.host_write(name, "os.mkdir"):
            refuse_write("os.makedirs", name)
        return real_makedirs(name, *args, **kwargs)

    monkeypatch.setattr(os, "makedirs", makedirs)
    # Moves and links write their destination (Path.symlink_to is os.symlink).
    for name in ("replace", "rename", "symlink", "link"):
        real_move = getattr(os, name)

        def move(src, dst, *args, _name=name, _real=real_move, **kwargs):
            if box.watching() and box.host_write(dst, f"os.{_name}"):
                refuse_write(f"os.{_name}", dst)
            return _real(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, name, move)
    # Everything else the interpreter audits (PEP 578): see _Sandbox.audit.
    _AUDIT_TARGET["sandbox"] = box
    try:
        yield box
    finally:
        _AUDIT_TARGET["sandbox"] = None


class _FakeRemoteBackend:
    """The backend a provider sees on cao-server under terminal.backend=remote."""

    resolves_paths_locally = False

    def __init__(self):
        self.sent = []
        self.prepared = []

    def supports_event_inbox(self):
        return False

    def send_keys(self, session, window, keys, **kwargs):
        self.sent.append(keys)

    def get_history(self, session, window, **kwargs):
        return ""

    def get_pane_current_command(self, session, window):
        return "bash"

    def get_pane_working_directory(self, session, window):
        return "/workspace"

    def merge_user_json(self, session, window, file_id, updates):
        self.prepared.append(("merge_user_json", file_id))
        return True

    def execution_user_is_root(self, session, window):
        self.prepared.append(("execution_user_is_root",))
        return False

    def unsupported_provider_reason(self, provider):
        return None  # the test-only seam: let every provider's launch code run

    def __getattr__(self, name):
        # Any other backend call is a remote op: harmless for the contract.
        return lambda *a, **k: None


def _launch(provider_name, monkeypatch, tmp_path, sandbox):
    """Run the provider's real initialize() against the fake remote backend."""
    backend = _FakeRemoteBackend()
    monkeypatch.setattr("cli_agent_orchestrator.backends.registry.get_backend", lambda: backend)
    monkeypatch.setattr(
        "cli_agent_orchestrator.backends.registry.current_backend", lambda: backend, raising=False
    )
    cls = ProviderManager.provider_class(provider_name)
    module = __import__(cls.__module__, fromlist=["_"])
    monkeypatch.setattr(module, "get_backend", lambda: backend, raising=False)

    async def ready(*args, **kwargs):
        return True

    for waiter in ("wait_for_shell", "wait_until_status"):
        monkeypatch.setattr(module, waiter, ready, raising=False)
    if hasattr(cls, "_handle_startup_prompts"):

        async def no_prompts(self, *args, **kwargs):
            return None

        monkeypatch.setattr(cls, "_handle_startup_prompts", no_prompts)
    outcome = "completed"

    async def launch():
        provider = cls(TERMINAL_ID, "cao-s1", "developer-1", "developer")
        await asyncio.wait_for(provider.initialize(), LAUNCH_TIMEOUT)

    sandbox.begin()
    try:
        asyncio.run(launch())
    except Exception as exc:  # noqa: BLE001 - a refused provider may fail in the sandbox
        outcome = type(exc).__name__
    finally:
        sandbox.end()
    # A path on this host baked into the launch command is execution-host work
    # too, even when the launch never touches it (copilot's --config-dir,
    # grok's GROK_HOME). Staged files are fine: the backend ships them and
    # rewrites their paths. A literal ~ or $HOME is fine too: the runner's
    # shell expands it to the runner's home.
    _record_command_paths(sandbox, backend.sent)
    return backend, outcome


def _record_command_paths(sandbox, sent):
    roots = [sandbox.home, sandbox.cao_home, sandbox.cwd, tempfile.gettempdir(), "/tmp"]
    pattern = "(?:" + "|".join(re.escape(r) for r in dict.fromkeys(roots)) + r")[^\s'\":;]*"
    for keys in sent:
        for match in re.finditer(pattern, str(keys)):
            path = match.group(0)
            if not path.startswith(os.path.join(sandbox.staging, TERMINAL_ID)):
                sandbox.record("command", path)


ALLOWED = sorted(rb_mod.DEFAULT_REMOTE_PROVIDERS)
REFUSED = sorted(p.value for p in ProviderType if p.value not in rb_mod.DEFAULT_REMOTE_PROVIDERS)


# --- the sandbox itself ---------------------------------------------------------------


def test_the_sandbox_records_every_primitive_from_every_kind_of_thread(sandbox):
    target = os.path.join(sandbox.home, ".copilot", "config.json")
    os.makedirs(os.path.dirname(target))
    with builtins.open(target, "w") as f:
        f.write("{}")

    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path

    scratch = Path(sandbox.home).parent / "scratch"  # outside CAO's roots
    scratch.mkdir()
    # An executor whose worker already runs before the launch begins.
    early = ThreadPoolExecutor(max_workers=1)
    early.submit(lambda: None).result()

    # Each probe alone must be recorded, so one guard cannot mask another.
    probes = {
        "builtins.open": lambda: builtins.open(target).close(),
        "io.open": lambda: io.open(target).close(),
        "Path.read_text": lambda: Path(target).read_text(),
        "os.open": lambda: os.close(os.open(target, os.O_RDONLY)),
        "os.stat": lambda: os.stat(target),
        "os.path.exists": lambda: os.path.exists(target),
        "os.lstat": lambda: os.lstat(target),
        "os.listdir": lambda: os.listdir(os.path.dirname(target)),
        "os.scandir": lambda: list(os.scandir(os.path.dirname(target))),
        "os.access": lambda: os.access(target, os.R_OK),
        "shutil.which": lambda: shutil.which("copilot"),
        "os.geteuid": lambda: os.geteuid(),
        "os.getuid": lambda: os.getuid(),
        "subprocess.run": lambda: subprocess.run(["copilot", "--help"]),
        "subprocess.Popen": lambda: subprocess.Popen(["copilot"]),
        "subprocess.check_output": lambda: subprocess.check_output(["copilot"]),
        "os.system": lambda: os.system("copilot --help"),
        "os.popen": lambda: os.popen("copilot --help"),
        "write outside": lambda: builtins.open(scratch / "probe", "w").close(),
        "os.open write outside": lambda: os.close(
            os.open(scratch / "p2", os.O_CREAT | os.O_WRONLY)
        ),
        "os.mkdir outside": lambda: os.mkdir(scratch / "d"),
        # Refused even where the directory already exists (exist_ok swallows that).
        "Path.mkdir exist_ok on an existing dir": lambda: scratch.mkdir(exist_ok=True),
        "os.makedirs exist_ok on an existing dir": lambda: os.makedirs(scratch, exist_ok=True),
        "os.replace outside": lambda: os.replace(target, scratch / "moved"),
        "os.symlink outside": lambda: os.symlink(target, scratch / "ln"),
        "Path.symlink_to outside": lambda: (scratch / "ln2").symlink_to(target),
        "os.link outside": lambda: os.link(target, scratch / "hl"),
        "listdir of CAO_HOME_DIR": lambda: os.listdir(sandbox.cao_home),
        # Reached through names no patch covers; the audit hook sees them.
        "io.FileIO read": lambda: io.FileIO(target).close(),
        "io.FileIO write outside": lambda: io.FileIO(scratch / "fio", "w").close(),
        "os.remove": lambda: os.remove(target + ".nope"),
        "os.truncate": lambda: os.truncate(target, 0),
        "os.utime": lambda: os.utime(target),
        "sqlite3.connect outside": lambda: __import__("sqlite3").connect(scratch / "db").close(),
        "os.mkfifo outside": lambda: os.mkfifo(scratch / "fifo"),
        "os.posix_spawnp": lambda: os.posix_spawnp("true", ["true"], dict(os.environ)),
        "Popen bound before the sandbox": lambda: _EARLY_POPEN(["true"]),
        "os.system bound before the sandbox": lambda: _EARLY_SYSTEM("true"),
        "shutil.copyfile outside": lambda: shutil.copyfile(target, scratch / "copy"),
        "shutil.rmtree outside": lambda: shutil.rmtree(scratch / "nothing-here"),
        "read in the server cwd": lambda: builtins.open(
            os.path.join(sandbox.cwd, "pyproject.toml")
        ).close(),
    }
    missed_per_thread = []

    def run_each():
        missed = []
        for name, probe in probes.items():
            with sandbox._lock:
                sandbox.violations.clear()
            try:
                probe()
            except OSError:
                pass
            if not sandbox.violations:
                missed.append(name)
        missed_per_thread.append(missed)

    async def launch():
        run_each()  # the launch's own thread
        await asyncio.to_thread(run_each)
        await asyncio.get_running_loop().run_in_executor(None, run_each)
        worker = threading.Thread(target=run_each)
        worker.start()
        worker.join()
        await asyncio.get_running_loop().run_in_executor(early, run_each)

    sandbox.begin()
    try:
        asyncio.run(launch())
    finally:
        sandbox.end()
        early.shutdown()
    # The launch's thread, to_thread, run_in_executor, a raw Thread and an
    # executor that predates the launch.
    assert missed_per_thread == [[], [], [], [], []]
    assert list(scratch.iterdir()) == []  # writes outside CAO's roots were refused


def test_the_sandbox_answers_what_it_can_so_a_launch_carries_on(sandbox):
    sandbox.begin()
    try:
        assert shutil.which("copilot") == "/nonexistent/bin/copilot"
        result = subprocess.run(["copilot", "--version"], capture_output=True, text=True)
        assert (result.returncode, result.stdout) == (0, "")
        assert subprocess.check_output(["copilot"]) == b""
        assert subprocess.check_call(["copilot"]) == 0
        assert os.system("copilot") == 0
        assert os.popen("copilot").read() == ""
        with pytest.raises(OSError):
            subprocess.Popen(["copilot"])
    finally:
        sandbox.end()
    assert {v.split()[1] for v in sandbox.violations if v.startswith("spawn")} == {
        "subprocess.run",
        "subprocess.check_output",
        "subprocess.check_call",
        "os.system",
        "os.popen",
        "subprocess.Popen",
    }


def test_a_host_path_in_a_launch_command_is_recorded(sandbox):
    cao = sandbox.cao_home
    _record_command_paths(
        sandbox,
        [
            f"env GROK_HOME={cao}/grok/terminals/x grok",
            f"copilot --config-dir {sandbox.home}/.copilot",
            "sh /tmp/cao_kimi_1000/probe.sh",
            f"claude --mcp-config {cao}/tmp/{TERMINAL_ID}-mcp.json",  # staged: shipped
            "codex --cd ~/work && echo $HOME",  # expanded by the runner's shell
            f"opencode --dir {sandbox.cwd}/src",
        ],
    )
    assert sandbox.violations == [
        "command: $CAO_HOME/grok/terminals/x",
        "command: ~/.copilot",
        "command: /tmp/cao_kimi_1000/probe.sh",
        f"command: {sandbox.cwd}/src",
    ]


def test_cao_control_plane_and_staging_are_not_host_work(sandbox):
    cao = sandbox.cao_home
    assert not sandbox.host_read(os.path.join(cao, "agent-store", "developer.md"))
    assert not sandbox.host_read(os.path.join(cao, "settings.json"))
    assert not sandbox.host_write(os.path.join(cao, "tmp", f"{TERMINAL_ID}-mcp.json"))
    assert not sandbox.host_write(os.path.join(cao, "db", "cli-agent-orchestrator.db"))
    assert sandbox.host_read(os.path.join(cao, "providers", "x"))
    assert sandbox.host_read(os.path.join(sandbox.home, ".grok", "auth.json"))
    assert sandbox.host_write(os.path.join(cao, "tmp", "omp", TERMINAL_ID))
    assert sandbox.host_write(os.path.join(cao, "agent-store", "developer.md"))
    assert sandbox.host_write("/tmp/cao_kimi_1000")
    # The roots themselves: a probe or the staging mkdir, never a listing.
    assert not sandbox.host_read(cao, "os.stat")
    assert not sandbox.host_write(os.path.join(cao, "tmp"), "os.mkdir")
    assert sandbox.host_read(cao, "os.listdir")
    assert sandbox.host_read(os.path.join(cao, "tmp"), "os.scandir")
    # Beyond the two homes: the server cwd and the temp dir are in scope; CAO's
    # own package data and the rest of the filesystem are not.
    assert sandbox.host_read(os.path.join(sandbox.cwd, "AGENTS.md"))
    assert sandbox.host_read("/tmp/cao_kimi_1000/probe.out")
    assert not sandbox.host_read(os.path.join(sandbox.package, "agent_store", "x.md"))
    assert not sandbox.host_read("/etc/hosts")
    assert not sandbox.host_read(os.path.join(sys.prefix, "bin", "cao-mcp-server"), "os.stat")
    # The installed-profile store, and the ancestors a path resolution probes.
    store = sandbox.readable[-1]
    assert not sandbox.host_read(os.path.join(store, "developer.json"))
    assert not sandbox.host_read(os.path.dirname(store), "os.lstat")
    assert sandbox.host_read(os.path.dirname(store), "os.listdir")


def test_a_thread_that_predates_the_launch_is_not_recorded(sandbox):
    go, done = threading.Event(), threading.Event()

    def background():
        go.wait(5)
        shutil.which("tmux")
        done.set()

    worker = threading.Thread(target=background, daemon=True)
    worker.start()
    sandbox.begin()
    try:
        go.set()
        done.wait(5)
    finally:
        sandbox.end()
    assert sandbox.violations == []


# --- every provider ---------------------------------------------------------------------


@pytest.mark.parametrize("provider_name", ALLOWED)
def test_an_allowed_provider_launches_without_touching_the_server_host(
    provider_name, sandbox, monkeypatch, tmp_path
):
    backend, outcome = _launch(provider_name, monkeypatch, tmp_path, sandbox)
    assert outcome == "completed"
    assert sandbox.violations == [], f"{provider_name} broke the remote launch contract"
    assert backend.sent, f"{provider_name} never sent its launch command"


def test_claude_code_asks_its_runner_what_it_used_to_read_here(sandbox, monkeypatch, tmp_path):
    backend, _ = _launch("claude_code", monkeypatch, tmp_path, sandbox)
    assert ("merge_user_json", "claude_settings") in backend.prepared
    assert ("execution_user_is_root",) in backend.prepared


@pytest.mark.parametrize("provider_name", REFUSED)
def test_every_other_provider_is_refused_before_its_launch_code_runs(provider_name):
    reason = rb_mod.RemoteBackend().unsupported_provider_reason(provider_name)
    assert reason and "not supported by the remote terminal backend" in reason


# What each refused provider's launch does on the server's host today (paths
# relative to the server's HOME), pinned so a port removes entries on purpose.
KNOWN_VIOLATIONS: Dict[str, List[str]] = {
    "antigravity_cli": [
        "host read: os.stat ~/.gemini/config/mcp_config.json",
        "which: agy",
        "which: cao-mcp-server",
        "write: Path.mkdir ~/.gemini/config",
        "write: open ~/.gemini/config/mcp_config.json.tmp",
    ],
    "copilot_cli": ["command: ~/.copilot", "spawn: subprocess.run copilot"],
    "cursor_cli": ["which: agent", "which: cursor-agent"],
    "grok_cli": [
        "host read: os.lstat $CAO_HOME/grok",
        "host read: os.lstat $CAO_HOME/grok/terminals",
        "host read: os.stat $CAO_HOME/grok",
        "host read: os.stat $CAO_HOME/grok/terminals",
        "host read: os.stat $CAO_HOME/grok/terminals/<terminal>",
        "uid: geteuid",
        "which: grok",
        "write: Path.mkdir $CAO_HOME/grok/terminals/<terminal>",
        "write: shutil.rmtree $CAO_HOME/grok/terminals/<terminal>",
    ],
    "kimi_cli": [
        "host read: os.stat /tmp/cao_kimi_<uid>",
        "host read: os.stat /tmp/cao_kimi_<uid>/<digest>",
        "uid: getuid",
        "write: Path.mkdir /tmp/cao_kimi_<uid>",
    ],
    "mcode": [
        "host read: os.lstat $CAO_HOME/providers",
        "host read: os.lstat $CAO_HOME/providers/minimax_code",
        "host read: os.stat $CAO_HOME/providers",
        "host read: os.stat $CAO_HOME/providers/minimax_code",
        "host read: os.stat $CAO_HOME/providers/minimax_code/<digest>",
        "which: mcode",
        "write: Path.mkdir $CAO_HOME/providers/minimax_code/<digest>",
    ],
    "omp": [
        "which: omp",
        "write: Path.mkdir $CAO_HOME/tmp/omp/aaaaaaaa",
    ],
    "opencode_cli": ["spawn: subprocess.run opencode"],
    # codex, hermes and kiro_cli record nothing from initialize(); Kiro's
    # host probes run in terminal creation, which this launch does not cover.
}

# How each refused provider's sandboxed launch ends: "completed", or the
# exception it stopped at (a refused write, a launch wait that never sees its
# fake pane become ready, a provider's own error).
KNOWN_OUTCOMES: Dict[str, str] = {
    "antigravity_cli": "PermissionError",
    "codex": "TimeoutError",
    "copilot_cli": "TimeoutError",
    "cursor_cli": "completed",
    "grok_cli": "PermissionError",
    "hermes": "completed",
    "kimi_cli": "PermissionError",
    "kiro_cli": "KiroAgentIdentityError",
    "mcode": "PermissionError",
    "omp": "PermissionError",
    "opencode_cli": "RuntimeError",
}


def _normalized(violations: List[str]) -> List[str]:
    # A temp suffix (``.tmp.<pid>``, random names) is not part of the contract.
    def one(v: str) -> str:
        v = re.sub(r"\.tmp[.\w-]*$", ".tmp", v)
        v = re.sub(r"cao_kimi_\d+", "cao_kimi_<uid>", v)
        v = re.sub(rf"/{TERMINAL_ID}-[0-9a-f]+", "/<terminal>", v)
        return re.sub(r"/[0-9a-f]{64}", "/<digest>", v)

    return sorted({one(v) for v in violations})


@pytest.mark.parametrize("provider_name", REFUSED)
def test_a_refused_providers_host_dependencies_are_pinned(
    provider_name, sandbox, monkeypatch, tmp_path
):
    _, outcome = _launch(provider_name, monkeypatch, tmp_path, sandbox)
    assert _normalized(sandbox.violations) == KNOWN_VIOLATIONS.get(provider_name, []), (
        f"{provider_name}'s server-host dependencies changed; update KNOWN_VIOLATIONS "
        "(a port shrinks it)"
    )
    # Where the sandboxed launch stops is pinned too, so a launch that now
    # stops earlier (hiding later dependencies) or fails differently is seen.
    assert outcome == KNOWN_OUTCOMES[provider_name]


def test_the_two_lists_cover_every_provider():
    assert set(ALLOWED) | set(REFUSED) == {p.value for p in ProviderType}
    assert set(ALLOWED) & set(REFUSED) == set()
    assert set(KNOWN_VIOLATIONS) <= set(REFUSED)
    assert set(KNOWN_OUTCOMES) == set(REFUSED)
