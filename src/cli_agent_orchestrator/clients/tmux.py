"""Simplified tmux client as module singleton."""

import logging
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from typing import Callable, Dict, FrozenSet, List, Optional, Tuple, TypeVar

import libtmux
from libtmux.pane import Pane
from libtmux.session import Session
from libtmux.window import Window

from cli_agent_orchestrator.constants import (
    BRACKETED_PASTE_INCOMPATIBLE_SHELLS,
    TMUX_HISTORY_LINES,
)
from cli_agent_orchestrator.utils.path_validation import (
    BLOCKED_SYSTEM_DIRECTORIES,
    resolve_and_validate_path,
)
from cli_agent_orchestrator.utils.terminal import validate_tmux_name

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class TmuxLookupError(RuntimeError):
    """A tmux listing could not be read, so the answer is UNKNOWN — not "absent".

    libtmux >= 0.53.1 parses ``list-sessions`` / ``list-windows`` / ``list-panes``
    output with ``dict(zip(formats, values, strict=True))`` (``libtmux/neo.py``,
    ``parse_output``) against a fixed 125-name format list, stripping only ONE
    trailing empty field. Any row that yields fewer than 125 values — a
    session/window/pane that disappears mid-listing, or a trailing field this
    tmux build simply omits — raises ``ValueError('zip() argument 2 is shorter
    than argument 1')`` instead of degrading. Under load (many concurrent CAO
    sessions polling the same tmux server) that race fires constantly.

    This exception exists so the failure cannot masquerade as a missing session.
    Everything else in this module signals a *genuine* absence with a plain
    ``ValueError`` (or ``None`` / ``False`` / ``[]`` for the query-style
    methods), so a caller that needs to tell "the session is really gone" from
    "we could not look" catches ``TmuxLookupError`` explicitly. It is
    deliberately NOT a subclass of ``ValueError``: an ``except ValueError``
    written for the not-found case must not swallow it.

    ``session_exists_strict`` raises it for a SECOND, unrelated cause: a
    ``list-sessions`` that tmux itself could not answer (an unreachable socket
    whose server may still be alive, a permission error, an unrecognised
    failure). Both causes mean the same thing to a caller — the answer is
    UNKNOWN — which is why they share one type. Session teardown turns it into a
    failed, re-runnable teardown with the registry left intact (#498), the
    fail-CLOSED direction.

    Treat it as transient and retryable — ``TmuxClient`` already retries the
    read once before raising.
    """


# ---------------------------------------------------------------------------
# stderr classification for ``session_exists_strict``.
#
# WARNING: THE WORDING BELOW IS TMUX-VERSION-DEPENDENT. The same injected
# failure does NOT produce the same message on every tmux: a socket path that is
# a regular file yields "Socket operation on non-socket" (ENOTSOCK) on tmux 3.7b
# but "no server running on <path>" (ECONNREFUSED) on tmux 3.2a. So do not grow
# these tables from one version's observed output, and do not write tests around
# a vector whose message tmux words ITSELF. The "error connecting to <path>
# (<strerror>)" messages are worded by the kernel's errno and are the portable
# ones. Wording we do not recognise raises, which is the safe direction.
# ---------------------------------------------------------------------------

# tmux got a definitive answer from the kernel ABOUT THE SOCKET PATH: the path
# resolved and nothing is listening on it (ECONNREFUSED). No server on the socket
# means no sessions on it, so this is a CONFIRMED absence. Treating it as an
# error instead would strand registry rows left by a long-dead server forever —
# ``delete_session`` would raise on every retry and never reconcile them.
_NO_SERVER_STDERR_MARKERS = ("no server running",)

# The socket path does not exist (ENOENT). On its own this is NOT an absence: a
# tmux server keeps serving its socket after the PATH has been unlinked — the
# classic cause is a tmp-cleaner / systemd-tmpfiles sweeping /tmp on a long-lived
# host, and the recovery is ``kill -USR1 <server-pid>``, which makes the server
# recreate the path. A never-created socket and an unlinked socket under a LIVE
# server produce byte-identical stderr, so no string matching can separate them;
# this marker is a confirmed absence only once ``_tmux_server_liveness`` says no
# tmux server is bound to the path.
_SOCKET_MISSING_STDERR_MARKERS = ("no such file or directory",)

# "error connecting to /tmp/tmux-1000/default (No such file or directory)".
# Greedy, anchored on the trailing "(<strerror>)", so socket paths that
# themselves contain " (" still parse.
_CONNECT_ERROR_PATH_PATTERN = re.compile(r"error connecting to (?P<path>.+) \([^()]*\)\s*$")

# Verdicts of ``_tmux_server_liveness``. UNKNOWN must be treated exactly like
# ALIVE by anything deciding whether a session can be declared gone.
_SERVER_ALIVE = "alive"
_SERVER_ABSENT = "absent"
_SERVER_UNKNOWN = "unknown"

# Which socket-table detector to prefer is a PLATFORM question, so it is read
# from a named module constant rather than from ``sys.platform`` at the call site:
# a test can then exercise another platform's ladder without mutating the real
# ``sys`` for everything else running in the process.
_HOST_PLATFORM = sys.platform

# The kernel's AF_UNIX socket table. Linux-only; absence of it is "cannot tell".
_PROC_NET_UNIX = "/proc/net/unix"

# macOS/BSD equivalent. ``-U`` selects AF_UNIX sockets only; ``-F n`` prints one
# field per line ("n<name>") so socket paths containing spaces survive, which
# column splitting would not guarantee. Deliberately NO other flags: ``-b``
# (avoid blocking kernel calls) suppresses the socket rows entirely on Linux lsof
# 4.94, and ``-n``/``-P`` only affect internet sockets, which ``-U`` excludes.
_LSOF_COMMAND = "lsof"
_LSOF_ARGUMENTS = ("-U", "-F", "n")
# lsof walks every process's descriptors, so on a busy host it is slow, and it
# can block on a hung mount. Teardown must not hang behind it: this bound is the
# whole reason the call is safe to make in that path. A timeout is "cannot tell"
# from THIS detector, so the ladder simply moves on.
_LSOF_TIMEOUT_SECONDS = 3.0
_PROCESS_LIST_TIMEOUT_SECONDS = 5.0
# Linux lsof decorates a unix socket's name with " type=STREAM" / " type=DGRAM";
# macOS lsof does not. Stripped so both platforms yield the bare bound path.
_LSOF_NAME_SUFFIX_PATTERN = re.compile(r"\s+type=\S+\s*$")


def _connect_error_socket_path(stderr_lines: List[str]) -> Optional[str]:
    """The socket path out of tmux's own "error connecting to ..." message.

    Taken from tmux rather than re-derived from libtmux/``TMUX_TMPDIR``, so the
    path we probe is exactly the one tmux failed to reach. ``None`` when no line
    has that shape — the caller must then fail CLOSED, since a "no such file or
    directory" from somewhere other than the connect (a missing config file, say)
    says nothing about the server.
    """
    for line in stderr_lines:
        match = _CONNECT_ERROR_PATH_PATTERN.search(line.strip())
        if match:
            return match.group("path")
    return None


