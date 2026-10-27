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
        engine.dispose()
