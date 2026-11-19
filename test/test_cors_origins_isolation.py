"""The live ``CORS_ORIGINS`` allowlist does not leak between tests.

``api.main.main()`` extends the module-level list in place through
``add_local_cors_origins``. Before ``conftest._restore_cors_origins`` existed,
any test that drove ``main()`` left ``http://localhost:9889`` allowlisted for
every later test in the same process, and the scheme-downgrade rows of
``test/security/test_origin_matrix.py`` failed or passed depending on run order.

The two tests below are a pair and rely on file order: the first leaks on
purpose, the second fails if the leak survived. Under xdist they may land on
different workers, where the second passes trivially; in a serial run, and
whenever they share a worker, it is a real check. The third test reproduces the
original symptom in one test, so it does not depend on order at all.
"""

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from cli_agent_orchestrator import constants
from cli_agent_orchestrator.api import main as api_main

LEAKED = "http://localhost:9889"


def test_a_test_may_extend_the_live_allowlist():
    constants.add_local_cors_origins("127.0.0.1", 9889)
    assert LEAKED in constants.CORS_ORIGINS


def test_the_extension_did_not_survive_into_the_next_test():
    assert LEAKED not in constants.CORS_ORIGINS, (
        "a previous test's add_local_cors_origins() is still in the live "
        "CORS_ORIGINS list; conftest._restore_cors_origins is missing or broken"
    )


def test_the_same_list_object_is_restored_not_replaced():
    """The origin checks read the live list object, so identity matters.

    Drives the real leak (``main()`` with uvicorn patched), then runs the
    fixture's own restore step and asserts the downgrade row of the origin
    matrix is rejected again on the very same list object.

    The list is read from ``constants`` at call time, not from ``api.main``:
    tests that ``importlib.reload(constants)`` (``test_constants.py``,
    ``test_cursor_cli_unit.py``) rebind ``constants.CORS_ORIGINS`` to a new
    list, after which ``api.main``'s imported name is stale. ``main()`` and the
    origin checks both go through the module's current global, so that is the
    list this test has to hold for it to pass in any run order.
    """
    held = constants.CORS_ORIGINS
    before = list(held)
    with (
        patch("argparse.ArgumentParser.parse_args") as args,
        patch("uvicorn.run"),
        patch.dict("os.environ"),
    ):
        args.return_value = MagicMock(agents_dir=None, host=None, port=None, terminal=None)
        api_main.main()
    assert LEAKED in held, "main() no longer extends the allowlist; this test needs a new leak"

    client = TestClient(api_main.app, base_url="https://localhost:9889")
    post = lambda: client.post(  # noqa: E731
        "/flows", headers={"Content-Type": "text/plain", "Origin": LEAKED}, content="x"
    )
    assert post().status_code == 422, "with the leak in place the downgrade is accepted"

    constants.CORS_ORIGINS[:] = before  # what conftest._restore_cors_origins does

    assert constants.CORS_ORIGINS is held
    assert post().status_code == 403
