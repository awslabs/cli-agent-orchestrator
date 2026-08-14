"""Packaging and installed-Pi contract tests for the bundled extension."""

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

FAKE_PROXY_SOURCE = r'''"""Controllable JSONL child for Pi extension behavior tests."""
import json
import os
import signal
import sys
import time


def send(request_id, result):
    sys.stdout.write(json.dumps({"id": request_id, "ok": True, "result": result}) + "\n")
    sys.stdout.flush()


def send_error(request_id, error):
    sys.stdout.write(json.dumps({"id": request_id, "ok": False, "error": error}) + "\n")
    sys.stdout.flush()


def send_chunked(response):
    payload = (json.dumps(response, ensure_ascii=False) + "\n").encode()
    marker = "☃".encode()
    split = payload.find(marker)
    split = split + 1 if split >= 0 else max(1, len(payload) // 2)
    sys.stdout.buffer.write(payload[:split])
    sys.stdout.buffer.flush()
    time.sleep(0.01)
    sys.stdout.buffer.write(payload[split:])
    sys.stdout.buffer.flush()


scenario = os.environ["CAO_TEST_PROXY_SCENARIO"]
with open(os.environ["CAO_TEST_PROXY_PID_FILE"], "w") as pid_file:
    pid_file.write(str(os.getpid()))
if scenario == "ignore_sigterm":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
call_count = 0
pending_calls = []
for raw_line in sys.stdin:
    request = json.loads(raw_line)
    if request["type"] == "list_tools":
        tools = []
        if scenario == "builtin_collision":
            tools = [
                {
                    "server": "fixture",
                    "name": name,
                    "description": f"{name} tool",
                    "inputSchema": {"type": "object"},
                }
                for name in ["safe_mcp_tool", "bash", "read", "edit", "write", "grep", "find", "ls"]
            ]
        if scenario in {
            "mcp_is_error",
            "request_error",
            "normalize",
            "chunked_correlation",
            "abort_late",
            "abort_hang",
            "exit_during_call",
            "close_stdin",
        }:
            tools = [{
                "server": "fixture",
                "name": "echo",
                "description": "Echo ☃ text" if scenario == "chunked_correlation" else "Echo text",
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            }]
        if scenario == "chunked_correlation":
            send_chunked({"id": request["id"], "ok": True, "result": {"tools": tools}})
        else:
            send(request["id"], {"tools": tools})
        if scenario == "exit_while_idle":
            time.sleep(0.1)
            raise SystemExit(7)
        if scenario == "close_stdin":
            os.close(0)
            with open(os.environ["CAO_TEST_PROXY_READY_FILE"], "w") as ready_file:
                ready_file.write("closed")
            time.sleep(60)
    elif request["type"] == "call_tool":
        call_count += 1
        if scenario == "chunked_correlation":
            pending_calls.append(request)
            if len(pending_calls) == 2:
                for pending in reversed(pending_calls):
                    send_chunked({
                        "id": pending["id"],
                        "ok": True,
                        "result": {
                            "content": [{"type": "text", "text": pending["arguments"]["text"]}],
                            "isError": False,
                        },
                    })
        elif scenario == "abort_late" and call_count == 1:
            time.sleep(0.15)
            send(request["id"], {
                "content": [{"type": "text", "text": "late"}],
                "isError": False,
            })
        elif scenario == "abort_hang" and call_count == 1:
            pending_calls.append(request)
        elif scenario == "abort_hang" and pending_calls:
            continue
        elif scenario == "exit_during_call":
            raise SystemExit(9)
        elif scenario == "mcp_is_error" and call_count == 1:
            send(request["id"], {
                "content": [{"type": "text", "text": "tool failed"}],
                "isError": True,
            })
        elif scenario == "request_error" and call_count == 1:
            send_error(request["id"], "request rejected")
        elif scenario == "normalize":
            send(request["id"], {
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "image", "data": "aW1n", "mimeType": "image/png"},
                    {"type": "resource", "resource": {"text": "resource text"}},
                    {
                        "type": "resource",
                        "resource": {"blob": "anBlZw==", "mimeType": "image/jpeg"},
                    },
                    {"type": "resource_link", "name": "Doc", "uri": "https://example.test"},
                    {"type": "audio", "data": "YXVkaW8=", "mimeType": "audio/wav"},
                    {"type": "custom", "value": 1},
                ],
                "isError": False,
            })
        else:
            send(request["id"], {
                "content": [{"type": "text", "text": "recovered"}],
                "isError": False,
            })
    elif request["type"] == "cancel":
        cancelled = bool(
            pending_calls and request.get("targetId") == pending_calls[0]["id"]
        )
        if cancelled:
            pending_calls.clear()
        send(request["id"], {"cancelled": cancelled})
    elif request["type"] == "shutdown":
        if scenario == "exit_on_shutdown":
            raise SystemExit(0)
        if scenario in {"hang_on_shutdown", "ignore_sigterm"}:
            time.sleep(60)
        send(request["id"], {})
        raise SystemExit(0)
'''

