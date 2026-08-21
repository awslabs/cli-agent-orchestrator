"""Tests for the HerdrInboxService registry.

The registry stores its state in a module-level global
(`_herdr_inbox_service`) that persists for the lifetime of the process. The
`reset_registry` autouse fixture below resets that global before and after
every test so the cases stay independent (no test depends on another's set()).
"""

import importlib
import subprocess
import sys
import textwrap

import pytest

from cli_agent_orchestrator.services import herdr_inbox_registry
from cli_agent_orchestrator.services.herdr_inbox_registry import (
    get_herdr_inbox_service,
    set_herdr_inbox_service,
)


@pytest.fixture(autouse=True)
def reset_registry():
    """Reset the module-level singleton before and after each test.

    Without this, a set() in one test leaks into the next and breaks the
    "default is None" expectation. We reset on both sides so a test that runs
    first sees a clean slate and a test that runs last does not pollute others.
    """
    herdr_inbox_registry._herdr_inbox_service = None
    yield
    herdr_inbox_registry._herdr_inbox_service = None


class TestGetHerdrInboxService:
    """Tests for get_herdr_inbox_service / default state."""

    def test_get_returns_none_before_any_set(self):
        """get() returns None when nothing has been set yet."""
        assert get_herdr_inbox_service() is None

    def test_module_default_is_none_on_fresh_import(self):
        """The module-level default is genuinely None on a fresh import.

        Reloading re-executes the module body, so this asserts the declared
        default rather than the value the reset fixture happens to install.
        """
        reloaded = importlib.reload(herdr_inbox_registry)
        assert reloaded.get_herdr_inbox_service() is None


class TestSetHerdrInboxService:
    """Tests for set_herdr_inbox_service round-trip and overwrite behavior."""

    def test_set_then_get_round_trip(self):
        """set() stores the exact object get() later returns (identity)."""
        service = object()

        set_herdr_inbox_service(service)  # type: ignore[arg-type]

        assert get_herdr_inbox_service() is service

    def test_set_twice_returns_latest_value(self):
        """A second set() overwrites the first; get() returns the latest."""
        first = object()
        second = object()

        set_herdr_inbox_service(first)  # type: ignore[arg-type]
        set_herdr_inbox_service(second)  # type: ignore[arg-type]

        result = get_herdr_inbox_service()
        assert result is second
        assert result is not first

    def test_set_accepts_arbitrary_object_via_duck_typing(self):
        """The forward-ref type hint is not enforced at runtime.

        Any object is accepted and returned unchanged, so callers can inject a
        test double (MagicMock, sentinel, or plain value) without subclassing
        HerdrInboxService.
        """
        for sentinel in (object(), "a-string", 123, {"k": "v"}):
            set_herdr_inbox_service(sentinel)  # type: ignore[arg-type]
            assert get_herdr_inbox_service() is sentinel

    def test_set_none_overwrites_back_to_unset(self):
        """set(None) clears a previously configured service (no validation).

        Adversarial: the signature advertises a HerdrInboxService, but None is
        accepted and round-trips, indistinguishable from the never-set state.
        """
        set_herdr_inbox_service(object())  # type: ignore[arg-type]
        assert get_herdr_inbox_service() is not None

        set_herdr_inbox_service(None)  # type: ignore[arg-type]

        assert get_herdr_inbox_service() is None


class TestModuleImport:
    """Tests guarding the TYPE_CHECKING import that breaks the circular dep."""

    def test_registry_imports_without_importing_service(self):
        """Importing the registry must not import HerdrInboxService at runtime.

        The service import lives under a TYPE_CHECKING guard precisely to break
        the registry <-> service circular import. If someone moves that import
        to module scope, importing the registry would drag the service module
        into sys.modules.

        This MUST run in a fresh interpreter. In-process, the parent package
        (`...services`) keeps a `herdr_inbox_service` attribute and the module
        object lingers in sys.modules from earlier imports, so a same-process
        pop() + re-import cannot prove the guard holds -- a module-scope
        `from ...services import herdr_inbox_service` mutant would survive. A
        clean subprocess has neither the cached module nor the package
        attribute, so it actually exercises the import.
        """
        service_name = "cli_agent_orchestrator.services.herdr_inbox_service"
        probe = textwrap.dedent(f"""
            import sys
            import cli_agent_orchestrator.services.herdr_inbox_registry as r
            assert r.get_herdr_inbox_service() is None, "default should be None"
            assert "{service_name}" not in sys.modules, (
                "registry import pulled in the service module -- the "
                "TYPE_CHECKING guard was defeated"
            )
            print("OK")
            """)
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"subprocess import probe failed:\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
        assert "OK" in result.stdout

    def test_public_callables_present_after_import(self):
        """Both public functions are importable and callable."""
        reloaded = importlib.reload(herdr_inbox_registry)
        assert callable(reloaded.get_herdr_inbox_service)
        assert callable(reloaded.set_herdr_inbox_service)
