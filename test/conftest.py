"""Top-level pytest configuration.

Sets process-wide env vars that disable optional v2.5 listeners so the
existing test suite (and CI) doesn't have to coordinate around real
port bindings or filesystem writes.

These knobs match how the lifespan reads them at runtime — see
``api/main.py``. Each is opt-out: the default is "feature on" in
production; tests flip them off.

Also exposes shared security fixtures (RSA keys, JWKS stub,
``AUTH0_*`` env, JWT mint helper) for tests outside ``test/security/``
that need to exercise the Auth0 paths.
"""

import fcntl
import logging
import os
import pathlib
import time
import uuid
from typing import Any, Dict
from unittest.mock import patch
from uuid import uuid4

import pytest

# Make the `mock_cli` test-fixture binary discoverable for the pytest
# session so MockCliProvider can `shlex.join(["mock_cli", ...])` without
# an absolute path. Not on PATH outside the test session — production
# code paths never reach this binary. See docs/mock-cli-provider.md.
_MOCK_CLI_BIN_DIR = pathlib.Path(__file__).parent / "providers" / "fixtures" / "bin"
if str(_MOCK_CLI_BIN_DIR) not in os.environ.get("PATH", "").split(os.pathsep):
    os.environ["PATH"] = f"{_MOCK_CLI_BIN_DIR}{os.pathsep}{os.environ.get('PATH', '')}"


# Expose the managed-subprocess fixtures (cao_server, cao_server_with_auth,
# cao_terminal) and the shared infra fixtures (jwt_factory, jwks_server,
# terminal_factory) to every test under test/ without per-conftest imports.
#
# ``test._must_run`` is the must_run assurance gate (see that module): it turns a
# skipped or deselected assurance test into a session failure in the CI job that
# owns the gate.
pytest_plugins = (
    "test._must_run",
    "test.fixtures.cao_server",
    "test.fixtures.jwt_factory",
    "test.fixtures.jwks_server",
    "test.fixtures.terminal_factory",
)


# --------------------------------------------------------------------------- #
# HOME isolation
# --------------------------------------------------------------------------- #
#
# The suite must never read or write the developer's real home. Run with the
# real HOME, it used to: Antigravity tests merged the developer's installed agent
# plugins (from the real ``~/.aws/cli-agent-orchestrator/agent-plugins``) into a
# profile and wrote the result into the real ``~/.gemini/config/mcp_config.json``;
# memory tests left ``vault-projection-*.lock`` files in the real CAO home; and
# about thirty tests failed because they saw the live fleet, installed plugins
# and Kiro agents instead of an empty home.
#
# Most of CAO's home-derived paths are module constants evaluated at import
# (``constants.CAO_HOME_DIR`` and everything built on it, ``KIRO_AGENTS_DIR``,
# ``COPILOT_AGENTS_DIR``, ``OPENCODE_CONFIG_DIR``, ``settings_service._DEFAULTS``),
# and the DB engine binds to ``CAO_HOME_DIR`` at import too. Monkeypatching each
# copy per test would miss every ``from constants import X`` binding. So HOME is
# redirected when this conftest is imported, before any other conftest or test
# module (and therefore any ``cli_agent_orchestrator`` module) is imported. The
# provider config-root variables that point inside the real home are removed so
# every root derives from the fake HOME; one already under the system temp dir is
# kept, and the Rust toolchain dirs keep pointing at the real ones. The
# home is one per session, as a CI runner's HOME is: a fresh HOME per test would make a
# subprocess re-resolve ``CAO_HOME_DIR`` differently from this process (see
# ``_hermetic_cao_env``) and leave a herdr server per test (see ``_empty_home``).
#
# Opting out, smallest scope first:
# - ``@pytest.mark.real_home``: the test runs with the real HOME and the
#   provider variables restored. Import-time constants still point at the fake
#   home; only runtime ``Path.home()`` lookups and subprocesses see the real one.
# - A session that selects e2e tests or the Kiro integration test keeps the real
#   HOME for the whole session: those drive authenticated provider CLIs, and
#   their session-scoped fixtures read provider logins under HOME before any
#   function-scoped opt-out could apply.
# - ``CAO_TEST_REAL_HOME=1`` keeps the real HOME for the whole session, and
#   ``CAO_TEST_REAL_HOME=0`` forces isolation.
#
# The decision is made when this module is imported, which pytest does before
# any other conftest and so before anything imports ``cli_agent_orchestrator``.

