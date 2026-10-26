"""Send a few typed decisions to a /v1/systemone endpoint and print the probabilities and latency.

    python3 check.py                          # the local shim, 127.0.0.1:8090
    python3 check.py http://127.0.0.1:18090   # the AWS host, through the SSH tunnel

Loopback only: a remote host is reached through the tunnel, never a public address.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

MODEL_ROUTE = {
    "type": "choice",
    "instructions": "What model size does this task need? Pick the smallest model that will do this task well.",
    "criteria": {
        "small": "Mechanical or narrow work with little ambiguity: formatting, renaming, simple lookups.",
        "medium": "Ordinary engineering work: a contained bug fix, tests for known behaviour, a focused review.",
        "large": "Open-ended or high-stakes work: design across components, subtle bugs, security review.",
        "unsure": "The task text does not say enough to tell.",
    },
}

CASES = [
    (
        "rename",
        "Rename parse_cfg to parse_config in src/settings.py and update its three call sites.",
    ),
    ("typo", "Fix the typo 'recieve' in docs/usage.md. Make no other edits."),
    (
        "design",
        "Design how session state should be shared between the API server and the scheduler, "
        "covering crash recovery and concurrent writers, and list the risks.",
    ),
]


def main() -> int:
    url = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8090").rstrip("/")
    if urlparse(url).hostname not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("use a loopback URL; reach a remote host through the SSH tunnel")
    try:
        with urllib.request.urlopen(url + "/v1/models", timeout=10) as response:
            print("model:", json.loads(response.read())["data"][0]["id"])
    except urllib.error.URLError as error:
        raise SystemExit(
            f"nothing answers at {url} ({error.reason}): start it with ./serve.sh start, "
            "or open the tunnel with aws/cli/ssh.sh --tunnel (a stopped host must be started first)"
        ) from None
    for label, task in CASES:
        body = {"state": {"task": task}, "questions": {"model_route": MODEL_ROUTE}}
        request = urllib.request.Request(
            url + "/v1/systemone",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        started = time.perf_counter()
        with urllib.request.urlopen(request, timeout=60) as response:
            answer = json.loads(response.read())["answers"]["model_route"]
        elapsed = (time.perf_counter() - started) * 1000
        ranked = sorted(answer["probabilities"].items(), key=lambda item: -item[1])
        probs = ", ".join(f"{option} {p:.2f}" for option, p in ranked)
        print(f"{label:7s} -> {answer['choice']:6s} ({elapsed:4.0f} ms)  {probs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
