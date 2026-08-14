"""workflow — parameterized review pipeline: sequential plan, then concurrent
fan-out (issue #591, main-gallery example for the `cao workflow` lifecycle).

Demonstrates the full `cao_workflow` script contract in one small pipeline:
typed `INPUTS` read via `get_inputs()`, a sequential `run_step`, a
`ThreadPoolExecutor` fan-out with an explicit, stable `step_id` per unit
(the shape used in docs/examples/fanout_example.py), per-unit `ShimHTTPError`
tolerance so one failed check never loses the others, and a structured
`emit_output()` result.

Run standalone via `cao workflow run workflow --run-id <id> --input target=<name>`
after copying this file to `~/.aws/cli-agent-orchestrator/workflows/workflow.py`
— see examples/workflow/README.md for the full lifecycle (validate/run/status/
cancel) and examples/workflow/run.sh for a non-interactive entry point.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from cao_workflow import ShimHTTPError, emit_output, get_inputs, run_step

# Typed runtime inputs (read via get_inputs() below). Field values must be AST
# literals — the server's static loader extracts this dict without executing
# the script — so `concurrency`'s cap is re-read from THIS dict at runtime
# (below) rather than duplicated into a second constant.
INPUTS = {
    "target": {"type": "string", "required": True},
    "concurrency": {"type": "int", "required": False, "default": 2},
    "strict": {"type": "bool", "required": False, "default": False},
}

# Fixed check catalogue for the fan-out — one concurrent run_step per entry,
# same shape as fanout_example.py's SHARDS list.
CHECKS = ["style", "security", "performance"]


def _tone(strict: bool) -> str:
    return "flag every deviation, however minor" if strict else "flag only significant issues"


def _plan(target: str, tone: str) -> str:
    """Sequential step: a short review plan for ``target``, run before the fan-out."""
    handle = run_step(
        "claude_code",
        "reviewer",
        f"Draft a one-line review plan for '{target}'. {tone}. Return the plan only.",
        step_id=f"plan:{target}",
    )
    return handle.output


def _run_check(target: str, check: str, tone: str):
    """One concurrent fan-out unit. Explicit, stable step_id (fan-out rule) —
    derived from ``target`` + ``check``, never a bare counter.

    Per-unit fault tolerance: a ShimHTTPError (the shim's HTTP error type)
    turns a failed call into a missing result for THIS check only — the
    other checks, and the run itself, still complete.
    """
    try:
        handle = run_step(
            "claude_code",
            "reviewer",
            f"Review '{target}' for {check} issues. {tone}. Return findings only.",
            step_id=f"check:{target}:{check}",
        )
        return check, handle.output
    except ShimHTTPError:
        return check, None


def main() -> None:
    inputs = get_inputs()
    target = inputs["target"]
    strict = inputs.get("strict", False)
    tone = _tone(strict)

    # Conservative concurrency: never exceed the declared default, even when a
    # run asks for more (measured guidance for claude_code fan-out — see the
    # authoring guide's fan-out section and the cao-workflow skill's R1).
    max_workers = min(inputs.get("concurrency", 2), INPUTS["concurrency"]["default"])

    plan = _plan(target, tone)

    results = {}
    failed_checks = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_run_check, target, check, tone) for check in CHECKS]
        for future in as_completed(futures):
            check, output = future.result()
            if output is None:
                failed_checks.append(check)
            else:
                results[check] = output

    emit_output(
        {
            "target": target,
            "strict": strict,
            "max_workers": max_workers,
            "plan": plan,
            "checks": results,
            "failed_checks": failed_checks,
        }
    )


if __name__ == "__main__":
    main()
