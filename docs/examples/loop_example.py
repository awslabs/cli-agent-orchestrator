"""Loop shape — shim-based (M1, FR-7.1).

Runs the same agent step N times, once per iteration. Sequential calls with
no explicit ``step_id`` are safe: the shim's lock-guarded counter assigns
``call-1``, ``call-2``, ... in program order (ADR-10's sequential case).

Run standalone by copying it into the workflows directory and running it by its
stem — ``cao workflow run`` resolves a bare name, and rejects a path outside
that directory::

    cp docs/examples/loop_example.py ~/.aws/cli-agent-orchestrator/workflows/
    cao workflow validate ~/.aws/cli-agent-orchestrator/workflows/loop_example.py
    cao workflow run loop_example
"""

from __future__ import annotations

from cao_workflow import emit_output, run_step

ITERATIONS = 3


def main() -> None:
    outputs = []
    for i in range(ITERATIONS):
        handle = run_step("kiro_cli", "reviewer", f"summarize item {i}")
        outputs.append(handle.output)
    emit_output({"iterations": ITERATIONS, "outputs": outputs})


if __name__ == "__main__":
    main()