NODE_HARNESS = r"""
import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";

const [extensionPath, action] = process.argv.slice(1);
const extension = await import(pathToFileURL(extensionPath).href);

async function waitForFile(path, timeoutMs = 1_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      await readFile(path);
      return;
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error(`Timed out waiting for ${path}`);
}

const handlers = new Map();
const statuses = [];
const tools = [];
const piBuiltins = new Set(["bash", "read", "edit", "write", "grep", "find", "ls"]);
const pi = {
  on(name, handler) { handlers.set(name, handler); },
  registerTool(tool) {
    if (process.env.CAO_TEST_NATIVE_TOOL_POLICY === "denied" && piBuiltins.has(tool.name)) {
      return;
    }
    tools.push(tool);
  },
};
const ctx = {
  ui: {
    setStatus(key, text) { statuses.push({ key, text: text ?? null }); },
  },
};
extension.default(pi);
let startError;
try {
  await handlers.get("session_start")({}, ctx);
} catch (error) {
  if (action !== "spawn_failure" && action !== "builtin_collision") throw error;
  startError = { name: error.name, message: error.message };
}

let output;
if (action === "spawn_failure") {
  const started = Date.now();
  const settled = await Promise.race([
    handlers.get("session_shutdown")({}, ctx).then(() => true),
    new Promise((resolve) => setTimeout(() => resolve(false), 1_500)),
  ]);
  output = {
    startError,
    settled,
    elapsedMs: Date.now() - started,
    state: JSON.parse(await readFile(process.env.CAO_PI_STATE_FILE, "utf8")),
    statuses,
  };
} else if (action === "builtin_collision") {
  output = {
    startError,
    registeredToolNames: tools.map((tool) => tool.name),
    state: JSON.parse(await readFile(process.env.CAO_PI_STATE_FILE, "utf8")),
    statuses,
  };
  await handlers.get("session_shutdown")({}, ctx);
} else if (action === "shutdown_exit") {
  const started = Date.now();
  const settled = await Promise.race([
    handlers.get("session_shutdown")({}, ctx).then(() => true),
    new Promise((resolve) => setTimeout(() => resolve(false), 1_000)),
  ]);
  output = {
    settled,
    elapsedMs: Date.now() - started,
    state: JSON.parse(await readFile(process.env.CAO_PI_STATE_FILE, "utf8")),
    statuses,
  };
} else if (action === "shutdown_hang") {
  const started = Date.now();
  const settled = await Promise.race([
    handlers.get("session_shutdown")({}, ctx).then(() => true),
    new Promise((resolve) => setTimeout(() => resolve(false), 6_500)),
  ]);
  output = {
    settled,
    elapsedMs: Date.now() - started,
    state: JSON.parse(await readFile(process.env.CAO_PI_STATE_FILE, "utf8")),
    statuses,
  };
} else if (action === "shutdown_ignore_term") {
  const started = Date.now();
  const settled = await Promise.race([
    handlers.get("session_shutdown")({}, ctx).then(() => true),
    new Promise((resolve) => setTimeout(() => resolve(false), 7_000)),
  ]);
  const proxyPid = Number(await readFile(process.env.CAO_TEST_PROXY_PID_FILE, "utf8"));
  let proxyAlive = false;
  try {
    process.kill(proxyPid, 0);
    proxyAlive = true;
  } catch {}
  output = {
    settled,
    elapsedMs: Date.now() - started,
    proxyAlive,
    state: JSON.parse(await readFile(process.env.CAO_PI_STATE_FILE, "utf8")),
    statuses,
  };
} else if (action === "shutdown_closed_stdin") {
  await waitForFile(process.env.CAO_TEST_PROXY_READY_FILE);
  const started = Date.now();
  const settled = await Promise.race([
    handlers.get("session_shutdown")({}, ctx).then(() => true),
    new Promise((resolve) => setTimeout(() => resolve(false), 2_000)),
  ]);
  const proxyPid = Number(await readFile(process.env.CAO_TEST_PROXY_PID_FILE, "utf8"));
  let proxyAlive = false;
  try {
    process.kill(proxyPid, 0);
    proxyAlive = true;
  } catch {}
  output = {
    settled,
    elapsedMs: Date.now() - started,
    proxyAlive,
    state: JSON.parse(await readFile(process.env.CAO_PI_STATE_FILE, "utf8")),
    statuses,
  };
} else if (action === "active_closed_stdin") {
  await waitForFile(process.env.CAO_TEST_PROXY_READY_FILE);
  await handlers.get("agent_start")({}, ctx);
  let callError;
  try {
    await tools[0].execute("call-1", { text: "first" }, undefined, undefined, ctx);
  } catch (error) {
    callError = { name: error.name, message: error.message };
  }
  await new Promise((resolve) => setTimeout(resolve, 50));
  await handlers.get("agent_settled")({}, ctx);
  const started = Date.now();
  await handlers.get("session_shutdown")({}, ctx);
  const proxyPid = Number(await readFile(process.env.CAO_TEST_PROXY_PID_FILE, "utf8"));
  let proxyAlive = false;
  try {
    process.kill(proxyPid, 0);
    proxyAlive = true;
  } catch {}
  output = {
    callError,
    shutdownElapsedMs: Date.now() - started,
    proxyAlive,
    state: JSON.parse(await readFile(process.env.CAO_PI_STATE_FILE, "utf8")),
    statuses,
  };
} else if (action === "idle_exit") {
  await new Promise((resolve) => setTimeout(resolve, 300));
  output = {
    state: JSON.parse(await readFile(process.env.CAO_PI_STATE_FILE, "utf8")),
    statuses,
  };
} else if (action === "terminal_sticky") {
  await new Promise((resolve) => setTimeout(resolve, 300));
  const failureState = JSON.parse(await readFile(process.env.CAO_PI_STATE_FILE, "utf8"));
  await handlers.get("agent_start")({}, ctx);
  await handlers.get("message_end")({
    message: { role: "assistant", content: [{ type: "text", text: "must not publish" }] },
  }, ctx);
  await handlers.get("agent_settled")({}, ctx);
  output = {
    failureState,
    state: JSON.parse(await readFile(process.env.CAO_PI_STATE_FILE, "utf8")),
    statuses,
  };
} else if (action === "nonterminal_error") {
  await handlers.get("agent_start")({}, ctx);
  let firstError;
  try {
    await tools[0].execute("call-1", { text: "first" }, undefined, undefined, ctx);
  } catch (error) {
    firstError = { name: error.name, message: error.message };
  }
  const second = await tools[0].execute(
    "call-2",
    { text: "second" },
    undefined,
    undefined,
    ctx,
  );
  await handlers.get("message_end")({
    message: { role: "assistant", content: [{ type: "text", text: "after recovery" }] },
  }, ctx);
  await handlers.get("agent_settled")({}, ctx);
  output = {
    firstError,
    second,
    state: JSON.parse(await readFile(process.env.CAO_PI_STATE_FILE, "utf8")),
    statuses,
  };
  await handlers.get("session_shutdown")({}, ctx);
} else if (action === "normalize") {
  const result = await tools[0].execute(
    "call-1",
    { text: "input" },
    undefined,
    undefined,
    ctx,
  );
  output = {
    tool: {
      name: tools[0].name,
      description: tools[0].description,
      parameters: tools[0].parameters,
    },
    result,
  };
  await handlers.get("session_shutdown")({}, ctx);
} else if (action === "correlation") {
  const [first, second] = await Promise.all([
    tools[0].execute("call-1", { text: "first" }, undefined, undefined, ctx),
    tools[0].execute("call-2", { text: "second" }, undefined, undefined, ctx),
  ]);
  output = {
    description: tools[0].description,
    first: first.content,
    second: second.content,
  };
  await handlers.get("session_shutdown")({}, ctx);
} else if (action === "abort_late") {
  await handlers.get("agent_start")({}, ctx);
  const controller = new AbortController();
  const firstCall = tools[0].execute(
    "call-1",
    { text: "first" },
    controller.signal,
    undefined,
    ctx,
  );
  setTimeout(() => controller.abort(), 20);
  let firstError;
  try {
    await firstCall;
  } catch (error) {
    firstError = { name: error.name, message: error.message };
  }
  const second = await tools[0].execute(
    "call-2",
    { text: "second" },
    undefined,
    undefined,
    ctx,
  );
  await handlers.get("message_end")({
    message: { role: "assistant", content: [{ type: "text", text: "abort recovered" }] },
  }, ctx);
  await handlers.get("agent_settled")({}, ctx);
  output = {
    firstError,
    second: second.content,
    state: JSON.parse(await readFile(process.env.CAO_PI_STATE_FILE, "utf8")),
    statuses,
  };
  await handlers.get("session_shutdown")({}, ctx);
} else if (action === "abort_hang") {
  await handlers.get("agent_start")({}, ctx);
  const controller = new AbortController();
  const abortStarted = Date.now();
  const firstCall = tools[0].execute(
    "call-1",
    { text: "hang" },
    controller.signal,
    undefined,
    ctx,
  );
  setTimeout(() => controller.abort(), 20);
  let firstError;
  try {
    await firstCall;
  } catch (error) {
    firstError = { name: error.name, message: error.message };
  }
  const abortElapsedMs = Date.now() - abortStarted;
  const secondStarted = Date.now();
  const secondCall = tools[0].execute(
    "call-2",
    { text: "second" },
    undefined,
    undefined,
    ctx,
  );
  const second = await Promise.race([
    secondCall.then((result) => ({ settled: true, content: result.content })),
    new Promise((resolve) => setTimeout(() => resolve({ settled: false }), 750)),
  ]);
  const secondElapsedMs = Date.now() - secondStarted;
  const shutdownStarted = Date.now();
  await handlers.get("session_shutdown")({}, ctx);
  const shutdownElapsedMs = Date.now() - shutdownStarted;
  const proxyPid = Number(await readFile(process.env.CAO_TEST_PROXY_PID_FILE, "utf8"));
  let proxyAlive = false;
  try {
    process.kill(proxyPid, 0);
    proxyAlive = true;
  } catch {}
  output = {
    firstError,
    abortElapsedMs,
    second,
    secondElapsedMs,
    shutdownElapsedMs,
    proxyAlive,
    statuses,
  };
} else if (action === "lifecycle") {
  const idle = JSON.parse(await readFile(process.env.CAO_PI_STATE_FILE, "utf8"));
  await handlers.get("agent_start")({}, ctx);
  const processing = JSON.parse(await readFile(process.env.CAO_PI_STATE_FILE, "utf8"));
  await handlers.get("message_end")({
    message: {
      role: "assistant",
      content: [
        { type: "text", text: "final " },
        { type: "thinking", thinking: "hidden" },
        { type: "text", text: "answer" },
      ],
    },
  }, ctx);
  await handlers.get("agent_settled")({}, ctx);
  const completed = JSON.parse(await readFile(process.env.CAO_PI_STATE_FILE, "utf8"));
  output = { idle, processing, completed };
  await handlers.get("session_shutdown")({}, ctx);
} else if (action === "terminal_call") {
  await handlers.get("agent_start")({}, ctx);
  let callError;
  try {
    await tools[0].execute("call-1", { text: "first" }, undefined, undefined, ctx);
  } catch (error) {
    callError = { name: error.name, message: error.message };
  }
  await new Promise((resolve) => setTimeout(resolve, 50));
  await handlers.get("agent_settled")({}, ctx);
  output = {
    callError,
    state: JSON.parse(await readFile(process.env.CAO_PI_STATE_FILE, "utf8")),
    statuses,
  };
}

process.stdout.write(`${JSON.stringify(output)}\n`, () => process.exit(0));
"""


