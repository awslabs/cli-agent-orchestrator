"""Self-tests for the ``test/tui/`` hermeticity tripwire (NFR-3 / NFR-4).

These live in a ``test_*.py`` file rather than beside the fixture in
``conftest.py`` because ``pyproject.toml`` sets ``python_files = "test_*.py"``:
pytest never collects a ``conftest.py``, so tests written there are silently never
run. That is the exact failure mode one of these tests exists to close — a matcher
that never matches makes every hermeticity claim in the conftest docstring vacuous,
and a *proof* that never runs is no better.

The autouse fixture itself stays in ``conftest.py`` (that is the only place pytest
will apply it to the whole directory); only its proofs live here.
"""

from __future__ import annotations

import subprocess
from test.tui.conftest import _HermeticityViolation, _names_the_cao_binary
from typing import Any, List
from unittest import mock

import pytest

from cli_agent_orchestrator.tui.app import App
from cli_agent_orchestrator.tui.command_catalog import CommandCatalog


class _InertClipboard:
    """A prompt_toolkit-shaped clipboard double (never the developer's real one)."""

    def __init__(self) -> None:
        self.texts: List[str] = []

    def set_text(self, text: str) -> None:
        self.texts.append(text)

    def set_data(self, data: object) -> None:  # pragma: no cover - unused here
        pass

    def get_data(self) -> object:  # pragma: no cover - unused here
        raise NotImplementedError

    def rotate(self) -> None:  # pragma: no cover - unused here
        pass


def _catalog_double() -> Any:
    """A catalog double, so nothing here shells out to a real ``cao``."""

    catalog = mock.MagicMock(spec=CommandCatalog)
    catalog.groups.return_value = []
    catalog.commands.return_value = []
    catalog.params.return_value = []
    return catalog


def test_the_tripwire_itself_recognises_a_cao_spawn() -> None:
    """The tripwire is only worth having if its matcher works — assert it directly.

    A matcher that never matched would make every hermeticity claim above vacuous.
    """

    assert _names_the_cao_binary(["cao", "--help"])
    assert _names_the_cao_binary("cao")
    assert _names_the_cao_binary(["/usr/local/bin/cao", "session", "list"])
    assert _names_the_cao_binary(["cao-server"])

    # And it does NOT over-match.
    assert not _names_the_cao_binary(["cacao", "--help"])
    assert not _names_the_cao_binary(["git", "status"])
    assert not _names_the_cao_binary(["/repos/cao-tui-front-door/scripts/build.sh"])
    assert not _names_the_cao_binary([])
    assert not _names_the_cao_binary(None)


def test_the_tripwire_is_active_for_http() -> None:
    """The HTTP half of the tripwire actually fires (autouse fixtures are easy to lose)."""

    import requests

    with pytest.raises(_HermeticityViolation, match="REAL HTTP request"):
        requests.get("http://127.0.0.1:9889/health", timeout=0.1)


def test_the_tripwire_is_active_for_cao_spawn() -> None:
    """The spawn half fires, and an unrelated subprocess still works."""

    with pytest.raises(_HermeticityViolation, match="REAL `cao` binary"):
        subprocess.run(["cao", "--help"], capture_output=True)

    # A non-`cao` command is untouched.
    result = subprocess.run(["echo", "ok"], capture_output=True, text=True)
    assert result.stdout.strip() == "ok"


def test_the_tripwire_escapes_the_production_except_Exception_guards() -> None:
    """NFR-4: the violation must NOT be catchable by a broad ``except Exception``.

    Mutation target: ``conftest.py``'s ``class _HermeticityViolation(BaseException)``.
    Reverting the base to ``Exception`` makes this RED — which is the defect: the
    production guards (``App._run_probe``, ``App._focus_input``) swallow any
    ``Exception``, so an ``AssertionError``-based tripwire produced a silent FALSE
    PASS instead of failing the test.
    """

    assert not issubclass(_HermeticityViolation, Exception)
    assert issubclass(_HermeticityViolation, BaseException)

    def production_shaped_guard() -> str:
        try:
            raise _HermeticityViolation("boom")
        except Exception:  # noqa: BLE001 - deliberately mirrors the production shape
            return "swallowed"

    with pytest.raises(_HermeticityViolation):
        production_shaped_guard()


def test_a_real_startup_probe_fails_loudly_instead_of_reporting_unreachable() -> None:
    """NFR-4: the STARTUP-PROBE escape route must not produce a silent false pass.

    This is the behavioural half of the guard above, on the exact production path the
    reviewer reproduced: ``App.__init__`` runs the liveness probe through
    :meth:`App._run_probe`, whose body is ``try: ... except Exception: return False``.
    An ``App(...)`` built with **no** ``liveness_probe=`` and **no** ``client=``
    therefore issued a real, blocked ``GET`` and still ended up on
    ``screen == "unreachable"`` — a test asserting that would have PASSED for
    entirely the wrong reason, with a live HTTP dependency intact.

    Mutation target: ``conftest.py``'s ``class _HermeticityViolation(BaseException)``.
    Reverting the base to ``Exception`` makes ``_run_probe`` swallow it again, the
    construction below succeeds (silently reporting ``unreachable``), and this REDs
    on ``pytest.raises`` never firing.
    """

    with pytest.raises(_HermeticityViolation, match="REAL HTTP request"):
        App(catalog=_catalog_double(), clipboard=_InertClipboard())
