"""A /v1/systemone typed-decision server over llama-server, for JevK5-family GGUF models.

    llama-server -m models/jevk5-4b-v0.3-Q8_0.gguf -c 8192 -ngl 99 --host 127.0.0.1 --port 8080
    PYTHONPATH=vendor/jevk5 python3 serve_gguf.py --name jevk5-4b-v0.3-q8 --temperature 1.367

The same request and response shape as the jevk5 runtime's own jevk5-serve (model, answers, usage,
latency_ms), plus GET /v1/models and /health. The GGUF path needs only the standard library.
It binds to loopback only: reach a remote host through an SSH tunnel, never a public port.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from jevk5.gguf import JevK5GGUF

MAX_BODY = 256 * 1024


def normalized(question: dict) -> dict:
    # Mirrors jevk5.server.normalized, which can't be imported without torch.
    if question.get("type") not in ("noul", "choice", "score"):
        raise ValueError(f"unknown question type {question.get('type')!r}")
    if "instructions" not in question:
        raise ValueError("question is missing instructions")
    criteria = question.get("criteria")
    if question["type"] == "choice":
        if isinstance(criteria, list):
            criteria = dict.fromkeys(criteria)
        if not isinstance(criteria, dict) or len(criteria) < 2:
            raise ValueError("choice criteria must name at least two options")
    if question["type"] == "score" and (not isinstance(criteria, list) or len(criteria) < 2):
        raise ValueError("score criteria must list at least two levels")
    return {**question, "criteria": criteria}


def make_handler(model: JevK5GGUF, name: str, activity: Path | None = None):
    lock = threading.Lock()  # the client keeps per-call state; llama-server runs one slot anyway

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            path = self.path.rstrip("/")
            if path == "/health":
                self._send(200, {"ok": True, "model": name})
            elif path == "/v1/models":
                self._send(200, {"object": "list", "data": [{"id": name, "object": "model"}]})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path.rstrip("/") != "/v1/systemone":
                self._send(404, {"error": "not found"})
                return
            started = time.perf_counter()
            if activity is not None:
                activity.touch()  # read by the host's idle auto-stop
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length > MAX_BODY:
                    raise ValueError("request body too large")
                body = json.loads(self.rfile.read(length))
                with lock:
                    answers = {
                        qid: model.decide(body["state"], normalized(q))
                        for qid, q in body["questions"].items()
                    }
            except (ValueError, KeyError, TypeError) as error:
                self._send(400, {"error": str(error)})
                return
            except OSError as error:
                self._send(502, {"error": f"llama-server unavailable: {type(error).__name__}"})
                return
            tokens = sum(a.pop("input_tokens") for a in answers.values())
            self._send(
                200,
                {
                    # The served model, not the caller's alias, so records name what answered.
                    "model": name,
                    "answers": answers,
                    "usage": {"input_tokens": tokens, "output_tokens": 0},
                    "latency_ms": round((time.perf_counter() - started) * 1e3, 2),
                },
            )

        def log_message(self, format: str, *args: object) -> None:
            pass

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="/v1/systemone typed decisions over llama-server")
    parser.add_argument("--name", required=True, help="model name reported in responses")
    parser.add_argument("--temperature", type=float, required=True, help="the GGUF's calibration T")
    parser.add_argument("--llama-url", default="http://127.0.0.1:8080")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--activity-file", type=Path, default=None, help="touched on each request")
    args = parser.parse_args(argv)
    if not ipaddress.ip_address(args.host).is_loopback:
        parser.error("--host must be a loopback address; use an SSH tunnel for remote access")
    model = JevK5GGUF(args.llama_url, temperature=args.temperature, timeout_s=60.0)
    server = ThreadingHTTPServer(
        (args.host, args.port), make_handler(model, args.name, args.activity_file)
    )
    print(
        f"serving {args.name} (T={args.temperature}) on http://{args.host}:{args.port}", flush=True
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