def pi_extension_path() -> Path:
    return REPO_ROOT / "src/cli_agent_orchestrator/providers/pi_extension.ts"


def build_wheel(tmp_path: Path) -> zipfile.ZipFile:
    subprocess.run(
        ["uv", "build", "--offline", "--wheel", "--out-dir", str(tmp_path)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel_path = next(tmp_path.glob("cli_agent_orchestrator-*.whl"))
    return zipfile.ZipFile(wheel_path)


def run_node_harness(
    tmp_path: Path,
    *,
    scenario: str,
    action: str,
    timeout: float = 10,
    bridge_python: str | None = None,
    native_tool_policy: str = "allowed",
) -> dict:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for Pi extension behavior tests"
    fake_package = tmp_path / "fake-pythonpath/cli_agent_orchestrator/providers"
    fake_package.mkdir(parents=True)
    (fake_package.parent.parent / "__init__.py").write_text("")
    (fake_package.parent / "__init__.py").write_text("")
    (fake_package / "__init__.py").write_text("")
    (fake_package / "pi_mcp_proxy.py").write_text(FAKE_PROXY_SOURCE)
    config_file = tmp_path / "mcp.json"
    config_file.write_text("{}")
    state_file = tmp_path / "state/pi.json"
    env = {
        **os.environ,
        "CAO_PI_STATE_FILE": str(state_file),
        "CAO_PI_MCP_CONFIG": str(config_file),
        "CAO_PI_BRIDGE_PYTHON": bridge_python or sys.executable,
        "CAO_TEST_PROXY_SCENARIO": scenario,
        "CAO_TEST_PROXY_PID_FILE": str(tmp_path / "proxy.pid"),
        "CAO_TEST_PROXY_READY_FILE": str(tmp_path / "proxy.ready"),
        "CAO_TEST_NATIVE_TOOL_POLICY": native_tool_policy,
        "PYTHONPATH": str(tmp_path / "fake-pythonpath")
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
    }
    try:
        result = subprocess.run(
            [
                node,
                "--no-warnings",
                "--input-type=module",
                "-e",
                NODE_HARNESS,
                str(pi_extension_path()),
                action,
            ],
            cwd=REPO_ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    finally:
        pid_file = tmp_path / "proxy.pid"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.splitlines()[-1])


def test_bundled_extension_exists_and_has_required_events():
    source = pi_extension_path().read_text()
    assert 'pi.on("agent_start"' in source
    assert 'pi.on("message_end"' in source
    assert 'pi.on("agent_settled"' in source
    assert "pi.registerTool" in source


def test_wheel_contains_pi_extension(tmp_path):
    with build_wheel(tmp_path) as wheel:
        assert "cli_agent_orchestrator/providers/pi_extension.ts" in wheel.namelist()


def test_shutdown_does_not_hang_when_proxy_exits_before_reply(tmp_path):
    result = run_node_harness(tmp_path, scenario="exit_on_shutdown", action="shutdown_exit")

    assert result["settled"] is True
    assert result["elapsedMs"] < 1_000
    assert result["state"]["status"] == "idle"
    assert result["state"]["error"] == ""
    assert result["statuses"] == [{"key": "cao-pi-mcp", "text": None}]


def test_shutdown_bounds_nonreplying_proxy_and_kills_it(tmp_path):
    result = run_node_harness(
        tmp_path, scenario="hang_on_shutdown", action="shutdown_hang", timeout=8
    )

    assert result["settled"] is True
    assert 4_500 <= result["elapsedMs"] < 6_000
    assert result["state"]["status"] == "idle"
    assert result["state"]["error"] == ""
    assert result["statuses"] == [{"key": "cao-pi-mcp", "text": None}]


def test_shutdown_escalates_when_proxy_ignores_sigterm(tmp_path):
    result = run_node_harness(
        tmp_path,
        scenario="ignore_sigterm",
        action="shutdown_ignore_term",
        timeout=9,
    )

    assert result["settled"] is True
    assert 5_000 <= result["elapsedMs"] < 7_000
    assert result["proxyAlive"] is False
    assert result["state"]["status"] == "idle"
    assert result["state"]["error"] == ""
    assert result["statuses"] == [{"key": "cao-pi-mcp", "text": None}]


def test_shutdown_handles_proxy_closed_stdin_without_terminal_publication(tmp_path):
    result = run_node_harness(
        tmp_path,
        scenario="close_stdin",
        action="shutdown_closed_stdin",
        timeout=4,
    )

    assert result["settled"] is True
    assert result["elapsedMs"] < 2_000
    assert result["proxyAlive"] is False
    assert set(result["state"]) == {"status", "lastAssistantText", "error", "updatedAt"}
    assert result["state"]["status"] == "idle"
    assert result["state"]["error"] == ""
    assert result["statuses"] == [{"key": "cao-pi-mcp", "text": None}]


def test_active_call_closed_stdin_is_one_terminal_failure_and_tears_down(tmp_path):
    result = run_node_harness(
        tmp_path,
        scenario="close_stdin",
        action="active_closed_stdin",
        timeout=4,
    )

    assert result["callError"]["name"] == "BridgeTerminalError"
    assert "EPIPE" in result["callError"]["message"]
    assert result["shutdownElapsedMs"] < 2_000
    assert result["proxyAlive"] is False
    assert set(result["state"]) == {"status", "lastAssistantText", "error", "updatedAt"}
    assert result["state"]["status"] == "error"
    assert "EPIPE" in result["state"]["error"]
    assert len(result["statuses"]) == 2
    assert result["statuses"][0] == {"key": "cao-pi-mcp", "text": None}
    assert result["statuses"][1]["key"] == "cao-pi-mcp"
    assert "EPIPE" in result["statuses"][1]["text"]


def test_failed_spawn_leaves_shutdown_immediately_settled(tmp_path):
    result = run_node_harness(
        tmp_path,
        scenario="normal",
        action="spawn_failure",
        timeout=3,
        bridge_python=str(tmp_path / "missing-python"),
    )

    assert result["startError"]["name"] == "BridgeTerminalError"
    assert result["settled"] is True
    assert result["elapsedMs"] < 750
    assert set(result["state"]) == {"status", "lastAssistantText", "error", "updatedAt"}
    assert result["state"]["status"] == "error"
    assert result["state"]["error"]
    assert len(result["statuses"]) == 1
    assert result["statuses"][0]["text"].startswith("MCP error: ")


def test_unexpected_idle_proxy_exit_publishes_terminal_state_and_ui(tmp_path):
    result = run_node_harness(tmp_path, scenario="exit_while_idle", action="idle_exit")

    assert set(result["state"]) == {"status", "lastAssistantText", "error", "updatedAt"}
    assert result["state"]["status"] == "error"
    assert "exit code 7" in result["state"]["error"]
    assert result["statuses"][-1]["key"] == "cao-pi-mcp"
    assert "exit code 7" in result["statuses"][-1]["text"]


def test_terminal_bridge_state_survives_later_lifecycle_events(tmp_path):
    result = run_node_harness(tmp_path, scenario="exit_while_idle", action="terminal_sticky")

    assert set(result["state"]) == {"status", "lastAssistantText", "error", "updatedAt"}
    assert result["state"] == result["failureState"]
    assert result["state"]["status"] == "error"
    assert "exit code 7" in result["state"]["error"]
    assert result["statuses"] == [
        {"key": "cao-pi-mcp", "text": None},
        {
            "key": "cao-pi-mcp",
            "text": "MCP error: Pi MCP proxy exited with exit code 7",
        },
    ]


@pytest.mark.parametrize(
    ("scenario", "first_error"),
    [("mcp_is_error", "tool failed"), ("request_error", "request rejected")],
)
def test_tool_level_errors_leave_bridge_and_lifecycle_usable(tmp_path, scenario, first_error):
    result = run_node_harness(tmp_path, scenario=scenario, action="nonterminal_error")

    assert result["firstError"]["message"] == first_error
    assert result["second"]["content"] == [{"type": "text", "text": "recovered"}]
    assert result["state"]["status"] == "completed"
    assert result["state"]["lastAssistantText"] == "after recovery"
    assert result["state"]["error"] == ""
    assert result["statuses"] == [{"key": "cao-pi-mcp", "text": None}]


@pytest.mark.parametrize("native_tool_policy", ["allowed", "denied"])
def test_builtin_collision_fails_before_any_mcp_registration(tmp_path, native_tool_policy):
    """MCP cannot shadow or be suppressed by Pi built-ins under either native policy."""
    result = run_node_harness(
        tmp_path,
        scenario="builtin_collision",
        action="builtin_collision",
        native_tool_policy=native_tool_policy,
    )

    assert result["startError"] == {
        "name": "Error",
        "message": "Pi MCP proxy returned reserved Pi tool name: bash",
    }
    assert result["registeredToolNames"] == []
    assert result["state"]["status"] == "error"


def test_dynamic_tool_preserves_raw_schema_and_normalizes_actual_call_result(tmp_path):
    result = run_node_harness(tmp_path, scenario="normalize", action="normalize")

    assert result["tool"] == {
        "name": "echo",
        "description": "Echo text",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    }
    assert result["result"]["content"] == [
        {"type": "text", "text": "hello"},
        {"type": "image", "data": "aW1n", "mimeType": "image/png"},
        {"type": "text", "text": "resource text"},
        {"type": "image", "data": "anBlZw==", "mimeType": "image/jpeg"},
        {"type": "text", "text": "Doc: https://example.test"},
        {"type": "text", "text": "[MCP audio content (audio/wav)]"},
        {"type": "text", "text": '{"type":"custom","value":1}'},
    ]


def test_jsonl_chunks_preserve_utf8_and_correlate_reversed_replies(tmp_path):
    result = run_node_harness(tmp_path, scenario="chunked_correlation", action="correlation")

    assert result == {
        "description": "Echo ☃ text",
        "first": [{"type": "text", "text": "first"}],
        "second": [{"type": "text", "text": "second"}],
    }


def test_abort_cleans_pending_and_late_reply_does_not_poison_next_call(tmp_path):
    result = run_node_harness(tmp_path, scenario="abort_late", action="abort_late")

    assert result["firstError"] == {
        "name": "AbortError",
        "message": "MCP tool call aborted",
    }
    assert result["second"] == [{"type": "text", "text": "recovered"}]
    assert result["state"]["status"] == "completed"
    assert result["state"]["lastAssistantText"] == "abort recovered"
    assert result["state"]["error"] == ""
    assert result["statuses"] == [{"key": "cao-pi-mcp", "text": None}]


def test_abort_cancels_hanging_call_then_later_call_and_shutdown_are_immediate(tmp_path):
    """Abort crosses JSONL by request ID instead of leaving the serial bridge blocked."""
    result = run_node_harness(tmp_path, scenario="abort_hang", action="abort_hang", timeout=4)

    assert result["firstError"] == {
        "name": "AbortError",
        "message": "MCP tool call aborted",
    }
    assert result["abortElapsedMs"] < 500
    assert result["second"] == {
        "settled": True,
        "content": [{"type": "text", "text": "recovered"}],
    }
    assert result["secondElapsedMs"] < 750
    assert result["shutdownElapsedMs"] < 1_000
    assert result["proxyAlive"] is False
    assert result["statuses"] == [{"key": "cao-pi-mcp", "text": None}]


def test_lifecycle_transitions_publish_exact_state_and_assistant_text(tmp_path):
    result = run_node_harness(tmp_path, scenario="normal", action="lifecycle")

    for state in result.values():
        assert set(state) == {"status", "lastAssistantText", "error", "updatedAt"}
        assert state["updatedAt"]
    assert result["idle"]["status"] == "idle"
    assert result["processing"]["status"] == "processing"
    assert result["completed"]["status"] == "completed"
    assert result["completed"]["lastAssistantText"] == "final answer"
    assert result["completed"]["error"] == ""


def test_transport_failure_is_terminal_and_reported_once(tmp_path):
    result = run_node_harness(tmp_path, scenario="exit_during_call", action="terminal_call")

    assert result["callError"]["name"] == "BridgeTerminalError"
    assert "exit code 9" in result["callError"]["message"]
    assert result["state"]["status"] == "error"
    assert "exit code 9" in result["state"]["error"]
    assert result["statuses"] == [
        {"key": "cao-pi-mcp", "text": None},
        {"key": "cao-pi-mcp", "text": "MCP error: Pi MCP proxy exited with exit code 9"},
    ]


def _installed_pi() -> str | None:
    return os.environ.get("CAO_TEST_PI_BIN") or shutil.which("pi")


@pytest.mark.skipif(_installed_pi() is None, reason="installed Pi is required for load probe")
def test_installed_pi_loads_extension_without_model_or_ambient_resources(tmp_path):
    """Pi 0.84.1 initializes the explicit extension and publishes exact idle state."""
    state_file = tmp_path / "state" / "pi.json"
    config_file = tmp_path / "mcp.json"
    config_file.write_text(json.dumps({"terminalId": "probe", "servers": {}}))
    pi_config_dir = tmp_path / "pi-config"
    env = {
        **os.environ,
        "CAO_PI_STATE_FILE": str(state_file),
        "CAO_PI_MCP_CONFIG": str(config_file),
        "CAO_PI_BRIDGE_PYTHON": sys.executable,
        "PI_CODING_AGENT_DIR": str(pi_config_dir),
        "PI_OFFLINE": "1",
    }

    result = subprocess.run(
        [
            _installed_pi(),
            "--mode",
            "rpc",
            "--no-session",
            "--offline",
            "--no-approve",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
            "--extension",
            str(pi_extension_path()),
        ],
        cwd=tmp_path,
        env=env,
        input='{"id":"probe","type":"get_state"}\n',
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert any(
        response.get("id") == "probe" and response.get("success") is True for response in responses
    )
    state = json.loads(state_file.read_text())
    assert set(state) == {"status", "lastAssistantText", "error", "updatedAt"}
    assert state["status"] == "idle"
    assert state["lastAssistantText"] == ""
    assert state["error"] == ""
    assert isinstance(state["updatedAt"], str) and state["updatedAt"]
    assert stat.S_IMODE(state_file.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
    assert list(state_file.parent.glob("*.tmp")) == []