def _bound_unix_socket_paths_via_proc() -> Optional[FrozenSet[str]]:
    """Bound AF_UNIX socket paths from the kernel's own table (Linux).

    Returns ``None`` when the table cannot be read — which is the NORMAL case off
    Linux, where ``/proc/net/unix`` simply does not exist. ``None`` means "this
    detector cannot tell", never "nothing is bound"; see
    ``_bound_unix_socket_paths`` for how the ladder handles that.
    """
    try:
        with open(_PROC_NET_UNIX, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return None

    paths = set()
    # Columns: Num RefCount Protocol Flags Type St Inode Path. The path is the
    # last column and is optional (unnamed sockets have none); maxsplit keeps
    # paths containing spaces intact. Line 0 is the header.
    for line in lines[1:]:
        columns = line.rstrip("\n").split(None, 7)
        if len(columns) == 8 and columns[7]:
            paths.add(columns[7])
    return frozenset(paths)


def _bound_unix_socket_paths_via_lsof() -> Optional[FrozenSet[str]]:
    """Bound AF_UNIX socket paths from ``lsof -U`` (macOS/BSD, and Linux backup).

    ``None`` when lsof is missing, times out, or produces nothing parsable — all
    "cannot tell", never "nothing is bound". In particular an EMPTY parse is
    reported as ``None`` rather than as an empty set: a host with no named unix
    socket anywhere is implausible, so an empty listing much more likely means
    lsof failed, and reading it as "nothing is bound" would fail OPEN.

    Like ``/proc/net/unix``, lsof keeps reporting the path after it has been
    unlinked, because the report comes from the process's open descriptor rather
    than from the filesystem. Verified on macOS by the reviewer of fixup 5 (lsof
    still named an unlinked private tmux socket) and re-verified here on Linux
    lsof 4.94.

    Nonzero exit is NOT treated as failure on its own: lsof routinely exits 1
    after warning about processes it could not fully inspect while still printing
    a complete-enough listing. The parse result is what decides.
    """
    try:
        result = subprocess.run(
            [_LSOF_COMMAND, *_LSOF_ARGUMENTS],
            capture_output=True,
            text=True,
            timeout=_LSOF_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        # FileNotFoundError (no lsof) and TimeoutExpired both land here.
        return None

    paths = set()
    for line in result.stdout.splitlines():
        # "-F n" emits one field per line, tagged by its first character: "p"
        # for the pid set, "n" for a name. Anything else is not a name.
        if not line.startswith("n"):
            continue
        name = line[1:]
        # Both forms, because the " type=STREAM" decoration is indistinguishable
        # from a socket path that genuinely ends that way. Keeping the undecorated
        # name as well means such a path still matches; the extra string can only
        # ever produce a spurious ALIVE, which is the fail-CLOSED direction.
        for candidate in (name, _LSOF_NAME_SUFFIX_PATTERN.sub("", name)):
            # Unnamed sockets render as "type=STREAM" alone, and lsof uses
            # "->0x..." / "socket" placeholders; only absolute paths are usable.
            if candidate.startswith("/"):
                paths.add(candidate)
    return frozenset(paths) or None


# The socket-table detectors, in the order they should be tried, per platform.
# Split out as a named seam so the non-Linux ladder is exercisable from a Linux
# test (and vice versa) instead of only on the platform that selects it.
def _socket_table_detectors(
    platform_name: Optional[str] = None,
) -> Tuple[Callable[[], Optional[FrozenSet[str]]], ...]:
    """Which socket-table detectors to try, most authoritative first.

    Linux prefers ``/proc/net/unix``: exact, pid-free and free of a subprocess.
    Everywhere else that file does not exist, so lsof leads — the alternative
    (fixup 5's behaviour) was that the whole tier reported "cannot tell" on every
    non-Linux host, which combined with fail-closed made ``session_exists_strict``
    raise unconditionally there and blocked teardown permanently.

    lsof is kept as a Linux backup too, for the case where ``/proc`` is not
    mounted in the namespace CAO runs in.
    """
    if platform_name is None:
        platform_name = _HOST_PLATFORM
    if platform_name.startswith("linux"):
        return (_bound_unix_socket_paths_via_proc, _bound_unix_socket_paths_via_lsof)
    return (_bound_unix_socket_paths_via_lsof,)


def _bound_unix_socket_paths() -> Optional[FrozenSet[str]]:
    """Every filesystem path an AF_UNIX socket is currently bound to.

    Returns ``None`` only when NO detector on this platform could answer.
    Callers MUST treat ``None`` as "cannot tell", never as "nothing is bound".

    This is the one piece of evidence that survives the failure
    ``session_exists_strict`` has to detect: the bound path stays visible after
    that path has been UNLINKED, and disappears when the owning process dies.
    tmux itself cannot tell us — its only channel to the server is the path that
    is gone.
    """
    for detector in _socket_table_detectors():
        paths = detector()
        if paths is not None:
            return paths
    return None


def _tmux_process_command_lines() -> Optional[List[str]]:
    """Command lines of running processes that mention tmux, via ``ps``.

    Last resort, for hosts where no socket table can be read at all (no
    ``/proc/net/unix`` AND no usable ``lsof``). Returns ``None`` if the
    process table cannot be listed at all. Deliberately coarse: it can prove
    "there is no tmux process anywhere" and it can prove "a tmux process names
    this socket path", but on a server started without an explicit ``-S``/``-L``
    the socket path does not appear in the argv at all, so a match failure is
    ambiguous rather than negative — see ``_tmux_server_liveness``.
    """
    try:
        result = subprocess.run(
            ["ps", "-A", "-o", "command="],
            capture_output=True,
            text=True,
            timeout=_PROCESS_LIST_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if "tmux" in line]


def _socket_path_aliases(socket_path: str) -> FrozenSet[str]:
    """``socket_path`` plus its fully resolved form.

    tmux reports the path it tried to connect to; a socket table may report the
    same socket under a resolved path instead (on macOS ``/tmp`` is a symlink to
    ``/private/tmp``). Comparing both forms can only ever turn a missed match into
    a match, i.e. ABSENT into ALIVE, which is the fail-CLOSED direction.
    """
    aliases = {socket_path}
    try:
        aliases.add(os.path.realpath(socket_path))
    except OSError:
        pass
    return frozenset(aliases)


def _tmux_server_liveness(socket_path: str) -> str:
    """Is a tmux server serving ``socket_path``, even if the path is unlinked?

    Returns ``_SERVER_ALIVE``, ``_SERVER_ABSENT`` (demonstrably none) or
    ``_SERVER_UNKNOWN``. Only ``_SERVER_ABSENT`` licenses concluding that a
    session is gone; the other two must fail CLOSED.

    The ladder, in order:

    1. **The socket table**, via ``_bound_unix_socket_paths`` — per platform,
       ``/proc/net/unix`` on Linux and ``lsof -U`` on macOS/BSD (see
       ``_socket_table_detectors``). Authoritative in BOTH directions: a live tmux
       server necessarily holds a bound AF_UNIX socket at its path, so
       path-not-listed is real evidence of absence, and the entry survives the
       path being unlinked. Verified on Linux 6.1 / tmux 3.2a for both detectors:
       listed while alive, still listed after the socket file is unlinked,
       unlisted the moment the server dies (also after a normal ``kill-server``,
       which leaves the socket FILE behind).
    2. **The process table** (``ps``), only if no socket-table detector could
       answer at all. Coarse by nature, so it answers ABSENT only when there is no
       tmux process anywhere, ALIVE when some tmux process names this exact path,
       and UNKNOWN for a tmux process it cannot attribute to the path — a server
       started without an explicit ``-S``/``-L`` (i.e. every real CAO server) does
       not carry its socket path in argv, so a match failure here is ambiguous,
       never negative.

    UNKNOWN when nothing in the ladder can be consulted. That case fails closed
    and must stay RARE: fixup 5 made it universal off Linux, which blocked
    teardown permanently there.
    """
    candidates = _socket_path_aliases(socket_path)

    bound_paths = _bound_unix_socket_paths()
    if bound_paths is not None:
        return _SERVER_ALIVE if candidates & bound_paths else _SERVER_ABSENT

    command_lines = _tmux_process_command_lines()
    if command_lines is None:
        return _SERVER_UNKNOWN
    if not command_lines:
        return _SERVER_ABSENT
    if any(candidate in command_line for command_line in command_lines for candidate in candidates):
        return _SERVER_ALIVE
    return _SERVER_UNKNOWN


class TmuxClient:
    """Simplified tmux client for basic operations."""

    # How long ``kill_session`` waits for tmux to actually reap the session
    # before giving up and reporting the kill unconfirmed. tmux's own reaping is
    # asynchronous with respect to ``kill-session`` returning, so a bound is
    # needed; 2s covers the observed lag with margin without stalling a teardown.
    _KILL_SESSION_VERIFY_TIMEOUT_SECONDS = 2.0
    _KILL_SESSION_VERIFY_INTERVAL_SECONDS = 0.2

    def __init__(self) -> None:
        self.server = libtmux.Server()

    # ── libtmux listing boundary ─────────────────────────────────────────
    #
    # Every read that makes libtmux shell out to `list-sessions` /
    # `list-windows` / `list-panes` goes through _read_listing() below, so a
    # transient parse failure (see TmuxLookupError) becomes a distinct,
    # retryable, non-fatal condition instead of a raw ValueError that reads
    # like "not found" one layer up.

    # Short pause before the single retry so the mid-listing race (a pane or
    # session vanishing between tmux's enumeration and its output) has settled.
    # Deliberately tiny: get_history() is called from the FIFO pipe-liveness
    # watchdog loop, which must not be stalled by a slow retry.
    _LISTING_RETRY_DELAY_S = 0.05

    @classmethod
    def _read_listing(cls, description: str, read: Callable[[], _T]) -> _T:
        """Run one libtmux listing read, retrying ONCE on a parse failure.

        ``read`` must contain nothing but libtmux attribute access / query
        calls. That invariant is what lets us classify *any* ``ValueError``
        escaping it as a libtmux parse failure rather than matching on
        CPython's ``zip()`` message text (which is not part of any contract).
        Callers therefore raise their own "not found" ``ValueError``s outside
        the callable, never inside it.

        Args:
            description: What was being read, for the log/error message.
            read: The libtmux read to perform.

        Returns:
            Whatever ``read`` returns.

        Raises:
            TmuxLookupError: The read failed to parse twice in a row.
        """
        try:
            return read()
        except ValueError as first_error:
            logger.warning(
                "tmux listing parse failed (%s): %s — retrying once", description, first_error
            )

        time.sleep(cls._LISTING_RETRY_DELAY_S)
        try:
            return read()
        except ValueError as second_error:
            raise TmuxLookupError(
                f"Could not read tmux listing ({description}): {second_error}. "
                "The tmux server output failed to parse twice; this is transient "
                "and says nothing about whether the target still exists."
            ) from second_error

    def _find_session(self, session_name: str) -> Optional[Session]:
        """Return the named session, or None if it is genuinely absent.

        ``default=None`` is passed explicitly: libtmux's ``QueryList.get()``
        raises ``ObjectDoesNotExist`` when nothing matches and no default is
        given, and that exception type moved between libtmux releases
        (``libtmux._internal.query_list`` on 0.51, ``libtmux.exc`` on 0.62).
        Asking for ``None`` keeps genuine absence version-stable and keeps the
        ``if not session:`` checks below meaningful.

        Raises:
            TmuxLookupError: The session listing could not be parsed.
        """
        return self._read_listing(
            f"list-sessions for session '{session_name}'",
            lambda: self.server.sessions.get(session_name=session_name, default=None),
        )

    def _find_window(
        self, session: Session, session_name: str, window_name: str
    ) -> Optional[Window]:
        """Return the named window in ``session``, or None if genuinely absent.

        Raises:
            TmuxLookupError: The window listing could not be parsed.
        """
        return self._read_listing(
            f"list-windows for '{session_name}:{window_name}'",
            lambda: session.windows.get(window_name=window_name, default=None),
        )

    def _find_active_pane(
        self, window: Window, session_name: str, window_name: str
    ) -> Optional[Pane]:
        """Return the window's active pane. ``Window.active_pane`` lists panes.

        Raises:
            TmuxLookupError: The pane listing could not be parsed.
        """
        return self._read_listing(
            f"list-panes for '{session_name}:{window_name}'",
            lambda: window.active_pane,
        )

    def _find_first_pane(self, window: Window, session_name: str, window_name: str) -> Pane:
        """Return the window's first pane. ``Window.panes`` lists panes.

        Raises:
            TmuxLookupError: The pane listing could not be parsed.
        """
        return self._read_listing(
            f"list-panes for '{session_name}:{window_name}'",
            lambda: window.panes[0],
        )

    @staticmethod
    def _kill_via_cli(session_name: str, window_name: Optional[str] = None) -> bool:
        """Kill a session (or a single window) straight through the tmux CLI.

        Deliberately bypasses libtmux: no listing is parsed, so this still
        works when ``parse_output`` cannot read the server's output. That is
        what stops a launch aborted by a parse failure from leaving a live
        tmux session behind — the operator's retry would otherwise be blocked
        by "Session already exists" with no usable terminal to attach to.

        The target uses tmux's ``=`` exact-match prefix so a prefix collision
        can never kill a *different* session.

        Returns:
            True if tmux reported the kill succeeded, False otherwise.
        """
        try:
            target = f"={validate_tmux_name(session_name, 'session_name')}"
            if window_name is not None:
                target = f"{target}:{validate_tmux_name(window_name, 'window_name')}"
        except ValueError as e:
            logger.error("Cannot build tmux kill target: %s", e)
            return False

        command = "kill-window" if window_name is not None else "kill-session"
        try:
            result = subprocess.run(
                ["tmux", command, "-t", target],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as e:
            logger.error("Failed to run `tmux %s -t %s`: %s", command, target, e)
            return False

        if result.returncode == 0:
            logger.info("Killed %s via tmux CLI: %s", command.split("-")[1], target)
            return True
        logger.warning(
            "`tmux %s -t %s` failed (rc=%s): %s",
            command,
            target,
            result.returncode,
            (result.stderr or "").strip(),
        )
        return False

    @staticmethod
    def _has_session_via_cli(session_name: str) -> Optional[bool]:
        """Parse-free ``tmux has-session`` probe used when a listing won't parse.

        Returns:
            True/False when tmux answered, None when we could not even ask
            (invalid name, or the tmux binary is unavailable) — in which case
            the caller must keep reporting "unknown" rather than "gone".
        """
        try:
            target = f"={validate_tmux_name(session_name, 'session_name')}"
        except ValueError as e:
            logger.error("Cannot build tmux has-session target: %s", e)
            return None
        try:
            result = subprocess.run(
                ["tmux", "has-session", "-t", target],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as e:
            logger.error("Failed to run `tmux has-session -t %s`: %s", target, e)
            return None
        # tmux exits non-zero both for "no such session" and "no server
        # running"; both mean the session does not exist.
        return result.returncode == 0

    # Kept as an alias so existing callers/tests referencing the class
    # attribute keep working; the canonical set lives in
    # utils/path_validation.py (shared with archive export/import, D5).
    _BLOCKED_DIRECTORIES = BLOCKED_SYSTEM_DIRECTORIES

    def _resolve_and_validate_working_directory(self, working_directory: Optional[str]) -> str:
        """Resolve and validate working directory.

        Delegates to the shared validator
        (``utils.path_validation.resolve_and_validate_path``) with its
        strictest settings: the directory must already exist and file
        targets are rejected — byte-identical to the pre-extraction
        behavior.

        **Allowed directories:**

        - Any real directory that is not a blocked system path
        - Paths outside ``~/`` are permitted (e.g., ``/Volumes/workplace``,
          ``/opt/projects``, NFS mounts)

        **Blocked (unsafe) directories:**

        - System directories: ``/``, ``/bin``, ``/sbin``, ``/usr/bin``,
          ``/usr/sbin``, ``/etc``, ``/var``, ``/tmp``, ``/dev``, ``/proc``,
          ``/sys``, ``/root``, ``/boot``, ``/lib``, ``/lib64``

        Args:
            working_directory: Optional directory path, defaults to current directory

        Returns:
            Canonicalized absolute path

        Raises:
            ValueError: If directory does not exist or is a blocked system path
        """
        if working_directory is None:
            working_directory = os.getcwd()

        return resolve_and_validate_path(
            working_directory,
            allow_create=False,
            allow_file=False,
            description="Working directory",
        )

    # Provider env vars that would cause "nested session" errors when CAO
    # itself runs inside a provider (e.g. Claude Code), unless explicitly
    # allow-listed for provider authentication (Bedrock, Vertex AI, Foundry).
    # Applied to BOTH inherited env and operator-supplied --env vars so a
    # forwarded ``CLAUDE_CODE_*`` cannot reintroduce nesting.
    _BLOCKED_ENV_PREFIXES = ("CLAUDE", "CODEX_", "__MISE_")
    _BLOCKED_PREFIX_ALLOWLIST = frozenset(
        {
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
            "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
            "CLAUDE_CODE_SKIP_VERTEX_AUTH",
            "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
        }
    )
    # Per-var value cap (PR #246) — keeps the full tmux ``new-session -e`` /
    # ``new-window -e`` argv under the kernel argv limit on busy hosts.
    _MAX_ENV_VALUE_BYTES = 2048

    @classmethod
    def _is_blocked_env_key(cls, key: str) -> bool:
        """Return True if ``key`` matches a blocked prefix and isn't allowlisted."""
        if key in cls._BLOCKED_PREFIX_ALLOWLIST:
            return False
        return any(key.startswith(p) for p in cls._BLOCKED_ENV_PREFIXES)

    @classmethod
    def _merge_extra_env(
        cls, environment: Dict[str, str], extra_env: Optional[Dict[str, str]]
    ) -> None:
        """Merge operator-supplied env vars into ``environment`` in place.

        Mirrors the safety constraints applied to inherited env (blocked
        prefixes, 2048-byte value cap) so a malformed --env entry cannot
        slip past the validation that runs at the CLI boundary.
        """
        if not extra_env:
            return
        for key, value in extra_env.items():
            if cls._is_blocked_env_key(key):
                logger.warning("Dropping forwarded env var with blocked prefix: %s", key)
                continue
            if len(value.encode("utf-8")) >= cls._MAX_ENV_VALUE_BYTES:
                logger.warning(
                    "Dropping forwarded env var %s — value exceeds %d bytes",
                    key,
                    cls._MAX_ENV_VALUE_BYTES,
                )
                continue
            environment[key] = value

    def create_session(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> str:
        """Create detached tmux session with initial window and return window name."""
        try:
            working_directory = self._resolve_and_validate_working_directory(working_directory)

            # Only pass essential env vars to avoid tmux "command too long"
            essential_keys = {
                "HOME",
                "PATH",
                "SHELL",
                "USER",
                "LANG",
                "LC_ALL",
                "LC_CTYPE",
                "TERM",
                "SSH_AUTH_SOCK",
                "DISPLAY",
                "XDG_RUNTIME_DIR",
                "DO_NOT_TRACK",
            }
            environment = {
                k: v
                for k, v in os.environ.items()
                if (
                    k in essential_keys
                    or k in self._BLOCKED_PREFIX_ALLOWLIST
                    or (
                        not self._is_blocked_env_key(k)
                        and k.startswith(("CAO_", "KIRO_", "MISE_", "AWS_"))
                        and len(v.encode("utf-8")) < self._MAX_ENV_VALUE_BYTES
                    )
                )
            }
            # Operator-forwarded vars (from ``cao launch --env``) merge AFTER
            # the inherited slice and override on key collision, so an
            # explicit ``--env AWS_REGION=us-west-2`` wins over the inherited
            # value. See issue #248.
            self._merge_extra_env(environment, extra_env)
            environment["CAO_TERMINAL_ID"] = terminal_id

            # Explicit 220x50 pane size avoids the default 80x24 that tmux
            # assigns to detached sessions. kiro-cli 2.1.x's TUI v2 fails to
            # repaint after a SIGWINCH from the attach-time resize (80x24 →
            # user's real terminal): the screen goes blank and input is
            # silently dropped. Starting at a larger size makes the attach
            # resize a no-op/shrink, which kiro handles correctly. All other
            # providers tolerate wider panes. See issue #216.
            try:
                session = self.server.new_session(
                    session_name=session_name,
                    window_name=window_name,
                    start_directory=working_directory,
                    detach=True,
                    environment=environment,
                    x=220,
                    y=50,
                )
            except ValueError as e:
                # new_session() lists the server to build its Session object,
                # so it can fail to PARSE output for a session tmux has
                # ALREADY created (see TmuxLookupError). tmux refusing a
                # duplicate name raises LibTmuxException, not ValueError, so a
                # ValueError here means "our session may exist but we cannot
                # see it". Tear it down through the parse-free CLI: otherwise
                # the caller's rollback never fires (it only knows the session
                # was created once this method RETURNS) and the retry is
                # blocked by "Session already exists" with a dead pane.
                logger.error(
                    f"tmux listing failed while creating session {session_name}: {e} — "
                    "removing any session tmux created before re-raising"
                )
                self._kill_via_cli(session_name)
                raise TmuxLookupError(
                    f"Could not read tmux listing while creating session "
                    f"'{session_name}': {e}. Any partially created session has "
                    "been removed; the launch can be retried with the same name."
                ) from e

            # Keep mouse-wheel input inside tmux. With mouse mode disabled,
            # tmux forwards wheel events to the foreground application as
            # Up/Down keys, which makes shells and agent TUIs navigate their
            # command history instead of entering copy mode to scroll output
            # (#546). This is a session option, so it does not change the
            # user's other tmux sessions or their global configuration.
            # Standard tmux trade-off: click-drag now makes a tmux selection
            # rather than the terminal's (Shift-drag reaches the native one).
            # The flip side -- a wheel-scrolled pane now routinely sits in
            # copy mode, where it consumes orchestrated keys (#654) -- is
            # handled by the mode-cancel guards in each send path below.
            # Best-effort on purpose: mouse-on is a scroll convenience, not a
            # precondition for a usable session, and this line sits after
            # new_session but outside the rollback guard below -- raising
            # here would orphan a perfectly good session and block relaunch
            # under the same name.
            try:
                session.set_option("mouse", "on")
            except Exception as mouse_error:
                logger.warning(
                    f"Could not enable mouse mode on session {session_name}: "
                    f"{mouse_error} — continuing without wheel scrolling."
                )

            logger.info(
                f"Created tmux session: {session_name} with window: {window_name} in directory: {working_directory}"
            )
            try:
                window_name_result = self._read_listing(
                    f"list-windows for new session '{session_name}'",
                    lambda: session.windows[0].name,
                )
                if window_name_result is None:
                    raise ValueError(f"Window name is None for session {session_name}")
            except TmuxLookupError:
                # Same half-state, one step later: the session is up but we
                # cannot confirm its window. Use the parse-free CLI here too —
                # session.kill() would need the very listing that just failed.
                self._kill_via_cli(session_name)
                raise
            except Exception:
                # Any other post-creation failure: the session exists and this
                # method is about to raise, so nothing downstream will ever
                # learn it needs cleaning up. Kill it here.
                try:
                    session.kill()
                except Exception as kill_error:  # pragma: no cover - best effort
                    logger.warning(f"Failed to roll back session {session_name}: {kill_error}")
                raise
            return window_name_result
        except Exception as e:
            logger.error(f"Failed to create session {session_name}: {e}")
            raise

    def create_window(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        window_shell: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> str:
        """Create window in session and return window name.

        ``extra_env`` carries operator-forwarded vars from
        ``cao launch --env`` so workers spawned via ``assign`` / ``handoff`` /
        the web UI inherit the same context as the supervisor. See issue #248.
        """
        try:
            working_directory = self._resolve_and_validate_working_directory(working_directory)

            session = self._find_session(session_name)
            if not session:
                raise ValueError(f"Session '{session_name}' not found")

            window_env: dict[str, str] = {}
            self._merge_extra_env(window_env, extra_env)
            window_env["CAO_TERMINAL_ID"] = terminal_id

            kwargs: dict = {
                "window_name": window_name,
                "start_directory": working_directory,
                "environment": window_env,
            }
            if window_shell:
                kwargs["window_shell"] = window_shell

            window = session.new_window(**kwargs)

            logger.info(
                f"Created window '{window.name}' in session '{session_name}' in directory: {working_directory}"
            )
            window_name_result = window.name
            if window_name_result is None:
                raise ValueError(f"Window name is None for session {session_name}")
            return window_name_result
        except Exception as e:
            logger.error(f"Failed to create window in session {session_name}: {e}")
            raise

    # tmux >= 3.7 passes pasted buffer content through vis(3) sanitization
    # (hardening against bracket-end injection): raw ESC (0x1b) bytes loaded
    # into a buffer arrive in the pane as the literal two characters "^[".
    # Older tmux passes buffer bytes through unchanged. Cached per process —
    # the tmux server version cannot change under a running CAO server.
    _paste_buffer_sanitizes: Optional[bool] = None

    @classmethod
    def _tmux_sanitizes_paste_buffers(cls) -> bool:
        """Return True when this host's tmux (>= 3.7) vis(3)-sanitizes pasted buffers."""
        if cls._paste_buffer_sanitizes is None:
            try:
                out = subprocess.run(
                    ["tmux", "-V"], capture_output=True, text=True, check=True
                ).stdout
                match = re.search(r"(\d+)\.(\d+)", out)
                if match:
                    version = (int(match.group(1)), int(match.group(2)))
                    cls._paste_buffer_sanitizes = version >= (3, 7)
                else:
                    # "tmux master" / unparseable: assume a modern build that
                    # sanitizes. Raw content + -p never renders garbage on any
                    # version; a wrongly hand-crafted wrap on a sanitizing
                    # tmux does (issue #413), so this is the safe default.
                    cls._paste_buffer_sanitizes = True
            except Exception:
                cls._paste_buffer_sanitizes = True
        return cls._paste_buffer_sanitizes

    def _pane_is_bracketed_paste_incompatible(self, session_name: str, window_name: str) -> bool:
        """Whether the pane's live foreground command is a known shell.

        Used by ``send_keys(force_bracketed_paste=True)`` to avoid gluing
        bracketed-paste escape sequences onto a bare shell prompt that
        doesn't understand them (see BRACKETED_PASTE_INCOMPATIBLE_SHELLS'
        own docstring for the failure mode). Reuses the same
        ``#{pane_current_command}`` probe already trusted elsewhere in this
        codebase for an equivalent "has the TUI exited back to a shell?"
        check (``codex.py``/``kiro_cli.py``'s own ``shell_baseline``
        comparison in ``get_status()``).

        Fails closed to "compatible" (returns False) on any lookup failure
        or an unrecognized command name -- an unknown foreground process is
        assumed to be a real TUI expecting bracketed paste, preserving the
        existing behavior for every case this function can't positively
        rule out.
        """
        command = self.get_pane_current_command(session_name, window_name)
        return command is not None and command in BRACKETED_PASTE_INCOMPATIBLE_SHELLS

    def send_keys(
        self,
        session_name: str,
        window_name: str,
        keys: str,
        enter_count: int = 1,
        force_bracketed_paste: bool = False,
        submit_delay: float = 0.3,
    ) -> None:
        """Send keys to window using tmux paste-buffer for instant delivery.

        Uses load-buffer + paste-buffer instead of chunked send-keys to avoid
        slow character-by-character input and special character interpretation.
        Bracketed-paste framing keeps multi-line content as a single input
        rather than submitting on each newline; how it is applied depends on
        the tmux version (see force_bracketed_paste below).

        Args:
            session_name: Name of tmux session
            window_name: Name of window in session
            keys: Text to send
            enter_count: Number of Enter keys to send after pasting (default 1).
                Some TUIs enter multi-line mode after bracketed paste,
                requiring 2 Enters to submit.
            force_bracketed_paste: If True, guarantee bracketed-paste framing
                for message delivery to TUIs -- UNLESS the pane's live
                foreground command (per #{pane_current_command}) is a known
                shell (see BRACKETED_PASTE_INCOMPATIBLE_SHELLS), in which
                case the wrap is skipped entirely and the content is
                delivered as plain keystrokes instead. A bare shell doesn't
                understand the escape sequences and glues them onto the
                first token of the command, corrupting it -- the caller
                asking for bracketed delivery does not always know the
                target TUI has already exited (e.g. via its own `/exit`)
                and left the pane at a shell prompt, so this is checked
                fresh on every call rather than trusted from the caller's
                own intent.

                For a pane that IS running a real TUI, how the wrap is
                applied depends on the tmux version: on tmux < 3.7 this
                wraps the buffer in hand-crafted \x1b[200~...\x1b[201~
                markers and pastes with -r (no LF->CR conversion) —
                required because paste-buffer -p only emits markers when
                the pane enabled DECSET 2004, and some TUIs (e.g. kiro-cli)
                never do, so under -p their multi-line messages would
                submit per-line (#230). On tmux >= 3.7 the hand-crafted wrap
                is impossible: pasted buffer content passes through vis(3)
                sanitization (hardening against bracket-end injection),
                turning each raw ESC into the literal two characters "^["
                — hand-crafted markers render as visible "^[[200~" garbage
                in the receiving TUI (issue #413). There we paste raw
                content with -p, which emits genuine 0x1b markers
                conditionally on the pane's DECSET 2004 state; non-2004
                panes receive raw text and multi-line content submits
                per-line (tmux-sanctioned paste semantics — nothing better
                exists on >= 3.7 without -S, which is deliberately NOT used
                because it bypasses the vis(3) hardening and would let
                worker-authored message bytes smuggle control sequences
                into a receiving TUI). Do NOT set for shell commands sent
                to bash during initialization (bash 4.x would receive the
                literal escape sequences on tmux < 3.7).
        """
        # Defence-in-depth: re-validate at the sink even though callers
        # validate at the API/MCP boundary. Both halves flow into a
        # tmux subprocess argument (-t target), and tmux itself parses
        # ':' / '.' as target delimiters, so any leak past upstream
        # validation could pivot to a different pane. Validating here
        # also clears the CodeQL py/command-line-injection data flow.
        validated_session = validate_tmux_name(session_name, "session_name")
        validated_window = validate_tmux_name(window_name, "window_name")
        target = f"{validated_session}:{validated_window}"
        buf_name = f"cao_{uuid.uuid4().hex[:8]}"
        try:
            # Log metadata only at INFO: the payload is the full launch
            # command / message, which can include MCP env values (API
            # tokens from a profile's mcpServers.env) and entire system
            # prompts. This matches send_keys_via_paste, which logs only
            # the text length at INFO. Full content additionally remains
            # available here at DEBUG for local delivery troubleshooting.
            logger.info(f"send_keys: {target} - keys length: {len(keys)}")
            logger.debug(f"send_keys: {target} - keys: {keys}")
            # A pane the user has wheel-scrolled sits in copy mode (routine
            # now that sessions enable mouse), and a pane in a mode consumes
            # send-keys through the mode's key table instead of delivering
            # them to the application: the submitting Enter below would be
            # eaten by copy mode while the pasted message sits unsubmitted
            # (#654, verified on tmux 3.4). Cancel any active mode first.
            # Unconditional on purpose -- when the pane is not in a mode the
            # command fails with "not in a mode" and delivers nothing, so no
            # pane-state pre-check is needed (and none would be race-free).
            subprocess.run(
                ["tmux", "send-keys", "-t", target, "-X", "cancel"],
                check=False,
                capture_output=True,
            )
            if force_bracketed_paste and self._pane_is_bracketed_paste_incompatible(
                validated_session, validated_window
            ):
                # The pane's live foreground command is a known shell (see
                # BRACKETED_PASTE_INCOMPATIBLE_SHELLS) -- it won't reliably
                # understand bracketed-paste escapes, so don't ask tmux to
                # add them, on ANY tmux version. Deliberately omitting -p
                # here too, not just the manual \x1b[200~ wrap used below
                # for a real TUI on tmux < 3.7: -p's own bracket-emitting
                # decision is driven by tmux's per-pane ?2004h tracking,
                # which can itself be stale (the very TUI that used to run
                # in this pane can leave it "on" without ever sending
                # ?2004l on exit) -- so -p is not a safe fallback here
                # either. Sending with no paste flag at all never adds
                # escape sequences regardless of that tracked state; \n
                # still becomes Enter (no -r), the correct behavior for a
                # plain shell prompt.
                buf_content = keys.encode()
                paste_args = []
            elif force_bracketed_paste and not self._tmux_sanitizes_paste_buffers():
                # A real TUI, tmux < 3.7: buffer bytes pass through
                # paste-buffer unmodified, so keep the legacy unconditional
                # wrap + -r (no LF->CR conversion). paste-buffer -p only
                # emits markers when the pane enabled DECSET 2004, and some
                # TUIs (e.g. kiro-cli) never do — under -p their multi-line
                # messages would submit per-line (#230). No pane-level
                # bracketed-paste state format exists on these versions
                # (verified empirically on 3.4), so the wrap stays
                # unconditional here.
                buf_content = b"\x1b[200~" + keys.encode() + b"\x1b[201~"
                paste_args = ["-r"]
            else:
                # Either force_bracketed_paste=False, or a real TUI on
                # tmux >= 3.7 which vis(3)-sanitizes pasted buffer content:
                # raw ESC bytes in the buffer would render as literal
                # "^[[200~" in the receiving TUI (issue #413). Load ONLY the
                # raw message bytes and let tmux emit genuine markers via
                # -p, conditionally on the pane's DECSET 2004 state. -S is
                # deliberately NOT used: it bypasses the vis(3) hardening.
                buf_content = keys.encode()
                paste_args = ["-p"]
            subprocess.run(
                ["tmux", "load-buffer", "-b", buf_name, "-"],
                input=buf_content,
                check=True,
            )
            subprocess.run(
                ["tmux", "paste-buffer", *paste_args, "-b", buf_name, "-t", target],
                check=True,
            )
            # Delay to let the TUI process the bracketed paste end sequence before
            # sending Enter. Without enough delay, some TUIs (e.g. the newest
            # Claude Code Ink renderer) swallow the Enter that immediately follows
            # paste-buffer, leaving the message unsubmitted. The duration is
            # provider-tunable via ``submit_delay`` (BaseProvider.paste_submit_delay).
            time.sleep(submit_delay)
            for i in range(enter_count):
                if i > 0:
                    # Delay between Enter presses for TUIs that need time to
                    # process the previous Enter (e.g., Ink adding a newline)
                    # before the next Enter triggers form submission.
                    time.sleep(0.5)
                # Re-cancel right before each submitting Enter: submit_delay
                # is up to 2s (claude_code's paste_submit_delay), and a wheel
                # scroll inside that window would put the pane back in copy
                # mode and eat the Enter -- the exact #654 stall the leading
                # cancel exists to prevent (text in the composer, submission
                # never fires, nothing raises).
                subprocess.run(
                    ["tmux", "send-keys", "-t", target, "-X", "cancel"],
                    check=False,
                    capture_output=True,
                )
                subprocess.run(
                    ["tmux", "send-keys", "-t", target, "Enter"],
                    check=True,
                )
            logger.debug(f"Sent keys to {target}")
        except Exception as e:
            logger.error(f"Failed to send keys to {target}: {e}")
            raise
        finally:
            subprocess.run(
                ["tmux", "delete-buffer", "-b", buf_name],
                check=False,
            )

    def send_keys_via_paste(self, session_name: str, window_name: str, text: str) -> None:
        """Send text to window via tmux paste buffer with bracketed paste mode.

        Uses tmux set-buffer + paste-buffer -p to send text as a bracketed paste,
        which bypasses TUI hotkey handling. Essential for Ink-based CLIs and
        other TUI apps where individual keystrokes may trigger hotkeys.

        After pasting, sends C-m (Enter) to submit the input.

        Args:
            session_name: Name of tmux session
            window_name: Name of window in session
            text: Text to paste into the pane
        """
        try:
            logger.info(
                f"send_keys_via_paste: {session_name}:{window_name} - text length: {len(text)}"
            )

            session = self._find_session(session_name)
            if not session:
                raise ValueError(f"Session '{session_name}' not found")

            window = self._find_window(session, session_name, window_name)
            if not window:
                raise ValueError(f"Window '{window_name}' not found in session '{session_name}'")

            pane = self._find_active_pane(window, session_name, window_name)
            if pane:
                buf_name = "cao_paste"

                # Punch through any active copy mode first -- keys sent to a
                # pane in a mode go to the mode's key table, not the
                # application, so the submitting C-m below would be eaten
                # (#654). Harmless "not in a mode" failure when there is none.
                pane.cmd("send-keys", "-X", "cancel")

                # Load text into tmux buffer
                self.server.cmd("set-buffer", "-b", buf_name, text)

                # Paste with bracketed paste mode (-p flag).
                # This wraps the text in \x1b[200~ ... \x1b[201~ escape sequences,
                # telling the TUI "this is pasted text" so it bypasses hotkey handling.
                pane.cmd("paste-buffer", "-p", "-b", buf_name)

                time.sleep(0.3)

                # Re-cancel before the submitting C-m for the same reason as
                # send_keys: a wheel scroll during the 0.3s sleep would put
                # the pane back in copy mode and eat the submission (#654).
                pane.cmd("send-keys", "-X", "cancel")

                # Send Enter to submit the pasted text
                pane.send_keys("C-m", enter=False)

                # Clean up the paste buffer
                try:
                    self.server.cmd("delete-buffer", "-b", buf_name)
                except Exception:
                    pass

                logger.debug(f"Sent text via paste to {session_name}:{window_name}")
        except Exception as e:
            logger.error(f"Failed to send text via paste to {session_name}:{window_name}: {e}")
            raise

    def send_special_key(self, session_name: str, window_name: str, key: str) -> None:
        """Send a tmux special key sequence (e.g., C-d, C-c) to a window.

        Unlike send_keys(), this sends the key as a tmux key name (not literal text)
        and does not append a carriage return. Used for control signals like Ctrl+D (EOF).

        Args:
            session_name: Name of tmux session
            window_name: Name of window in session
            key: Tmux key name (e.g., "C-d", "C-c", "Escape")
        """
        try:
            logger.info(f"send_special_key: {session_name}:{window_name} - key: {key}")

            session = self._find_session(session_name)
            if not session:
                raise ValueError(f"Session '{session_name}' not found")

            window = self._find_window(session, session_name, window_name)
            if not window:
                raise ValueError(f"Window '{window_name}' not found in session '{session_name}'")

            pane = self._find_active_pane(window, session_name, window_name)
            if pane:
                # Same copy-mode guard as the paste paths (#654): a control
                # key like C-c sent into an active mode is consumed by the
                # mode's key table and never reaches the application.
                pane.cmd("send-keys", "-X", "cancel")
                pane.send_keys(key, enter=False)
                logger.debug(f"Sent special key to {session_name}:{window_name}")
        except Exception as e:
            logger.error(f"Failed to send special key to {session_name}:{window_name}: {e}")
            raise

    def get_history(
        self,
        session_name: str,
        window_name: str,
        tail_lines: Optional[int] = None,
        strip_escapes: bool = False,
        full_history: bool = False,
        visible_only: bool = False,
    ) -> str:
        """Get window history.

        Args:
            session_name: Name of tmux session
            window_name: Name of window in session
            tail_lines: Number of lines to capture from end (default: TMUX_HISTORY_LINES).
                NOTE this maps to capture-pane ``-S -N``, which starts N lines of
                HISTORY above the viewport and then includes the viewport — it is
                "viewport plus N lines of scrollback", not a viewport bounded to N rows.
            strip_escapes: If True, capture plain text without ANSI escape sequences
            full_history: If True, capture entire scrollback buffer (overrides tail_lines)
            visible_only: If True, capture exactly the currently rendered viewport
                (``-S 0``) and nothing from scrollback (overrides tail_lines/full_history)

        Raises:
            ValueError: The session or window is genuinely gone.
            TmuxLookupError: The tmux listing could not be parsed, so we cannot
                say whether it is gone. This is the hot path for the parse race
                — the FIFO pipe-liveness watchdog probes panes through here on
                every tick, from every concurrent CAO session. The distinction
                matters: the watchdog must not conclude a worker's pane has
                stopped producing output (and a supervisor must not conclude
                the worker finished) because a listing failed to parse.
        """
        try:
            session = self._find_session(session_name)
            if not session:
                raise ValueError(f"Session '{session_name}' not found")

            window = self._find_window(session, session_name, window_name)
            if not window:
                raise ValueError(f"Window '{window_name}' not found in session '{session_name}'")

            # Use cmd to run capture-pane with -e (escape sequences) and -p (print) flags
            pane = self._find_first_pane(window, session_name, window_name)
            if visible_only:
                # "-S 0" starts at the first line of the visible pane (default -E
                # already ends at its last line): the rendered viewport, no scrollback.
                flags = ["-p", "-S", "0"]
            elif full_history:
                # "-S -" captures from the start of the scrollback buffer
                flags = ["-p", "-S", "-"]
            else:
                lines = tail_lines if tail_lines is not None else TMUX_HISTORY_LINES
                flags = ["-p", "-S", f"-{lines}"]
            if not strip_escapes:
                flags = ["-e"] + flags
            result = pane.cmd("capture-pane", *flags)
            # Join all lines with newlines to get complete output
            return "\n".join(result.stdout) if result.stdout else ""
        except Exception as e:
            logger.error(f"Failed to get history from {session_name}:{window_name}: {e}")
            raise

    def list_sessions(self) -> List[Dict[str, str]]:
        """List all tmux sessions.

        Raises:
            TmuxLookupError: The listing could not be parsed. Deliberately NOT
                degraded to an empty list — "we could not read the tmux server"
                must never look like "there are no sessions".
        """

        def read() -> List[Dict[str, str]]:
            sessions: List[Dict[str, str]] = []
            for session in self.server.sessions:
                # Check if session has attached clients
                is_attached = len(getattr(session, "attached_sessions", [])) > 0

                session_name = session.name if session.name is not None else ""
                sessions.append(
                    {
                        "id": session_name,
                        "name": session_name,
                        "status": "active" if is_attached else "detached",
                    }
                )
            return sessions

        try:
            return self._read_listing("list-sessions", read)
        except TmuxLookupError:
            raise
        except Exception as e:
            logger.error(f"Failed to list sessions: {e}")
            return []

    def get_session_windows(self, session_name: str) -> List[Dict[str, str]]:
        """Get all windows in a session.

        Returns an empty list when the session is genuinely absent.

        Raises:
            TmuxLookupError: The listing could not be parsed — not the same
                thing as a session with no windows.
        """
        try:
            session = self._find_session(session_name)
            if not session:
                return []

            def read() -> List[Dict[str, str]]:
                windows: List[Dict[str, str]] = []
                for window in session.windows:
                    window_name = window.name if window.name is not None else ""
                    windows.append({"name": window_name, "index": str(window.index)})
                return windows

            return self._read_listing(f"list-windows for session '{session_name}'", read)
        except TmuxLookupError:
            raise
        except Exception as e:
            logger.error(f"Failed to get windows for session {session_name}: {e}")
            return []

    def kill_session(self, session_name: str) -> bool:
        """Kill tmux session, returning True only once it is CONFIRMED gone.

        Dispatching the kill is not the same as the session being gone: tmux
        reaps asynchronously, so ``session.kill()`` can return while the session
        is still listed. Session teardown treats True as proof the session is
        gone and only then drops the matching registry rows, so an optimistic
        True is exactly what lets tmux and the registry diverge (#498). Hence the
        bounded verification poll below.

        Returns False for BOTH "no such session" and "kill dispatched but not
        confirmed gone within the verify bound"; callers that need to tell those
        apart re-check existence themselves (see ``services/session_service.py``).

        A listing parse failure does NOT return False here: "we could not look"
        is not "nothing to kill", and swallowing it is exactly how a failed
        launch leaves a live session that blocks the retry. The kill is retried
        through the parse-free tmux CLI instead — and then verified like any
        other, so the fallback cannot report an unconfirmed kill as success.
        """
        try:
            session = self._find_session(session_name)
            if session is None:
                return False
            self._read_listing(f"kill-session '{session_name}'", session.kill)
        except TmuxLookupError as e:
            logger.warning(
                f"tmux listing failed while killing session {session_name}: {e} — "
                "falling back to the tmux CLI"
            )
            if not self._kill_via_cli(session_name):
                return False
        except Exception as e:
            logger.error(f"Failed to kill session {session_name}: {e}")
            return False

        return self._confirm_session_gone(session_name)

    def _confirm_session_gone(self, session_name: str) -> bool:
        """Poll, bounded, until tmux confirms ``session_name`` is gone.

        Confirmation uses ``session_exists_strict``, not ``session_exists``: a
        transient tmux/socket error during the poll must NOT be misread as
        "session gone". The strict check raises ``TmuxLookupError`` on an
        unanswerable lookup, which returns False here — so a kill is never
        reported as confirmed while the session may still be alive.

        Returns True only on a CONFIRMED absence.
        """
        start = time.monotonic()
        deadline = start + self._KILL_SESSION_VERIFY_TIMEOUT_SECONDS
        while True:
            try:
                still_here = self.session_exists_strict(session_name)
            except TmuxLookupError as e:
                logger.error(
                    f"Could not confirm tmux session {session_name} is gone after "
                    f"kill_session: {e}"
                )
                return False
            if not still_here:
                logger.info(f"Killed tmux session: {session_name}")
                return True
            if time.monotonic() >= deadline:
                # Elapsed time helps diagnose a false negative: on a heavily
                # loaded host tmux can take longer than the bound to reap a
                # session that kill() did dispatch, so a caller re-run will
                # typically then see it gone.
                elapsed = time.monotonic() - start
                logger.error(
                    f"Tmux session {session_name} still exists {elapsed:.2f}s after kill_session"
                )
                return False
            time.sleep(self._KILL_SESSION_VERIFY_INTERVAL_SECONDS)

    def kill_window(self, session_name: str, window_name: str) -> bool:
        """Kill a specific tmux window within a session.

        Like ``kill_session``, a listing parse failure falls back to the
        parse-free tmux CLI rather than reporting "nothing to kill".
        """
        try:
            session = self._find_session(session_name)
            if not session:
                return False
            window = self._find_window(session, session_name, window_name)
            if window is None:
                return False
            self._read_listing(f"kill-window '{session_name}:{window_name}'", window.kill)
            logger.info(f"Killed tmux window: {session_name}:{window_name}")
            return True
        except TmuxLookupError as e:
            logger.warning(
                f"tmux listing failed while killing window {session_name}:{window_name}: {e} — "
                "falling back to the tmux CLI"
            )
            return self._kill_via_cli(session_name, window_name)
        except Exception as e:
            logger.error(f"Failed to kill window {session_name}:{window_name}: {e}")
            return False

    def session_exists(self, session_name: str) -> bool:
        """Check if session exists.

        A listing parse failure must not answer False: callers use this both to
        decide a session is gone AND to guard against creating a duplicate, so
        a wrong False is the worst possible answer. On a parse failure we ask
        tmux directly (``has-session``), which needs no listing parse.

        Still LENIENT about everything else: any other lookup error collapses to
        False ("assume absent"). Fine for best-effort callers (status/UI, the
        duplicate-name guard), but NOT for teardown confirmation, where a
        transient socket error must not read as "gone" — use
        ``session_exists_strict`` there (#498).

        Raises:
            TmuxLookupError: The listing could not be parsed AND the direct
                probe could not answer either — genuinely unknown.
        """
        try:
            return self._find_session(session_name) is not None
        except TmuxLookupError:
            logger.warning(
                "tmux listing failed for session %s — probing with `tmux has-session`",
                session_name,
            )
            exists = self._has_session_via_cli(session_name)
            if exists is None:
                raise
            return exists
        except Exception:
            return False

    def session_exists_strict(self, session_name: str) -> bool:
        """Check if a session exists, RAISING when the lookup cannot be answered.

        Unlike ``session_exists``, this never collapses a query error into
        "absent": a live session returns True, a confirmed absence returns False,
        and an inability to tell raises ``TmuxLookupError`` — the only exception
        type this method lets out. Teardown depends on that third case existing —
        it deletes registry rows only once tmux is provably gone, so "couldn't
        tell" reaching it as False is precisely how rows get dropped from under a
        live session (#498).

        Why this does NOT go through ``self.server.sessions``: libtmux's
        ``Server.sessions`` property wraps its ``list-sessions`` fetch in a bare
        ``try/except Exception: pass`` and returns whatever it collected. A
        transient socket or tmux failure therefore yields an EMPTY QueryList,
        indistinguishable from a server with no sessions, and the lookup on it
        reports a clean absence. Any strict check built on that property fails
        OPEN by construction, no matter how its own exceptions are handled. So we
        issue the query ourselves through ``Server.cmd`` (which runs the tmux
        subprocess and exposes ``returncode``/``stderr`` without swallowing
        anything) and classify the outcome.

        **False is only ever returned on positive evidence of absence:**

        * exit 0 and the name is not in the list — the list is authoritative;
          membership is an exact string match against ``#{session_name}``, with no
          tmux target-syntax parsing involved, so no prefix or ``=``/``:`` quoting
          subtleties. Matches the old ``sessions.get(session_name=...)``.
        * the kernel refused the connection to the socket path (ECONNREFUSED,
          ``_NO_SERVER_STDERR_MARKERS``) — the path resolved and nothing is
          listening, so there is no server and therefore no session.
        * the socket path does NOT exist (ENOENT, ``_SOCKET_MISSING_STDERR_MARKERS``)
          **and** ``_tmux_server_liveness`` finds no tmux server bound to it. The
          extra check is load-bearing: a server keeps serving its socket after the
          path is unlinked (tmp-cleaners do this; ``kill -USR1 <pid>`` recovers),
          and that produces stderr byte-identical to a socket that never existed.
          Concluding absence from the message alone declared live sessions dead —
          the fail-open this check exists to prevent.

        Everything else raises: permission denied, a path that cannot be resolved
        at all (ENOTDIR, ENAMETOOLONG), an unrecognised message, a nonzero exit
        with no message, a missing socket whose server liveness cannot be
        established, or any unexpected exception from the tmux call itself.

        **What this does NOT guarantee.** The check reaches the server only
        through its socket PATH, so it cannot see a server the path no longer
        leads to:

        * If the path is unlinked and then re-created — by a second tmux server,
          or by an unrelated file or directory — ``list-sessions`` answers about
          the NEW object and reports the original server's sessions absent. (On
          tmux 3.2a a plain file at the path even yields ECONNREFUSED, i.e. the
          confirmed-absence branch above.) No check built on the socket path can
          detect that; it needs a server identity CAO does not record.
        * ``/proc/net/unix`` is per network namespace, so a server holding the
          unlinked socket from another netns reads as absent (it is also
          unreachable from here). The lsof detector has the analogous blind spot:
          unprivileged lsof cannot inspect other users' processes, so a socket
          bound by ANOTHER user's tmux server can read as absent. CAO's own
          server always runs as the same user.
        * Where no socket table can be read at all the liveness probe falls back
          to the process table and answers UNKNOWN for any tmux process it cannot
          attribute to the path. That fails CLOSED, but it means an unrelated
          tmux server can block reconciliation of a dead server's rows on such a
          host. This is now the rare case (no ``/proc/net/unix`` AND no usable
          ``lsof``); in fixup 5 it was every non-Linux host, which blocked
          teardown there permanently.
        * The stderr tables are tmux-version-dependent (see the comment on them).
          Wording from a build we do not recognise lands in the raise branch: a
          loud, non-destructive, re-runnable teardown FAILURE rather than a silent
          false success.

        Behaviour above measured on private sockets against tmux 3.2a / libtmux
        0.51.0 on Linux 6.1 (this repo's dev environment) with BOTH socket-table
        detectors, and, for the stderr wording, tmux 3.7b in fixup 4. The macOS
        lsof behaviour is the reviewer's measurement on macOS; no other BSD has
        been exercised.
        """
        try:
            return self._classify_session_lookup(session_name)
        except TmuxLookupError:
            raise
        except Exception as exc:
            # The tmux call itself can fail in ways that are not about the
            # session (no tmux binary, OSError spawning it). Callers handle
            # exactly one type, so translate rather than leak: both call sites
            # already turn TmuxLookupError into the fail-CLOSED outcome.
            raise TmuxLookupError(
                f"could not determine whether tmux session '{session_name}' exists: "
                f"the lookup itself failed with {type(exc).__name__}: {exc}"
            ) from exc

    def _classify_session_lookup(self, session_name: str) -> bool:
        """``session_exists_strict`` without the exception normalisation."""
        result = self.server.cmd("list-sessions", "-F", "#{session_name}")

        if result.returncode == 0:
            return session_name in result.stdout

        stderr_lines = [str(line) for line in result.stderr]
        stderr = " ".join(stderr_lines)
        lowered = stderr.lower()

        if any(marker in lowered for marker in _NO_SERVER_STDERR_MARKERS):
            return False

        if any(marker in lowered for marker in _SOCKET_MISSING_STDERR_MARKERS):
            socket_path = _connect_error_socket_path(stderr_lines)
            if socket_path is not None:
                liveness = _tmux_server_liveness(socket_path)
                if liveness == _SERVER_ABSENT:
                    return False
                verdict = (
                    "a tmux server is still bound to it"
                    if liveness == _SERVER_ALIVE
                    else "whether a tmux server is still bound to it cannot be "
                    "determined on this host"
                )
                raise TmuxLookupError(
                    f"could not determine whether tmux session '{session_name}' "
                    f"exists: the tmux socket {socket_path} is gone from the "
                    f"filesystem and {verdict}, so the session may be alive. A "
                    "tmp-cleaner unlinking the socket does this; "
                    "`kill -USR1 <server-pid>` makes the server recreate it."
                )

        raise TmuxLookupError(
            f"could not determine whether tmux session '{session_name}' exists: "
            f"list-sessions exited {result.returncode} "
            f"({stderr or 'no error output'})"
        )

    def get_pane_working_directory(self, session_name: str, window_name: str) -> Optional[str]:
        """Get the current working directory of a pane.

        Returns None when it cannot be determined — including on a listing
        parse failure. None already means "unknown" for this method (it is what
        an absent session/window returns too), so there is no "gone" claim to
        get wrong; the failure is logged rather than raised.
        """
        try:
            session = self._find_session(session_name)
            if not session:
                return None

            window = self._find_window(session, session_name, window_name)
            if not window:
                return None

            pane = self._find_active_pane(window, session_name, window_name)
            if pane:
                # Get pane_current_path from tmux
                result = pane.cmd("display-message", "-p", "#{pane_current_path}")
                if result.stdout:
                    return result.stdout[0].strip()
            return None
        except Exception as e:
            logger.error(f"Failed to get working directory for {session_name}:{window_name}: {e}")
            return None

    def get_pane_current_command(self, session_name: str, window_name: str) -> Optional[str]:
        """Get the current foreground command running in a pane.

        Returns None when it cannot be determined, including on a listing parse
        failure. Its only caller
        (``_pane_is_bracketed_paste_incompatible``) already treats None as
        "assume a real TUI", the pre-existing safe default — so there is no
        "gone" claim to get wrong here either.
        """
        try:
            session = self._find_session(session_name)
            if not session:
                return None
            window = self._find_window(session, session_name, window_name)
            if not window:
                return None
            pane = self._find_active_pane(window, session_name, window_name)
            if pane:
                result = pane.cmd("display-message", "-p", "#{pane_current_command}")
                if result.stdout:
                    return result.stdout[0].strip()
            return None
        except Exception as e:
            logger.error(f"Failed to get pane command for {session_name}:{window_name}: {e}")
            return None

    def pipe_pane(self, session_name: str, window_name: str, file_path: str) -> None:
        """Start piping pane output to file.

        Args:
            session_name: Tmux session name
            window_name: Tmux window name
            file_path: Absolute path to log file

        Raises:
            ValueError: The session or window is genuinely gone.
            TmuxLookupError: The tmux listing could not be parsed. Raised rather
                than swallowed so the pipe-liveness watchdog's re-arm is retried
                instead of being recorded as a permanent failure.
        """
        try:
            session = self._find_session(session_name)
            if not session:
                raise ValueError(f"Session '{session_name}' not found")

            window = self._find_window(session, session_name, window_name)
            if not window:
                raise ValueError(f"Window '{window_name}' not found in session '{session_name}'")

            pane = self._find_active_pane(window, session_name, window_name)
            if pane:
                pane.cmd("pipe-pane", "-o", f"cat >> {shlex.quote(str(file_path))}")
                logger.info(f"Started pipe-pane for {session_name}:{window_name} to {file_path}")
        except Exception as e:
            logger.error(f"Failed to start pipe-pane for {session_name}:{window_name}: {e}")
            raise

    def stop_pipe_pane(self, session_name: str, window_name: str) -> None:
        """Stop piping pane output.

        Args:
            session_name: Tmux session name
            window_name: Tmux window name

        Raises:
            ValueError: The session or window is genuinely gone.
            TmuxLookupError: The tmux listing could not be parsed.
        """
        try:
            session = self._find_session(session_name)
            if not session:
                raise ValueError(f"Session '{session_name}' not found")

            window = self._find_window(session, session_name, window_name)
            if not window:
                raise ValueError(f"Window '{window_name}' not found in session '{session_name}'")

            pane = self._find_active_pane(window, session_name, window_name)
            if pane:
                pane.cmd("pipe-pane")
                logger.info(f"Stopped pipe-pane for {session_name}:{window_name}")
        except Exception as e:
            logger.error(f"Failed to stop pipe-pane for {session_name}:{window_name}: {e}")
            raise


# Module-level singleton
tmux_client = TmuxClient()
