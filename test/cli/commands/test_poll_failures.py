"""What a caller sees when a status poll fails while it waits for a reply (#1016).

`cao session send` and `cao worker send` both send a message and then call
`poll_until_done` until the agent is done. Before #1016 the first failed poll
raised, and both commands skip the output fetch on any exception except
`KeyboardInterrupt`, so one 503 lost a reply the agent went on to finish.

These tests run the real commands against a stand-in server on a loopback port:
the cao-server routes for `session send`, and the fleet broker's proxied routes
for `worker send`, which reads status through `FleetClient`. Each one scripts
what the status route answers on each request and asserts on the command's exit
code and output.

The last test walks the source for loops that read a terminal's status, so a
new wait loop cannot skip the shared failure policy without failing here.
"""

import ast
import http.server
import json
import pathlib
import textwrap
import threading
import time
from unittest.mock import patch

import pytest
from click.testing import CliRunner

import cli_agent_orchestrator
from cli_agent_orchestrator.cli.commands.session import session
from cli_agent_orchestrator.cli.commands.worker import worker
from cli_agent_orchestrator.utils.fleet import FleetClient

REPLY = "the agent's reply"
_real_sleep = time.sleep


class _StandIn:
    """Serves the terminal routes, scripting the status route request by request.

    ``script`` is consumed one entry per status request: a status string, an
    ``int`` HTTP error code, ``("stall", seconds)``, or ``"drop"`` to close the
    connection without answering. Once it runs out, the last entry repeats.
    """

    def __init__(self, prefix, script):
        self.prefix = prefix
        self.script = list(script)
        self.status_requests = 0
        self._release = threading.Event()
        stand_in = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                self._json(200, {"success": True})

            def do_GET(self):
                path = self.path.split("?")[0]
                if not path.startswith(stand_in.prefix):
                    self._json(404, {"detail": f"no route {path}"})
                    return
                route = path[len(stand_in.prefix) :]
                if route == "sessions":
                    self._json(200, [{"name": "s1"}])
                elif route == "sessions/s1/terminals":
                    self._json(200, [{"id": "t1"}])
                elif route == "terminals/t1/output":
                    self._json(200, {"output": REPLY})
                elif route == "terminals/t1":
                    stand_in.status_requests += 1
                    index = min(stand_in.status_requests, len(stand_in.script)) - 1
                    step = stand_in.script[index]
                    if step == "drop":
                        self.close_connection = True
                        self.connection.close()
                    elif isinstance(step, int):
                        self._json(step, {"detail": f"stand-in {step}"})
                    elif isinstance(step, tuple):
                        stand_in._release.wait(step[1])
                        self._json(200, {"id": "t1", "status": "processing"})
                    else:
                        self._json(200, {"id": "t1", "status": step})
                else:
                    self._json(404, {"detail": f"no route {path}"})

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self):
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._release.set()
        self._server.shutdown()
        self._server.server_close()


def _run_session_send(script, timeout):
    """`cao session send` to a local terminal. Its first status read is the pre-send idle check."""
    with (
        _StandIn("/", ["idle"] + script) as server,
        patch("cli_agent_orchestrator.cli.commands.session.API_BASE_URL", server.url),
        patch("cli_agent_orchestrator.utils.terminal.API_BASE_URL", server.url),
        patch("time.sleep", lambda seconds: _real_sleep(min(seconds, 0.01))),
    ):
        result = CliRunner().invoke(
            session, ["send", "s1", "hello", "--terminal", "t1", "--timeout", str(timeout)]
        )
        return result, server.status_requests - 1


def _run_worker_send(script, timeout):
    """`cao worker send` to a worker behind the fleet broker, through the real FleetClient."""
    with (
        _StandIn("/workers/w1/api/", script) as server,
        patch(
            "cli_agent_orchestrator.cli.commands.worker.FleetClient.from_env",
            return_value=FleetClient(server.url, "token"),
        ),
        patch("time.sleep", lambda seconds: _real_sleep(min(seconds, 0.01))),
    ):
        result = CliRunner().invoke(worker, ["send", "w1", "hello", "--timeout", str(timeout)])
        return result, server.status_requests


SENDERS = {"session": _run_session_send, "worker": _run_worker_send}


@pytest.mark.parametrize("sender", SENDERS)
@pytest.mark.parametrize("failure", [503, "drop"], ids=["503", "dropped-connection"])
def test_a_failed_poll_mid_task_still_prints_the_reply(sender, failure):
    script = ["processing", "processing", failure, "processing", "completed"]
    result, polls = SENDERS[sender](script, timeout=30)
    assert result.exit_code == 0, result.output
    assert REPLY in result.output
    assert polls == 5


def test_a_stalled_poll_past_the_request_timeout_still_prints_the_reply():
    """A local poll stalls past its 5s per-request timeout and the next one succeeds."""
    script = ["processing", "processing", ("stall", 6), "processing", "completed"]
    result, polls = _run_session_send(script, timeout=30)
    assert result.exit_code == 0, result.output
    assert REPLY in result.output
    assert polls == 5


