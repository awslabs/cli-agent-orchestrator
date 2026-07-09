"""Loop shape — raw HTTP, NO shim (Q2=A shim-optionality proof, FR-6.1).

Demonstrates that the shim is a convenience, not a requirement: this script
reads the SAME three identity env vars ``cao workflow run`` injects and posts
to ``/terminals/run-step`` with stdlib ``urllib`` directly, with no
``cao_workflow`` import at all. Compare against ``loop_example.py`` — same
shape, same env contract, no shim.

Run standalone via ``cao workflow run --script docs/examples/loop_raw_http_example.py``.
"""

from __future__ import annotations

import json
import os
from urllib.request import Request, urlopen

ITERATIONS = 3


def main() -> None:
    run_id = os.environ["CAO_WORKFLOW_RUN_ID"]
    generation = os.environ["CAO_WORKFLOW_GENERATION"]
    base_url = os.environ["CAO_API_BASE_URL"]

    outputs = []
    for i in range(ITERATIONS):
        body = {
            "provider": "kiro_cli",
            "agent": "reviewer",
            "prompt": f"summarize item {i}",
            "env_vars": {
                "CAO_WORKFLOW_RUN_ID": run_id,
                "CAO_WORKFLOW_GENERATION": generation,
                "CAO_WORKFLOW_STEP_ID": f"call-{i + 1}",
            },
        }
        request = Request(
            f"{base_url}/terminals/run-step",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=630.0) as response:
            data = json.loads(response.read().decode("utf-8"))
        outputs.append(data["last_message"])

    print(f"CAO_WORKFLOW_OUTPUT:{json.dumps({'iterations': ITERATIONS, 'outputs': outputs})}")


if __name__ == "__main__":
    main()