#: Variables that relocate a provider's or CAO's config root. Removed so each
#: root falls back to its default under the fake HOME.
HOME_ROOT_ENV_VARS = (
    "CAO_HOME_DIR",
    "CAO_AGENTS_DIR",
    "CAO_GRAPH_EXPORT_ROOT",
    "CAO_TMP_DIR",
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
    "GROK_HOME",
    "KIMI_CODE_HOME",
    "KIRO_HOME",
    "KIRO_AGENT_CONFIG_DIR",
    "MINIMAX_DATA_DIR",
    "OPENCODE_CONFIG",
    "OPENCODE_CONFIG_DIR",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
)

_REAL_HOME_ENV: Dict[str, str] = {}
_ISOLATED_HOME: Dict[str, str] = {}
#: Root variables kept at import because they already pointed at a throwaway
#: location (see ``_keeps_root``); the per-test fixture re-pins them instead of
#: removing them.
_PINNED_ROOT_ENV: Dict[str, str] = {}
#: Hands the controller's isolation to pytest-xdist workers, whose argv is not
#: the command line (see ``_decide_at_import``).
_ISOLATION_HANDOFF_VAR = "_CAO_TEST_HOME_ISOLATION"


def _needs_real_home(args, markexpr: str) -> bool:
    """True when the invocation selects tests that drive real provider logins."""
    if os.environ.get("CAO_TEST_REAL_HOME") in ("0", "1"):
        return os.environ["CAO_TEST_REAL_HOME"] == "1"
    if "e2e" in (markexpr or "").replace("not e2e", ""):
        return True
    for arg in args:
        parts = pathlib.PurePath(str(arg).split("::", 1)[0]).parts
        if "e2e" in parts or "test_kiro_cli_integration.py" in parts:
            return True
    return False


#: pytest options whose value is a separate argv item and is not a test path.
_VALUE_OPTIONS = frozenset(
    {
        "-p",
        "-k",
        "-o",
        "-c",
        "--ignore",
        "--ignore-glob",
        "--deselect",
        "--cov",
        "--cov-report",
        "--cov-fail-under",
        "--rootdir",
        "--basetemp",
        "--confcutdir",
        "--junitxml",
        "--maxfail",
        "--durations",
        "-n",
        "--dist",
    }
)


def _argv_selection(argv):
    """The test paths and ``-m`` expression on the command line, best effort.

    Read here because this module is imported before any other conftest, and so
    before anything imports ``cli_agent_orchestrator``; ``pytest_configure``
    re-checks the decision against pytest's own parse of the same options.
    """
    args, markexpr, skip_next = [], "", False
    for i, arg in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if arg == "-m" and i + 1 < len(argv):
            markexpr, skip_next = argv[i + 1], True
        elif arg.startswith("-m") and not arg.startswith("--"):
            markexpr = arg[2:].lstrip("=")
        elif arg in _VALUE_OPTIONS:
            skip_next = True
        elif not arg.startswith("-"):
            args.append(arg)
    return args, markexpr


def _isolate_home() -> None:
    import atexit
    import shutil
    import sys
    import tempfile

    for name in ("HOME", *HOME_ROOT_ENV_VARS):
        if name in os.environ:
            _REAL_HOME_ENV[name] = os.environ[name]
    if "cli_agent_orchestrator.constants" in sys.modules:
        # Recorded, then reported by pytest_configure as a usage error.
        _ISOLATED_HOME["too_late"] = "1"
        return
    real_home = os.path.realpath(os.path.expanduser("~"))
    home = os.path.realpath(tempfile.mkdtemp(prefix="cao-test-home-"))
    _ISOLATED_HOME["path"] = home
    os.environ["HOME"] = home
    tmp_roots = _throwaway_roots()
    for name in HOME_ROOT_ENV_VARS:
        value = os.environ.get(name, "")
        if _keeps_root(value, real_home, tmp_roots):
            _PINNED_ROOT_ENV[name] = value
        else:
            os.environ.pop(name, None)
    # The Rust toolchain is a build tool, not CAO or provider state: keep using
    # the developer's rustup toolchains and cargo cache, or test_no_ffi_guard's
    # ``cargo metadata`` finds no default toolchain and would otherwise have to
    # download the whole crate index into the fake home on every session. cargo
    # may update its own ``~/.cargo/.global-cache`` while that test runs.
    for name, default in (("RUSTUP_HOME", ".rustup"), ("CARGO_HOME", ".cargo")):
        path = os.path.join(real_home, default)
        if name not in os.environ and os.path.isdir(path):
            os.environ[name] = path
    import json

    os.environ[_ISOLATION_HANDOFF_VAR] = json.dumps(
        {"home": home, "real": _REAL_HOME_ENV, "pinned": _PINNED_ROOT_ENV}
    )
    atexit.register(shutil.rmtree, home, True)