@pytest.mark.parametrize("sender", SENDERS)
def test_a_404_fails_at_once(sender):
    script = ["processing", 404]
    result, polls = SENDERS[sender](script, timeout=30)
    assert result.exit_code != 0
    assert "Failed to poll terminal status" in result.output
    assert "404" in result.output
    assert polls == 2


@pytest.mark.parametrize("sender", SENDERS)
def test_every_poll_failing_times_out_naming_the_last_error(sender):
    script = ["processing", 503]
    result, polls = SENDERS[sender](script, timeout=1)
    assert result.exit_code != 0
    assert "Timed out after" in result.output
    assert "the last poll failed" in result.output
    assert "503" in result.output
    assert polls > 2


# -- every loop that waits on a terminal's status uses the shared policy -------

# The loops allowed to read a terminal's status, and why, keyed by module and
# function. A new wait loop must either call one of the first two or be added
# here with its reason.
_STATUS_LOOPS = {
    ("utils/terminal.py", "poll_until_done"): "classifies failures with _is_transient_poll_error",
    ("utils/terminal.py", "wait_for_initial_delivery"): (
        "classifies failures with _is_transient_poll_error"
    ),
    ("utils/terminal.py", "wait_until_terminal_status"): (
        "returns False on any failure and never raises, so a failed poll cannot "
        "skip a caller's output fetch; `cao launch` attaches either way"
    ),
}


def _assignments(function):
    """Each local name in ``function`` mapped to the source of what it is assigned."""
    assigned = {}
    for node in ast.walk(function):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                assigned.setdefault(target.id, []).append(ast.unparse(value))
    return assigned


def _reads_terminal_status(call: ast.Call, assigned) -> bool:
    func = ast.unparse(call.func).split(".")[-1]
    if func in {"terminal_status", "read_status"}:
        return True
    if func != "get":
        return False
    url = (
        call.args[0]
        if call.args
        else next((kw.value for kw in call.keywords if kw.arg == "url"), None)
    )
    if url is None:
        return False
    if isinstance(url, ast.Name):
        sources = assigned.get(url.id, [])
    else:
        sources = [ast.unparse(url)]
    return any(
        "/terminals/" in source and not source.rstrip("'\"").endswith("/output")
        for source in sources
    )


def _calls_outside_lambdas(node):
    """Calls under ``node``, skipping lambda bodies.

    A lambda passed as ``read_status`` runs inside ``poll_until_done``'s loop,
    not the caller's, so `cao worker attach` re-prompting around
    ``poll_until_done`` is not a second wait loop.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.Lambda):
            continue
        if isinstance(child, ast.Call):
            yield child
        yield from _calls_outside_lambdas(child)


def _status_wait_loops(tree):
    """Names of the functions in ``tree`` with a loop that reads a terminal's status."""
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        assigned = _assignments(function)
        for loop in ast.walk(function):
            if isinstance(loop, (ast.While, ast.For)) and any(
                _reads_terminal_status(call, assigned) for call in _calls_outside_lambdas(loop)
            ):
                yield function
                break


_GUARD_CASES = {
    "inline-url": """
        def f(t):
            while True:
                api_http.get(f"{BASE}/terminals/{t}")
    """,
    "url-in-a-variable": """
        def f(t):
            url = f"{BASE}/terminals/{t}"
            while True:
                api_http.get(url)
    """,
    "url-keyword": """
        def f(t):
            while True:
                requests.get(url=f"{BASE}/terminals/{t}")
    """,
    "fleet-client": """
        def f(c, t):
            for _ in range(3):
                c.terminal_status("w", t)
    """,
}


@pytest.mark.parametrize("case", _GUARD_CASES)
def test_the_guard_sees_a_status_read_in_each_form(case):
    tree = ast.parse(textwrap.dedent(_GUARD_CASES[case]))
    assert [f.name for f in _status_wait_loops(tree)] == ["f"]


def test_the_guard_ignores_output_reads():
    source = """
        def f(t):
            while True:
                api_http.get(f"{BASE}/terminals/{t}/output")
    """
    assert list(_status_wait_loops(ast.parse(textwrap.dedent(source)))) == []


def test_every_status_wait_loop_goes_through_the_shared_failure_policy():
    package = pathlib.Path(cli_agent_orchestrator.__file__).parent
    found = {}
    for path in sorted((package / "cli").rglob("*.py")) + sorted((package / "utils").rglob("*.py")):
        module = path.relative_to(package).as_posix()
        for function in _status_wait_loops(ast.parse(path.read_text(encoding="utf-8"))):
            found[(module, function.name)] = function

    assert set(found) == set(_STATUS_LOOPS), (
        "a loop reads terminal status outside the known wait helpers; route it "
        "through poll_until_done or classify its failures with _is_transient_poll_error"
    )
    for key in (
        ("utils/terminal.py", "poll_until_done"),
        ("utils/terminal.py", "wait_for_initial_delivery"),
    ):
        calls = {
            ast.unparse(node.func) for node in ast.walk(found[key]) if isinstance(node, ast.Call)
        }
        assert "_is_transient_poll_error" in calls, key
