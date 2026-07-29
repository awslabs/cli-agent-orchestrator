"""Hermeticity tripwire for the ``cao tui`` suite (NFR-3 / NFR-4).

Every test under ``test/tui/`` must be hermetic: no real HTTP request, and no real
``cao`` process. The reviewers reported this as a nit against two tests; verifying
the premise found **23** — two spawning a real ``cao`` and **21** issuing a real
``GET http://127.0.0.1:9889/agents/providers``.

Why 21 tests were affected without anyone noticing: the App's footer renders a
provider pre-flight line on *every repaint*, and ``App(client=...)`` defaults to a
real :class:`ServerClient`. Only 3 of 37 ``App(...)`` constructions passed a
``client=`` double, so every keyboard-driven test performed a live GET on its first
frame. They passed only because connection-refused is caught into footer text — a
latent dependency, not a green one. Against a black-holing host (rather than a
locally-refusing one) each would have blocked for up to the client's timeout.

The fix is per-test doubles (``_headless_app`` in ``test_app.py`` and ``_app`` in
``test_app_review_round.py``). This tripwire is the guard that keeps it fixed: an
autouse fixture that makes the two escapes *impossible* rather than merely absent,
so a future test that forgets a double fails loudly here instead of silently
reintroducing the dependency.

Deliberately narrow, in three ways:

* It intercepts at ``HTTPAdapter.send`` — the single chokepoint below every
  ``requests`` entry point — so a test cannot dodge it by using ``Session`` or
  ``requests.request`` instead of ``requests.get``. Tests that legitimately stub
  ``requests`` higher up (``mock.patch.object(sc, "requests", fake)``) never reach
  this layer and are unaffected.
* It blocks only ``cao``-shaped spawns, not all of ``subprocess``. Several tests
  legitimately assert *how* ``subprocess.run`` would be called, with the call
  itself mocked; and non-``cao`` subprocess use stays available.
* It is scoped to ``test/tui/`` by living in this directory's ``conftest.py``. It
  makes no claim about, and has no effect on, the rest of the suite.

The tripwire's own self-tests live in ``test_hermeticity_tripwire.py`` beside this
file, NOT here: ``pyproject.toml`` sets ``python_files = "test_*.py"``, so pytest
never collects a ``conftest.py`` and tests written here would silently never run —
which is exactly the "matcher that never matches" risk one of them exists to close.
"""

from __future__ import annotations

import subprocess
from typing import Any, Iterator, Sequence

import pytest
import requests.adapters

_real_subprocess_run = subprocess.run
_real_subprocess_popen = subprocess.Popen


class _HermeticityViolation(BaseException):
    """Raised when a test escapes the hermetic sandbox — deliberately NOT an ``Exception``.

    Production code legitimately guards its integration boundaries with broad
    ``except Exception`` clauses (``App._run_probe`` turns any probe failure into
    "unreachable"; ``App._focus_input`` turns any focus failure into "close the
    overlay"). An ``AssertionError`` raised from the tripwire IS an ``Exception``, so
    those clauses swallowed it and the escape became a **false pass**: a test that
    issued a real, blocked HTTP request still saw ``screen == "unreachable"`` and
    went green for entirely the wrong reason.

    Subclassing :class:`BaseException` makes the tripwire pass straight through both
    sites and fail the test loudly, without widening a single production
    ``except`` clause (they are correct for their own purpose).
    """


def _names_the_cao_binary(argv: Any) -> bool:
    """Whether ``argv`` would spawn the real ``cao`` executable.

    Matches the executable name exactly (or its basename, for an absolute path) so
    an unrelated command that merely *contains* the substring — ``cacao``, or a repo
    path like ``/…/cao-tui-front-door/scripts/x`` — is not blocked.
    """

    if isinstance(argv, str):
        first: Any = argv
    elif isinstance(argv, Sequence) and argv:
        first = argv[0]
    else:
        return False
    if not isinstance(first, str):
        return False
    return first.rsplit("/", 1)[-1] in ("cao", "cao-server")


@pytest.fixture(autouse=True)
def _forbid_real_network_and_cao_spawn(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fail the test if it issues a real HTTP request or spawns a real ``cao``.

    ``monkeypatch`` undoes both patches at teardown, so nothing leaks into the rest
    of the session.
    """

    def _blocked_send(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
        raise _HermeticityViolation(
            f"NFR-4: this test issued a REAL HTTP request to {request.url}. "
            "Pass a `client=` double (see `_fake_client`) — the App's footer "
            "pre-flight read fires on every repaint."
        )

    def _blocked_run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if _names_the_cao_binary(argv):
            raise _HermeticityViolation(
                f"NFR-3: this test spawned the REAL `cao` binary: {argv!r}. "
                "Pass a `catalog=` double — a test that shells out fails wherever "
                "`cao` is not on PATH."
            )
        return _real_subprocess_run(argv, *args, **kwargs)

    def _blocked_popen(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if _names_the_cao_binary(argv):
            raise _HermeticityViolation(
                f"NFR-3: this test spawned the REAL `cao` binary: {argv!r}."
            )
        return _real_subprocess_popen(argv, *args, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _blocked_send)
    monkeypatch.setattr(subprocess, "run", _blocked_run)
    monkeypatch.setattr(subprocess, "Popen", _blocked_popen)
    yield