def _throwaway_roots() -> tuple:
    """The system temp directories, where a deliberate throwaway root lives."""
    import tempfile

    roots = {os.path.realpath(tempfile.gettempdir())}
    if os.path.isdir("/tmp"):
        roots.add(os.path.realpath("/tmp"))
    return tuple(sorted(roots))


def _keeps_root(value: str, real_home: str, tmp_roots: tuple) -> bool:
    """Keep a root variable only when it already points at a throwaway location.

    A root under the system temp dir is a deliberate relocation:
    ``CAO_HOME_DIR=$(mktemp -d) uv run pytest ...``, or a nested pytest that a
    test starts with its own ``CAO_HOME_DIR`` (whose "real" home is the parent
    session's fake home, itself under the temp dir). Anything else is removed:
    being outside the real home is not the same as being disposable, and a live
    ``CAO_HOME_DIR=/Volumes/...`` must not be written to. A temp dir that is
    inside the real home does not count.
    """
    value = (value or "").strip()
    if not value:
        return False
    path = os.path.expanduser(value)
    if _is_within(path, real_home):
        return False
    return any(_is_within(path, root) for root in tmp_roots)


def _is_within(path: str, root: str) -> bool:
    resolved = os.path.realpath(path)
    return resolved == root or resolved.startswith(root.rstrip(os.sep) + os.sep)


def _adopt_controller_isolation() -> None:
    """In a pytest-xdist worker, reuse the controller's decision and fake home.

    A worker's argv is xdist's own bootstrap, not the command line, so it cannot
    re-decide. It inherited the controller's environment: when the controller
    isolated, HOME is already its fake home and the handoff variable says so;
    when the controller kept the real HOME (e2e), there is no handoff and the
    worker keeps the inherited HOME too.
    """
    import json

    raw = os.environ.get(_ISOLATION_HANDOFF_VAR)
    if not raw:
        return
    state = json.loads(raw)
    _ISOLATED_HOME["path"] = state["home"]
    _REAL_HOME_ENV.update(state["real"])
    _PINNED_ROOT_ENV.update(state["pinned"])


# --------------------------------------------------------------------------- #
# tmux isolation
# --------------------------------------------------------------------------- #
#
# The suite must never reach a tmux server it did not start. CAO's own agents run
# inside tmux, so a contributor's pytest is often started from a tmux pane, and
# then ``TMUX`` names that pane's server. tmux gives ``TMUX`` priority over
# ``TMUX_TMPDIR``, so a test that only sets ``TMUX_TMPDIR`` still talks to the
# real server, and one bare ``tmux kill-server`` ends every session on it. Clearing
# ``TMUX`` alone is not enough either: the command then reaches the default socket
# under the real ``TMUX_TMPDIR``, which is usually the same server.
#
# So, with HOME isolation and decided by the same rule, this conftest removes
# ``TMUX`` and ``TMUX_PANE`` at import and points ``TMUX_TMPDIR`` at a fresh
# directory. Every default-socket and ``-L`` server a test starts lives there, and
# the directory, plus any server a test left running in it, is removed at exit.
# The directory is short and under /tmp on purpose: macOS caps a unix socket path
# at about 104 bytes, and pytest's ``tmp_path`` is often longer than that.
#
# One directory per process. Each pytest-xdist worker makes its own, so workers
# never share a default server. Re-pinned before and after every test
# (``_isolated_tmux``), so a ``TMUX`` that a test sets without monkeypatch is gone
# before the next test and before any session fixture's teardown sees it. A test
# may still set ``TMUX_TMPDIR`` itself after fixture setup.
#
# A session that keeps the real HOME (E2E and the Kiro integration test, see
# above) keeps the real tmux environment too, because those tests read the panes
# of a ``cao-server`` you started yourself.

#: Variables that name the tmux server a client talks to. ``TMUX_TMPDIR`` is
#: replaced rather than removed.
TMUX_SERVER_ENV_VARS = ("TMUX", "TMUX_PANE")

_ISOLATED_TMUX: Dict[str, str] = {}


def _isolate_tmux() -> None:
    import atexit
    import tempfile

    for name in TMUX_SERVER_ENV_VARS:
        os.environ.pop(name, None)
    tmpdir = tempfile.mkdtemp(prefix="cao-tmux-", dir="/tmp" if os.path.isdir("/tmp") else None)
    _ISOLATED_TMUX["tmpdir"] = tmpdir
    os.environ["TMUX_TMPDIR"] = tmpdir
    atexit.register(_remove_tmux_dir, tmpdir)


