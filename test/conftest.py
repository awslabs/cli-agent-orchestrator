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

import atexit
import os
import pathlib
import shutil
import tempfile
import time
from typing import Any, Dict
from unittest.mock import patch

import pytest

# --- Suite-wide state isolation (cond-0464) --------------------------------
# ``constants.py`` resolves ``CAO_HOME_DIR`` — and binds the import-time
# database engine — from ``CAO_STATE_ROOT`` while it is being imported, and
# ``settings_service`` reads ``settings.json`` beneath it. Left unset, the
# whole suite reads the OPERATOR's live ``settings.json``: an undeclared,
# machine-dependent input. An operator with ``memory.enabled=false`` sees
# ~400 ``MemoryDisabledError`` failures that hosted CI — whose clean
# environment has no operator settings to read — never sees, so a local
# total was comparable to nothing. Point the knob at a per-session scratch
# directory *before anything imports cli_agent_orchestrator* (this module's
# body runs ahead of the ``pytest_plugins`` imports below and of every test
# module), so the suite runs on the documented defaults — the same clean
# environment CI provides. An explicitly exported ``CAO_STATE_ROOT`` is
# honoured: that one is a declared input.
_TEST_STATE_ROOT_MARKER = "_CAO_PYTEST_INJECTED_STATE_ROOT"
if "CAO_STATE_ROOT" not in os.environ:
    _TEST_STATE_ROOT = tempfile.mkdtemp(prefix="cao-pytest-state-")
    os.environ["CAO_STATE_ROOT"] = _TEST_STATE_ROOT
    # Marker so test/e2e/conftest.py can hand live e2e runs back the ambient
    # environment they had before this injection, without ever removing an
    # operator-exported value.
    os.environ[_TEST_STATE_ROOT_MARKER] = _TEST_STATE_ROOT
    atexit.register(shutil.rmtree, _TEST_STATE_ROOT, True)

    # A fresh state root also means a fresh, schema-less database. Before the
    # injection, tests that use the process-default engine quietly relied on
    # the operator's already-migrated live database — and a full-suite run
    # relied on an API test booting the app (and its ``init_db``) before
    # test/services ran. Initialize the schema here, the same way the server
    # lifespan does, so any subset of the suite is self-sufficient.
    from cli_agent_orchestrator.clients import database as _test_database

    _test_database.init_db()


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
pytest_plugins = (
    "test.fixtures.cao_server",
    "test.fixtures.jwt_factory",
    "test.fixtures.jwks_server",
    "test.fixtures.terminal_factory",
)


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
def _restore_process_environment():
    """Restore ``os.environ`` after every test.

    The managed-provider bridge scrubs the process-wide environment for the
    bridge's lifetime (``_scope_direct_serve_environment``) and restores it
    only when ``_serve`` returns. Several tests run that accept loop in a
    daemon thread that outlives the test, so the scrubbed environment —
    without the suite's ``CAO_STATE_ROOT`` isolation, among everything else
    — used to leak into every later test's subprocesses (cond-0464).
    monkeypatch only undoes changes it made itself; this is the backstop for
    changes no fixture owns. Snapshot at setup so a session-scoped fixture's
    deliberate change (e.g. the e2e state-root un-injection) is preserved.
    """
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


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
