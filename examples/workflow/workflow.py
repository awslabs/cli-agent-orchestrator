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

import hashlib
import re
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

# /terminals/run-step validates CAO_WORKFLOW_STEP_ID against this exact
# charset (api/main.py's RunStepRequest.validate_env_vars, which checks every
# workflow env-var value against constants.WORKFLOW_NAME_RE) — `target` is
# an arbitrary author-supplied string, so it must be sanitized before it can
# be embedded in a step_id.
_STEP_ID_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")

# WORKFLOW_NAME_RE bounds length as well as charset: 1-64 characters. The
# longest step_id this script emits is "check-" + slug + "-" + the longest
# CHECKS entry, so that fixed cost is what the slug has to fit inside.
# Deriving the budget from CHECKS rather than hardcoding it keeps the bound
# correct if the catalogue grows.
_STEP_ID_MAX = 64
_SLUG_MAX = _STEP_ID_MAX - len("check--") - max(len(c) for c in CHECKS)

# Spent only on the truncation path (see _slug). sha256 and not the builtin
# hash(): hash() is salted per interpreter, and a step_id that changes between
# processes would break replay, which keys on the step_id.
_SLUG_HASH_LEN = 7
_SLUG_KEEP = _SLUG_MAX - 1 - _SLUG_HASH_LEN


def _slug(value: str) -> str:
    """Map ``value`` onto the charset and length CAO_WORKFLOW_STEP_ID requires.

    A value that already fits is only charset-mapped, so ordinary targets keep a
    readable step_id. A value that would overflow the budget is truncated and
    given a short digest of the original, because bare truncation collapses
    every long target sharing a prefix onto a single step_id — and in a fan-out
    a silently reused step_id is a correctness bug, not a cosmetic one. This
    file is the copy-paste template for other people's workflows, so it ships
    the collision-safe shape.

    Note this bounds the truncation path only. Two distinct targets can still
    map to one slug at short lengths (``a b`` and ``a/b`` both give ``a_b``);
    that is inherent to charset mapping and unchanged here.
    """
    mapped = _STEP_ID_UNSAFE.sub("_", value)
    if len(mapped) <= _SLUG_MAX:
        return mapped
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:_SLUG_HASH_LEN]
    return f"{mapped[:_SLUG_KEEP]}-{digest}"


def _tone(strict: bool) -> str:
    return "flag every deviation, however minor" if strict else "flag only significant issues"


def _plan(target: str, tone: str) -> str:
    """Sequential step: a short review plan for ``target``, run before the fan-out."""
    handle = run_step(
        "claude_code",
        "reviewer",
        f"Draft a one-line review plan for '{target}'. {tone}. Return the plan only.",
        step_id=f"plan-{_slug(target)}",
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
            step_id=f"check-{_slug(target)}-{check}",
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