def _remove_tmux_dir(tmpdir: str) -> None:
    """Stop any server a test left in the private dir, by explicit socket, then delete it."""
    import shutil
    import stat
    import subprocess

    try:
        env = {k: v for k, v in os.environ.items() if k not in TMUX_SERVER_ENV_VARS}
        for root, _dirs, files in os.walk(tmpdir):
            for name in files:
                path = os.path.join(root, name)
                try:
                    if not stat.S_ISSOCK(os.lstat(path).st_mode):
                        continue
                    subprocess.run(
                        ["tmux", "-S", path, "kill-server"],
                        env=env,
                        capture_output=True,
                        timeout=5,
                        check=False,
                    )
                except Exception:
                    # Best effort at interpreter exit, where even starting a
                    # subprocess can fail; the directory is removed regardless.
                    pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _decide_at_import() -> None:
    import sys

    if os.environ.get("PYTEST_XDIST_WORKER"):
        _adopt_controller_isolation()
        if "path" in _ISOLATED_HOME:
            _isolate_tmux()
        return
    os.environ.pop(_ISOLATION_HANDOFF_VAR, None)
    args, markexpr = _argv_selection(sys.argv[1:])
    if not _needs_real_home(args, markexpr):
        _isolate_home()
        _isolate_tmux()


_decide_at_import()


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_home: run with the developer's real HOME and provider config roots "
        "(see test/conftest.py, HOME isolation)",
    )
    if _ISOLATED_HOME.get("too_late"):
        raise pytest.UsageError(
            "cli_agent_orchestrator was imported before test/conftest.py could isolate "
            "HOME, so its home-derived paths would point at the real home. Set "
            "CAO_TEST_REAL_HOME=1 to run against the real home deliberately."
        )
    isolated = "path" in _ISOLATED_HOME
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return
    if isolated == _needs_real_home(config.args, config.option.markexpr or ""):
        raise pytest.UsageError(
            "test/conftest.py could not tell from the command line whether this session "
            "needs the real HOME (e2e and Kiro integration tests do). Set "
            "CAO_TEST_REAL_HOME=1 to use the real home, or CAO_TEST_REAL_HOME=0 to isolate."
        )


@pytest.fixture(autouse=True)
def _isolated_home(request, monkeypatch):
    """Pin HOME to the session's fake home, or restore the real one on opt-out.

    Re-pinned per test so that a test which changed HOME or a provider root
    variable without monkeypatch cannot leak it into the next test.
    """
    if "path" not in _ISOLATED_HOME:
        yield None
        return
    if request.node.get_closest_marker("real_home") is not None:
        for name in ("HOME", *HOME_ROOT_ENV_VARS):
            if name in _REAL_HOME_ENV:
                monkeypatch.setenv(name, _REAL_HOME_ENV[name])
        yield pathlib.Path(_REAL_HOME_ENV.get("HOME", _ISOLATED_HOME["path"]))
        return
    monkeypatch.setenv("HOME", _ISOLATED_HOME["path"])
    for name in HOME_ROOT_ENV_VARS:
        if name in _PINNED_ROOT_ENV:
            monkeypatch.setenv(name, _PINNED_ROOT_ENV[name])
        else:
            monkeypatch.delenv(name, raising=False)
    yield pathlib.Path(_ISOLATED_HOME["path"])


@pytest.fixture(autouse=True)
def _isolated_tmux():
    """Keep ``TMUX`` and ``TMUX_PANE`` unset and ``TMUX_TMPDIR`` on the private dir.

    Applied before and after each test with plain ``os.environ`` writes, not
    monkeypatch: monkeypatch would restore a value that leaked in before setup,
    so it would linger until the next test. See "tmux isolation" above.
    ``real_home`` does not opt a test out of this.
    """
    if "tmpdir" not in _ISOLATED_TMUX:
        yield None
        return
    _pin_tmux_env()
    yield pathlib.Path(_ISOLATED_TMUX["tmpdir"])
    _pin_tmux_env()


def _pin_tmux_env() -> None:
    for name in TMUX_SERVER_ENV_VARS:
        os.environ.pop(name, None)
    os.environ["TMUX_TMPDIR"] = _ISOLATED_TMUX["tmpdir"]


_DECISION_HOME_UNSTATABLE = object()
_DECISION_HOME_SNAPSHOT = pytest.StashKey[dict[pathlib.Path, object]]()
logger = logging.getLogger(__name__)


def _decision_home_file_state(path: pathlib.Path) -> object:
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning("Could not stat %s; skipping decision home guard comparison", path.name)
        return _DECISION_HOME_UNSTATABLE
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


def pytest_sessionstart(session):
    # Resolve the startup home before any per-test redirects; inspect metadata only.
    from cli_agent_orchestrator.constants import CAO_HOME_DIR

    session.stash[_DECISION_HOME_SNAPSHOT] = {
        CAO_HOME_DIR / name: _decision_home_file_state(CAO_HOME_DIR / name)
        for name in ("decisions.halt", "decisions-egress.lock")
    }


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    before = session.stash.get(_DECISION_HOME_SNAPSHOT, {})
    changed = []
    for path, state in before.items():
        if state is _DECISION_HOME_UNSTATABLE:
            continue
        current = _decision_home_file_state(path)
        if current is not _DECISION_HOME_UNSTATABLE and current != state:
            changed.append(path.name)
    if changed:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_sep(
                "=", "Decision state changed under startup CAO home: " + ", ".join(changed)
            )


# The cross-process mutex for the one-time schema build below. Lives beside the
# database it guards, so it is scoped to exactly what it protects: one
# ``CAO_HOME_DIR``. Named with a leading dot and a ``.lock`` suffix to read as
# harness state, not as a database file, to anyone who lists the directory.
SCHEMA_INIT_LOCK_NAME = ".test-schema-init.lock"


def ensure_cao_home_schema() -> None:
    """Build the configured home's ORM schema if, and only if, it has none.

    Factored out of the fixture so the concurrency guard in
    ``test_isolated_home_has_schema.py`` can run *this* function in several
    processes at once rather than a copy of it -- a copy would keep passing
    after someone dropped the lock from the real body.

    The check and ``init_db()`` both happen under an exclusive ``flock`` on
    ``$CAO_HOME_DIR/db/.test-schema-init.lock``, and THE CHECK IS INSIDE THE
    LOCK, not before it. Each pytest-xdist worker is its own process with its
    own session, so each one runs this. Read the table names outside the lock
    and every worker on a home with no schema sees no tables, every worker
    calls ``init_db()``, and ``Base.metadata.create_all()`` -- which checks for
    each table and then creates it -- lets the losers fail with
    ``table session_incarnations already exists``. Because the fixture that
    calls this is session-scoped and autouse, that is a setup error for every
    test on every losing worker, not just the database tests:
    ``pytest -n 4 test/services/vault`` on a fresh home reported 336-448 errors
    (haofeif, #1049 review at 10587987). ``DEVELOPMENT.md`` documents
    ``uv run pytest -n auto``, and a machine that has never run CAO reaches the
    same state with the default home.

    ``flock`` rather than a marker file for the same reasons as
    ``utils/server_lock.py``: the kernel drops it however the holder dies, so
    there is no stale-lock recovery path, and it needs no PID bookkeeping. It
    blocks rather than failing fast (``LOCK_EX``, no ``LOCK_NB``) because the
    waiter's whole purpose is to observe the winner's finished schema; the wait
    is one ``init_db()`` long and happens at most once per home.
    """
    from sqlalchemy import inspect as sa_inspect

    from cli_agent_orchestrator.clients import database
    from cli_agent_orchestrator.constants import DB_DIR

    # A never-initialized home has no db/ yet. ``database``'s import-time
    # ``_ensure_db_dir()`` has in fact just created it, but the lock must not
    # depend on an import side effect staying where it is.
    DB_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    # 0o600 and O_CLOEXEC, as ``acquire_server_lock`` opens server.lock: DB_DIR is
    # owner-only by design (``database._ensure_db_dir``) and a plain ``open(...,
    # "w")`` would drop a 0o644 file into it under the usual umask. O_CLOEXEC so
    # a subprocess a test spawns cannot inherit the descriptor and hold the lock
    # past the session. Never unlinked: unlinking a lock file while another
    # process is about to open it leaves two holders locking two different inodes.
    fd = os.open(DB_DIR / SCHEMA_INIT_LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        present = set(sa_inspect(database.engine).get_table_names())
        if present & set(database.Base.metadata.tables):
            return
        database.init_db()
    finally:
        os.close(fd)


@pytest.fixture(scope="session", autouse=True)
def _cao_home_schema() -> None:
    """Make the configured home's DB schema exist before any test runs.

    ``clients/database.py`` binds ``SessionLocal`` to a module-global engine at
    import, but only ``init_db()`` creates the tables, and ``_hermetic_cao_env``
    below deliberately leaves ``CAO_HOME_DIR`` pointing at the real configured
    home. So an isolated run against a throwaway home --
    ``CAO_HOME_DIR=$(mktemp -d) uv run pytest test/services/vault`` -- had tables
    only if some earlier test in the same process happened to call ``init_db()``
    itself. That made a test's database depend on which tests ran before it: the
    vault suite reported 50 failures alone and 0 inside a warm home, and
    ``test_session_service.py`` 29 and 0, all of them
    ``no such table: project_aliases`` surfacing through the fail-closed path as
    ``ProjectAliasLookupUnavailableError`` / ``VaultConfigUnavailableError`` --
    false security refusals on a harness artefact.

    Conditional on the *whole* schema being absent, not on one table. An
    un-initialized home gets the full ``init_db()``; a home that already holds
    any CAO table is left exactly as it is. Calling ``init_db()`` unconditionally
    would re-run all of its migrators -- several of which rebuild tables -- over
    a developer's live home on every pytest invocation, and would silently
    repair a half-migrated home mid-session, masking the migration gaps that
    ``test/clients/test_*_migration.py`` exists to catch. A stale home should
    still fail loudly and be fixed with ``cao init``.

    Runs once per pytest session, which under ``pytest -n`` means once in each
    xdist worker process. ``ensure_cao_home_schema()`` takes a cross-process
    lock so that is safe; see its docstring.
    """
    ensure_cao_home_schema()


_AUTH_TEST_DOMAIN = "test.local"
_AUTH_TEST_AUDIENCE = "cao://test"


@pytest.fixture
def rsa_keys():
    """Generate a fresh RSA-2048 keypair for the test.

    Same shape as the local fixture in ``test/security/test_auth.py``
    (which still wins locally — pytest fixture resolution prefers the
    closest definition). Lifted here so sibling test modules can mint
    their own tokens without duplicating the RSA boilerplate.
    """
    from authlib.jose import JsonWebKey
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_jwk = JsonWebKey.import_key(public_pem, {"kty": "RSA", "use": "sig", "kid": "test-kid"})
    return private_pem, public_jwk


def mint_test_token(
    private_pem: bytes,
    *,
    scopes: str = "cao:read cao:write cao:admin",
    audience: str = _AUTH_TEST_AUDIENCE,
    exp_offset: int = 300,
    iat_offset: int = 0,
) -> str:
    """Mint an RS256 JWT for tests. Mirrors test/security/test_auth.py."""
    from authlib.jose import JsonWebToken

    jwt = JsonWebToken(["RS256"])
    now = int(time.time())
    header = {"alg": "RS256", "kid": "test-kid"}
    claims: Dict[str, Any] = {
        "iss": f"https://{_AUTH_TEST_DOMAIN}/",
        "aud": audience,
        "iat": now + iat_offset,
        "exp": now + exp_offset,
        "scope": scopes,
    }
    token = jwt.encode(header, claims, private_pem)
    return token.decode("utf-8") if isinstance(token, bytes) else token


@pytest.fixture
def auth_enabled_env(monkeypatch):
    """Switch on Auth0 enforcement (AUTH0_DOMAIN + AUTH0_AUDIENCE)."""
    from cli_agent_orchestrator.security import auth as _auth_mod

    monkeypatch.setenv("AUTH0_DOMAIN", _AUTH_TEST_DOMAIN)
    monkeypatch.setenv("AUTH0_AUDIENCE", _AUTH_TEST_AUDIENCE)
    _auth_mod.reset_jwks_cache()
    yield
    _auth_mod.reset_jwks_cache()


@pytest.fixture
def mock_jwks(rsa_keys):
    """Stub the JWKS HTTP fetch with the in-process public key."""
    _, public_jwk = rsa_keys
    jwks = {"keys": [public_jwk.as_dict()]}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return jwks

    with patch("cli_agent_orchestrator.security.auth.requests.get", return_value=_Resp()):
        yield


@pytest.fixture(autouse=True)
def _no_llm_compile_in_tests(monkeypatch):
    """Default memory wiki compilation to append mode for every test.

    The production default is "llm", which drives whichever coding-agent CLI
    (claude / codex / kiro-cli) is installed on the developer's machine — each
    invocation cold-starts for tens of seconds and would make the suite both
    slow and non-hermetic. Tests that exercise the LLM path override this env
    var themselves or stub the ``wiki_compiler`` seams.
    """
    monkeypatch.setenv("CAO_MEMORY_COMPILE_MODE", "append")
    # Append mode does not cover every LLM caller: wiki_lint's contradiction
    # pass and a test that switches to llm mode still ask _build_llm_client,
    # which picks the first coding-agent CLI on PATH and runs it headless (a
    # real, possibly billable call). Default it to "no CLI" everywhere it is
    # bound; tests that exercise the LLM path install their own stubs.
    from cli_agent_orchestrator.services import wiki_compiler, wiki_lint

    _ORIGINAL_LLM_CLIENT.setdefault("fn", wiki_compiler._build_llm_client)
    monkeypatch.setattr(wiki_compiler, "_build_llm_client", lambda *a, **k: None)
    monkeypatch.setattr(wiki_lint, "_build_llm_client", lambda *a, **k: None)


_ORIGINAL_LLM_CLIENT: Dict[str, Any] = {}


@pytest.fixture
def real_llm_client_selection(monkeypatch):
    """Undo the default LLM-client stub, for a test that controls PATH itself.

    Only for a test that puts its own fake CLI first on a PATH it built, so the
    real selection logic runs without reaching a real coding-agent CLI.
    """
    from cli_agent_orchestrator.services import wiki_compiler, wiki_lint

    monkeypatch.setattr(wiki_compiler, "_build_llm_client", _ORIGINAL_LLM_CLIENT["fn"])
    monkeypatch.setattr(wiki_lint, "_build_llm_client", _ORIGINAL_LLM_CLIENT["fn"])


def _drain_launch_status_writes() -> None:
    """Wait for every tmux status-event write queued so far (issue #1082).

    ``StatusMonitor`` writes status transitions on one module-level FIFO
    thread, and a caller on the event loop does not wait for its write, so a
    write queued by one test could otherwise land in a later test's database.
    A no-op submitted behind them returns once they are all done.
    """
    import sys

    status_monitor = sys.modules.get("cli_agent_orchestrator.services.status_monitor")
    writer = getattr(status_monitor, "_status_event_writer", None)
    if writer is not None:
        writer.submit(lambda: None).result(timeout=30)


@pytest.fixture(autouse=True)
def _drain_launch_status_writer():
    """No queued status-event write crosses a test boundary (issue #1082)."""
    _drain_launch_status_writes()
    yield
    _drain_launch_status_writes()


@pytest.fixture(autouse=True)
def _reset_backend_registry():
    """Prevent leaked backend singletons from crossing test boundaries (fixes #522)."""
    from cli_agent_orchestrator.backends import registry

    original = registry._backend
    registry._backend = None
    yield
    registry._backend = original


@pytest.fixture(autouse=True)
def _hermetic_cao_env(monkeypatch, tmp_path):
    """Keep tests independent of CAO runtime identity and persisted settings.

    Settings reads use a fresh per-test file while ``CAO_HOME_DIR`` stays
    unchanged, so tests see documented defaults without invalidating assertions
    about the default home layout. Runtime env vars are removed before each test;
    tests can still set them explicitly after fixture setup. In particular,
    stripping ``CAO_TERMINAL_ID`` is load-bearing for vault-recall exclusion
    tests as well as sender-id defaults.
    """
    from cli_agent_orchestrator.decisions import egress
    from cli_agent_orchestrator.decisions.settings import load_settings
    from cli_agent_orchestrator.services import settings_service

    monkeypatch.setattr(egress, "HALT_FILE", tmp_path / "decisions.halt")
    monkeypatch.setattr(egress, "EGRESS_LOCK_FILE", tmp_path / "decisions-egress.lock")
    monkeypatch.setattr(egress, "_settings_loader", load_settings)
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(settings_service, "_server_settings_cache", None)
    monkeypatch.setattr(settings_service, "_server_settings_mtime_ns", -1)
    # The server lock (#406) lives beside settings.json in the real home. Every
    # in-process ``lifespan`` run would otherwise contend with a cao-server the
    # developer has running, so each test locks its own file.
    from cli_agent_orchestrator.utils import server_lock

    monkeypatch.setattr(server_lock, "SERVER_LOCK_FILE", tmp_path / "server.lock")

    # server.py defaults sender_id to "supervisor" when unset
    monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
    # server.py reads these for workflow_return context detection
    monkeypatch.delenv("CAO_WORKFLOW_RUN_ID", raising=False)
    monkeypatch.delenv("CAO_WORKFLOW_STEP_ID", raising=False)
    # cli/commands/info.py uses this for session detection
    monkeypatch.delenv("CAO_SESSION_NAME", raising=False)
    # utils/mcp_resolution.cao_server_env() reads these directly; an inherited
    # value (e.g. a developer's pane on a non-default port) would otherwise
    # leak into any test asserting an exact bundled-server `env`. Tests can
    # still set them explicitly after fixture setup.
    #
    # CAO_HOME_DIR is deliberately NOT stripped here: constants.CAO_HOME_DIR is
    # resolved once at interpreter import, before this fixture ever runs, so
    # delenv'ing the raw env var would not change it for this process -- but it
    # WOULD change it for any test that spawns a real subprocess (e.g.
    # test/utils/test_atomic_file.py's multiprocess lock test), which
    # re-resolves CAO_HOME_DIR from its own (now-stripped) inherited env and
    # disagrees with the already-imported value here. A test asserting an
    # exact forwarded CAO_HOME_DIR must still set/patch it itself (see
    # test/utils/test_mcp_resolution.py's TestServerEnv for the pattern).
    monkeypatch.delenv("CAO_API_HOST", raising=False)
    monkeypatch.delenv("CAO_API_PORT", raising=False)


@pytest.fixture(scope="session")
def _empty_home(tmp_path_factory):
    """The HOME and cwd ``hermetic_kiro_agent_store`` gives every test of a session.

    One per session, not per test: a test that reaches a real herdr backend (an
    inherited ``CAO_TERMINAL_BACKEND=herdr``, say) starts a herdr server under
    HOME, and a fresh HOME per test would leave one server behind per such test.
    """
    return tmp_path_factory.mktemp("home")


@pytest.fixture
def hermetic_kiro_agent_store(_empty_home, tmp_path_factory, monkeypatch):
    """An empty Kiro agent store, so the developer's real agents are never read (#1074).

    ``create_terminal`` refuses a Kiro launch whose agent no agent directory
    defines, and it searches ``~/.kiro/agents``, ``$KIRO_HOME/agents``, the
    ``cao install`` directory (fixed from the real home at import) and the launch
    cwd's ``.kiro/agents``. All four are empty here, so a contributor who ran
    ``cao install developer`` for Kiro sees what CI sees. HOME and the cwd are an
    empty directory shared by the session (see ``_empty_home``); a test that
    writes under ``~`` sets its own HOME. Returns the ``cao install`` directory,
    fresh and empty per test, for a test that needs an agent installed.

    Autouse for ``test/services`` (its conftest); opt in elsewhere. It moves the
    cwd, which tests that read repo files by relative path cannot take.
    """
    # A UUID-named directory under the shared basetemp, not a numbered
    # tmp_path_factory.mktemp(): mktemp scans every existing basetemp entry to
    # pick its next number, which is O(n) per call and quadratic over a run
    # that creates one of these per test (#1074).
    agents = tmp_path_factory.getbasetemp() / f"kiro-agents-{uuid4().hex}"
    agents.mkdir()
    monkeypatch.setenv("HOME", str(_empty_home))
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.setattr("cli_agent_orchestrator.services.install_service.KIRO_AGENTS_DIR", agents)
    # A create with no working_directory launches from the process cwd.
    monkeypatch.chdir(_empty_home)
    return agents


@pytest.fixture(autouse=True)
def _hermetic_ephemeral_store(monkeypatch, tmp_path_factory):
    """Keep the ephemeral store and CAO's lock dir out of the startup CAO home.

    Every in-process ``lifespan`` runs the ephemeral startup sweep (#1079), which
    deletes rowless live files and prunes aged records. Against a developer's real
    home, a test's replacement ``SessionLocal`` makes a running agent's files look
    rowless. ``atomic_file`` locks would also leave permanent lock files there. The
    directory sits outside ``tmp_path`` because some tests assert its exact contents.
    Tests that build their own store still patch these paths themselves.
    """
    from cli_agent_orchestrator.services import ephemeral_service
    from cli_agent_orchestrator.utils import agent_profiles, atomic_file

    home = tmp_path_factory.getbasetemp() / "cao-homes" / uuid.uuid4().hex
    home.mkdir(parents=True)
    monkeypatch.setattr(ephemeral_service, "EPHEMERAL_DIR", home / "ephemeral")
    monkeypatch.setattr(agent_profiles, "EPHEMERAL_LIVE_DIR", home / "ephemeral" / "live")
    monkeypatch.setattr(atomic_file, "LOCK_DIR", home / "locks")


@pytest.fixture
def isolated_memory_db(tmp_path, monkeypatch):
    """Route default memory sessions to an initialized per-test SQLite database."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from cli_agent_orchestrator.clients import database

    engine = create_engine(
        f"sqlite:///{tmp_path / 'memory-metadata.db'}",
        connect_args={"check_same_thread": False},
    )
    database.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(
        database,
        "SessionLocal",
        sessionmaker(autocommit=False, autoflush=False, bind=engine),
    )
    try:
        yield engine
    finally:
        # Land this test's queued status-event writes in its own database.
        _drain_launch_status_writes()
        engine.dispose()
